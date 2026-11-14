"""Apply bounded administrator commands on the Bridge event loop."""
import asyncio
from collections import Counter,deque
from contextvars import ContextVar
from functools import wraps
import json
import os
import random
from pathlib import Path
import re
import shutil
import tempfile
import time
from ceylan_control_core import DEFAULTS,RANGES,FLOATS,BOOLS,revision,validate

ROOT=Path(os.getenv('CEYLAN_CONTROL_DIR','./data/control-v4'))
# 收件节奏：面板保存的参数按这个间隔被接走。单独一条循环，不陪 20 秒的状态快照。
INBOX_SECONDS=float(os.getenv('CEYLAN_INBOX_SECONDS','2'))
INBOX_TASK=None
SCOPE=ContextVar('reply_scope',default='other')
BUSY=Counter();OPTIONS=dict(DEFAULTS);LEDGER={};BATCH_LOCKS={}
# 办事交货的那段话该怎么说。写在这里而不是散落在补发提示词里：
# 面板调「汇报上限」时她听到的要求必须跟着变 —— 模板里留 %s，别把数字写死，
# 否则调到 400 她还是只被要求说 300 字，旋钮又变成假的。
# 下限也要给：只给上限时实测她交 66 个字就走（主人原话是「解析详细一点」），
# 那不算短，算没交货。
REPORT_INSTRUCTION=(
    "办事汇报：这是你替对方查完、算完之后的交代，不是水群里随手接的一句。"
    "说清结论和依据，一般 %d 到 %d 字，一次最多不超过 %d 字："
    "先给结论，再说你是根据什么这么说，最后落到对方能直接拿去用的一句判断。"
    "对方点了名要详细的，就一项一项说透，别只甩一句结论。"
    "写成连着说的话，别编号、别起小标题、别用「综上所述」这类收尾，"
    "也别把人家的原话复述一遍。")


def report_instruction():
    cap = int(OPTIONS.get('report_max_chars') or 300)
    return REPORT_INSTRUCTION % (max(80, cap // 2), cap, cap)

def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,temp=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False,allow_nan=False);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)

def clip_report(text,options=None):
    """整段交代收在 report_max_chars 以内，且落在句子边界上。

    以前只会 `text[:N]` 硬切，切在句子中间，交出去的东西读起来像坏了。
    放在控制层而不是调用方：汇报档自己就得守住这个顶，谁调都一样。
    """
    opts=OPTIONS if options is None else options
    cap=max(40,int(opts.get('report_max_chars') or 300))
    s=str(text or "").strip()
    if len(s)<=cap:return s
    parts=[x.strip() for x in re.findall(r'[^。！？!?\n]+[。！？!?]?',s) if x.strip()]
    out=[];used=0
    for part in parts:
        if used+len(part)>cap:break
        out.append(part);used+=len(part)
    if not out:
        # 整段一个标点也没有，只能硬切 —— 但宁缺一个字也不能超上限
        return s[:cap-1].rstrip()+'…'
    return ''.join(out).strip()

