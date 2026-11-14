"""工具层：把云端 Agent 的 skill 和本地能力接成她能调用的工具。

设计原则：
  1. 每个工具自己负责「怎么调」和「结果怎么讲」，上层只管路由。
  2. 调用都记日志，控制台能看见她什么时候用了什么、花了多久、拿到什么。
  3. Agent 的 skill 靠 prompt 触发，所以每个工具的 prompt 要写得能让它选对工具。
"""
import hashlib
import json
import logging
import os
import re
import time

DATA_DIR = os.getenv("DATA_DIR", "./data")
TOOL_LOG_FILE = os.path.join(DATA_DIR, "tool_calls.json")
LOG_MAX = 120

# 云端工具为什么敢给 300 秒：这些调用现在都挂在「先回一句」之后的后台任务里，
# 首气泡早就发出去了，多等的是后台，不是人。
# 生产实测 11-08 02:4x：fortune 跑到 152.1 秒被 150 秒的超时剪断，返回 0 字 ——
# 那种「工具坏了」其实是我们自己把还在出活的会话掐了。
AGENT_TIMEOUT = int(os.getenv("AGENT_TOOL_TIMEOUT", "300"))

# ── 工具定义 ────────────────────────────────────────────
# kind: agent=云端智能体  local=本地实现
TOOLS = {
    "tarot": {
        "name": "塔罗占卜",
        "skill": "tarot-spread-interpretation",
        "kind": "local",
        "timeout": AGENT_TIMEOUT,
        "desc": "抽塔罗牌并解读牌阵",
        "patterns": r"塔罗|抽牌|牌阵|牌面",
        "prompt": (
            "请用 tarot-spread-interpretation 技能，为提问者抽牌并解读。\n"
            "要求：真实随机抽牌，不要假装；给出牌名、正逆位、牌阵位置含义，"
            "再结合提问者的具体情况解读。直接给结果，不要反问。\n"
            # 生产实测：这个技能包这两天装不上（SKILL_DOWNLOAD_FAILED 重试两次），
            # 于是她只回一句「安装失败」就不抽了 —— 主人要的是一次真抽牌，不是安装报告。
            "技能装不上就自己用密码学随机源（secrets，78 张全牌堆，正逆位同样随机）"
            "真实抽完并解读，开头说一句「这次是自己摇的牌」即可；"
            "两条路都走不通才说没成，别只报安装错误。\n"
            # 生产实测：agent 回一句「我改用加密随机源自己抽」就断了（整段只有 21 字），
            # 牌面留在它下一个回合里 —— 我们只收得到这一条，等于白算。所以要求一次说完。
            "必须在同一条回复里把牌名、正逆位、位置含义和解读全部给完，"
            "不要先说要怎么抽、把结果留到下一句。\n"
            "提问者的问题：%s"
        ),
    },
    "zhouyi": {
        "name": "周易起卦",
        "skill": "zhouyi-hexagram-analysis",
        "kind": "local",
        "timeout": AGENT_TIMEOUT,
        "desc": "起一卦并按周易解析",
        "patterns": r"卦|易经|周易|爻|六十四卦",
        "prompt": (
            "请用 zhouyi-hexagram-analysis 技能，为提问者起卦并解析。\n"
            "要求：真实起卦（掷出六爻），给出卦名、卦象、动爻，"
            "再结合提问者的问题给出解读与建议。直接给结果，不要反问。\n"
            "技能装不上就自己用随机源真掷六爻（从初爻往上掷，少阳/少阴/老阳/老阴四种等概率，"
            "用 secrets），照掷出来的卦解读，别只报安装失败。\n"
            "必须在同一条回复里把卦名、卦象、动爻和解读全部给完，不要分两步说。\n"
            "提问者的问题：%s"
        ),
    },
    "fortune": {
        "name": "今日运势",
        "skill": "daily-comprehensive-fortune",
        "kind": "local",
        "timeout": AGENT_TIMEOUT,
        "desc": "综合运势（事业/感情/财运等）",
        # 除了「运势」这种明说，还要认「算下…抽卡能出六星吗」这类口语问法：
        # 真实群聊里没人会说「帮我算算运势」，说的是「算下今天抽卡能出啥吗」。
        # 两种都要求「算/看」这类动作词在前，避免「我抽卡出了六星」这种陈述被当成问卜。
        "patterns": r"运势|今天运气|今日运|运气怎么样|算算运"
                    r"|(算|卜|占|看)[^。！？]{0,10}(抽卡|单抽|十连|出货|运气|运势)"
                    r"|(抽卡|单抽|十连)[^。！？]{0,8}(能不能|会不会|出不出|可以出)",
        "prompt": (
            "请用 daily-comprehensive-fortune 技能，给出提问者今天的综合运势。\n"
            "要求：覆盖事业、感情、财运、健康几方面，具体到可执行的小建议。"
            "直接给结果，不要反问。\n"
            "技能装不上就自己用随机源真实摇一遍（secrets，别拿同一套话术糊弄），"
            "开头说明是自己摇的；别只报安装失败。\n"
            "必须在同一条回复里把各维度的分数和建议全部给完，不要分两步说。\n"
            "提问者的问题：%s"
        ),
    },
    "video": {
        "name": "视频分析",
        "skill": "alibabacloud-bailian-videoanalysis",
        "kind": "agent",
        "timeout": 240,
        "desc": "看视频内容并总结",
        "patterns": r"这个视频|这段视频|视频里|帮我看看视频|分析一下视频",
        "prompt": (
            "请用视频分析技能分析这段视频，说明画面内容、发生了什么、值得注意的点。\n"
            "视频地址：%s\n"
            "如果拿不到视频，就直接说拿不到，不要编造内容。"
        ),
        "needs": "video_url",
    },
    "video2": {
        "name": "视频分析·备用",
        "skill": "video-ai-analyzer",
        "kind": "agent",
        "timeout": 240,
        "desc": "另一套视频分析（主用失败时兜底）",
        "patterns": r"__never_auto__",
        "prompt": (
            "请用视频分析能力分析这段视频：画面内容、发生了什么、值得注意的细节。\n"
            "视频地址：%s\n"
            "如果拿不到视频，就直接说拿不到，不要编造。"
        ),
        "needs": "video_url",
    },
    "excel": {
        "name": "表格处理",
        "skill": "excel-processing",
        "kind": "agent",
        "timeout": 200,
        "desc": "处理 Excel / 表格数据",
        "patterns": r"excel|表格|xlsx|csv|统计一下这表",
        "prompt": (
            "请用 excel-processing 技能处理这个需求。\n"
            "需求：%s\n"
            "如果缺少文件，就直接说明需要他发文件，不要编数据。"
        ),
        "needs": "file_url",
    },
}

