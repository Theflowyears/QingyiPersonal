"""回归锁：三库分词必须同一口径，且写入键要能被检索键命中。

这条锁的起因是取证发现的三件事：
  1) persona_store.tokenize 的注释写着「和群事实库同一套分词」，
     实际没载她的专属词典、也没过停用词；
  2) knowledge_base._tok 的注释写着「跟群记忆那边保持一致」，
     实际只有二元组、根本没 jieba —— 专名「主人」在知识库里只能碰运气；
  3) 群库打分最重的一项要求 `topic in query`，而 topic 是 LLM 起的名
     （「赤合金使用建议」），用户原话（「赤合金太贵了」）永远整词命中不了。
"""
import os
import shutil
import sys
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import group_memory_store as gms      # noqa: E402
import knowledge_base as kb           # noqa: E402
import persona_store as ps            # noqa: E402
import text_tokens as tt              # noqa: E402

SAMPLES = [
    "主人昨天说赤合金太贵了",
    "提弗洛斯的武器是不是要换",
    "我们今天聊的那个塔防到底怎么过",
    "主人家在重庆，做大三课程设计",
    "Arasaka Tower 在夜之城中心",
]


class ParityTests(unittest.TestCase):
    """同一个字符串在三个库里必须切成同样的样子。"""

    def test_all_stores_agree_on_every_sample(self):
        for s in SAMPLES:
            a = gms._tokens(s)
            b = set(ps.tokenize(s))
            c = kb._tok(s)
            self.assertEqual(a, b, "群库 vs 人格库 不一致: %r" % s)
            self.assertEqual(a, c, "群库 vs 知识库 不一致: %r" % s)

    def test_delegates_to_the_single_implementation(self):
        for s in SAMPLES:
            expect = tt.tokens(s)
            self.assertEqual(gms._tokens(s), expect)
            self.assertEqual(set(ps.tokenize(s)), expect)
            self.assertEqual(kb._tok(s), expect)

    def test_persona_dict_names_survive_as_one_token(self):
        # 「主人」在她的专属词典里。没有 userdict 时 jieba 会把它切开，
        # 于是「主人说了X」这条记忆永远检索不到。
        for store_fn in (gms._tokens, lambda t: set(ps.tokenize(t)), kb._tok):
            self.assertIn("主人", store_fn("主人昨天说赤合金太贵了"))

    def test_stopwords_filtered_everywhere(self):
        # 停用词不过滤的话，「我们/今天/那个」这种高频词会把 IDF 摊平，
        # 重要的稀有词反而排不上去
        q = "我们今天聊的那个塔防到底怎么过"
        for name, fn in (("群库", gms._tokens), ("人格库", lambda t: set(ps.tokenize(t))),
                         ("知识库", kb._tok)):
            toks = fn(q)
            for stopw in ("我们", "今天", "那个"):
                if stopw in tt.stopwords():
                    self.assertNotIn(stopw, toks, "%s 没滤停用词: %r" % (name, stopw))

    def test_content_words_are_words_not_bigrams(self):
        cw = tt.content_words("主人昨天说赤合金太贵了")
        self.assertIn("主人", cw)
        for w in cw:
            self.assertGreaterEqual(len(w), 2)


