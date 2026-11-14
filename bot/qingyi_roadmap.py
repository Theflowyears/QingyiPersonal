"""清漪推进目录。这是检测器的唯一机器目录，前端不自行猜测依赖。"""
import copy

STAGES = (
    {"id": "tonight", "name": "当前可推进", "order": 0, "purpose": "当前满足前置即可做的本地观测、检测建设和页面接入；不绑定日期。"},
    {"id": "stage0", "name": "阶段0 · 统一证据", "order": 1, "purpose": "稳定事件、身份、范围与撤回契约。"},
    {"id": "stage1", "name": "阶段1 · 自传体记忆", "order": 2, "purpose": "逐命题来源、矛盾与修订。"},
    {"id": "ripple", "name": "Ripple · 关联记忆", "order": 3, "purpose": "多线索初召回与受限联想扩散。"},
    {"id": "stage2", "name": "阶段2 · 情感影子", "order": 4, "purpose": "可重放的内部状态观察。"},
    {"id": "stage25", "name": "阶段2.5 · 准入", "order": 5, "purpose": "既有十二项自然与工程证据。"},
    {"id": "stage3a", "name": "阶段3a · 有限表达", "order": 6, "purpose": "经人工评审后的局部状态表达。"},
    {"id": "stage3b", "name": "阶段3b · 扩大范围", "order": 7, "purpose": "仅在3a稳定后另行授权。"},
    {"id": "stage4", "name": "阶段4 · 目标与行动", "order": 8, "purpose": "统一提案、预算、等待和真实结果。"},
    {"id": "stage5", "name": "阶段5 · 持续自我", "order": 9, "purpose": "长期经历与反例下的可解释修订。"},
    {"id": "delivery", "name": "本次检测与页面", "order": 10, "purpose": "前置检测和清漪板块交付。"},
    {"id": "validation", "name": "贯穿验证", "order": 11, "purpose": "隔离、评测、故障和回退。"},
)

_IMPL = True
_PLAN = False

