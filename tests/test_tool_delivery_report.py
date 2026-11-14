"""她答应办的事必须交货，而且交货可以说两三百字。

主人报的两件事，生产日志里都找得到现场：

    [Agent] sesn_01M6VHFMW4EGS 用时 105.6s 图 0 音频 0 文本 1970 字
    [工具] tarot ok 105.6s 塔罗牌抽取完成。先说明两件事：
    [后台查询] 对话已有新轮次，不补过时结果

牌算完了，1970 字，然后被自己扔了。原因：`_TURN_VERSIONS` 每来一条群消息就 +1，
而 105 秒里群里不可能没人说话 —— 于是**任何**耗时工具的交货必然「过时」。
那道闸门本来是给「她顺嘴查了个小东西」用的，不该管「有人点名让她办事」。

查资料同理：`[搜索] 她判断这句不用查，不联网` 今天出现了十几条，
其中就有主人明确说「去查 XXX」的那些 —— 关键词判定一弃权，整件事就当没发生过。

还有长度：就算补上了，交货也要过水群包装器 —— 生产 settings.json 现在是
45 字 × 3 条 = 135 字，`reply_tokens` 还把一次生成的 token 掐在 150。
1970 字的牌面进来，出去就剩一句。主人说两三百字可以接受、别硬截、也别超 300。
所以交货单独一档：`reply_chars_report` × `bubbles_report`（默认 200×2），
总长由 `report_max_chars` 封顶在 300，超了就在句子边界收住，不切在半句话上。
"""
import asyncio
import io
import json
import os
import sys
import tempfile
import time
import unittest
from collections import Counter, deque
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import ceylan_control_runtime as cr        # noqa: E402
import qq_openclaw_bridge as b             # noqa: E402

TAROT = "宝宝帮我抽两张塔罗牌呗，解析详细一点~"
LOOKUP = "清清你去查一下虎牙伯千和灰烬alex到底啥关系"
_REAL_SLEEP = asyncio.sleep
CARDS = ("宝剑三 逆位：一段勉强维持的体面，两个人都心里有数，谁也不肯先说破。\n"
         "星币二 正位：钱和精力同时托着两摊，眼下没塌，但那是硬撑出来的平衡。\n"
         "星星 正位：熬过这一段是真的能看见光的，不是安慰话。")


async def _fast_sleep(_seconds):
    # 不能用 `lambda s: asyncio.sleep(0)` —— 那打的是 asyncio 模块本身，
    # 替身里再调 asyncio.sleep 就是调替身自己，直接递归爆栈。
    return await _REAL_SLEEP(0)


class OwedRequestTests(unittest.TestCase):
    def test_explicit_tool_requests_are_owed(self):
        for t in (TAROT, "清清宝宝，帮我算下塔罗牌", "快帮阿甲算下今天她抽卡可以出六星吗",
                  LOOKUP, "帮我查查明日方舟Mon3tr是什么", "你搜一下滔博夺冠了吗",
                  "今天天气怎么样"):
            self.assertTrue(b.is_owed_request(t), t)

    def test_chatter_is_not_owed(self):
        for t in ("甜甜甜", "太高冷了我聊不起来呜呜", "哦", "[图片]", "呜呜呜呜呜",
                  "感觉还是", "在门口记得捡一下"):
            self.assertFalse(b.is_owed_request(t), t)

    def test_a_question_is_owed_too(self):
        # 「滔博夺冠了吗」不是祈使句，但她要答就得先查，查了就得回来交代
        self.assertTrue(b.is_owed_request("滔博夺冠了吗"))


class SearchStillRunsWhenSheHesitatesTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_lookup_falls_back_to_the_question(self):
        """她判「不用查」不能把主人的明确指令一起判掉。"""
        with patch.object(b, "_her_search_keywords", new=AsyncMock(return_value="")):
            q = await b.search_query_or_fallback(LOOKUP, "主人", context="")
        self.assertTrue(q)
        self.assertIn("虎牙伯千", q)

    async def test_chatter_still_gets_no_query(self):
        with patch.object(b, "_her_search_keywords", new=AsyncMock(return_value="")):
            self.assertEqual(await b.search_query_or_fallback("哦", "主人", context=""), "")

    def test_explicit_lookup_is_recognised_even_when_intent_says_request(self):
        # 「帮我查查 X」被 _classify_intent 判成 request，以前正好绕开搜索的 spawn
        self.assertTrue(b._explicit_lookup("帮我查查明日方舟Mon3tr是什么"))
        self.assertFalse(b._explicit_lookup("今天天气真好"))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    """`_lookup_then_reply` 的后半段：拿到东西以后怎么回去。"""

    def setUp(self):
        self.gid = "771166"
        self.key = "group-" + self.gid
        # 「同一件事只交一次」是进程级台账，测试之间必须清干净，
        # 否则前一个用例交过货，后面的用例全被自己拦掉。
        for d in (b._OWED_DELIVERED, b._TURN_VERSIONS, b.group_message_cache):
            self.addCleanup(d.clear)
        b._OWED_DELIVERED.clear()
        b._TURN_VERSIONS.pop(self.key, None)
        b.group_message_cache[self.gid] = deque(maxlen=200)

    async def _run(self, text=TAROT, owed=True, version_bump=True, tool_texts=(CARDS,),
                    reply="刚抽完了，两张都落在事业这摊上，先说宝剑三。", mention=False,
                    chat_fast=True):
        sent = []
        calls = {"tool": [], "mention": [], "timeout": [], "kwargs": [], "tool_scope": []}
        results = [{"ok": True, "text": t, "seconds": 105.6, "images": [], "audio": [],
                    "error": ""} for t in tool_texts]

        async def fake_group(ws, gid, uid, body, mention=False, **kw):
            sent.append({"text": body, "uid": uid})
            calls["mention"].append(mention)
            return True

        async def fake_tool(key, question, context="", **scope):
            calls["tool_scope"].append(scope)
            calls["tool"].append(question)
            return results[min(len(calls["tool"]) - 1, len(results) - 1)]

        gen = AsyncMock(return_value=reply)
        said = {"text": "等我看看牌", "delivered": True, "version": 7}
        versions = {self.key: 8 if version_bump else 7}

        import toolkit
        with patch.dict(b._TURN_VERSIONS, versions, clear=False), \
                patch.object(b, "_lookup_is_owed", return_value=owed), \
                patch.object(b, "_tool_plan", return_value={"cloud": owed, "tool": "tarot"}), \
                patch.object(b, "get_system_prompt", lambda: "人设"), \
                patch.object(b, "call_openclaw", new=gen), \
                patch.object(b, "send_group_msg", new=fake_group), \
                patch.object(b, "_BRIDGE_WS", {"ws": object()}), \
                patch.object(b.asyncio, "sleep", new=_fast_sleep), \
                patch.object(toolkit, "run_agent_tool", new=fake_tool):
            await b._lookup_then_reply(self.key, text, self.gid, "9000000004",
                                       "阿戊", "divine", {}, said=said)
        if gen.call_args:
            calls["kwargs"] = [gen.call_args.kwargs]
            calls["timeout"] = [gen.call_args.kwargs.get("timeout")]
        return sent, gen, calls

    async def test_owed_delivery_is_sent_even_after_new_turns(self):
        sent, _gen, _calls = await self._run()
        self.assertEqual(len(sent), 1, "答应了 105 秒，结果一个字也没回去")
        self.assertIn("宝剑三", sent[0]["text"])

    async def test_owed_delivery_reaches_the_original_asker(self):
        sent, _, _ = await self._run()
        self.assertEqual(sent[0]["uid"], "9000000004")

    async def test_owed_delivery_is_not_marked_as_a_ping_she_did_not_send(self):
        # 群里的 @ 由 mention 参数决定；交货不 @ 人，那就别让正文里长出假 @
        sent, _, calls = await self._run(
            reply="阿戊，两张牌都指着工作，宝剑三逆位说明那层体面还没塌。")
        self.assertFalse(calls["mention"][0])

    async def test_owed_report_is_not_bounded_by_the_chat_lane(self):
        _sent, gen, calls = await self._run()
        self.assertEqual(calls["timeout"][0], 180, "交货还挂在水群那 10 秒预算上")
        self.assertNotIn("chat_fast", calls["kwargs"][0],
                         "开着非思考模式，1970 字的牌面会被压成一句套话")

    async def test_optional_supplement_is_still_dropped_when_stale(self):
        # 「顺嘴查到的小东西」迟到了就不该再插一句，这条闸门不能一起废掉
        sent, _gen, _calls = await self._run(owed=False, version_bump=True, reply="")
        self.assertEqual(sent, [])

    async def test_owed_failure_still_reports_instead_of_vanishing(self):
        # 工具两次都没回东西，模型也没组词成功 —— 三条路全断，仍然要说一声
        sent, _, calls = await self._run(
            tool_texts=("技能包下载失败了，我改用随机源自己抽。", "", ""), reply="")
        self.assertEqual(len(calls["tool"]), 2, "只重试一次就够了，别把云端刷爆")
        self.assertEqual(len(sent), 1, "算不成也要交代一句，沉默才是最大的坏掉")
        self.assertIn("没算成", sent[0]["text"])

    async def test_a_dead_model_still_gets_an_honest_fallback_line(self):
        # 模型挂了会吐兜底句（「脑子突然卡壳」那句），那句既不是交货也不是人话；
        # 换掉，但不能换成一串可爱的废话糊弄过去 —— 必须明说没成。
        sent, _, _ = await self._run(
            tool_texts=("技能包下载失败。", "", ""),
            reply="唔…我脑子突然卡壳了，你再说一遍嘛")
        self.assertNotIn("卡壳", sent[0]["text"])
        self.assertIn("没算成", sent[0]["text"])

    async def test_truncated_tool_reply_is_asked_for_once_more(self):
        # 生产实测：agent 今天常只回一句「我改用随机源自己抽」（56 秒 61 字）就断了，
        # 牌面留在它的下一个回合。后台没人在等，值得再要一次完整的。
        sent, _, calls = await self._run(
            tool_texts=("技能包下载失败，我改用加密随机源自己抽。", CARDS))
        self.assertEqual(len(calls["tool"]), 2, "半句就被当成结果交了")
        self.assertEqual(len(sent), 1)
        self.assertIn("宝剑三", sent[0]["text"])

    async def test_owed_delivery_addresses_the_fixed_name_not_the_group_card(self):
        import toolkit
        sent = []
        async def fake_group(ws, gid, uid, body, mention=False, **kw):
            sent.append(body); return True
        gen = AsyncMock(return_value="阿丁，牌看完了。")
        said = {"text": "等我看看牌", "delivered": True, "version": 7}
        async def fake_tool(key, question, context="", **scope):
            return {"ok": True, "text": CARDS, "seconds": 1, "images": [], "audio": [], "error": ""}
        with patch.dict(b._TURN_VERSIONS, {self.key: 8}, clear=False), \
                patch.object(b, "_lookup_is_owed", return_value=True), \
                patch.object(b, "_tool_plan", return_value={"cloud": True, "tool": "tarot"}), \
                patch.object(b, "_call_name", return_value="阿丁"), \
                patch.object(b, "get_system_prompt", lambda: "人设"), \
                patch.object(b, "call_openclaw", new=gen), \
                patch.object(b, "send_group_msg", new=fake_group), \
                patch.object(b, "_BRIDGE_WS", {"ws": object()}), \
                patch.object(b.asyncio, "sleep", new=_fast_sleep), \
                patch.object(toolkit, "run_agent_tool", new=fake_tool):
            await b._lookup_then_reply(self.key, TAROT, self.gid, "9000000004",
                                       "阿渔", "divine", {}, said=said)
        prompt = gen.call_args.args[0][1]["content"]
        self.assertIn("阿丁", prompt)
        self.assertNotIn("阿渔", prompt)

    async def test_tool_evidence_keeps_the_request_session_and_visibility(self):
        _sent, _gen, calls = await self._run()
        self.assertEqual(calls['tool_scope'][0], {'session': 'group:771166', 'visibility': 'group'})

    async def test_a_full_tool_reply_is_not_asked_twice(self):
        _sent, _gen, calls = await self._run()
        self.assertEqual(len(calls["tool"]), 1, "没坏也重试，白花一次云端")

    async def test_delivery_is_not_sent_twice_for_the_same_request(self):
        sent1, _, _ = await self._run()
        sent2, _, _ = await self._run()
        self.assertEqual(len(sent1) + len(sent2), 1, "同一件事交代两遍，跟不交代一样烦")

    async def test_report_text_is_clipped_at_the_hard_cap_not_mid_sentence(self):
        long_reply = "这张牌讲的是勉强维持的体面，两个人都知道。" * 30
        sent, _, _ = await self._run(reply=long_reply)
        self.assertLessEqual(len(sent[0]["text"]), cr.OPTIONS["report_max_chars"])
        self.assertTrue(sent[0]["text"].endswith("。"), sent[0]["text"][-8:])

    async def test_report_clip_follows_the_panel_knob(self):
        # 旋钮不跟着 clip_report 走就是假的：调到 200 她还是被塞回 300
        with patch.dict(cr.OPTIONS, {"report_max_chars": 200}):
            self.assertEqual(len(b.clip_report("牌" * 900)), 200)


