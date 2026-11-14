"""面板上「阶段2.5 准入判定」这一块。

用户提这一块的缘由很具体：面板原来展示的是校准 10/10 这类好看的数字，
而准入看的是另一码事（动作带不带证据、终态有没有回执）。
这两套混在一起，「检查通过」就会被读成「可以进阶段3」——
所以这里必须把每一项的真实状态，包括不好看的那些，原样摊开。
"""
import json
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
WEB = os.path.join(ROOT, "website")
for p in (BOT, WEB):
    if p not in sys.path:
        sys.path.insert(0, p) if p == BOT else sys.path.append(p)


def _make_app():
    from flask import Flask
    import ceylan_dashboard
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-only"
    ceylan_dashboard.install(app, {})
    return app


_SAMPLE = {
    "ok": True, "counts": {}, "phases": {},
    "phase25": {
        "status": "blocked", "window_seconds": 86400,
        "blockers": ["action_evidence", "consent_enforcement"],
        "checks": [
            {"key": "action_evidence", "status": "blocked",
             "observed": {"total": 111, "linked": 0, "missing": 111,
                          "dangling_refs": 0, "unusable_refs": 0},
             "reason": "Every recent action needs existing evidence."},
            {"key": "consent_enforcement", "status": "not_verified", "observed": None,
             "reason": "This ledger audit cannot establish end-to-end enforcement."},
        ],
        "shadow_review": {"calibration_passed": 10, "calibration_total": 10},
    },
    "stage3": {"authorized": False, "expression_control": False,
               "stage3a": "blocked", "stage3b": "blocked"},
}


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rdpanel_")
        self.addCleanup(__import__('shutil').rmtree, self.dir, True)
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        self.client = _make_app().test_client()
        with self.client.session_transaction() as s:
            s["ceylan_admin"] = True

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old

    def test_it_shows_the_blocked_checks_not_only_the_pretty_ones(self):
        import phase_audit
        with patch.object(phase_audit, "audit_readiness", return_value=_SAMPLE):
            r = self.client.get("/admin/api/bot/readiness")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        keys = {c["key"]: c["status"] for c in body["phase25"]["checks"]}
        self.assertEqual(keys, {"action_evidence": "blocked",
                               "consent_enforcement": "not_verified"})
        self.assertEqual(body["phase25"]["status"], "blocked")
        self.assertEqual(body["phase25"]["blockers"],
                         ["action_evidence", "consent_enforcement"])

    def test_the_endpoint_never_claims_stage_three_is_open(self):
        """这一条是防「面板说好就是好」：authorized 只能是 false。"""
        import phase_audit
        with patch.object(phase_audit, "audit_readiness", return_value=_SAMPLE):
            body = self.client.get("/admin/api/bot/readiness").get_json()
        self.assertFalse(body["stage3"]["authorized"])
        self.assertFalse(body["stage3"]["expression_control"])

    def test_an_audit_failure_degrades_instead_of_passing(self):
        import phase_audit
        with patch.object(phase_audit, "audit_readiness",
                          side_effect=RuntimeError("ledger locked")):
            r = self.client.get("/admin/api/bot/readiness")
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "RuntimeError")

    def test_http_success_does_not_overwrite_failed_audit(self):
        import phase_audit
        sample = dict(_SAMPLE, ok=False)
        with patch.object(phase_audit, 'audit_readiness', return_value=sample):
            response = self.client.get('/admin/api/bot/readiness')
        self.assertEqual(200, response.status_code)
        self.assertFalse(response.get_json()['ok'])
        self.assertTrue(response.get_json()['transport_ok'])

    def test_cognition_controls_require_session_csrf_and_explicit_reason(self):
        url = '/admin/api/bot/cognition-control'
        payload = dict(operation='consent', recipient='19', action='dm',
                       decision='deny', reason='explicit administrator instruction')
        self.assertEqual(403, self.client.post(url, json=payload).status_code)
        with self.client.session_transaction() as s:
            s['bot_control_csrf'] = 'test-token'
        headers = {'X-CSRF-Token': 'test-token'}
        import self_model
        with patch.object(self_model, 'record_consent', return_value='consent-id') as record:
            response = self.client.post(url, json=payload, headers=headers)
            self.assertEqual(200, response.status_code)
            record.assert_called_once_with('19', 'dm', 'deny', payload['reason'])
        self.assertEqual(400, self.client.post(url, json=dict(payload, reason=''), headers=headers).status_code)
        with self.client.session_transaction() as s:
            s['ceylan_admin'] = False
        self.assertEqual(401, self.client.post(url, json=payload, headers=headers).status_code)

    def test_it_stays_behind_the_admin_session(self):
        with self.client.session_transaction() as s:
            s["ceylan_admin"] = False
        self.assertEqual(self.client.get("/admin/api/bot/readiness").status_code, 401)


class FrontendTests(unittest.TestCase):
    """卡片在前端，判定文字不能是前端自己编的。"""

    def setUp(self):
        with open(os.path.join(WEB, "static", "console.js"), encoding="utf-8") as f:
            self.js = f.read()
        with open(os.path.join(WEB, "console.html"), encoding="utf-8") as f:
            self.html = f.read()

    def test_the_card_reads_the_endpoint_and_the_real_verdict(self):
        self.assertIn("readiness", self.js)
        block = self.js[self.js.index("async function loadReadiness"):]
        self.assertIn("phase25", block)
        self.assertIn("stage3", block)

    def test_observed_numbers_are_not_hardcoded_into_the_card(self):
        """面板不许出现「10/10」这类现成数字：读数只能来自账本。"""
        block = self.js[self.js.index("async function loadReadiness"):]
        block = block[:block.index("\n  }")]
        self.assertIsNone(re.search(r"'10/10'|\"10/10\"", block))

    def test_text_from_the_ledger_is_escaped(self):
        block = self.js[self.js.index("async function loadReadiness"):]
        self.assertIn("esc(", block)


if __name__ == "__main__":
    unittest.main()
