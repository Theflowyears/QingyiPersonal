import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from contextlib import contextmanager
from flask import jsonify,request,session
from ceylan_control_core import validate

ROOT=Path(__file__).resolve().parent

# 本地文件桥：bot 每20秒把状态写入 data/control-v4/state.json，并把命令从 inbox/ 取走。
# 同机部署时用它替代 https 回传轮询，实现“一键启动即已连接 + 可编辑”。
BOT_STATE_DIR = Path(os.getenv('CEYLAN_CONTROL_DIR', './data/control-v4'))
BOT_INBOX = BOT_STATE_DIR / 'inbox'


def _read_local_state():
    p = BOT_STATE_DIR / 'state.json'
    if not p.exists():
        return None
    try:
        st = json.loads(p.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return None
    if isinstance(st, dict) and 'sampled' in st:
        return st
    return None


def _group_rows(groups, slim):
    """统一每群字段的裁剪口径，正常路径与本地文件桥回退必须一致。

    slim=True 只给长度（首屏只要「有几个群」）；slim=False 必须给 `memory` 原文
    与 `cache` 数组 —— 「记忆」页读的就是这两个字段。回退路径以前无条件把
    它们换成数字，前端 `cache.slice(...)` 直接抛 TypeError，整块面板变成空白，
    看起来就像「群长期记忆没了」。
    """
    rows = []
    for g in (groups or []):
        if not isinstance(g, dict):
            continue
        row = {'id': g.get('id'), 'name': g.get('name'), 'code': g.get('code'),
               'memory_updated': g.get('memory_updated') or g.get('memory_update'),
               'memory_revision': g.get('memory_revision'),
               'cache_total': g.get('cache_total'),
               'cache_revision': g.get('cache_revision'),
               'memory_complete': g.get('memory_complete'),
               'last_message': g.get('last_message'), 'busy': g.get('busy')}
        if slim:
            row['memory'] = len(g.get('memory') or '')
            row['cache'] = len(g.get('cache') or [])
        else:
            row['memory'] = g.get('memory') or ''
            row['cache'] = g.get('cache') or []
        rows.append(row)
    return rows


def _write_local_command(payload):
    BOT_INBOX.mkdir(parents=True, exist_ok=True)
    ident = secrets.token_hex(16)
    now = time.time()
    cmd = dict(payload)
    cmd['id'] = ident
    cmd['expires'] = now + 600
    tmp = BOT_INBOX / ('.tmp-' + ident)
    tmp.write_text(json.dumps(cmd, ensure_ascii=False), encoding='utf-8')
    tmp.replace(BOT_INBOX / (ident + '.json'))
    return ident


def _read_local_ledger():
    p = BOT_STATE_DIR / 'ledger.json'
    if not p.exists():
        return []
    try:
        led = json.loads(p.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return []
    ops = []
    for ident, rec in sorted(led.items(), key=lambda kv: kv[1].get('time', 0)):
        ops.append({'id': ident, 'created': rec.get('time', 0),
                    'status': rec.get('status', ''), 'result': rec.get('message', '')})
    # 升序排完再取前 30 条 = 最老的 30 条：生产 ledger 攒过 30 条那天起，
    # 刚保存的那条回执就永远不在这个列表里，面板看起来就成了「改了没反应」。
    return list(reversed(ops[-30:]))


def install(app,base):
    if app.extensions.get('bot_control_v4'):return
    app.extensions['bot_control_v4']=True
    dbpath=os.getenv('CEYLAN_CONTROL_DB','./data/bot_control.db')
    @contextmanager
    def db():
        c=sqlite3.connect(dbpath,timeout=5);c.row_factory=sqlite3.Row
        try:
            c.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, received REAL, payload TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, created REAL, expires REAL, payload TEXT, status TEXT, result TEXT)')
            yield c
            c.commit()
            # The SQLite file does not exist until the first connection commits.
            # Apply the private mode after commit so first boot is safe too.
            try: os.chmod(dbpath,0o600)
            except FileNotFoundError: pass
        except BaseException:c.rollback();raise
        finally:c.close()
    def expire(c):
        c.execute("UPDATE commands SET status='expired',result='操作超时，未确认执行' WHERE status='pending' AND expires<?",(time.time(),))
        c.execute('DELETE FROM commands WHERE created<?',(time.time()-7*86400,))
    def fail(msg,status):return jsonify(error=msg),status
    def csrf():
        return session.get('bot_control_csrf') and hmac.compare_digest(session['bot_control_csrf'].encode(),request.headers.get('X-CSRF-Token','').encode())
    def readbody(limit):
        if not request.is_json:raise ValueError('需要 JSON')
        raw=request.stream.read(limit+1)
        if len(raw)>limit:raise ValueError('内容过大')
        return json.loads(raw)
    @app.post('/api/bot/control/sync',endpoint='control_v4_sync')
    def sync():
        token=os.getenv('CEYLAN_BOT_TOKEN','')
        if len(token)<32 or not hmac.compare_digest(request.headers.get('Authorization','').encode(),('Bearer '+token).encode()):return fail('unauthorized',401)
        try:
            data=readbody(1100000);state=data.get('state')
            if state is not None:
                if not isinstance(state,dict) or type(state.get('sampled')) not in (int,float) or not isinstance(state.get('groups'),list):raise ValueError('快照格式错误')
                if not time.time()-86400<=state['sampled']<=time.time()+60:raise ValueError('快照时间错误')
                if len(state['groups'])>500:raise ValueError('群数量过多')
                # Fixed data schema, intentionally includes user-authorized cache and memory text.
                groups=[]
                for g in state['groups']:
                    gid=str(g.get('id',''))
                    if not gid.isdigit():continue
                    if len(g.get('memory',''))>20000 or len(g.get('cache',[]))>30:raise ValueError('内容过大')
                    groups.append({k:g[k] for k in ['id','memory','memory_complete','memory_revision','memory_updated','cache_total','cache_revision','busy'] if k in g}|{'cache':[{k:r[k] for k in ['seq','time','nickname','user_id','text','is_bot','image_count','revision'] if k in r} for r in g.get('cache',[])]})
                validate({'action':'settings','revision':state.get('settings_revision'),'values':state.get('settings')})
                clean={k:state[k] for k in ['sampled','settings','settings_revision','group_total'] if k in state};clean['groups']=groups
        except (ValueError,TypeError,AttributeError,KeyError,RecursionError):return fail('invalid control snapshot',400)
        with db() as c:
            c.execute('BEGIN IMMEDIATE');expire(c)
            if state is not None:
                c.execute('INSERT OR REPLACE INTO state VALUES(1,?,?)',(time.time(),json.dumps(clean,ensure_ascii=False)))
                for ack in state.get('acks',[])[:100]:
                    if ack.get('status') not in ('done','failed'):continue
                    result=str(ack.get('message',''))[:300]
                    if ack.get('backup'):result+='；备份：'+str(ack['backup'])[:200]
                    c.execute("UPDATE commands SET status=?,result=? WHERE id=? AND status IN ('pending','expired')",(ack['status'],result,ack.get('id')))
            pending=[json.loads(r['payload'])|{'id':r['id'],'expires':r['expires']} for r in c.execute("SELECT * FROM commands WHERE status='pending' ORDER BY created LIMIT 5")]
        return jsonify(ok=True,commands=pending)
    @app.get('/admin/bot/control',endpoint='control_v4_page')
    @base['admin_required']
    def control_page():
        session.setdefault('bot_control_csrf',secrets.token_urlsafe(32))
        return base['render_console']('bot',(ROOT/'bot-control.html').read_text(encoding='utf-8'),csrf=session['bot_control_csrf'])
    @app.get('/admin/api/bot/control',endpoint='control_v4_status')
    def status():
        if not session.get('ceylan_admin'):return fail('请重新登录',401)
        with db() as c:
            expire(c);r=c.execute('SELECT * FROM state WHERE id=1').fetchone()
            operations=[dict(x) for x in c.execute('SELECT id,created,status,result FROM commands ORDER BY created DESC LIMIT 30')]
        state=json.loads(r['payload']) if r else None
        # slim=1：首屏只看「有几个群」，不需要每个群缓存的聊天记录（实测 62KB）。
        # 完整数据留给「记忆」页单独拉，首屏传输量能砍掉九成。
        if state and request.args.get('slim') in ('1', 'true', 'yes'):
            try:
                state = dict(state)
                state['groups'] = _group_rows(state.get('groups'), True)
            except Exception:
                pass
        telemetry=Path(os.getenv('CEYLAN_TELEMETRY_DB','./data/bot_telemetry.db'))
        if state and telemetry.is_file():
            other=None
            try:
                other=sqlite3.connect('file:'+str(telemetry)+'?mode=ro',uri=True,timeout=2)
                snap=other.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
                names={str(g.get('group_id')):g.get('group_name') for g in json.loads(snap[0]).get('napcat',{}).get('groups',[]) or []} if snap else {}
                for group in state['groups']:group['name']=names.get(str(group['id']))
            except (sqlite3.Error,ValueError,TypeError,AttributeError):pass
            finally:
                if other:other.close()
        fresh=bool(state and 0<=time.time()-state['sampled']<90 and 0<=time.time()-r['received']<90)

        # —— 本地文件桥回退：同机部署时读 bot 写的 state.json，无需 https 轮询 ——
        if not fresh:
            local = _read_local_state()
            if local and 0 <= time.time() - local.get('sampled', 0) < 90:
                # 面板首屏只要每群的摘要长度；「记忆」页要原文和缓存数组。
                # 以前这条回退路径无条件把两者换成数字，前端就整块空白；
                # 同时它也不该把 69KB 群聊原文搬进浏览器 —— 所以按 slim 分流，
                # 和上面那条正常路径共用同一个裁剪函数，不会再各写一套。
                state = dict(local)
                slim = request.args.get('slim') in ('1', 'true', 'yes')
                state['groups'] = _group_rows(local.get('groups'), slim)
                fresh = True
                operations = _read_local_ledger()
                received = local.get('sampled')
                return jsonify(state=state, fresh=True, received=received, operations=operations)

        return jsonify(state=state,fresh=fresh,received=r['received'] if r else None,operations=operations)
    @app.post('/admin/api/bot/control',endpoint='control_v4_enqueue')
    def enqueue():
        if not session.get('ceylan_admin'):return fail('请重新登录',401)
        if not csrf():return fail('安全校验失败，请刷新页面',403)
        try:data=validate(readbody(100000))
        except (ValueError,TypeError,UnicodeError) as e:return fail(str(e),400)
        # —— 本地文件桥：SQLite 无状态时，用 bot 写的 state.json 校验，并把命令投递到 inbox/ ——
        local = _read_local_state()
        if local is not None and 0 <= time.time() - local.get('sampled', 0) < 90:
            lstate = local
            target = lstate if data['action'] == 'settings' else next(
                (x for x in lstate.get('groups', []) if str(x['id']) == str(data.get('group'))), None)
            if target is None:
                return fail('请重新加载该群', 409)
            expected = target.get({'settings': 'settings_revision', 'memory_set': 'memory_revision',
                                   'cache_clear': 'cache_revision', 'cache_edit': 'revision'}
                                  .get(data['action'], 'settings_revision'))
            if data['action'] == 'cache_edit':
                expected = next((x.get('revision') for x in target.get('cache', [])
                                 if x.get('seq') == data.get('seq')), None)
            if data.get('revision') != expected:
                return fail('内容版本已变化，请重新加载', 409)
            ident = _write_local_command(data)
            return jsonify(ok=True, id=ident, status='pending'), 202

        with db() as c:
            c.execute('BEGIN IMMEDIATE');expire(c);row=c.execute('SELECT * FROM state WHERE id=1').fetchone()
            if not row:return fail('尚未连接机器人',409)
            state=json.loads(row['payload'])
            if not (0<=time.time()-state['sampled']<90 and 0<=time.time()-row['received']<90):return fail('机器人状态过期，暂不接受修改',409)
            if c.execute("SELECT COUNT(*) FROM commands WHERE status='pending'").fetchone()[0]>=10:return fail('待执行操作过多，请等待',429)
            target=state if data['action']=='settings' else next((x for x in state['groups'] if str(x['id'])==data['group']),None)
            if not target:return fail('请重新加载该群',409)
            expected=target.get({'settings':'settings_revision','memory_set':'memory_revision','cache_clear':'cache_revision'}.get(data['action'],''))
            if data['action']=='cache_edit':expected=next((x.get('revision') for x in target.get('cache',[]) if x.get('seq')==data['seq']),None)
            if data['revision']!=expected:return fail('内容版本已变化，请重新加载',409)
            ident=secrets.token_hex(16);now=time.time()
            c.execute('INSERT INTO commands VALUES(?,?,?,?,?,?)',(ident,now,now+600,json.dumps(data,ensure_ascii=False),'pending','等待机器人执行'))
        return jsonify(ok=True,id=ident,status='pending'),202
    @app.post('/admin/api/bot/control/<ident>/cancel',endpoint='control_v4_cancel')
    def cancel(ident):
        if not session.get('ceylan_admin'):return fail('请重新登录',401)
        if not csrf():return fail('安全校验失败',403)
        # Cancellation of already delivered commands cannot be guaranteed; omit a misleading feature.
        return fail('已排队的操作可能正在执行，请等待执行结果',409)
    @app.after_request
    def headers(response):
        if '/bot/control' in request.path:response.headers['Cache-Control']='no-store'
        return response
    original=app.view_functions['admin_bot']
    def bot_page():
        response=app.make_response(original())
        if response.status_code==200:
            html=response.get_data(as_text=True)
            link='<div class="actions" style="margin:18px 0"><a class="button primary" href="/admin/bot/control">记忆、缓存与回复设置 ↗</a></div>'
            html=html.replace('<section class="bot-hero">',link+'<section class="bot-hero">',1) if '<section class="bot-hero">' in html else html.replace('<section class="page-heading">',link+'<section class="page-heading">',1)
            response.set_data(html)
        return response
    app.view_functions['admin_bot']=bot_page
