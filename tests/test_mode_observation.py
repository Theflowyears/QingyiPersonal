# -*- coding: utf-8 -*-
"""V04 四种运行模式观测隔离的回归锁（路线 §9 验收 / §4.3 确定性重放）。

钉死的行为（全部对真实生产者产物做端到端，不打桩断言口径）：
  · 盖章：四轨迹 certify 口径正确、自报在线被覆盖、未知轨迹大声拒绝、不改输入；
  · 普通报告分拣：v2_live 计在线，候选/历史/参考仅观测，自报在线被契约覆盖，
    缺 trajectory / 未知轨迹 / 非 dict 一律拒绝，缺字段不补造；
  · 产物形状锁：候选 run/脱敏视图/valence_samples、实时 shadow status（含嵌套
    历史/候选）、历史 retrospective、S06 矩阵、S03 修订重放、v1 合成参考、
    R07 消融评审包、S08 计划/结果/评审包——任何 counts_for_live=True /
    expression_control=True 的冒充形状一律拒绝；
  · 跨模式污染：混合产物稳定分拣四类、保持顺序、确定性、不改输入、reason_code
    全部落在 REASON_CODES 内、unknown 不冒充 pass。
AST 锁钉死旁路契约：import 面只许 copy + emotion_trajectories；禁 import
emotion_shadow 写入器/paramiko/ops_remote/bridge/网络/存储；禁墙钟、禁随机、
禁写操作、禁顶层冻路径、禁第二套动力学；必须复用 trajectories.stamp。

全部用隔离临时 DATA_DIR 与合成夹具，不读不写真实 data/，不发网络、不触发 QQ 发送。
"""
import ast
import copy
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import mode_observation as mo            # noqa: E402
import emotion_trajectories as tr        # noqa: E402
import emotion_core as legacy            # noqa: E402
import emotion_dynamics as core          # noqa: E402
import candidate_replay as cr            # noqa: E402
import emotion_revision as rev           # noqa: E402
import emotion_replay_matrix as matrix   # noqa: E402
import emotion_replay as replay          # noqa: E402
import emotion_shadow as sh              # noqa: E402
import ripple_ablation as ra             # noqa: E402
import memory_retention as mr            # noqa: E402
import recall_emotion as s08             # noqa: E402

START, END = 900.0, 5000.0
NOW, RECALL_AT = 6000.0, 4500.0
R07_NOW = 4000000000.0
PRAISE = '谢谢你帮我查清楚了'


def msg(eid, text, ts, addressed=True, **extra):
    return {'id': eid, 'kind': 'message', 'ts': ts, 'source': 'onebot',
            'payload': {'text': text, 'addressed_to_self': addressed, **extra}}


def tool(eid, ts, ok):
    return {'id': eid, 'kind': 'tool_result', 'ts': ts, 'source': 'onebot',
            'payload': {'ok': ok}}


def standard_events():
    return [msg('p1', PRAISE, 1000), msg('n1', '今天天气不错', 2000, addressed=False),
            msg('r1', '你刚才说错了', 3000), tool('t1', 4000, False)]


def claim_item(oid, value, sources, **over):
    row = {'object_id': oid, 'object_kind': 'belief', 'store': 'cognition',
           'root_source_ids': list(sources), 'validity': 'valid',
           'subject_ref': 'per_1', 'restatement': value, 'occurred_at': 1000.0,
           'epistemic_kind': 'reported',
           'evidence_spans': [{'event_id': s, 'quote': value, 'subject_ref': 'per_1'}
                              for s in sources]}
    row.update(over)
    return row


def r07_fixture():
    s = claim_item('s', '喜欢茶', ['e1'])
    n1 = claim_item('n1', '喜欢咖啡', ['e2'])
    n2 = claim_item('n2', '喜欢可可', ['e3'])
    dead = claim_item('dead', '喜欢果汁', ['e4'], validity='invalidated')
    retention = mr.retention_set([s, n1, n2, dead], ['e1', 'e2', 'e3', 'e4'], mode='keep')
    edges = [{'edge_id': 'edge_s_n1', 'type': 'shared_entity', 'a': 's',
              'a_kind': 'belief', 'b': 'n1', 'b_kind': 'belief',
              'basis': {'reason': 'shared_entity'}},
             {'edge_id': 'edge_s_n2', 'type': 'shared_entity', 'a': 's',
              'a_kind': 'belief', 'b': 'n2', 'b_kind': 'belief',
              'basis': {'reason': 'shared_entity'}}]
    return retention, [s], edges


