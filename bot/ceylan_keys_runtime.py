"""Hot-load vision key at the next model call without restarting Bridge."""
from functools import wraps
import json
from pathlib import Path
import os
import tempfile
import time
from ceylan_keys_common import digest

ROOT = Path(os.getenv('CEYLAN_KEYS_DIR', './data/keys-v5'))


def install(ns):
    if ns.get('_keys_v5'):
        return
    ns['_keys_v5'] = True
    original_key = ns.get('MIMO_VISION_API_KEY', '')
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    baseline = digest({'MIMO_VISION_API_KEY': original_key,
                       'MIMO_VISION_BASE_URL': ns.get('MIMO_VISION_BASE_URL', ''),
                       'MIMO_VISION_MODEL': ns.get('MIMO_VISION_MODEL', 'mimo-v2.5'),
                       'USE_DIRECT_MIMO_VISION': '1' if ns.get('USE_DIRECT_MIMO_VISION', True) else '0'})
    fd, tmp = tempfile.mkstemp(dir=ROOT)
    try:
        with os.fdopen(fd, 'w') as stream: json.dump({'baseline': baseline, 'started': time.time()}, stream)
        os.replace(tmp, ROOT / 'vision-runtime.json')
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    original = ns['call_mimo_vision']
    @wraps(original)
    async def vision(*args, **kwargs):
        path = ROOT / 'vision.json'
        config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        ns['MIMO_VISION_API_KEY'] = config.get('key', original_key)
        ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=ROOT)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'id': config.get('id'), 'loaded_at': time.time(), 'source': '网站覆盖值' if config else 'model.env'}, stream)
            os.replace(tmp, ROOT / 'vision-loaded.json')
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        try:
            return await original(*args, **kwargs)
        except Exception as exc:
            # 保留原始错误类型和关键信息，方便定位（不再一律吞成一句话）；
            # 但截断 body，避免把带凭据的上游响应打进日志。
            detail = str(exc)[:200].replace('\n', ' ')
            raise RuntimeError('视觉请求失败: ' + type(exc).__name__ + ': ' + detail) from None
    ns['call_mimo_vision'] = vision