# id, title, stage, track, implementation, dev, acceptance, manual_review
_TASKS = (
    ("A01", "本地审计基线", "stage0", "evidence", True, [], [], False),
    ("A02", "记忆聚合幂等修复", "tonight", "memory", True, [], [], False),
    ("A03", "最终请求记忆清单", "stage0", "memory", True, ["A02"], ["A02"], False),
    ("A04", "按实际入选集合去重与预算", "stage0", "memory", True, ["A03"], ["A03"], False),
    ("A05", "稳定ID、时间与范围契约", "stage0", "evidence", True, [], ["A07"], False),
    ("A06", "权威写入入口盘点", "stage0", "evidence", True, ["A05"], ["A05"], False),
    ("A07", "隔离回归夹具", "stage0", "evidence", True, [], [], False),
    ("A08", "撤回与观察语义契约", "stage0", "evidence", True, ["A05", "A07"], ["A05", "A07"], False),

    ("M01", "情景、状态、信念与承诺准入", "stage1", "memory", _IMPL, ["A05", "A07"], ["A05", "A06", "A07"], False),
    ("M02", "逐命题原句来源", "stage1", "memory", _IMPL, ["M01", "A07"], ["M01", "A07"], False),
    ("M03", "人物与实体归一", "stage1", "memory", _IMPL, ["A05", "M02"], ["A05", "M02"], False),
    ("M04", "全库候选矛盾裁决", "stage1", "memory", _IMPL, ["M02", "M03"], ["M02", "M03"], False),
    ("M05", "派生来源失效传播", "stage1", "memory", _IMPL, ["A08", "M04"], ["A08", "M04"], False),
    ("M06", "单一认知写入与投影事务", "stage1", "memory", _IMPL, ["A06", "M02"], ["A06", "M02"], False),
    ("M07", "私聊原始轮次情景化", "stage1", "memory", _IMPL, ["M02", "M05", "M06"], ["M02", "M05", "M06"], False),
    ("M08", "有限巩固与晋升", "stage1", "memory", _IMPL, ["M04", "M05", "M07"], ["M04", "M05", "M07"], False),

    ("S01", "实时、历史、候选三轨迹口径", "stage2", "emotion", _IMPL, ["A01", "A08"], ["A01", "A08"], False),
    ("S02", "动力学与评估规则分别冻结", "stage2", "emotion", _IMPL, ["S01"], ["S01"], False),
    ("S03", "撤回、纠正和版本化情感重算", "stage2", "emotion", _IMPL, ["A08", "M05", "S02"], ["A08", "M05", "S02"], False),
    ("S04", "采集、标注、回放、表达控制分离", "stage2", "emotion", _IMPL, ["S01", "A06"], ["S01", "A06"], False),
    ("S05", "效价候选人工标注基准", "stage2", "emotion", _IMPL, ["A07", "S02"], ["A07", "S02"], True),
    ("S06", "迟到、重启、同刻与撤回回放矩阵", "stage2", "emotion", _IMPL, ["S03", "S05"], ["S03", "S05"], False),
    ("S07", "候选回放性能与脱敏读取", "stage2", "emotion", _IMPL, ["S03", "S04"], ["S03", "S04"], False),
    ("S08", "回忆影响情感的隔离实验", "stage3a", "emotion", _IMPL, ["S06", "R06"], ["S06", "R06"], True),

    ("R01", "多库统一只读检索对象", "ripple", "ripple", _IMPL, ["A05", "M01", "A07"], ["A04", "M01", "M05"], False),
    ("R02", "类型化稀疏联想边", "ripple", "ripple", _IMPL, ["R01", "M02"], ["R01", "M06"], False),
    ("R03", "实体、人物、时间与词法初召回", "ripple", "ripple", _IMPL, ["R01", "A07"], ["R01", "A04"], False),
    ("R04", "受限一跳与根源去重", "ripple", "ripple", _IMPL, ["R02", "R03", "M05"], ["R02", "R03"], False),
    ("R05", "缺失槽位与冲突判定", "ripple", "ripple", _IMPL, ["R04", "M04"], ["R04", "M04"], False),
    ("R06", "证据场景渲染和清单", "ripple", "ripple", _IMPL, ["R05", "A03"], ["R05", "A03"], False),
    ("R07", "关联记忆保留集消融", "ripple", "ripple", _IMPL, ["R06", "V02"], ["R06", "V02"], True),
    ("R08", "可选离线向量与二跳", "ripple", "ripple", _IMPL, ["R07"], ["R07"], True),

    ("C01", "阶段2.5自然样本收口", "stage25", "admission", _PLAN, ["A01", "A07"], ["A01", "A07"], False),
    ("C02", "阶段2.5工程证据重绑", "stage25", "admission", _PLAN, ["A01", "A07"], ["A01", "A07"], False),
    ("C03", "阶段3a人工评审包", "stage3a", "admission", _PLAN, ["C01", "C02", "V04", "V05"], ["C01", "C02", "R07", "V04", "V05"], True),
    ("C04", "工作状态快照与三层注入", "stage3a", "expression", _IMPL, ["M05", "M06", "R06"], ["M05", "M06", "R06"], False),
    ("C05", "ExpressionPlan隔离接线", "stage3a", "expression", _IMPL, ["C04", "S06"], ["C04", "S06"], False),
    ("C06", "阶段3a局部灰度", "stage3a", "admission", _IMPL, ["C03", "C05", "V05"], ["C03", "C05", "V05"], True),

    ("G01", "统一行动提案契约", "stage4", "goals", _IMPL, ["A05", "M06"], ["A05", "M06"], False),
    ("G02", "承诺与目标进入注意", "stage4", "goals", _IMPL, ["C04", "G01"], ["C04", "G01"], False),
    ("G03", "低风险检索整理草稿调度", "stage4", "goals", _IMPL, ["G01", "G02", "C06"], ["G01", "G02", "C06"], False),
    ("G04", "对外行动灰度与结果确认", "stage4", "goals", _IMPL, ["G03", "V05"], ["G03", "V05"], True),

    ("L01", "仅从真实新增来源反思", "stage5", "self", _IMPL, ["M08", "S06"], ["M08", "S06"], False),
    ("L02", "信念和自我认识的版本修订", "stage5", "self", _IMPL, ["L01", "M04", "M05"], ["L01", "M04", "M05"], False),
    ("L03", "习惯和关系长期更新", "stage5", "self", _IMPL, ["L02", "G03"], ["L02", "G03"], False),
    ("L04", "跨时间选择连续性验收", "stage5", "self", _IMPL, ["L03", "V06"], ["L03", "V06"], True),

    ("V01", "核心契约回归", "validation", "validation", _IMPL, ["A07", "M02", "M06"], ["A07", "M02", "M06"], False),
    ("V02", "按根源切分的记忆保留集", "validation", "validation", _IMPL, ["A07", "A05"], ["A07", "A05"], True),
    ("V03", "分层撤回与范围故障矩阵", "validation", "validation", _IMPL, ["M05", "S03", "R04"], ["M05", "S03", "R04"], False),
    ("V04", "四种运行模式观测隔离", "validation", "validation", _IMPL, ["S06", "R07"], ["S06", "R07"], False),
    ("V05", "发布前故障注入与回退", "validation", "validation", _IMPL, ["V01", "V03", "C04", "C05"], ["V01", "V03", "C04", "C05"], False),
    ("V06", "长期消融与回退复查", "validation", "validation", _IMPL, ["R07", "C06", "G03"], ["R07", "C06", "G03"], False),

    ("D01", "清漪阶段目录与依赖校验", "delivery", "detection", True, [], [], False),
    ("D02", "只读快照与schema检查", "delivery", "detection", True, ["D01"], ["D01"], False),
    ("D03", "统一前置检测内核与CLI", "delivery", "detection", True, ["D02"], ["D02"], False),
    ("D04", "固定隔离行为探针", "delivery", "detection", True, ["D03"], ["D03"], False),
    ("D05", "单活动检测任务与租约", "delivery", "detection", True, ["D03"], ["D03"], False),
    ("D06", "管理员检测API和脱敏报告", "delivery", "detection", True, ["D05"], ["D05"], False),
    ("D07", "检测器全模式验收", "delivery", "detection", _IMPL, ["D04", "D06"], ["D04", "D06"], False),

    ("U01", "清漪侧栏和子页骨架", "delivery", "ui", True, ["D01"], ["D01"], False),
    ("U02", "影子与准入UI整体迁移", "delivery", "ui", True, ["U01", "D02"], ["U01", "D02"], False),
    ("U03", "一键检测、轮询和报告下载", "delivery", "ui", True, ["D06", "U01"], ["D06", "U01"], False),
    ("U04", "阶段、前置和当前可推进视图", "delivery", "ui", True, ["D01", "D03", "U03"], ["D01", "D03", "U03"], False),
    ("U05", "算法与任务说明面板", "delivery", "ui", True, ["U04"], ["U04"], False),
    ("U06", "页面故障态与真实浏览器验收", "delivery", "ui", True, ["U02", "U05", "D07"], ["U02", "U05", "D07"], False),

    ("Q01", "对话主体归属与纠错状态", "stage1", "context", _IMPL, ["A05", "A08", "M02"], ["A05", "A08", "M02"], False),
    ("Q02", "主动记忆运行编排与真实注入", "ripple", "memory", _IMPL, ["R01", "R02", "R03", "R04", "R05", "R06"], ["R01", "R02", "R03", "R04", "R05", "R06"], False),
    ("Q03", "人设事实优先级与情感提示去冲突", "stage3a", "expression", _IMPL, ["Q01", "C06"], ["Q01", "C06"], False),
    ("Q04", "知识记忆分库与社区世界书版本治理", "stage1", "knowledge", _IMPL, ["M01", "M05", "R01"], ["M01", "M05", "R01"], False),
    ("Q05", "在线契约探针进程级写入隔离", "validation", "validation", _IMPL, ["A07", "V01"], ["A07", "V01"], False),
    ("Q06", "真实能力五层审计与回放面板", "delivery", "ui", _IMPL, ["Q01", "Q02", "Q04", "U05"], ["Q01", "Q02", "Q04", "U05"], False),
    ("Q07", "上下文记忆独立验收集", "validation", "validation", _IMPL, ["Q01", "Q02", "Q04", "Q05"], ["Q01", "Q02", "Q04", "Q05"], False),
)


