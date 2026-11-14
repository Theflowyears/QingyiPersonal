"""控制面板：PRTS／罗德岛终端风格 + DeepSeek 清漪元素。

分区编号、状态芯片、理智进度条、等高等宽卡片；配色沿用网页控制台 token 再叠
明日方舟的琥珀黄与 DeepSeek 的深蓝。外部来路（MAA 配置名、日志）全按纯文本填，
不拼 HTML。刷新只读后台缓存、按签名增量更新表格，不每轮整表重建。
关面板只是收起（closeEvent→hide），托盘和清漪继续在后台跑。
"""
from __future__ import annotations

import time

from PySide6 import QtCore, QtGui, QtWidgets

import sprites

QSS = """
QWidget{background:#060a12;color:#dbe7f3;font-family:'Microsoft YaHei','Segoe UI';font-size:13px}
QFrame[card="1"]{background:#0c1420;border:1px solid #1c2f42;border-left:3px solid #2a6f8f;border-radius:8px}
QFrame[head="1"]{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #0d1b2e,stop:0.55 #0a1424,stop:1 #060a12);border:1px solid #1c2f42;border-radius:10px}
QLabel[num="1"]{color:#ffb454;font-family:Consolas,monospace;font-size:15px;font-weight:700}
QLabel[kicker="1"]{color:#5f93b8;font-family:Consolas,monospace;font-size:10px;letter-spacing:2px}
QLabel[title="1"]{color:#eaf3fb;font-size:14px;font-weight:600}
QLabel[sub="1"]{color:#7488a0;font-size:11px}
QLabel[h="1"]{color:#3ee0ff;font-size:11px;letter-spacing:1.4px;font-weight:600}
QLabel[k="1"]{color:#7488a0}
QLabel[v="1"]{color:#dbe7f3}
QLabel[warn="1"]{color:#ffb454}
QLabel[bad="1"]{color:#ff5f6d}
QLabel[good="1"]{color:#35f0a0}
QLabel[chip="good"]{background:#0f2a1f;color:#35f0a0;border:1px solid #1d5c40;border-radius:9px;padding:2px 10px;font-size:11px}
QLabel[chip="warn"]{background:#2a2010;color:#ffb454;border:1px solid #6b4d1d;border-radius:9px;padding:2px 10px;font-size:11px}
QLabel[chip="bad"]{background:#2a1216;color:#ff5f6d;border:1px solid #6b2730;border-radius:9px;padding:2px 10px;font-size:11px}
QLabel[chip="idle"]{background:#101a26;color:#7488a0;border:1px solid #24384c;border-radius:9px;padding:2px 10px;font-size:11px}
QLabel[chip="info"]{background:#0e1f33;color:#3ee0ff;border:1px solid #1d4d6b;border-radius:9px;padding:2px 10px;font-size:11px}
QPushButton{background:#101c2b;color:#dbe7f3;border:1px solid #2a4560;border-radius:7px;padding:7px 15px;font-size:12px}
QPushButton:hover{border-color:#3ee0ff;color:#3ee0ff}
QPushButton:pressed{background:#0a1420}
QPushButton:disabled{color:#4f6379;border-color:#1c2f42;background:#0a121c}
QPushButton[primary="1"]{background:#12607f;border-color:#3ee0ff;color:#eaf9ff;font-weight:600}
QPushButton[primary="1"]:hover{background:#177a9e}
QPushButton[primary="1"]:disabled{background:#0e2230;border-color:#1c3a4a;color:#4f6379}
QPushButton[danger="1"]{border-color:#ff5f6d;color:#ff5f6d}
QPushButton[danger="1"]:hover{background:#2a1216}
QComboBox{background:#101c2b;border:1px solid #2a4560;border-radius:6px;padding:5px 10px;color:#dbe7f3}
QComboBox:hover{border-color:#3ee0ff}
QComboBox QAbstractItemView{background:#0c1420;border:1px solid #2a4560;selection-background-color:#12607f;color:#dbe7f3}
QTableWidget{background:#0a121c;border:1px solid #1c2f42;gridline-color:#16283a;color:#dbe7f3;alternate-background-color:#0c1826}
QTableWidget::item{padding:4px 6px}
QHeaderView::section{background:#101c2b;color:#5f93b8;border:0;border-bottom:1px solid #2a4560;padding:6px 8px;font-family:Consolas,monospace;font-size:11px}
QPlainTextEdit{background:#070d15;border:1px solid #1c2f42;border-radius:7px;color:#9fb3c8;font-family:Consolas,monospace;font-size:11.5px}
QLineEdit{background:#0a121c;border:1px solid #1c2f42;border-radius:6px;padding:6px 9px;color:#dbe7f3}
QLineEdit:focus{border-color:#3ee0ff}
QProgressBar{background:#0a121c;border:1px solid #1c2f42;border-radius:6px;height:18px;text-align:center;color:#dbe7f3;font-size:11px}
QProgressBar::chunk{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #12607f,stop:1 #3ee0ff);border-radius:5px}
QScrollArea{border:0;background:transparent}
QScrollBar:vertical{background:#070d15;width:10px}
QScrollBar::handle:vertical{background:#2a4560;border-radius:5px;min-height:24px}
QScrollBar::handle:vertical:hover{background:#3a5a78}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0}
QMenu{background:#0c1420;border:1px solid #2a4560;color:#dbe7f3}
QMenu::item:selected{background:#12607f}
"""

