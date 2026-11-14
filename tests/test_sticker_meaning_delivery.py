import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

BOT = Path(__file__).resolve().parents[1] / 'bot'
sys.path.insert(0, str(BOT))

# 测试夹具自declare：桥的 MASTER_QQ_IDS 在 import 时求值，
# 必须在 import 前把主人号声明好（生产默认值已改为空，见脱敏基线）。
os.environ.setdefault("MASTER_QQ_IDS", "1000000001")

import qq_openclaw_bridge as b
import sticker_store as sk
import vision_notes as vn


class StickerMeaningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='stk_meaning_')
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name
        environment = patch.dict(os.environ, {'DATA_DIR': self.tmp, 'PERSONA_DATA_DIR': self.tmp})
        environment.start()
        self.addCleanup(environment.stop)
        sk._invalidate_cache() if hasattr(sk, '_invalidate_cache') else None
        index = patch.object(sk, '_load_index', new=lambda ttl=3.0: {
            'sid1': {'id': 'sid1', 'src': 'https://example.test/cat.jpg',
                     'meaning': '吃瓜，路过看戏，与我无关', 'meaning_src': 'human'},
        })
        index.start()
        self.addCleanup(index.stop)
        b._IMAGE_REPLY_LAST.clear()

    def test_a_descriptive_note_is_not_treated_as_meaning(self):
        note = {'kind': '表情包', 'intent': '小葱吃西瓜还带角', 'mood': '',
                'objects': ['小葱', '西瓜'], 'scene': '表情包'}
        card = vn.meme_card(note)
        self.assertNotIn('小葱', card)
        self.assertTrue(card, '至少要给一条「别描述画面」的交代')
        self.assertIn('没把握', card)

    def test_a_short_meaning_label_from_the_library_is_usable(self):
        self.assertTrue(vn.meaning_like('装可怜'))
        self.assertTrue(vn.meaning_like('吃瓜'))
        self.assertFalse(vn.meaning_like('小葱吃西瓜还带角'))

    def test_calibrated_meaning_is_found_by_its_url(self):
        got = sk.meaning_for_src('https://example.test/cat.jpg')
        self.assertEqual(got.get('meaning'), '吃瓜，路过看戏，与我无关')

    async def test_reply_uses_calibrated_meaning_not_the_visual_description(self):
        note = {'kind': '表情包', 'intent': '小葱吃西瓜还带角', 'mood': '很热',
                'objects': ['小葱', '西瓜', '角'], 'scene': '表情包'}
        with patch.object(vn, 'note_for', return_value=note), \
                patch.object(b, '_look_now', new=AsyncMock(return_value=note)):
            urls, card = await b._image_reply_material('[图片]', ['https://example.test/cat.jpg'],
                                                       scene='group', session='group:1')
        self.assertEqual(urls, [])
        self.assertIn('吃瓜', card)
        self.assertNotIn('小葱', card)

    async def test_meaning_card_forbids_repeating_the_explanation(self):
        note = {'kind': '表情包', 'intent': '想被哄', 'mood': '撒娇',
                'objects': [], 'scene': '表情包'}
        with patch.object(vn, 'note_for', return_value=note), \
                patch.object(b, '_look_now', new=AsyncMock(return_value=note)):
            _urls, card = await b._image_reply_material('[图片]', ['https://example.test/other.jpg'],
                                                        scene='group', session='group:1')
        self.assertIn('不要照念', card)
        self.assertIn('不要描述画面', card)

    async def test_same_sticker_reminds_her_what_she_said_last_time(self):
        note = {'kind': '表情包', 'intent': '想被哄', 'mood': '撒娇',
                'objects': [], 'scene': '表情包'}
        b._IMAGE_REPLY_LAST[('group:1', vn.key_of('https://example.test/other.jpg'))] = '又撒娇，我才不吃这套。'
        with patch.object(vn, 'note_for', return_value=note), \
                patch.object(b, '_look_now', new=AsyncMock(return_value=note)):
            _urls, card = await b._image_reply_material('[图片]', ['https://example.test/other.jpg'],
                                                        scene='group', session='group:1')
        self.assertIn('上次', card)
        self.assertIn('别重复', card)


class EmojiAndCallNameTests(unittest.TestCase):
    def test_qq_face_bracket_codes_are_removed_from_her_text(self):
        self.assertEqual(b.strip_emoji('哼，我才没[可爱]呢。'), '哼，我才没呢。')
        self.assertEqual(b.strip_emoji('真的[呲牙]了'), '真的了')

    def test_non_face_brackets_are_kept(self):
        self.assertEqual(b.strip_emoji('他说[书名]很好看'), '他说[书名]很好看')

    def test_venting_is_not_an_owed_task_or_a_reason_to_search(self):
        for text in ('为什么你们都单抽六星', '怎么又是我加班，烦死了',
                     '每次都这样，真的服了', '你们都在晒，好离谱'):
            self.assertTrue(b._is_venting(text), text)
            self.assertFalse(b.is_owed_request(text), text)
            self.assertFalse(b._should_web_search(text), text)

    def test_a_real_request_still_passes(self):
        self.assertFalse(b._is_venting('帮我查下单抽的概率'))
        self.assertTrue(b.is_owed_request('帮我查下单抽的概率'))

    def test_delivery_calls_use_the_fixed_name_not_the_group_card(self):
        with patch.object(b, 'member_memory_get_for_test', create=True):
            pass
        self.assertEqual(b._call_name('g1', '123', '阿渔', getter=lambda g, u: {'call': '阿丁'}), '阿丁')
        self.assertEqual(b._call_name('g1', '1000000001', '阿舟',
                                      getter=lambda g, u: {'call': '阿舟'}), '主人')
        self.assertEqual(b._call_name('g1', '999', '路人甲', getter=lambda g, u: {}), '路人甲')

    def test_sticker_reply_memory_is_recorded_per_image(self):
        b._IMAGE_REPLY_LAST.clear()
        b._record_image_reply('group:9', ['https://example.test/a.jpg'], '接住了')
        self.assertEqual(b._IMAGE_REPLY_LAST[('group:9', vn.key_of('https://example.test/a.jpg'))], '接住了')


if __name__ == '__main__':
    unittest.main()
