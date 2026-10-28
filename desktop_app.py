#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
作业备忘 · Windows 桌面应用

- 内嵌本地服务（127.0.0.1 随机端口），不打开浏览器。
- 主窗口使用 WebView2 渲染现有界面。
- 关闭主窗口不退出：主窗隐藏，桌面出现 80×80 的透明悬浮球（置顶、可拖拽、带未完成角标）。
- 复用 homework_server.py 的加密凭据、深大登录和作业接口。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import webview

import homework_server as hs
from ball_window import BallWindow
from shrink_anim import ShrinkAnimation


APP_TITLE = "作业备忘 · 深大优课"
BALL_TITLE = "作业备忘 · 悬浮球"
TRAY_TEXT = "作业备忘 · 深大优课"
APP_VERSION = hs.APP_VERSION
# 开发时可以加 HOMEWORK_DESK_MUTEX_SUFFIX=dev，让自己起的实例和已安装版并存。
MUTEX_NAME = "Local\\HomeworkDeskDesktopApp" + (os.environ.get("HOMEWORK_DESK_MUTEX_SUFFIX") or "")
SHOW_MAIN_EVENT = "Local\\HomeworkDeskShowMain" + (os.environ.get("HOMEWORK_DESK_MUTEX_SUFFIX") or "")
EVENT_MODIFY_STATE = 0x0002
MAIN_WIDTH = 1180
MAIN_HEIGHT = 830
# 和 create_window(min_size=...) 保持一致，手动缩放时不得小于它。
WINDOW_MIN_WIDTH = 900
WINDOW_MIN_HEIGHT = 620
BALL_SIZE = 80
BALL_URGENT_SECONDS = 48 * 3600
BALL_CARD_ROWS = 3
BALL_STATE_FILE = "ball-state.json"
# 主窗口过渡动画时长，必须和 homework-reminder.html 里的 DURATION 对上。
MAIN_ANIM_ENTER_MS = 280
MAIN_ANIM_LEAVE_MS = 180
# 收起主窗口时"吸进悬浮球"的收缩动画时长（秒）。
MAIN_SHRINK_SECONDS = 0.28
# 悬浮球是原生分层窗口（见 ball_window.py），建好即可直接定位；
# 这里保留一个屏幕外的兜底坐标，避免任何情况下在桌面闪一下。
BALL_PARKING_POSITION = (-BALL_SIZE - 400, -BALL_SIZE - 400)

# ---------------------------------------------------------------------------
# 无边框窗口
# ---------------------------------------------------------------------------
WM_NCLBUTTONDOWN = 0x00A1
VK_LBUTTON = 0x01

HT_LEFT = 10
HT_RIGHT = 11
HT_TOP = 12
HT_TOPLEFT = 13
HT_TOPRIGHT = 14
HT_BOTTOM = 15
HT_BOTTOMLEFT = 16
HT_BOTTOMRIGHT = 17

RESIZE_EDGES = {
    "left": HT_LEFT,
    "right": HT_RIGHT,
    "top": HT_TOP,
    "top-left": HT_TOPLEFT,
    "top-right": HT_TOPRIGHT,
    "bottom": HT_BOTTOM,
    "bottom-left": HT_BOTTOMLEFT,
    "bottom-right": HT_BOTTOMRIGHT,
}

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2
DWMWA_COLOR_NONE = 0xFFFFFFFE
# COLORREF = 0x00BBGGRR，对应界面里的 #e4dfd4（暖米色描边）
FRAME_BORDER_COLOR = 0x00D4DFE4


def window_hwnd(window: "webview.Window | None") -> int | None:
    """取窗口的原生句柄；窗口还没创建好时返回 None。"""
    if window is None:
        return None
    # 原生悬浮球直接暴露 hwnd
    hwnd = getattr(window, "hwnd", None)
    if isinstance(hwnd, int) and hwnd:
        return hwnd
    try:
        native = getattr(window, "native", None)
        if native is None:
            return None
        return int(native.Handle.ToInt64())
    except Exception:
        return None


def dwm_set_attribute(hwnd: int, attribute: int, value: int) -> None:
    try:
        data = ctypes.c_int(value)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data)
        )
    except Exception:
        pass


def apply_window_frame(window: "webview.Window | None", *, maximized: bool) -> None:
    """Win11 圆角 + 细描边；最大化时还原成直角，避免切掉屏幕边缘。"""
    hwnd = window_hwnd(window)
    if not hwnd:
        return
    if maximized:
        dwm_set_attribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_DONOTROUND)
        dwm_set_attribute(hwnd, DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE)
    else:
        dwm_set_attribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
        dwm_set_attribute(hwnd, DWMWA_BORDER_COLOR, FRAME_BORDER_COLOR)


MONITOR_DEFAULTTONEAREST = 2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
    ]


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wt.RECT()
    try:
        ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    except Exception:
        return 0, 0, BALL_SIZE, BALL_SIZE
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def work_area_for_window(hwnd: int) -> tuple[int, int, int, int]:
    """返回窗口所在显示器的工作区（已排除任务栏）。"""
    try:
        user32 = ctypes.windll.user32
        monitor = user32.MonitorFromWindow(ctypes.c_void_p(hwnd), MONITOR_DEFAULTTONEAREST)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if monitor and user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
            rc = info.rcWork
            return int(rc.left), int(rc.top), int(rc.right), int(rc.bottom)
    except Exception:
        pass
    user32 = ctypes.windll.user32
    return 0, 0, int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))


def primary_work_area() -> tuple[int, int, int, int]:
    """主显示器工作区（已排除任务栏）。"""
    rect = wt.RECT()
    try:
        if ctypes.windll.user32.SystemParametersInfoW(
            0x0030, 0, ctypes.byref(rect), 0
        ):
            return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        pass
    user32 = ctypes.windll.user32
    return 0, 0, int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))


def default_ball_position() -> tuple[int, int]:
    """没有保存位置时，把悬浮球放在主屏右上角偏下。"""
    left, top, right, bottom = primary_work_area()
    x = right - BALL_SIZE - 24
    y = top + max(24, (bottom - top) // 5)
    return x, y


def clamp_ball_position(hwnd: int, x: int, y: int) -> tuple[int, int]:
    left, top, right, bottom = work_area_for_window(hwnd)
    x = max(left + 2, min(int(x), right - BALL_SIZE - 2))
    y = max(top + 2, min(int(y), bottom - BALL_SIZE - 2))
    return x, y


def move_window_to(hwnd: int, x: int, y: int) -> tuple[int, int]:
    x, y = clamp_ball_position(hwnd, x, y)
    try:
        ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(hwnd),
            None,
            x,
            y,
            0,
            0,
            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
        )
    except Exception:
        pass
    return x, y


