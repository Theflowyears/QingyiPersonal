"""回归锁：B站那条链接怎么从「群里有人发了个视频」变成「她真的看过」。

这一批最容易出的三种错，每种都在这里钉住：
  1) 把群里任何 URL 都当视频去 fetch —— SSRF；
  2) 没登录拿不到字幕，却返回空列表，下游读成「这视频没字幕」，
     于是她说出一句没有依据的话；
  3) 笔记写了、知识库写了，但**从来没被检索注入过** ——
     那就是「档案里写了主人是大三学生却一直在问上下班」的翻版。
     所以这里验的是她真的走 knowledge_base 那条已被接进提示词的路。
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import qq_openclaw_bridge as b      # noqa: E402
import bili                          # noqa: E402
import media_link as ml              # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


META = {"ok": True, "bvid": "BV1GJ411x7h7", "aid": 80433022, "cid": 1,
        "title": "【官方 MV】Never Gonna Give You Up", "owner": "索尼音乐中国",
        "duration": 213, "pubdate": 1577835803, "desc": "单曲循环",
        "view": 108120793, "like": 210000, "pages": 1, "parts": ["MV"]}


class Harness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="biliw_")
        self._env = {k: os.environ.get(k) for k in
                     ("DATA_DIR", "BILI_MODE", "BILI_COOKIE")}
        os.environ["DATA_DIR"] = self.dir
        bili.MODE = "meta"
        bili.COOKIE = ""
        bili._cache.clear()
        bili._notes.update({"sig": None, "items": None})
        b._BILI_QUEUE[:] = []
        self.calls = {"meta": [], "sub": [], "kb": [], "exp": [], "llm": [],
                      "resolve": []}
        self.ok_refs = {META["bvid"]}
        self.ok_short = {"https://b23.tv/aB3xKd"}
        self.reply = "这UP主标题起得真唬人。"
        self._patch()

    def tearDown(self):
        for name, orig in self._orig.items():
            setattr(name[0], name[1], orig)
        b._BILI_QUEUE[:] = []
        bili._cache.clear()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)

    def _patch(self):
        self._orig = {}

        def swap(obj, name, fn):
            self._orig[(obj, name)] = getattr(obj, name)
            setattr(obj, name, fn)

        async def fake_fetch(ref, client=None):
            self.calls["meta"].append(ref)
            return dict(META, bvid=ref) if ref in self.ok_refs else {
                "ok": False, "reason": "查无此片"}

        async def fake_sub(meta):
            self.calls["sub"].append(meta.get("bvid"))
            if bili.has_login_cookie():
                return {"ok": True, "reason": "", "text": "never gonna give you up"}
            return {"ok": False, "reason": "需要登录 cookie", "text": ""}

        async def fake_resolve(url):
            self.calls["resolve"].append(url)
            return "BV1GJ411x7h7" if url in self.ok_short else ""

        async def fake_kb(key, title, summary, source="", llm=None):
            self.calls["kb"].append((key, title, summary, source))
            return {"saved": True, "reason": ""}

        async def fake_llm(msgs, **kw):
            self.calls["llm"].append(msgs)
            return getattr(self, "reply", "这UP主标题起得真唬人。")

        def fake_record(text):
            self.calls["exp"].append(str(text))

        import knowledge_base as kbm
        import persona_growth as pgc
        swap(bili, "fetch_meta", fake_fetch)
        swap(bili, "fetch_subtitles", fake_sub)
        swap(bili, "resolve_short", fake_resolve)
        swap(kbm, "judge_and_remember", fake_kb)
        swap(b, "call_openclaw", fake_llm)
        swap(pgc, "record_experience", fake_record)


class CollectTests(Harness):
    def test_bvid_is_queued(self):
        got = b.bili_collect_links("看这个 https://www.bilibili.com/video/BV1GJ411x7h7")
        self.assertEqual(got, ["BV1GJ411x7h7"])
        self.assertEqual([i["ref"] for i in b._BILI_QUEUE], ["BV1GJ411x7h7"])

    def test_short_link_is_queued_separately(self):
        b.bili_collect_links("https://b23.tv/aB3xKd 笑死")
        self.assertEqual([i["kind"] for i in b._BILI_QUEUE], ["short"])

    def test_intranet_url_is_not_queued(self):
        b.bili_collect_links("帮我看看 http://127.0.0.1:3001/api 怎么了")
        self.assertEqual(b._BILI_QUEUE, [])

    def test_lookalike_host_is_not_queued(self):
        b.bili_collect_links("https://evil.com/?x=bilibili.com&v=BV1GJ411x7h7")
        # 裸写的 BV 号本身还是要认的 —— 那不需要 fetch 任何人的地址
        self.assertEqual([i["ref"] for i in b._BILI_QUEUE], ["BV1GJ411x7h7"])

    def test_duplicate_links_collapse(self):
        b.bili_collect_links("BV1GJ411x7h7")
        b.bili_collect_links("BV1GJ411x7h7")
        self.assertEqual(len(b._BILI_QUEUE), 1)

    def test_queue_is_bounded(self):
        for i in range(60):
            b.bili_collect_links("BV1%09d" % i)
        self.assertLessEqual(len(b._BILI_QUEUE), b._BILI_QUEUE_MAX)

    def test_mode_off_queues_nothing(self):
        bili.MODE = "off"
        self.assertEqual(b.bili_collect_links("https://www.bilibili.com/video/BV1GJ411x7h7"),
                         [])
        self.assertEqual(b._BILI_QUEUE, [])

    def test_collect_does_no_network(self):
        # 她在等回复：这一步只准匹配正则，一次请求都不许发
        with_self = b.bili_collect_links("https://www.bilibili.com/video/BV1GJ411x7h7")
        self.assertTrue(with_self)
        self.assertEqual(self.calls["meta"], [])
        self.assertEqual(self.calls["resolve"], [])


class DrainTests(Harness):
    def test_video_becomes_note_and_knowledge(self):
        b._BILI_QUEUE.append({"kind": "bvid", "ref": META["bvid"], "scene": "group"})
        n = run(b._bili_drain(bili))
        self.assertEqual(n, 1)
        self.assertEqual(self.calls["meta"], [META["bvid"]])
        self.assertEqual(len(self.calls["kb"]), 1, self.calls["kb"])
        key, title, summary, source = self.calls["kb"][0]
        self.assertEqual(key, META["bvid"])
        self.assertEqual(source, "B站")
        self.assertIn("索尼音乐中国", summary)
        rec = bili.note_for(META["bvid"])
        self.assertEqual(rec["reaction"], "这UP主标题起得真唬人。")

    def test_knowledge_entry_carries_no_made_up_content(self):
        # 没字幕时，入库那条必须写着「字幕未取到」，
        # 不然下一次检索把它当证据注入，她就以为自己在视频里看到了什么
        b._BILI_QUEUE.append({"kind": "bvid", "ref": META["bvid"], "scene": "group"})
        run(b._bili_drain(bili))
        summary = self.calls["kb"][0][2]
        self.assertIn("字幕未取到", summary)
        self.assertIn("需要登录 cookie", summary)

    def test_reaction_prompt_states_what_she_does_not_know(self):
        b._BILI_QUEUE.append({"kind": "bvid", "ref": META["bvid"], "scene": "group"})
        run(b._bili_drain(bili))
        prompt = self.calls["llm"][0][-1]["content"]
        self.assertIn("字幕没拿到", prompt)
        self.assertIn("别编剧情", prompt)

    def test_reaction_is_asked_in_her_own_voice(self):
        # 不带 system 的那次实测回了「吾一时失语，未得佳答」——
        # 那不是她，而且这句会被当成「她当时的想法」永久留在知识库里，
        # 比编内容更难发现：内容是真的，人设是假的。
        b._BILI_QUEUE.append({"kind": "bvid", "ref": META["bvid"], "scene": "group"})
        run(b._bili_drain(bili))
        msgs = self.calls["llm"][0]
        self.assertEqual(msgs[0]["role"], "system", "反应必须带着她自己问")
        self.assertTrue(len(str(msgs[0]["content"])) > 100,
                        "system 是空的等于没带人格")

    def test_no_reaction_means_no_experience(self):
        self.reply = "[[无感]]"
        b._BILI_QUEUE.append({"kind": "bvid", "ref": META["bvid"], "scene": "group"})
        run(b._bili_drain(bili))
        self.assertEqual(self.calls["exp"], [], "没想法不该硬凑一段人生经历")
        self.assertEqual(bili.note_for(META["bvid"])["reaction"], "")

    def test_short_link_resolves_then_watches(self):
        b._BILI_QUEUE.append({"kind": "short",
                              "ref": "https://b23.tv/aB3xKd", "scene": "group"})
        n = run(b._bili_drain(bili))
        self.assertEqual(n, 1)
        self.assertEqual(self.calls["resolve"], ["https://b23.tv/aB3xKd"])
        self.assertEqual(self.calls["meta"], ["BV1GJ411x7h7"])

    def test_unresolvable_short_link_is_skipped_not_fetched(self):
        # 跳不出视频号就别硬猜：拿一个不存在的 BV 去查，
        # 她就会「看过」一个根本不存在的东西
        b._BILI_QUEUE.append({"kind": "short", "ref": "https://b23.tv/dead01",
                              "scene": "group"})
        n = run(b._bili_drain(bili))
        self.assertEqual(n, 0)
        self.assertEqual(self.calls["resolve"], ["https://b23.tv/dead01"])
        self.assertEqual(self.calls["meta"], [])
        self.assertEqual(self.calls["kb"], [])

    def test_second_share_of_same_video_is_not_watched_twice(self):
        b._BILI_QUEUE.append({"kind": "bvid", "ref": META["bvid"], "scene": "group"})
        run(b._bili_drain(bili))
        b._BILI_QUEUE.append({"kind": "bvid", "ref": META["bvid"], "scene": "group"})
        n = run(b._bili_drain(bili))
        self.assertEqual(n, 0)
        self.assertEqual(self.calls["meta"], [META["bvid"]], "重复转发又看了一遍")

    def test_round_is_capped(self):
        refs = ["BV1%09d" % i for i in range(5)]
        self.ok_refs = set(refs)
        for r in refs:
            b._BILI_QUEUE.append({"kind": "bvid", "ref": r, "scene": "group"})
        old = os.environ.get("BILI_MAX_PER_ROUND")
        os.environ["BILI_MAX_PER_ROUND"] = "2"
        try:
            n = run(b._bili_drain(bili))
        finally:
            if old is None:
                os.environ.pop("BILI_MAX_PER_ROUND", None)
            else:
                os.environ["BILI_MAX_PER_ROUND"] = old
        self.assertEqual(n, 2, n)
        self.assertEqual(len(self.calls["meta"]), 2, self.calls["meta"])
        self.assertEqual(len(b._BILI_QUEUE), 3, "一轮只该吃两个，剩下的下轮再看")

    def test_dead_links_also_count_against_the_round(self):
        # 取不到的那些也要发一次请求才知道取不到，不计进上限就是
        # 「一队列死链，一轮全敲一遍」
        refs = ["BV1%09d" % i for i in range(5)]
        self.ok_refs = set()
        for r in refs:
            b._BILI_QUEUE.append({"kind": "bvid", "ref": r, "scene": "group"})
        old = os.environ.get("BILI_MAX_PER_ROUND")
        os.environ["BILI_MAX_PER_ROUND"] = "2"
        try:
            run(b._bili_drain(bili))
        finally:
            if old is None:
                os.environ.pop("BILI_MAX_PER_ROUND", None)
            else:
                os.environ["BILI_MAX_PER_ROUND"] = old
        self.assertEqual(len(self.calls["meta"]), 2, self.calls["meta"])

    def test_unknown_video_reports_why_and_stores_nothing(self):
        b._BILI_QUEUE.append({"kind": "bvid", "ref": "BV1zzzzzzzzz",
                              "scene": "group"})
        n = run(b._bili_drain(bili))
        self.assertEqual(n, 0)
        self.assertEqual(self.calls["kb"], [])
        self.assertEqual(bili.latest_notes(5), [])

    def test_reaction_stores_nothing_when_the_model_failed(self):
        # 模型挂了 → call_openclaw 回兜底句 → 反应必须是空，
        # 不然「吾一时失语，未得佳答」会被当成她的想法永久存进知识库
        self.reply = "吾一时失语，未得佳答。"
        b._BILI_QUEUE.append({"kind": "bvid", "ref": META["bvid"], "scene": "group"})
        run(b._bili_drain(bili))
        self.assertEqual(self.calls["exp"], [], "兜底句被当成她的经历存了")
        self.assertEqual(bili.note_for(META["bvid"])["reaction"], "")
        self.assertNotIn("她当时的想法", self.calls["kb"][0][2])


class InjectionPathTests(unittest.TestCase):
    def test_worker_registered(self):
        with open(os.path.join(BOT, "ceylan_runtime.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("ns['bili_worker']()", src)

    def test_inbound_hook_is_on_the_converging_path(self):
        # 三条入站路径（巡群 / 被@ / 主动接话）最后都走 ask_openclaw，
        # 挂在别处就会有一条路看不见视频链接
        with open(os.path.join(BOT, "qq_openclaw_bridge.py"), encoding="utf-8") as f:
            src = f.read()
        lane = src[src.index("async def ask_openclaw"):]
        lane = lane[:lane.index("_t0 = time.time()")]
        self.assertIn("bili_collect_links(text", lane)

    def test_notes_reach_her_through_the_knowledge_base(self):
        # 笔记本身不进提示词；进提示词的是知识库那条 —— 那条路已经验证过会被注入。
        # 这条锁住别以后改成「只存 note 不入库」，那等于她看了但从没记住。
        with open(os.path.join(BOT, "qq_openclaw_bridge.py"), encoding="utf-8") as f:
            src = f.read()
        lane = src[src.index("async def _bili_drain"):]
        lane = lane[:lane.index("async def bili_worker")]
        self.assertIn("_kb_judge(", lane)


class LaneAnswerGuardTests(unittest.TestCase):
    """模型挂了时那句兜底话不能当成她的想法发出去或存下来。

    「吾一时失语，未得佳答。」是 call_openclaw 的失败兜底，不在 _is_silence 里，
    只在 is_bad_periodic_answer 里 —— 后台那几条自主链路只查前者的话，
    一次模型故障就会把它私聊发给主人、或写进她的知识库永久留着。
    """

    def test_canned_fallback_is_not_usable(self):
        self.assertFalse(b._lane_answer_usable("吾一时失语，未得佳答。"))

    def test_normal_sentence_is_usable(self):
        self.assertTrue(b._lane_answer_usable("这UP主标题起得真唬人。"))

    def test_empty_and_overlong_are_not_usable(self):
        self.assertFalse(b._lane_answer_usable(""))
        self.assertFalse(b._lane_answer_usable("   "))
        self.assertFalse(b._lane_answer_usable("啊" * 300, 160))

    def test_silence_markers_are_not_usable(self):
        for t in ("[[沉默]]", "null", "None", "No response from OpenClaw"):
            self.assertFalse(b._lane_answer_usable(t), t)


if __name__ == "__main__":
    unittest.main()
