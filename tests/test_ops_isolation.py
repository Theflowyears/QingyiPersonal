# -*- coding: utf-8 -*-
"""运维模块的结构性隔离锁。

功能测试能证明"这次没碰人设"，但证明不了"下次改代码也不会碰"。这里用 AST 钉住
四件容易在后续维护中被静默破坏的事：

1. 拦截顺序：ops 必须在 handle_private_message **之前**。顺序反了，功能测试
   照样可能过（因为 mock 掉了人格处理），但线上她的记忆会被运维指令污染。
2. 模块边界：运维模块不许引用人格发话/观测入口，也不许 import 人格写入库。
3. worker 真的注册了：没注册的 worker 和挂掉的 worker 在日志里长得一样。
4. 远端函数白名单真实存在且是同步函数：白名单里写个不存在的名字，
   调用会静默失败，看起来像"主人没批过"。

扫字符串一律用 AST 且跳过 docstring —— 说明文字里本来就要写着那些被禁的名字。
"""
import ast
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")

OPS_MODULES = ("ops_channel.py", "ops_remote.py", "ops_autopilot.py", "ops_dispatch.py")

# 人格链路的入口：运维模块碰任何一个都算越界
FORBIDDEN_REFS = ("send_private_msg", "handle_private_message", "_record_private_observation",
                  "ask_openclaw", "handle_group_message", "get_system_prompt")
# 人格写入库：运维文本绝不能流进它们
FORBIDDEN_IMPORTS = ("self_model", "persona_store", "persona_growth", "trigger_ledger",
                     "proactive_dm", "curiosity", "knowledge_base", "member_memory",
                     "group_memory_store", "custom_memory", "emotion_core", "emotion_dynamics")


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _tree(name):
    return ast.parse(_read(os.path.join(BOT, name)))


def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _code_string_literals(tree):
    skip = _docstring_nodes(tree)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip]


class InterceptionOrderTests(unittest.TestCase):
    def test_ops_intercepts_before_persona_private_handler(self):
        tree = _tree("qq_openclaw_bridge.py")
        handler = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_event")
        private_branch = None
        for node in ast.walk(handler):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
                left = node.test.left
                if isinstance(left, ast.Name) and left.id == "message_type":
                    for comp in node.test.comparators:
                        if isinstance(comp, ast.Constant) and comp.value == "private":
                            private_branch = node
        self.assertIsNotNone(private_branch, "handle_event 里找不到私聊分支")
        calls = []
        for node in ast.walk(private_branch):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    calls.append((node.lineno, func.attr))
                elif isinstance(func, ast.Name):
                    calls.append((node.lineno, func.id))
        intercept = [ln for ln, name in calls if name == "intercept"]
        persona = [ln for ln, name in calls if name == "handle_private_message"]
        self.assertTrue(intercept, "私聊分支里没有 ops 拦截")
        self.assertTrue(persona, "私聊分支里没有人格处理")
        self.assertLess(min(intercept), min(persona),
                        "ops 拦截漂到 handle_private_message 之后了 —— "
                        "运维指令会被写进她的人格观测账")

    def test_interception_is_not_swallowed_into_persona(self):
        """拦截返回 True 必须直接 return，不许继续往下走人格处理。"""
        src = _read(os.path.join(BOT, "qq_openclaw_bridge.py"))
        start = src.index("async def handle_event")
        seg = src[start:start + 2000]
        i = seg.index("ops_channel.intercept")
        window = seg[i:i + 200]
        self.assertIn("return", window, "拦截命中后没有立刻 return")