_TIER_CN = {"auto": "自动", "confirm": "要点", "manual": "只列"}
_PROV_CN = {"confirmed": "继承", "inferred": "推测", "default": "默认"}


def _chip(text: str, kind: str) -> QtWidgets.QLabel:
    lab = QtWidgets.QLabel(text)
    lab.setProperty("chip", kind)
    return lab


def _card(num: str, kicker: str, title: str, sub: str | None = None
          ) -> tuple[QtWidgets.QFrame, QtWidgets.QVBoxLayout]:
    f = QtWidgets.QFrame()
    f.setProperty("card", "1")
    lay = QtWidgets.QVBoxLayout(f)
    lay.setContentsMargins(16, 12, 16, 14)
    lay.setSpacing(9)
    head = QtWidgets.QHBoxLayout()
    head.setSpacing(9)
    n = QtWidgets.QLabel(num)
    n.setProperty("num", "1")
    head.addWidget(n)
    k = QtWidgets.QLabel(kicker)
    k.setProperty("kicker", "1")
    head.addWidget(k)
    head.addStretch(1)
    lay.addLayout(head)
    t = QtWidgets.QLabel(title)
    t.setProperty("title", "1")
    lay.addWidget(t)
    if sub:
        s = QtWidgets.QLabel(sub)
        s.setProperty("sub", "1")
        s.setWordWrap(True)
        lay.addWidget(s)
    return f, lay


def _table(headers: list[str], height: int) -> QtWidgets.QTableWidget:
    t = QtWidgets.QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(26)
    t.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    t.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
    t.setAlternatingRowColors(True)
    t.horizontalHeader().setStretchLastSection(True)
    t.setFixedHeight(height)
    return t


def _set_rows(tbl, rows, color=None) -> None:
    """按签名增量填表：内容没变就不重建 item，免得每轮刷新都闪一下、滚回顶。"""
    sig = tuple(tuple(str(c) for c in r) for r in rows)
    if getattr(tbl, "_sig", None) == sig:
        return
    tbl._sig = sig
    tbl.setRowCount(len(rows))
    for i, r in enumerate(rows):
        for col, text in enumerate(r):
            item = QtWidgets.QTableWidgetItem(str(text))
            if color is not None:
                color(item, i, col, r)
            tbl.setItem(i, col, item)


class PanelWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl):
        super().__init__()
        self.ctrl = ctrl
        self.setWindowTitle("清漪 · 桌面日常 — PRTS")
        self.resize(1000, 800)
        self.setMinimumWidth(880)
        self.setStyleSheet(QSS)
        self._build()
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1200)
        self.refresh()

    def closeEvent(self, ev):
        # 关面板只是收起，托盘和清漪继续在后台跑；退出走托盘菜单
        self.hide()
        ev.ignore()

    # ---------- 结构 ----------
    def _build(self):
        root = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(12)

        # 顶栏：清漪徽标 + PRTS 终端字样 + 状态芯片
        head = QtWidgets.QFrame()
        head.setProperty("head", "1")
        hl = QtWidgets.QHBoxLayout(head)
        hl.setContentsMargins(16, 12, 16, 12)
        hl.setSpacing(14)
        logo = QtWidgets.QLabel()
        logo.setFixedSize(50, 50)
        logo.setAlignment(QtCore.Qt.AlignCenter)
        frames = sprites.frames("idle", skin=str(self.ctrl.store["pet_skin"] or "fish"))
        if frames:
            logo.setPixmap(QtGui.QPixmap(str(frames[0])).scaledToHeight(
                48, QtCore.Qt.SmoothTransformation))
        hl.addWidget(logo)
        tb = QtWidgets.QVBoxLayout()
        tb.setSpacing(3)
        title = QtWidgets.QLabel("清漪 · 桌面日常")
        title.setStyleSheet("font-size:19px;font-weight:700;color:#eaf3fb;letter-spacing:.5px")
        kicker = QtWidgets.QLabel("RHODES ISLAND ／ PRTS TERMINAL")
        kicker.setProperty("kicker", "1")
        tb.addWidget(title)
        tb.addWidget(kicker)
        hl.addLayout(tb)
        hl.addStretch(1)
        right = QtWidgets.QVBoxLayout()
        right.setSpacing(5)
        self.chip = _chip("自检中", "idle")
        right.addWidget(self.chip, 0, QtCore.Qt.AlignRight)
        tag = QtWidgets.QLabel("本地运行 · 不上云")
        tag.setProperty("sub", "1")
        right.addWidget(tag, 0, QtCore.Qt.AlignRight)
        hl.addLayout(right)
        outer.addWidget(head)
        stripe = QtWidgets.QFrame()
        stripe.setFixedHeight(2)
        stripe.setStyleSheet("background:#ffb454;border:0")
        outer.addWidget(stripe)

        # 01 环境自检
        env_card, env_lay = _card(
            "01", "ENV-CHECK", "环境自检",
            "读你的 MAA 安装与连接配置；设备闸门后台每 8 秒查一次，这里只显示最近一次结果。")
        self.status = QtWidgets.QLabel("—")
        self.status.setProperty("v", "1")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-size:14px")
        env_lay.addWidget(self.status)
        self.env = QtWidgets.QLabel("环境还没读")
        self.env.setProperty("k", "1")
        self.env.setWordWrap(True)
        env_lay.addWidget(self.env)
        self.probe_at = QtWidgets.QLabel("")
        self.probe_at.setProperty("sub", "1")
        env_lay.addWidget(self.probe_at)
        outer.addWidget(env_card)

        # 02 今日套餐
        plan_card, plan_lay = _card(
            "02", "DAILY-ORDER", "今日套餐",
            "参数继承自你的 MAA 配置；标「推测」的是映射推断。点「确认这套套餐」即授权会花资源的任务。")
        self.plan_tbl = _table(["任务", "说明", "参数", "来源", "档位"], 188)
        plan_lay.addWidget(self.plan_tbl)
        diff_row = QtWidgets.QHBoxLayout()
        self.diff = QtWidgets.QLabel("和上次点头的没有差别")
        self.diff.setProperty("k", "1")
        self.diff.setWordWrap(True)
        diff_row.addWidget(self.diff, 1)
        self.btn_approve = QtWidgets.QPushButton("确认这套套餐")
        self.btn_approve.setProperty("primary", "1")
        self.btn_approve.clicked.connect(self._approve)
        diff_row.addWidget(self.btn_approve)
        plan_lay.addLayout(diff_row)
        outer.addWidget(plan_card)

        # 03 跑单执行
        run_card, run_lay = _card(
            "03", "RUN-EXEC", "跑单执行",
            "进度只认 MaaCore 回调；局数与理智都是它报的原值，不是我估的。")
        gates_row = QtWidgets.QHBoxLayout()
        self.gates_chip = _chip("还没查", "idle")
        gates_row.addWidget(self.gates_chip)
        self.gates = QtWidgets.QLabel("")
        self.gates.setProperty("k", "1")
        self.gates.setWordWrap(True)
        gates_row.addWidget(self.gates, 1)
        run_lay.addLayout(gates_row)
        prog_row = QtWidgets.QHBoxLayout()
        sl = QtWidgets.QLabel("理智 SANITY")
        sl.setProperty("kicker", "1")
        prog_row.addWidget(sl)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("还没有跑单")
        prog_row.addWidget(self.progress, 1)
        run_lay.addLayout(prog_row)
        self.run_tbl = _table(["任务", "状态", "局数", "理智", "耗时"], 158)
        run_lay.addWidget(self.run_tbl)
        btns = QtWidgets.QHBoxLayout()
        self.btn_run = QtWidgets.QPushButton("开始跑")
        self.btn_run.setProperty("primary", "1")
        self.btn_run.clicked.connect(lambda: self.ctrl.start_run())
        self.btn_stop = QtWidgets.QPushButton("停止")
        self.btn_stop.setProperty("danger", "1")
        self.btn_stop.clicked.connect(lambda: self.ctrl.stop_run())
        self.btn_quiet = QtWidgets.QPushButton("今天别吵")
        self.btn_quiet.clicked.connect(self._quiet)
        for b in (self.btn_run, self.btn_stop, self.btn_quiet):
            btns.addWidget(b)
        self.run_state = QtWidgets.QLabel("")
        self.run_state.setProperty("k", "1")
        self.run_state.setWordWrap(True)
        btns.addWidget(self.run_state, 1)
        run_lay.addLayout(btns)
        outer.addWidget(run_card)

        # 04 账本 + 05 日志（等高）
        bottom = QtWidgets.QHBoxLayout()
        bottom.setSpacing(12)
        led_card, led_lay = _card("04", "LEDGER", "账本", "每一单的开始/收尾/中断都落盘。")
        self.led_tbl = _table(["时间", "结果", "为什么"], 230)
        led_lay.addWidget(self.led_tbl)
        bottom.addWidget(led_card, 3)
        log_card, log_lay = _card("05", "LOG", "日志", "后台每一步都出声，沉默视为故障。")
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(230)
        log_lay.addWidget(self.log)
        bottom.addWidget(log_card, 2)
        outer.addLayout(bottom)

        # 06 路径与连接
        set_card, set_lay = _card(
            "06", "CONFIG", "路径与连接",
            "改完点「保存并重新发现」；只写本地 state，不碰你的 MAA 配置。")
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(7)
        self.ed = {}
        for row, (key, label) in enumerate((("maa_dir", "MAA 目录"),
                                            ("emulator_path", "模拟器目录"),
                                            ("adb_path", "adb.exe"),
                                            ("address", "地址"))):
            lab = QtWidgets.QLabel(label)
            lab.setProperty("k", "1")
            grid.addWidget(lab, row, 0)
            e = QtWidgets.QLineEdit()
            e.setText(str(self.ctrl.store[key] or ""))
            grid.addWidget(e, row, 1)
            self.ed[key] = e
        grid.setColumnStretch(1, 1)
        set_lay.addLayout(grid)
        srow = QtWidgets.QHBoxLayout()
        btn_save = QtWidgets.QPushButton("保存并重新发现")
        btn_save.setProperty("primary", "1")
        btn_save.clicked.connect(self._save_settings)
        srow.addWidget(btn_save)
        srow.addWidget(QtWidgets.QLabel("形象"))
        self.skin_box = QtWidgets.QComboBox()
        self.skin_box.addItems(["fish", "maid"])
        self.skin_box.setCurrentText(str(self.ctrl.store["pet_skin"] or "fish"))
        self.skin_box.currentTextChanged.connect(lambda s: self.ctrl.set_skin(s))
        srow.addWidget(self.skin_box)
        self.say_what = QtWidgets.QLabel("")
        self.say_what.setProperty("k", "1")
        srow.addWidget(self.say_what, 1)
        set_lay.addLayout(srow)
        prev = QtWidgets.QHBoxLayout()
        prev.setSpacing(6)
        pl = QtWidgets.QLabel("动作预览")
        pl.setProperty("k", "1")
        prev.addWidget(pl)
        for state in sprites.FISH_STATES:
            b = QtWidgets.QPushButton(state)
            b.setStyleSheet("padding:5px 9px;font-size:11px")
            b.clicked.connect(lambda _c=False, s=state: self.ctrl.preview_state(s))
            prev.addWidget(b)
        prev.addStretch(1)
        set_lay.addLayout(prev)
        outer.addWidget(set_card)

        foot = QtWidgets.QLabel("清漪 × MAA · PRTS 本地终端 · 素材仅供本机个人使用")
        foot.setProperty("sub", "1")
        foot.setAlignment(QtCore.Qt.AlignCenter)
        outer.addWidget(foot)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(root)
        self.setCentralWidget(scroll)

    # ---------- 动作 ----------
    def _approve(self):
        fp = self.ctrl.approve()
        self.say_what.setText(f"已确认套餐 {fp}")

    def _quiet(self):
        self.ctrl.mute_today()
        self.say_what.setText("今天不再主动开口，跑单结果照样汇报")

    def _save_settings(self):
        self.ctrl.save_settings({k: e.text().strip() for k, e in self.ed.items()})
        self.refresh()

    # ---------- 刷新 ----------
    @staticmethod
    def _set_text(lab, text: str) -> None:
        if lab.text() != text:
            lab.setText(text)

    def refresh(self):
        try:
            st = self.ctrl.status()
        except Exception as exc:
            self.status.setText(f"状态读不出来：{type(exc).__name__}: {exc}")
            return
        self._set_text(self.status, st.get("headline", "—"))
        self._set_text(self.env, " · ".join(st.get("env") or []))
        pa = st.get("probe_at") or 0
        self._set_text(self.probe_at,
                       ("设备闸门最近自查：" + time.strftime("%H:%M:%S", time.localtime(pa)))
                       if pa else "设备闸门还没查完")
        self._fill_chip(st)
        self._fill_plan(st)
        self._fill_run(st)
        self._fill_ledger(st)
        self._fill_log(st)

    def _fill_chip(self, st):
        snap = st.get("run") or {}
        state = snap.get("state")
        blocked = st.get("blocked") or []
        if state == "stalled":
            kind, text = "warn", "卡住"
        elif state == "running":
            kind, text = "info", "跑单中"
        elif state == "done":
            kind, text = "good", "上一单完成"
        elif blocked:
            kind, text = "bad", "闸门拦下"
        else:
            kind, text = "idle", "待机"
        if self.chip.text() != text or self.chip.property("chip") != kind:
            self.chip.setText(text)
            self.chip.setProperty("chip", kind)
            self.chip.style().polish(self.chip)

    def _fill_plan(self, st):
        rows = []
        for r in st.get("intents") or []:
            params = "，".join(f"{k}={v}" for k, v in sorted(r["params"].items())
                              if k != "facility") or "（无参数）"
            fac = r["params"].get("facility")
            if fac:
                params += f" ｜ 房间 {len(fac)} 间"
            prov = "、".join(sorted({_PROV_CN.get(v, v) for v in r["prov"].values()}))
            rows.append((r["label"], r["note"], params, prov,
                         _TIER_CN.get(r["tier"], r["tier"])))

        def color(item, i, col, r):
            if col == 4 and r[4] != "自动":
                item.setForeground(QtGui.QBrush(QtGui.QColor("#ffb454")))
            if col == 3 and "推测" in r[3]:
                item.setForeground(QtGui.QBrush(QtGui.QColor("#9d7bff")))

        _set_rows(self.plan_tbl, rows, color)
        changed = st.get("changed") or []
        if changed:
            self._set_text(self.diff, "和上次点头的有差别：" + "；".join(changed))
            self.diff.setProperty("warn", "1")
        else:
            self._set_text(self.diff, "和上次点头的没有差别（套餐 " + str(st.get("fp", "")) + "）")
            self.diff.setProperty("k", "1")
        self.diff.style().polish(self.diff)

    def _fill_run(self, st):
        blocked = st.get("blocked") or []
        if blocked:
            self._set_text(self.gates, "闸门拦下：" + "；".join(blocked))
            kind, ctext = "bad", "拦下"
        else:
            self._set_text(self.gates, "闸门全过，可以开工。")
            kind, ctext = "good", "全过"
        if self.gates_chip.property("chip") != kind:
            self.gates_chip.setText(ctext)
            self.gates_chip.setProperty("chip", kind)
            self.gates_chip.style().polish(self.gates_chip)
        snap = st.get("run") or {"state": "idle"}
        chains = snap.get("chains") or []
        rows = []
        for c in chains:
            sanity = "—"
            if c.get("sanity_before") is not None or c.get("sanity_after") is not None:
                sanity = f"{c.get('sanity_before')}→{c.get('sanity_after')}"
            rows.append((c.get("task"), c.get("state"), c.get("times_finished") or "—",
                         sanity, f"{c.get('seconds')}s" if c.get("seconds") else "—"))

        def color(item, i, col, r):
            if col == 1:
                item.setForeground(QtGui.QBrush(QtGui.QColor(
                    {"done": "#35f0a0", "running": "#3ee0ff", "error": "#ff5f6d",
                     "stopped": "#ffb454"}.get(r[1], "#7488a0"))))

        _set_rows(self.run_tbl, rows, color)
        total = len(chains)
        done = sum(1 for c in chains if c.get("state") == "done")
        if self.progress.maximum() != max(total, 1):
            self.progress.setRange(0, max(total, 1))
        if self.progress.value() != done:
            self.progress.setValue(done)
        self.progress.setFormat(f"{done} / {total} 条完成" if total else "还没有跑单")
        self._set_text(self.run_state, f"这单 {snap.get('state')} " +
                       ("；".join(snap.get("notes") or [])))
        running = snap.get("state") in ("running", "stalled")
        self.btn_run.setEnabled(not running and not blocked)
        self.btn_stop.setEnabled(running)

    def _fill_ledger(self, st):
        rows = []
        for r in st.get("ledger") or []:
            t = time.strftime("%m-%d %H:%M", time.localtime(float(r.get("at") or 0)))
            rows.append((t, r.get("event"), r.get("why") or r.get("state") or ""))

        def color(item, i, col, r):
            if col == 1:
                ev = r[1]
                c = ("#35f0a0" if ev == "run_finished"
                     else "#ffb454" if ev == "run_interrupted" else "#3ee0ff")
                item.setForeground(QtGui.QBrush(QtGui.QColor(c)))

        _set_rows(self.led_tbl, rows, color)

    def _fill_log(self, st):
        lines = st.get("logs") or []
        joined = "\n".join(lines[-220:])
        if self.log.toPlainText() != joined:
            self.log.setPlainText(joined)
            sb = self.log.verticalScrollBar()
            sb.setValue(sb.maximum())