class ReportLengthTests(unittest.TestCase):
    def test_report_profile_packs_sentences_instead_of_dropping_them(self):
        text = "。".join(["这张牌讲的是勉强维持的体面，两个人都心里有数"] * 12) + "。"
        got = cr.chunks(text, profile="report")
        self.assertLessEqual(len(got), cr.OPTIONS["bubbles_report"])
        self.assertTrue(all(len(x) <= cr.OPTIONS["reply_chars_report"] for x in got))
        self.assertEqual("".join(got), text, "分条把牌面分掉了")

    def test_chat_lane_capacity_covers_the_report_cap(self):
        # 150×2 配 300 的顶：第三句正好被整句丢掉，那还是「被硬截」，只是换了个地方
        self.assertGreaterEqual(cr.OPTIONS["reply_chars_report"] * cr.OPTIONS["bubbles_report"],
                                cr.OPTIONS["report_max_chars"])

    def test_long_sentence_is_split_not_eaten(self):
        # 一句 210 字（汇报里很常见）以前会整句丢掉，交货直接少一段
        one = "牌" * 200 + "。"
        got = cr.chunks(one, profile="report")
        self.assertGreater(sum(len(x) for x in got), 150, "超长单句被整句丢掉了")

    def test_chat_profile_capacity_is_the_chat_knobs(self):
        text = "。".join(["短"] * 40) + "。"
        got = cr.chunks(text)
        self.assertTrue(all(len(x) <= cr.OPTIONS["reply_chars"] for x in got))
        self.assertLessEqual(len(got), cr.OPTIONS["bubbles"])

    def test_report_knobs_are_bounded_and_hot(self):
        for key in ("reply_chars_report", "bubbles_report", "report_max_chars"):
            self.assertIn(key, cr.RANGES)
            self.assertIn(key, cr.DEFAULTS)
        self.assertLessEqual(cr.RANGES["report_max_chars"][1], 400,
                             "主人说别超 300，上限就不该能调到天上")