# 本地工具（不走云端）
LOCAL_TOOLS = {
    "weather": {"name": "天气查询", "kind": "local", "desc": "查真实天气", "patterns": r"天气|气温|下雨|带伞|几度"},
    "search": {"name": "联网搜索", "kind": "local", "desc": "查最新信息", "patterns": r"查|搜|最新|是谁|多少"},
    "memory": {"name": "记忆检索", "kind": "local", "desc": "查群里的长期记忆", "patterns": r"总结|之前说|记得"},
}


def all_tools():
    """给面板用的完整列表。"""
    out = []
    for k, v in TOOLS.items():
        out.append({"key": k, "scope": "cloud", **{x: v.get(x) for x in ("name", "skill", "desc", "kind")}})
    for k, v in LOCAL_TOOLS.items():
        out.append({"key": k, "scope": "local", "skill": "", **v})
    return out


def pick_agent_tool(text):
    """从问句里认出该用哪个云端工具。"""
    from tool_intent import request_text
    t = request_text(text)
    # 塔罗要优先于泛化的「算」
    if re.search(TOOLS["tarot"]["patterns"], t):
        return "tarot"
    if re.search(TOOLS["zhouyi"]["patterns"], t):
        return "zhouyi"
    if re.search(TOOLS["video"]["patterns"], t):
        return "video"
    if re.search(TOOLS["excel"]["patterns"], t, re.I):
        return "excel"
    if re.search(TOOLS["fortune"]["patterns"], t):
        return "fortune"
    return ""


# ── 调用日志 ────────────────────────────────────────────
def _load_log():
    try:
        if os.path.isfile(TOOL_LOG_FILE):
            with open(TOOL_LOG_FILE, encoding="utf-8") as f:
                got = json.load(f) or []
            if isinstance(got, list):
                return got
    except Exception:
        pass
    return []