class ModuleBoundaryTests(unittest.TestCase):
    def test_ops_modules_do_not_reference_persona_entrypoints(self):
        offenders = []
        for name in OPS_MODULES:
            tree = _tree(name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in FORBIDDEN_REFS:
                    offenders.append("%s:%d %s" % (name, node.lineno, node.id))
                if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_REFS:
                    offenders.append("%s:%d .%s" % (name, node.lineno, node.attr))
        self.assertEqual(offenders, [], "运维模块引用了人格链路入口：\n" + "\n".join(offenders))

    def test_ops_modules_do_not_import_persona_writers(self):
        offenders = []
        for name in OPS_MODULES:
            tree = _tree(name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in FORBIDDEN_IMPORTS:
                            offenders.append("%s:%d import %s" % (name, node.lineno, alias.name))
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.split(".")[0] in FORBIDDEN_IMPORTS:
                        offenders.append("%s:%d from %s" % (name, node.lineno, node.module))
        self.assertEqual(offenders, [], "运维模块 import 了人格写入库：\n" + "\n".join(offenders))

    def test_autopilot_kernel_does_not_deploy_by_itself(self):
        """部署必须由"本地全量测试绿了"之后的判断触发，不能写死在确定性内核里。"""
        src = _read(os.path.join(BOT, "ops_autopilot.py"))
        for token in ("_deploy_qingyi_release", "sync_to_cloud", "systemctl"):
            self.assertNotIn(token, src,
                             "决策内核里出现了自行部署/重启的捷径：%s" % token)

    def test_ops_channel_sends_via_raw_onebot(self):
        tree = _tree("ops_channel.py")
        actions = [s for s in _code_string_literals(tree) if s == "send_private_msg"]
        self.assertTrue(actions, "运维发话没有走裸 OneBot send_private_msg 动作")


class WorkerRegistrationTests(unittest.TestCase):
    def test_outbox_worker_is_registered(self):
        src = _read(os.path.join(BOT, "ceylan_runtime.py"))
        self.assertIn("ns['ops_outbox_worker']", src,
                      "outbox worker 没注册 —— 报文会永远躺在账上没人发")

    def test_bridge_exposes_outbox_worker(self):
        tree = _tree("qq_openclaw_bridge.py")
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
        self.assertIn("ops_outbox_worker", names)


class RemoteWhitelistTests(unittest.TestCase):
    def test_whitelisted_funcs_exist_and_are_sync(self):
        import inspect
        sys.path.insert(0, BOT)
        import ops_channel
        import ops_remote
        bad = []
        for name in ops_remote.ALLOWED_REMOTE_FUNCS:
            func = getattr(ops_channel, name, None)
            if func is None:
                bad.append(name + ":missing")
            elif not callable(func):
                bad.append(name + ":not_callable")
            elif inspect.iscoroutinefunction(func):
                bad.append(name + ":async_not_supported")
        self.assertEqual(bad, [], "远端白名单指向不存在/不可调用/异步的函数：%s" % bad)

    def test_whitelist_rejects_arbitrary_names(self):
        sys.path.insert(0, BOT)
        import ops_remote
        for name in ("__import__", "eval", "exec", "system", "save_round_any", "os.system"):
            self.assertNotIn(name, ops_remote.ALLOWED_REMOTE_FUNCS)

    def test_call_ops_refuses_before_connecting(self):
        """不在白名单里的名字必须在建连接之前就被拒，不该产生任何远端执行。"""
        sys.path.insert(0, BOT)
        import ops_remote
        remote = ops_remote.Remote.__new__(ops_remote.Remote)
        remote._client = None
        res = remote.call_ops("eval", {"payload": "1+1"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "func_not_allowed")


class VoiceRegisterTests(unittest.TestCase):
    """运维报文是系统通道不是她本人，但同样不许文言腔/客服腔。"""

    def test_no_classical_chinese_in_ops_messages(self):
        banned = ("吾", "汝", "矣", "焉", "领命", "下新令", "辞多不尽", "姑止于此")
        offenders = []
        for name in OPS_MODULES:
            for literal in _code_string_literals(_tree(name)):
                for token in banned:
                    if token in literal:
                        offenders.append("%s: %s" % (name, token))
        self.assertEqual(offenders, [], "运维文案出现文言腔：%s" % offenders)

    def test_no_customer_service_tone(self):
        banned = ("请注意", "建议您", "为您查到", "感谢您的耐心等待")
        offenders = []
        for literal in _code_string_literals(_tree("ops_channel.py")):
            for token in banned:
                if token in literal:
                    offenders.append(token)
        self.assertEqual(offenders, [], "运维文案出现客服腔：%s" % offenders)


if __name__ == "__main__":
    unittest.main()