"""资产分类与来源清单只产生派生索引，不搬走或重写原件。"""
import hashlib
import json
import os
from pathlib import Path

import safe_store

_ASSETS=(('memory/world_knowledge.json','world_knowledge','public:knowledge'),
         ('memory/group_facts.json','autobiographical_memory','group_scoped'),
         ('persona/journal.jsonl','autobiographical_memory','source_session'),
         ('persona/persona.db','autobiographical_memory','source_session'),
         ('persona/cognition.db','autobiographical_memory','source_event'),
         ('periodic_state.json','working_state','source_session'))


def inventory():
    root=Path(os.getenv('DATA_DIR','./data'))
    assets=[]
    for relative,category,scope in _ASSETS:
        path=root/relative
        if not path.is_file():continue
        raw=path.read_bytes()
        quality='unknown'
        count=None
        if category=='world_knowledge':
            try:
                rows=json.loads(raw).get('topics') or []
                count=len(rows)
                quality='verified' if rows and all(x.get('verified') is True and x.get('evidence_url') for x in rows) else 'unknown'
            except (ValueError,AttributeError):quality='unreadable'
        assets.append({'asset':relative,'category':category,'scope_basis':scope,
                       'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
                       'source_quality':quality,'count':count})
    world=root/'memory'/'worldbooks'/'arknights'/'active.json'
    if world.is_file():
        import worldbook_store
        release=worldbook_store.WorldbookStore().load_active()
        assets.append({'asset':'memory/worldbooks/arknights','category':'world_knowledge',
                       'scope_basis':'public:knowledge','revision':release.get('revision'),
                       'source_quality':'community_unverified','count':len(release.get('entries') or [])})
    return {'schema':1,'assets':assets,'mutations':[],
            'rules':['知识不是亲历','来源不明不升级','私聊不跨受众','原件保留','训练语料不纳入本次整理']}


def build_catalog():
    report=inventory()
    revision=hashlib.sha256(json.dumps(report,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    report['revision']=revision
    path=Path(os.getenv('DATA_DIR','./data'))/'ops'/'memory_catalog'/(revision+'.json')
    if not path.exists() and not safe_store.atomic_write_json(str(path),report):
        raise OSError('derived catalog write failed')
    return report