class ReportLaneWiringTests(unittest.IsolatedAsyncioTestCase):
    """汇报档必须真的接在控制层包装器上。

    上面那些用例是拿 `call_openclaw` 的替身跑的，看不见真正把话压缩掉的那一层：
    水群包装器会同时掐 token（`reply_tokens`）、改指令（「一次最多三句」）、
    再按水群档切条。三处任一没让开，交货照样只剩一句 —— 所以这层要单独锁住。
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="report_lane_")
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        runtime = patch.multiple(
            cr, ROOT=Path(root), OPTIONS=dict(cr.DEFAULTS), BUSY=Counter(),
            LEDGER={}, BATCH_LOCKS={}, INBOX_TASK=None, INBOX_SECONDS=3600.0)
        runtime.start()
        self.addCleanup(runtime.stop)
        self.calls = []
        self.sent = []
        # 一段真实的交货长度：水群档（45×3）装不下，汇报档整段装得下。
        self.REPORT = "。".join(
            ["这张牌说的是两个人都在勉强维持一层体面，谁也不肯先说破，"
             "所以事情会一直卡在这里不动"] * 7) + "。"

        async def call_openclaw(messages, max_tokens=800, timeout=300, **extra):
            self.calls.append({"messages": messages, "max_tokens": max_tokens, "extra": extra})
            return self.REPORT

        async def send_group_msg(ws, group_id, user_id, text, mention=True, reason="", report=False):
            self.sent.append(text)
            return True

        async def noop(*a, **kw):
            return None

        self.ns = {
            "handle_group_message": noop, "handle_private_message": noop,
            "periodic_group_review": noop, "compact_group_memory_once": noop,
            "run_agent_task": noop, "call_openclaw": call_openclaw,
            "call_mimo_vision": call_openclaw, "send_group_msg": send_group_msg,
            "send_private_msg": noop, "build_group_recent_text": lambda g: ("", 0, 0, 0, []),
        }
        self.rt = SimpleNamespace(blocked=lambda gid: False, snapshot=lambda: None)
        with redirect_stdout(io.StringIO()):
            cr.install(self.ns, self.rt)
        if cr.INBOX_TASK is not None:
            cr.INBOX_TASK.cancel()

    async def asyncSetUp(self):
        self.scope_token = cr.SCOPE.set("chat")

    async def asyncTearDown(self):
        cr.SCOPE.reset(self.scope_token)

    async def test_report_lane_keeps_the_text_the_chat_lane_would_eat(self):
        chat = await self.ns["call_openclaw"]([{"role": "user", "content": "水群"}],
                                              max_tokens=500, timeout=180, chat_fast=True)
        self.calls.clear()
        report = await self.ns["call_openclaw"]([{"role": "user", "content": "交货"}],
                                                max_tokens=500, timeout=180, report=True)
        self.assertLessEqual(len(chat), cr.OPTIONS["reply_chars"] * cr.OPTIONS["bubbles"])
        self.assertEqual(len(report.replace("\n", "")), len(self.REPORT))
        self.assertGreater(len(report), cr.OPTIONS["reply_chars"] * cr.OPTIONS["bubbles"] * 2)

    async def test_report_lane_is_not_token_clamped_or_told_to_say_three_words(self):
        await self.ns["call_openclaw"]([{"role": "user", "content": "交货"}],
                                        max_tokens=500, timeout=180, report=True)
        call = self.calls[-1]
        self.assertEqual(call["max_tokens"], 500, "reply_tokens 把手伸进汇报，说不完牌面")
        self.assertNotIn("report", call["extra"], "标记要给包装器吃掉，不能漏给底层模型函数")
        system = call["messages"][0]["content"]
        self.assertIn("办事汇报", system)
        self.assertNotIn("一次最多发三句", system)
        # 只给上限，实测她交 66 个字就完（主人原话是「解析详细一点」）—— 那得有个下限
        cap = cr.OPTIONS["report_max_chars"]
        self.assertIn("一般 %d 到 %d 字" % (cap // 2, cap), system, "汇报档没有下限")

    async def test_the_hard_cap_lives_in_the_lane_not_only_in_the_caller(self):
        # 调用方漏剪也不行：汇报档自己就得守住 report_max_chars 这道顶
        with patch.dict(cr.OPTIONS, {"report_max_chars": 300}):
            got = cr.chunks("牌" * 900, profile="report")
        self.assertLessEqual(sum(len(x) for x in got), 300)
        self.assertGreater(sum(len(x) for x in got), 200)

    async def test_report_send_splits_by_the_report_lane(self):
        await self.ns["send_group_msg"](None, "771166", "9000000004",
                                        self.REPORT, mention=False, report=True)
        self.assertLessEqual(len(self.sent), cr.OPTIONS["bubbles_report"])
        self.assertTrue(all(len(x) <= cr.OPTIONS["reply_chars_report"] for x in self.sent))
        self.assertEqual("".join(self.sent).replace("\n", ""), self.REPORT.replace("\n", ""),
                         "分条不能把内容分掉")

    async def test_owed_delivery_survives_patrol_being_switched_off(self):
        # 后台查询挂在巡群作用域里跑：开关一关就把答应人的交货一起关了，那是最坏的静默
        cr.OPTIONS["patrol_enabled"] = False
        cr.SCOPE.set("patrol")
        await self.ns["send_group_msg"](None, "771166", "9000000004", self.REPORT, report=True)
        self.assertTrue(self.sent, "关掉巡群把答应了的事也关了")
        self.sent.clear()
        await self.ns["send_group_msg"](None, "771166", "9000000004", "顺嘴接的一句")
        self.assertEqual(self.sent, [], "非交货仍该被巡群开关拦住")

    async def test_blocked_group_still_gets_no_delivery(self):
        with patch.object(self.rt, "blocked", lambda gid: True):
            await self.ns["send_group_msg"](None, "771166", "9000000004", self.REPORT, report=True)
        self.assertEqual(self.sent, [])

    def test_base_functions_accept_the_report_flag_without_the_control_layer(self):
        """没装包装器的进程（巡群 worker、面板子进程）也调得动，不能 TypeError。"""
        import inspect
        for name in ("send_group_msg", "send_private_msg"):
            self.assertIn("report", inspect.signature(getattr(b, name)).parameters, name)
        self.assertIn("report", inspect.signature(b.call_openclaw).parameters)


class PanelCopyTests(unittest.TestCase):
    def test_advanced_panel_exposes_the_report_lane(self):
        html = (ROOT / "website" / "console.html").read_text(encoding="utf-8")
        js = (ROOT / "website" / "static" / "console.js").read_text(encoding="utf-8")
        for el in ("ad-report-chars", "ad-report-bubbles", "ad-report-max"):
            self.assertIn('id="%s"' % el, html, el + " 没做成可调的")
        for key in ("reply_chars_report", "bubbles_report", "report_max_chars"):
            self.assertIn(key, js)

    def test_copy_states_the_length_rule(self):
        html = (ROOT / "website" / "console.html").read_text(encoding="utf-8")
        i = html.index("ad-report-chars")
        block = html[max(0, i - 1200):i + 1200]
        self.assertIn("办事", block)
        self.assertIn("300", block)


class UpstreamToolBreakageTests(unittest.TestCase):
    """主人问「这些工具调用坏掉了吗」—— 生产日志里有三种坏法，都得锁住。

    一、技能包装不上（`SKILL_DOWNLOAD_FAILED` / `WGET_RC=4`）：
        以前 agent 只回一句安装失败就不抽了，她照着交代「没成」。
    二、会话还在出活就被掐：fortune 实测 152.1 秒返回 0 字，正好撞上 150 秒超时。
        交货早就挪到「先回一句」之后了，多等的是后台，不是人，没必要剪。
    三、只说过程不说结果：agent 回一句「我改用随机源自己抽」就结束，
        牌面留在下一个回合 —— 所以要求一条回复里说完，短到像半句就再要一次。
    """

    def test_divination_prompts_allow_a_local_real_draw(self):
        import toolkit
        for key in ("tarot", "zhouyi", "fortune"):
            p = toolkit.TOOLS[key]["prompt"]
            self.assertIn("装不上", p, key)
            self.assertIn("secrets", p, key)

    def test_prompts_require_the_whole_reading_in_one_reply(self):
        import toolkit
        for key in ("tarot", "zhouyi", "fortune"):
            self.assertIn("同一条回复", toolkit.TOOLS[key]["prompt"], key)

    def test_slow_agent_tools_are_not_cut_off_at_150s(self):
        import toolkit
        for key in ("tarot", "zhouyi", "fortune"):
            self.assertGreaterEqual(toolkit.TOOLS[key]["timeout"], 300, key)


if __name__ == "__main__":
    unittest.main()
