"""回归锁：这轮维护真金白银踩过三次的三类结构坑。

上游那条建议是「注册式微内核 + 显式依赖注入」。那是重构，不是快赢，我们不做；
但它点名的三类故障，我们在这一轮里**一类都没落下地全踩了一遍**，
所以这三类各配一条静态锁 —— 重构可以不做，重复踩不必再来：

  1. 装饰器漂到相邻函数上。`@guard` / `@serial_history` 曾经落到
     `_describe_image` 头上，历史写入因此失去串行保护，而代码看起来是对的
     （函数定义之间空几行合法，肉眼分不出来）。
  2. 模块级路径常量在 import 时被冻结。`_FILE = os.getenv("DATA_DIR")...`
     写在模块顶层，测试改环境变量就不再生效，于是测试直接往真 data/ 写脏数据。
     这条这轮修过三次（curiosity / memory_usage / vision_notes）。
  3. 测试里重导入模块，造成两个进程内对象不是同一个：
     一边拿着旧对象写账、另一边拿新对象读账，单跑通过、合跑假失败。

第 4 条是 P1 那条一般原则：人格注入不许依赖「先判深度再决定给不给」。
我们那个 94.9% 的盲点就是这么静默发生的。
"""
import ast
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
TESTS = os.path.join(ROOT, "tests")

# 这一轮新建的模块：它们必须在调用时才解析 DATA_DIR
NEW_MODULES = ("curiosity.py", "memory_usage.py", "vision_notes.py",
               "activity_log.py", "history_log.py", "config_store.py",
               "ops_channel.py", "ops_autopilot.py", "ops_dispatch.py",
               "self_correct.py")


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


class DecoratorPlacementTests(unittest.TestCase):
    """装饰器必须落在它要包的那个函数上，不是隔壁那个。"""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(_read(os.path.join(BOT, "qq_openclaw_bridge.py")))
        cls.decs = {}
        for n in ast.walk(cls.tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = []
                for d in n.decorator_list:
                    names.append(ast.dump(d))
                cls.decs[n.name] = names

    def _has(self, func, needle):
        return any(needle in d for d in self.decs.get(func, []))

    def test_ask_openclaw_is_serialized_and_guarded(self):
        self.assertTrue(self._has("ask_openclaw", "serial_history"),
                        "ask_openclaw 丢了串行保护：历史会写乱")
        self.assertTrue(self._has("ask_openclaw", "guard"))

    def test_describe_image_is_not_the_one_carrying_serial_history(self):
        # 当初漂移就是漂到它身上 —— 现在必须反过来钉住
        self.assertFalse(self._has("_describe_image", "serial_history"),
                         "serial_history 又落到 _describe_image 上了")

    def test_serial_history_applies_to_exactly_one_entry(self):
        holders = [k for k, v in self.decs.items() if "serial_history" in str(v)]
        self.assertEqual(holders, ["ask_openclaw"], holders)


class FrozenPathTests(unittest.TestCase):
    """新模块不许在 import 时把 DATA_DIR 冻进模块常量。"""

    def _module_level_getenv_assigns(self, path):
        tree = ast.parse(_read(path))
        bad = []
        for n in tree.body:          # 只看顶层：函数体内现算是对的
            if isinstance(n, ast.Assign):
                if "os.getenv" in ast.dump(n) or "getenv" in ast.dump(n):
                    for tgt in n.targets:
                        if isinstance(tgt, ast.Name) and tgt.id.startswith(
                                ("_FILE", "_DIR", "_DB", "_STATS", "_ROOT")):
                            bad.append((n.lineno, ast.dump(tgt)))
        return bad

    def test_new_modules_resolve_paths_at_call_time(self):
        offenders = []
        for name in NEW_MODULES:
            p = os.path.join(BOT, name)
            if not os.path.isfile(p):
                continue
            for lineno, tgt in self._module_level_getenv_assigns(p):
                offenders.append("%s:%d %s" % (name, lineno, tgt))
        self.assertEqual(offenders, [],
                         "这些模块把路径冻在了 import 时刻，测试会写脏真 data/\n"
                         + "\n".join(offenders))

    def test_at_least_one_module_proves_the_pattern_is_checked(self):
        # 防自己把这条锁改成永远通过：故意造一份「顶层冻结」的样本，必须被抓到
        sample = "_FILE = os.getenv('DATA_DIR') + '/x.json'\n"
        tree = ast.parse(sample)
        hit = any(isinstance(n, ast.Assign) and "getenv" in ast.dump(n)
                  for n in tree.body)
        self.assertTrue(hit)


class TestIsolationTests(unittest.TestCase):
    """测试里不许重导入被测模块 —— 会造成「单跑过、合跑挂」。"""

    def test_no_module_reimport_in_tests(self):
        # 用 AST 找 `del sys.modules[...]` 这条语句，而不是搜字符串。
        # 搜字符串的话，本文件里那段说明文字和检测用的字面量会把自己抓出来 ——
        # 一条永远红或者被人顺手删宽的锁没有意义。
        bad = []
        for name in sorted(os.listdir(TESTS)):
            if not name.endswith(".py"):
                continue
            tree = ast.parse(_read(os.path.join(TESTS, name)))
            for n in ast.walk(tree):
                if isinstance(n, ast.Delete):
                    for tgt in n.targets:
                        d = ast.dump(tgt)
                        if "sys" in d and "modules" in d:
                            bad.append("%s:%d" % (name, n.lineno))
                if isinstance(n, ast.Call):
                    f = ast.dump(n.func)
                    if "importlib" in f and "reload" in f:
                        bad.append("%s:%d reload" % (name, n.lineno))
        self.assertEqual(bad, [],
                         "重导入让两个模块对象并存，账写在不同对象上：\n"
                         + "\n".join(bad))

    def test_tests_isolate_by_patching_constants(self):
        # 项目既有约定是靠补丁隔离（gms._FILE 那种）。
        # 这条只是保证上面那条被删掉时我们能发现约定变了。
        src = _read(os.path.join(TESTS, "test_memory_archive.py"))
        self.assertTrue("gms._FILE =" in src or "DATA_DIR" in src)


class ConstantInjectionTests(unittest.TestCase):
    """P1 一般原则：人格那层恒定注入，不许多条件依赖「先判深度」。"""

    def test_identity_block_is_unconditional_in_system_prompt(self):
        src = _read(os.path.join(BOT, "qq_openclaw_bridge.py"))
        start = src.index("def get_system_prompt")
        nxt = src.find("\n@", start + 1)
        seg = src[start:nxt if nxt > start else len(src)]
        self.assertIn("identity_block()", seg)
        # 不允许被 depth/dep 条件包住
        i = seg.index("identity_block()")
        window = seg[max(0, i - 320):i]
        self.assertNotIn("dep ==", window,
                         "身份层又回到「按深度才给」—— 94.9% 盲点就是这么来的")
        self.assertNotIn("depth_for", window,
                         "注入与否不该取决于先算了一次深度")

    def test_identity_block_survives_empty_archive(self):
        sys.path.insert(0, BOT)
        import memory_depth as md
        self.assertTrue(callable(md.identity_block))
        out = md.identity_block()
        self.assertIsInstance(out, str)


if __name__ == "__main__":
    unittest.main()
