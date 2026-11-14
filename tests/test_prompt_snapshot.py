"""回归锁：面板上的「提示词预览」必须是她真的看到的那一份。

线上实测的问题：`remember_prompt_snapshot` 只存 `messages[:8]` ——
剪掉的是**尾巴**，而尾巴才是这一轮：当前用户消息、时间语境、好感真值、
看图提示全在那里。快照看着像样，实际把最要紧的部分全丢了。

（当初就是被这个假象骗过一次：从快照里找不到「好感度真值」，差点结论成
「提示词没注入」。）
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import qq_openclaw_bridge as b      # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="snap_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        shutil.rmtree(self.dir, ignore_errors=True)

    def snap(self):
        p = os.path.join(self.dir, "prompt_snapshot.json")
        with io.open(p, encoding="utf-8") as f:
            return json.load(f)["group"]


class SnapshotTests(Base):
    def _msgs(self, n):
        out = [{"role": "system", "content": "系统提示 %d" % i} for i in range(4)]
        for i in range(n):
            out.append({"role": "user" if i % 2 == 0 else "assistant",
                        "content": "历史 %d" % i})
        out.append({"role": "user",
                    "content": "【好感度真值】你对他有好感 85 分\n用户消息：那查询一下好感度"})
        return out

    def test_the_current_turn_is_kept(self):
        b.remember_prompt_snapshot(self._msgs(10), "group", "g1", "chat")
        snap = self.snap()
        joined = "\n".join(m["text"] for m in snap["messages"])
        self.assertIn("好感度真值", joined, "尾巴丢了 = 预览不是她看到的那份")
        self.assertIn("用户消息：那查询一下好感度", joined)

    def test_system_prompt_is_kept_too(self):
        b.remember_prompt_snapshot(self._msgs(10), "group", "g1", "chat")
        joined = "\n".join(m["text"] for m in self.snap()["messages"])
        self.assertIn("系统提示 0", joined, "开头的人格也不能丢")

    def test_trimming_is_visible_not_silent(self):
        b.remember_prompt_snapshot(self._msgs(30), "group", "g1", "chat")
        snap = self.snap()
        self.assertGreater(snap["count"], len(snap["messages"]),
                           "被剪了多少要看得出来")
        self.assertTrue(any("省略" in m["text"] for m in snap["messages"]),
                        "中间省略要有标记，不能假装这就是全部")

    def test_images_are_placeholders(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
        b.remember_prompt_snapshot(msgs, "group", "g1", "chat")
        text = "\n".join(m["text"] for m in self.snap()["messages"])
        self.assertIn("[图片已省略]", text)
        self.assertNotIn("base64", text)


if __name__ == "__main__":
    unittest.main()