"""桌面悬浮球窗口（含左键展开的概览卡片、右键操作菜单）。

为什么不用 WebView2 / HTML：
    pywebview（HWND 宿主模式）的 WebView2 不支持窗口逐像素透明 ——
    即使设置 ``transparent=True``、把页面背景设成 ``transparent``，
    窗口底色仍然是 WebView2 自己的不透明表面（实测是一块深灰方块），
    色键（``LWA_COLORKEY``）也对子窗口内容无效。
    所以悬浮球、概览卡片、右键菜单都用同一个原生 Win32 分层窗口 + GDI+ 绘制，
    通过 ``UpdateLayeredWindow`` 提交带 alpha 的位图。

Electron 概念在本文件的对应关系：
    transparent / frame:false / alwaysOnTop  -> WS_EX_LAYERED + WS_POPUP + HWND_TOPMOST
    win.setBounds 动画                        -> ``_tick_anim`` 里逐帧 SetWindowPos（缓动）
    HTML + backdrop-filter: blur()            -> 截屏 + 多次重采样模糊 + 深色半透明叠加
    点击浮层外部收起                          -> 展开期间用定时器轮询全局鼠标键

对外接口（供 desktop_app 使用）：
    hwnd / show() / hide() / destroy() / move_to() / position() / refresh()
    set_snapshot(dict) / poll_forever() / request_refresh()
    expand() / collapse() / open_menu() / toggle_card() / expanded
    回调：data_provider() -> dict / menu_provider() -> list[dict]
          on_menu_action(action) / on_open_main() / on_hide() / on_moved(x, y)

线程约定：窗口状态只允许在窗口线程上改。外部线程的 show()/hide() 走
``WM_BALL_CMD`` 投递，数据更新走 ``WM_BALL_DATA``，避免和展开动画抢同一块 DIB。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time

import clr

clr.AddReference("System.Drawing")
clr.AddReference("System.Windows.Forms")

from System.Drawing import (  # noqa: E402
    Bitmap,
    Color,
    ColorTranslator,
    CopyPixelOperation,
    Font,
    FontStyle,
    Graphics,
    GraphicsUnit,
    Pen,
    PointF,
    Rectangle,
    RectangleF,
    Size,
    SolidBrush,
    StringAlignment,
    StringFormat,
    StringFormatFlags,
    StringTrimming,
)
from System.Drawing.Drawing2D import (  # noqa: E402
    CompositingQuality,
    GraphicsPath,
    InterpolationMode,
    LineCap,
    LineJoin,
    LinearGradientBrush,
    PathGradientBrush,
    PixelOffsetMode,
    SmoothingMode,
)
from System import IntPtr  # noqa: E402
from System.Drawing.Imaging import PixelFormat  # noqa: E402
from System.Drawing.Text import TextRenderingHint  # noqa: E402

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32


class _LARGE_INTEGER(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_longlong)]


CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x00000002
TIMER_ALL_ACCESS = 0x1F0003
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0


class AnimClock:
    """逐帧时钟（约 60 fps）。

    为什么不用 ``SetTimer``：``WM_TIMER`` 受系统时钟节拍量化，默认节拍 15.625 ms，
    请求 16 ms 会被顶到两个节拍 ≈ 31 ms —— 展开动画就只剩 32 fps，而且忽快忽慢
    （实测中位间隔 30.4 ms）。``timeBeginPeriod(1)`` 在 Win10+ 也压不下来。
    改用 ``CreateWaitableTimerExW(CREATE_WAITABLE_TIMER_HIGH_RESOLUTION)``：
    实测 16.0~16.6 ms，稳定 61 fps。老系统上创建失败时退回 ``SetTimer(10)``，
    10 ms 小于一个节拍，同样能每拍触发（≈64 fps）。
    """

    def __init__(self, log: logging.Logger | None = None) -> None:
        self._log = log
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._timer = 0
        self._hwnd = 0
        self._fallback = False

    def start(self, hwnd: int, interval_ms: int) -> None:
        self.stop()
        if not hwnd:
            return
        self._hwnd = hwnd
        self._stop = threading.Event()
        handle = kernel32.CreateWaitableTimerExW(
            None, None, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS
        )
        if not handle:
            self._fallback = True
            user32.SetTimer(ctypes.c_void_p(hwnd), TIMER_ANIM, min(10, interval_ms), None)
            return
        self._fallback = False
        self._timer = handle
        self._thread = threading.Thread(
            target=self._loop, args=(hwnd, int(interval_ms), self._stop, handle), daemon=True
        )
        self._thread.start()

    def _loop(self, hwnd: int, interval_ms: int, stop: threading.Event, timer: int) -> None:
        due = _LARGE_INTEGER()
        due.QuadPart = -int(interval_ms * 10000)  # 100 ns 单位、负数 = 相对时间
        # 显式转成 c_void_p：SetWaitableTimer 的形参声明为 c_void_p（见
        # _setup_prototypes 的注释），这里传指针而不是结构体本身。
        due_ptr = ctypes.cast(ctypes.byref(due), ctypes.c_void_p)
        try:
            while not stop.is_set():
                if not kernel32.SetWaitableTimer(
                    ctypes.c_void_p(timer), due_ptr, 0, None, None, False
                ):
                    raise OSError("SetWaitableTimer 返回失败")
                if kernel32.WaitForSingleObject(ctypes.c_void_p(timer), INFINITE) != WAIT_OBJECT_0:
                    break
                if stop.is_set():
                    break
                # 交给窗口线程渲染：GDI+ 位图不能跨线程乱改。
                if not user32.PostMessageW(ctypes.c_void_p(hwnd), WM_BALL_TICK, 0, 0):
                    break
        except Exception:
            if self._log:
                self._log.warning("逐帧时钟不可用，退回 SetTimer 驱动动画", exc_info=True)
            # 高精度时钟用不了也必须让动画能跑，否则 _animating 会一直停在 True，
            # 悬浮球的左右键会彻底失效（点不动、收不起来）。
            try:
                user32.SetTimer(ctypes.c_void_p(hwnd), TIMER_ANIM, min(10, interval_ms), None)
            except Exception:
                pass

    def stop(self) -> None:
        thread = self._thread
        self._stop.set()
        if self._fallback and self._hwnd:
            user32.KillTimer(ctypes.c_void_p(self._hwnd), TIMER_ANIM)
            self._fallback = False
        if self._timer:
            try:
                kernel32.CloseHandle(ctypes.c_void_p(self._timer))
            except Exception:
                pass
            self._timer = 0
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.2)
        self._thread = None

# ---------------------------------------------------------------- Win32 常量
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000
GWL_EXSTYLE = -20

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST = -1

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_PAINT = 0x000F
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_MOUSELEAVE = 0x02A3
WM_TIMER = 0x0113
WM_SETCURSOR = 0x0020
WM_MOUSEACTIVATE = 0x0021
WM_APP = 0x8000
WM_BALL_DATA = WM_APP + 1
WM_BALL_CMD = WM_APP + 2
WM_BALL_TICK = WM_APP + 3
CMD_SHOW = 1
CMD_HIDE = 2
MA_NOACTIVATE = 3
IDC_HAND = 32649
IDC_ARROW = 32512
TME_LEAVE = 0x00000002
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_ESCAPE = 0x1B
KEY_DOWN = 0x8000

TIMER_BREATH = 1
TIMER_ANIM = 2
TIMER_WATCH = 3

# WM_TIMER 的精度受系统时钟节拍限制：默认 15.625 ms 时，SetTimer(16) 会被量化成
# 两个节拍（≈31 ms），展开动画就只剩 32 fps 且忽快忽慢。动画期间把节拍调到 1 ms
# 才能真正做到 60 fps（实测中位间隔 30.4 → 16.2 ms）。
ANIM_INTERVAL_MS = 16
ANIM_DURATION_OPEN = 0.24
ANIM_DURATION_CLOSE = 0.17

BALL_COLOR_A = "#4c8f76"
BALL_COLOR_B = "#2b5647"
DANGER = "#cf5b4e"
AMBER = "#f0c04a"
BADGE_INK = "#244a3c"
AMBER_INK = "#4a3a08"

# 卡片设计尺寸（96 DPI 下的逻辑像素，实际按窗口 DPI 缩放）
CARD_W = 328.0
CARD_HEADER = 62.0
CARD_ROW = 46.0
CARD_FOOTER = 34.0
CARD_MAX_ROWS = 3
CARD_RADIUS = 16.0

# 右键菜单设计尺寸
MENU_W = 196.0
MENU_PAD = 8.0
MENU_ITEM_H = 34.0
MENU_SEP_H = 9.0
MENU_RADIUS = 12.0

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT),
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
        ("hIconSm", ctypes.c_void_p),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", ctypes.c_void_p),
        ("fErase", wt.BOOL),
        ("rcPaint", wt.RECT),
        ("fRestore", wt.BOOL),
        ("fIncUpdate", wt.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("hwndTrack", ctypes.c_void_p),
        ("dwHoverTime", wt.DWORD),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
    ]


MONITOR_DEFAULTTONEAREST = 2


def _setup_prototypes() -> None:
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    user32.RegisterClassExW.restype = wt.WORD
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.CreateWindowExW.argtypes = [
        wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.UpdateLayeredWindow.restype = wt.BOOL
    # 第 8 个参数（BLENDFUNCTION*）同样用 c_void_p：shrink_anim.py 里有自己的
    # BLENDFUNCTION 结构，声明成 POINTER(自己的类) 两处会互相覆盖。
    user32.UpdateLayeredWindow.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
        ctypes.c_void_p, ctypes.POINTER(wt.POINT), wt.DWORD, ctypes.c_void_p, wt.DWORD,
    ]
    user32.GetDC.restype = ctypes.c_void_p
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.BeginPaint.restype = ctypes.c_void_p
    user32.BeginPaint.argtypes = [ctypes.c_void_p, ctypes.POINTER(PAINTSTRUCT)]
    user32.EndPaint.argtypes = [ctypes.c_void_p, ctypes.POINTER(PAINTSTRUCT)]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), ctypes.c_void_p, wt.UINT, wt.UINT]
    user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.ReleaseCapture.restype = wt.BOOL
    user32.SetWindowPos.restype = wt.BOOL
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wt.UINT,
    ]
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetCapture.restype = ctypes.c_void_p
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
    user32.InvalidateRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.BOOL]
    user32.LoadCursorW.restype = ctypes.c_void_p
    user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.SetCursor.restype = ctypes.c_void_p
    user32.SetCursor.argtypes = [ctypes.c_void_p]
    user32.DestroyWindow.argtypes = [ctypes.c_void_p]
    user32.PostMessageW.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
    user32.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, wt.DWORD]
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
    user32.GetDpiForWindow.restype = wt.UINT
    user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    # 第 2 个参数用 c_void_p 而不是 POINTER(BITMAPINFO)：shrink_anim.py 也会声明
    # 这个函数，两个模块的结构体类不同，声明成 POINTER(自己的类) 会互相打架
    # （后设置的覆盖前面的），一调用就 TypeError。
    gdi32.CreateDIBSection.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wt.UINT,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wt.DWORD,
    ]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    kernel32.CreateWaitableTimerExW.restype = ctypes.c_void_p
    kernel32.CreateWaitableTimerExW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.DWORD, wt.DWORD]
    kernel32.SetWaitableTimer.restype = wt.BOOL
    # 第 2 个参数（LARGE_INTEGER*）必须写成 c_void_p：shrink_anim.py 里有自己的
    # _LARGE_INTEGER 类，也会给同一个函数设 argtypes。ctypes.windll 是进程级共享，
    # 后设置的那个生效 —— 声明成 POINTER(自己的类) 就会让另一边的结构体传不进去，
    # 动画时钟线程第一帧抛 ArgumentError 直接退出（动画永久卡住、悬浮球点不动）。
    kernel32.SetWaitableTimer.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wt.LONG,
        ctypes.c_void_p, ctypes.c_void_p, wt.BOOL,
    ]
    kernel32.WaitForSingleObject.restype = wt.DWORD
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]
    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


_setup_prototypes()


def _color(hex_text: str) -> Color:
    return ColorTranslator.FromHtml(hex_text)


def _rounded_doc_path(left: float, top: float, width: float, height: float, radius: float) -> GraphicsPath:
    path = GraphicsPath()
    radius = max(0.0, min(radius, width / 2.0, height / 2.0))
    d = radius * 2
    if d <= 0.5:
        path.AddRectangle(RectangleF(left, top, width, height))
        return path
    path.AddArc(left, top, d, d, 180, 90)
    path.AddArc(left + width - d, top, d, d, 270, 90)
    path.AddArc(left + width - d, top + height - d, d, d, 0, 90)
    path.AddArc(left, top + height - d, d, d, 90, 90)
    path.CloseFigure()
    return path


def format_remaining(seconds: float) -> str:
    """把剩余秒数说成人话。负数表示已逾期。"""
    sec = int(seconds)
    if sec <= 0:
        sec = -sec
        if sec < 3600:
            return "已逾期 %d 分钟" % max(1, sec // 60)
        if sec < 86400:
            return "已逾期 %d 小时" % max(1, sec // 3600)
        return "已逾期 %d 天" % max(1, sec // 86400)
    if sec < 3600:
        return "还剩 %d 分钟" % max(1, sec // 60)
    if sec < 86400:
        return "还剩 %d 小时" % max(1, sec // 3600)
    return "还剩 %d 天" % max(1, sec // 86400)


class BallWindow:
    """80×80 的透明悬浮球；左键展开概览卡片，右键弹出操作菜单。"""

    CLASS_NAME = "HomeworkDeskBallWindow"

    def __init__(
        self,
        size: int = 80,
        position: tuple[int, int] = (0, 0),
        log: logging.Logger | None = None,
        data_provider=None,
        on_hide=None,
        on_moved=None,
        clamp=None,
        on_open_main=None,
        menu_provider=None,
        on_menu_action=None,
    ) -> None:
        self.size = int(size)
        self._log = log or logging.getLogger("homework-desk")
        self._data_provider = data_provider
        self._on_open_main = on_open_main
        self._on_hide = on_hide
        self._on_moved = on_moved
        self._clamp = clamp

        self._hwnd: int = 0
        self._ready = threading.Event()
        self._wake = threading.Event()
        self._closing = False
        self._clock = AnimClock(self._log)
        self._lock = threading.RLock()
        self._hbmp: int = 0
        self._bits: ctypes.c_void_p | None = None
        self._bits_ptr: IntPtr = IntPtr.Zero
        self._screen_dc: int = 0
        self._mem_dc: int = 0
        self._surface_size: tuple[int, int] = (0, 0)

        self._pos = (int(position[0]), int(position[1]))
        self._win_rect = (self._pos[0], self._pos[1], self.size, self.size)

        self._snapshot: dict = {}
        self._counts: tuple[int, int, int] | None = None
        self._failed = False

        self._hover = False
        self._dragging = False
        self._drag_start = (0, 0)
        self._drag_origin = (0, 0)
        self._drag_moved = False

        # 弹层状态（卡片 / 右键菜单共用同一套展开动画）
        self._ui_scale = 1.0
        self._popup_open = False
        self._popup_kind = ""  # "card" | "menu"
        self._pop_rect: tuple[int, int, int, int] | None = None
        self._pop_w = int(CARD_W)
        self._pop_h = int(CARD_HEADER + CARD_ROW + CARD_FOOTER)
        self._ball_rect: tuple[int, int, int, int] = self._win_rect
        self._items: list[dict] = []
        self._row_count = 0
        self._stats = {"today": 0, "urgent": 0, "overdue": 0}
        self._backdrop = None
        self._content_alpha = 0.0
        self._hover_row = -1
        self._hover_close = False
        self._lbutton_prev = False
        self._rbutton_prev = False
        self._escape_prev = False

        # 右键菜单
        self._menu_items: list[dict] = []
        self._menu_provider = menu_provider
        self._on_menu_action = on_menu_action
        self._hover_item = -1
        self._badge_font_cache = None

        # 动画状态
        self._animating = False
        self._anim_from = (0.0, 0.0, 0.0, 0.0)
        self._anim_to = (0.0, 0.0, 0.0, 0.0)
        self._anim_t0 = 0.0
        self._anim_dur = 0.2
        self._anim_expanding = True

        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._wndproc_ref = WNDPROC(self._wndproc)
        self._hand_cursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND))
        self._arrow_cursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))

    # ------------------------------------------------------------------ 生命周期
    @property
    def hwnd(self) -> int:
        return self._hwnd or 0

    @property
    def expanded(self) -> bool:
        return bool(self._popup_open or (self._animating and self._anim_expanding))

    def start(self, timeout: float = 5.0) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._thread = threading.Thread(target=self._run, name="homework-ball", daemon=True)
        self._thread.start()
        return self._ready.wait(timeout)

    def _run(self) -> None:
        try:
            self._thread_id = kernel32.GetCurrentThreadId()
            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.style = 0
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.hCursor = self._arrow_cursor
            wc.lpszClassName = self.CLASS_NAME
            if not user32.RegisterClassExW(ctypes.byref(wc)):
                err = ctypes.get_last_error()
                if err not in (0, 1410):  # 1410 = 已注册
                    self._log.error("注册悬浮球窗口类失败：%s", err)
                    return

            ex_style = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE
            hwnd = user32.CreateWindowExW(
                ex_style,
                self.CLASS_NAME,
                "作业备忘 · 悬浮球",
                WS_POPUP,
                self._pos[0],
                self._pos[1],
                self.size,
                self.size,
                None,
                None,
                hinst,
                None,
            )
            if not hwnd:
                self._log.error("创建悬浮球窗口失败")
                return
            self._hwnd = int(hwnd)
            self._ui_scale = self._dpi_scale()
            self._create_surface(self.size, self.size)
            self._render()
            user32.SetTimer(ctypes.c_void_p(self._hwnd), TIMER_BREATH, 60, None)
            self._ready.set()

            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            self._log.exception("悬浮球窗口线程异常退出")
        finally:
            self._ready.set()
            self._release_surface()
            self._hwnd = 0

    def destroy(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._clock.stop()
        self._animating = False
        hwnd = self._hwnd
        if hwnd:
            try:
                for timer in (TIMER_BREATH, TIMER_ANIM, TIMER_WATCH):
                    user32.KillTimer(ctypes.c_void_p(hwnd), timer)
                user32.PostMessageW(ctypes.c_void_p(hwnd), WM_CLOSE, 0, 0)
            except Exception:
                self._log.debug("关闭悬浮球失败", exc_info=True)

    def show(self) -> None:
        # 窗口状态只由窗口线程改，避免和展开动画抢同一块绘图表面。
        self._post_cmd(CMD_SHOW)

    def hide(self) -> None:
        self._post_cmd(CMD_HIDE)

    def _post_cmd(self, cmd: int) -> None:
        hwnd = self._hwnd
        if not hwnd or self._closing:
            return
        try:
            user32.PostMessageW(ctypes.c_void_p(hwnd), WM_BALL_CMD, cmd, 0)
        except Exception:
            self._log.debug("投递悬浮球命令失败：%s", cmd, exc_info=True)

    def _do_show(self) -> None:
        hwnd = self._hwnd
        if not hwnd or self._closing:
            return
        # 上次是展开状态的话，重新出现时先变回小球。
        if self._popup_open or self._animating:
            self._reset_to_ball()
        user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOWNOACTIVATE)
        user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self._render()

    def _do_hide(self) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        if self._popup_open or self._animating:
            self._reset_to_ball()
        user32.ShowWindow(ctypes.c_void_p(hwnd), SW_HIDE)

    # ------------------------------------------------------------------ 位置
    def move_to(self, x: int, y: int, notify: bool = False) -> tuple[int, int]:
        x, y = self._clamp_position(int(x), int(y))
        hwnd = self._hwnd
        if hwnd:
            user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOPMOST), x, y, 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        self._pos = (x, y)
        self._win_rect = (x, y, self._win_rect[2], self._win_rect[3])
        if notify and self._on_moved:
            try:
                self._on_moved(x, y)
            except Exception:
                self._log.debug("保存悬浮球位置失败", exc_info=True)
        return x, y

    def position(self) -> tuple[int, int]:
        return self._pos

    def _clamp_position(self, x: int, y: int) -> tuple[int, int]:
        if self._clamp:
            try:
                return self._clamp(self._hwnd, x, y)
            except Exception:
                pass
        return x, y

    def _dpi_scale(self) -> float:
        try:
            dpi = int(user32.GetDpiForWindow(ctypes.c_void_p(self._hwnd)) or 0)
            if dpi:
                return max(1.0, min(2.5, dpi / 96.0))
        except Exception:
            pass
        return 1.0

    def _work_area(self) -> tuple[int, int, int, int]:
        try:
            monitor = user32.MonitorFromWindow(ctypes.c_void_p(self._hwnd), MONITOR_DEFAULTTONEAREST)
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if monitor and user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
                rc = info.rcWork
                return int(rc.left), int(rc.top), int(rc.right), int(rc.bottom)
        except Exception:
            pass
        return 0, 0, int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))

    # ------------------------------------------------------------------ 数据
    def set_snapshot(self, data: dict | None) -> None:
        payload = dict(data or {})
        with self._lock:
            self._snapshot = payload
        self._apply_snapshot()
        self._post(WM_BALL_DATA)

    def set_counts(self, total: int, urgent: int, overdue: int, failed: bool = False) -> None:
        """兼容旧调用：只给三个数字。"""
        self.set_snapshot(
            {
                "total": int(total),
                "urgent": int(urgent),
                "overdue": int(overdue),
                "today": 0,
                "items": [],
                "failed": bool(failed),
            }
        )

    def _apply_snapshot(self) -> None:
        with self._lock:
            data = dict(self._snapshot)
        failed = bool(data.get("failed"))
        items = [it for it in (data.get("items") or []) if isinstance(it, dict)]
        total = int(data.get("total") or len(items))
        urgent = int(data.get("urgent") or 0)
        overdue = int(data.get("overdue") or 0)
        today = int(data.get("today") or 0)
        with self._lock:
            self._counts = (total, urgent, overdue)
            self._failed = failed
            self._stats = {"today": today, "urgent": urgent, "overdue": overdue}
            if self._popup_open:
                # 展开期间数据更新：行数固定，避免卡片高度跳变。
                rows = self._row_count
                self._items = (items + [{}] * rows)[:rows]
            else:
                self._row_count = min(CARD_MAX_ROWS, len(items))
                self._items = items[: self._row_count]

    def refresh(self) -> None:
        if self._hwnd:
            self._render()

    def request_refresh(self) -> None:
        self._wake.set()

    def poll_forever(self, interval: float = 30.0) -> None:
        while not self._closing:
            try:
                if self._data_provider:
                    result = self._data_provider()
                    if isinstance(result, dict):
                        self.set_snapshot(result)
                    elif result:
                        self.set_counts(*result[:3])
            except Exception:
                self.set_snapshot({"failed": True})
                self._log.debug("刷新悬浮球数据失败", exc_info=True)
            self._wake.wait(interval)
            self._wake.clear()

    def _post(self, message: int) -> None:
        hwnd = self._hwnd
        if hwnd:
            try:
                user32.PostMessageW(ctypes.c_void_p(hwnd), message, 0, 0)
            except Exception:
                pass

    # ------------------------------------------------------------------ 展开 / 收起
    def toggle_card(self) -> None:
        if self._popup_open:
            self.collapse()
        else:
            self.expand()

    def _open_popup(self, kind: str, width: int, height: int) -> None:
        """弹出层的公共部分：摆位置、拍背景、启动展开动画。"""
        self._popup_kind = kind
        self._pop_w = int(width)
        self._pop_h = int(height)
        self._ball_rect = self._win_rect
        rect = self._compute_pop_rect()
        self._pop_rect = rect
        # 先把球后面的桌面拍下来虚化：这才是“毛玻璃”的来源。
        self._backdrop = self._capture_backdrop(rect)
        self._popup_open = True
        self._content_alpha = 0.0
        self._hover_row = -1
        self._hover_close = False
        self._hover_item = -1
        self._start_animation(rect, ANIM_DURATION_OPEN, expanding=True)

    def expand(self) -> None:
        self._recover_stale_anim()
        if not self._hwnd or self._popup_open or self._animating:
            return
        s = self._ui_scale
        self._apply_snapshot()
        self._row_count = max(1, min(CARD_MAX_ROWS, len(self._items)))
        self._items = (self._items + [{}] * self._row_count)[: self._row_count]
        # 数据可能已经放了 30 秒，展开时顺手让后台刷新一次。
        self.request_refresh()
        self._open_popup(
            "card",
            int(round(CARD_W * s)),
            int(round((CARD_HEADER + self._row_count * CARD_ROW + CARD_FOOTER) * s)),
        )

    def open_menu(self) -> None:
        """右键：原地展开操作菜单。"""
        self._recover_stale_anim()
        if not self._hwnd or self._popup_open or self._animating:
            return
        items = self._load_menu_items()
        if not items:
            self._log.debug("右键菜单没有可用项")
            return
        s = self._ui_scale
        height = MENU_PAD * 2
        for item in items:
            height += MENU_SEP_H if item.get("sep") else MENU_ITEM_H
        self._menu_items = items
        self._open_popup(
            "menu",
            int(round(MENU_W * s)),
            int(round(height * s)),
        )

    def _load_menu_items(self) -> list[dict]:
        raw = None
        if self._menu_provider:
            try:
                raw = self._menu_provider()
            except Exception:
                self._log.debug("生成右键菜单项失败", exc_info=True)
                raw = None
        items: list[dict] = []
        for entry in raw or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("sep"):
                if items and not items[-1].get("sep"):
                    items.append({"sep": True})
                continue
            if not entry.get("label"):
                continue
            items.append(
                {
                    "action": str(entry.get("action") or ""),
                    "label": str(entry.get("label") or ""),
                    "icon": str(entry.get("icon") or ""),
                    "danger": bool(entry.get("danger")),
                    "bold": bool(entry.get("bold")),
                }
            )
        while items and items[-1].get("sep"):
            items.pop()
        return items

    def collapse(self) -> None:
        self._recover_stale_anim()
        if not self._hwnd or not self._popup_open or self._animating:
            return
        user32.KillTimer(ctypes.c_void_p(self._hwnd), TIMER_WATCH)
        self._start_animation(self._ball_rect, ANIM_DURATION_CLOSE, expanding=False)

    def _reset_to_ball(self) -> None:
        hwnd = self._hwnd
        if hwnd:
            for timer in (TIMER_ANIM, TIMER_WATCH):
                user32.KillTimer(ctypes.c_void_p(hwnd), timer)
        # 动画被人为打断（隐藏 / 重新显示）时把逐帧时钟收掉。
        self._clock.stop()
        self._animating = False
        self._popup_open = False
        self._popup_kind = ""
        self._pop_rect = None
        self._backdrop = None
        self._content_alpha = 0.0
        self._hover_row = -1
        self._hover_close = False
        self._hover_item = -1
        rect = self._ball_rect if self._ball_rect[2] else (self._pos[0], self._pos[1], self.size, self.size)
        self._win_rect = (int(rect[0]), int(rect[1]), self.size, self.size)
        if hwnd:
            user32.SetWindowPos(
                ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOPMOST),
                self._win_rect[0], self._win_rect[1], self.size, self.size,
                SWP_NOACTIVATE,
            )
        self._pos = (self._win_rect[0], self._win_rect[1])
        self._render()

    def _compute_pop_rect(self) -> tuple[int, int, int, int]:
        bx, by, bw, bh = self._ball_rect
        w, h = self._pop_w, self._pop_h
        left_w, top_w, right_w, bottom_w = self._work_area()
        # 球在屏幕右半边就把卡片往左下展开，否则往右下展开。
        if bx + bw / 2.0 > (left_w + right_w) / 2.0:
            left = bx + bw - w
        else:
            left = bx
        left = max(left_w, min(left, right_w - w))
        top = by
        if top + h > bottom_w:
            top = bottom_w - h
        top = max(top_w, min(top, bottom_w - h))
        return int(left), int(top), int(w), int(h)

    # ------------------------------------------------------------------ 动画
    # 逐帧时钟万一停摆（定时器创建失败、线程异常退出……），动画就会永远停在中间
    # 状态：_animating 一直是 True，之后 expand/collapse 全被短路 —— 表现就是
    # 悬浮球左右键彻底失灵。所以超过「时长 + 宽限期」还没收尾就强制落地。
    ANIM_STALE_GRACE = 0.6

    def _recover_stale_anim(self) -> None:
        if not self._animating:
            return
        elapsed = time.perf_counter() - self._anim_t0
        if elapsed <= self._anim_dur + self.ANIM_STALE_GRACE:
            return
        self._log.warning("展开/收起动画超时未收尾（%.2fs），强制复位并恢复交互", elapsed)
        self._finish_animation()

    def _start_animation(self, target: tuple[int, int, int, int], duration: float, *, expanding: bool) -> None:
        self._anim_from = tuple(float(v) for v in self._win_rect)
        self._anim_to = tuple(float(v) for v in target)
        self._anim_t0 = time.perf_counter()
        self._anim_dur = max(0.05, float(duration))
        self._anim_expanding = bool(expanding)
        self._animating = True
        hwnd = self._hwnd
        if hwnd:
            self._clock.start(hwnd, ANIM_INTERVAL_MS)

    def _tick_anim(self) -> None:
        if not self._animating:
            self._clock.stop()
            return
        elapsed = time.perf_counter() - self._anim_t0
        p = min(1.0, elapsed / self._anim_dur)
        if self._anim_expanding:
            # 展开：先快后慢，收尾轻一点
            eased = 1.0 - (1.0 - p) ** 3
        else:
            # 收起：先慢后快，像是被吸回球里
            eased = p ** 2.6
        rect = tuple(
            self._anim_from[i] + (self._anim_to[i] - self._anim_from[i]) * eased for i in range(4)
        )
        if self._anim_expanding:
            # 卡片先长出来，文字后淡入，避免小尺寸下文字挤压变形。
            self._content_alpha = 0.0 if p < 0.4 else min(1.0, (p - 0.4) / 0.45)
        else:
            self._content_alpha = max(0.0, 1.0 - p / 0.4)
        self._apply_rect(rect)
        if p >= 1.0:
            self._finish_animation()

    def _finish_animation(self) -> None:
        self._animating = False
        hwnd = self._hwnd
        self._clock.stop()
        if self._anim_expanding:
            self._content_alpha = 1.0
            self._apply_rect(tuple(float(v) for v in self._anim_to))
            if hwnd:
                # 开始监听“点到卡片外面”就收起。
                self._lbutton_prev = bool(user32.GetAsyncKeyState(VK_LBUTTON) & KEY_DOWN)
                self._rbutton_prev = bool(user32.GetAsyncKeyState(VK_RBUTTON) & KEY_DOWN)
                self._escape_prev = bool(user32.GetAsyncKeyState(VK_ESCAPE) & KEY_DOWN)
                user32.SetTimer(ctypes.c_void_p(hwnd), TIMER_WATCH, 40, None)
        else:
            self._popup_open = False
            self._popup_kind = ""
            self._pop_rect = None
            self._backdrop = None
            self._content_alpha = 0.0
            self._hover_row = -1
            self._hover_close = False
            self._hover_item = -1
            self._win_rect = (
                int(self._ball_rect[0]), int(self._ball_rect[1]), self.size, self.size
            )
            self._pos = (self._win_rect[0], self._win_rect[1])
            self._apply_rect(tuple(float(v) for v in self._win_rect))

    def _apply_rect(self, rect) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        x, y, w, h = (int(round(v)) for v in rect)
        w = max(self.size, w) if self._popup_open else max(1, w)
        h = max(self.size, h) if self._popup_open else max(1, h)
        user32.SetWindowPos(
            ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOPMOST), x, y, w, h, SWP_NOACTIVATE
        )
        self._win_rect = (x, y, w, h)
        if not self._popup_open and not self._animating:
            self._pos = (x, y)
        self._render()

    # ------------------------------------------------------------------ 外部点击监听
    def _tick_watch(self) -> None:
        if not self._popup_open or self._animating:
            user32.KillTimer(ctypes.c_void_p(self._hwnd), TIMER_WATCH)
            return
        # 左右键都算：点卡片/菜单外面就收起。
        left = bool(user32.GetAsyncKeyState(VK_LBUTTON) & KEY_DOWN)
        right = bool(user32.GetAsyncKeyState(VK_RBUTTON) & KEY_DOWN)
        pressed = (left and not self._lbutton_prev) or (right and not self._rbutton_prev)
        self._lbutton_prev = left
        self._rbutton_prev = right
        if pressed:
            cx, cy = self._cursor_pos()
            if not self._point_inside(cx, cy):
                self.collapse()
                return
        esc = bool(user32.GetAsyncKeyState(VK_ESCAPE) & KEY_DOWN)
        if esc and not self._escape_prev:
            self._escape_prev = esc
            self.collapse()
            return
        self._escape_prev = esc

    def _point_inside(self, sx: int, sy: int) -> bool:
        x, y, w, h = self._win_rect
        return x <= sx < x + w and y <= sy < y + h

    # ------------------------------------------------------------------ 绘制表面
    def _create_surface(self, width: int, height: int) -> None:
        self._release_surface()
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = int(width)
        bmi.bmiHeader.biHeight = -int(height)  # 负值 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB

        self._screen_dc = user32.GetDC(None)
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(
            ctypes.c_void_p(self._screen_dc),
            ctypes.cast(ctypes.pointer(bmi), ctypes.c_void_p),
            0, ctypes.byref(bits), None, 0,
        )
        if not hbmp:
            self._log.error("创建悬浮球绘图表面失败")
            return
        self._hbmp = int(hbmp)
        self._bits = bits
        self._bits_ptr = IntPtr(int(bits.value or 0))
        self._mem_dc = gdi32.CreateCompatibleDC(ctypes.c_void_p(self._screen_dc))
        gdi32.SelectObject(ctypes.c_void_p(self._mem_dc), ctypes.c_void_p(self._hbmp))
        self._surface_size = (int(width), int(height))

    def _release_surface(self) -> None:
        try:
            if self._hbmp:
                gdi32.DeleteObject(ctypes.c_void_p(self._hbmp))
            if self._mem_dc:
                gdi32.DeleteDC(ctypes.c_void_p(self._mem_dc))
            if self._screen_dc:
                user32.ReleaseDC(None, ctypes.c_void_p(self._screen_dc))
        except Exception:
            pass
        self._hbmp = 0
        self._mem_dc = 0
        self._screen_dc = 0
        self._surface_size = (0, 0)

    def _ensure_surface(self) -> None:
        need_w = max(self.size, int(self._win_rect[2]), self._pop_w)
        need_h = max(self.size, int(self._win_rect[3]), self._pop_h)
        if self._hbmp and self._surface_size == (need_w, need_h):
            return
        self._create_surface(need_w, need_h)

    def _render(self) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        self._ensure_surface()
        if not self._hbmp or not self._bits_ptr:
            return
        width, height = self._surface_size
        with self._lock:
            bmp = Bitmap(width, height, width * 4, PixelFormat.Format32bppPArgb, self._bits_ptr)
            try:
                g = Graphics.FromImage(bmp)
                try:
                    g.SmoothingMode = SmoothingMode.AntiAlias
                    g.PixelOffsetMode = PixelOffsetMode.HighQuality
                    g.TextRenderingHint = TextRenderingHint.AntiAlias
                    g.Clear(Color.Transparent)
                    if self._popup_open:
                        if self._popup_kind == "menu":
                            self._paint_menu(g)
                        else:
                            self._paint_card(g)
                    else:
                        self._paint(g)
                finally:
                    g.Dispose()
                self._update_layered()
            finally:
                bmp.Dispose()

    # ------------------------------------------------------------------ 小球
    def _paint(self, g) -> None:
        size = self.size
        total, urgent, overdue = self._counts or (0, 0, 0)
        grew = 1.6 if self._hover else 0.0
        diameter = size - 16 + grew * 2
        left = (size - diameter) / 2
        top = (size - diameter) / 2

        # 柔和投影（真 alpha，随圆形渐变淡出）
        shadow = GraphicsPath()
        shadow.AddEllipse(left + 1.0, top + 4.0, diameter - 2.0, diameter - 2.0)
        shadow_brush = PathGradientBrush(shadow)
        shadow_brush.CenterColor = Color.FromArgb(92, 16, 32, 26)
        shadow_brush.SurroundColors = [Color.FromArgb(0, 16, 32, 26)]
        g.FillPath(shadow_brush, shadow)

        body = RectangleF(left, top, diameter, diameter)
        gradient = LinearGradientBrush(body, _color(BALL_COLOR_A), _color(BALL_COLOR_B), 150.0)
        g.FillEllipse(gradient, body)

        # 左上高光
        highlight = GraphicsPath()
        highlight.AddEllipse(left + diameter * 0.12, top + diameter * 0.06,
                             diameter * 0.70, diameter * 0.58)
        highlight_brush = PathGradientBrush(highlight)
        highlight_brush.CenterColor = Color.FromArgb(88, 255, 255, 255)
        highlight_brush.SurroundColors = [Color.FromArgb(0, 255, 255, 255)]
        g.FillPath(highlight_brush, highlight)

        rim = Pen(Color.FromArgb(64, 255, 255, 255), 1.0)
        g.DrawEllipse(rim, left + 0.5, top + 0.5, diameter - 1.0, diameter - 1.0)

        # 紧急时的呼吸环
        if urgent > 0 and not self._failed:
            phase = (time.time() * 0.42) % 1.0
            ring_r = diameter / 2 + 2.0 + phase * 5.0
            alpha = int(125 * (1.0 - phase))
            ring = Pen(Color.FromArgb(alpha, 207, 91, 78), 1.6)
            cx = left + diameter / 2
            cy = top + diameter / 2
            g.DrawEllipse(ring, cx - ring_r, cy - ring_r, ring_r * 2, ring_r * 2)

        self._paint_glyph(g, left, top, diameter)
        self._paint_badge(g, int(total), int(urgent), int(overdue))

    def _paint_glyph(self, g, left: float, top: float, diameter: float) -> None:
        width = diameter * 0.30
        height = diameter * 0.38
        x = left + (diameter - width) / 2
        y = top + (diameter - height) / 2
        pen = Pen(Color.FromArgb(245, 255, 255, 255), max(1.6, diameter * 0.026))
        pen.LineJoin = LineJoin.Round
        pen.StartCap = LineCap.Round
        pen.EndCap = LineCap.Round
        path = _rounded_doc_path(x, y, width, height, width * 0.18)
        g.DrawPath(pen, path)

        line_pen = Pen(Color.FromArgb(225, 255, 255, 255), max(1.4, diameter * 0.022))
        line_pen.StartCap = LineCap.Round
        line_pen.EndCap = LineCap.Round
        for index, ratio in enumerate((0.36, 0.56, 0.74)):
            line_y = y + height * ratio
            line_width = width * (0.62 if index < 2 else 0.42)
            g.DrawLine(line_pen, x + width * 0.19, line_y, x + width * 0.19 + line_width, line_y)

    def _paint_badge(self, g, total: int, urgent: int, overdue: int) -> None:
        if self._failed:
            text, fill, ink = "!", _color(AMBER), _color(AMBER_INK)
        elif total <= 0:
            return
        else:
            text = "99+" if total > 99 else str(total)
            if urgent + overdue > 0:
                fill, ink = _color(DANGER), Color.White
            else:
                fill, ink = Color.White, _color(BADGE_INK)

        height = 21.0
        width = height if len(text) <= 2 else height + 9.0
        right = self.size - 3.0
        top = 3.0
        left = right - width

        path = GraphicsPath()
        path.AddArc(left, top, height, height, 90, 180)
        path.AddArc(left + width - height, top, height, height, 270, 180)
        path.CloseFigure()

        shadow = PathGradientBrush(path)
        shadow.CenterColor = Color.FromArgb(70, 14, 28, 22)
        shadow.SurroundColors = [Color.FromArgb(0, 14, 28, 22)]
        g.FillPath(shadow, path)

        g.FillPath(SolidBrush(fill), path)
        ring = Pen(Color.FromArgb(190, 255, 255, 255) if fill != Color.White else Color.FromArgb(150, 43, 86, 71), 1.6)
        g.DrawPath(ring, path)

        fmt = StringFormat()
        fmt.Alignment = StringAlignment.Center
        fmt.LineAlignment = StringAlignment.Center
        font = self._badge_font()
        brush = SolidBrush(ink)
        g.DrawString(text, font, brush, RectangleF(left, top - 0.5, width, height), fmt)

    def _badge_font(self):
        if self._badge_font_cache is not None:
            return self._badge_font_cache
        font = None
        for family in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"):
            try:
                font = Font(family, 11.5, FontStyle.Bold, GraphicsUnit.Pixel)
                break
            except Exception:
                continue
        if font is None:
            font = Font("Arial", 11.5, FontStyle.Bold, GraphicsUnit.Pixel)
        self._badge_font_cache = font
        return font

    # ------------------------------------------------------------------ 卡片
    def _content_offset(self) -> tuple[float, float]:
        """卡片的 (0,0) 在窗口坐标系里的偏移。展开动画期间窗口比卡片小。"""
        if not self._pop_rect:
            return 0.0, 0.0
        return float(self._win_rect[0] - self._pop_rect[0]), float(self._win_rect[1] - self._pop_rect[1])

    def _rgba(self, r: int, g: int, b: int, alpha: float, fade: float = 1.0) -> Color:
        a = int(max(0.0, min(255.0, alpha * fade)))
        return Color.FromArgb(a, int(r), int(g), int(b))

    def _ui_font(self, size: float, bold: bool = False):
        cache = self.__dict__.setdefault("_fonts", {})
        key = (round(float(size), 1), bool(bold))
        font = cache.get(key)
        if font is not None:
            return font
        style = FontStyle.Bold if bold else FontStyle.Regular
        font = None
        for family in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"):
            try:
                font = Font(family, float(size), style, GraphicsUnit.Pixel)
                break
            except Exception:
                continue
        if font is None:
            font = Font("Arial", float(size), style, GraphicsUnit.Pixel)
        cache[key] = font
        return font

    def _text(self, g, text, font, color, x, y, width, height,
              align=StringAlignment.Near, ellipsis=True) -> None:
        if not text:
            return
        fmt = StringFormat()
        fmt.Alignment = align
        fmt.LineAlignment = StringAlignment.Near
        fmt.FormatFlags = StringFormatFlags.NoWrap
        if ellipsis:
            fmt.Trimming = StringTrimming.EllipsisCharacter
        g.DrawString(str(text), font, SolidBrush(color), RectangleF(float(x), float(y), float(width), float(height)), fmt)

    def _paint_card(self, g) -> None:
        s = self._ui_scale
        fade = max(0.0, min(1.0, self._content_alpha))
        ww, wh = int(self._win_rect[2]), int(self._win_rect[3])
        dx, dy = self._content_offset()
        cw, ch = self._pop_w, self._pop_h

        # 1) 虚化后的桌面截图（真实“背景模糊”）
        if self._backdrop is not None:
            try:
                g.DrawImage(self._backdrop, RectangleF(dx, dy, float(cw), float(ch)))
            except Exception:
                self._log.debug("绘制卡片背景失败", exc_info=True)

        # 2) 深色半透明玻璃层
        radius = min(CARD_RADIUS * s, ww / 2.0, wh / 2.0)
        shell = _rounded_doc_path(0.5, 0.5, max(1.0, ww - 1.0), max(1.0, wh - 1.0), radius)
        body = RectangleF(0.0, 0.0, float(ww), float(wh))
        glass = LinearGradientBrush(body, Color.FromArgb(188, 27, 32, 35), Color.FromArgb(202, 15, 19, 22), 90.0)
        g.FillPath(glass, shell)
        g.DrawPath(Pen(Color.FromArgb(56, 255, 255, 255), 1.0), shell)

        if fade <= 0.02:
            return

        x0 = dx + 16.0 * s
        x1 = dx + cw - 16.0 * s

        # 3) 标题 + 统计
        self._text(g, "作业备忘", self._ui_font(15.5 * s, bold=True),
                   self._rgba(238, 245, 240, 250, fade), x0, dy + 10.0 * s,
                   cw - 32.0 * s, 22.0 * s)
        self._paint_stats(g, x0, dy + 33.0 * s, s, fade)

        divider = Pen(self._rgba(255, 255, 255, 30, fade), 1.0)
        g.DrawLine(divider, x0, dy + 54.0 * s, x1, dy + 54.0 * s)

        # 4) 作业行
        for index in range(self._row_count):
            item = self._items[index] if index < len(self._items) else {}
            self._paint_row(g, item, index, dx, dy, cw, s, fade)

        # 5) 底部提示
        hint = "点条目打开主界面 · 点外部区域收起"
        self._text(g, hint, self._ui_font(10.5 * s),
                   self._rgba(150, 168, 158, 210, fade),
                   x0, dy + ch - 24.0 * s, cw - 32.0 * s, 16.0 * s)

        # 6) 收起按钮（就是原来悬浮球的位置）
        self._paint_close_button(g, ww, s, fade)

    def _paint_stats(self, g, x, y, s, fade) -> None:
        stats = self._stats or {}
        label_font = self._ui_font(11.0 * s)
        value_font = self._ui_font(11.0 * s, bold=True)
        label_color = self._rgba(146, 164, 155, 220, fade)
        chunks = [
            ("今日到期", int(stats.get("today") or 0), self._rgba(240, 192, 74, 245, fade)),
            ("紧急", int(stats.get("urgent") or 0), self._rgba(232, 150, 92, 245, fade)),
            ("逾期", int(stats.get("overdue") or 0), self._rgba(224, 121, 107, 245, fade)),
        ]
        cursor = float(x)
        for index, (label, value, color) in enumerate(chunks):
            if index:
                dot = "·"
                g.DrawString(dot, label_font, SolidBrush(label_color), PointF(cursor, y))
                cursor += g.MeasureString(dot, label_font).Width + 3.0 * s
            g.DrawString(label, label_font, SolidBrush(label_color), PointF(cursor, y))
            cursor += g.MeasureString(label, label_font).Width + 1.0 * s
            text = str(value)
            g.DrawString(text, value_font, SolidBrush(color), PointF(cursor, y))
            cursor += g.MeasureString(text, value_font).Width + 6.0 * s

    def _paint_row(self, g, item: dict, index: int, dx, dy, cw, s, fade) -> None:
        row_y = dy + (CARD_HEADER + index * CARD_ROW) * s
        left = dx + 11.0 * s
        width = cw - 22.0 * s
        height = (CARD_ROW - 4.0) * s

        if self._hover_row == index:
            hover = _rounded_doc_path(left, row_y, width, height, 10.0 * s)
            g.FillPath(SolidBrush(self._rgba(255, 255, 255, 16, fade)), hover)

        remaining = item.get("remaining")
        overdue = bool(item.get("overdue"))
        urgent = bool(item.get("urgent"))
        if remaining is None:
            accent = self._rgba(120, 140, 130, 150, fade)
        elif overdue:
            accent = self._rgba(207, 91, 78, 235, fade)
        elif urgent:
            accent = self._rgba(240, 192, 74, 235, fade)
        else:
            accent = self._rgba(95, 138, 118, 235, fade)

        # 左侧色条
        bar = _rounded_doc_path(dx + 16.0 * s, row_y + 9.0 * s, 3.0 * s, 22.0 * s, 1.5 * s)
        g.FillPath(SolidBrush(accent), bar)

        name = str(item.get("name") or item.get("title") or "").strip()
        course = str(item.get("course") or "").strip()
        time_text = format_remaining(float(remaining)) if remaining is not None else ""

        text_left = dx + 27.0 * s
        text_width = cw - 27.0 * s - 16.0 * s - 88.0 * s
        if name:
            self._text(g, name, self._ui_font(13.0 * s, bold=True),
                       self._rgba(233, 240, 235, 245, fade),
                       text_left, row_y + 3.0 * s, text_width, 20.0 * s)
        else:
            self._text(g, "暂无未完成作业", self._ui_font(13.0 * s),
                       self._rgba(160, 178, 168, 220, fade),
                       text_left, row_y + 4.0 * s, text_width, 20.0 * s)
        sub = course or ("全部完成，休息一下吧" if not name else "")
        if sub:
            self._text(g, sub, self._ui_font(10.5 * s),
                       self._rgba(142, 161, 151, 205, fade),
                       text_left, row_y + 24.0 * s, text_width, 16.0 * s)

        if time_text:
            if overdue:
                time_color = self._rgba(224, 121, 107, 240, fade)
            elif urgent:
                time_color = self._rgba(240, 192, 74, 240, fade)
            else:
                time_color = self._rgba(157, 176, 166, 220, fade)
            self._text(g, time_text, self._ui_font(11.0 * s, bold=overdue or urgent), time_color,
                       dx + cw - 16.0 * s - 84.0 * s, row_y + 7.0 * s, 84.0 * s, 18.0 * s,
                       align=StringAlignment.Far, ellipsis=False)

    def _close_button_center(self, ww: int, s: float) -> tuple[float, float]:
        return ww - 16.0 * s - 11.0 * s, 10.0 * s + 11.0 * s

    def _paint_close_button(self, g, ww: int, s: float, fade) -> None:
        cx, cy = self._close_button_center(ww, s)
        radius = 11.0 * s
        alpha = 44 if self._hover_close else 20
        g.FillEllipse(SolidBrush(self._rgba(255, 255, 255, alpha, fade)),
                      cx - radius, cy - radius, radius * 2, radius * 2)
        pen = Pen(self._rgba(226, 236, 230, 205, fade), 1.5 * s)
        pen.StartCap = LineCap.Round
        pen.EndCap = LineCap.Round
        arm = 3.6 * s
        g.DrawLine(pen, cx - arm, cy - arm, cx + arm, cy + arm)
        g.DrawLine(pen, cx + arm, cy - arm, cx - arm, cy + arm)

    # ------------------------------------------------------------------ 右键菜单
    def _menu_layout(self) -> list[tuple[int, float, float, dict]]:
        """返回 [(index, top, height, item)]，坐标为窗口坐标。"""
        s = self._ui_scale
        dx, dy = self._content_offset()
        y = dy + MENU_PAD * s
        rows: list[tuple[int, float, float, dict]] = []
        for index, item in enumerate(self._menu_items):
            height = (MENU_SEP_H if item.get("sep") else MENU_ITEM_H) * s
            rows.append((index, y, height, item))
            y += height
        return rows

    def _menu_index_at(self, x: float, y: float) -> int:
        s = self._ui_scale
        dx, _dy = self._content_offset()
        left = dx + MENU_PAD * s
        if x < left or x > left + (MENU_W - MENU_PAD * 2) * s:
            return -1
        for index, top, height, item in self._menu_layout():
            if item.get("sep"):
                continue
            if top <= y < top + height:
                return index
        return -1

    def _paint_menu(self, g) -> None:
        s = self._ui_scale
        fade = max(0.0, min(1.0, self._content_alpha))
        ww, wh = int(self._win_rect[2]), int(self._win_rect[3])
        dx, dy = self._content_offset()
        cw, ch = self._pop_w, self._pop_h

        if self._backdrop is not None:
            try:
                g.DrawImage(self._backdrop, RectangleF(dx, dy, float(cw), float(ch)))
            except Exception:
                self._log.debug("绘制菜单背景失败", exc_info=True)

        radius = min(MENU_RADIUS * s, ww / 2.0, wh / 2.0)
        shell = _rounded_doc_path(0.5, 0.5, max(1.0, ww - 1.0), max(1.0, wh - 1.0), radius)
        body = RectangleF(0.0, 0.0, float(ww), float(wh))
        glass = LinearGradientBrush(body, Color.FromArgb(198, 28, 33, 36), Color.FromArgb(210, 16, 20, 23), 90.0)
        g.FillPath(glass, shell)
        g.DrawPath(Pen(Color.FromArgb(58, 255, 255, 255), 1.0), shell)

        if fade <= 0.02:
            return

        left = dx + MENU_PAD * s
        width = (MENU_W - MENU_PAD * 2) * s
        for index, top, height, item in self._menu_layout():
            if item.get("sep"):
                pen = Pen(self._rgba(255, 255, 255, 30, fade), 1.0)
                mid = top + height / 2.0
                g.DrawLine(pen, left + 4 * s, mid, left + width - 4 * s, mid)
                continue
            if self._hover_item == index:
                hover = _rounded_doc_path(left, top + 1 * s, width, height - 2 * s, 8 * s)
                g.FillPath(SolidBrush(self._rgba(255, 255, 255, 20, fade)), hover)
            self._paint_menu_icon(g, item.get("icon"), left + 11 * s, top + height / 2.0, s, fade,
                                  bool(item.get("danger")))
            if item.get("danger"):
                color = self._rgba(232, 118, 105, 245, fade)
            elif item.get("bold"):
                color = self._rgba(240, 246, 242, 250, fade)
            else:
                color = self._rgba(222, 232, 226, 240, fade)
            text_x = left + (11 + 17 + 10) * s
            self._text(g, item.get("label"), self._ui_font(12.5 * s, bold=bool(item.get("bold"))),
                       color, text_x, top + (height - 18 * s) / 2.0,
                       width - (11 + 17 + 10) * s, 18 * s)

    def _paint_menu_icon(self, g, kind: str, x: float, cy: float, s: float, fade: float, danger: bool) -> None:
        if danger:
            color = self._rgba(232, 118, 105, 235, fade)
        else:
            color = self._rgba(150, 172, 161, 235, fade)
        pen = Pen(color, 1.5 * s)
        pen.StartCap = LineCap.Round
        pen.EndCap = LineCap.Round
        pen.LineJoin = LineJoin.Round
        top = cy - 8 * s
        if kind == "open":
            path = _rounded_doc_path(x, top + 2 * s, 15 * s, 12 * s, 2.5 * s)
            g.DrawPath(pen, path)
            g.DrawLine(pen, x + 1 * s, top + 6 * s, x + 14 * s, top + 6 * s)
        elif kind == "hide":
            path = _rounded_doc_path(x + 1 * s, top + 4 * s, 13 * s, 10 * s, 2.5 * s)
            g.DrawPath(pen, path)
            g.DrawLine(pen, x + 2 * s, top + 14 * s, x + 13 * s, top + 3 * s)
        elif kind == "quit":
            g.DrawArc(pen, x + 1 * s, top + 3 * s, 13 * s, 13 * s, -62.0, 304.0)
            g.DrawLine(pen, x + 7.5 * s, top + 1 * s, x + 7.5 * s, top + 8 * s)
        elif kind == "clock":
            g.DrawEllipse(pen, x + 1 * s, top + 1 * s, 14 * s, 14 * s)
            g.DrawLine(pen, x + 8 * s, top + 4.5 * s, x + 8 * s, top + 8 * s)
        elif kind == "refresh":
            g.DrawArc(pen, x + 1 * s, top + 1 * s, 14 * s, 14 * s, 40.0, 260.0)
        else:
            g.DrawEllipse(pen, x + 4 * s, top + 4 * s, 8 * s, 8 * s)

    # ------------------------------------------------------------------ 桌面虚化
    def _capture_backdrop(self, rect):
        """截取卡片将要覆盖的桌面并模糊，作为毛玻璃底。"""
        x, y, w, h = rect
        try:
            shot = Bitmap(int(w), int(h), PixelFormat.Format32bppPArgb)
            g = Graphics.FromImage(shot)
            try:
                g.CopyFromScreen(int(x), int(y), 0, 0, Size(int(w), int(h)), CopyPixelOperation.SourceCopy)
            finally:
                g.Dispose()
            self._patch_ball_area(shot, x, y)
            # 多级重采样 ≈ 高斯模糊，代价比逐像素卷积低得多。
            small = self._resample(shot, max(8, int(w / 10)), max(8, int(h / 10)))
            mid = self._resample(small, max(16, int(w / 4)), max(16, int(h / 4)))
            blurred = self._resample(mid, int(w), int(h))
            return blurred
        except Exception:
            self._log.debug("截取卡片背景失败", exc_info=True)
            return None

    def _resample(self, source, width: int, height: int):
        out = Bitmap(max(1, width), max(1, height), PixelFormat.Format32bppPArgb)
        g = Graphics.FromImage(out)
        try:
            g.InterpolationMode = InterpolationMode.HighQualityBicubic
            g.PixelOffsetMode = PixelOffsetMode.HighQuality
            g.CompositingQuality = CompositingQuality.HighQuality
            g.DrawImage(source, Rectangle(0, 0, out.Width, out.Height))
        finally:
            g.Dispose()
        return out

    def _patch_ball_area(self, shot, origin_x: int, origin_y: int) -> None:
        """截图时球还在原位，先把这块涂成周边平均色，免得虚化后留一团绿影。"""
        bx, by, bw, bh = self._ball_rect
        px, py = bx - origin_x, by - origin_y
        if px + bw <= 0 or py + bh <= 0 or px >= shot.Width or py >= shot.Height:
            return
        samples = []
        for ox, oy in ((-5, -5), (bw + 5, -5), (-5, bh + 5), (bw + 5, bh + 5),
                       (bw // 2, -7), (bw // 2, bh + 7), (-7, bh // 2), (bw + 7, bh // 2)):
            qx = max(0, min(shot.Width - 1, px + ox))
            qy = max(0, min(shot.Height - 1, py + oy))
            try:
                samples.append(shot.GetPixel(qx, qy))
            except Exception:
                continue
        if not samples:
            return
        r = sum(c.R for c in samples) // len(samples)
        gg = sum(c.G for c in samples) // len(samples)
        b = sum(c.B for c in samples) // len(samples)
        try:
            gr = Graphics.FromImage(shot)
            try:
                gr.FillRectangle(SolidBrush(Color.FromArgb(255, r, gg, b)), px, py, bw, bh)
            finally:
                gr.Dispose()
        except Exception:
            pass

    def _update_layered(self) -> None:
        hwnd = self._hwnd
        if not hwnd or not self._mem_dc or not self._screen_dc:
            return
        x, y, w, h = self._win_rect
        w, h = max(1, int(w)), max(1, int(h))
        dst = wt.POINT(int(x), int(y))
        size = wt.SIZE(w, h)
        src = wt.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(
            ctypes.c_void_p(hwnd),
            ctypes.c_void_p(self._screen_dc),
            ctypes.byref(dst),
            ctypes.byref(size),
            ctypes.c_void_p(self._mem_dc),
            ctypes.byref(src),
            0,
            ctypes.cast(ctypes.pointer(blend), ctypes.c_void_p),
            ULW_ALPHA,
        )

    # ------------------------------------------------------------------ 消息
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_PAINT:
                ps = PAINTSTRUCT()
                user32.BeginPaint(ctypes.c_void_p(hwnd), ctypes.byref(ps))
                user32.EndPaint(ctypes.c_void_p(hwnd), ctypes.byref(ps))
                # 改变窗口尺寸会招来 WM_PAINT；动画期间定时器已经每帧重画，
                # 这里再画一次等于白费一半渲染时间，直接跳过。
                if not self._animating:
                    self._render()
                return 0
            if msg == WM_BALL_DATA:
                if not self._animating:
                    self._render()
                return 0
            if msg == WM_BALL_CMD:
                if wparam == CMD_SHOW:
                    self._do_show()
                elif wparam == CMD_HIDE:
                    self._do_hide()
                return 0
            if msg == WM_BALL_TICK:
                self._tick_anim()
                return 0
            if msg == WM_TIMER:
                if wparam == TIMER_ANIM:
                    self._tick_anim()
                elif wparam == TIMER_WATCH:
                    self._tick_watch()
                elif wparam == TIMER_BREATH:
                    total, urgent, overdue = self._counts or (0, 0, 0)
                    if urgent > 0 and not self._popup_open:
                        self._render()
                return 0
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_SETCURSOR:
                if not self._popup_open:
                    user32.SetCursor(ctypes.c_void_p(self._hand_cursor))
                    return 1
                return user32.DefWindowProcW(ctypes.c_void_p(hwnd), msg, wparam, lparam)
            if msg == WM_MOUSEMOVE:
                x, y = self._lparam_point(lparam)
                if self._popup_open:
                    self._update_pop_hover(x, y)
                elif not self._hover:
                    self._hover = True
                    track = TRACKMOUSEEVENT()
                    track.cbSize = ctypes.sizeof(TRACKMOUSEEVENT)
                    track.dwFlags = TME_LEAVE
                    track.hwndTrack = ctypes.c_void_p(self._hwnd)
                    user32.TrackMouseEvent(ctypes.byref(track))
                    self._render()
                if self._dragging:
                    self._drag_step()
                return 0
            if msg == WM_MOUSELEAVE:
                if self._popup_open:
                    if self._hover_row != -1 or self._hover_close or self._hover_item != -1:
                        self._hover_row = -1
                        self._hover_close = False
                        self._hover_item = -1
                        self._render()
                elif not self._dragging and self._hover:
                    self._hover = False
                    self._render()
                return 0
            if msg == WM_LBUTTONDOWN:
                if not self._popup_open:
                    self._begin_drag()
                return 0
            if msg == WM_LBUTTONUP:
                if self._popup_open:
                    self._popup_click(*self._lparam_point(lparam))
                else:
                    self._end_drag()
                return 0
            if msg == WM_RBUTTONUP:
                if self._popup_open:
                    self.collapse()
                elif self._menu_provider:
                    self.open_menu()
                elif self._on_hide:
                    threading.Thread(target=self._safe_call, args=(self._on_hide,), daemon=True).start()
                return 0
            if msg == WM_CLOSE:
                for timer in (TIMER_BREATH, TIMER_ANIM, TIMER_WATCH):
                    user32.KillTimer(ctypes.c_void_p(hwnd), timer)
                user32.DestroyWindow(ctypes.c_void_p(hwnd))
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            self._log.debug("悬浮球消息处理异常 msg=%s", msg, exc_info=True)
        return user32.DefWindowProcW(ctypes.c_void_p(hwnd), msg, wparam, lparam)

    @staticmethod
    def _lparam_point(lparam) -> tuple[int, int]:
        x = lparam & 0xFFFF
        y = (lparam >> 16) & 0xFFFF
        if x >= 0x8000:
            x -= 0x10000
        if y >= 0x8000:
            y -= 0x10000
        return int(x), int(y)

    def _popup_click(self, x: int, y: int) -> None:
        if self._popup_kind == "menu":
            self._menu_click(x, y)
            return
        s = self._ui_scale
        cx, cy = self._close_button_center(int(self._win_rect[2]), s)
        if abs(x - cx) <= 13 * s and abs(y - cy) <= 13 * s:
            self.collapse()
            return
        dx, dy = self._content_offset()
        for index in range(self._row_count):
            row_y = dy + (CARD_HEADER + index * CARD_ROW) * s
            if row_y - 3 * s <= y <= row_y + (CARD_ROW - 5) * s:
                if self._row_count and index < len(self._items) and self._items[index].get("name"):
                    self.collapse()
                    if self._on_open_main:
                        threading.Thread(target=self._safe_call, args=(self._on_open_main,), daemon=True).start()
                return

    def _menu_click(self, x: int, y: int) -> None:
        index = self._menu_index_at(x, y)
        if index < 0 or index >= len(self._menu_items):
            return
        item = self._menu_items[index]
        action = item.get("action")
        self.collapse()
        if action and self._on_menu_action:
            threading.Thread(
                target=self._safe_call, args=(lambda: self._on_menu_action(action),), daemon=True
            ).start()

    def _update_pop_hover(self, x: int, y: int) -> None:
        s = self._ui_scale
        track = TRACKMOUSEEVENT()
        track.cbSize = ctypes.sizeof(TRACKMOUSEEVENT)
        track.dwFlags = TME_LEAVE
        track.hwndTrack = ctypes.c_void_p(self._hwnd)
        user32.TrackMouseEvent(ctypes.byref(track))
        if self._popup_kind == "menu":
            item = self._menu_index_at(x, y)
            if item != self._hover_item:
                self._hover_item = item
                self._render()
            user32.SetCursor(ctypes.c_void_p(self._hand_cursor if item >= 0 else self._arrow_cursor))
            return
        cx, cy = self._close_button_center(int(self._win_rect[2]), s)
        close = abs(x - cx) <= 13 * s and abs(y - cy) <= 13 * s
        dx, dy = self._content_offset()
        row = -1
        for index in range(self._row_count):
            row_y = dy + (CARD_HEADER + index * CARD_ROW) * s
            if row_y - 3 * s <= y <= row_y + (CARD_ROW - 5) * s:
                row = index
                break
        if row != self._hover_row or close != self._hover_close:
            self._hover_row = row
            self._hover_close = close
            self._render()
        clickable = close or (row >= 0 and row < len(self._items) and self._items[row].get("name"))
        user32.SetCursor(ctypes.c_void_p(self._hand_cursor if clickable else self._arrow_cursor))

    def _safe_call(self, fn) -> None:
        try:
            fn()
        except Exception:
            self._log.debug("悬浮球回调失败", exc_info=True)

    def _cursor_pos(self) -> tuple[int, int]:
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return int(pt.x), int(pt.y)

    def _begin_drag(self) -> None:
        self._dragging = True
        self._drag_moved = False
        self._drag_start = self._cursor_pos()
        self._drag_origin = self._pos
        user32.SetCapture(ctypes.c_void_p(self._hwnd))

    def _drag_step(self) -> None:
        cx, cy = self._cursor_pos()
        dx = cx - self._drag_start[0]
        dy = cy - self._drag_start[1]
        if not self._drag_moved and abs(dx) + abs(dy) < 4:
            return
        self._drag_moved = True
        self.move_to(self._drag_origin[0] + dx, self._drag_origin[1] + dy)

    def _end_drag(self) -> None:
        if not self._dragging:
            return
        self._dragging = False
        user32.ReleaseCapture()
        if self._drag_moved:
            self.move_to(*self._pos, notify=True)
            self._hover = False
            self._render()
        else:
            # 点一下球 = 原地展开概览卡片（不打开新窗口）
            self.expand()