class AliasTests(unittest.TestCase):
    """写入键 vs 检索键：topic 是 LLM 起的，用户原话得另存一份。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="alias_")
        # 群库的路径是模块级常量（导入时就定死了），只改环境变量不隔离。
        # 必须直接改 _FILE 并把缓存复位成「空的」，
        # 注意不能 _cache.clear() —— 那会连 "data" 键一起删掉。
        self._old_file = gms._FILE
        gms._FILE = os.path.join(self.dir, "memory", "group_facts.json")
        gms._cache["data"] = None
        gms._cache["ts"] = 0.0
        gms._INDEX["sig"] = None
        if isinstance(gms._INDEX.get("groups"), dict):
            gms._INDEX["groups"] = {}

    def tearDown(self):
        gms._FILE = self._old_file
        gms._cache["data"] = None
        gms._cache["ts"] = 0.0
        gms._INDEX["sig"] = None
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_aliases_are_stored_from_the_users_own_words(self):
        rec = gms.add_entry("1001", "赤合金使用建议", "赤合金很贵，建议少用",
                            aliases=["赤合金太贵了"])
        self.assertTrue(rec.get("aliases"), rec)

    def test_repeated_saying_merges_alias_instead_of_duplicating(self):
        # 同一件事换个说法再来一遍：条目不重复，但要收下新说法
        gms.add_entry("1005", "赤合金使用建议", "赤合金很贵，建议少用",
                      aliases=["赤合金太贵"])
        n_before = len(gms.get_entries("1005"))
        gms.add_entry("1005", "赤合金使用建议", "赤合金很贵，建议少用",
                      aliases=["贵得离谱"])
        ents = gms.get_entries("1005")
        self.assertEqual(len(ents), n_before, "重复写入不该新增条目")
        self.assertIn("贵得离谱", ents[0].get("aliases") or [], ents)

    def test_alias_hit_lifts_score_when_topic_does_not_match(self):
        # 这正是线上那个死结：topic「赤合金使用建议」整词不在 query 里 → +8 拿不到
        e_no = {"topic": "赤合金使用建议", "text": "赤合金很贵，建议少用"}
        e_yes = dict(e_no)
        e_yes["aliases"] = ["赤合金", "太贵"]
        q = "赤合金太贵了吧"
        qtok = gms._tokens(q)
        s_no = gms._score_fast(e_no, q, qtok, gms._tokens(e_no["topic"] + e_no["text"]))
        s_yes = gms._score_fast(e_yes, q, qtok,
                                gms._tokens(e_yes["topic"] + e_yes["text"]) |
                                set(e_yes["aliases"]))
        self.assertGreater(s_yes, s_no, "别名没有带来任何分数差别")

    def test_retrieval_finds_entry_by_alias(self):
        gms.add_entry("1002", "赤合金使用建议", "赤合金很贵，建议少用",
                      aliases=["赤合金", "太贵"])
        ctx = gms.build_context("1002", query="赤合金太贵了吧")
        self.assertIn("赤合金", ctx)


class MigrationTests(unittest.TestCase):
    """重算 tokens 必须幂等、不能改条数、不能碰别的列。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="mig_")
        # persona_store 现在按调用时的 DATA_DIR 取库（模块常量在 import 时冻结过，
        # 于是换个 DATA_DIR 还在读写第一个库），所以这里改环境而不是改常量。
        self._old_env = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        self._reset()

    def _reset(self):
        if ps._conn is not None:
            try:
                ps._conn.close()
            except Exception:
                pass
        ps._conn = None
        ps._tok_cache.update({"tokens": None, "df": None, "inv": None,
                              "texts": None, "imps": None, "kinds": None,
                              "built": 0.0})

    def tearDown(self):
        self._reset()
        if self._old_env is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old_env
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_migrate_is_idempotent_and_count_preserving(self):
        ps.add("facts", "主人家在重庆")
        ps.add("facts", "主人喜欢赤合金")
        before = ps.counts()
        r1 = ps.migrate_tokens()
        r2 = ps.migrate_tokens()
        self.assertEqual(ps.counts(), before, "迁移不该改变条目数")
        # 第一次可能要改（旧口径写的），第二次必须是 0 —— 这才叫幂等
        self.assertEqual(r2["rewritten"], 0, r2)
        self.assertEqual(r1["total"], r2["total"])

    def test_migrated_tokens_match_the_new_tokenizer(self):
        ps.add("facts", "主人家在重庆")
        ps.migrate_tokens()
        want = " ".join(ps.tokenize("主人家在重庆"))
        row = list(ps.conn().execute(
            "SELECT tokens FROM memory WHERE text=?", ("主人家在重庆",)))
        self.assertEqual(str(row[0][0] or ""), want)

    def test_migrate_does_not_touch_importance_or_ts(self):
        ps.add("facts", "赤合金很贵")
        before = list(ps.conn().execute("SELECT importance, ts FROM memory WHERE text=?",
                                        ("赤合金很贵",)))[0]
        ps.migrate_tokens()
        after = list(ps.conn().execute("SELECT importance, ts FROM memory WHERE text=?",
                                       ("赤合金很贵",)))[0]
        self.assertEqual(float(before[0]), float(after[0]))
        self.assertEqual(float(before[1]), float(after[1]))

    def test_dry_run_writes_nothing(self):
        ps.add("facts", "主人家在重庆")
        r = ps.migrate_tokens(dry_run=True)
        want = " ".join(ps.tokenize("主人家在重庆"))
        row = list(ps.conn().execute("SELECT tokens FROM memory WHERE text=?",
                                     ("主人家在重庆",)))[0]
        if r["rewritten"]:
            self.assertNotEqual(str(row[0] or ""), want, "dry-run 居然写盘了")


if __name__ == "__main__":
    unittest.main()