def _save_log(rows):
    try:
        os.makedirs(os.path.dirname(TOOL_LOG_FILE), exist_ok=True)
        tmp = TOOL_LOG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows[-LOG_MAX:], f, ensure_ascii=False)
        os.replace(tmp, TOOL_LOG_FILE)
    except Exception:
        pass


def log_call(tool, ok, seconds=0, query="", preview="", error="", extra=None):
    rows = _load_log()
    rows.append({
        "ts": time.time(),
        "time": time.strftime("%m-%d %H:%M:%S"),
        "tool": str(tool),
        "ok": bool(ok),
        "seconds": round(float(seconds or 0), 1),
        "query": str(query)[:80],
        "preview": str(preview)[:160],
        "error": str(error)[:100],
        "extra": extra or {},
    })
    _save_log(rows)


def recent_calls(limit=30):
    return _load_log()[-int(limit):][::-1]


def stats():
    rows = _load_log()
    outcomes = dict(success=0, failure=0, unknown=0)
    per = {}
    for row in rows:
        kind = (row.get('extra') or {}).get('outcome')
        if kind not in outcomes:
            kind = 'unknown'
        outcomes[kind] += 1
        item = per.setdefault(row.get('tool'), dict(n=0, ok=0, failure=0, unknown=0))
        item['n'] += 1
        item['ok' if kind == 'success' else kind] += 1
    return dict(total=len(rows), ok=outcomes['success'], fail=outcomes['failure'],
                unknown=outcomes['unknown'], outcomes=outcomes, by_tool=per,
                avg_seconds=round(sum(float(r.get('seconds') or 0) for r in rows) / len(rows), 1) if rows else 0)


# ── 执行 ────────────────────────────────────────────────
def _local_artifact(path):
    """URL / 模型声称的路径不是落盘证据；只承认实际存在的非空文件。"""
    try:
        return isinstance(path, (str, os.PathLike)) and os.path.isfile(path) and os.path.getsize(path) > 0
    except (OSError, ValueError, TypeError):
        return False


def _tool_outcome(result, evidence):
    """不同 I/O 的完成证据不同，绝不拿 Agent 的 legacy ok+text 代替完成。"""
    if not isinstance(result, dict):
        return "failure" if evidence == "agent" else "unknown"
    if result.get("ok") is False:
        return "failure"
    if evidence == "agent":
        declared = result.get("outcome")
        if declared in ("success", "failure", "unknown"):
            return declared
        artifacts = [result.get("path")]
        for key in ("images", "audio"):
            values = result.get(key)
            if isinstance(values, (list, tuple)):
                artifacts.extend(values)
        if any(_local_artifact(path) for path in artifacts):
            return "success"
        if not (str(result.get("text") or "").strip() or result.get("images") or result.get("audio") or result.get("path")):
            return "failure"
        return "unknown"
    if result.get("ok") is not True:
        return "unknown"
    if evidence == "artifact":
        return "success" if _local_artifact(result.get("path")) else "unknown"
    if evidence == "text":
        text = result.get("text")
        return "success" if isinstance(text, str) and text.strip() else "unknown"
    return "unknown"


def _record_tool_execution(tool, ref, result, *, evidence, session="", visibility="private",
                           run_id=None, evidence_origin=None, reason=""):
    """公共 I/O 收口复用；ref 只存摘要，reason 只能是调用点给的固定分类。"""
    outcome = _tool_outcome(result, evidence)
    if not reason:
        reason = {"success": "completion_evidence", "failure": "execution_failed",
                  "unknown": "completion_unverified"}[outcome]
    if outcome != "success":
        logging.getLogger(__name__).warning("tool_result tool=%s outcome=%s boundary=%s", tool, outcome, reason)
    digest = "sha256:" + hashlib.sha256(str(ref or "").encode("utf-8")).hexdigest()
    try:
        import self_model
        eid = self_model.record_tool_result(
            tool, digest, outcome=outcome, session=session, visibility=visibility,
            run_id=run_id, evidence_origin=evidence_origin, reason=reason)
        if not eid:
            logging.getLogger(__name__).error("tool_result write failed tool=%s", tool)
    except Exception:
        # 记账坏了不能吞掉真实 I/O 结果，也不能静默丢失证据。
        logging.getLogger(__name__).exception("tool_result write failed tool=%s", tool)
    return outcome


