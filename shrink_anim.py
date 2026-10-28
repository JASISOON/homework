"""把主窗口「吸进」悬浮球的过渡动画。

为什么不用窗口 resize 做动画：
    WebView2 每改一次窗口尺寸都会重新排版（文字回流、卡片重排），收缩过程中
    内容会明显抽搐，缩到 80×80 时页面更是没法看。所以这里先把窗口定格成一帧
    位图，再用一个分层窗口（``WS_EX_LAYERED`` + ``UpdateLayeredWindow``）按缓动
    曲线逐帧缩小、淡出，最后一帧正好落在悬浮球的位置上 —— 视觉上就是窗口被吸
    进小球里。

Electron 里的等价物：
    ``BrowserWindow.setBounds()`` 动画 / ``win.animate()``  → 这里的分层窗口逐帧
    ``UpdateLayeredWindow``（带位置和尺寸，一步到位，不需要 SetWindowPos）。

线程：``play()`` 会阻塞到动画结束，只在调用它的线程创建 / 销毁窗口。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import time

import clr

clr.AddReference("System.Drawing")

from System.Drawing import (  # noqa: E402
    Bitmap,
    Color,
    CopyPixelOperation,
    Graphics,
    Rectangle,
    Size,
    SolidBrush,
)
from System.Drawing.Drawing2D import (  # noqa: E402
    CompositingMode,
    CompositingQuality,
    InterpolationMode,
    PixelOffsetMode,
)
from System.Drawing.Imaging import PixelFormat  # noqa: E402
from System import IntPtr  # noqa: E402

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32


# ---------------------------------------------------------------------------
# Win32 常量
# ---------------------------------------------------------------------------
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000

SW_SHOWNOACTIVATE = 4
ULW_ALPHA = 0x00000002
# PrintWindow 的 PW_RENDERFULLCONTENT：让 WebView2 这类走合成器的子窗口也能画出来。
PW_RENDERFULLCONTENT = 0x00000002

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x00000002
TIMER_ALL_ACCESS = 0x1F0003
INFINITE = 0xFFFFFFFF
FRAME_SECONDS = 0.016


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", wt.BYTE),
        ("BlendFlags", wt.BYTE),
        ("SourceConstantAlpha", wt.BYTE),
        ("AlphaFormat", wt.BYTE),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class _LARGE_INTEGER(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_longlong)]


# 64 位下句柄必须声明成 c_void_p，否则默认 c_int 会把高 32 位截掉。
user32.CreateWindowExW.restype = ctypes.c_void_p
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
]
user32.GetDC.restype = ctypes.c_void_p
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.UpdateLayeredWindow.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
    ctypes.c_void_p, ctypes.POINTER(wt.POINT), wt.DWORD, ctypes.c_void_p, wt.DWORD,
]
user32.DestroyWindow.argtypes = [ctypes.c_void_p]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.UINT]

gdi32.CreateDIBSection.restype = ctypes.c_void_p
# 参数 2 用 c_void_p：ball_window.py 也声明了同一个函数，两个模块的 BITMAPINFO
# 是两个不同的类，声明成 POINTER(自己的类) 会互相覆盖并触发 TypeError。
gdi32.CreateDIBSection.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, wt.UINT,
    ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wt.DWORD,
]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]

kernel32.CreateWaitableTimerExW.restype = ctypes.c_void_p
kernel32.CreateWaitableTimerExW.argtypes = [
    ctypes.c_void_p, wt.LPCWSTR, wt.DWORD, wt.DWORD
]
# 参数 2 用 c_void_p：ball_window.py 也声明了同一个函数、并且有自己的
# _LARGE_INTEGER 类。ctypes.windll 是进程级共享的，声明成 POINTER(自己的类)
# 会互相覆盖，另一边的结构体一传就 TypeError。
kernel32.SetWaitableTimer.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, wt.LONG,
    ctypes.c_void_p, ctypes.c_void_p, wt.BOOL,
]
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wt.RECT()
    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


class _FrameClock:
    """16 ms 一拍的等待器。

    ``time.sleep`` 会被系统时钟节拍量化（15.6 ms → 实际 31 ms），收缩动画就只剩
    30 fps。这里用 Win10 的高精度可等待定时器，实测稳定在 16 ms 左右。
    """

    def __init__(self) -> None:
        self._handle = 0
        try:
            self._handle = kernel32.CreateWaitableTimerExW(
                None, None, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS
            )
        except Exception:
            self._handle = 0

    def wait(self, ms: int) -> None:
        if not self._handle:
            time.sleep(ms / 1000.0)
            return
        due = _LARGE_INTEGER(-int(ms) * 10000)
        kernel32.SetWaitableTimer(
            ctypes.c_void_p(self._handle),
            ctypes.cast(ctypes.byref(due), ctypes.c_void_p),
            0, None, None, False,
        )
        kernel32.WaitForSingleObject(ctypes.c_void_p(self._handle), INFINITE)

    def close(self) -> None:
        if self._handle:
            kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = 0


class ShrinkAnimation:
    """把一帧窗口截图缩到目标矩形并淡出的分层窗口动画。"""

    def __init__(
        self,
        frame,
        source_rect: tuple[int, int, int, int],
        target_rect: tuple[int, int, int, int],
        duration: float = 0.26,
        log: logging.Logger | None = None,
    ) -> None:
        self._frame = frame
        self._src = tuple(float(v) for v in source_rect)
        self._dst = tuple(float(v) for v in target_rect)
        self._duration = max(0.08, float(duration))
        self._log = log
        self._stopped = False
        # 半透明画布的擦除笔刷（CompositingMode.SourceCopy 下等于把区域清零）。
        self._clear_brush = SolidBrush(Color.FromArgb(0, 0, 0, 0))
        self._hwnd = 0
        self._screen_dc = 0
        self._mem_dc = 0
        self._hbmp = 0
        self._bits = IntPtr.Zero
        self._dib_w = 0
        self._dib_h = 0

    # ------------------------------------------------------------------ 抓图
    @classmethod
    def from_window(
        cls,
        hwnd: int,
        target_rect: tuple[int, int, int, int],
        duration: float = 0.26,
        log: logging.Logger | None = None,
    ) -> "ShrinkAnimation | None":
        """抓下窗口当前画面；抓不到就返回 None（调用方直接切换即可）。"""
        if not hwnd:
            return None
        try:
            left, top, right, bottom = _window_rect(hwnd)
            width, height = right - left, bottom - top
            if width < 16 or height < 16:
                return None
            frame = cls._grab(hwnd, left, top, width, height, log)
            if frame is None:
                return None
            return cls(frame, (left, top, width, height), target_rect, duration, log)
        except Exception:
            if log is not None:
                log.debug("抓取主窗口画面失败，跳过收缩动画", exc_info=True)
            return None

    @staticmethod
    def _grab(hwnd: int, left: int, top: int, width: int, height: int, log):
        """抓窗口内容。

        先用 ``PrintWindow(PW_RENDERFULLCONTENT)``：窗口被遮挡、甚至跑到屏幕外
        （多显示器负坐标）都能抓到，且不会把别的程序一起拍进来。WebView2 在
        Win11 上支持这条路；老系统上可能只画出空白，所以再加一次「是否全黑」
        的判断，失败就退回直接从屏幕抓。
        """
        try:
            raw = Bitmap(int(width), int(height), PixelFormat.Format32bppRgb)
            g = Graphics.FromImage(raw)
            hdc = g.GetHdc()
            try:
                printed = user32.PrintWindow(
                    ctypes.c_void_p(hwnd), ctypes.c_void_p(hdc.ToInt64()), PW_RENDERFULLCONTENT
                )
            finally:
                g.ReleaseHdc(hdc)
                g.Dispose()
            if printed:
                # PrintWindow 不保证 alpha，先过到 32bppPArgb 上把不透明度补满。
                frame = Bitmap(int(width), int(height), PixelFormat.Format32bppPArgb)
                g2 = Graphics.FromImage(frame)
                try:
                    g2.DrawImage(raw, Rectangle(0, 0, int(width), int(height)))
                finally:
                    g2.Dispose()
                raw.Dispose()
                if not ShrinkAnimation._looks_blank(frame):
                    return frame
                frame.Dispose()
            else:
                raw.Dispose()
        except Exception:
            if log is not None:
                log.debug("PrintWindow 抓帧失败，改用屏幕截图", exc_info=True)

        try:
            # 兜底：直接抓屏幕，但要裁到虚拟屏幕内，否则 CopyFromScreen 会报错。
            vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
            vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
            vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
            gx, gy = max(left, vx), max(top, vy)
            gr, gb = min(left + width, vx + vw), min(top + height, vy + vh)
            cw, ch = gr - gx, gb - gy
            if cw < 16 or ch < 16:
                return None
            frame = Bitmap(int(cw), int(ch), PixelFormat.Format32bppPArgb)
            g = Graphics.FromImage(frame)
            try:
                g.CopyFromScreen(int(gx), int(gy), 0, 0, Size(int(cw), int(ch)),
                                 CopyPixelOperation.SourceCopy)
            finally:
                g.Dispose()
            return frame
        except Exception:
            if log is not None:
                log.debug("屏幕截图也失败了", exc_info=True)
            return None

    @staticmethod
    def _looks_blank(frame) -> bool:
        """采样 9 个点，全黑就当没抓到内容（WebView2 抓帧失败的典型表现）。"""
        try:
            for fx in (0.08, 0.5, 0.92):
                for fy in (0.08, 0.5, 0.92):
                    px = frame.GetPixel(int(frame.Width * fx), int(frame.Height * fy))
                    if px.R + px.G + px.B > 12:
                        return False
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ 播放
    def play(self, on_ready=None) -> bool:
        """阻塞播放；``on_ready`` 在第一帧画好之后调用（此时再把主窗口藏起来）。"""
        if not self._create_window() or not self._ensure_surface():
            self._destroy()
            return False
        clock = _FrameClock()
        try:
            sx, sy, sw, sh = self._src
            tx, ty, tw, th = self._dst
            # 第 0 帧：和主窗口现在看到的一模一样，先盖上去再隐藏真窗口，避免闪桌面。
            self._paint(sx, sy, sw, sh, 255)
            if on_ready is not None:
                try:
                    on_ready()
                except Exception:
                    if self._log is not None:
                        self._log.exception("收缩动画的 on_ready 回调失败")
            t0 = time.perf_counter()
            index = 0
            while not self._stopped:
                p = min(1.0, (time.perf_counter() - t0) / self._duration)
                eased = p * p  # easeInQuad：越缩越快，像被吸走
                self._paint(
                    sx + (tx - sx) * eased,
                    sy + (ty - sy) * eased,
                    sw + (tw - sw) * eased,
                    sh + (th - sh) * eased,
                    int(255 * max(0.0, 1.0 - p ** 1.5)),
                )
                if p >= 1.0:
                    break
                # 按绝对节拍等待：把渲染耗时算进去，否则每帧都多花 3~5 ms。
                index += 1
                delay = (t0 + index * FRAME_SECONDS) - time.perf_counter()
                if delay > 0:
                    clock.wait(max(1, int(round(delay * 1000))))
            return True
        except Exception:
            if self._log is not None:
                self._log.debug("收缩动画播放失败", exc_info=True)
            return False
        finally:
            clock.close()
            self._destroy()

    def stop(self) -> None:
        self._stopped = True

    # ------------------------------------------------------------------ 分层窗口
    def _create_window(self) -> bool:
        try:
            self._hwnd = user32.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_TRANSPARENT
                | WS_EX_NOACTIVATE,
                "Static",
                "HomeworkDeskShrink",
                WS_POPUP,
                0, 0, 1, 1,
                None, None, None, None,
            )
        except Exception:
            self._hwnd = 0
        if not self._hwnd:
            return False
        user32.ShowWindow(ctypes.c_void_p(self._hwnd), SW_SHOWNOACTIVATE)
        return True

    def _ensure_surface(self) -> bool:
        if self._hbmp:
            return True
        self._dib_w = int(self._frame.Width)
        self._dib_h = int(self._frame.Height)
        if self._dib_w <= 0 or self._dib_h <= 0:
            return False
        try:
            self._screen_dc = user32.GetDC(None)
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = self._dib_w
            bmi.bmiHeader.biHeight = -self._dib_h  # 负高度 = 自上而下
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0  # BI_RGB
            bits = ctypes.c_void_p()
            self._hbmp = gdi32.CreateDIBSection(
                ctypes.c_void_p(self._screen_dc),
                ctypes.cast(ctypes.pointer(bmi), ctypes.c_void_p),
                0, ctypes.byref(bits), None, 0,
            )
            if not self._hbmp or not bits:
                return False
            # GDI+ 的 Bitmap 构造函数不认 ctypes.c_void_p，必须换成 System.IntPtr。
            self._bits = IntPtr(bits.value)
            self._mem_dc = gdi32.CreateCompatibleDC(ctypes.c_void_p(self._screen_dc))
            gdi32.SelectObject(ctypes.c_void_p(self._mem_dc), ctypes.c_void_p(self._hbmp))
            return True
        except Exception:
            if self._log is not None:
                self._log.debug("创建收缩动画画布失败", exc_info=True)
            return False

    def _paint(self, x: float, y: float, w: float, h: float, alpha: int) -> None:
        if not self._hwnd or not self._mem_dc:
            return
        width, height = max(1, int(round(w))), max(1, int(round(h)))
        # 位图的尺寸固定成截图大小（最大帧），每帧只把内容画到左上角这块区域，
        # 剩下的透明部分不会被 UpdateLayeredWindow 提交。
        bmp = Bitmap(self._dib_w, self._dib_h, self._dib_w * 4,
                     PixelFormat.Format32bppPArgb, self._bits)
        try:
            g = Graphics.FromImage(bmp)
            try:
                # SourceCopy + 只清当前帧区域：Clear() 会抹整块 900×600 画布，
                # 实测每帧多花 7 ms，把 60 fps 拖成 43 fps。
                g.CompositingMode = CompositingMode.SourceCopy
                g.FillRectangle(self._clear_brush, 0, 0, width, height)
                g.CompositingMode = CompositingMode.SourceOver
                g.InterpolationMode = InterpolationMode.Bilinear
                g.PixelOffsetMode = PixelOffsetMode.HighSpeed
                g.CompositingQuality = CompositingQuality.HighSpeed
                g.DrawImage(self._frame, Rectangle(0, 0, width, height))
            finally:
                g.Dispose()
        finally:
            bmp.Dispose()
        dst = wt.POINT(int(round(x)), int(round(y)))
        size = wt.SIZE(width, height)
        src = wt.POINT(0, 0)
        blend = BLENDFUNCTION(0, 0, max(0, min(255, int(alpha))), 1)
        user32.UpdateLayeredWindow(
            ctypes.c_void_p(self._hwnd),
            ctypes.c_void_p(self._screen_dc),
            ctypes.byref(dst),
            ctypes.byref(size),
            ctypes.c_void_p(self._mem_dc),
            ctypes.byref(src),
            0,
            ctypes.cast(ctypes.pointer(blend), ctypes.c_void_p),
            ULW_ALPHA,
        )

    def _destroy(self) -> None:
        if self._hwnd:
            try:
                user32.DestroyWindow(ctypes.c_void_p(self._hwnd))
            except Exception:
                pass
            self._hwnd = 0
        if self._mem_dc:
            try:
                gdi32.DeleteDC(ctypes.c_void_p(self._mem_dc))
            except Exception:
                pass
            self._mem_dc = 0
        if self._hbmp:
            try:
                gdi32.DeleteObject(ctypes.c_void_p(self._hbmp))
            except Exception:
                pass
            self._hbmp = 0
        self._bits = IntPtr.Zero
        if self._screen_dc:
            try:
                user32.ReleaseDC(None, ctypes.c_void_p(self._screen_dc))
            except Exception:
                pass
            self._screen_dc = 0
        if self._frame is not None:
            try:
                self._frame.Dispose()
            except Exception:
                pass
            self._frame = None
        if self._clear_brush is not None:
            try:
                self._clear_brush.Dispose()
            except Exception:
                pass
            self._clear_brush = None
