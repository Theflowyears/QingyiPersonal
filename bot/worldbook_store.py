# -*- coding: utf-8 -*-
"""社区世界书的只读导入、版本化索引与有领域门的检索。"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path

import safe_store
import text_tokens

REPOSITORY="https://github.com/Mr-Courage/ArknightsWorldbook"
_TYPES={"00":"concept","01":"race","02":"event","03":"organization","04":"operator"}
_AMBIGUOUS={"主人","博士","罗德岛","感染"}


def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def parse_lorebook(data,*,source_file,source_commit):
    if not isinstance(data,dict) or data.get("version")!=1 or data.get("type")!="lorebook":
        raise ValueError("unsupported lorebook format")
    entries=(data.get("data") or {}).get("entries")
    if not isinstance(entries,list):
        raise ValueError("lorebook entries missing")
    rows=[];seen=set()
    kind=_TYPES.get(str(source_file)[:2],"concept")
    for entry in entries:
        eid=str(entry.get("id") or "")
        content=str(entry.get("content") or "")
        name=str(entry.get("name") or "")
        if not eid or eid in seen or not content or not name:
            raise ValueError("invalid or duplicate lorebook entry")
        seen.add(eid)
        row={"knowledge_id":"arknights:"+eid,"namespace":"arknights","entity_type":kind,
             "canonical_name":name,"aliases":[str(x) for x in entry.get("keywords") or [] if x],
             "content":content,"source_repository":REPOSITORY,"source_commit":str(source_commit),
             "source_file":str(source_file),"source_entry_id":eid,"source_hash":_hash(content),
             "trust_level":"community_unverified","fictional_domain":"明日方舟",
             "spoiler_scope":"unknown","validity":"active" if entry.get('enabled',True) is True else "disabled","revision":_hash([eid,content])}
        rows.append(row)
    return rows


def build_release(files,commit):
    entries=[]
    for name,data in sorted(files,key=lambda x:x[0]):
        entries.extend(parse_lorebook(data,source_file=name,source_commit=commit))
    ids=[x["knowledge_id"] for x in entries]
    if len(ids)!=len(set(ids)):
        raise ValueError("duplicate entry across lorebooks")
    return {"schema":1,"namespace":"arknights","source_commit":str(commit),
            "revision":_hash(entries),"entries":entries,
            "trust_level":"community_unverified","license_status":"not_declared"}


def diff_releases(old,new):
    a={x["knowledge_id"]:x for x in (old or {}).get("entries") or []}
    b={x["knowledge_id"]:x for x in (new or {}).get("entries") or []}
    return {"added":sorted(set(b)-set(a)),"removed":sorted(set(a)-set(b)),
            "changed":sorted(k for k in set(a)&set(b) if a[k]["source_hash"]!=b[k]["source_hash"])}


class WorldbookStore:
    def __init__(self,root=None):
        self.root=Path(root or os.getenv("DATA_DIR","./data"))/"memory"/"worldbooks"

    def _release_path(self,revision):
        if not re.fullmatch(r'[0-9a-f]{64}',str(revision)):
            raise ValueError('invalid worldbook revision')
        return self.root/"arknights"/(str(revision)+".json")

    def _active_path(self):
        return self.root/"arknights"/"active.json"

    def save_release(self,release):
        if not release.get("revision") or not isinstance(release.get("entries"),list):
            return False
        if _hash(release['entries']) != release['revision']:
            return False
        path=self._release_path(release['revision'])
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8')) == release
        return safe_store.atomic_write_json(str(path),release)

    def activate_revision(self,revision):
        path=self._release_path(revision)
        if not path.is_file():return False
        release=json.loads(path.read_text(encoding='utf-8'))
        if release.get('revision') != revision or _hash(release.get('entries')) != revision:
            return False
        return safe_store.atomic_write_json(str(self._active_path()),{"revision":str(revision)})

    def rollback(self,revision):
        return self.activate_revision(revision)

    def load_active(self):
        try:
            active=json.loads(self._active_path().read_text(encoding="utf-8"))
            release=json.loads(self._release_path(active['revision']).read_text(encoding='utf-8'))
            return release if _hash(release.get('entries')) == active['revision'] else {}
        except (OSError,ValueError,KeyError):
            return {}

    def revisions(self):
        folder=self.root/"arknights"
        return sorted(p.stem for p in folder.glob("*.json") if p.name!="active.json") if folder.is_dir() else []

    def search(self,query,*,domain_hint="",limit=4):
        query=str(query or "");hint=str(domain_hint or "")
        tokens=text_tokens.tokens(query)
        domain=("明日方舟" in query or "明日方舟" in hint or "方舟" in query or
                any(w in query for w in ("泰拉","源石","干员","矿石病")))
        ranked=[]
        for row in self.load_active().get("entries") or []:
            if row.get('validity') != 'active':
                continue
            names=[row["canonical_name"]]+row.get("aliases",[])
            exact=any(name and name in query for name in names)
            if row["canonical_name"] in _AMBIGUOUS and not domain:
                continue
            overlap=len(tokens & text_tokens.tokens(row["canonical_name"]+" "+" ".join(row.get("aliases",[]))+" "+row["content"]))
            if not exact and overlap<2:
                continue
            ranked.append((2 if exact else 1,overlap,row))
        ranked.sort(key=lambda x:(-x[0],-x[1],x[2]["knowledge_id"]))
        return [x[2] for x in ranked[:max(0,int(limit))]]


def render_context(rows):
    if not rows:return ""
    lines=["【明日方舟社区设定参考 · 未逐项核验】",
           "这不是清漪的亲历；只在当前话题确实属于该作品时使用，不确定细节不要补造。"]
    for row in rows:
        lines.append("- "+row["canonical_name"]+"："+row["content"])
    return "\n".join(lines)


def import_directory(path,commit,*,activate=False,root=None):
    path=Path(path)
    files=[]
    for file in sorted(path.glob("*.json")):
        files.append((file.name,json.loads(file.read_text(encoding="utf-8"))))
    release=build_release(files,commit)
    store=WorldbookStore(root)
    if not store.save_release(release):
        raise OSError("worldbook release write failed")
    if activate and not store.activate_revision(release["revision"]):
        raise OSError("worldbook activation failed")
    return {"revision":release["revision"],"entries":len(release["entries"]),"activated":bool(activate)}


def main(argv=None):
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest="command",required=True)
    imp=sub.add_parser("import");imp.add_argument("--input-dir",required=True);imp.add_argument("--commit",required=True);imp.add_argument("--activate",action="store_true")
    sub.add_parser("status")
    rollback=sub.add_parser("rollback");rollback.add_argument("revision")
    args=parser.parse_args(argv)
    if args.command=="import":
        result=import_directory(args.input_dir,args.commit,activate=args.activate)
    elif args.command=="rollback":
        result={"ok":WorldbookStore().rollback(args.revision)}
    else:
        active=WorldbookStore().load_active();result={"revision":active.get("revision"),"entries":len(active.get("entries") or []),"revisions":WorldbookStore().revisions()}
    print(json.dumps(result,ensure_ascii=False));return 0

if __name__=="__main__":raise SystemExit(main())