def dpi_scale_for_window(hwnd: int) -> float:
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(hwnd))
        if dpi:
            return float(dpi) / 96.0
    except Exception:
        pass
    return 1.0


def fit_maximized_bounds(window: "webview.Window | None") -> None:
    """无边框窗口最大化时默认会盖住任务栏，这里限制到工作区。"""
    if window is None:
        return
    try:
        from System import Action  # pythonnet
        import clr

        clr.AddReference("System.Windows.Forms")
        import System.Windows.Forms as WinForms

        native = window.native
        if native is None:
            return

        def _apply() -> None:
            try:
                native.MaximizedBounds = WinForms.Screen.FromControl(native).WorkingArea
            except Exception:
                pass

        native.Invoke(Action(_apply))
    except Exception:
        logging.getLogger(hs.APP_NAME).debug("设置最大化边界失败", exc_info=True)


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def acquire_single_instance():
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    already_running = kernel32.GetLastError() == 183
    return handle, already_running


def signal_existing_instance() -> bool:
    """通知已经运行的实例把主窗口显示出来。"""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenEventW.restype = ctypes.c_void_p
        kernel32.OpenEventW.argtypes = [wt.DWORD, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.SetEvent.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, SHOW_MAIN_EVENT)
        if not handle:
            return False
        try:
            return bool(kernel32.SetEvent(handle))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def show_message(text: str, title: str = APP_TITLE, flags: int = 0x40) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 托盘菜单（自绘）
# ---------------------------------------------------------------------------
TRAY_MENU_WIDTH = 218
TRAY_MENU_PAD = 6
TRAY_MENU_HEADER_H = 28
TRAY_MENU_SEP_H = 11
TRAY_MENU_ITEM_H = 38
TRAY_MENU_ICON = 18

TRAY_COLORS = {
    "bg": (251, 252, 249),
    "hover": (232, 241, 235),
    "text": (32, 44, 38),
    "muted": (138, 146, 139),
    "accent": (47, 91, 76),
    "danger": (178, 66, 60),
    "sep": (231, 235, 227),
    "border": (222, 226, 217),
}


