"""回归锁：她自己上网时「打开网页读正文」这一层。

搜索摘要不等于读过网页；但为了读正文去 fetch 任意 URL，等于把内网的门
交给搜索结果页。这里锁住两件事：私网地址一次都不发，正文抽取不执行脚本。
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import web_read        # noqa: E402

PAGE = ("<html><head><title>明日方舟 wiki</title>"
        "<style>body{color:red}</style></head><body>"
        "<script>alert('x')</script><nav>导航</nav>"
        "<h1>新角色</h1><p>抽卡概率 &amp; 机制说明。</p>"
        "<p>第二段正文，足够长才值得记。</p></body></html>")


class UrlGuardTests(unittest.TestCase):
    def test_private_and_local_targets_are_rejected(self):
        for url in ("http://127.0.0.1/admin",
                    "http://localhost:5000/",
                    "http://10.0.0.8/",
                    "http://169.254.169.254/latest/meta-data",
                    "http://[::1]/",
                    "file:///etc/passwd",
                    "javascript:alert(1)",
                    "https://user:pass@example.com/"):
            with self.subTest(url=url):
                self.assertEqual(web_read.safe_url(url), "")

    def test_public_url_passes(self):
        self.assertTrue(web_read.safe_url("https://example.com/a?b=1"))


class ExtractTests(unittest.TestCase):
    def test_scripts_and_styles_do_not_reach_her(self):
        title, text = web_read.extract(PAGE)
        self.assertEqual(title, "明日方舟 wiki")
        self.assertIn("抽卡概率 & 机制说明", text)
        self.assertIn("第二段正文", text)
        for bad in ("alert", "color:red", "导航"):
            self.assertNotIn(bad, text)

    def test_blank_runs_collapse(self):
        _, text = web_read.extract("<p>甲</p>\n\n\n<p>乙</p>")
        self.assertNotIn("\n\n\n", text)

    def test_text_is_capped(self):
        _, text = web_read.extract("<p>" + ("字" * 5000) + "</p>")
        self.assertLessEqual(len(text), web_read.MAX_CHARS)


class FetchTests(unittest.TestCase):
    def test_private_url_never_downloads(self):
        calls = []

        async def fake(url, client=None):
            calls.append(url)
            return {"ok": True, "body": PAGE.encode("utf-8"),
                    "content_type": "text/html", "final_url": url}

        with patch.object(web_read, "_download", fake):
            got = asyncio.run(web_read.fetch_page("http://127.0.0.1/x"))
        self.assertFalse(got["ok"])
        self.assertEqual(calls, [])

    def test_html_page_is_readable(self):
        async def fake(url, client=None):
            return {"ok": True, "body": PAGE.encode("utf-8"),
                    "content_type": "text/html; charset=utf-8",
                    "final_url": url}

        with patch.object(web_read, "_download", fake):
            got = asyncio.run(web_read.fetch_page("https://example.com/a"))
        self.assertTrue(got["ok"])
        self.assertEqual(got["title"], "明日方舟 wiki")
        self.assertIn("抽卡概率", got["text"])
        self.assertGreater(got["chars"], 10)

    def test_non_html_is_refused(self):
        async def fake(url, client=None):
            return {"ok": True, "body": b"%PDF-1.7", "content_type": "application/pdf",
                    "final_url": url}

        with patch.object(web_read, "_download", fake):
            got = asyncio.run(web_read.fetch_page("https://example.com/a.pdf"))
        self.assertFalse(got["ok"])
        self.assertIn("不是网页", got["reason"])


if __name__ == "__main__":
    unittest.main()