def catalog():
    tasks = []
    for task_id, title, stage, track, implementation, dev, acc, manual in _TASKS:
        tasks.append({
            "id": task_id, "title": title, "stage": stage, "track": track,
            "implementation": bool(implementation),
            "dev_requires": list(dev),
            "acceptance_requires": list(acc),
            "release_requires": list(acc),
            "manual_review": bool(manual),
        })
    return tasks


def stage_catalog():
    return [dict(stage) for stage in STAGES]


def _all_dependencies(task):
    return set(task["dev_requires"]) | set(task["acceptance_requires"]) | set(task["release_requires"])


def validate_catalog(tasks=None):
    tasks = tasks if tasks is not None else catalog()
    ids = {task["id"] for task in tasks}
    known_stages = {stage["id"] for stage in STAGES}
    duplicates = sorted({task["id"] for task in tasks
                         if sum(1 for other in tasks if other["id"] == task["id"]) > 1})
    unknown_stages = sorted({task["stage"] for task in tasks if task["stage"] not in known_stages})
    missing = []
    graph = {}
    for task in tasks:
        dependencies = _all_dependencies(task)
        graph[task["id"]] = sorted(dependencies)
        missing.extend(sorted(dependencies - ids))
    seen, active, cycles = set(), set(), []

    def visit(node, path):
        if node in active:
            cycles.append(path + [node])
            return
        if node in seen:
            return
        active.add(node)
        for dependency in graph.get(node, []):
            if dependency in graph:
                visit(dependency, path + [node])
        active.remove(node)
        seen.add(node)

    for node in graph:
        visit(node, [])
    return {"ok": not missing and not cycles and not duplicates and not unknown_stages,
            "missing_dependencies": sorted(set(missing)),
            "duplicate_ids": duplicates,
            "unknown_stages": unknown_stages,
            "cycles": cycles}