class TrayMenuPopup:
    """自绘的托盘右键菜单。

    后端是 WinForms，但 pythonnet 在这个运行时里不会回调重写过的虚拟方法，
    ToolStrip 那套 ``OnRender*`` 换肤用不了，所以菜单改成
    「无边框 Form + 用 GDI+ 自己画」，交互全部用事件委托绑定。

    rows 支持两种写法：

    - ``("sep",)``：分隔线
    - ``("item", 文本, 图标名, 危险色, 加粗, 回调)``：可点击项
    """

    def __init__(self, rows, version: str, log: logging.Logger):
        import clr

        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        import System.Windows.Forms as WinForms
        from System.Drawing import Color, Font, FontStyle, Size

        self._rows = rows
        self._version = version
        self._log = log
        self._wf = WinForms
        self._color = Color
        self._hover = -1
        self._opened_at = 0.0
        self._closed = False

        form = WinForms.Form()
        form.FormBorderStyle = WinForms.FormBorderStyle(0)  # None
        form.ShowInTaskbar = False
        form.TopMost = True
        form.StartPosition = WinForms.FormStartPosition.Manual
        form.BackColor = Color.FromArgb(*TRAY_COLORS["bg"])
        form.AutoScaleMode = WinForms.AutoScaleMode(0)  # None
        form.KeyPreview = True
        form.Text = "作业备忘菜单"
        self._form = form

        try:
            _ = form.Handle  # 先建句柄，才能读 DeviceDpi
            self._scale = float(form.DeviceDpi) / 96.0
        except Exception:
            self._scale = 1.0

        self._font_header = Font("Microsoft YaHei UI", 8.25, FontStyle.Regular)
        self._font_item = Font("Microsoft YaHei UI", 9.0, FontStyle.Regular)
        self._font_item_bold = Font("Microsoft YaHei UI", 9.0, FontStyle.Bold)

        s = self._scale
        self._width = int(TRAY_MENU_WIDTH * s)
        height = TRAY_MENU_PAD * 2 + TRAY_MENU_HEADER_H
        for row in rows:
            height += TRAY_MENU_SEP_H if row[0] == "sep" else TRAY_MENU_ITEM_H
        self._height = int(height * s)
        form.Size = Size(self._width, self._height)

        self._enable_double_buffer()

        form.Paint += self._on_paint
        form.MouseMove += self._on_mouse_move
        form.MouseLeave += self._on_mouse_leave
        form.MouseUp += self._on_mouse_up
        form.Deactivate += self._on_deactivate
        form.KeyDown += self._on_key_down

    # -- 基础设置 ------------------------------------------------------
    def _enable_double_buffer(self) -> None:
        try:
            import System.Reflection as Reflection

            prop = self._form.GetType().GetProperty(
                "DoubleBuffered",
                Reflection.BindingFlags.Instance | Reflection.BindingFlags.NonPublic,
            )
            prop.SetValue(self._form, True)
        except Exception:
            self._log.debug("开启菜单双缓冲失败", exc_info=True)

    def _apply_window_style(self) -> None:
        """Win11 圆角 + 不进 Alt+Tab。"""
        try:
            hwnd = self._form.Handle.ToInt64()
            user32 = ctypes.windll.user32
            gwl_exstyle = -20
            ws_ex_toolwindow = 0x00000080
            ex_style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), gwl_exstyle)
            user32.SetWindowLongW(
                ctypes.c_void_p(hwnd), gwl_exstyle, ex_style | ws_ex_toolwindow
            )
            corner = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), 33, ctypes.byref(corner), ctypes.sizeof(corner)
            )
        except Exception:
            self._log.debug("设置菜单窗口样式失败", exc_info=True)

    # -- 绘制 ----------------------------------------------------------
    def _rounded(self, g, x, y, w, h, r):
        from System.Drawing.Drawing2D import GraphicsPath

        path = GraphicsPath()
        d = r * 2.0
        path.AddArc(float(x), float(y), float(d), float(d), 180.0, 90.0)
        path.AddArc(float(x + w - d), float(y), float(d), float(d), 270.0, 90.0)
        path.AddArc(float(x + w - d), float(y + h - d), float(d), float(d), 0.0, 90.0)
        path.AddArc(float(x), float(y + h - d), float(d), float(d), 90.0, 90.0)
        path.CloseFigure()
        return path

    def _draw_icon(self, g, kind, x, y, s):
        from System.Drawing import Pen, SolidBrush
        from System.Drawing.Drawing2D import LineCap

        color = self._color.FromArgb(
            *(TRAY_COLORS["danger"] if kind == "quit" else TRAY_COLORS["accent"])
        )
        if kind == "open":
            pen = Pen(color, 1.5 * s)
            path = self._rounded(g, x + 1.5 * s, y + 3.0 * s, 15.0 * s, 12.0 * s, 2.5 * s)
            g.DrawPath(pen, path)
            path.Dispose()
            pen.Dispose()
            line = Pen(color, 1.2 * s)
            g.DrawLine(line, x + 2.0 * s, y + 7.2 * s, x + 16.0 * s, y + 7.2 * s)
            line.Dispose()
        elif kind == "float":
            pen = Pen(color, 1.4 * s)
            path = self._rounded(g, x + 1.5 * s, y + 5.0 * s, 10.5 * s, 11.0 * s, 2.0 * s)
            g.DrawPath(pen, path)
            path.Dispose()
            pen.Dispose()
            brush = SolidBrush(color)
            path = self._rounded(g, x + 7.5 * s, y + 1.5 * s, 9.0 * s, 8.0 * s, 2.0 * s)
            g.FillPath(brush, path)
            path.Dispose()
            brush.Dispose()
        elif kind == "quit":
            pen = Pen(color, 1.5 * s)
            pen.StartCap = LineCap.Round
            pen.EndCap = LineCap.Round
            g.DrawArc(pen, x + 2.6 * s, y + 3.4 * s, 12.8 * s, 12.8 * s, -62.0, 304.0)
            pen.Dispose()
            line = Pen(color, 1.6 * s)
            g.DrawLine(line, x + 9.0 * s, y + 1.6 * s, x + 9.0 * s, y + 8.4 * s)
            line.Dispose()

    def _on_paint(self, sender, event) -> None:
        try:
            self._paint(event.Graphics)
        except Exception:
            self._log.debug("绘制托盘菜单失败", exc_info=True)

    def _paint(self, g) -> None:
        from System.Drawing import (Pen, RectangleF, SolidBrush, StringAlignment,
                                    StringFormat)
        from System.Drawing.Drawing2D import SmoothingMode
        from System.Drawing.Text import TextRenderingHint

        s = self._scale
        width, height = self._width, self._height
        g.SmoothingMode = SmoothingMode.AntiAlias
        g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit
        g.Clear(self._color.FromArgb(*TRAY_COLORS["bg"]))

        pen = Pen(self._color.FromArgb(*TRAY_COLORS["border"]), 1.0)
        g.DrawRectangle(pen, 0, 0, width - 1, height - 1)
        pen.Dispose()

        sf = StringFormat()
        sf.LineAlignment = StringAlignment.Center

        y = TRAY_MENU_PAD * s
        header = RectangleF(
            TRAY_MENU_PAD * s + 8 * s,
            y,
            width - (TRAY_MENU_PAD * s + 8 * s) * 2,
            TRAY_MENU_HEADER_H * s,
        )
        brush = SolidBrush(self._color.FromArgb(*TRAY_COLORS["muted"]))
        g.DrawString("作业备忘  ·  v" + self._version, self._font_header, brush, header, sf)
        brush.Dispose()
        y += TRAY_MENU_HEADER_H * s

        for idx, row in enumerate(self._rows):
            if row[0] == "sep":
                pen = Pen(self._color.FromArgb(*TRAY_COLORS["sep"]), 1.0)
                mid = y + TRAY_MENU_SEP_H * s / 2.0
                g.DrawLine(pen, 12 * s, mid, width - 12 * s, mid)
                pen.Dispose()
                y += TRAY_MENU_SEP_H * s
                continue

            _, text, icon, danger, bold, _callback = row
            rect_x = TRAY_MENU_PAD * s
            rect_w = width - TRAY_MENU_PAD * s * 2
            rect_h = TRAY_MENU_ITEM_H * s

            if idx == self._hover:
                path = self._rounded(g, rect_x, y + 1 * s, rect_w, rect_h - 2 * s, 6 * s)
                brush = SolidBrush(self._color.FromArgb(*TRAY_COLORS["hover"]))
                g.FillPath(brush, path)
                brush.Dispose()
                path.Dispose()

            self._draw_icon(
                g, icon, rect_x + 9 * s, y + (TRAY_MENU_ITEM_H - TRAY_MENU_ICON) / 2.0 * s, s
            )

            font = self._font_item_bold if bold else self._font_item
            brush = SolidBrush(
                self._color.FromArgb(*(TRAY_COLORS["danger"] if danger else TRAY_COLORS["text"]))
            )
            text_rect = RectangleF(
                rect_x + (9 + TRAY_MENU_ICON + 11) * s,
                y,
                rect_w - (9 + TRAY_MENU_ICON + 11) * s,
                rect_h,
            )
            g.DrawString(text, font, brush, text_rect, sf)
            brush.Dispose()
            y += rect_h

    # -- 交互 ----------------------------------------------------------
    def _row_at(self, y) -> int:
        s = self._scale
        cursor = TRAY_MENU_PAD * s + TRAY_MENU_HEADER_H * s
        for idx, row in enumerate(self._rows):
            step = (TRAY_MENU_SEP_H if row[0] == "sep" else TRAY_MENU_ITEM_H) * s
            if row[0] != "sep" and cursor <= y < cursor + step:
                return idx
            cursor += step
        return -1

    def _set_hover(self, idx: int) -> None:
        if idx == self._hover:
            return
        self._hover = idx
        try:
            self._form.Invalidate()
        except Exception:
            pass

    def _on_mouse_move(self, sender, event) -> None:
        self._set_hover(self._row_at(event.Y))

    def _on_mouse_leave(self, sender, event) -> None:
        self._set_hover(-1)

    def _on_mouse_up(self, sender, event) -> None:
        if event.Button != self._wf.MouseButtons.Left:
            return
        idx = self._row_at(event.Y)
        if idx < 0 or self._rows[idx][0] == "sep":
            return
        callback = self._rows[idx][5]
        self.close()
        if callback is not None:
            try:
                callback()
            except Exception:
                self._log.exception("执行托盘菜单命令失败")

    def _on_key_down(self, sender, event) -> None:
        if event.KeyCode == self._wf.Keys.Escape:
            self.close()

    def _on_deactivate(self, sender, event) -> None:
        # 刚弹出的一瞬间可能还没拿到焦点，避免菜单立刻自己关掉。
        if time.time() - self._opened_at < 0.25:
            return
        self.close()

    # -- 显示 / 关闭 ---------------------------------------------------
    def show_at(self, x: int, y: int) -> None:
        from System.Drawing import Point

        screen = self._wf.Screen.FromPoint(Point(x, y)).WorkingArea
        left = min(
            max(screen.Left + 4, x - self._width + 8), screen.Right - self._width - 4
        )
        top = min(
            max(screen.Top + 4, y - self._height - 8), screen.Bottom - self._height - 4
        )
        self._form.Location = Point(left, top)
        self._form.Show()
        self._form.Activate()
        self._opened_at = time.time()
        self._apply_window_style()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._form.Close()
        except Exception:
            self._log.debug("关闭托盘菜单失败", exc_info=True)


