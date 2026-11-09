#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游戏前提醒弹窗（2.2.0）。

用户打开游戏的那一刻，如果有红档作业（逾期 / 剩 5 小时内），
在主显示器右下角弹这个窗 —— 目的是「投入游戏前看一眼」，所以：

- 只在游戏启动瞬间弹一次，本次游戏会话内不重复（由 ReminderEngine 控制）
- TopMost 常驻，被游戏抢了焦点也不会自己消失，必须点按钮处理
- 三个按钮：已阅（把列出的红档全部静音）/ 打开主窗口 / 稍后再说（只关窗）
- 配色与主界面四档方案一致（这里出现的全是红档）

实现方式与 TrayMenuPopup 一致：无边框 WinForms Form + GDI+ 自绘，
事件用委托绑定（pythonnet 不回调重写的虚拟方法）。
必须在 WinForms UI 线程上创建/显示（desktop_app 侧用 Invoke 调度）。
"""

from __future__ import annotations

import ctypes
import logging
import time

from reminders import TIER_COLORS, TIER_LEVEL_RED, fmt_remaining

ALERT_WIDTH = 344
ALERT_PAD = 14
ALERT_HEADER_H = 56
ALERT_ITEM_H = 58
ALERT_FOOTER_H = 54
ALERT_BTN_H = 30

ALERT_COLORS = {
    "bg": (251, 252, 249),
    "text": (32, 44, 38),
    "muted": (138, 146, 139),
    "border": (222, 226, 217),
    "btn_bg": (235, 240, 233),
    "btn_text": (47, 91, 76),
}

# 按钮布局：三个等宽按钮排一行
BTN_LABELS = ("已阅", "打开主窗口", "稍后再说")


class GameAlertWindow:
    """红档作业提醒弹窗。rows: [{name, course, due(datetime), remaining}]"""

    def __init__(self, rows, log: logging.Logger, on_read_all, on_open_main):
        import clr

        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        import System.Windows.Forms as WinForms
        from System.Drawing import Color, Font, FontStyle, Size

        self._log = log
        self._rows = list(rows or [])[:3]
        self._on_read_all = on_read_all
        self._on_open_main = on_open_main
        self._wf = WinForms
        self._color = Color
        self._closed = False
        self._btn_rects: list[tuple[int, int, int, int]] = []
        self._item_rects: list[tuple[int, int, int, int]] = []

        form = WinForms.Form()
        form.FormBorderStyle = WinForms.FormBorderStyle(0)
        form.ShowInTaskbar = False
        form.TopMost = True
        form.StartPosition = WinForms.FormStartPosition.Manual
        form.BackColor = Color.FromArgb(*ALERT_COLORS["bg"])
        form.AutoScaleMode = WinForms.AutoScaleMode(0)
        form.KeyPreview = True
        form.Text = "作业备忘 · 游戏前提醒"
        self._form = form

        try:
            _ = form.Handle
            self._scale = float(form.DeviceDpi) / 96.0
        except Exception:
            self._scale = 1.0
        s = self._scale

        self._width = int(ALERT_WIDTH * s)
        height = ALERT_PAD + ALERT_HEADER_H
        self._row_top = []
        for _row in self._rows:
            self._row_top.append(height)
            height += ALERT_ITEM_H
        height += 6 + ALERT_FOOTER_H + ALERT_PAD
        self._height = int(height * s)
        form.Size = Size(self._width, self._height)

        self._enable_double_buffer()

        form.Paint += self._on_paint
        form.MouseMove += self._on_mouse_move
        form.MouseUp += self._on_mouse_up
        form.KeyDown += self._on_key_down

    # -- 基础 ----------------------------------------------------------
    def _enable_double_buffer(self) -> None:
        try:
            import System.Reflection as Reflection

            prop = self._form.GetType().GetProperty(
                "DoubleBuffered",
                Reflection.BindingFlags.Instance | Reflection.BindingFlags.NonPublic,
            )
            prop.SetValue(self._form, True)
        except Exception:
            self._log.debug("开启弹窗双缓冲失败", exc_info=True)

    def _apply_window_style(self) -> None:
        """Win11 圆角 + 不进 Alt+Tab。"""
        try:
            hwnd = self._form.Handle.ToInt64()
            user32 = ctypes.windll.user32
            ex_style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), -20)
            user32.SetWindowLongW(
                ctypes.c_void_p(hwnd), -20, ex_style | 0x00000080  # WS_EX_TOOLWINDOW
            )
            corner = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), 33, ctypes.byref(corner), ctypes.sizeof(corner)
            )
        except Exception:
            self._log.debug("设置弹窗窗口样式失败", exc_info=True)

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

    def _tier_color(self):
        from System.Drawing import SolidBrush

        return SolidBrush(self._color.FromArgb(*_rgb(TIER_COLORS[TIER_LEVEL_RED])))

    def _on_paint(self, sender, event) -> None:
        try:
            self._paint(event.Graphics)
        except Exception:
            self._log.exception("绘制游戏提醒弹窗失败")

    def _paint(self, g) -> None:
        from System.Drawing import Font, FontStyle, Pen, SolidBrush, StringFormat

        s = self._scale
        c = ALERT_COLORS
        sf = StringFormat()

        brush_text = SolidBrush(self._color.FromArgb(*c["text"]))
        brush_muted = SolidBrush(self._color.FromArgb(*c["muted"]))
        brush_border = Pen(self._color.FromArgb(*c["border"]), 1.0)
        brush_red = self._tier_color()
        font_title = Font("Microsoft YaHei UI", 11.25, FontStyle.Bold)
        font_sub = Font("Microsoft YaHei UI", 8.25, FontStyle.Regular)
        font_name = Font("Microsoft YaHei UI", 9.75, FontStyle.Bold)
        font_meta = Font("Microsoft YaHei UI", 8.25, FontStyle.Regular)
        font_tag = Font("Microsoft YaHei UI", 8.0, FontStyle.Bold)
        font_btn = Font("Microsoft YaHei UI", 9.0, FontStyle.Regular)

        # 头部
        y = int(ALERT_PAD * s)
        g.DrawString("要开玩了？先看一眼作业", font_title, brush_text,
                     self._rect(ALERT_PAD, y + 6, ALERT_WIDTH - ALERT_PAD * 2, 26))
        g.DrawString("下面这些今晚之前要交，处理完再安心玩", font_sub, brush_muted,
                     self._rect(ALERT_PAD, y + 32, ALERT_WIDTH - ALERT_PAD * 2, 16))

        # 作业行（全部红档）
        self._item_rects = []
        row_y = ALERT_PAD + ALERT_HEADER_H
        for idx, row in enumerate(self._rows):
            top = int(row_y * s)
            self._item_rects.append((int(ALERT_PAD * s), top,
                                     int((ALERT_WIDTH - ALERT_PAD * 2) * s), int(ALERT_ITEM_H * s)))
            left = int(ALERT_PAD * s)
            width = int((ALERT_WIDTH - ALERT_PAD * 2) * s)
            card = self._rounded(g, left, top, width, int(ALERT_ITEM_H * s), 8.0 * s)
            g.FillPath(SolidBrush(self._color.FromArgb(245, 246, 242)), card)
            card.Dispose()
            g.DrawPath(brush_border, self._rounded(g, left, top, width, int(ALERT_ITEM_H * s), 8.0 * s))
            # 左缘红条
            bar = self._rounded(g, left, top, int(4 * s), int(ALERT_ITEM_H * s), 2.0 * s)
            g.FillPath(brush_red, bar)
            bar.Dispose()
            # 标签（红底白字）
            tag = self._tag_text(row)
            tag_w = int(88 * s)
            tag_x = left + int(12 * s)
            tag_y = top + int(10 * s)
            pill = self._rounded(g, tag_x, tag_y, tag_w, int(18 * s), 9.0 * s)
            g.FillPath(brush_red, pill)
            pill.Dispose()
            white = SolidBrush(self._color.FromArgb(255, 255, 255))
            g.DrawString(tag, font_tag, white, self._rect_f(tag_x, tag_y, tag_w, int(18 * s)))
            white.Dispose()
            # 名称 + 说明
            g.DrawString(str(row.get("name") or "未命名作业"), font_name, brush_text,
                         self._rect_f(tag_x + tag_w + int(8 * s), tag_y - int(2 * s),
                                      width - int((12 + 88 + 8 + 20) * s), int(22 * s)))
            meta = self._meta_text(row)
            g.DrawString(meta, font_meta, brush_muted,
                         self._rect_f(tag_x, tag_y + int(24 * s), width - int(24 * s), int(16 * s)))
            row_y += ALERT_ITEM_H

        # 底部按钮
        self._btn_rects = []
        btn_top = int((row_y + 6) * s)
        area = int((ALERT_WIDTH - ALERT_PAD * 2) * s)
        gap = int(8 * s)
        btn_w = (area - gap * (len(BTN_LABELS) - 1)) // len(BTN_LABELS)
        for idx, label in enumerate(BTN_LABELS):
            bx = int(ALERT_PAD * s) + idx * (btn_w + gap)
            rect = (bx, btn_top, btn_w, int(ALERT_BTN_H * s))
            self._btn_rects.append(rect)
            fill = SolidBrush(self._color.FromArgb(*c["btn_bg"]))
            path = self._rounded(g, *rect, 6.0 * s)
            g.FillPath(fill, path)
            path.Dispose()
            fill.Dispose()
            g.DrawPath(brush_border, self._rounded(g, *rect, 6.0 * s))
            accent = SolidBrush(self._color.FromArgb(*c["btn_text"]))
            g.DrawString(label, font_btn, accent, self._rect_f(*rect))
            accent.Dispose()

        # 释放字体
        for f in (font_title, font_sub, font_name, font_meta, font_tag, font_btn):
            f.Dispose()
        for b in (brush_text, brush_muted, brush_red):
            b.Dispose()
        brush_border.Dispose()
        sf.Dispose()

    @staticmethod
    def _rect(x, y, w, h):
        from System.Drawing import RectangleF
        return RectangleF(float(x), float(y), float(w), float(h))

    @staticmethod
    def _rect_f(x, y, w, h):
        from System.Drawing import RectangleF
        return RectangleF(float(x), float(y), float(w), float(h))

    @staticmethod
    def _tag_text(row) -> str:
        remaining = float(row.get("remaining") or 0)
        if remaining <= 0:
            over = fmt_remaining(-remaining)
            if over.startswith("剩 "):
                over = over[2:]
            return "已逾期 " + over
        return fmt_remaining(remaining)

    @staticmethod
    def _meta_text(row) -> str:
        due = row.get("due")
        course = str(row.get("course") or "")
        stamp = due.strftime("%m月%d日 %H:%M") if due is not None else "未设截止"
        prefix = "原定 " if float(row.get("remaining") or 0) <= 0 else ""
        line = f"{prefix}{stamp} 截止"
        if course:
            line += f" · {course}"
        return line

    # -- 交互 ----------------------------------------------------------
    def _hit(self, rects, x, y):
        for idx, (bx, by, bw, bh) in enumerate(rects):
            if bx <= x < bx + bw and by <= y < by + bh:
                return idx
        return -1

    def _on_mouse_move(self, sender, event) -> None:
        hover = (
            self._hit(self._btn_rects, event.X, event.Y) >= 0
            or self._hit(self._item_rects, event.X, event.Y) >= 0
        )
        self._form.Cursor = self._wf.Cursors.Hand if hover else self._wf.Cursors.Default

    def _on_mouse_up(self, sender, event) -> None:
        if event.Button != self._wf.MouseButtons.Left:
            return
        x, y = event.X, event.Y
        btn = self._hit(self._btn_rects, x, y)
        if btn == 0:  # 已阅
            self.close()
            self._safe(self._on_read_all)
        elif btn == 1:  # 打开主窗口
            self.close()
            self._safe(self._on_open_main)
        elif btn == 2:  # 稍后再说
            self.close()
            return
        else:
            item = self._hit(self._item_rects, x, y)
            if item >= 0:  # 点作业行 = 打开主窗口
                self.close()
                self._safe(self._on_open_main)

    def _on_key_down(self, sender, event) -> None:
        if event.KeyCode == self._wf.Keys.Escape:
            self.close()

    def _safe(self, func) -> None:
        if func is None:
            return
        try:
            func()
        except Exception:
            self._log.exception("游戏提醒弹窗回调失败")

    # -- 显示 / 关闭 ---------------------------------------------------
    def show(self) -> None:
        from System.Drawing import Point

        area = self._wf.Screen.PrimaryScreen.WorkingArea
        left = area.Right - self._width - 14
        top = area.Bottom - self._height - 14
        self._form.Location = Point(left, top)
        self._form.Show()
        self._apply_window_style()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._form.Close()
        except Exception:
            self._log.debug("关闭游戏提醒弹窗失败", exc_info=True)


def _rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