def s08_rendered():
    entry = {'object_id': 'm1', 'object_kind': 'belief', 'store': 'cognition',
             'epistemic_kind': 'reported', 'subject_ref': 'per_1',
             'root_source_ids': ['e1'], 'root_representative': 'e1',
             'occurred_at': 1000.0, 'validity': 'valid',
             'restatement': '合成记忆 m1', 'missing_slots': [], 'verdict_refs': []}
    return {'status': 'sufficient', 'scenes': [
        {'scene_id': 'scene_hand', 'scene_key': 'e1', 'root_representative': 'e1',
         'root_source_ids': ['e1'], 'entries': [entry], 'entry_count': 1}]}


class Base(unittest.TestCase):
    """隔离夹具：每个用例独立临时 DATA_DIR，路径调用时解析，不碰真实 data/。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v04_', ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self._old = {k: os.environ.get(k)
                     for k in ('DATA_DIR', 'PERSONA_DATA_DIR', 'EMOTION_SHADOW_ENABLED')}
        os.environ['DATA_DIR'] = self.root
        os.environ['PERSONA_DATA_DIR'] = self.root
        os.environ['EMOTION_SHADOW_ENABLED'] = '1'

    def tearDown(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def candidate_run(self):
        return cr.replay_candidates(standard_events(), {}, start_at=START,
                                    end_at=END, ts=5.0)

    def base_run(self, kernel='shadow-v2'):
        return rev.build_base_run(standard_events(), start_at=START, end_at=END,
                                  kernel=kernel, ts=5.0)

    def matrix_report(self):
        spec = matrix.case('c1', 'late', 'shadow-v2', {'checks_ok': True},
                           lambda: {'checks_ok': True})
        return matrix.run_matrix([spec])

    def r07_packet(self):
        retention, seeds, edges = r07_fixture()
        return ra.build_review_packet(retention, seeds=seeds, edges=edges, now=R07_NOW)

    def s08_plan(self):
        return s08.plan_experiment(standard_events(), s08_rendered(),
                                   now=NOW, recall_at=RECALL_AT)

    def s08_result(self):
        return s08.run_experiment(self.s08_plan())

    def s08_package(self):
        plan = self.s08_plan()
        return s08.review_package(plan, s08.run_experiment(plan))

    def seed_cognition(self, now):
        source = Path(self.root) / 'cognition.db'
        with closing(sqlite3.connect(source)) as c, c:
            c.execute('CREATE TABLE event(id TEXT PRIMARY KEY,kind TEXT,ts REAL,'
                      'session TEXT,actor_qq TEXT,topic_qq TEXT,source TEXT,'
                      'visibility TEXT,payload TEXT,dedupe TEXT,created REAL)')
            payload = {'text': '谢谢你', 'addressed_to_self': True,
                       'address_basis': 'private'}
            c.execute('INSERT INTO event VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      ('live', 'private_message', now - 1, 'private:one', '', '',
                       'onebot', 'private', json.dumps(payload), 'live', now - 1))
        return source

    def live_status(self, include_valence=True):
        now = time.time()
        self.seed_cognition(now)
        sh.tick(now=now)
        sh.tick(now=now + 1)
        return sh.status(now=now + 2, include_valence=include_valence)


# ── 盖章：口径由契约决定 ─────────────────────────────────────────────


class CertifyTests(Base):
    def test_v1_reference_stamp(self):
        out = mo.certify({'mode': 'x'}, tr.V1_REFERENCE)
        self.assertEqual(out['trajectory'], tr.V1_REFERENCE)
        self.assertEqual(out['kernel_version'], legacy.VERSION)
        self.assertFalse(out['counts_for_live'])

    def test_v2_live_stamp(self):
        out = mo.certify({}, tr.V2_LIVE)
        self.assertEqual(out['kernel_version'], core.VERSION)
        self.assertTrue(out['counts_for_live'])

    def test_candidate_stamp(self):
        out = mo.certify({}, tr.CANDIDATE_COUNTERFACTUAL)
        self.assertEqual(out['kernel_version'], core.SEMANTIC_VERSION)
        self.assertFalse(out['counts_for_live'])

    def test_historical_stamp(self):
        out = mo.certify({}, tr.HISTORICAL_REPLAY)
        self.assertEqual(out['kernel_version'], core.VERSION)
        self.assertFalse(out['counts_for_live'])

    def test_self_reported_live_overridden_on_candidate(self):
        out = mo.certify({'counts_for_live': True}, tr.CANDIDATE_COUNTERFACTUAL)
        self.assertFalse(out['counts_for_live'])

    def test_self_reported_live_overridden_on_historical(self):
        out = mo.certify({'counts_for_live': True}, tr.HISTORICAL_REPLAY)
        self.assertFalse(out['counts_for_live'])

    def test_self_reported_live_overridden_on_v1(self):
        out = mo.certify({'counts_for_live': True}, tr.V1_REFERENCE)
        self.assertFalse(out['counts_for_live'])

    def test_wrong_kernel_version_overridden(self):
        out = mo.certify({'kernel_version': 'bogus'}, tr.V2_LIVE)
        self.assertEqual(out['kernel_version'], core.VERSION)

    def test_v2_live_forced_true_even_if_producer_says_false(self):
        out = mo.certify({'counts_for_live': False}, tr.V2_LIVE)
        self.assertTrue(out['counts_for_live'])

    def test_unknown_trajectory_raises_loudly(self):
        with self.assertRaises(ValueError) as ctx:
            mo.certify({}, 'v3_imaginary')
        self.assertIn('unknown_trajectory', str(ctx.exception))

    def test_non_dict_raises(self):
        with self.assertRaises(ValueError):
            mo.certify([], tr.V2_LIVE)

    def test_input_not_mutated(self):
        report = {'counts_for_live': True, 'kernel_version': 'bogus', 'keep': 1}
        before = copy.deepcopy(report)
        mo.certify(report, tr.CANDIDATE_COUNTERFACTUAL)
        self.assertEqual(report, before)

    def test_other_fields_preserved(self):
        out = mo.certify({'events': 3, 'note': '保留'}, tr.HISTORICAL_REPLAY)
        self.assertEqual(out['events'], 3)
        self.assertEqual(out['note'], '保留')


# ── 普通报告分拣 ─────────────────────────────────────────────────────


class GenericClassifyTests(Base):
    def test_v2_live_generic_counts_online(self):
        entry = mo.classify({'trajectory': tr.V2_LIVE})
        self.assertEqual(entry['bucket'], 'live')
        self.assertIn('live_contract_ok', entry['reason_codes'])
        self.assertTrue(entry['caliber']['counts_for_live'])

    def test_candidate_generic_observation_only(self):
        entry = mo.classify({'trajectory': tr.CANDIDATE_COUNTERFACTUAL})
        self.assertEqual(entry['bucket'], 'observation_only')

    def test_historical_generic_observation_only(self):
        entry = mo.classify({'trajectory': tr.HISTORICAL_REPLAY})
        self.assertEqual(entry['bucket'], 'observation_only')

    def test_v1_generic_observation_only(self):
        entry = mo.classify({'trajectory': tr.V1_REFERENCE})
        self.assertEqual(entry['bucket'], 'observation_only')

    def test_self_reported_live_overridden(self):
        entry = mo.classify({'trajectory': tr.CANDIDATE_COUNTERFACTUAL,
                             'counts_for_live': True})
        self.assertEqual(entry['bucket'], 'observation_only')
        self.assertIn('self_reported_live_overridden', entry['reason_codes'])
        self.assertFalse(entry['caliber']['counts_for_live'])

    def test_missing_trajectory_rejected(self):
        entry = mo.classify({'mode': 'shadow'})
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('missing_trajectory', entry['reason_codes'])

    def test_unknown_trajectory_rejected(self):
        entry = mo.classify({'trajectory': 'v3_imaginary'})
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('unknown_trajectory', entry['reason_codes'])

    def test_non_string_trajectory_rejected(self):
        entry = mo.classify({'trajectory': 2})
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('unknown_trajectory', entry['reason_codes'])

    def test_non_dict_rejected(self):
        for value in (None, [], 'report', 7):
            entry = mo.classify(value)
            self.assertEqual(entry['bucket'], 'rejected')
            self.assertIn('not_a_dict', entry['reason_codes'])

    def test_expression_control_true_rejected(self):
        entry = mo.classify({'trajectory': tr.V2_LIVE, 'expression_control': True})
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('expression_control_true', entry['reason_codes'])

    def test_wrong_kernel_version_restamped(self):
        entry = mo.classify({'trajectory': tr.HISTORICAL_REPLAY,
                             'kernel_version': 'bogus'})
        self.assertEqual(entry['bucket'], 'observation_only')
        self.assertIn('caliber_restamped', entry['reason_codes'])
        self.assertEqual(entry['caliber']['kernel_version'], core.VERSION)

    def test_missing_counts_supplied_by_contract(self):
        entry = mo.classify({'trajectory': tr.V2_LIVE})
        self.assertTrue(entry['caliber']['counts_for_live'])
        self.assertNotIn('caliber_restamped', entry['reason_codes'])

    def test_input_not_mutated(self):
        report = {'trajectory': tr.CANDIDATE_COUNTERFACTUAL, 'counts_for_live': True}
        before = copy.deepcopy(report)
        mo.classify(report)
        self.assertEqual(report, before)


# ── 候选产物形状锁（真实 candidate_replay）──────────────────────────


class CandidateArtifactTests(Base):
    def test_real_candidate_run_observation_only(self):
        entry = mo.classify(self.candidate_run())
        self.assertEqual(entry['kind'], 'candidate_run')
        self.assertEqual(entry['bucket'], 'observation_only')
        self.assertIn('observation_contract_ok', entry['reason_codes'])

    def test_candidate_run_live_claim_rejected(self):
        run = self.candidate_run()
        run['counts_for_live'] = True
        entry = mo.classify(run)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('counts_for_live_mismatch', entry['reason_codes'])

    def test_candidate_run_expression_control_rejected(self):
        run = self.candidate_run()
        run['expression_control'] = True
        entry = mo.classify(run)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('expression_control_mismatch', entry['reason_codes'])

    def test_candidate_run_trajectory_impersonation_rejected(self):
        run = self.candidate_run()
        run['trajectory'] = tr.V2_LIVE
        entry = mo.classify(run)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('trajectory_mismatch', entry['reason_codes'])

    def test_candidate_run_kernel_mismatch_rejected(self):
        run = self.candidate_run()
        run['kernel_version'] = 'bogus'
        entry = mo.classify(run)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('kernel_version_mismatch', entry['reason_codes'])

    def test_candidate_run_nested_live_claim_rejected(self):
        run = self.candidate_run()
        run['timeline'][0]['counts_for_live'] = True
        entry = mo.classify(run)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('counts_for_live_true_present', entry['reason_codes'])

    def test_candidate_run_missing_field_rejected(self):
        run = self.candidate_run()
        del run['stats']
        entry = mo.classify(run)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('missing_stats', entry['reason_codes'])

    def test_desensitized_view_observation_only(self):
        view = cr.desensitize_report(self.candidate_run())
        entry = mo.classify(view)
        self.assertEqual(entry['kind'], 'candidate_view')
        self.assertEqual(entry['bucket'], 'observation_only')

    def test_desensitized_view_live_claim_rejected(self):
        view = cr.desensitize_report(self.candidate_run())
        view['counts_for_live'] = True
        entry = mo.classify(view)
        self.assertEqual(entry['bucket'], 'rejected')

    def test_valence_samples_observation_only(self):
        report = sh.valence_samples(now=time.time())
        entry = mo.classify(report)
        self.assertEqual(entry['kind'], 'valence_samples')
        self.assertEqual(entry['bucket'], 'observation_only')

    def test_valence_samples_live_claim_rejected(self):
        report = sh.valence_samples(now=time.time())
        report['counts_for_live'] = True
        entry = mo.classify(report)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('counts_for_live_mismatch', entry['reason_codes'])


# ── 实时 shadow status 与嵌套历史/候选（真实 emotion_shadow）─────────


class ShadowStatusTests(Base):
    def test_empty_status_is_live(self):
        entry = mo.classify(sh.status(now=time.time()))
        self.assertEqual(entry['kind'], 'shadow_status')
        self.assertEqual(entry['bucket'], 'live')
        self.assertTrue(entry['caliber']['counts_for_live'])

    def test_seeded_status_is_live_with_nested(self):
        status = self.live_status()
        self.assertIn('retrospective', status)
        entry = mo.classify(status)
        self.assertEqual(entry['bucket'], 'live')
        self.assertIn('live_contract_ok', entry['reason_codes'])

    def test_nested_retrospective_impersonation_rejected(self):
        status = self.live_status()
        status['retrospective']['counts_for_live'] = True
        entry = mo.classify(status)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('nested_retrospective_mismatch', entry['reason_codes'])

    def test_nested_valence_impersonation_rejected(self):
        status = self.live_status()
        status['valence_samples']['counts_for_live'] = True
        entry = mo.classify(status)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('nested_valence_mismatch', entry['reason_codes'])

    def test_status_live_flag_false_rejected(self):
        status = self.live_status()
        status['counts_for_live'] = False
        entry = mo.classify(status)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('counts_for_live_mismatch', entry['reason_codes'])

    def test_status_expression_control_rejected(self):
        status = self.live_status()
        status['expression_control'] = True
        entry = mo.classify(status)
        self.assertEqual(entry['bucket'], 'rejected')

    def test_retrospective_alone_observation_only(self):
        status = self.live_status()
        entry = mo.classify(status['retrospective'])
        self.assertEqual(entry['kind'], 'retrospective')
        self.assertEqual(entry['bucket'], 'observation_only')
        self.assertFalse(entry['caliber']['counts_for_live'])

    def test_retrospective_live_claim_rejected(self):
        status = self.live_status()
        retro = status['retrospective']
        retro['counts_for_live'] = True
        entry = mo.classify(retro)
        self.assertEqual(entry['bucket'], 'rejected')


# ── 历史矩阵 / 修订重放 / v1 参考（真实生产者）──────────────────────


class HistoricalArtifactTests(Base):
    def test_matrix_report_observation_only(self):
        entry = mo.classify(self.matrix_report())
        self.assertEqual(entry['kind'], 'matrix_report')
        self.assertEqual(entry['bucket'], 'observation_only')

    def test_matrix_live_claim_rejected(self):
        report = self.matrix_report()
        report['counts_for_live'] = True
        entry = mo.classify(report)
        self.assertEqual(entry['bucket'], 'rejected')

    def test_matrix_trajectory_impersonation_rejected(self):
        report = self.matrix_report()
        report['trajectory'] = tr.V2_LIVE
        entry = mo.classify(report)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('trajectory_mismatch', entry['reason_codes'])

    def test_matrix_provenance_network_rejected(self):
        report = self.matrix_report()
        report['provenance']['network'] = True
        entry = mo.classify(report)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('provenance_mismatch', entry['reason_codes'])

    def test_v2_base_run_is_historical(self):
        entry = mo.classify(self.base_run('shadow-v2'))
        self.assertEqual(entry['kind'], 'revision_run')
        self.assertEqual(entry['bucket'], 'observation_only')
        self.assertEqual(entry['caliber']['trajectory'], tr.HISTORICAL_REPLAY)

    def test_v1_base_run_is_reference(self):
        entry = mo.classify(self.base_run('shadow-v1'))
        self.assertEqual(entry['kind'], 'revision_run')
        self.assertEqual(entry['bucket'], 'observation_only')
        self.assertEqual(entry['caliber']['trajectory'], tr.V1_REFERENCE)

    def test_base_run_live_claim_rejected(self):
        run = self.base_run()
        run['counts_for_live'] = True
        entry = mo.classify(run)
        self.assertEqual(entry['bucket'], 'rejected')

    def test_base_run_kernel_trajectory_mismatch_rejected(self):
        run = self.base_run('shadow-v2')
        run['trajectory'] = tr.V1_REFERENCE
        entry = mo.classify(run)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('kernel_version_mismatch', entry['reason_codes'])

    def test_synthetic_reference_observation_only(self):
        bundle = {'histories': {'private-user-1': {'turns': [
            {'role': 'user', 'content': '谢谢你', 'ts': 100}]}}}
        entry = mo.classify(replay.reference_report(bundle))
        self.assertEqual(entry['kind'], 'synthetic_reference')
        self.assertEqual(entry['bucket'], 'observation_only')
        self.assertEqual(entry['caliber']['trajectory'], tr.V1_REFERENCE)

    def test_synthetic_reference_live_claim_rejected(self):
        bundle = {'histories': {'private-user-1': {'turns': [
            {'role': 'user', 'content': '谢谢你', 'ts': 100}]}}}
        report = replay.reference_report(bundle)
        report['counts_for_live'] = True
        entry = mo.classify(report)
        self.assertEqual(entry['bucket'], 'rejected')


# ── R07 消融评审包口径锁（真实 ripple_ablation）─────────────────────


class R07PacketTests(Base):
    def test_real_packet_manual_review(self):
        entry = mo.classify(self.r07_packet())
        self.assertEqual(entry['kind'], 'r07_review_packet')
        self.assertEqual(entry['bucket'], 'manual_review')
        self.assertIn('manual_review_ok', entry['reason_codes'])

    def test_review_constants_match_producer(self):
        self.assertEqual(mo.REVIEW_AUTHORIZATION, ra.REVIEW_AUTHORIZATION)
        self.assertEqual(mo.REVIEW_ORGANIC, ra.REVIEW_ORGANIC)

    def test_authorization_mismatch_rejected(self):
        packet = self.r07_packet()
        packet['review']['authorization'] = 'auto'
        entry = mo.classify(packet)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('review_authorization_mismatch', entry['reason_codes'])

    def test_organic_mismatch_rejected(self):
        packet = self.r07_packet()
        packet['review']['organic'] = 'observed'
        entry = mo.classify(packet)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('review_organic_mismatch', entry['reason_codes'])

    def test_judgement_not_none_rejected(self):
        packet = self.r07_packet()
        packet['review']['judgement'] = '关联有用'
        entry = mo.classify(packet)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('review_judgement_not_none', entry['reason_codes'])

    def test_nested_live_claim_rejected(self):
        packet = self.r07_packet()
        packet['variants'][0]['counts_for_live'] = True
        entry = mo.classify(packet)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('counts_for_live_true_present', entry['reason_codes'])

    def test_nested_expression_control_rejected(self):
        packet = self.r07_packet()
        packet['plan']['expression_control'] = True
        entry = mo.classify(packet)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('expression_control_true_present', entry['reason_codes'])

    def test_missing_review_rejected(self):
        packet = self.r07_packet()
        del packet['review']
        entry = mo.classify(packet)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('missing_review', entry['reason_codes'])


# ── S08 隔离实验计划/结果/评审包口径锁（真实 recall_emotion）─────────


class S08Tests(Base):
    def test_real_package_manual_review(self):
        entry = mo.classify(self.s08_package())
        self.assertEqual(entry['kind'], 's08_review_package')
        self.assertEqual(entry['bucket'], 'manual_review')

    def test_real_result_manual_review(self):
        entry = mo.classify(self.s08_result())
        self.assertEqual(entry['kind'], 's08_experiment_result')
        self.assertEqual(entry['bucket'], 'manual_review')

    def test_real_plan_manual_review(self):
        entry = mo.classify(self.s08_plan())
        self.assertEqual(entry['kind'], 's08_plan')
        self.assertEqual(entry['bucket'], 'manual_review')

    def test_package_organic_mismatch_rejected(self):
        package = self.s08_package()
        package['organic'] = 'observed'
        entry = mo.classify(package)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('organic_mismatch', entry['reason_codes'])

    def test_package_authorization_mismatch_rejected(self):
        package = self.s08_package()
        package['authorization'] = 'auto'
        entry = mo.classify(package)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('authorization_mismatch', entry['reason_codes'])

    def test_package_live_claim_rejected(self):
        package = self.s08_package()
        package['counts_for_live'] = True
        entry = mo.classify(package)
        self.assertEqual(entry['bucket'], 'rejected')

    def test_package_trajectory_impersonation_rejected(self):
        package = self.s08_package()
        package['trajectory'] = tr.V2_LIVE
        entry = mo.classify(package)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('trajectory_mismatch', entry['reason_codes'])

    def test_package_provenance_production_ledger_rejected(self):
        package = self.s08_package()
        package['provenance']['production_ledger'] = True
        entry = mo.classify(package)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('provenance_mismatch', entry['reason_codes'])

    def test_package_arm_live_claim_rejected(self):
        package = self.s08_package()
        package['arms']['recall']['counts_for_live'] = True
        entry = mo.classify(package)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('arm_caliber_mismatch', entry['reason_codes'])

    def test_plan_recall_event_as_message_rejected(self):
        plan = self.s08_plan()
        plan['recall_events'][0]['kind'] = 'message'
        entry = mo.classify(plan)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('recall_event_not_retrieval', entry['reason_codes'])

    def test_plan_recall_layer_mismatch_rejected(self):
        plan = self.s08_plan()
        plan['recall_events'][0]['payload']['recall_layer'] = 'experienced'
        entry = mo.classify(plan)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('recall_layer_mismatch', entry['reason_codes'])

    def test_plan_expression_control_rejected(self):
        plan = self.s08_plan()
        plan['expression_control'] = True
        entry = mo.classify(plan)
        self.assertEqual(entry['bucket'], 'rejected')

    def test_plan_legacy_source_rejected(self):
        plan = self.s08_plan()
        plan['recall_events'][0]['source'] = 'legacy_bridge'
        entry = mo.classify(plan)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('recall_source_legacy', entry['reason_codes'])

    def test_result_shadow_check_failed_rejected(self):
        result = self.s08_result()
        result['checks']['trajectory_is_shadow'] = False
        entry = mo.classify(result)
        self.assertEqual(entry['bucket'], 'rejected')
        self.assertIn('shadow_check_failed', entry['reason_codes'])


# ── 跨模式污染分拣 ───────────────────────────────────────────────────


class SortTests(Base):
    def mixed_batch(self):
        bad_candidate = self.candidate_run()
        bad_candidate['counts_for_live'] = True
        return [
            sh.status(now=time.time()),                 # live
            self.candidate_run(),                       # observation_only
            self.r07_packet(),                          # manual_review
            bad_candidate,                              # rejected
            {'trajectory': 'v3_imaginary'},             # rejected
            self.base_run('shadow-v1'),                 # observation_only
            {'trajectory': tr.CANDIDATE_COUNTERFACTUAL,
             'counts_for_live': True},                  # observation_only (override)
            self.s08_package(),                         # manual_review
            {'mode': 'no_trajectory'},                  # rejected
        ]

    def test_four_buckets_partition(self):
        batch = self.mixed_batch()
        out = mo.sort_observations(batch)
        self.assertEqual(set(out) - {'counts'}, set(mo.BUCKETS))
        total = sum(len(out[b]) for b in mo.BUCKETS)
        self.assertEqual(total, len(batch))
        self.assertEqual(out['counts']['live'], 1)
        self.assertEqual(out['counts']['observation_only'], 3)
        self.assertEqual(out['counts']['manual_review'], 2)
        self.assertEqual(out['counts']['rejected'], 3)

    def test_indices_preserve_input_order(self):
        out = mo.sort_observations(self.mixed_batch())
        seen = sorted(e['index'] for b in mo.BUCKETS for e in out[b])
        self.assertEqual(seen, list(range(9)))
        self.assertEqual([e['index'] for e in out['live']], [0])

    def test_deterministic(self):
        batch = self.mixed_batch()
        first = mo.sort_observations(batch)
        second = mo.sort_observations(copy.deepcopy(batch))
        self.assertEqual(first, second)

    def test_inputs_not_mutated(self):
        batch = self.mixed_batch()
        before = copy.deepcopy(batch)
        mo.sort_observations(batch)
        self.assertEqual(batch, before)

    def test_empty_input(self):
        out = mo.sort_observations([])
        for bucket in mo.BUCKETS:
            self.assertEqual(out[bucket], [])
            self.assertEqual(out['counts'][bucket], 0)

    def test_none_input(self):
        out = mo.sort_observations(None)
        self.assertEqual(sum(out['counts'].values()), 0)

    def test_all_reason_codes_are_registered(self):
        out = mo.sort_observations(self.mixed_batch())
        for bucket in mo.BUCKETS:
            for entry in out[bucket]:
                for code in entry['reason_codes']:
                    self.assertIn(code, mo.REASON_CODES)

    def test_unknown_never_passes(self):
        out = mo.sort_observations([{'trajectory': 'v3_imaginary'},
                                    {'no': 'trajectory'}, None])
        self.assertEqual(out['counts']['rejected'], 3)
        self.assertEqual(out['counts']['live'], 0)
        self.assertEqual(out['counts']['observation_only'], 0)

    def test_rejected_entries_carry_no_ok_code(self):
        out = mo.sort_observations(self.mixed_batch())
        ok = {'live_contract_ok', 'observation_contract_ok', 'manual_review_ok'}
        for entry in out['rejected']:
            self.assertFalse(ok & set(entry['reason_codes']))

    def test_counts_match_lengths(self):
        out = mo.sort_observations(self.mixed_batch())
        for bucket in mo.BUCKETS:
            self.assertEqual(out['counts'][bucket], len(out[bucket]))


# ── AST 静态锁：旁路契约 ─────────────────────────────────────────────


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, 'mode_observation.py'), encoding='utf-8') as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def _imported_names(self):
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split('.')[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or '').split('.')[0])
        return names

    def test_import_surface_is_copy_plus_trajectories(self):
        self.assertEqual(self._imported_names(), {'copy', 'emotion_trajectories'})

    def test_no_writer_or_remote_or_bridge_imports(self):
        banned = {'emotion_shadow', 'self_model', 'valence_candidates',
                  'qq_openclaw_bridge', 'llm_router', 'cognition_writer',
                  'paramiko', 'ops_remote', 'sync_to_cloud', '_deploy_qingyi_release',
                  'candidate_replay', 'emotion_revision', 'ripple_ablation',
                  'recall_emotion'}
        self.assertEqual(set(), self._imported_names() & banned)

    def test_no_network_or_storage_imports(self):
        banned = {'requests', 'urllib', 'socket', 'http', 'sqlite3', 'os',
                  'pathlib', 'subprocess', 'asyncio', 'time', 'random'}
        self.assertEqual(set(), self._imported_names() & banned)

    def test_no_wall_clock_calls(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr,
                                 ('time', 'localtime', 'mktime', 'monotonic',
                                  'perf_counter', 'strftime', 'sleep'),
                                 '契约里出现墙上时钟调用')

    def test_no_write_or_file_calls(self):
        forbidden = {'commit', 'executescript', 'executemany', 'execute', 'rollback',
                     'mkdir', 'makedirs', 'remove', 'unlink', 'rmtree', 'rename',
                     'write_text', 'write_bytes', 'tick', 'record_event', 'enqueue',
                     'drain', 'apply_event', 'connect', 'getenv'}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden,
                                 '契约里出现写操作 %s' % node.func.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual('open', node.func.id, '契约不直接开文件')

    def test_no_write_sql_anywhere(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ('INSERT ', 'UPDATE ', 'DELETE ', 'CREATE ', 'DROP ',
                             'ALTER ', 'REPLACE '):
                    self.assertNotIn(verb, upper)

    def test_no_path_frozen_at_import_time(self):
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ('getenv', 'join', 'abspath'))

    def test_no_second_dynamics(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotEqual(node.func.attr, 'exp',
                                    '不许自带第二套恢复动力学')

    def test_reuses_contract_stamp(self):
        called = set()
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)):
                called.add((node.func.value.id, node.func.attr))
        self.assertIn(('trajectories', 'stamp'), called)

    def test_expression_control_literals_are_false(self):
        values = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.keyword) and node.arg == 'expression_control':
                values.append(node.value)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == 'expression_control':
                        values.append(value)
        for value in values:
            self.assertTrue(isinstance(value, ast.Constant) and value.value is False,
                            'expression_control 字面量必须是 False')

    def test_retired_flag_not_revived(self):
        self.assertNotIn('counts_as_live_observation', self.source)

    def test_reason_codes_are_stable_and_unique(self):
        self.assertEqual(len(mo.REASON_CODES), len(set(mo.REASON_CODES)))
        self.assertEqual(list(mo.REASON_CODES), sorted(mo.REASON_CODES))

    def test_limitations_declare_boundaries(self):
        joined = ' '.join(mo.LIMITATIONS)
        for token in ('不接管表达', '第二套动力学', 'manual_review', 'not_observed',
                      '不读生产账本', '缺字段不补造'):
            self.assertIn(token, joined)

    def test_docstring_declares_pure_contract(self):
        doc = ast.get_docstring(self.tree)
        for token in ('纯契约', '不接真实生产消费者', '不调 LLM', 'emotion_trajectories'):
            self.assertIn(token, doc)


if __name__ == '__main__':
    unittest.main()