"""回归锁：固定话术必须是她本人的口吻，不许出现文言腔。

起因：用户指出「吾一时失语，未得佳答。」不符合她的人设 ——
角色是现代人口语语域；固定话术不许半文半白。
查下去发现这不是孤例：`call_openclaw` 的失败兜底、长文截断后缀、
任务系统的四句固定回复全都是半文半白的句子（一共 7 处），
而且 persona_growth 早就有条「语料库隔离」规则明令不许把古风文本
当成她自己 —— 也就是说这些罐头句违反的是这个项目自己定的规矩。

为什么用 AST 扫字符串字面量而不是 grep：
`is_bad_periodic_answer` 里必须留着「吾一时失语」这种旧句子才能拦住
历史记录和模型故障，grep 会把它一起报错，然后有人图省事把断言关掉。
所以这里只查「会被发出去或存起来的文本」，检测用的表排除在外。
"""
import ast
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import qq_openclaw_bridge as b        # noqa: E402

# 文言腔标记。挑的都是现代群聊里她不会用的字，
# 别把「资源/语言」这种词里的字算进来（所以用单字时要看语境，这里只留安全的）。
CLASSICAL = [
    u"吾", u"汝", u"尔等", u"辞多不尽", u"姑止于此", u"不才",
    u"且慢", u"领命", u"清听", u"下新令", u"尚未终了", u"这便",
]

# 这些函数里的字符串是「检测表」，本来就该写着旧句子，不算违规
DETECTORS = {"is_bad_periodic_answer", "_lane_answer_usable"}


def _strings_under(node):
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.add(id(sub))
    return out


def _scan(path):
    with io.open(path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    skip = set()
    docstrings = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if n.name in DETECTORS:
                skip |= _strings_under(n)
            first = n.body and n.body[0]
            if (first and isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    hits = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
            continue
        if id(n) in skip or id(n) in docstrings:
            continue
        for m in CLASSICAL:
            if m in n.value:
                hits.append((os.path.basename(path), n.lineno, m, n.value[:50]))
    return hits


class RegisterScanTests(unittest.TestCase):
    def test_no_classical_register_in_fixed_text(self):
        bad = []
        for name in sorted(os.listdir(BOT)):
            if not name.endswith(".py"):
                continue
            bad += _scan(os.path.join(BOT, name))
        self.assertEqual(bad, [], "这些固定文本是文言腔，不像她：\n" +
                         "\n".join(str(x) for x in bad[:12]))

    def test_model_failure_sentinel_is_still_detected(self):
        # 换了措辞但没换性质：这句仍然是「模型挂了」的标记，
        # 后台自主链路不能把它当她的想法发出去或存进档案
        self.assertTrue(b.is_bad_periodic_answer(u"吾一时失语，未得佳答。"),
                        "旧句子不再拦，历史里的文言腔会被当她的话复用")
        self.assertTrue(b.is_bad_periodic_answer(u"唔…我脑子突然卡壳了，你再说一遍嘛"),
                        "新兜底句没进检测表，一次故障就会被存成她的想法")

    def test_lanes_refine_both_sentinels(self):
        self.assertFalse(b._lane_answer_usable(u"吾一时失语，未得佳答。"))
        self.assertFalse(b._lane_answer_usable(u"唔…我脑子突然卡壳了，你再说一遍嘛"))
        self.assertTrue(b._lane_answer_usable(u"荒坂塔原来是荒坂公司在夜之城的总部。"))


class CannedLineVoiceTests(unittest.TestCase):
    """换掉的那几句得仍然像她：现代人、带口癖、我不放在句首。"""

    LINES = [
        u"……有点长，先说到这儿吧。",
        u"好啦，我这就把文件传上去：report.png",
        u"上一个还没弄完呢，稍等一下下。",
        u"好嘞，我一步一步来做；事情多的话每轮都报一声。",
        u"先停在这儿啦，想继续再喊我一声。",
        u"连着做了 12 轮啦，怕吵到群里，先停在这儿；要接着做再喊我一声。",
        u"唔…我脑子突然卡壳了，你再说一遍嘛",
    ]

    def test_no_classical_in_new_lines(self):
        for line in self.LINES:
            for m in CLASSICAL:
                self.assertNotIn(m, line, line)

    def test_benyu_not_at_the_start(self):
        # 提示词里明写着「我」一次回复最多一次、别放句首（她习惯每句自称，
        # 真人不会这么自我介绍）
        for line in self.LINES:
            self.assertFalse(line.startswith(u"我"), line)
            self.assertLessEqual(line.count(u"我"), 1, line)

    def test_tone_particles_within_quota(self):
        # 语气词配额是程序级限频的：罐头句要是超了，发出去会被削掉，
        # 等于我们以为她说了「呐」，实际那句被裁成了别的
        for line in self.LINES:
            self.assertEqual(b.limit_tone_particles(line), line, line)

    def test_lines_are_short_enough_for_one_bubble(self):
        for line in self.LINES:
            self.assertLess(len(line), 80, line)


if __name__ == "__main__":
    unittest.main()
