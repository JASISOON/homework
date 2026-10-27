#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
作业备忘 · Windows 桌面应用

- 内嵌本地服务（127.0.0.1 随机端口），不打开浏览器。
- 主窗口使用 WebView2 渲染现有界面。
- 悬浮窗是独立的小窗口，可置顶，和主界面通过本机 API / BroadcastChannel 同步。
- 复用 homework_server.py 的加密凭据、深大登录和作业接口。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

import webview

import homework_server as hs


APP_TITLE = "作业备忘 · 深大优课"
FLOATING_TITLE = "作业备忘 · 悬浮窗"
TRAY_TEXT = "作业备忘 · 深大优课"
APP_VERSION = hs.APP_VERSION
MUTEX_NAME = "Local\\HomeworkDeskDesktopApp"
SHOW_MAIN_EVENT = "Local\\HomeworkDeskShowMain"
EVENT_MODIFY_STATE = 0x0002
MAIN_WIDTH = 1180
MAIN_HEIGHT = 830
FLOAT_WIDTH = 380
FLOAT_HEIGHT = 500

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


class TrayController:
    """任务栏通知区域的托盘图标。

    关闭窗口只是把窗口收起来，进程继续在后台运行；想彻底退出要用托盘菜单，
    或再次启动应用后从托盘里选择“退出”。
    """

    def __init__(self, *, on_open, on_toggle_float, on_quit, log: logging.Logger):
        self._on_open = on_open
        self._on_toggle_float = on_toggle_float
        self._on_quit = on_quit
        self._log = log
        self._icon = None
        self._menu = None
        self._native = None

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

            menu = WinForms.ContextMenuStrip()
            open_item = WinForms.ToolStripMenuItem("打开主界面")
            float_item = WinForms.ToolStripMenuItem("显示悬浮窗")
            quit_item = WinForms.ToolStripMenuItem("退出作业备忘")
            open_item.Click += self._on_open_clicked
            float_item.Click += self._on_float_clicked
            quit_item.Click += self._on_quit_clicked
            menu.Items.Add(open_item)
            menu.Items.Add(float_item)
            menu.Items.Add(WinForms.ToolStripSeparator())
            menu.Items.Add(quit_item)

            icon.ContextMenuStrip = menu
            icon.DoubleClick += self._on_open_clicked
            icon.Visible = True

            self._icon = icon
            self._menu = menu
            self._log.info("托盘图标已创建")
        except Exception:
            self._log.exception("创建托盘图标失败")

    def notify_hidden(self) -> None:
        """第一次收起到托盘时给个提示。"""
        if self._icon is None:
            return
        try:
            self._run_on_ui(
                self._native,
                lambda: self._icon.ShowBalloonTip(
                    3000,
                    "作业备忘还在后台运行",
                    "点击托盘图标可以重新打开窗口。",
                    1,  # ToolTipIcon.Info
                ),
            )
        except Exception:
            self._log.debug("显示托盘提示失败", exc_info=True)

    def dispose(self) -> None:
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
            self._menu = None

    # -- menu handlers (WinForms UI thread) ----------------------------
    def _on_open_clicked(self, sender=None, event=None) -> None:
        self._dispatch(self._on_open)

    def _on_float_clicked(self, sender=None, event=None) -> None:
        self._dispatch(self._on_toggle_float)

    def _on_quit_clicked(self, sender=None, event=None) -> None:
        self._dispatch(self._on_quit)