def _dependency_state(value, field):
    if field == "implementation":
        return value in ("implemented", "existing")
    return value in ("pass", "not_required")


def _requires_organic(task):
    return bool(task["manual_review"] or task["stage"] in
                ("stage25", "stage3a", "stage3b", "stage4", "stage5"))


def _projected_value(task_id, field, projection, tasks):
    overlay = projection.get("tasks", {}).get(task_id, {})
    if field in overlay:
        return overlay[field]
    task = tasks[task_id]
    if field == "implementation":
        return "implemented" if task["implementation"] else "planned"
    if field == "engineering":
        return "not_verified"
    if field == "organic":
        return "not_observed" if _requires_organic(task) else "not_required"
    # authorization
    return "manual_review" if task["manual_review"] else "not_required"


def project_task(task_id, projection=None):
    projection = projection or {}
    tasks = {task["id"]: task for task in catalog()}
    if task_id not in tasks:
        return None
    task = dict(tasks[task_id])
    for field in ("implementation", "engineering", "organic", "authorization"):
        task[field] = _projected_value(task_id, field, projection, tasks)
    task["blocked_by"] = [dependency for dependency in task["dev_requires"]
                          if not _dependency_state(_projected_value(
                              dependency, "implementation", projection, tasks), "implementation")]
    task["dev_ready"] = (_dependency_state(task["implementation"], "implementation")
                         and not task["blocked_by"])
    task["release_ready"] = (
        _dependency_state(task["implementation"], "implementation")
        and all(_dependency_state(_projected_value(dependency, "implementation", projection, tasks),
                                  "implementation")
                for dependency in task["acceptance_requires"])
        and _dependency_state(task["engineering"], "engineering")
        and _dependency_state(task["organic"], "organic")
        and _dependency_state(task["authorization"], "authorization")
        and (not task["manual_review"] or task["authorization"] == "pass")
    )
    return task


def summary(tasks=None, projection=None):
    tasks = tasks or catalog()
    stages = {stage["id"]: {"pass": 0, "total": 0} for stage in STAGES}
    rows = []
    for task in tasks:
        projected = task
        if projection is not None:
            projected = project_task(task["id"], projection) or task
        value = copy.deepcopy(projected)
        stages.setdefault(task["stage"], {"pass": 0, "total": 0})
        stages[task["stage"]]["total"] += 1
        if value.get("release_ready"):
            stages[task["stage"]]["pass"] += 1
        rows.append(value)
    return {"stages": [dict(stage, **stages.get(stage["id"], {})) for stage in STAGES],
            "tasks": rows, "validation": validate_catalog(tasks)}
