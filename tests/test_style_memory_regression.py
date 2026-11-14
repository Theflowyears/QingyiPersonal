"""文风与整理窗口回归；运行器须在导入前隔离 DATA_DIR。"""
import ast
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import qq_openclaw_bridge as bridge
import persona_growth as growth


class MemoryPromptBoundaryTests(unittest.TestCase):
    def test_private_and_group_prompts_require_evidence(self):
        source = Path(bridge.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        for name in ('compact_private_memory', 'compact_group_memory_once'):
            function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
            assignment = next(node for node in function.body if isinstance(node, ast.Assign)
                              and any(isinstance(target, ast.Name) and target.id == 'prompt' for target in node.targets))
            prompt = ''.join(node.value for node in ast.walk(assignment.value)
                             if isinstance(node, ast.Constant) and isinstance(node.value, str))
            with self.subTest(function=name):
                for forbidden in ('宁可多记不要漏', '宁多勿漏'):
                    self.assertNotIn(forbidden, prompt)
                for boundary in ('证据优先', '用户自述', '说话人', '助手的猜测、计划、承诺、自称完成',
                                 '既成事实或工具成功', '引用、转述、玩笑、角色扮演',
                                 '本人事实', '不同成员不可合并', '助手高频口癖',
                                 '用户偏好或强制表达指令', '不确定不写'):
                    self.assertIn(boundary, prompt)


class OpeningTests(unittest.TestCase):
    def test_longest_lead(self):
        self.assertEqual(bridge._open_lead('哼哼，知道啦'), '哼哼')
        self.assertEqual(bridge._open_lead('哎呀，别急'), '哎呀')

    def test_different_suffix_same_particle_and_session_isolation(self):
        with patch.dict(bridge.histories, {'private-user-a': [('assistant', '哼，给你留着'), ('assistant', '哼，才没有呢')], 'private-user-b': [('assistant', '知道啦')]}):
            self.assertIn('最近', bridge._open_hint(session_key='private-user-a'))
            self.assertIn('「哼」', bridge._open_hint(session_key='private-user-a'))
            self.assertNotIn('「哼」', bridge._open_hint(session_key='private-user-b'))


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    def test_prompt_keeps_sources_and_entire_window(self):
        rows = [{'session': 'group:alpha', 'user': '甲' * 300, 'bot': '乙' * 300} for _ in range(10)]
        rows.append({'session': 'private-user-beta', 'user': '末行证据', 'bot': '只是猜测'})
        with patch('member_memory.all_rosters_text', return_value=''):
            prompt = growth._build_prompt({}, rows)
        for token in ('group:alpha', 'private-user-beta', '末行证据', '助手', '工具', '用户陈述'):
            self.assertIn(token, prompt)

    def test_old_voice_not_injected_as_instruction(self):
        with patch.object(growth, 'load', return_value={'voice': {'口癖': ['哼哼强制每句'], '句式': [], '语气': ''}, 'likes': ['听歌']}):
            block = growth.build_block()
        self.assertNotIn('哼哼强制每句', block)
        self.assertIn('背景', block)

    async def test_window_cursor_covers_only_rendered_rows(self):
        rows = [{'ts': i, 'session': 'group:alpha', 'user': str(i) + '甲' * 1000} for i in range(1, 9)]
        d = {'last_consolidated': 0}
        prompts = []
        async def llm(prompt):
            prompts.append(prompt)
            return '{"facts": []}'
        with patch.object(growth, 'load', return_value=d), patch.object(growth, 'read_new_journal', return_value=rows), patch.object(growth, 'save'), patch('member_memory.all_rosters_text', return_value=''):
            result = await growth.consolidate(llm)
        self.assertTrue(result['ok'])
        cursor = d['last_consolidated']
        self.assertLess(cursor, 8)
        self.assertEqual(result['count'], int(cursor))
        self.assertIn('"时间": ' + str(int(cursor)), prompts[0])
        self.assertNotIn('"时间": ' + str(int(cursor) + 1), prompts[0])

    async def test_empty_output_does_not_advance_cursor(self):
        d = {'last_consolidated': 0}
        async def llm(_):
            return ''
        with patch.object(growth, 'load', return_value=d), patch.object(growth, 'read_new_journal', return_value=[{'ts': i} for i in (1, 2, 3)]), patch.object(growth, 'save') as save:
            result = await growth.consolidate(llm)
        self.assertFalse(result['ok'])
        save.assert_not_called()
