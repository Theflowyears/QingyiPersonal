import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import qq_openclaw_bridge as b
import ceylan_control_runtime as ctl
import vision_notes as vn


class InstalledVisionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_image_note_is_not_cut_by_chat_control(self):
        with tempfile.TemporaryDirectory() as root, patch.dict('os.environ',{'DATA_DIR':root}):
            vn._reset_cache()
            expected={'objects':['小人','加载圈'],'scene':'表情包','mood':'困惑','meme_guess':'在等一个答案'}
            async def provider(messages,**kwargs):
                self.assertGreaterEqual(kwargs['max_tokens'],700)
                return json.dumps(expected,ensure_ascii=False)
            original=b.call_mimo_vision
            async def stub(*args,**kwargs):return None
            ns={name:stub for name in ['handle_group_message','handle_private_message',
                'periodic_group_review','compact_group_memory_once','run_agent_task',
                'send_group_msg','send_private_msg','call_openclaw']}
            ns.update(call_mimo_vision=provider,build_group_recent_text=lambda:('',0,0,0,[]))
            rt=type('RT',(),{'snapshot':lambda self:None})()
            with patch.object(ctl,'ROOT',Path(root)/'control'),patch.dict(ctl.OPTIONS,{'reply_tokens':80,'reply_chars':30,'bubbles':2}):
                ctl.install(ns,rt)
                token=ctl.SCOPE.set('chat')
                try:
                    with patch.object(b,'call_mimo_vision',ns['call_mimo_vision']), \
                         patch.object(b,'image_url_to_openai_ref',new=AsyncMock(return_value='data:image/png;base64,eA==')):
                        await b._describe_image('','',image_ref='test.png',session='group:test')
                finally:ctl.SCOPE.reset(token)
            note=vn.note_for('test.png',scene='group',session='group:test')
            self.assertEqual(note['mood'],'困惑')
            self.assertEqual(note['meme_guess'],'在等一个答案')
            vn._reset_cache()


class UnwrappedSignatureTests(unittest.TestCase):
    """没装控制层包装时，`internal=True` 也必须收得下。

    服务器端到端实测（一次性别 DATA_DIR 直接跑 `_describe_image`）：
    `TypeError: call_mimo_vision() got an unexpected keyword argument 'internal'`
    —— 那张图根本没被看过，而且炸在一个「看起来一切正常」的进程里。
    包装器负责消费这个标记，底层函数负责忽略它；谁缺席都不该把看图链路打断。
    """

    def test_vision_accepts_internal_flag(self):
        self.assertIn("internal", inspect.signature(b.call_mimo_vision).parameters)

    def test_text_model_accepts_internal_flag(self):
        self.assertIn("internal", inspect.signature(b.call_openclaw).parameters)


if __name__=='__main__':unittest.main()
