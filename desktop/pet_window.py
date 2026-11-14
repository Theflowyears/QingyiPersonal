"""桌宠本体：透明置顶的清漪 + 头顶气泡。

透明靠 Qt 的 WA_TranslucentBackground（帧图本身带 alpha），不用 WebView2 那套 hack。
全屏应用避让：前台窗口几乎铺满整块屏幕时先躲开，别挡主人的游戏。
"""
from __future__ import annotations

import ctypes
import math
import os
import time

from PySide6 import QtCore, QtGui, QtWidgets

import sprites

FRAME_MS = 260
BUBBLE_SEC = 7.0
MAX_BUBBLE_LINES = 3


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def foreground_is_fullscreen() -> bool:
    """当前前台窗口是不是铺满屏幕（全屏游戏/放映）。测不准就当不是。"""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        rect = _Rect()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        monitor = user32.MonitorFromWindow(hwnd, 2)
        if not monitor:
            return False

        class _MonInfo(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rc", _Rect),
                        ("dwFlags", ctypes.c_ulong)]

        mi = _MonInfo()
        mi.cbSize = ctypes.sizeof(_MonInfo)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(mi)):
            return False
        w, h = rect.right - rect.left, rect.bottom - rect.top
        mw, mh = mi.rc.right - mi.rc.left, mi.rc.bottom - mi.rc.top
        return w >= mw * 0.97 and h >= mh * 0.97
    except Exception:
        return False


