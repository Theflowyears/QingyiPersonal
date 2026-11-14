"""Shared bounded command contract. No arbitrary shell, path, or Python execution."""
import hashlib
import json

# 水群默认极简：几个字到二十几字，一次一条；想长再在面板调高
DEFAULTS={'reply_tokens':80,'reply_chars':30,'bubbles':2,'delay_ms':500,'patrol_enabled':True,'memory_enabled':True,'style_note':''}
RANGES={'reply_tokens':(30,800),'reply_chars':(15,600),'bubbles':(1,3),'delay_ms':(50,10000),
 'ACTIVE_GROUP_CONTEXT_LIMIT':(3,40),'PERIODIC_REVIEW_INTERVAL_SECONDS':(10,86400),
 'GROUP_MEMORY_COMPACT_INTERVAL_SECONDS':(300,86400),'GROUP_MEMORY_MIN_NEW_MESSAGES':(1,500),
 'GROUP_MEMORY_CONTEXT_MAX_CHARS':(200,3000),

 # ── 自动接话节奏（高级设置里可热改，改完立刻生效，不用重启）──
 'GROUP_AUTO_REPLY_COOLDOWN':(5,600),     # 说完一句后多少秒内不再主动接话
 'GROUP_AUTO_REPLY_EVERY':(1,100),        # 每多少条群消息考虑一次接话
 'GROUP_AUTO_REPLY_FORCE':(1,200),        # 连续多少条没说话就强制说一句
 'GROUP_AUTO_REPLY_MIN_HUMAN':(1,50),     # 至少几条真人消息才允许接话
 'MAX_GROUPS_PER_REVIEW':(1,100)}        # 每轮巡群最多检查几个群

# 浮点参数单独放：validate 里 int 的 type 检查会挡住 0.8 这种值
FLOATS={'GROUP_AUTO_REPLY_PROB':(0.0,1.0),      # 接话概率
 'GROUP_AUTO_REPLY_DEBOUNCE':(0.0,60.0)}        # 消息连续到达时的防抖秒数

# 布尔开关
BOOLS=('patrol_enabled','memory_enabled','GROUP_AUTO_REPLY_ENABLED')
def revision(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def validate(data):
    if not isinstance(data,dict):raise ValueError('请求格式错误')
    action=data.get('action');rev=data.get('revision')
    if action not in ('settings','memory_set','cache_clear','cache_edit'):raise ValueError('不支持的操作')
    if not isinstance(rev,str) or len(rev)!=64 or any(x not in '0123456789abcdef' for x in rev):raise ValueError('缺少有效版本号，请重新加载')
    result={'action':action,'revision':rev}
    if action=='settings':
        values=data.get('values')
        if not isinstance(values,dict) or not values:raise ValueError('参数为空')
        for k,v in values.items():
            if k=='style_note':
                if not isinstance(v,str) or len(v)>500:raise ValueError('语气偏好最多 500 字')
            elif k in BOOLS:
                if type(v) is not bool:raise ValueError('开关应为布尔值')
            elif k in FLOATS:
                if type(v) not in (int,float) or not FLOATS[k][0]<=float(v)<=FLOATS[k][1]:raise ValueError('参数超出范围：'+str(k))
            elif k not in RANGES or type(v) is not int or not RANGES[k][0]<=v<=RANGES[k][1]:raise ValueError('参数超出范围：'+str(k))
        result['values']=values
    else:
        gid=str(data.get('group',''))
        if not gid.isdigit() or len(gid)>20:raise ValueError('无效群号')
        result['group']=gid
        if action in ('memory_set','cache_edit'):
            text=data.get('text');limit=20000 if action=='memory_set' else 300
            if not isinstance(text,str) or len(text)>limit:raise ValueError('内容超过长度上限')
            result['text']=text
        if action=='cache_edit':
            seq=data.get('seq')
            if type(seq) is not int or seq<0:raise ValueError('缓存序号错误')
            result['seq']=seq
    return result
