"""悬浮窗这一侧的锁：点一张才谈她的理解，删除要两步，格子里不许铺摘要。

主人说得很具体：
    「列出她对表情包的总结，然后由我修改（通过点击对应的表情包展示悬浮窗，
      不要直接在界面展示）」

所以这里锁的都是「界面行为的形式」，不是函数返回值：
  · 格子必须是一个入口（data-sid + role=button + 能键盘操作），不是死图；
  · 她的总结只出现在悬浮窗里，格子的 title 不许带含义文字；
  · 保存/删除都带 CSRF（写操作，光 cookie 能被别的页面触发）；
  · 删除两步：先摊开确认条，第二次点击才真删，而且不许用原生 confirm；
  · 筛选要能只看「我校准过的」，否则人工筛到一半找不到进度。
"""
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "website" / "console.html").read_text(encoding="utf-8")
JS = (ROOT / "website" / "static" / "console.js").read_text(encoding="utf-8")
CSS = (ROOT / "website" / "static" / "console.css").read_text(encoding="utf-8")


def block(src, start, length=6000):
    i = src.index(start)
    return src[i:i + length]


def cell_template(src):
    """只取「一格一格的 HTML」那一段：从拼接到 .join('') 结束。

    窗口不能宽——再往下就是 openStk，里面当然会出现 s.meaning（悬浮窗里要有），
    用固定长度会把「格子里不许铺摘要」这条锁测成假阳性。
    """
    i = src.index("box.innerHTML = shown.slice")
    j = src.index("}).join('');", i)
    self_check = src[i:j]
    assert len(self_check) < 1600, "格子模板取形太大，锁不住"
    return self_check


class PopupMarkupTests(unittest.TestCase):
    def test_popup_has_the_parts_a_human_needs(self):
        for el in ("stk-mask", "stk-img", "stk-title", "stk-meta", "stk-meaning",
                   "stk-meaning-src", "stk-save", "stk-del", "stk-confirm",
                   "stk-del-yes", "stk-del-no", "stk-msg", "stk-close"):
            self.assertIn('id="%s"' % el, HTML, "悬浮窗少了 " + el)

    def test_grid_cells_are_entries_not_dead_images(self):
        cells = cell_template(JS)
        self.assertIn('data-sid=', cells)
        self.assertIn('role="button"', cells)
        self.assertIn('tabindex="0"', cells)

    def test_meanings_are_not_smeared_across_the_grid(self):
        """格子里不许出现她的总结：title 只说「点开能做什么」。"""
        cells = cell_template(JS)
        self.assertNotIn("s.meaning", cells)
        self.assertIn("点开看她的理解", cells)

    def test_calibrated_stickers_are_marked_without_text(self):
        cells = cell_template(JS)
        self.assertIn("' cal'", cells)
        self.assertIn(".emoji-cell.cal::after", CSS)

    def test_filter_can_show_only_calibrated(self):
        self.assertIn('data-src="已校准"', HTML)
        self.assertIn("已校准", block(JS, "var shown = sk.filter"))

    def test_copy_says_where_delete_and_calibration_happen(self):
        hint = HTML[HTML.index('id="em-stk-hint"'):HTML.index('id="em-stk-hint"') + 400]
        self.assertIn("悬浮窗", hint.replace("<b>", "").replace("</b>", ""))


class PopupBehaviorTests(unittest.TestCase):
    def test_writes_carry_the_csrf_token(self):
        fn = block(JS, "async function stickerPost(")
        self.assertIn("X-CSRF-Token", fn)
        self.assertIn("csrfToken()", fn)

    def test_delete_needs_two_clicks(self):
        fn = block(JS, "async function stkDelete(")
        self.assertIn("STK.armed", fn)
        self.assertIn("stk-confirm", fn)

    def test_no_native_dialogs(self):
        # 面板一贯不许原生控件：confirm/alert 弹的是浏览器的框，风格与流程都断
        zone = block(JS, "function openStk(", 4000) + block(JS, "async function stkDelete(")
        self.assertIsNone(re.search(r"\bconfirm\s*\(", zone))
        self.assertIsNone(re.search(r"\balert\s*\(", zone))

    def test_saving_explains_where_the_meaning_will_be_used(self):
        fn = block(JS, "async function stkSave(")
        self.assertIn("已生效", fn)

    def test_clearing_is_reported_as_a_judgement_not_as_nothing(self):
        fn = block(JS, "async function stkSave(")
        self.assertIn("没有固定含义", fn)

    def test_meaning_source_is_told_apart(self):
        # 三档可信度都要在窗里说明白，不然「谁说的」这一层就丢了
        fn = block(JS, "function openStk(")
        self.assertIn("你校准过的", fn)
        self.assertIn("群里有人教她的", fn)
        self.assertIn("自己看图猜的", fn)
        self.assertIn("还没看懂过这张", fn)

    def test_popup_is_bound_after_the_grid_renders(self):
        fn = block(JS, "async function loadEmojis(")
        self.assertIn("bindStickerPop()", fn)

    def test_stats_line_counts_calibrated_and_trash(self):
        fn = block(JS, "var cal = sk.filter(stkCalibrated)")
        self.assertIn("你校准过", fn)
        self.assertIn("回收目录", fn)

    def test_escapes_every_interpolated_value_in_the_popup(self):
        # innerHTML 拼字符串就必须全走 esc()：标签、id、尺寸都是用户可见数据
        fn = block(JS, "var meta = [", 900)
        for token in ("esc(emSourceOf(s))", "esc(when)", "esc(s.kind)",
                      "esc(s.tags.join", "esc(s.id)"):
            self.assertIn(token, fn)


class StyleTests(unittest.TestCase):
    def test_popup_has_a_real_dialog_style(self):
        for sel in (".stk-mask", ".stk-pop", ".stk-preview", ".stk-confirm",
                    ".btn.danger"):
            self.assertIn(sel, CSS, sel + " 没定义，浮层会是一坨无样式文本")

    def test_mask_is_hidden_until_opened(self):
        self.assertIn(".stk-mask{", CSS)
        self.assertIn("display:none", CSS[CSS.index(".stk-mask{"):CSS.index(".stk-mask{") + 200])


if __name__ == "__main__":
    unittest.main()
