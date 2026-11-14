# -*- coding: utf-8 -*-
"""V01 核心契约回归（validation 轨道）：跨契约集成，不复制既有单测。

被测契约：M01 cognition_admission / M02 claim_provenance / M06 cognition_writer
+ self_model 事务账本 + A03/A04 memory_manifest。按 V01 口径分六组：

  1. 来源/逐命题归因：合格候选经 commit 后，读投影（retrieval_beliefs）能逐命题
     定位真实源文本原句；窗口外 ID / 转述丢否定 / 主体称呼落不到真实参与者
     分别被拒/隔离，隔离候选绝不被当成已核验信念消费。
  2. 投影失败：事务中途失败（unittest.mock 注入，自动恢复）整体回滚，
     异常原样抛出（沉默写入视为故障），失败后重试同一候选可成功且不堆重复。
  3. 并发幂等：多线程并发 commit 同一候选（BEGIN IMMEDIATE 串行化）账本一致；
     quarantine dedupe 键并发重复提交不堆垃圾；同 dedupe 事件观察只算一次。
  4. 预算/去重/截断/缓存：source_window 冻结语义；cognition_cache_token 在
     撤回后变化（旧缓存不得复活）；截断口径不产生「看起来完整实际被截」的
     假证据；memory_manifest 预算/去重与认知读投影的交叉用例。
  5. 读不写：只读 API 在空库/缺库场景不建文件不建表；只读连接写入必须失败。
  6. 真实资产不变：整个模块运行前后仓库 data/ 文件清单与 mtime 不变，
     所有写入只落 tempfile 隔离目录。

已知真实缺陷（保留用例、expectedFailure 标注，等主代理决策，不许改 bot 绕过）：
  V01-D1 self_model::_db —— retrieval_beliefs/pending_claims/source_window 等
         只读 API 经 _db() 在缺库时建 cognition.db 并 executescript 建全套表，
         违反 X15「读路径不做隐式初始化」。目前只有 R01/M03 适配器层用
         isfile 挡板兜住，self_model 自身读 API 不满足契约。
  V01-D2 self_model::write_cognition_transaction（add_belief 同病灶）——
         quote 截到 [:400] 但 span_start/span_end 保留全长 offset，
         存下来的 span 无法再逐字复核（看起来完整实际被截的假证据）。
  V01-D3 memory_manifest::_manifest_path —— 只读 last() 经 _manifest_path()
         在缺目录时 mkdir(parents=True)，读路径顺手建目录（X15 同类）。
  V01-D4 self_model::_db —— 冷账本上 PRAGMA journal_mode=WAL 转换遇到别的
         连接持写锁时立即 SQLITE_BUSY（不吃 timeout=8），record_event 吞掉
         异常返回 ""：并发首开/短暂持锁窗口内的真实观察被静默丢失。
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import cognition_writer as cw      # noqa: E402
import memory_manifest as mm       # noqa: E402
import self_model as sm            # noqa: E402

NOW = 2000.0
REPO_DATA = Path(ROOT) / "data"


# ── 组 6 的模块级快照：导入后、任何测试运行前取一次 ──────────────
def _repo_data_snapshot():
    if not REPO_DATA.is_dir():
        return [], []
    files = sorted((str(p.relative_to(REPO_DATA)), p.stat().st_mtime_ns, p.stat().st_size)
                   for p in REPO_DATA.rglob("*") if p.is_file())
    dirs = sorted(str(p.relative_to(REPO_DATA)) for p in REPO_DATA.rglob("*") if p.is_dir())
    return files, dirs


_REPO_DATA_BEFORE = None


def setUpModule():
    global _REPO_DATA_BEFORE
    _REPO_DATA_BEFORE = _repo_data_snapshot()


def _tree_snapshot(root):
    """目录里有什么（含空目录）：证明只读路径没顺手建任何东西。"""
    entries = []
    for base, dirs, files in os.walk(root):
        for name in dirs:
            entries.append((os.path.relpath(os.path.join(base, name), root), "dir"))
        for name in files:
            entries.append((os.path.relpath(os.path.join(base, name), root), "file"))
    return sorted(entries)


def _readonly_connection(path):
    """与 emotion_shadow._readonly 同一口径：mode=ro + query_only。"""
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=2)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _inject_failure(sql_fragment, error):
    """真实连接代理：只在命中 SQL 片段时抛指定异常；patch 自动恢复。"""
    real_db = sm._db

    class Proxy:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args):
            if sql_fragment in sql:
                raise error
            return self._real.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._real, name)

    return patch.object(sm, "_db", lambda: Proxy(real_db()))


# ── 隔离夹具：Windows 可见 tempfile + 调用时解析的环境变量 ────────
class LedgerIsolated(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="v01_", ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        environment = patch.dict(os.environ, {"DATA_DIR": self.root,
                                              "PERSONA_DATA_DIR": self.root,
                                              "QQ_DRY_RUN": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def event(self, text, actor="1001", session="private:1001"):
        # 必须给 dedupe：A05 身份哈希含时间/会话/说话人，同刻两条不同消息
        # 不给键会被幂等丢掉，窗口塌成一条事件的话回归锁就是假绿。
        return sm.record_event("private_message", session=session, actor_qq=actor,
                               visibility="private", payload={"text": text}, ts=1000.0,
                               dedupe="v01:" + session + ":" + text)

    def claim(self, eid, quote, content, **over):
        base = {"object_kind": "claim", "object_id": "obj_1", "content": content,
                "occurred_at": 1000.0, "recorded_at": 1001.0,
                "source_event_ids": [eid], "epistemic_type": "reported",
                "visibility": "private", "source_session": "private:1001",
                "evidence": [{"event_id": eid, "quote": quote}]}
        base.update(over)
        return base

    def counts(self):
        with closing(sm._db()) as connection:
            return {table: connection.execute(
                "SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                for table in ("person", "belief", "belief_evidence", "claim_span",
                              "claim_candidate", "episode", "commitment", "event")}

    def rows(self, table):
        with closing(sm._db()) as connection:
            return [dict(r) for r in connection.execute("SELECT * FROM %s" % table)]

    def source_text(self, eid):
        return json.loads(sm.get_event(eid)["payload"])["text"]


# ── 组 1：来源/逐命题归因（M02+M06 → 读投影端到端） ──────────────
class ProvenanceEndToEndTests(LedgerIsolated):
    def test_each_committed_belief_locates_its_own_sentence_in_the_real_source(self):
        first = self.event("我最喜欢喝冰可乐了")
        second = self.event("我家猫喜欢晒太阳")
        window = sm.source_window([first, second])
        actor_ref = window[0]["actor_ref"]
        # 信封故意带整窗事件号：依据仍必须逐命题只挂自己那句（X04）。
        got_a = cw.commit(self.claim(first, "我最喜欢喝冰可乐了", "喜欢冰可乐",
                                     object_id="obj_a", source_event_ids=[first, second],
                                     subject_ref=actor_ref, candidate_confidence=0.9),
                          window, now=NOW)
        got_b = cw.commit(self.claim(second, "猫喜欢晒太阳", "猫喜欢晒太阳",
                                     object_id="obj_b", source_event_ids=[first, second],
                                     subject_ref=actor_ref, candidate_confidence=0.9),
                          window, now=NOW)
        self.assertEqual(("accept", "accept"), (got_a["decision"], got_b["decision"]))
        projected = {row["id"]: row for row in sm.retrieval_beliefs()}
        self.assertEqual({got_a["object_ids"]["belief"], got_b["object_ids"]["belief"]},
                         set(projected))
        for got, eid, quote in ((got_a, first, "我最喜欢喝冰可乐了"),
                                (got_b, second, "猫喜欢晒太阳")):
            row = projected[got["object_ids"]["belief"]]
            self.assertEqual([eid], row["evidence_event_ids"],
                             "整窗事件又被摊给这条结论了")
            self.assertEqual(1, len(row["evidence_spans"]))
            span = row["evidence_spans"][0]
            self.assertEqual(quote, span["quote"])
            self.assertEqual(quote, self.source_text(eid)[span["span_start"]:span["span_end"]],
                             "读投影里的 span 必须能在真实源文本里逐字复核")
            self.assertEqual(actor_ref, span["subject_ref"])
            self.assertEqual("source_actor", span["subject_basis"])
            self.assertEqual(actor_ref, row["subject_qq"])
            self.assertIsNone(row["confidence"], "自报置信度不许写进已核验信念")
        self.assertEqual(0, self.counts()["claim_candidate"])

    def test_mixed_valid_and_out_of_window_evidence_is_rejected_without_partial_spans(self):
        eid = self.event("我最喜欢喝冰可乐了")
        window = sm.source_window([eid])
        got = cw.commit(self.claim(eid, "我最喜欢喝冰可乐了", "喜欢冰可乐",
                                   subject_ref=window[0]["actor_ref"],
                                   evidence=[{"event_id": eid, "quote": "我最喜欢喝冰可乐了"},
                                             {"event_id": "evt_ghost", "quote": "冰可乐"}]),
                        window, now=NOW)
        self.assertEqual("reject", got["decision"])
        self.assertIn("event_outside_window", got["reasons"])
        # 投影可以保留已核出的那一段 span 供审计；契约是账本里不许有半套证据。
        self.assertEqual(0, self.counts()["belief"])
        self.assertEqual(0, self.counts()["claim_span"])
        self.assertEqual(0, self.counts()["belief_evidence"])
        self.assertEqual([], sm.retrieval_beliefs())
        audit = sm.pending_claims()
        self.assertEqual(1, len(audit))
        self.assertEqual("rejected", audit[0]["decision"], "拒绝要留 rejected 候选供审计")

    def test_negation_dropping_paraphrase_is_rejected_and_auditable(self):
        eid = self.event("我不喜欢牛肉面")
        window = sm.source_window([eid])
        got = cw.commit(self.claim(eid, "我不喜欢牛肉面", "喜欢牛肉面",
                                   subject_ref=window[0]["actor_ref"]),
                        window, now=NOW)
        self.assertEqual("reject", got["decision"])
        self.assertIn("negation_dropped", got["reasons"])
        self.assertEqual(0, self.counts()["belief"])
        self.assertEqual([], sm.retrieval_beliefs())
        self.assertEqual("rejected", sm.pending_claims()[0]["decision"])

    def test_person_marker_without_a_real_participant_is_quarantined_not_fact(self):
        # 事件没有真实 actor：「主人」这个称呼落不到任何参与者，不许按称呼编主体。
        eid = self.event("我最喜欢喝冰可乐了", actor="")
        window = sm.source_window([eid])
        self.assertEqual("", window[0]["actor_ref"])
        got = cw.commit(self.claim(eid, "我最喜欢喝冰可乐了", "主人喜欢冰可乐"),
                        window, now=NOW)
        self.assertEqual("quarantine", got["decision"])
        self.assertIn("subject_unresolved", got["reasons"])
        self.assertEqual("", got["subject_ref"])
        self.assertEqual(0, self.counts()["belief"])
        self.assertEqual([], sm.retrieval_beliefs())
        self.assertEqual("deferred", sm.pending_claims()[0]["decision"])

    def test_quarantined_candidates_are_never_consumed_as_verified_beliefs(self):
        eid = self.event("我最喜欢喝冰可乐了")
        window = sm.source_window([eid])
        actor_ref = window[0]["actor_ref"]
        held = cw.commit(self.claim(eid, "我最喜欢喝冰可乐了", "喜欢冰可乐",
                                    object_id="obj_held", evidence=[]),
                         window, now=NOW)
        live = cw.commit(self.claim(eid, "我最喜欢喝冰可乐了", "喜欢冰可乐",
                                    object_id="obj_live", subject_ref=actor_ref),
                         window, now=NOW)
        self.assertEqual("quarantine", held["decision"])
        self.assertEqual("accept", live["decision"])
        self.assertEqual([live["object_ids"]["belief"]],
                         [row["id"] for row in sm.retrieval_beliefs()],
                         "隔离候选混进了已核验信念的读投影")
        self.assertEqual(1, self.counts()["claim_candidate"])
        self.assertEqual("pending_review", sm.pending_claims()[0]["state"])


# ── 组 2：投影失败（事务中途故障 → 整体回滚 + 原样出声 + 可重试） ──
class ProjectionFailureTests(LedgerIsolated):
    def test_multi_object_plan_rolls_back_every_object_when_the_last_insert_fails(self):
        eid = self.event("我最喜欢喝冰可乐了")
        plan = {"ts": NOW,
                "belief": {"object_id": "obj_m", "proposition": "喜欢冰可乐",
                           "subject_ref": "", "confidence": None, "source": "v01",
                           "spans": [{"event_id": eid, "quote": "冰可乐",
                                      "span_start": 5, "span_end": 8}]},
                "episode": {"object_id": "epi_m", "summary": "她看到朋友晒冰可乐",
                            "event_ids": [eid]},
                "commitment": {"object_id": "cmt_m", "to_qq": "1001",
                               "text": "周五前把图发给他", "due_ts": None,
                               "event_ids": [eid]}}
        before = self.counts()  # 夹具事件已建了 person/event 行，这是合法基线
        with _inject_failure("INTO commitment", sqlite3.OperationalError("injected disk failure")):
            with self.assertRaises(sqlite3.OperationalError):
                sm.write_cognition_transaction(plan)
        self.assertEqual(before, self.counts(),
                         "最后一个对象失败后留下了半套前面对象")
        # 故障之后重试同一 plan：三个对象完整落账，再重试不堆重复。
        first = sm.write_cognition_transaction(plan)
        again = sm.write_cognition_transaction(plan)
        self.assertEqual(first, again)
        self.assertEqual({"belief", "episode", "commitment"}, set(first))
        counts = self.counts()
        self.assertEqual((1, 1, 1), (counts["belief"], counts["episode"], counts["commitment"]))
        self.assertEqual((1, 1), (counts["belief_evidence"], counts["claim_span"]))

    def test_commit_propagates_the_exact_exception_and_leaves_no_half_belief(self):
        eid = self.event("我最喜欢喝冰可乐了")
        window = sm.source_window([eid])
        candidate = self.claim(eid, "我最喜欢喝冰可乐了", "喜欢冰可乐",
                               subject_ref=window[0]["actor_ref"])
        sentinel = RuntimeError("v01 injected mid-transaction failure")
        # 注入点选 belief_evidence（belief 已插、span 未插）：与既有 span 注入互补。
        with _inject_failure("INTO belief_evidence", sentinel):
            with self.assertRaises(RuntimeError) as caught:
                cw.commit(candidate, window, now=NOW)
        self.assertIs(sentinel, caught.exception, "异常必须原样抛出，不许吞掉或改包装")
        counts = self.counts()
        self.assertEqual((0, 0, 0), (counts["belief"], counts["belief_evidence"],
                                     counts["claim_span"]))
        got = cw.commit(candidate, window, now=NOW)
        self.assertEqual("accept", got["decision"])
        self.assertEqual(1, self.counts()["belief"])

    def test_quarantine_write_failure_is_loud_and_retryable(self):
        eid = self.event("我最喜欢喝冰可乐了")
        window = sm.source_window([eid])
        candidate = self.claim(eid, "我最喜欢喝冰可乐了", "喜欢冰可乐", evidence=[])
        with _inject_failure("INTO claim_candidate", sqlite3.OperationalError("injected")):
            with self.assertRaises(sqlite3.OperationalError):
                cw.commit(candidate, window, now=NOW)
        self.assertEqual(0, self.counts()["claim_candidate"], "隔离写入失败不许留半条候选")
        self.assertEqual(0, self.counts()["belief"])
        got = cw.commit(candidate, window, now=NOW)
        self.assertEqual("quarantine", got["decision"])
        self.assertEqual(1, self.counts()["claim_candidate"])


# ── 组 3：并发幂等（BEGIN IMMEDIATE 串行化，多连接账本一致） ──────
class ConcurrencyIdempotencyTests(LedgerIsolated):
    def test_parallel_commits_of_the_same_candidate_keep_one_object(self):
        eid = self.event("我最喜欢喝冰可乐了")
        window = sm.source_window([eid])
        candidate = self.claim(eid, "我最喜欢喝冰可乐了", "喜欢冰可乐",
                               subject_ref=window[0]["actor_ref"])
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda i: cw.commit(candidate, window, now=NOW + i), range(8)))
        self.assertEqual({"accept"}, {r["decision"] for r in results})
        self.assertEqual(1, len({r["object_ids"]["belief"] for r in results}),
                         "并发提交同一候选派生出了多个对象 ID")
        counts = self.counts()
        self.assertEqual((1, 1, 1), (counts["belief"], counts["belief_evidence"],
                                     counts["claim_span"]))
        self.assertEqual(0, counts["claim_candidate"])

    def test_parallel_quarantine_submissions_dedupe_to_one_candidate(self):
        eid = self.event("我最喜欢喝冰可乐了")
        window = sm.source_window([eid])
        candidate = self.claim(eid, "我最喜欢喝冰可乐了", "凭空断言喜欢冰可乐", evidence=[])
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda i: cw.commit(candidate, window, now=NOW + i), range(8)))
        self.assertEqual({"quarantine"}, {r["decision"] for r in results})
        self.assertEqual(1, len({r["object_ids"]["candidate"] for r in results}))
        self.assertEqual(1, self.counts()["claim_candidate"], "并发隔离提交堆了垃圾")
        self.assertEqual(0, self.counts()["belief"])

    def test_parallel_distinct_candidates_all_land_once(self):
        eid = self.event("我最喜欢喝冰可乐了")
        window = sm.source_window([eid])
        actor_ref = window[0]["actor_ref"]

        def submit(index):
            return cw.commit(self.claim(eid, "我最喜欢喝冰可乐了", "喜欢冰可乐",
                                        object_id="obj_c%d" % index,
                                        subject_ref=actor_ref),
                             window, now=NOW + index)

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(submit, range(6)))
        self.assertEqual({"accept"}, {r["decision"] for r in results})
        self.assertEqual(6, len({r["object_ids"]["belief"] for r in results}))
        counts = self.counts()
        self.assertEqual((6, 6, 6), (counts["belief"], counts["belief_evidence"],
                                     counts["claim_span"]))

    def test_parallel_observations_with_one_dedupe_key_stay_one_event(self):
        # 先单线程建库（生产常态：账本早已存在且是 WAL），再并发观察。
        # 冷启动并发首开的竞态另见 test_transient_writer_lock_must_not_drop_observation。
        self.assertTrue(self.event("建库种子消息"))

        def observe(index):
            return sm.record_event("group_message", session="group:771177",
                                   actor_qq="1001", visibility="group",
                                   payload={"text": "同一条消息的第%d次观察" % index},
                                   ts=1000.0, dedupe="v01:race:seq:7")

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(observe, range(8)))
        self.assertTrue(all(ids), "并发观察里有写入被静默吞掉")
        self.assertEqual(1, len(set(ids)), "同一 dedupe 被记成了多件事")
        with closing(sm._db()) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM event WHERE dedupe='v01:race:seq:7'").fetchone()[0])

    def test_transient_writer_lock_must_not_drop_observation(self):
        # V01-D4 修复回归锁：_db() 的 WAL 转换曾撞上写锁立即 SQLITE_BUSY，
        # 冷启动并发首开新账本会丢观察；现在 busy_timeout 忙等 + 容忍转换失败。
        # 契约：短暂写锁应在忙等窗口内被等到，而不是立刻丢掉这条观察。
        path = os.path.join(self.root, "cognition.db")
        locked = threading.Event()
        released = threading.Event()
        holder_errors = []

        def hold_briefly():
            # 连接必须在用它自己的线程里创建（sqlite3 线程亲和）。
            try:
                holder = sqlite3.connect(path, timeout=1)
                holder.execute("CREATE TABLE IF NOT EXISTS lock_holder(x)")
                holder.execute("BEGIN IMMEDIATE")
                holder.execute("INSERT INTO lock_holder VALUES(1)")
                locked.set()
                time.sleep(0.5)
                holder.rollback()
                holder.close()
            except Exception as exc:
                holder_errors.append(exc)
            finally:
                released.set()

        worker = threading.Thread(target=hold_briefly)
        worker.start()
        self.addCleanup(worker.join)
        self.assertTrue(locked.wait(5), "持锁线程没有就位")
        eid = sm.record_event("group_message", session="group:771177",
                              actor_qq="1001", visibility="group",
                              payload={"text": "持锁窗口内的一条真实观察"},
                              ts=1000.0, dedupe="v01:lock:1")
        self.assertTrue(released.wait(5))
        self.assertEqual([], holder_errors)
        self.assertTrue(eid, "短暂写锁期间的观察被静默丢弃（返回空 ID）")
        self.assertEqual(eid, sm.get_event(eid)["id"])


# ── 组 4：预算/去重/截断/缓存 ────────────────────────────────────
class BudgetCacheTruncationTests(LedgerIsolated):
    def test_source_window_freeze_survives_later_writes_and_mutations(self):
        first = self.event("我最喜欢喝冰可乐了")
        window = sm.source_window([first])
        later = self.event("今天天气不错")
        # 冻结之后发生的事件不进本次窗口：引用它必须被来源门拒。
        got = cw.commit(self.claim(later, "今天天气不错", "天气不错",
                                   object_id="obj_later"),
                        window, now=NOW)
        self.assertEqual("reject", got["decision"])
        self.assertIn("event_outside_window", got["reasons"])
        self.assertEqual([first], [row["event_id"] for row in window])
        # 窗口是脱离账本的只读副本：改它不许碰到账本或后续窗口。
        window[0]["text"] = "被篡改的原句"
        window[0]["retracted"] = True
        fresh = sm.source_window([first, later])
        self.assertEqual("我最喜欢喝冰可乐了", fresh[0]["text"])
        self.assertFalse(fresh[0]["retracted"])
        self.assertEqual("我最喜欢喝冰可乐了", self.source_text(first))
        self.assertEqual(2, len(fresh))

    def test_frozen_window_commit_after_retraction_lands_invalidated_not_live(self):
        eid = self.event("我喜欢猫")
        window = sm.source_window([eid])
        self.assertFalse(window[0]["retracted"])
        token_before = sm.cognition_cache_token()
        sm.retract_evidence(eid, "misread the source")
        self.assertNotEqual(token_before, sm.cognition_cache_token(),
                            "撤回必须推翻稳定日缓存的 token")
        # 冻结语义：旧窗口仍说源可用，来源门放行；但账本触发器必须让这条
        # 信念落地即失效，读投影绝不把已撤回的依据复活（X06）。
        self.assertFalse(window[0]["retracted"])
        self.assertTrue(sm.source_window([eid])[0]["retracted"])
        candidate = self.claim(eid, "我喜欢猫", "喜欢猫",
                               subject_ref=window[0]["actor_ref"])
        got = cw.commit(candidate, window, now=NOW)
        bid = got["object_ids"]["belief"]
        self.assertEqual("invalidated", self.rows("belief")[0]["status"])
        self.assertEqual([], sm.retrieval_beliefs(), "已撤回依据的信念混进了读投影")
        # 同一 object_id 再提交也不许把失效信念复活。
        cw.commit(candidate, window, now=NOW + 500)
        self.assertEqual(1, self.counts()["belief"])
        self.assertEqual("invalidated", self.rows("belief")[0]["status"])
        self.assertEqual([], sm.retrieval_beliefs())
        # 历史仍可审计：span 还在，只是不再被当前消费。
        self.assertEqual(1, len(sm.belief_spans(bid)))

    def test_cache_token_is_stable_across_reads_and_changes_only_on_new_invalidations(self):
        eid = self.event("我最喜欢喝冰可乐了")
        window = sm.source_window([eid])
        cw.commit(self.claim(eid, "我最喜欢喝冰可乐了", "喜欢冰可乐",
                             subject_ref=window[0]["actor_ref"]), window, now=NOW)
        first = sm.cognition_cache_token()
        self.assertEqual(first, sm.cognition_cache_token())
        sm.retract_evidence(eid, "source corrected")
        second = sm.cognition_cache_token()
        self.assertNotEqual(first, second)
        sm.retract_evidence(eid, "source corrected")
        self.assertEqual(second, sm.cognition_cache_token(),
                         "重复撤回不该让缓存 token 抖动")

    def test_short_quote_round_trips_verbatim_through_the_read_projection(self):
        eid = self.event("我最喜欢喝冰可乐了")
        window = sm.source_window([eid])
        got = cw.commit(self.claim(eid, "我最喜欢喝冰可乐了", "喜欢冰可乐",
                                   subject_ref=window[0]["actor_ref"]), window, now=NOW)
        span = sm.retrieval_beliefs()[0]["evidence_spans"][0]
        self.assertEqual(span["quote"],
                         self.source_text(eid)[span["span_start"]:span["span_end"]])
        self.assertEqual(len(span["quote"]), span["span_end"] - span["span_start"])
        self.assertTrue(got["object_ids"]["belief"])

    def test_long_quote_span_must_still_reverify_verbatim(self):
        # V01-D2 修复回归锁：claim_span 的 quote 不再截断，与全长 offset
        # 同一口径。断言不削平：不能产出「看起来完整实际被截」的假证据。
        long_quote = "我不喜欢牛肉面" * 70  # 490 字符，超过 400 上限
        eid = self.event(long_quote)
        window = sm.source_window([eid])
        got = cw.commit(self.claim(eid, long_quote, "不喜欢牛肉面",
                                   subject_ref=window[0]["actor_ref"]), window, now=NOW)
        self.assertEqual("accept", got["decision"])
        span = sm.belief_spans(got["object_ids"]["belief"])[0]
        self.assertEqual(len(long_quote), span["span_end"] - span["span_start"])
        self.assertEqual(span["quote"],
                         self.source_text(eid)[span["span_start"]:span["span_end"]],
                         "截断后的 quote 与 offset 对不上：假证据")
        self.assertEqual(len(span["quote"]), span["span_end"] - span["span_start"])

    def test_manifest_cross_budget_dedupe_and_hash_only_record(self):
        # M06 写入 → 只读读投影 → A03/A04 预算/去重 → 最终清单只落哈希。
        first = self.event("我最喜欢喝冰可乐了")
        second = self.event("我家猫喜欢晒太阳")
        window = sm.source_window([first, second])
        actor_ref = window[0]["actor_ref"]
        cw.commit(self.claim(first, "我最喜欢喝冰可乐了", "喜欢冰可乐",
                             object_id="obj_ma", subject_ref=actor_ref), window, now=NOW)
        cw.commit(self.claim(second, "猫喜欢晒太阳", "猫喜欢晒太阳",
                             object_id="obj_mb", subject_ref=actor_ref), window, now=NOW)
        projected = sm.retrieval_beliefs()
        self.assertEqual(2, len(projected))
        by_text = {row["proposition"]: row for row in projected}
        top = by_text["喜欢冰可乐"]
        blocks = [{"id": row["id"], "text": row["proposition"],
                   "score": 9 if row["id"] == top["id"] else 1,
                   "kind": "belief", "source": "cognition"} for row in projected]
        blocks.append({"id": top["id"], "text": "重复ID的另一种文本", "score": 99})
        blocks.append({"id": "dup_text", "text": top["proposition"], "score": 98})
        budget = len(top["proposition"])  # 只装得下最高分的一块
        selected, rendered, dropped = mm.compose(blocks, budget)
        self.assertEqual([top["id"]], [row["id"] for row in selected])
        self.assertEqual(top["proposition"], rendered, "预算不许把入选块切半句")
        self.assertEqual({"duplicate_id", "duplicate_text", "budget"},
                         {row["reason"] for row in dropped})
        messages = [{"role": "user", "content": "背景：" + rendered + "\n用户消息：在吗"}]
        manifest = mm.record("dm", "private:1001", messages, selected, dropped)
        self.assertIsNotNone(manifest, "清单记录失败必须出声（返回 None 即故障）")
        self.assertLessEqual(manifest["selected_chars"], budget)
        self.assertTrue(manifest["selected"][0]["present"])
        stored = mm.last(1)
        self.assertEqual(manifest["request_sha256"], stored[0]["request_sha256"])
        raw = mm._manifest_path().read_text(encoding="utf-8")
        self.assertNotIn(top["proposition"], raw, "清单只记 ID/长度/哈希，不落原文")


# ── 组 5：读不写 ─────────────────────────────────────────────────
class ReadNoWriteTests(LedgerIsolated):
    def test_read_apis_on_an_existing_empty_ledger_add_no_rows(self):
        sm._db().close()  # 显式建一个空库：把「读不到」和「没有」分开
        before = self.counts()
        self.assertEqual([], sm.retrieval_beliefs())
        self.assertEqual([], sm.pending_claims())
        self.assertEqual([], sm.source_window(["evt_missing"]))
        self.assertEqual(before, self.counts(), "只读 API 顺手写了账本")

    def test_retrieval_beliefs_on_absent_ledger_creates_nothing(self):
        # V01-D1 修复回归锁：只读检索走 _ro_db，缺库不建 cognition.db/表（X15）。
        before = _tree_snapshot(self.root)
        self.assertEqual([], sm.retrieval_beliefs())
        self.assertEqual(before, _tree_snapshot(self.root),
                         "只读检索顺手建了库/表，读路径就不再是只读")

    def test_pending_claims_on_absent_ledger_creates_nothing(self):
        # V01-D1 修复回归锁：pending_claims 缺库不再经 _db() 隐式初始化。
        before = _tree_snapshot(self.root)
        self.assertEqual([], sm.pending_claims())
        self.assertEqual(before, _tree_snapshot(self.root))

    def test_source_window_on_absent_ledger_creates_nothing(self):
        # V01-D1 修复回归锁：source_window 自称只读，缺库时 get_event 不许建库建表。
        before = _tree_snapshot(self.root)
        self.assertEqual([], sm.source_window(["evt_missing"]))
        self.assertEqual(before, _tree_snapshot(self.root))

    def test_readonly_connection_refuses_writes_and_cannot_create_a_ledger(self):
        eid = self.event("我最喜欢喝冰可乐了")
        self.assertTrue(eid)
        path = os.path.join(self.root, "cognition.db")
        with closing(_readonly_connection(path)) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("INSERT INTO meta(key,value) VALUES('x','y')")
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("UPDATE belief SET status='invalidated'")
        absent = os.path.join(self.root, "absent.db")
        # 3.14 里 connect 失败会留一个待 GC 的连接壳，屏蔽其 ResourceWarning。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            with self.assertRaises(sqlite3.OperationalError):
                sqlite3.connect(Path(absent).resolve().as_uri() + "?mode=ro", uri=True)
        self.assertFalse(os.path.exists(absent), "只读打开不许顺手建库")

    def test_manifest_last_on_absent_dir_creates_nothing(self):
        # V01-D3 修复回归锁：last() 走 _manifest_path(create=False)，只读不建目录。
        before = _tree_snapshot(self.root)
        self.assertEqual([], mm.last())
        self.assertEqual(before, _tree_snapshot(self.root),
                         "只读清单查询顺手建了目录（X15 同类）")


# ── 组 6：真实资产不变（类名保证按字母序最后运行） ────────────────
class ZzRealAssetInvariantTests(LedgerIsolated):
    @unittest.skipIf(os.getenv("CONTRACT_PROBE_ONLINE") == "1",
                     "在线模式由进程级写入边界证明；生产并发写入另记观察信息")
    def test_repository_data_directory_is_untouched_by_this_suite(self):
        self.assertEqual(_REPO_DATA_BEFORE, _repo_data_snapshot(),
                         "V01 套件写到了仓库 data/（文件清单或 mtime 变了）")

    def test_all_writes_land_only_in_the_tempfile_isolation(self):
        self.assertTrue(Path(self.root).resolve().is_relative_to(
            Path(tempfile.gettempdir()).resolve()), "夹具没有落在系统临时目录")
        eid = self.event("我最喜欢喝冰可乐了")
        window = sm.source_window([eid])
        got = cw.commit(self.claim(eid, "我最喜欢喝冰可乐了", "喜欢冰可乐",
                                   subject_ref=window[0]["actor_ref"]), window, now=NOW)
        self.assertEqual("accept", got["decision"])
        self.assertTrue(os.path.isfile(os.path.join(self.root, "cognition.db")))
        if os.getenv("CONTRACT_PROBE_ONLINE") != "1":
            self.assertEqual(_REPO_DATA_BEFORE, _repo_data_snapshot())


if __name__ == "__main__":
    unittest.main()