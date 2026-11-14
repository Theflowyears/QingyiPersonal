"""回归锁：云端控制台的观察员账号 —— 只能看，不能改；admin 一点不变。

为什么用子进程：website/app.py 和 bot/project_config.py 同名，同进程 import
会拿错模块（test_visit_recording 里已经踩过）。这里 PYTHONPATH 只给 website/。

锁住的事：
  · 观察员凭据独立（CEYLAN_OBSERVER_USER/HASH），没配就登不进；
  · 观察员能读全部面板与设置，但任何 /admin 写操作一律 403，且不落盘；
  · NapCat 登录二维码不给观察员（拿到就能扫码接管她的 QQ）；
  · admin 的登录、读写、旧会话（没有 role 字段）全部保持原样；
  · 前端有只读横幅与 fetch 拦截，点了保存也会被告知只读。
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "website")

SCRIPT = textwrap.dedent('''
    import os, sys, json
    sys.path.insert(0, os.environ["WEB_DIR"])
    import app
    from pathlib import Path

    out = {}
    admin = app.app.test_client()
    obs = app.app.test_client()
    anon = app.app.test_client()
    bad = app.app.test_client()

    def login(client, user, pw):
        return client.post("/admin/login",
                           data={"username": user, "password": pw}).status_code

    out["admin_login"] = login(admin, os.environ["CEYLAN_ADMIN_USER"], "admin-pass")
    out["obs_login"] = login(obs, os.environ.get("CEYLAN_OBSERVER_USER", ""), "watch-pass")
    out["bad_obs_login"] = login(bad, os.environ.get("CEYLAN_OBSERVER_USER", ""), "wrong-pass")

    def me(client):
        r = client.get("/admin/api/me")
        try:
            return {"code": r.status_code, "body": r.get_json()}
        except Exception:
            return {"code": r.status_code, "body": None}

    out["me_admin"] = me(admin)
    out["me_obs"] = me(obs)
    out["me_anon"] = me(anon)
    out["me_bad"] = me(bad)

    # 观察员读面板与设置：全部要通
    reads = {}
    for path in ("/admin", "/admin/dashboard", "/admin/api/bot/personality",
                 "/admin/api/bot/control", "/admin/api/bot/telemetry",
                 "/admin/api/bot/effective-config", "/admin/api/visits",
                 "/admin/export.csv", "/admin/api/bot/models-config"):
        reads[path] = obs.get(path).status_code
    out["obs_reads"] = reads

    # 观察员写面板：全部要 403，而且一个字节都不许落盘
    writes = {}
    for method, path in (("POST", "/admin/api/bot/personality"),
                         ("POST", "/admin/api/bot/control"),
                         ("POST", "/admin/api/bot/models"),
                         ("POST", "/admin/api/bot/play"),
                         ("POST", "/admin/api/clipboard"),
                         ("PUT", "/admin/api/clipboard/abc"),
                         ("DELETE", "/admin/api/clipboard/abc"),
                         ("POST", "/admin/api/bot/memories/rebuild"),
                         ("POST", "/admin/api/bot/style/reload"),
                         ("POST", "/admin/api/bot/health"),
                         ("POST", "/admin/api/bot/custom-memory")):
        r = obs.open(path, method=method, json={"name": "hacked"})
        body = r.get_json(silent=True) or {}
        writes[method + " " + path] = {"code": r.status_code,
                                       "readonly": body.get("error") == "observer_readonly"}
    out["obs_writes"] = writes
    persona = Path(os.environ["DATA_DIR"]) / "persona.json"
    out["persona_untouched"] = not persona.exists()

    # 登录二维码是接管 QQ 的钥匙：观察员拿不到，admin 不受这道门禁影响
    r = obs.get("/admin/api/bot/napcat/qr")
    out["obs_qr"] = {"code": r.status_code,
                     "readonly": (r.get_json(silent=True) or {}).get("error") == "observer_readonly"}
    out["admin_qr_code"] = admin.get("/admin/api/bot/napcat/qr").status_code

    # admin 的写必须照常（账号行为一点没变）
    r = admin.post("/admin/api/bot/personality",
                   json={"name": "清漪", "persona": "测试人设", "trigger_words": "清漪"})
    out["admin_write"] = {"code": r.status_code, "body": r.get_json()}
    out["persona_saved"] = persona.exists() and json.loads(
        persona.read_text(encoding="utf-8")).get("name") == "清漪"

    # 旧会话（只有 ceylan_admin，没有 role）必须仍按 admin 对待
    legacy = app.app.test_client()
    with legacy.session_transaction() as s:
        s["ceylan_admin"] = True
    r = legacy.post("/admin/api/bot/personality", json={"name": "旧会话"})
    out["legacy_write"] = r.status_code
    out["legacy_me"] = me(legacy)

    # 观察员登出、以及拿 admin 凭据重新登录（换账号）都要允许
    out["obs_logout"] = obs.get("/admin/logout").status_code
    out["obs_after_logout"] = me(obs)["code"]
    out["obs_relogin_admin"] = login(obs, os.environ["CEYLAN_ADMIN_USER"], "admin-pass")
    out["obs_after_relogin"] = me(obs)

    print("RESULT" + json.dumps(out, ensure_ascii=True))
''')

NO_OBSERVER_SCRIPT = textwrap.dedent('''
    import os, sys, json
    sys.path.insert(0, os.environ["WEB_DIR"])
    import app
    out = {}
    c = app.app.test_client()
    out["obs_login"] = c.post("/admin/login", data={
        "username": "watcher", "password": "watch-pass"}).status_code
    out["me"] = c.get("/admin/api/me").status_code
    a = app.app.test_client()
    out["admin_login"] = a.post("/admin/login", data={
        "username": os.environ["CEYLAN_ADMIN_USER"],
        "password": "admin-pass"}).status_code
    print("RESULT" + json.dumps(out, ensure_ascii=True))
''')


def run_script(script, with_observer):
    from werkzeug.security import generate_password_hash
    tmp = tempfile.mkdtemp(prefix="observer_")
    env = dict(os.environ)
    env.update({
        "WEB_DIR": WEB,
        "PYTHONPATH": WEB,
        "PYTHONIOENCODING": "utf-8",
        "WEB_HOST": "127.0.0.1",
        "CEYLAN_SECRET_KEY": "test-only",
        "CEYLAN_ADMIN_USER": "boss",
        "CEYLAN_ADMIN_HASH": generate_password_hash("admin-pass"),
        "CEYLAN_DB_PATH": os.path.join(tmp, "visitors.db"),
        "CEYLAN_TELEMETRY_DB": os.path.join(tmp, "telemetry.db"),
        "CEYLAN_CONTROL_DB": os.path.join(tmp, "control.db"),
        "CEYLAN_KEYS_DB": os.path.join(tmp, "keys.db"),
        "CEYLAN_CLIPBOARD_DB": os.path.join(tmp, "clipboard.db"),
        "CEYLAN_CONTROL_DIR": os.path.join(tmp, "control-v4"),
        "DATA_DIR": os.path.join(tmp, "data"),
    })
    env.pop("NO_PROXY", None)
    env.pop("CEYLAN_OBSERVER_USER", None)
    env.pop("CEYLAN_OBSERVER_HASH", None)
    if with_observer:
        env["CEYLAN_OBSERVER_USER"] = "watcher"
        env["CEYLAN_OBSERVER_HASH"] = generate_password_hash("watch-pass")
    proc = subprocess.run([sys.executable, "-c", script], env=env, cwd=WEB,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=240)
    if proc.returncode != 0:
        raise AssertionError("子进程没跑起来：%s\n%s" % (proc.stdout[-2000:],
                                                        proc.stderr[-2000:]))
    line = [l for l in proc.stdout.splitlines() if l.startswith("RESULT")]
    if not line:
        raise AssertionError("没拿到结果：%s" % proc.stdout[-2000:])
    return json.loads(line[0][len("RESULT"):])


class ObserverRoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_script(SCRIPT, with_observer=True)

    def test_both_accounts_log_in_with_their_own_credentials(self):
        self.assertEqual(self.out["admin_login"], 302)
        self.assertEqual(self.out["obs_login"], 302)
        self.assertEqual(self.out["bad_obs_login"], 200, "错密码应留在登录页")
        self.assertEqual(self.out["me_bad"]["code"], 401)

    def test_me_reports_the_role(self):
        self.assertEqual(self.out["me_admin"]["body"]["role"], "admin")
        self.assertEqual(self.out["me_admin"]["body"]["username"], "boss")
        self.assertEqual(self.out["me_obs"]["body"]["role"], "observer")
        self.assertEqual(self.out["me_obs"]["body"]["username"], "watcher")
        self.assertEqual(self.out["me_anon"]["code"], 401)

    def test_observer_can_read_every_panel(self):
        for path, code in self.out["obs_reads"].items():
            with self.subTest(path=path):
                self.assertEqual(code, 200)

    def test_observer_cannot_write_anything(self):
        for key, got in self.out["obs_writes"].items():
            with self.subTest(endpoint=key):
                self.assertEqual(got["code"], 403)
                self.assertTrue(got["readonly"], "要返回 observer_readonly 标记")
        self.assertTrue(self.out["persona_untouched"], "403 之外还不能落盘")

    def test_observer_cannot_take_the_qq_login_qr(self):
        self.assertEqual(self.out["obs_qr"]["code"], 403)
        self.assertTrue(self.out["obs_qr"]["readonly"])
        self.assertNotIn(self.out["admin_qr_code"], (403,),
                         "admin 不该被观察员门禁拦（没有 docker 时是 404/500）")

    def test_admin_write_is_unchanged(self):
        self.assertEqual(self.out["admin_write"]["code"], 200)
        self.assertTrue(self.out["admin_write"]["body"].get("ok"))
        self.assertTrue(self.out["persona_saved"])

    def test_legacy_admin_session_keeps_full_access(self):
        self.assertEqual(self.out["legacy_write"], 200)
        self.assertEqual(self.out["legacy_me"]["body"]["role"], "admin")

    def test_observer_logout_and_account_switch(self):
        self.assertEqual(self.out["obs_logout"], 302)
        self.assertEqual(self.out["obs_after_logout"], 401)
        self.assertEqual(self.out["obs_relogin_admin"], 302)
        self.assertEqual(self.out["obs_after_relogin"]["body"]["role"], "admin")


class ObserverDisabledTests(unittest.TestCase):
    def test_without_observer_env_the_account_does_not_exist(self):
        out = run_script(NO_OBSERVER_SCRIPT, with_observer=False)
        self.assertEqual(out["obs_login"], 200, "没配观察员时不能登进去")
        self.assertEqual(out["me"], 401)
        self.assertEqual(out["admin_login"], 302, "admin 不受影响")


class ObserverFrontendLockTests(unittest.TestCase):
    """前端结构锁：横幅元素要在，JS 拦截与角色读取要在。"""

    def test_banner_element_exists(self):
        with io.open(os.path.join(WEB, "console.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn('id="observer-banner"', html)

    def test_js_reads_role_and_blocks_mutations(self):
        with io.open(os.path.join(WEB, "static", "console.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("/admin/api/me", js)
        self.assertIn("observer-banner", js)
        self.assertIn("observer_readonly", js)


if __name__ == "__main__":
    unittest.main()