"""Bounded concurrency and block-list safeguards for the existing bridge."""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from functools import wraps
import inspect
import json
import os
from pathlib import Path
import tempfile
import time
from project_config import BLOCKED_GROUP_IDS

BLOCKED = set(BLOCKED_GROUP_IDS)
NAMESPACE = {}
MODEL_LIMIT = asyncio.Semaphore(max(1,min(8,int(os.getenv('CEYLAN_MODEL_CONCURRENCY','2')))))
MAX_PENDING = max(8,min(512,int(os.getenv('CEYLAN_MAX_PENDING','128'))))
EXTRA_TASKS = set()
LOCKS = {}
SEEN = OrderedDict()
LAST_MESSAGE = None
LAST_PATROL = None
DATA_DIR = Path(os.getenv('DATA_DIR', './data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT = Path(os.getenv('CEYLAN_BOT_SNAPSHOT', str(DATA_DIR / 'runtime-status.json')))


def blocked(group):
    return str(group).strip() in BLOCKED


def purge():
    for key in ('group_message_cache','group_image_cache','group_message_seq','group_last_periodic_seq','periodic_answer_fingerprints','group_memory_summary','group_memory_last_seq','group_memory_last_update_ts'):
        mapping=NAMESPACE.get(key,{})
        for gid in list(mapping):
            if blocked(gid):mapping.pop(gid,None)
    histories=NAMESPACE.get('histories',{})
    for key in list(histories):
        if any(str(key).startswith('group-'+gid+'-') for gid in BLOCKED):histories.pop(key,None)


def guard(default=None):
    def decorate(fn):
        sig=inspect.signature(fn)
        def denied(args,kwargs):
            bound=sig.bind(*args,**kwargs).arguments
            group=bound.get('group_id')
            event=bound.get('event')
            if isinstance(event,dict):group=event.get('group_id',group)
            return group is not None and blocked(group)
        if inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def wrapper(*args,**kwargs):
                if denied(args,kwargs):return default
                return await fn(*args,**kwargs)
        else:
            @wraps(fn)
            def wrapper(*args,**kwargs):
                if denied(args,kwargs):return default
                return fn(*args,**kwargs)
        return wrapper
    return decorate


def state_io(fn):
    @wraps(fn)
    def wrapper(*args,**kwargs):
        purge()
        result=fn(*args,**kwargs)
        purge()
        return result
    return wrapper


def limit_model(fn):
    @wraps(fn)
    async def wrapper(*args,**kwargs):
        async with MODEL_LIMIT:
            return await fn(*args,**kwargs)
    return wrapper


@asynccontextmanager
async def conversation_lock(key):
    entry=LOCKS.setdefault(key,[asyncio.Lock(),0])
    entry[1]+=1
    try:
        async with entry[0]:yield
    finally:
        entry[1]-=1
        if entry[1]==0:LOCKS.pop(key,None)


def serial_history(fn):
    @wraps(fn)
    async def wrapper(user_key,*args,**kwargs):
        async with conversation_lock(user_key):
            return await fn(user_key,*args,**kwargs)
    return wrapper


def spawn_task(coro):
    if len(EXTRA_TASKS)>=32:
        coro.close()
        return None
    task=asyncio.create_task(coro)
    EXTRA_TASKS.add(task)
    def done(t):
        EXTRA_TASKS.discard(t)
        if not t.cancelled():
            e=t.exception()
            if e:print('[后台任务失败] '+type(e).__name__,flush=True)
    task.add_done_callback(done)
    return task


def accepted(event):
    if not isinstance(event,dict) or event.get('post_type')!='message':return False
    if event.get('message_type') not in ('group','private'):return False
    if blocked(event.get('group_id','')):return False
    if str(event.get('user_id',''))==str(event.get('self_id','')):return False
    mid=event.get('message_id')
    if mid is None:return True
    now=time.monotonic()
    while SEEN and (len(SEEN)>4096 or next(iter(SEEN.values()))<now-600):SEEN.popitem(last=False)
    key=(str(event.get('self_id')),event.get('message_type'),str(event.get('group_id')),str(event.get('user_id')),str(mid))
    if key in SEEN:return False
    SEEN[key]=now
    return True


def snapshot():
    purge()
    memory=NAMESPACE.get('group_memory_summary',{})
    data={'memory':'enabled','memory_count':sum(bool(v) for v in memory.values()),'patrol':'enabled',
          'last_message':LAST_MESSAGE,'last_patrol':LAST_PATROL,
          'snapshot_at':time.time(),'blocklist_enabled':bool(BLOCKED),
          'blocked_group_count':len(BLOCKED)}
    temp=None
    try:
        with tempfile.NamedTemporaryFile('w',dir=SNAPSHOT.parent,prefix='.runtime-',delete=False,encoding='utf-8') as f:
            temp=f.name
            json.dump(data,f,ensure_ascii=False)
        os.replace(temp,SNAPSHOT)
    except OSError as e:
        print('[状态快照失败] '+type(e).__name__,flush=True)
    finally:
        if temp and os.path.exists(temp):os.unlink(temp)


# Web 控制台直接读取的本地状态文件（与 website/app.py 的 BOT_STATUS_FILE 对齐）
WEB_STATUS_FILE = Path(os.getenv('CEYLAN_BOT_STATUS_FILE', str(DATA_DIR / 'bot_status.json')))


def write_web_status():
    """把机器人运行态写入 web 可读的 bot_status.json，让 /admin/bot 显示“已连接”。

    同机部署时 web 直接读这个共享文件即可判活，无需 HTTP 心跳回传。
    """
    from datetime import datetime, timezone
    memory = NAMESPACE.get('group_memory_summary', {})
    try:
        groups = len(NAMESPACE.get('group_message_cache', {}))
    except Exception:
        groups = 0
    data = {
        'received_at': datetime.now(timezone.utc).isoformat(),
        'online': bool(_active_online_flag()),
        'bridge': 'online' if _active_online_flag() else 'unknown',
        'napcat': 'online' if _active_online_flag() else 'unknown',
        'model': os.getenv('LLM_TEXT_MODEL', '') or None,
        'memory': 'enabled',
        'memory_count': sum(bool(v) for v in memory.values()),
        'patrol': 'enabled',
        'last_message': LAST_MESSAGE,
        'last_patrol': LAST_PATROL,
        'groups': groups,
    }
    temp = None
    try:
        WEB_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', dir=str(WEB_STATUS_FILE.parent), prefix='.bs-', delete=False, encoding='utf-8') as f:
            temp = f.name
            json.dump(data, f, ensure_ascii=False)
        os.replace(temp, WEB_STATUS_FILE)
    except OSError as e:
        print('[web状态写入失败] ' + type(e).__name__, flush=True)
    finally:
        if temp and os.path.exists(temp):
            try:
                os.unlink(temp)
            except OSError:
                pass


def _active_online_flag():
    """当前 OneBot 是否连着：用 napcat_channel.channel.connected。"""
    try:
        import napcat_channel
        return bool(napcat_channel.channel.connected)
    except Exception:
        return False


_VALENCE_DRAIN = {"running": False}
_VALENCE_TASKS = set()


async def drain_valence_candidates():
    """把新登记的效价候选交给小模型标注；一次两条，失败留待下次。"""
    if _VALENCE_DRAIN["running"]:
        return
    _VALENCE_DRAIN["running"] = True
    try:
        import valence_candidates
        r = await valence_candidates.drain(limit=2)
        if r.get("judged") or r.get("errors"):
            print("[效价候选] 标注 %d 条，失败 %d，待 %d"
                  % (r.get("judged", 0), r.get("errors", 0), r.get("pending", 0)),
                  flush=True)
    except Exception as exc:
        print("[效价候选] 后台标注失败 " + type(exc).__name__ + ": "
              + str(exc)[:80], flush=True)
    finally:
        _VALENCE_DRAIN["running"] = False


async def poll_emotion_shadow():
    import emotion_shadow
    result = await asyncio.to_thread(emotion_shadow.tick)
    # 候选账本与影子状态完全分开：这里只标注、只记账，不推进效价。
    _vt = asyncio.create_task(drain_valence_candidates())
    _VALENCE_TASKS.add(_vt)
    _vt.add_done_callback(_VALENCE_TASKS.discard)
    last = getattr(poll_emotion_shadow, '_last_log', 0.0)
    now = time.time()
    if result.get('batch') or now - last >= 600 or not last:
        print('[情感影子] mode=shadow processed=%s cursor=%s %s' % (
            result.get('processed', 0), result.get('cursor', 0),
            result.get('reason') or '不接管表达与行动'), flush=True)
        poll_emotion_shadow._last_log = now
    return result


async def periodic_snapshot():
    import sys
    while True:
        # 通过模块属性调用，确保拿到被 control 层包装后的 snapshot（含 poll→inbox/state.json）
        _mod = sys.modules[__name__]
        try:
            _mod.snapshot()
        except Exception as e:
            print('[快照循环失败] ' + str(e), flush=True)
        try:
            await poll_emotion_shadow()
        except Exception as e:
            print('[情感影子] 本轮失败，保留游标与状态 ' + type(e).__name__ + ': ' + str(e)[:100],
                  flush=True)
        try:
            import sticker_store
            await sticker_store.get_qq_native_faces()
        except Exception:
            pass
        try:
            _ns = globals().get('NAMESPACE') or _mod.__dict__.get('NAMESPACE') or {}
            _pmr = _ns.get('process_memory_requests') if isinstance(_ns, dict) else None
            if _pmr:
                await _pmr()
        except Exception as e:
            print('[记忆请求循环失败] ' + str(e)[:80], flush=True)
        try:
            write_web_status()
        except Exception as e:
            print('[web状态循环失败] ' + str(e), flush=True)
        await asyncio.sleep(20)


def mark_patrol():
    global LAST_PATROL
    LAST_PATROL=time.strftime('%Y-%m-%dT%H:%M:%S%z')


async def run_bridge(ns):
    global LAST_MESSAGE, NAMESPACE
    NAMESPACE=ns
    from ceylan_keys_runtime import install as install_keys_v5
    install_keys_v5(ns)
    from ceylan_control_runtime import install as install_control
    install_control(ns, __import__(__name__))
    from ceylan_observer import install as install_observer
    install_observer(ns, __import__(__name__))
    from ceylan_fun_runtime import install as install_fun_v6
    install_fun_v6(ns, __import__(__name__))
    ns['load_periodic_state']()
    ns['load_group_memory_state']()
    purge()
    # Deployment backs up both state files before this filtered persistence.
    ns['save_periodic_state']()
    ns['save_group_memory_state']()

    # 状态快照循环独立启动：即使 NapCat 连不上（未登录 QQ），
    # Web 面板也要能读到在线状态、参数、消息统计，不至于全是空。
    import sys as _sys
    _mod = _sys.modules[__name__]
    asyncio.create_task(periodic_snapshot())
    print('[快照循环] 已启动（独立于 NapCat 连接）', flush=True)

    while True:
        tasks=set()
        pending=set()
        try:
            print('连接 NapCat OneBot: '+ns['ONEBOT_WS'],flush=True)
            async with ns['websockets'].connect(ns['ONEBOT_WS'],ping_interval=20,ping_timeout=20,max_size=4*1024*1024) as ws:
                print('已连接 NapCat OneBot',flush=True)
                # 绑定主动接口通道（get_image/收藏/撤回等需要登录态的操作走这里）
                try:
                    import napcat_channel
                    napcat_channel.channel.bind(ws, ns.get('send_lock'))
                except Exception as _e:
                    print('[napcat_channel 绑定失败] '+str(_e),flush=True)
                # periodic_snapshot 已在连接前独立启动，这里不再重复创建
                tasks={asyncio.create_task(ns['periodic_group_review'](ws)),
                       asyncio.create_task(ns['periodic_group_memory_compaction']()),
                       asyncio.create_task(ns['periodic_memory_usage_rollup']()),
                       asyncio.create_task(ns['periodic_memory_archive']()),
                       asyncio.create_task(ns['idle_topic_worker'](ws)),
                       asyncio.create_task(ns['periodic_persona_growth']()),
                       asyncio.create_task(ns['periodic_group_liveness']()),
                       asyncio.create_task(ns['interest_worker']()),
                       asyncio.create_task(ns['whisper_worker']()),
                       asyncio.create_task(ns['wish_seed_worker']()),
                       asyncio.create_task(ns['wish_archive_worker']()),
                       asyncio.create_task(ns['chat_corpus_worker']()),
                       asyncio.create_task(ns['whisper_memory_worker']()),
                       asyncio.create_task(ns['whisper_photo_worker']()),
                       asyncio.create_task(ns['bili_worker']()),
                       asyncio.create_task(ns['curiosity_worker'](ws)),
                       asyncio.create_task(ns['friend_dm_worker'](ws)),
                       asyncio.create_task(ns['signature_worker'](ws)),
                       asyncio.create_task(ns['qq_face_sync_worker']()),
                       asyncio.create_task(ns['ops_outbox_worker'](ws))}
                async def handle(event):
                    try:
                        await ns['handle_event'](ws,event)
                    except asyncio.CancelledError:raise
                    except Exception as e:print('[消息处理失败] '+type(e).__name__,flush=True)
                async for raw in ws:
                    try:event=json.loads(raw)
                    except (ValueError,TypeError):continue
                    # API 主动调用的回包（带 echo）先交给通道，不当普通事件
                    try:
                        import napcat_channel
                        if napcat_channel.channel.feed(event):
                            continue
                    except Exception:
                        pass
                    # 其余 API 回包（没有 post_type、只带 echo）交给 bridge 的等待器
                    if 'echo' in event and 'post_type' not in event:
                        try:
                            ns['_handle_api_reply'](event)
                        except Exception:
                            pass
                        continue
                    if len(pending)>=MAX_PENDING:
                        # Bound memory and model queue; do not cache or echo dropped content.
                        print('[消息队列已满] 丢弃新事件',flush=True)
                        continue
                    if not accepted(event):continue
                    LAST_MESSAGE=time.strftime('%Y-%m-%dT%H:%M:%S%z')
                    # Make late-arriving images visible while a prior question waits for them.
                    if not ns['is_ignored_sender'](event):
                        _pm = ns['parse_message'](event)
                        images = _pm[2] if len(_pm) > 2 else []
                        if images and event.get('message_type')=='group':
                            ns['remember_group_images'](event['group_id'],event['user_id'],images)
                    task=asyncio.create_task(handle(event))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
        except asyncio.CancelledError:raise
        except Exception as e:
            import traceback
            print('[连接断开] ' + type(e).__name__ + ': ' + str(e)[:300], flush=True)
            traceback.print_exc()
        finally:
            try:
                import napcat_channel
                napcat_channel.channel.unbind()
            except Exception:
                pass
            all_tasks=(tasks|pending|set(EXTRA_TASKS)|set(ns.get('_BG_TASKS',set()))|
                       set(ns.get('_VISION_INFLIGHT',{}).values()))
            for task in all_tasks:task.cancel()
            if all_tasks:await asyncio.gather(*all_tasks,return_exceptions=True)
            ns['save_periodic_state']()
            ns['save_group_memory_state']()
        await asyncio.sleep(5)