class PetWindow(QtWidgets.QWidget):
    open_panel = QtCore.Signal()
    clicked = QtCore.Signal()
    run_requested = QtCore.Signal()

    def __init__(self, store, parent=None):
        super().__init__(parent, QtCore.Qt.FramelessWindowHint
                         | QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.Tool)
        self.store = store
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self._cache: dict[tuple[str, int], list[QtGui.QPixmap]] = {}
        self._frames: list[QtGui.QPixmap] = []
        self._idx = 0
        self._phase = 0
        self._state = "idle"
        self._lines: list[str] = []
        self._until = 0.0
        self._press_at: tuple[int, int] | None = None
        self._moved = False
        self._hidden_for_fullscreen = False
        self._user_hidden = False
        self.skin = str(store["pet_skin"] or "fish")
        self._size = sprites.pick_size(int(store["pet_size"] or 245))
        first = self._pixmaps("idle")
        fw = first[0].width() if first else self._size
        fh = first[0].height() if first else self._size
        self._frame_h = fh
        self.setFixedSize(fw + 60, fh + 200)
        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.PreciseTimer)
        self._timer.timeout.connect(self._next_frame)
        self._timer.start(sprites.delay_ms("idle", self.skin))
        self._watch = QtCore.QTimer(self)
        self._watch.timeout.connect(self._avoid_fullscreen)
        self._watch.start(2500)
        self.set_state("idle")
        self._place_bottom_right()

    def reload_skin(self, skin: str | None = None) -> None:
        self.skin = skin or str(self.store["pet_skin"] or "fish")
        self._cache.clear()
        first = self._pixmaps("idle")
        if first:
            self._frame_h = first[0].height()
            self.setFixedSize(first[0].width() + 60, self._frame_h + 200)
        self.set_state("idle")
        self._place_bottom_right()

    # ---------- 位置 ----------
    def _screen_for_me(self):
        """跟着主人当前光标所在的那块屏幕走；拿不到就退回主屏。"""
        cur = QtWidgets.QApplication.screenAt(QtGui.QCursor.pos())
        return cur or QtWidgets.QApplication.primaryScreen()

    def _place_bottom_right(self):
        geo = self._screen_for_me().availableGeometry()
        self.move(geo.right() - self.width() - 24, geo.bottom() - self.height() - 16)
        self._clamp_to_screen()

    def _clamp_to_screen(self):
        """多屏虚拟桌面里最容易把宠物甩到屏幕外，落点每次都夹回来。"""
        geo = self._screen_for_me().availableGeometry()
        x = max(geo.left(), min(self.x(), geo.right() - self.width()))
        y = max(geo.top(), min(self.y(), geo.bottom() - self.height()))
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def _avoid_fullscreen(self):
        if self._user_hidden:
            return
        if os.environ.get("DESKTOP_NO_AVOID") == "1":
            if self._hidden_for_fullscreen:
                self._hidden_for_fullscreen = False
                self.show()
            return
        full = foreground_is_fullscreen()
        if full and not self._hidden_for_fullscreen:
            self._hidden_for_fullscreen = True
            self.hide()
        elif not full and self._hidden_for_fullscreen:
            self._hidden_for_fullscreen = False
            self.show()

    # ---------- 显隐（独立于面板，托盘也能切） ----------
    def hide_pet(self):
        self._user_hidden = True
        self._hidden_for_fullscreen = False
        self.hide()

    def show_pet(self):
        self._user_hidden = False
        self.show()
        self._avoid_fullscreen()

    def pet_visible(self) -> bool:
        return not self._user_hidden

    # ---------- 帧 ----------
    def _pixmaps(self, state: str) -> list[QtGui.QPixmap]:
        key = (state, self._size, self.skin)
        if key not in self._cache:
            self._cache[key] = [QtGui.QPixmap(str(p))
                                for p in sprites.frames(state, self._size, self.skin)]
            # 全帧抽取后一个状态两百多帧，12 态全缓存会吃掉上 GB；只留最近两个
            while len(self._cache) > 2:
                self._cache.pop(next(iter(self._cache)))
        return [p for p in self._cache[key] if not p.isNull()]

    def set_state(self, state: str):
        self._state = state
        self._frames = self._pixmaps(state)
        self._idx = 0
        self._timer.start(sprites.delay_ms(state, self.skin))
        self.update()

    def _next_frame(self):
        self._phase += 1
        if len(self._frames) > 1:
            self._idx = (self._idx + 1) % len(self._frames)
        self.update()

    # ---------- 说话 ----------
    def say(self, lines, state: str | None = None):
        if isinstance(lines, str):
            lines = [lines]
        self._lines = [str(x) for x in lines if str(x).strip()][:MAX_BUBBLE_LINES]
        self._until = time.time() + BUBBLE_SEC
        if state:
            self.set_state(state)
        self.update()

    def current_text(self) -> list[str]:
        return self._lines if time.time() < self._until else []

    # ---------- 画 ----------
    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        text = self.current_text()
        if not text and self._lines and time.time() >= self._until:
            self._lines = []
        pm = self._frames[self._idx % len(self._frames)] if self._frames else None
        if pm is not None:
            # 单帧动作靠呼吸/上下浮动活起来；多帧动作就按帧放，不叠变形
            still = len(self._frames) <= 1
            bob = math.sin(self._phase / 6.0) * 3.0 if still else 0.0
            breath = 1.0 + math.sin(self._phase / 7.0) * 0.012 if still else 1.0
            x = (self.width() - pm.width()) // 2
            y = self.height() - pm.height() + int(bob)
            if breath != 1.0:
                p.save()
                p.translate(self.width() / 2.0, float(self.height()))
                p.scale(breath, breath)
                p.translate(-self.width() / 2.0, -float(self.height()))
            if self._state == "sleep":
                p.setOpacity(0.55)
            p.drawPixmap(x, y, pm)
            p.setOpacity(1.0)
            if breath != 1.0:
                p.restore()
        if text:
            self._draw_bubble(p, text)

    def _draw_bubble(self, p: QtGui.QPainter, lines: list[str]):
        f = QtGui.QFont("Microsoft YaHei", 10)
        p.setFont(f)
        fm = QtGui.QFontMetrics(f)
        inner_w = max(fm.horizontalAdvance(ln) for ln in lines)
        inner_w = min(inner_w, self.width() - 34)
        pad_x, pad_y = 11, 9
        line_h = fm.height() + 2
        h = pad_y * 2 + line_h * len(lines)
        w = min(inner_w + pad_x * 2, self.width() - 12)
        x = (self.width() - w) // 2
        y = max(4, self.height() - self._frame_h - h - 34)
        path = QtGui.QPainterPath()
        path.addRoundedRect(QtCore.QRectF(x, y, w, h), 13, 13)
        tail = QtGui.QPainterPath()
        cx = self.width() / 2
        tail.moveTo(cx - 8, y + h - 1)
        tail.lineTo(cx, y + h + 12)
        tail.lineTo(cx + 8, y + h - 1)
        path = path.united(tail)
        p.setPen(QtGui.QPen(QtGui.QColor(56, 92, 78), 1))
        p.setBrush(QtGui.QColor(11, 17, 25, 235))
        p.drawPath(path)
        p.setPen(QtGui.QColor(215, 229, 242))
        ty = y + pad_y + fm.ascent()
        for ln in lines:
            p.drawText(QtCore.QRectF(x + pad_x, ty - fm.ascent(), w - pad_x * 2, line_h),
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, ln)
            ty += line_h

    # ---------- 交互 ----------
    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            self._press_at = (ev.globalPosition().x(), ev.globalPosition().y())
            self._moved = False

    def mouseMoveEvent(self, ev):
        if self._press_at is None:
            return
        dx = ev.globalPosition().x() - self._press_at[0]
        dy = ev.globalPosition().y() - self._press_at[1]
        if abs(dx) + abs(dy) > 4:
            self._moved = True
            self.move(self.pos() + QtCore.QPoint(int(dx), int(dy)))
            self._clamp_to_screen()
            self._press_at = (ev.globalPosition().x(), ev.globalPosition().y())

    def mouseReleaseEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton and not self._moved:
            self.clicked.emit()
        self._press_at = None

    def contextMenuEvent(self, ev):
        menu = QtWidgets.QMenu(self)
        act_panel = menu.addAction("打开面板")
        act_run = menu.addAction("现在清日常")
        act_quiet = menu.addAction("今天别吵")
        menu.addSeparator()
        act_hide = menu.addAction("隐藏清漪（后台还在）")
        act_quit = menu.addAction("退出")
        chosen = menu.exec(ev.globalPos())
        if chosen is act_panel:
            self.open_panel.emit()
        elif chosen is act_run:
            self.run_requested.emit()
        elif chosen is act_quiet:
            self.store.mute_today()
        elif chosen is act_hide:
            self.hide_pet()
        elif chosen is act_quit:
            QtWidgets.QApplication.quit()