class TrayController:
    """任务栏通知区域的托盘图标。

    关闭窗口只是把窗口收起来，进程继续在后台运行；想彻底退出要用托盘菜单。
    """

    def __init__(self, *, on_open, on_toggle_float, on_quit, float_state, log: logging.Logger):
        self._on_open = on_open
        self._on_toggle_float = on_toggle_float
        self._on_quit = on_quit
        self._float_state = float_state
        self._log = log
        self._icon = None
        self._menu = None
        self._native = None
        self._wf = None
        self._popup = None

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _dispatch(func) -> None:
        # 托盘回调跑在 WinForms UI 线程上，窗口 API 必须换到别的线程执行。
        threading.Thread(target=func, daemon=True).start()

    @staticmethod
    def _run_on_ui(native, func) -> None:
        from System import Action  # pythonnet

        if native is None:
            return
        if native.InvokeRequired:
            native.Invoke(Action(func))
        else:
            func()

    # -- lifecycle -----------------------------------------------------
    def ensure(self, window) -> None:
        """在主窗口原生控件创建好之后挂上托盘图标。"""
        if self._icon is not None or window is None:
            return
        try:
            native = window.native
            self._native = native
            self._run_on_ui(native, self._create)
        except Exception:
            self._log.exception("创建托盘图标失败")

    def _create(self) -> None:
        if self._icon is not None:
            return
        try:
            from System.Drawing import Icon
            import clr

            clr.AddReference("System.Windows.Forms")
            import System.Windows.Forms as WinForms

            icon = WinForms.NotifyIcon()
            try:
                icon.Icon = Icon(str(hs.ICON_PATH))
            except Exception:
                icon.Icon = WinForms.SystemIcons.Application
            icon.Text = TRAY_TEXT

            icon.MouseUp += self._on_icon_mouse_up
            icon.Visible = True

            self._wf = WinForms
            self._icon = icon
            self._log.info("托盘图标已创建")
        except Exception:
            self._log.exception("创建托盘图标失败")

    def notify_hidden(self) -> None:
        """第一次收起到托盘时给个提示。"""
        if self._icon is None:
            return
        try:
            # 第 4 个参数是 ToolTipIcon 枚举，pythonnet 3 不允许隐式传 int，
            # 必须给枚举成员，否则会抛「int can not be converted to Enum」。
            icon_kind = self._wf.ToolTipIcon.Info if self._wf is not None else None
            self._run_on_ui(
                self._native,
                lambda: self._icon.ShowBalloonTip(
                    3000,
                    "作业备忘还在后台运行",
                    "点击托盘图标可以重新打开窗口。",
                    icon_kind,
                ),
            )
        except Exception:
            self._log.debug("显示托盘提示失败", exc_info=True)

    def dispose(self) -> None:
        if self._popup is not None:
            try:
                self._popup.close()
            except Exception:
                pass
            self._popup = None
        if self._icon is None:
            return
        try:
            def _dispose():
                try:
                    self._icon.Visible = False
                    self._icon.Dispose()
                except Exception:
                    pass

            self._run_on_ui(self._native, _dispose)
        except Exception:
            self._log.debug("释放托盘图标失败", exc_info=True)
        finally:
            self._icon = None

    # -- 托盘菜单（WinForms UI 线程） -----------------------------------
    def _on_icon_mouse_up(self, sender=None, event=None) -> None:
        """左键直接打开主界面，右键弹出菜单。"""
        wf = self._wf
        if wf is not None and event is not None and event.Button == wf.MouseButtons.Right:
            self._show_menu()
            return
        self._dispatch(self._on_open)

    def _menu_rows(self):
        try:
            floating_open = bool(self._float_state())
        except Exception:
            floating_open = False
        float_text = "收起悬浮球" if floating_open else "显示悬浮球"
        return [
            ("sep",),
            ("item", "打开主界面", "open", False, True,
             lambda: self._dispatch(self._on_open)),
            ("item", float_text, "float", False, False,
             lambda: self._dispatch(self._on_toggle_float)),
            ("sep",),
            ("item", "退出作业备忘", "quit", True, False,
             lambda: self._dispatch(self._on_quit)),
        ]

    def _show_menu(self) -> None:
        try:
            if self._popup is not None:
                self._popup.close()
            wf = self._wf
            popup = TrayMenuPopup(self._menu_rows(), APP_VERSION, self._log)
            cursor = wf.Cursor.Position
            popup.show_at(cursor.X, cursor.Y)
            self._popup = popup
        except Exception:
            self._log.exception("显示托盘菜单失败")