class DesktopBridge:
    """Exposed to both WebView windows through window.pywebview.api."""

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self._main: webview.Window | None = None
        self._floating: webview.Window | None = None
        self._floating_visible = False
        self._shutting_down = False
        self._tray: TrayController | None = None
        self._tray_tip_shown = False
        self._lock = threading.RLock()
        self._log = logging.getLogger(hs.APP_NAME)

    def attach_tray(self, tray: TrayController) -> None:
        self._tray = tray

    def _attach(self, main: webview.Window, floating: webview.Window) -> None:
        self._main = main
        self._floating = floating
        main.events.closing += self._on_main_closing
        floating.events.closing += self._on_float_closing
        floating.events.closed += self._on_float_closed
        for window in (main, floating):
            window.events.shown += self._on_window_shown
            window.events.maximized += self._on_window_maximized
            window.events.restored += self._on_window_restored

    # ------------------------------------------------------------------
    # Frameless window chrome
    # ------------------------------------------------------------------
    def _on_window_shown(self, window):
        fit_maximized_bounds(window)
        apply_window_frame(window, maximized=False)
        if window is self._main and self._tray is not None:
            self._tray.ensure(window)
        # 去掉系统边框后客户区会缩小 16×39，这里补回设计尺寸。
        try:
            if window is self._main:
                window.resize(MAIN_WIDTH, MAIN_HEIGHT)
            else:
                window.resize(FLOAT_WIDTH, FLOAT_HEIGHT)
        except Exception:
            self._log.debug("恢复窗口尺寸失败", exc_info=True)

    def _on_window_maximized(self, window):
        apply_window_frame(window, maximized=True)

    def _on_window_restored(self, window):
        apply_window_frame(window, maximized=False)

    def _target_window(self, target: str) -> webview.Window | None:
        key = str(target or "").strip().lower()
        if key in {"main", "primary"}:
            return self._main
        if key in {"float", "floating", "pip"}:
            return self._floating
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
                    # 关闭按钮 = 收起到托盘，后台继续运行。
                    threading.Thread(target=self.hide_to_tray, daemon=True).start()
                else:
                    window.destroy()
            elif name == "hide":
                window.hide()
            else:
                return {"ok": False, "maximized": False}
        except Exception:
            self._log.exception("窗口操作失败：%s %s", target, action)
        return self.window_state(target)

    def window_resize(self, target: str = "main", edge: str = "") -> dict:
        """无边框窗口没有系统缩放边框，前端按下边缘时交给系统原生缩放。"""
        window = self._target_window(target)
        hwnd = window_hwnd(window)
        hit_test = RESIZE_EDGES.get(str(edge or "").strip().lower())
        if not hwnd or hit_test is None:
            return {"ok": False}
        try:
            # 必须是在按住左键的瞬间发起，系统才会进入缩放循环。
            if not ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
                return {"ok": False}
            ctypes.windll.user32.ReleaseCapture()
            ctypes.windll.user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, hit_test, 0)
            return {"ok": True}
        except Exception:
            self._log.debug("调整窗口大小失败：%s %s", target, edge, exc_info=True)
            return {"ok": False}

    # ------------------------------------------------------------------
    # Window control
    # ------------------------------------------------------------------
    def hide_to_tray(self) -> dict:
        """收起所有窗口，进程留在后台，托盘图标常驻。"""
        with self._lock:
            if self._shutting_down:
                return {"ok": False, "hidden": True}
            self._log.info("窗口已收起到托盘，应用继续在后台运行。")
            self._hide_float()
            if self._main:
                try:
                    self._main.hide()
                except Exception:
                    self._log.exception("收起主窗口失败")
            if self._tray is not None and not self._tray_tip_shown:
                self._tray_tip_shown = True
                self._tray.notify_hidden()
            return {"ok": True, "hidden": True}

    def activate_main(self) -> dict:
        """从托盘重新打开主界面。"""
        with self._lock:
            if not self._main or self._shutting_down:
                return {"ok": False}
            try:
                self._main.show()
                self._main.restore()
                hwnd = window_hwnd(self._main)
                if hwnd:
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                self._log.exception("显示主窗口失败")
            return {"ok": True}

    def begin_shutdown(self) -> None:
        self._shutting_down = True

    def _show_float(self) -> None:
        with self._lock:
            if not self._floating or self._shutting_down:
                return
            try:
                self._floating.show()
                self._floating.restore()
                self._floating.on_top = True
                self._floating_visible = True
            except Exception:
                self._log.exception("显示悬浮窗失败")
                self._floating_visible = False

    def _hide_float(self) -> None:
        with self._lock:
            if not self._floating:
                return
            try:
                self._floating.hide()
            except Exception:
                pass
            self._floating_visible = False

    def toggle_floating(self) -> dict:
        with self._lock:
            if self._floating_visible:
                self._hide_float()
            else:
                self._show_float()
            return {"open": bool(self._floating_visible)}

    def show_main(self) -> dict:
        with self._lock:
            self._hide_float()
            if self._main and not self._shutting_down:
                try:
                    self._main.show()
                    self._main.restore()
                except Exception:
                    self._log.exception("显示主窗口失败")
            return {"open": False}

    def get_floating_state(self) -> dict:
        return {"open": bool(self._floating_visible)}

    def show_main_and_login(self) -> dict:
        # 登录弹窗由悬浮页通过 BroadcastChannel 通知主界面打开，避免跨线程
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
        self._hide_float()
        return {"open": False}

    # ------------------------------------------------------------------
    # Closing
    # ------------------------------------------------------------------
    def _on_float_closing(self):
        if self._shutting_down:
            return None
        # 关闭悬浮窗等同于“回到完整界面”。隐藏操作放到后台线程，避免在
        # WinForms 的关闭事件回调里同步调用窗口 API 造成死锁。
        threading.Thread(target=self._return_to_main, daemon=True).start()
        return False  # cancel actual close

    def _return_to_main(self):
        self._hide_float()
        try:
            if self._main and not self._shutting_down:
                self._main.show()
                self._main.restore()
        except Exception:
            self._log.exception("从悬浮窗返回主窗口失败")

    def _on_float_closed(self):
        self._floating_visible = False

    def _on_main_closing(self):
        if self._shutting_down:
            return None
        # Alt+F4、任务栏关闭都只把窗口收到托盘，进程继续后台运行。
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
            log=self.log,
        )
        self.bridge.attach_tray(self.tray)
        self.main: webview.Window | None = None
        self.floating: webview.Window | None = None
        self._show_event = None
        self._exiting = False

    # ------------------------------------------------------------------
    # Tray actions
    # ------------------------------------------------------------------
    def _toggle_float_from_tray(self) -> None:
        if self.bridge._shutting_down:
            return
        self.bridge.toggle_floating()

    def _quit_from_tray(self) -> None:
        self.log.info("从托盘退出应用。")
        try:
            self.bridge.begin_shutdown()
            self.tray.dispose()
            for window in (self.floating, self.main):
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
        screen_width = ctypes.windll.user32.GetSystemMetrics(0)
        float_x = max(20, screen_width - FLOAT_WIDTH - 30)
        float_y = 70
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
            background_color="#f4f1eb",
            text_select=True,
            zoomable=False,
            confirm_close=False,
        )
        self.floating = webview.create_window(
            FLOATING_TITLE,
            base_url + "/floating.html",
            js_api=self.bridge,
            width=FLOAT_WIDTH,
            height=FLOAT_HEIGHT,
            x=float_x,
            y=float_y,
            min_size=(340, 380),
            hidden=True,
            focus=False,
            on_top=True,
            resizable=True,
            frameless=True,
            easy_drag=False,
            shadow=True,
            background_color="#f4f1eb",
            text_select=True,
            zoomable=False,
            confirm_close=False,
        )
        self.bridge._attach(self.main, self.floating)

    def run(self) -> None:
        base_url = self.start_server()
        self.create_windows(base_url)
        self._listen_show_main()
        self._listen_session_end()

        if os.environ.get("HOMEWORK_DESK_AUTOSHOW_FLOAT") == "1":
            def show_float_later():
                time.sleep(4)
                self.bridge.toggle_floating()
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

    if not hs.APP_HTML.exists() or not hs.FLOATING_HTML.exists():
        missing = hs.APP_HTML if not hs.APP_HTML.exists() else hs.FLOATING_HTML
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
