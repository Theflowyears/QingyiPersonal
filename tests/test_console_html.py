"""面板 HTML 的自检：id 不能重复，标签不能错配。

重排人格页那种大改，最容易留下的不是样式问题，而是「同一份卡片忘了删」——
两个 #pm-list 时 getElementById 只会拿到第一个，页面看着正常、JS 却改的是另一份。
所以这段先跑，跑绿了再谈上线。
"""
import io
import os
import re
import sys
import unittest
from html.parser import HTMLParser

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "website")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}
# 这个文件只读文件、不改 sys.path：往路径前面插 website/ 会让 `project_config`
# 解析到 website 那一份（它和 bot 那份同名但字段不同），一仓库两个坑。


class Checker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.ids = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if "id" in d:
            self.ids.append(d["id"])
        if tag not in VOID:
            self.stack.append((tag, self.getpos()))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append("多余的 </%s> 在 %s" % (tag, self.getpos()))
            return
        open_tag, pos = self.stack.pop()
        if open_tag != tag:
            self.errors.append("</%s> 对不上 <%s>（开在 %s，关在 %s）"
                               % (tag, open_tag, pos, self.getpos()))


def parse(path):
    c = Checker()
    with io.open(path, encoding="utf-8") as f:
        c.feed(f.read())
    return c


class HtmlStructureTests(unittest.TestCase):
    def test_console_html_is_balanced(self):
        c = parse(os.path.join(WEB, "console.html"))
        self.assertEqual(c.errors, [])
        self.assertEqual([t for t, _ in c.stack], [], "有标签没闭合：%s" % c.stack[:6])

    def test_no_duplicate_ids(self):
        c = parse(os.path.join(WEB, "console.html"))
        dup = sorted({i for i in c.ids if c.ids.count(i) > 1})
        self.assertEqual(dup, [], "重复的 id 会让 getElementById 拿到错的那一份：%s" % dup)

    def test_every_unguarded_js_lookup_has_an_element(self):
        """JS 里没加保护就用的 `$('xxx')`，那个 id 必须真的在页面里。

        面板以前有过「永远加载中」的板块：不是接口坏了，是那个 id 早被删了，
        JS 拿到 null 直接抛错，界面就停在最后一句占位文案上。
        写成 `if($('x')) $('x').value = …` 的算有保护，允许它指向还没做的控件。
        """
        with io.open(os.path.join(WEB, "console.html"), encoding="utf-8") as f:
            html = f.read()
        with io.open(os.path.join(WEB, "static", "console.js"), encoding="utf-8") as f:
            js = f.read()
        have = set(re.findall(r'id="([^"]+)"', html))
        missing = []
        for wid in sorted(set(re.findall(r"\$\('([a-zA-Z][\w-]*)'\)", js))):
            if wid in have:
                continue
            if re.search(r"if\(\$\('%s'\)" % re.escape(wid), js):
                continue
            missing.append(wid)
        self.assertEqual(missing, [], "JS 直接用、页面里却没有的 id：%s" % missing[:12])


if __name__ == "__main__":
    unittest.main()
