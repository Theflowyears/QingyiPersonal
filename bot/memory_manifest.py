"""A03/A04：一次请求的记忆清单与按实际入选集合去重/预算。

边界：只处理「记忆」块（人格/情景上下文/群上下文），不接管行为提示；
manifest 只记 ID、长度和哈希，不落原文。记录失败永远不影响回复。
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path

MANIFEST_BUDGET = {"dm": int(os.getenv("MEMORY_BUDGET_DM_CHARS", "4200")),
                   "group": int(os.getenv("MEMORY_BUDGET_GROUP_CHARS", "3200"))}
KEEP_LINES = int(os.getenv("MEMORY_MANIFEST_KEEP_LINES", "1200"))


def _norm(text):
    return " ".join(str(text or "").split())


def _digest(text):
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def compose(blocks, budget_chars, system_text=""):
    """按 object_id/文本去重，再按分数装预算；返回 (selected, rendered, dropped)。

    blocks 每项：{"id","text","score","kind","source"}。
    - 同一 id 只留一次；
    - 已出现在 system[0] 的整块文本不再重复；
    - 预算内按分数从高到低装；装不下的记录原因，不切半句话。
    """
    system_norm = _norm(system_text)
    seen_ids, seen_text = set(), set()
    candidates, dropped = [], []
    for index, block in enumerate(blocks or []):
        text = str((block or {}).get("text") or "")
        norm = _norm(text)
        block_id = str((block or {}).get("id") or "")
        if not norm:
            continue
        if block_id and block_id in seen_ids:
            dropped.append({"id": block_id, "reason": "duplicate_id"})
            continue
        if norm in seen_text:
            dropped.append({"id": block_id, "reason": "duplicate_text"})
            continue
        if system_norm and norm in system_norm:
            dropped.append({"id": block_id, "reason": "already_in_system"})
            continue
        seen_ids.add(block_id)
        seen_text.add(norm)
        candidates.append({"id": block_id, "kind": str((block or {}).get("kind") or ""),
                           "text": text, "norm": norm,
                           "score": float((block or {}).get("score") or 0),
                           "source": str((block or {}).get("source") or ""),
                           "order": index})
    candidates.sort(key=lambda row: (-row["score"], row["order"]))
    selected, used = [], 0
    for row in candidates:
        length = len(row["text"])
        if used + length > int(budget_chars):
            dropped.append({"id": row["id"], "reason": "budget"})
            continue
        used += length
        selected.append(row)
    selected.sort(key=lambda row: row["order"])
    rendered = "\n\n".join(row["text"] for row in selected)
    return selected, rendered, dropped


def compose_budget(blocks,budget_chars):
    used=0;kept=[];dropped=[]
    for row in blocks:
        size=len(row['text'])+(1 if kept else 0)
        if used+size>int(budget_chars):
            dropped.append({'id':row['id'],'reason':'budget'})
        else:
            kept.append(row);used+=size
    return kept,dropped


def _manifest_path(create=True):
    root = Path(os.getenv("DATA_DIR", "./data")) / "memory" / "manifests"
    # 只读查询不建目录（X15，V01-D3）；写入路径才允许初始化。
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root / (time.strftime("%Y-%m-%d") + ".jsonl")


def _trim(path):
    try:
        if path.stat().st_size < 2 * 1024 * 1024:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        path.write_text("\n".join(lines[-KEEP_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def record(scene, session, messages, selected, dropped, attempt=1, *, runtime=None):
    """写一条最终请求清单：绑定 request hash、实际入选 ID 与渲染哈希。"""
    try:
        texts = []
        for message in messages or []:
            content = (message or {}).get("content")
            if isinstance(content, list):
                content = " ".join(str((part or {}).get("text") or "")
                                   for part in content if isinstance(part, dict))
            texts.append(str(content or ""))
        request_text = "\n".join(texts)
        request_hash = _digest(request_text)
        rendered = {row["id"]: _digest(row["text"])[:16] for row in selected}
        present = {row["id"]: (_norm(row["text"])[:64] in _norm(request_text))
                   for row in selected}
        manifest = {
            "ts": round(time.time(), 3),
            "scene": str(scene or ""),
            "attempt": int(attempt),
            "session": _digest(session)[:16] if session else "",
            "request_sha256": request_hash,
            "budget_chars": MANIFEST_BUDGET.get(str(scene or ""), 0),
            "selected": [{"id": row["id"], "kind": row["kind"],
                          "chars": len(row["text"]), "sha16": rendered[row["id"]],
                          "present": present[row["id"]],
                          'source': row['source'],
                          'source_session': row.get('source_session',''),
                          'visibility': row.get('visibility',''),
                          'root_source_ids': row.get('root_source_ids') or []} for row in selected],
            "dropped": dropped[:40],
            "selected_chars": sum(len(row["text"]) for row in selected),
            "request_chars": len(request_text),
        }
        if runtime is not None:
            manifest['runtime'] = {k: runtime[k] for k in
                                   ('status','filtered','correction','paths','degradation','pipeline','dialogue_projected','expression_source')
                                   if k in runtime}
            manifest['runtime']['queries'] = [
                {'kind':q.get('kind',''), 'sha256':_digest(q.get('text','')),
                 'time_constraints':q.get('time_constraints') or {}} for q in runtime.get('queries') or []]
        path = _manifest_path()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(manifest, ensure_ascii=False,
                                    sort_keys=True, allow_nan=False) + "\n")
        _trim(path)
        return manifest
    except Exception as exc:
        print("[记忆清单] 记录失败 " + type(exc).__name__, flush=True)
        return None


digest = _digest


def last(limit=5):
    try:
        path = _manifest_path(create=False)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return [json.loads(line) for line in lines[-int(limit):] if line.strip()]
    except Exception:
        return []