"""回归锁：把本地视频临时公布出去的那扇门（HMAC 签名 URL）。

四条不能松的线：
  1) URL 里只有随机 token，不带文件名也不带目录 —— 猜不到、也看不出盘上结构。
  2) token 被改一个字符就失效；过期就失效。
  3) resolve 只认 token，并且落点必须在 media_cache 里（防注册表被改成 ../../etc）。
  4) cleanup 只删过期的，没过期的那条必须还能取。
全部在临时 DATA_DIR 里跑，不联网、不碰真实 data/。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import media_public as mp       # noqa: E402

BASE = "https://media.example.com"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mp_")
        self._env = {k: os.environ.get(k) for k in
                     ("DATA_DIR", "PUBLIC_MEDIA_BASE_URL",
                      "MEDIA_PUBLIC_BASE_URL", "MEDIA_PUBLIC_SECRET")}
        os.environ["DATA_DIR"] = self.dir
        for k in ("PUBLIC_MEDIA_BASE_URL", "MEDIA_PUBLIC_BASE_URL",
                  "MEDIA_PUBLIC_SECRET"):
            os.environ.pop(k, None)
        self.cache = os.path.join(self.dir, "media_cache")
        os.makedirs(self.cache, exist_ok=True)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)

    def video(self, name="a.mp4", body=b"x" * 32):
        path = os.path.join(self.cache, name)
        with open(path, "wb") as f:
            f.write(body)
        return path

    def token_of(self, url):
        return url.rsplit("/", 1)[-1]

    def registry(self):
        with open(os.path.join(self.cache, "registry.json"), encoding="utf-8") as f:
            return json.load(f)


class PublishTests(Base):
    def test_no_base_is_explicit_not_silent(self):
        got = mp.publish(self.video())
        self.assertFalse(got["ok"])
        self.assertEqual(got["reason"], "no_public_base")

    def test_explicit_base_url_wins_and_token_hides_path(self):
        path = self.video("secret-name.mp4")
        got = mp.publish(path, base_url=BASE)
        self.assertTrue(got["ok"], got)
        url = got["url"]
        self.assertTrue(url.startswith(BASE + "/media/"), url)
        self.assertNotIn("secret-name", url)
        self.assertNotIn("media_cache", url)
        self.assertNotIn(self.dir.replace("\\", "/"), url)
        # 注册表里存的是路径，URL 里永远只有 token
        reg = self.registry()
        self.assertIn(self.token_of(url), reg["items"])
        self.assertEqual(os.path.realpath(reg["items"][self.token_of(url)]["path"]),
                         os.path.realpath(path))

    def test_env_base_and_precedence(self):
        os.environ["MEDIA_PUBLIC_BASE_URL"] = "https://fallback.example.com"
        got = mp.publish(self.video())
        self.assertTrue(got["ok"], got)
        self.assertTrue(got["url"].startswith("https://fallback.example.com/media/"))
        os.environ["PUBLIC_MEDIA_BASE_URL"] = BASE
        got2 = mp.publish(self.video("b.mp4"))
        self.assertTrue(got2["url"].startswith(BASE + "/media/"), got2["url"])

    def test_two_publishes_get_different_unguessable_tokens(self):
        a = mp.publish(self.video(), base_url=BASE)
        b = mp.publish(self.video("b.mp4"), base_url=BASE)
        self.assertNotEqual(a["token"], b["token"])
        self.assertGreaterEqual(len(a["token"]), 32)

    def test_path_outside_cache_is_refused(self):
        outside = os.path.join(self.dir, "outside.mp4")
        with open(outside, "wb") as f:
            f.write(b"x")
        got = mp.publish(outside, base_url=BASE)
        self.assertFalse(got["ok"])
        self.assertIn("outside", got["reason"])


class ResolveTests(Base):
    def test_roundtrip_returns_the_published_file(self):
        path = self.video()
        got = mp.publish(path, base_url=BASE)
        self.assertEqual(os.path.realpath(mp.resolve(got["token"])),
                         os.path.realpath(path))

    def test_tampered_token_is_rejected(self):
        path = self.video()
        token = mp.publish(path, base_url=BASE)["token"]
        last = token[-1]
        bad = token[:-1] + ("0" if last != "0" else "1")
        self.assertEqual(mp.resolve(bad), "")
        self.assertEqual(mp.resolve(token[:-1]), "")
        self.assertEqual(mp.resolve("../../etc/passwd"), "")
        self.assertEqual(mp.resolve(""), "")

    def test_expired_token_is_rejected(self):
        path = self.video()
        with mock.patch.object(mp, "_now", return_value=1000.0):
            token = mp.publish(path, ttl=5, base_url=BASE)["token"]
            self.assertTrue(mp.resolve(token))
            with mock.patch.object(mp, "_now", return_value=1006.0):
                self.assertEqual(mp.resolve(token), "")

    def test_registry_path_escape_is_rejected(self):
        path = self.video()
        token = mp.publish(path, base_url=BASE)["token"]
        outside = os.path.join(self.dir, "outside.mp4")
        with open(outside, "wb") as f:
            f.write(b"x")
        reg = self.registry()
        reg["items"][token]["path"] = outside
        with open(os.path.join(self.cache, "registry.json"), "w",
                  encoding="utf-8") as f:
            json.dump(reg, f)
        self.assertEqual(mp.resolve(token), "",
                         "注册表被改成越界路径时必须拒绝")

    def test_missing_file_is_not_resolved(self):
        path = self.video()
        token = mp.publish(path, base_url=BASE)["token"]
        os.remove(path)
        self.assertEqual(mp.resolve(token), "")


class CleanupTests(Base):
    def test_cleanup_removes_only_expired(self):
        old = self.video("old.mp4")
        keep = self.video("keep.mp4")
        with mock.patch.object(mp, "_now", return_value=2000.0):
            old_token = mp.publish(old, ttl=1, base_url=BASE)["token"]
            keep_token = mp.publish(keep, ttl=1000, base_url=BASE)["token"]
        with mock.patch.object(mp, "_now", return_value=2001.5):
            removed = mp.cleanup()
            self.assertEqual(removed, 1)
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(keep))
            self.assertEqual(mp.resolve(keep_token), os.path.realpath(keep))
            self.assertEqual(mp.resolve(old_token), "")
            self.assertNotIn(old_token, self.registry()["items"])

    def test_cleanup_drops_registration_for_missing_file(self):
        path = self.video()
        token = mp.publish(path, ttl=900, base_url=BASE)["token"]
        os.remove(path)
        self.assertEqual(mp.cleanup(), 0)
        self.assertNotIn(token, self.registry()["items"])

    def test_cleanup_does_not_touch_live_files(self):
        keep = self.video()
        mp.publish(keep, ttl=900, base_url=BASE)
        self.assertEqual(mp.cleanup(), 0)
        self.assertTrue(os.path.exists(keep))


if __name__ == "__main__":
    unittest.main()