class DesktopBridge:
    """Exposed to both WebView windows through window.pywebview.api."""

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self._main: webview.Window | None = None
        self._ball: BallWindow | None = None
        self._ball_visible = False
        self._ball_position: tuple[int, int] | None = None
        self._drag_residual = (0.0, 0.0)
        self._drag_thread: threading.Thread | None = None
        self._resize_thread: threading.Thread | None = None
        self._shrink: ShrinkAnimation | None = None
        self._shutting_down = False
        self._tray: TrayController | None = None
        self._tray_tip_shown = False
        self._lock = threading.RLock()
        self._log = logging.getLogger(hs.APP_NAME)

    def attach_tray(self, tray: TrayController) -> None:
        self._tray = tray

    def _attach(self, main: webview.Window, ball: BallWindow) -> None:
        self._main = main
        self._ball = ball
        main.events.closing += self._on_main_closing
        main.events.shown += self._on_window_shown
        main.events.maximized += self._on_window_maximized
        main.events.restored += self._on_window_restored

    # ------------------------------------------------------------------
    # Frameless window chrome
    # ------------------------------------------------------------------
    def _play_main_anim(self, kind: str) -> None:
        """让主窗口播一遍进场 / 退场动画（真正干活的是页面里的 CSS）。

        投递到后台线程执行：pywebview 的 ``shown`` 回调可能就在 UI 线程上，
        在 UI 线程里同步 evaluate_js 有把自己等死的风险，而动画早几十毫秒
        或晚几十毫米都不影响观感。
        """
        window = self._main
        if window is None:
            return
        script = "window.__appAnim && window.__appAnim('%s')" % ("leave" if kind == "leave" else "enter")

        def run():
            try:
                window.evaluate_js(script)
            except Exception:
                self._log.debug("播放主窗口过渡动画失败：%s", kind, exc_info=True)

        threading.Thread(target=run, daemon=True).start()

    def _on_window_shown(self, window):
        fit_maximized_bounds(window)
        apply_window_frame(window, maximized=False)
        if window is self._main and self._tray is not None:
            self._tray.ensure(window)
        # 去掉系统边框后客户区会缩小 16×39，这里补回设计尺寸。
        try:
            window.resize(MAIN_WIDTH, MAIN_HEIGHT)
        except Exception:
            self._log.debug("恢复窗口尺寸失败", exc_info=True)
        if window is self._main:
            # 从托盘 / 悬浮球唤回时窗口是“空白”的（上次退场停在透明态），
            # 这里补一次进场，顺便兜住任何没走到 __appAnim 的显示路径。
            self._play_main_anim("enter")

    def _on_window_maximized(self, window):
        apply_window_frame(window, maximized=True)

    def _on_window_restored(self, window):
        apply_window_frame(window, maximized=False)

    def _target_window(self, target: str) -> webview.Window | None:
        key = str(target or "").strip().lower()
        if key in {"main", "primary"}:
            return self._main
        if key in {"ball", "float", "floating", "pip"}:
            return self._ball
        return None

    def window_state(self, target: str = "main") -> dict:
        """给自绘标题栏查询窗口状态（是否最大化）。"""
        window = self._target_window(target)
        hwnd = window_hwnd(window)
        maximized = False
        if hwnd:
            try:
                maximized = bool(ctypes.windll.user32.IsZoomed(hwnd))
            except Exception:
                maximized = False
        return {"ok": bool(window), "maximized": maximized}

    def window_control(self, target: str = "main", action: str = "") -> dict:
        """自绘标题栏的最小化 / 最大化 / 关闭。"""
        window = self._target_window(target)
        if window is None:
            return {"ok": False, "maximized": False}

        name = str(action or "").strip().lower()
        try:
            if name == "minimize":
                window.minimize()
            elif name in {"maximize", "restore", "toggle-maximize", "toggle_maximize"}:
                hwnd = window_hwnd(window)
                maximized = bool(hwnd and ctypes.windll.user32.IsZoomed(hwnd))
                if name == "maximize" or (name.startswith("toggle") and not maximized):
                    window.maximize()
                else:
                    window.restore()
            elif name in {"close", "destroy"}:
                if self._shutting_down:
                    window.destroy()
                elif window is self._main:
                    # 关闭按钮 = 收起主窗口并显示悬浮球，后台继续运行。
                    threading.Thread(target=self.hide_to_tray, daemon=True).start()
                else:
                    # 悬浮球没有系统关闭按钮，这里按「收起」处理。
                    threading.Thread(target=self._hide_ball, daemon=True).start()
            elif name == "hide":
                if window is self._main:
                    # 主窗口直接 hide 会跳过退场动画，统一走收起流程。
                    threading.Thread(target=self.hide_to_tray, daemon=True).start()
                else:
                    window.hide()
            else:
                return {"ok": False, "maximized": False}
        except Exception:
            self._log.exception("窗口操作失败：%s %s", target, action)
        return self.window_state(target)

    def window_drag(self, target: str = "main") -> dict:
        """按住自绘标题栏时拖动窗口。

        为什么不用 ``WM_NCLBUTTONDOWN + HTCAPTION``：frameless 窗口的
        ``DefWindowProc`` 对这条消息不启动移动循环（实测窗口纹丝不动），而且
        ``ReleaseCapture()`` 只对**本线程**的鼠标捕获有效 —— 拖动时捕获在
        WebView2 的 UI 线程手里，后台线程调它等于没调。

        所以自己跟：记下「光标 - 窗口原点」的偏移，之后按 ~60 fps 轮询全局光标，
        把窗口摆到「光标 - 偏移」。这是绝对位移，DPI 缩放下也不会累积误差，
        只用一次 IPC，不用每帧回传坐标。
        """
        window = self._target_window(target)
        hwnd = window_hwnd(window)
        if not hwnd:
            return {"ok": False}
        if not ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
            return {"ok": False}
        worker = self._drag_thread
        if worker is not None and worker.is_alive():
            return {"ok": True}
        self._drag_thread = threading.Thread(
            target=self._drag_loop, args=(hwnd,), name="homework-window-drag", daemon=True
        )
        self._drag_thread.start()
        return {"ok": True}

    def _drag_loop(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        try:
            point = wt.POINT()
            if not user32.GetCursorPos(ctypes.byref(point)):
                return
            left, top, _, _ = window_rect(hwnd)
            offset_x, offset_y = int(point.x) - left, int(point.y) - top
            self._log.debug("开始拖动窗口 hwnd=%s 偏移=(%s,%s)", hwnd, offset_x, offset_y)
            # 最多跟 30 秒，防止按键状态卡住时线程一直转。
            deadline = time.time() + 30
            while time.time() < deadline:
                if not user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
                    break
                if not user32.GetCursorPos(ctypes.byref(point)):
                    break
                user32.SetWindowPos(
                    ctypes.c_void_p(hwnd),
                    None,
                    int(point.x) - offset_x,
                    int(point.y) - offset_y,
                    0,
                    0,
                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
                )
                time.sleep(0.008)
            self._log.debug("结束拖动窗口 %s", window_rect(hwnd))
        except Exception:
            self._log.debug("拖动主窗口失败", exc_info=True)

    def window_resize(self, target: str = "main", edge: str = "") -> dict:
        """拖拽四边 / 四角缩放。

        和 ``window_drag`` 同样的原因：frameless 窗口上 ``WM_NCLBUTTONDOWN``
        不会启动系统缩放循环（``ReleaseCapture()`` 也只对本线程的捕获有效），
        所以改成自己跟光标：起点记下光标与窗口矩形，之后逐帧按位移重算。
        """
        window = self._target_window(target)
        hwnd = window_hwnd(window)
        key = str(edge or "").strip().lower()
        if not hwnd or key not in RESIZE_EDGES:
            return {"ok": False}
        if not ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
            return {"ok": False}
        worker = self._resize_thread
        if worker is not None and worker.is_alive():
            return {"ok": True}
        self._resize_thread = threading.Thread(
            target=self._resize_loop, args=(hwnd, key), name="homework-window-resize", daemon=True
        )
        self._resize_thread.start()
        return {"ok": True}

    def _resize_loop(self, hwnd: int, edge: str) -> None:
        user32 = ctypes.windll.user32
        try:
            point = wt.POINT()
            if not user32.GetCursorPos(ctypes.byref(point)):
                return
            start_x, start_y = int(point.x), int(point.y)
            left, top, right, bottom = window_rect(hwnd)
            deadline = time.time() + 30
            while time.time() < deadline:
                if not user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
                    break
                if not user32.GetCursorPos(ctypes.byref(point)):
                    break
                dx, dy = int(point.x) - start_x, int(point.y) - start_y
                new_left, new_top, new_right, new_bottom = left, top, right, bottom
                if "left" in edge:
                    new_left = min(left + dx, right - WINDOW_MIN_WIDTH)
                if "right" in edge:
                    new_right = max(right + dx, left + WINDOW_MIN_WIDTH)
                if "top" in edge:
                    new_top = min(top + dy, bottom - WINDOW_MIN_HEIGHT)
                if "bottom" in edge:
                    new_bottom = max(bottom + dy, top + WINDOW_MIN_HEIGHT)
                user32.SetWindowPos(
                    ctypes.c_void_p(hwnd),
                    None,
                    new_left,
                    new_top,
                    new_right - new_left,
                    new_bottom - new_top,
                    SWP_NOZORDER | SWP_NOACTIVATE,
                )
                time.sleep(0.008)
        except Exception:
            self._log.debug("缩放主窗口失败", exc_info=True)

    # ------------------------------------------------------------------
    # Window control
    # ------------------------------------------------------------------
    def hide_to_tray(self) -> dict:
        """收起主窗口，改为显示桌面悬浮球；进程留在后台不退出。

        视觉上是「窗口被吸进悬浮球」：先抓住主窗口当前画面，用分层窗口把它
        逐帧缩到球的位置并淡出，第一帧盖上去之后才隐藏真窗口，所以中间不会
        闪一下桌面。
        """
        with self._lock:
            if self._shutting_down:
                return {"ok": False, "hidden": True}
            main = self._main
            ball = self._ball

        def hide_and_show_ball() -> None:
            with self._lock:
                if main is not None:
                    try:
                        main.hide()
                    except Exception:
                        self._log.exception("收起主窗口失败")
                self._show_ball()

        anim = None
        hwnd = window_hwnd(main) if main is not None else 0
        # 已经在收起中 / 窗口本来就不可见时不要再抓帧，否则会抓出一张空白去缩放。
        if (
            hwnd
            and ball is not None
            and self._shrink is None
            and ctypes.windll.user32.IsWindowVisible(ctypes.c_void_p(hwnd))
        ):
            # 先把球摆到最终位置，动画才能准确落在球上。
            self._position_ball()
            ball_x, ball_y = ball.position()
            anim = ShrinkAnimation.from_window(
                hwnd,
                (ball_x, ball_y, BALL_SIZE, BALL_SIZE),
                duration=MAIN_SHRINK_SECONDS,
                log=self._log,
            )
        if anim is None:
            # 抓不到画面（远程桌面 / 抓屏受限）就直接切换，功能不受影响。
            hide_and_show_ball()
        else:
            self._shrink = anim
            try:
                anim.play(on_ready=hide_and_show_ball)
            except Exception:
                self._log.debug("播放收缩动画失败", exc_info=True)
                if self._ball_visible is False:
                    hide_and_show_ball()
            finally:
                self._shrink = None

        with self._lock:
            if self._shutting_down:
                return {"ok": False, "hidden": True}
            self._log.info("主窗口已收起，悬浮球已显示，应用继续在后台运行。")
            if self._tray is not None and not self._tray_tip_shown:
                self._tray_tip_shown = True
                self._tray.notify_hidden()
            return {"ok": True, "hidden": True}

    def _cancel_shrink(self) -> None:
        """用户在一半又点开主窗口时，立刻让收缩动画退场。"""
        anim = self._shrink
        if anim is not None:
            anim.stop()

    def activate_main(self) -> dict:
        """从托盘 / 悬浮球重新打开主界面。"""
        self._cancel_shrink()
        with self._lock:
            if not self._main or self._shutting_down:
                return {"ok": False}
            self._hide_ball()
            try:
                self._main.show()
                self._main.restore()
                hwnd = window_hwnd(self._main)
                if hwnd:
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                # shown 事件不一定每次都给，这里主动补一次进场（页面侧有 300ms 去重）。
                self._play_main_anim("enter")
            except Exception:
                self._log.exception("显示主窗口失败")
            return {"ok": True}

    def begin_shutdown(self) -> None:
        self._shutting_down = True

    # ------------------------------------------------------------------
    # 悬浮球
    # ------------------------------------------------------------------
    def _ball_state_path(self) -> Path:
        return self._data_dir / BALL_STATE_FILE

    def _load_ball_state(self) -> tuple[int, int] | None:
        try:
            data = json.loads(self._ball_state_path().read_text(encoding="utf-8"))
            return int(data["x"]), int(data["y"])
        except Exception:
            return None

    def _store_ball_state(self, x: int, y: int) -> None:
        self._ball_position = (int(x), int(y))
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self._ball_state_path().write_text(
                json.dumps({"x": int(x), "y": int(y)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            self._log.debug("保存悬浮球位置失败", exc_info=True)

    def _position_ball(self) -> None:
        """把悬浮球放到保存的位置，没有记录或越界时用默认位置。"""
        hwnd = window_hwnd(self._ball)
        if not hwnd:
            return
        saved = self._ball_position or self._load_ball_state() or default_ball_position()
        self._ball.move_to(saved[0], saved[1])
        self._ball_position = self._ball.position()

    def _show_ball(self) -> None:
        with self._lock:
            if not self._ball or self._shutting_down:
                return
            try:
                self._ball_visible = True
                # 先摆好位置再显示，避免从默认位置“滑”过来。
                self._position_ball()
                self._ball.show()
            except Exception:
                self._log.exception("显示悬浮球失败")
                self._ball_visible = False

    def _hide_ball(self) -> None:
        with self._lock:
            if not self._ball:
                return
            self._ball_visible = False
            try:
                self._ball.hide()
            except Exception:
                self._log.debug("收起悬浮球失败", exc_info=True)

    def toggle_floating(self) -> dict:
        """兼容主界面按钮：切换悬浮球的显示 / 收起。"""
        with self._lock:
            if self._ball_visible:
                self._hide_ball()
            else:
                self._show_ball()
            return {"open": bool(self._ball_visible)}

    @property
    def floating_visible(self) -> bool:
        return bool(self._ball_visible)

    def show_main(self) -> dict:
        self.activate_main()
        return {"open": False}

    def get_floating_state(self) -> dict:
        return {"open": bool(self._ball_visible)}

    def show_main_and_login(self) -> dict:
        # 登录弹窗由悬浮球通过 BroadcastChannel 通知主界面打开，避免跨线程
        # 同步执行 JavaScript。
        return self.show_main()

    def open_external(self, url: str) -> bool:
        try:
            parsed = urlparse(str(url))
            if parsed.scheme not in {"http", "https"}:
                return False
            webbrowser.open(str(url))
            return True
        except Exception:
            return False

    def hide_floating(self) -> dict:
        self._hide_ball()
        return {"open": False}

    # ------------------------------------------------------------------
    # 悬浮球桌面桥（供 ball.html 调用）
    # ------------------------------------------------------------------
    def ball_move(self, dx: float = 0, dy: float = 0, final: bool = False) -> dict:
        """按 CSS 像素增量移动悬浮球（相当于 Electron 的 win.setPosition 增量版）。

        - ``dx`` / ``dy`` 是相对上一次调用的累计增量，JS 侧只发增量；
        - DPI 缩放下换算成物理像素，取整误差留在 ``_drag_residual`` 里下一帧补上，
          否则每帧丢零点几像素，拖久了会明显偏离鼠标；
        - ``final=True`` 表示松手，顺手把位置写盘，避免“保存”和“移动”两次调用乱序。
        """
        hwnd = window_hwnd(self._ball)
        if not hwnd:
            return {"ok": False}
        scale = dpi_scale_for_window(hwnd)
        left, top, _right, _bottom = window_rect(hwnd)

        step_x = float(dx) * scale + self._drag_residual[0]
        step_y = float(dy) * scale + self._drag_residual[1]
        move_x = int(round(step_x))
        move_y = int(round(step_y))
        self._drag_residual = (step_x - move_x, step_y - move_y)

        x, y = move_window_to(hwnd, left + move_x, top + move_y)
        self._ball_position = (x, y)
        if final:
            self._drag_residual = (0.0, 0.0)
            self._store_ball_state(x, y)
        return {"ok": True, "x": x, "y": y}

    def ball_position(self) -> dict:
        hwnd = window_hwnd(self._ball)
        if not hwnd:
            return {"ok": False, "x": 0, "y": 0}
        left, top, _right, _bottom = window_rect(hwnd)
        return {"ok": True, "x": left, "y": top}

    def ball_save_position(self) -> dict:
        hwnd = window_hwnd(self._ball)
        if not hwnd:
            return {"ok": False}
        left, top, _right, _bottom = window_rect(hwnd)
        self._store_ball_state(left, top)
        return {"ok": True, "x": left, "y": top}

    def ball_open_main(self) -> dict:
        return self.activate_main()

    def ball_hide(self) -> dict:
        self._hide_ball()
        return {"ok": True, "open": False}

    # ------------------------------------------------------------------
    # Closing
    # ------------------------------------------------------------------
    def _on_main_closing(self):
        if self._shutting_down:
            return None
        # Alt+F4、任务栏关闭都只是收起主窗口并显示悬浮球，进程继续后台运行。
        threading.Thread(target=self.hide_to_tray, daemon=True).start()
        return False


class DesktopApplication:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.log = logging.getLogger(hs.APP_NAME)
        self.context = hs.AppContext(data_dir)
        self.server: hs.LocalServer | None = None
        self.server_thread: threading.Thread | None = None
        self.bridge = DesktopBridge(data_dir)
        self.tray = TrayController(
            on_open=self.bridge.activate_main,
            on_toggle_float=self._toggle_float_from_tray,
            on_quit=self._quit_from_tray,
            float_state=lambda: self.bridge.floating_visible,
            log=self.log,
        )
        self.bridge.attach_tray(self.tray)
        self.main: webview.Window | None = None
        self.ball: BallWindow | None = None
        self._show_event = None
        self._exiting = False
        # 主题变更（前端切换/导入/删除）后让悬浮球跟着换色。
        self.context.on_theme_changed = self._apply_theme_to_ball

    def _apply_theme_to_ball(self, theme: dict) -> None:
        if self.ball is None:
            return
        self.ball.set_palette(hs.theme_mod.ball_palette(theme))

    # ------------------------------------------------------------------
    # Tray actions
    # ------------------------------------------------------------------
    def _ball_snapshot(self) -> dict:
        """给悬浮球和概览卡片用的数据快照。

        角标只关心数字，卡片还要最近几条最紧急的作业，所以一次算全。
        """
        payload = self.context.homework()
        now = datetime.now()
        total = urgent = overdue = today = 0
        items: list[dict] = []
        for item in payload.get("assignments") or []:
            if item.get("completed"):
                continue
            total += 1
            try:
                due = datetime.fromisoformat(str(item.get("dueDate")))
            except Exception:
                items.append({"name": self._item_title(item), "course": item.get("course") or ""})
                continue
            if due.tzinfo is not None:
                due = due.astimezone().replace(tzinfo=None)
            remaining = (due - now).total_seconds()
            if remaining <= 0:
                overdue += 1
            elif remaining <= BALL_URGENT_SECONDS:
                urgent += 1
            if due.date() == now.date() and remaining > 0:
                today += 1
            items.append(
                {
                    "name": self._item_title(item),
                    "course": str(item.get("course") or ""),
                    "remaining": remaining,
                    "urgent": remaining <= BALL_URGENT_SECONDS,
                    "overdue": remaining <= 0,
                }
            )
        items.sort(key=lambda row: row.get("remaining", 0) or 0)
        return {
            "total": total,
            "urgent": urgent,
            "overdue": overdue,
            "today": today,
            "items": items[:BALL_CARD_ROWS],
            "failed": False,
        }

    @staticmethod
    def _item_title(item: dict) -> str:
        return str(item.get("name") or item.get("title") or "未命名作业").strip()

    def _ball_menu(self) -> list[dict]:
        """右键悬浮球弹出的菜单项（框架：以后加项只要往这里塞）。"""
        return [
            {"action": "open", "label": "打开主界面", "icon": "open", "bold": True},
            {"action": "refresh", "label": "刷新数据", "icon": "refresh"},
            {"sep": True},
            {"action": "hide", "label": "隐藏悬浮球", "icon": "hide"},
            {"sep": True},
            {"action": "quit", "label": "退出作业备忘", "icon": "quit", "danger": True},
        ]

    def _ball_menu_action(self, action: str) -> None:
        name = str(action or "").strip().lower()
        try:
            if name == "open":
                self.bridge.activate_main()
            elif name == "refresh":
                threading.Thread(target=self._refresh_ball_now, name="ball-manual-refresh",
                                 daemon=True).start()
            elif name == "hide":
                self.bridge.hide_floating()
            elif name == "quit":
                self._quit_from_tray()
            else:
                self.log.debug("未知的悬浮球菜单动作：%s", action)
        except Exception:
            self.log.exception("执行悬浮球菜单动作失败：%s", action)

    def _refresh_ball_now(self) -> None:
        """手动刷新：强制拉一次远端数据，再让悬浮球重新取快照。"""
        try:
            self.context.homework(force=True)
        except Exception:
            self.log.warning("手动刷新作业数据失败", exc_info=True)
        if self.ball is not None:
            self.ball.request_refresh()

    def _toggle_float_from_tray(self) -> None:
        if self.bridge._shutting_down:
            return
        self.bridge.toggle_floating()

    def _quit_from_tray(self) -> None:
        self.log.info("从托盘退出应用。")
        try:
            self.bridge.begin_shutdown()
            self.tray.dispose()
            for window in (self.ball, self.main):
                if window is None:
                    continue
                try:
                    window.destroy()
                except Exception:
                    self.log.debug("销毁窗口失败", exc_info=True)
        except Exception:
            self.log.exception("退出应用失败")

    def _listen_session_end(self) -> None:
        """系统关机 / 注销时不要拦着，直接退出。"""
        try:
            import clr

            clr.AddReference("System")
            import Microsoft.Win32 as Win32

            Win32.SystemEvents.SessionEnding += self._on_session_ending
        except Exception:
            self.log.debug("监听关机事件失败", exc_info=True)

    def _on_session_ending(self, sender=None, args=None) -> None:
        self.log.info("系统正在关机或注销，退出应用。")
        self._quit_from_tray()

    def _listen_show_main(self) -> None:
        """第二个实例启动时，把已经收起的窗口叫回来。"""
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateEventW.restype = ctypes.c_void_p
        kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        kernel32.WaitForSingleObject.restype = wt.DWORD
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]

        handle = kernel32.CreateEventW(None, False, False, SHOW_MAIN_EVENT)
        if not handle:
            self.log.debug("创建唤醒事件失败")
            return
        self._show_event = handle

        def _wait() -> None:
            while not self._exiting:
                if kernel32.WaitForSingleObject(handle, 500) == 0:
                    self.log.info("收到重新打开请求。")
                    self.bridge.activate_main()

        threading.Thread(target=_wait, name="homework-show-main", daemon=True).start()

    def start_server(self) -> str:
        self.server = hs.LocalServer((hs.HOST, 0), hs.ApiHandler, self.context)
        port = self.server.server_address[1]
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.4},
            name="homework-http",
            daemon=True,
        )
        self.server_thread.start()
        self.context.auth.start_auto_login()
        self.log.info("桌面版本地服务已启动：127.0.0.1:%s", port)
        return f"http://127.0.0.1:{port}"

    def stop_server(self) -> None:
        if not self.server:
            return
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.server.server_close()
        except Exception:
            pass
        self.server = None

    def create_windows(self, base_url: str) -> None:
        ball_x, ball_y = BALL_PARKING_POSITION
        webview.settings["ALLOW_DOWNLOADS"] = False
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        remote_debug_port = os.environ.get("HOMEWORK_DESK_REMOTE_DEBUG")
        if remote_debug_port:
            webview.settings["REMOTE_DEBUGGING_PORT"] = int(remote_debug_port)

        self.main = webview.create_window(
            APP_TITLE,
            base_url + "/",
            js_api=self.bridge,
            width=MAIN_WIDTH,
            height=MAIN_HEIGHT,
            min_size=(900, 620),
            frameless=True,
            easy_drag=False,
            shadow=True,
            background_color=self.context.active_theme().get("vars", {}).get("--bg", "#f4f1eb"),
            text_select=True,
            zoomable=False,
            confirm_close=False,
        )
        # 悬浮球：原生分层窗口 + GDI+ 逐像素透明（WebView2 做不到真透明）。
        self.ball = BallWindow(
            size=BALL_SIZE,
            position=(ball_x, ball_y),
            log=self.log,
            data_provider=self._ball_snapshot,
            on_open_main=self.bridge.activate_main,
            on_hide=self.bridge.hide_floating,
            on_moved=self.bridge._store_ball_state,
            clamp=clamp_ball_position,
            menu_provider=self._ball_menu,
            on_menu_action=self._ball_menu_action,
        )
        if not self.ball.start():
            self.log.error("悬浮球窗口初始化失败")
        # 悬浮球初始配色跟着当前主题走
        self._apply_theme_to_ball(self.context.active_theme())
        threading.Thread(target=self.ball.poll_forever, name="homework-ball-data", daemon=True).start()
        self.bridge._attach(self.main, self.ball)

    def run(self) -> None:
        base_url = self.start_server()
        self.create_windows(base_url)
        self._listen_show_main()
        self._listen_session_end()

        if os.environ.get("HOMEWORK_DESK_AUTOSHOW_FLOAT") == "1":
            # 回归测试开关：走「关闭主窗口 -> 吸进悬浮球」的完整路径，
            # 这样能覆盖收缩动画 + 悬浮球交互，和用户的真实操作一致。
            def show_float_later():
                time.sleep(4)
                self.bridge.hide_to_tray()
            threading.Thread(target=show_float_later, daemon=True).start()

        try:
            webview.start(
                debug=bool(os.environ.get("HOMEWORK_DESK_DEBUG")),
                private_mode=False,
                storage_path=str(self.data_dir / "webview"),
                icon=str(hs.ICON_PATH) if hs.ICON_PATH.exists() else None,
            )
        finally:
            self._exiting = True
            self.tray.dispose()
            if self._show_event:
                try:
                    ctypes.windll.kernel32.CloseHandle(self._show_event)
                except Exception:
                    pass
                self._show_event = None
            self.stop_server()
            self.log.info("桌面应用退出。")


def main() -> int:
    enable_dpi_awareness()
    data_dir = hs.default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    logger = hs.configure_logging(data_dir, debug=bool(os.environ.get("HOMEWORK_DESK_DEBUG")))
    hs.LOG = logger

    mutex, already_running = acquire_single_instance()
    if already_running:
        if signal_existing_instance():
            logger.info("已有实例在运行，已通知它显示主窗口。")
            return 0
        show_message(
            "作业备忘已经在后台运行。\n请查看任务栏通知区域（托盘）的图标。",
            flags=0x40,
        )
        return 0

    if not hs.APP_HTML.exists():
        missing = hs.APP_HTML
        logger.error("缺少界面文件：%s", missing)
        show_message(f"缺少界面文件：\n{missing}\n\n请重新安装作业备忘。", flags=0x10)
        return 1

    logger.info("启动作业备忘桌面版 v%s", APP_VERSION)
    try:
        DesktopApplication(data_dir).run()
    except Exception as exc:
        logger.exception("桌面应用异常退出")
        show_message(f"作业备忘启动失败：\n{exc}", flags=0x10)
        return 1
    finally:
        try:
            ctypes.windll.kernel32.ReleaseMutex(mutex)
            ctypes.windll.kernel32.CloseHandle(mutex)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