def chunks(text,options=None,profile=None):
    """把回复切成要发送的气泡。

    先按句子边界（。！？!?换行）分句，再把句子**装进**气泡：一条装到装不下才开下一条，
    单句自己超长就在逗号处拆。
    这里原来是「一句一条、超过条数就丢」：四句配 2 条气泡，后两句直接不见了。
    水群丢两句看不出来，办事汇报丢两句就是答非所问 —— 主人要的是别硬截。
    """
    opts=OPTIONS if options is None else options;text=str(text).strip()
    if not text:return []
    if profile=='report':
        # 只归一化本次交货的有效包宽，不改面板保存值。总容量必须容纳汇报硬顶。
        text=clip_report(text,opts)
        if not text:return []
        max_bubbles=max(1,int(opts.get('bubbles_report') or opts['bubbles']))
        cap=max(40,int(opts.get('report_max_chars') or 300))
        max_chars=max(int(opts.get('reply_chars_report') or opts['reply_chars']),
                      (cap+max_bubbles-1)//max_bubbles)
        # 优先完整句；若剩余条数装不下，就拆开下一句填满余量，标点/空白也不能丢。
        out=[]
        while text:
            cut=min(max_chars,len(text))
            if len(text)>max_chars:
                ends=[m.end() for m in re.finditer(r'[。！？!?\n]',text[:max_chars])]
                if ends and len(text)-ends[-1]<=max_chars*(max_bubbles-len(out)-1):
                    cut=ends[-1]
            out.append(text[:cut]);text=text[cut:]
        return out
    max_chars=int(opts['reply_chars']);max_bubbles=int(opts['bubbles'])
    parts=[x.strip() for x in re.findall(r'[^。！？!?\n]+[。！？!?]?|[。！？!?]',text) if x.strip()]
    blocks=[]
    for part in parts:
        while len(part)>max_chars:            # 单句自己就超长：在逗号处拆，别硬切字
            cut=part[:max_chars]
            pos=max(cut.rfind('，'),cut.rfind('、'),cut.rfind(' '))
            if pos<max(8,max_chars//3):pos=max_chars
            head,part=part[:pos].rstrip('，、 '),part[pos:].lstrip()
            if head:blocks.append(head)
        if part:blocks.append(part)
    out=[];dropped=0
    for block in blocks:
        if out and len(out[-1])+len(block)<=max_chars:
            out[-1]+=block
        elif len(out)<max_bubbles:
            out.append(block)
        else:
            dropped+=len(block)
    if dropped:
        # 装不下必须出声（主人要的是别硬截），但一条回复顶多喊一次，别把日志刷满
        print('[切气泡] %s%d 条 × %d 字装不下，后面 %d 字没发出去'
              % ('汇报' if profile=='report' else '',max_bubbles,max_chars,dropped),flush=True)
    if not out:out=[text[:max_chars]]
    return out

def _sentence_parts(text):
    """按句末标点/换行断句，标点留在前一句里。拆分和计数共用同一套口径。"""
    return [s for s in re.split(r'(?<=[。！？!?])\s*|\n+', str(text or "").strip()) if s.strip()]

def split_bubbles(text,target=None,rng=None):
    """聊天回复按句子拆成 1~3 个气泡，条数随内容加权随机。

    以前聊天恒走 chunks()：它按字数打包，短回复装不满一个气泡就永远是 1 条，
    主人看到的是「一句一条 / 一句不动」。这里先在句末标点处断句，再按
    「句子越多、越可能分成更多条」掷 1~3 条：两句大多分成两条，三句以上才可能到三条。
    target 是给情感模块以后接管条数留的口子 —— 传了就用，不传才掷骰。
    内容一条都不丢，只在句子边界处切。
    """
    text=str(text or "").strip()
    if not text:return []
    sents=_sentence_parts(text)
    if not sents:sents=[text]
    n=len(sents)
    if n<=1:return [text]
    if target is None:
        rng=rng or random
        if n==2:
            target=2 if rng.random()<0.7 else 1
        else:
            r=rng.random()
            target=1 if r<0.15 else 3 if r>0.75 else 2
    target=max(1,min(int(target),min(3,n)))
    if target<=1:return [text]
    groups=[[] for _ in range(target)]
    for i,s in enumerate(sents):
        groups[min(i*target//n,target-1)].append(s)
    out=[''.join(g).strip() for g in groups]
    out=[x for x in out if x]
    return out or [text]

def is_silence(text):
    """先读原始决定，再做任何长度处理，不能把尾部沉默标记剪掉。"""
    raw=str(text or '').upper()
    return '[[沉默]]' in raw or 'SILENCE' in raw


def settings(ns):return {**OPTIONS,**{k:ns.get(k) for k in list(RANGES)+list(FLOATS)+list(BOOLS) if k in ns}}
def backup(ns,action,ident):
    folder=ROOT/'backups'/ident;folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    paths=[ROOT/'settings.json'] if action=='settings' else [Path(ns['GROUP_MEMORY_STATE_FILE'])] if action=='memory_set' else [Path(ns['PERIODIC_STATE_FILE'])]
    for p in paths:
        if p.is_file():shutil.copy2(p,folder/p.name)
    # Keep the exact pre-change in-memory state as well as its last disk copy.
    if action=='memory_set':atomic(folder/'live-memory.json',dict(ns['group_memory_summary']))
    if action.startswith('cache_'):atomic(folder/'live-cache.json',{k:list(v) for k,v in ns['group_message_cache'].items()})
    old=sorted((ROOT/'backups').iterdir(),key=lambda p:p.stat().st_mtime)
    for p in old[:-30]:shutil.rmtree(p)
    return str(folder)

def apply(ns,rt,command):
    data=validate(command);act=data['action'];gid=data.get('group')
    if gid and (rt.blocked(gid) or BUSY[gid]):raise ValueError('该群被屏蔽或正在处理消息/整理记忆，请稍后重试')
    if gid and gid not in ns['group_message_cache'] and gid not in ns['group_memory_summary']:raise ValueError('该群暂无运行状态，请先加载有效群')
    if act=='settings':
        if data['revision']!=revision(settings(ns)):raise ValueError('设置已变化，请重新加载')
    elif act=='memory_set':
        if data['revision']!=revision(ns['group_memory_summary'].get(gid,'')):raise ValueError('记忆已变化，请重新加载后编辑')
    elif act=='cache_clear':
        if data['revision']!=revision(list(ns['group_message_cache'].get(gid,[]))):raise ValueError('缓存已变化，请重新加载后确认')
    else:
        row=next((x for x in ns['group_message_cache'].get(gid,[]) if x.get('seq')==data['seq']),None)
        if row is None or revision(row)!=data['revision']:raise ValueError('缓存条目已变化或被淘汰')
    b=backup(ns,act,command['id'])
    if act=='settings':
        values={**settings(ns),**data['values']};atomic(ROOT/'settings.json',values)
        for k,v in data['values'].items():
            if k in ns:ns[k]=v
            else:OPTIONS[k]=v
        _push_autonomy(data['values'])
    elif act=='memory_set':
        summaries=dict(ns['group_memory_summary']);summaries[gid]=data['text']
        seqs=dict(ns['group_memory_last_seq']);seqs[gid]=ns['group_message_seq'].get(gid,0)
        timestamps=dict(ns['group_memory_last_update_ts']);timestamps[gid]=time.time()
        atomic(Path(ns['GROUP_MEMORY_STATE_FILE']),{'group_memory_summary':summaries,'group_memory_last_seq':seqs,'group_memory_last_update_ts':timestamps})
        ns['group_memory_summary'][gid]=data['text'];ns['group_memory_last_seq'][gid]=seqs[gid];ns['group_memory_last_update_ts'][gid]=timestamps[gid]
    else:
        caches={k:list(v) for k,v in ns['group_message_cache'].items()}
        if act=='cache_clear':caches[gid]=[]
        else:caches[gid]=[{**x,'text':data['text']} if x.get('seq')==data['seq'] else x for x in caches[gid]]
        done=dict(ns['group_last_periodic_seq'])
        if act=='cache_clear':done[gid]=ns['group_message_seq'].get(gid,0)
        atomic(Path(ns['PERIODIC_STATE_FILE']),{'group_message_cache':caches,'group_message_seq':dict(ns['group_message_seq']),'group_last_periodic_seq':done,'periodic_answer_fingerprints':{k:list(v) for k,v in ns['periodic_answer_fingerprints'].items()}})
        cache=ns['group_message_cache'][gid];cache.clear();cache.extend(caches[gid])
        if act=='cache_clear':
            ns['group_image_cache'].pop(gid,None);ns['group_last_periodic_seq'][gid]=done[gid]
            for key in list(ns['histories']):
                if key.startswith('group-'+gid+'-'):ns['histories'].pop(key,None)
    return b

def drain(ns,rt):
    """排空 inbox —— 面板点「保存并生效」以后，参数是在这里落到运行中的进程的。

    以前这段只挂在 20 秒一圈的 poll 里，面板却写着「1~2 秒内生效」：
    保存完立刻回来看，那个数还是旧的，主人就以为热加载坏了。拆成单独的函数，
    由 `_inbox_loop` 每 2 秒跑一次；poll 继续顺带排空，但 70KB 的 state.json
    仍按 20 秒写，不为了拿一条命令就把状态整份重算。
    返回值 = 这次真的应用成功了几条命令，收件循环靠它决定要不要刷快照。
    """
    inbox=ROOT/'inbox';inbox.mkdir(parents=True,exist_ok=True,mode=0o700)
    applied=0
    for path in sorted(inbox.glob('*.json'))[:10]:
        cmd=json.loads(path.read_text(encoding='utf-8'));ident=cmd.get('id','')
        if not re.fullmatch('[a-f0-9]{32}',ident):path.unlink();continue
        if ident not in LEDGER:
            result={'id':ident,'time':time.time()}
            try:
                if time.time()>cmd['expires']:raise ValueError('操作已过期，未执行')
                result['backup']=apply(ns,rt,cmd);result.update(status='done',message='已执行并保存')
                applied+=1
            except ValueError as e:result.update(status='failed',message=str(e))
            except Exception as e:result.update(status='failed',message='执行失败：'+type(e).__name__)
            LEDGER[ident]=result
            while len(LEDGER)>200:LEDGER.pop(next(iter(LEDGER)))
            atomic(ROOT/'ledger.json',LEDGER)
        path.unlink()
    return applied


async def _inbox_loop(ns,rt,period=None):
    """只排空收件；真的改了参数才补一份状态快照。

    以前 inbox 只跟着 20 秒一圈的快照循环被顺带排空，面板却写「1~2 秒内生效」。
    改成 2 秒单独跑以后又冒出第二个坑：快照还是 20 秒一写，于是保存回执明明写着
    「已执行并保存」，面板回头再抓一次读到的仍是 20 秒前的旧档 ——
    主人那句「报成功之后立即重新抓取数据还是没有修改」就是它。
    收件这边只要真的改动过就顺手刷一次快照；没改动就不写（state.json 有 70KB）。
    """
    while True:
        try:
            if drain(ns,rt):write_state(ns,rt)
        except Exception as e:print('[控制收件] '+type(e).__name__,flush=True)
        await asyncio.sleep(period if period is not None else INBOX_SECONDS)


def poll(ns,rt):
    drain(ns,rt)
    write_state(ns,rt)


def write_state(ns,rt):
    groups=[];used=0
    ids=set(ns['group_message_cache'])|set(ns['group_memory_summary'])
    for gid in sorted(ids):
        if rt.blocked(gid):continue
        memory=ns['group_memory_summary'].get(gid,'');cache=list(ns['group_message_cache'].get(gid,[]))
        rows=[{'seq':x.get('seq'),'time':x.get('time'),'nickname':x.get('nickname'),'user_id':x.get('user_id'),'text':x.get('text',''),'is_bot':bool(x.get('is_bot')),'image_count':len(x.get('image_urls',[])),'revision':revision(x)} for x in cache[-30:]]
        g={'id':gid,'memory':memory[:20000],'memory_complete':len(memory)<=20000,'memory_revision':revision(memory),'memory_updated':ns['group_memory_last_update_ts'].get(gid),'cache':rows,'cache_total':len(cache),'cache_revision':revision(cache),'busy':bool(BUSY[gid])}
        size=len(json.dumps(g,ensure_ascii=False).encode())
        if used+size>800000:break
        groups.append(g);used+=size
    atomic(ROOT/'state.json',{'sampled':time.time(),'settings':settings(ns),'settings_revision':revision(settings(ns)),'groups':groups,'group_total':len([x for x in ids if not rt.blocked(x)]),'acks':list(LEDGER.values())[-100:]})

def _push_autonomy(values):
    """把自主额度转给真正读它的模块。

    `bili_auto` / `curiosity` 不在这个命名空间里（各自 import），
    apply 里那句 `ns[k]=v` 覆盖不到它们 —— 不显式转发的话，面板保存只写进了
    settings.json，运行中的额度一动不动，得等重启才生效（重启会断 QQ）。
    """
    try:
        import autonomy_limits
        autonomy_limits.update(values or {})
    except Exception as e:
        print("[自主额度] 下发失败 %s: %s" % (type(e).__name__, str(e)[:80]), flush=True)


def install(ns,rt):
    if ns.get('_control_v4'):return
    ns['_control_v4']=True;ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    if (ROOT/'settings.json').exists():
        values=json.loads((ROOT/'settings.json').read_text(encoding='utf-8'));validate({'action':'settings','revision':'0'*64,'values':values})
        for k,v in values.items():
            if k in ns:ns[k]=v
            else:OPTIONS[k]=v
        _push_autonomy(values)
    if (ROOT/'ledger.json').exists():LEDGER.update(json.loads((ROOT/'ledger.json').read_text(encoding='utf-8')))
    def scoped(name,scope,group=False):
        fn=ns[name]
        @wraps(fn)
        async def wrapper(*args,**kwargs):
            if scope=='memory' and not OPTIONS['memory_enabled']:return False
            import inspect
            bound=inspect.signature(fn).bind(*args,**kwargs).arguments if group else {}
            gid=str(bound.get('group_id','')) if scope=='memory' else str(bound.get('event',{}).get('group_id','')) if group else ''
            token=SCOPE.set(scope)
            if gid:BUSY[gid]+=1
            try:return await fn(*args,**kwargs)
            finally:
                SCOPE.reset(token)
                if gid:
                    BUSY[gid]-=1
                    if BUSY[gid]<=0:BUSY.pop(gid,None)
        ns[name]=wrapper
    for name,scope,group in [('handle_group_message','chat',True),('handle_private_message','chat',False),('periodic_group_review','patrol',False),('compact_group_memory_once','memory',True),('run_agent_task','task',False)]:scoped(name,scope,group)
    # 启动自检：确认包装器能接受 chat_fast（历史上因签名不匹配导致 TypeError）
    try:
        import inspect as _insp
        for _n in ('call_openclaw','call_mimo_vision'):
            _p = _insp.signature(ns[_n]).parameters
            _ok = ('chat_fast' in _p) or any(x.kind == _insp.Parameter.VAR_KEYWORD for x in _p.values())
            print('[控制层自检] ' + _n + ' 接受 chat_fast: ' + ('YES' if _ok else 'NO >>> 会 TypeError'), flush=True)
    except Exception as _e:
        print('[控制层自检失败] ' + str(_e), flush=True)
    for name in ('call_openclaw','call_mimo_vision'):
        def wrap(fn):
            @wraps(fn)
            async def call(messages,max_tokens=800,timeout=300,reply_profile=None,**extra):
                internal=extra.pop('internal',False) or SCOPE.get()=='internal'
                report=extra.pop('report',False)
                if internal:
                    token=SCOPE.set('internal')
                    try:return await fn(messages,max_tokens=max_tokens,timeout=timeout,**extra)
                    finally:SCOPE.reset(token)
                # long_output=True：这轮要成篇输出（写小说/介绍OC/生成代码），
                # 不套字数限制、不切分条、给足 token，避免内容被砍后反复补写。
                # 注意：这里不能用 pop —— 否则 long_output 传不到下游，
                # 下游会以为这是水群，把思考关掉，只吐出几个 token。
                if reply_profile=='long' and not report:
                    extra['long_output']=True
                if extra.get('long_output'):
                    max_tokens=max(max_tokens, 3000)
                    return await fn(messages,max_tokens=max_tokens,timeout=timeout,**extra)
                # report=True：这是「办事的交代」，不是水群接话。
                # 以前交货也走这条通道，被 reply_tokens 掐住 token、被水群指令教成
                # 「一次最多三句」，再被 chunks 切成 60 字 —— 1970 字的牌面就是这么没的。
                if report:
                    copied=[dict(m) for m in messages]
                    instruction=report_instruction()
                    if OPTIONS.get('style_note'):instruction+='\n管理员补充的语气偏好：'+OPTIONS['style_note']
                    if copied and copied[0].get('role')=='system' and isinstance(copied[0].get('content'),str):
                        copied[0]['content']+='\n'+instruction
                    else:copied.insert(0,{'role':'system','content':instruction})
                    answer=await fn(copied,max_tokens=max_tokens,timeout=timeout,**extra)
                    return answer if is_silence(answer) else '\n'.join(chunks(answer,profile='report'))
                if reply_profile in ('intimate','boundary','task'):
                    # 专用格式由 bridge 负责；保留请求的 token/chat_fast，不借长文档开启思考。
                    return await fn(messages,max_tokens=max_tokens,timeout=timeout,**extra)
                if SCOPE.get() in ('chat','patrol'):
                    max_tokens=min(max_tokens,OPTIONS['reply_tokens'])
                    instruction='水群式回复：一次最多发三句，多数时候两句就够。像真人群友随手打字，可以有情绪、有态度、接得住梗，别冷冰冰只回两三个字。别铺垫、别总结、别列点。只有用户明确要详细或写东西时才展开。他说的原话是唯一事实：他没说自己逃课/迟到/撒谎，就别替他说；他纠正你时先认错，别把被冤枉的角色安到自己身上。保留SILENCE判断规则。'
                    if OPTIONS.get('style_note'):instruction+='\n管理员补充的语气偏好：'+OPTIONS['style_note']
                    copied=[dict(m) for m in messages]
                    if copied and copied[0].get('role')=='system' and isinstance(copied[0].get('content'),str):copied[0]['content']+='\n'+instruction
                    else:copied.insert(0,{'role':'system','content':instruction})
                    answer=await fn(copied,max_tokens=max_tokens,timeout=timeout,**extra)
                    return answer if is_silence(answer) else '\n'.join(chunks(answer))
                return await fn(messages,max_tokens=max_tokens,timeout=timeout,**extra)
            return call
        ns[name]=wrap(ns[name])
    def outgoing(name):
        fn=ns[name]
        @wraps(fn)
        async def send(ws,*args,reply_profile=None,**kw):
            import inspect
            signature=inspect.signature(fn)
            bound=signature.bind(ws,*args,**kw);bound.apply_defaults()
            # 兼容显式参数、位置传参及仅有 **kwargs 的底层发送函数。
            profile=bound.arguments.pop('reply_profile',None)
            if reply_profile is None:reply_profile=profile
            extras=next((bound.arguments[p.name] for p in signature.parameters.values()
                         if p.kind==inspect.Parameter.VAR_KEYWORD),{})
            profile=extras.pop('reply_profile',None)
            if reply_profile is None:reply_profile=profile
            gid=bound.arguments.get('group_id')
            if gid is not None and rt.blocked(gid):return None
            # 交货优先：有人点名让她办的事不该被「巡群开关」顺手判掉 ——
            # 后台查询挂在巡群作用域里跑，关掉巡群就等于把答应的那句一起关了。
            report=bool(bound.arguments.get('report',extras.get('report',False)))
            if 'reply_profile' in signature.parameters and reply_profile in ('long','task') and not report:
                bound.arguments['reply_profile']=reply_profile
            _txt=bound.arguments['text']
            if SCOPE.get()=='internal':return await fn(*bound.args,**bound.kwargs)
            if is_silence(_txt):return False
            if not report:
                if SCOPE.get() not in ('chat','patrol'):return await fn(*bound.args,**bound.kwargs)
                if SCOPE.get()=='patrol' and not OPTIONS['patrol_enabled']:return None
            # 专用格式已在 bridge 收好；显式长文不再靠 200/201 字的启发式猜测。
            if report:
                messages=chunks(_txt,profile='report')
            elif reply_profile in ('intimate','boundary','long','task'):
                messages=[_txt]
            elif len(_txt) > max(200, OPTIONS['reply_chars']*4):
                messages=[_txt]
            else:
                # 多句闲聊按 1~3 个气泡随机发（情感模块以后可通过 target 接管条数）；
                # 单句/无标点长块仍走 chunks() 的旧口径，别把整段长句硬拆成气泡。
                messages=(split_bubbles(_txt) if len(_sentence_parts(_txt)) >= 2
                          else chunks(_txt))
            key=('g',str(gid)) if gid is not None else ('p',str(bound.arguments.get('user_id')))
            entry=BATCH_LOCKS.setdefault(key,[asyncio.Lock(),0]);entry[1]+=1
            try:
                async with entry[0]:
                    for i,message in enumerate(messages):
                        if i:await asyncio.sleep(OPTIONS['delay_ms']/1000)
                        bound.arguments['text']=message
                        if i and 'mention' in bound.arguments:bound.arguments['mention']=False
                        result=await fn(*bound.args,**bound.kwargs)
                        if result is not True:return result
                    return bool(messages)
            finally:
                entry[1]-=1
                if not entry[1]:BATCH_LOCKS.pop(key,None)
        ns[name]=send
    outgoing('send_group_msg');outgoing('send_private_msg')
    recent=ns['build_group_recent_text']
    @wraps(recent)
    def review(*a,**kw):return recent(*a,**kw) if OPTIONS['patrol_enabled'] else ('',0,0,0,[])
    ns['build_group_recent_text']=review
    old=rt.snapshot
    def snapshot():
        old()
        try:poll(ns,rt)
        except Exception as e:print('[控制同步] '+type(e).__name__,flush=True)
    rt.snapshot=snapshot
    # 收件单独跑：保存参数以后等 20 秒才生效，面板上那句「立刻」就是假话。
    global INBOX_TASK
    if INBOX_TASK is None:
        try:
            INBOX_TASK = asyncio.get_running_loop().create_task(_inbox_loop(ns, rt))
            print('[控制收件] 已启动，每 %g 秒排空一次 inbox' % INBOX_SECONDS, flush=True)
        except RuntimeError:
            print('[控制收件] 没有运行中的事件循环，收件仍跟着 20 秒快照走', flush=True)