async def run_agent_tool(key, question, context="", *, session="", visibility="private",
                         run_id=None, evidence_origin=None):
    """legacy ok 表示可消费输出；outcome 才表示有无任务完成证据。"""
    import uuid
    execution = run_id or uuid.uuid4().hex
    t0 = time.time()
    result = {"ok": False, "error": "执行未完成"}
    reason = ""
    try:
        cfg = TOOLS.get(key)
        if not cfg:
            result = {"ok": False, "error": "没有这个工具: " + str(key)}
            reason = "unknown_tool"
        elif cfg.get("kind") == "local":
            # 占卜类本地真随机，一次给完，不走云端（云端装技能常失败/留半截）。
            import local_divination
            raw = local_divination.run_local(key, question)
            text = str(raw.get("text") or "").strip()
            ok = bool(raw.get("ok") and text)
            result = {"ok": ok, "text": text, "seconds": round(time.time() - t0, 1),
                      "error": "" if ok else str(raw.get("error") or "没有产出"),
                      "images": [], "audio": []}
            outcome = _tool_outcome(result, "text")
            result["outcome"] = outcome
        else:
            import qwen_agent as qa
            if not qa.configured():
                result = {"ok": False, "error": "Agent 未配置"}
                reason = "not_configured"
            else:
                subj = str(question or "").strip()
                if context:
                    subj += "\n\n补充背景：" + str(context)[:300]
                raw = await qa.run(cfg["prompt"] % subj, timeout=cfg.get("timeout") or 150,
                                   title="tool:" + key)
                outcome = _tool_outcome(raw, "agent")
                if not isinstance(raw, dict):
                    raw = {}
                text = str(raw.get("text") or "").strip()
                ok = bool(raw.get("ok") and text)  # 保持旧调用语义，不能当任务成功使用
                result = {
                    "ok": ok, "text": text, "seconds": round(time.time() - t0, 1),
                    "error": "" if ok else str(raw.get("error") or "没有产出"),
                    "images": raw.get("images") or [], "audio": raw.get("audio") or [],
                    "outcome": outcome,
                }
    except Exception as exc:
        reason = "exception:" + type(exc).__name__
        result = {"ok": False, "error": type(exc).__name__}
    finally:
        # 取消同样留失败证据，再由 asyncio 正常传播取消；不在下层重复包同一执行。
        ev = "text" if (TOOLS.get(key) or {}).get("kind") == "local" else "agent"
        if ev == "text":
            observed = result  # 本地要保留 ok/text，归约成 outcome 会丢完成证据
        elif "outcome" in result:
            observed = {"outcome": result["outcome"]}
        else:
            observed = result
        outcome = _record_tool_execution(
            key, question, observed, evidence=ev, session=session, visibility=visibility,
            run_id=execution, evidence_origin=evidence_origin, reason=reason)
        result["outcome"] = outcome
        result["run_id"] = execution
        try:
            log_call(key, result.get("ok"), time.time() - t0, question,
                     preview=result.get("text", "")[:150], error=result.get("error", ""),
                     extra={"outcome": outcome, "run_id": execution, "session": session})
        except Exception:
            logging.getLogger(__name__).exception("tool call log failed tool=%s", key)
    return result


def build_block(key, result, question=""):
    """把工具结果包成给她看的提示词。"""
    cfg = TOOLS.get(key) or {}
    name = cfg.get("name") or key
    if result.get("outcome") == "unknown":
        return (
            "\n\n【「%s」返回了内容，但任务是否完成尚未核验】\n%s\n"
            "这些只是工具返回的叙述，不是完成证据；不要声称已办完或已真实抽取。\n"
            % (name, str(result.get("text") or "")[:2000])
        )
    if result.get("outcome") == "failure" or not result.get("ok"):
        return (
            "\n\n【你刚想用「%s」，但没成功】\n原因：%s\n"
            "别编结果。如实说一句「这个我没弄成」，或者换个办法。\n"
            % (name, str(result.get("error") or "未知")[:60])
        )
    body = str(result.get("text") or "").strip()
    return (
        "\n\n【你刚用「%s」办完了 · 以下是真实结果，照它说，不许改数字和牌名】\n%s\n\n"
        "用你自己的口气把结论转述给他：先说结果，再说对他的意义，"
        "可以带你的态度（比如吐槽他运气），但事实部分必须照上面的来。\n"
        % (name, body[:2000])
    )
