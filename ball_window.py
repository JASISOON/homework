"""桌面悬浮球窗口。

为什么不用 WebView2：
    pywebview（HWND 宿主模式）的 WebView2 不支持窗口逐像素透明 ——
    即使设置 ``transparent=True``、把页面背景设成 ``transparent``，
    窗口底色仍然是 WebView2 自己的不透明表面（实测是一块深灰方块），
    色键（``LWA_COLORKEY``）也对子窗口内容无效。
    所以这里改用原生 Win32 分层窗口 + GDI+ 直接绘制，
    通过 ``UpdateLayeredWindow`` 提交带 alpha 的位图，得到真正的圆球和柔和阴影。

对外接口（供 desktop_app 使用）：
    hwnd / show() / hide() / destroy() / move_to(x, y) / set_position(x, y)
    回调：on_click / on_hide / on_moved(x, y) / data_provider() -> (total, urgent, overdue)
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
    Font,
    FontStyle,
    Graphics,
    GraphicsUnit,
    Pen,
    PointF,
    RectangleF,
    SizeF,
    SolidBrush,
    StringAlignment,
    StringFormat,
)
from System.Drawing.Drawing2D import (  # noqa: E402
    GraphicsPath,
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
MA_NOACTIVATE = 3
IDC_HAND = 32649
TME_LEAVE = 0x00000002
VK_LBUTTON = 0x01

BALL_COLOR_A = "#4c8f76"
BALL_COLOR_B = "#2b5647"
DANGER = "#cf5b4e"
AMBER = "#f0c04a"
BADGE_INK = "#244a3c"
AMBER_INK = "#4a3a08"

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
    user32.UpdateLayeredWindow.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
        ctypes.c_void_p, ctypes.POINTER(wt.POINT), wt.DWORD, ctypes.POINTER(BLENDFUNCTION), wt.DWORD,
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
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    gdi32.CreateDIBSection.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wt.UINT,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wt.DWORD,
    ]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]


_setup_prototypes()


def _color(hex_text: str) -> Color:
    return ColorTranslator.FromHtml(hex_text)


def _rounded_doc_path(left: float, top: float, width: float, height: float, radius: float) -> GraphicsPath:
    path = GraphicsPath()
    d = radius * 2
    path.AddArc(left, top, d, d, 180, 90)
    path.AddArc(left + width - d, top, d, d, 270, 90)
    path.AddArc(left + width - d, top + height - d, d, d, 0, 90)
    path.AddArc(left, top + height - d, d, d, 90, 90)
    path.CloseFigure()
    return path


class BallWindow:
    """80×80 的透明悬浮球：置顶、不进任务栏、不抢焦点、可拖动。"""

    CLASS_NAME = "HomeworkDeskBallWindow"

    def __init__(
        self,
        size: int = 80,
        position: tuple[int, int] = (0, 0),
        log: logging.Logger | None = None,
        data_provider=None,
        on_click=None,
        on_hide=None,
        on_moved=None,
        clamp=None,
    ) -> None:
        self.size = int(size)
        self._log = log or logging.getLogger("homework-desk")
        self._data_provider = data_provider
        self._on_click = on_click
        self._on_hide = on_hide
        self._on_moved = on_moved
        self._clamp = clamp

        self._hwnd: int = 0
        self._ready = threading.Event()
        self._closing = False
        self._lock = threading.RLock()
        self._hbmp: int = 0
        self._bits: ctypes.c_void_p | None = None
        self._bits_ptr: IntPtr = IntPtr.Zero
        self._screen_dc: int = 0
        self._mem_dc: int = 0

        self._pos = (int(position[0]), int(position[1]))
        self._counts: tuple[int, int, int] | None = None
        self._failed = False
        self._hover = False
        self._dragging = False
        self._drag_start = (0, 0)
        self._drag_origin = (0, 0)
        self._drag_moved = False
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._wndproc_ref = WNDPROC(self._wndproc)
        self._hand_cursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND))

    # ------------------------------------------------------------------ 生命周期
    @property
    def hwnd(self) -> int:
        return self._hwnd or 0

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
            wc.hCursor = self._hand_cursor
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
            self._create_surface()
            self._render()
            user32.SetTimer(ctypes.c_void_p(self._hwnd), 1, 60, None)
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
        hwnd = self._hwnd
        if hwnd:
            try:
                user32.KillTimer(ctypes.c_void_p(hwnd), 1)
                user32.PostMessageW(ctypes.c_void_p(hwnd), WM_CLOSE, 0, 0)
            except Exception:
                self._log.debug("关闭悬浮球失败", exc_info=True)

    def show(self) -> None:
        hwnd = self._hwnd
        if not hwnd or self._closing:
            return
        user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOWNOACTIVATE)
        user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self.refresh()

    def hide(self) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        user32.ShowWindow(ctypes.c_void_p(hwnd), SW_HIDE)

    # ------------------------------------------------------------------ 位置
    def move_to(self, x: int, y: int, notify: bool = False) -> tuple[int, int]:
        x, y = self._clamp_position(int(x), int(y))
        hwnd = self._hwnd
        if hwnd:
            user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOPMOST), x, y, 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        self._pos = (x, y)
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

    # ------------------------------------------------------------------ 数据
    def set_counts(self, total: int, urgent: int, overdue: int, failed: bool = False) -> None:
        with self._lock:
            counts = (int(total), int(urgent), int(overdue))
            if counts == self._counts and failed == self._failed:
                return
            self._counts = counts
            self._failed = failed
        self.refresh()

    def refresh(self) -> None:
        hwnd = self._hwnd
        if hwnd:
            self._render()

    def poll_forever(self, interval: float = 30.0) -> None:
        while not self._closing:
            try:
                if self._data_provider:
                    result = self._data_provider()
                    if result:
                        total, urgent, overdue = result
                        self.set_counts(total, urgent, overdue)
            except Exception:
                self.set_counts(0, 0, 0, failed=True)
            for _ in range(int(interval * 10)):
                if self._closing:
                    return
                time.sleep(0.1)

    # ------------------------------------------------------------------ 绘制
    def _create_surface(self) -> None:
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.size
        bmi.bmiHeader.biHeight = -self.size  # 负值 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB

        self._screen_dc = user32.GetDC(None)
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(
            ctypes.c_void_p(self._screen_dc), ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0
        )
        if not hbmp:
            self._log.error("创建悬浮球绘图表面失败")
            return
        self._hbmp = int(hbmp)
        self._bits = bits
        self._bits_ptr = IntPtr(int(bits.value or 0))
        self._mem_dc = gdi32.CreateCompatibleDC(ctypes.c_void_p(self._screen_dc))
        gdi32.SelectObject(ctypes.c_void_p(self._mem_dc), ctypes.c_void_p(self._hbmp))

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

    def _render(self) -> None:
        hwnd = self._hwnd
        if not hwnd or not self._hbmp or not self._bits_ptr:
            return
        with self._lock:
            bmp = Bitmap(self.size, self.size, self.size * 4, PixelFormat.Format32bppPArgb, self._bits_ptr)
            try:
                g = Graphics.FromImage(bmp)
                try:
                    g.SmoothingMode = SmoothingMode.AntiAlias
                    g.PixelOffsetMode = PixelOffsetMode.HighQuality
                    g.TextRenderingHint = TextRenderingHint.AntiAlias
                    g.Clear(Color.Transparent)
                    self._paint(g)
                finally:
                    g.Dispose()
                self._update_layered()
            finally:
                bmp.Dispose()

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
        cached = getattr(self, "_font", None)
        if cached is not None:
            return cached
        font = None
        for family in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"):
            try:
                font = Font(family, 11.5, FontStyle.Bold, GraphicsUnit.Pixel)
                break
            except Exception:
                continue
        if font is None:
            font = Font("Arial", 11.5, FontStyle.Bold, GraphicsUnit.Pixel)
        self._font = font
        return font

    def _update_layered(self) -> None:
        hwnd = self._hwnd
        if not hwnd or not self._mem_dc or not self._screen_dc:
            return
        # 用窗口的真实坐标，避免外部 SetWindowPos 后这里把它拉回旧位置。
        rect = wt.RECT()
        user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
        self._pos = (int(rect.left), int(rect.top))
        dst = wt.POINT(int(rect.left), int(rect.top))
        size = wt.SIZE(self.size, self.size)
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
            ctypes.byref(blend),
            ULW_ALPHA,
        )

    # ------------------------------------------------------------------ 消息
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_PAINT:
                ps = PAINTSTRUCT()
                user32.BeginPaint(ctypes.c_void_p(hwnd), ctypes.byref(ps))
                user32.EndPaint(ctypes.c_void_p(hwnd), ctypes.byref(ps))
                self._render()
                return 0
            if msg == WM_TIMER:
                total, urgent, overdue = self._counts or (0, 0, 0)
                if urgent > 0:
                    self._render()
                return 0
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_SETCURSOR:
                user32.SetCursor(ctypes.c_void_p(self._hand_cursor))
                return 1
            if msg == WM_MOUSEMOVE:
                if not self._hover:
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
                if not self._dragging and self._hover:
                    self._hover = False
                    self._render()
                return 0
            if msg == WM_LBUTTONDOWN:
                self._begin_drag()
                return 0
            if msg == WM_LBUTTONUP:
                self._end_drag()
                return 0
            if msg == WM_RBUTTONUP:
                if self._on_hide:
                    threading.Thread(target=self._safe_call, args=(self._on_hide,), daemon=True).start()
                return 0
            if msg == WM_CLOSE:
                user32.KillTimer(ctypes.c_void_p(hwnd), 1)
                user32.DestroyWindow(ctypes.c_void_p(hwnd))
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            self._log.debug("悬浮球消息处理异常 msg=%s", msg, exc_info=True)
        return user32.DefWindowProcW(ctypes.c_void_p(hwnd), msg, wparam, lparam)

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
        elif self._on_click:
            threading.Thread(target=self._safe_call, args=(self._on_click,), daemon=True).start()
