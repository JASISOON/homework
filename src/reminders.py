#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2.2.0 自动提醒引擎。

设计目标（用户明确要求的约束）：
- 低占用：不维护独立的数据拉取循环 —— 作业数据由悬浮球既有的 30 秒轮询
  顺便喂进来（``ingest``），引擎自身只有一个 10 秒一跳的轻量守护循环
  （游戏进程快照 + 3 小时一次的强制同步）， ``Event.wait`` 睡眠式调度。
- 通知用真 Toast（WinRT，经 PowerShell 子进程发送，支持按钮），
  气球通知 ``ShowBalloonTip`` 挂不了按钮，弃用。
- 按钮回传走 ``homeworkdesk://`` 协议：点击后 Windows 拉起临时进程，
  它把 URL 写进 ack 文件并点亮命名事件，然后秒退；常驻进程收到事件后处理。
- 已阅 ≠ 完成：已阅只是静音提醒（红标跳过、卡片盖灰标签），
  作业仍留在待办里，勾「完成」才消掉。
- 紧急度四档配色（与界面共用）：红 = 逾期或剩 ≤5h，琥珀 = 5~24h，
  浅琥珀 = 24h~3 天，灰 = 更远。逾期和临期同级同色，靠文字区分。

注册表只写 HKCU（Toast 身份 + 协议处理器），无服务无驱动，卸载即净。
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import logging
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

TOAST_SCHEME = "homeworkdesk"
AUMID = "HomeworkDesk"
ACK_FILE = "toast-ack.json"
ACK_EVENT_FMT = "Local\\HomeworkDeskToastAck{}"

# 提醒档位：每条作业每档只发一次
TIER_D24 = "d24"
TIER_H1 = "h1"
TIER_OVERDUE = "overdue"
TIER_SECONDS = {TIER_D24: 24 * 3600, TIER_H1: 3600, TIER_OVERDUE: None}

# 四档配色阈值（与主界面/悬浮球共用，改动请同步 CHANGELOG）
TIER_RED_SECONDS = 5 * 3600       # 逾期 + 剩 5 小时内 → 红
TIER_AMBER_SECONDS = 24 * 3600    # 剩 5~24 小时 → 琥珀
TIER_SOON_SECONDS = 3 * 86400     # 剩 24 小时~3 天 → 浅琥珀（红标统计窗口）

TIER_LEVEL_RED = "red"
TIER_LEVEL_AMBER = "amber"
TIER_LEVEL_SOON = "soon"
TIER_LEVEL_PLAIN = "plain"

TIER_COLORS = {
    TIER_LEVEL_RED: "#E24B4A",
    TIER_LEVEL_AMBER: "#BA7517",
    TIER_LEVEL_SOON: "#EFB94A",
    TIER_LEVEL_PLAIN: "#888780",
}


def tier_level(remaining: float) -> str:
    """剩余秒数 → 四档配色档位。"""
    if remaining < TIER_RED_SECONDS:
        return TIER_LEVEL_RED  # 负数（逾期）和 5 小时内同档
    if remaining <= TIER_AMBER_SECONDS:
        return TIER_LEVEL_AMBER
    if remaining <= TIER_SOON_SECONDS:
        return TIER_LEVEL_SOON
    return TIER_LEVEL_PLAIN


def reminder_tier(remaining: float) -> str | None:
    """剩余秒数 → 该发哪一档提醒；不需要发返回 None。"""
    if remaining <= 0:
        return TIER_OVERDUE
    if remaining <= TIER_SECONDS[TIER_H1]:
        return TIER_H1
    if remaining <= TIER_SECONDS[TIER_D24]:
        return TIER_D24
    return None


def fmt_remaining(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 分钟内"
    minutes = seconds // 60
    if minutes < 60:
        return f"剩 {minutes} 分钟"
    hours = minutes // 60
    if hours < 48:
        return f"剩 {hours} 小时" + (f" {minutes % 60} 分" if minutes % 60 and hours < 8 else "")
    days = hours // 24
    return f"剩 {days} 天" + (f" {hours % 24} 小时" if hours % 24 else "")


# ---------------------------------------------------------------------------
# 协议 URL：解析 / 转发（临时进程侧）
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,128}")


def parse_toast_url(argv: list[str]) -> str | None:
    """从命令行参数里找 homeworkdesk:// 协议 URL。"""
    for arg in argv[1:]:
        if arg.lower().startswith(TOAST_SCHEME + "://"):
            return arg
    return None


def forward_toast_url(data_dir: Path, url: str, log: logging.Logger | None = None) -> bool:
    """临时进程侧：把协议 URL 转交给常驻进程（写 ack 文件 + 点亮事件）。"""
    try:
        path = data_dir / ACK_FILE
        payload = json.dumps({"url": url, "ts": time.time()}, ensure_ascii=False)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
        event_name = ACK_EVENT_FMT.format(_mutex_suffix())
        handle = ctypes.windll.kernel32.OpenEventW(EVENT_MODIFY_STATE, False, event_name)
        if not handle:
            return False
        try:
            ctypes.windll.kernel32.SetEvent(ctypes.c_void_p(handle))
        finally:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
        return True
    except Exception:
        if log is not None:
            log.exception("转发 Toast 回传失败：%s", url)
        return False


EVENT_MODIFY_STATE = 0x0002


def _mutex_suffix() -> str:
    import os
    return os.environ.get("HOMEWORK_DESK_MUTEX_SUFFIX") or ""


# ---------------------------------------------------------------------------
# Toast 身份注册（HKCU，幂等）
# ---------------------------------------------------------------------------

def _app_command_line() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" "%1"'
    script = Path(__file__).with_name("desktop_app.py")
    return f'"{sys.executable}" "{script}" "%1"'


def register_toast_identity(icon_path: Path | None, log: logging.Logger) -> None:
    """注册 AUMID（通知上显示的应用名/图标）与 homeworkdesk:// 协议。"""
    try:
        import winreg
        # AUMID：HKCU\Software\Classes\AppUserModelId\<AUMID>
        # IconUri 支持 ico/png 的绝对路径；桌面壳传 assets/homework-desk.ico。
        key_path = rf"Software\Classes\AppUserModelId\{AUMID}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "作业备忘")
            if icon_path and Path(icon_path).exists():
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon_path))
        # 协议：HKCU\Software\Classes\homeworkdesk
        command = _app_command_line()
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{TOAST_SCHEME}") as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, f"URL:{TOAST_SCHEME}")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, rf"Software\Classes\{TOAST_SCHEME}\shell\open\command"
        ) as key:
            current = winreg.QueryValueEx(key, None)[0] if _value_exists(key) else ""
            if current != command:
                winreg.SetValueEx(key, None, 0, winreg.REG_SZ, command)
                log.info("已注册 %s:// 协议处理器", TOAST_SCHEME)
        log.info("Toast 通知身份已就绪（AUMID=%s）", AUMID)
    except Exception:
        log.exception("注册 Toast 身份失败，按钮回传可能不可用")


def _value_exists(key) -> bool:
    import winreg
    try:
        winreg.QueryValueEx(key, None)
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Toast 发送（PowerShell + WinRT，发完即退）
# ---------------------------------------------------------------------------

_POWERSHELL_FLAGS = 0x08000000  # CREATE_NO_WINDOW，不闪黑框
_pending_popen: list[subprocess.Popen] = []


def _xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def build_toast_xml(
    title: str,
    body: str,
    actions: list[tuple[str, str]] | None = None,
) -> str:
    """actions: [(按钮文字, protocol 参数)]。点正文/通知本体 = launch 属性。"""
    launch = f"{TOAST_SCHEME}://show"
    action_xml = "".join(
        f'<action content="{_xml_escape(label)}" arguments="{_xml_escape(args)}"'
        f' activationType="protocol"/>'
        for label, args in (actions or [])
    )
    return (
        f'<toast launch="{launch}" activationType="protocol">'
        f'<visual><binding template="ToastGeneric">'
        f"<text>{_xml_escape(title)}</text>"
        f"<text>{_xml_escape(body)}</text>"
        f"</binding></visual>"
        f"<actions>{action_xml}</actions>"
        f"</toast>"
    )


def send_toast(title: str, body: str, actions: list[tuple[str, str]] | None = None) -> bool:
    """发一条系统 Toast。立即返回，不等待 PowerShell 结束。"""
    xml = build_toast_xml(title, body, actions)
    script = (
        "[void][Windows.UI.Notifications.ToastNotificationManager,"
        " Windows.UI.Notifications, ContentType=WindowsRuntime]\n"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        f"$xml.LoadXml('{xml}')\n"
        "$toast = New-Object Windows.UI.Notifications.ToastNotification($xml)\n"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{AUMID}')"
        ".Show($toast)\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    global _pending_popen
    try:
        proc = subprocess.Popen(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                "-EncodedCommand", encoded,
            ],
            creationflags=_POWERSHELL_FLAGS,
            close_fds=True,
        )
    except Exception:
        return False
    _pending_popen = [p for p in _pending_popen if p.poll() is None][-4:]
    _pending_popen.append(proc)
    return True


# ---------------------------------------------------------------------------
# 开机自启（HKCU Run 键）
# ---------------------------------------------------------------------------

_RUN_VALUE = "HomeworkDesk"


def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script = Path(__file__).with_name("desktop_app.py")
    return f'"{sys.executable}" "{script}"'


def get_autostart() -> bool:
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
        ) as key:
            value, _ = winreg.QueryValueEx(key, _RUN_VALUE)
            return bool(value)
    except FileNotFoundError:
        return False
    except Exception:
        return False


def set_autostart(enabled: bool) -> bool:
    try:
        import winreg
        run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
        if enabled:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE
                ) as key:
                    winreg.DeleteValue(key, _RUN_VALUE)
            except FileNotFoundError:
                pass
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 游戏进程快照（Toolhelp32，毫秒级）
# ---------------------------------------------------------------------------

TH32CS_SNAPPROCESS = 0x2


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


def _setup_toolhelp() -> None:
    """原型进程级共享（见 desktop_app 顶部注释），结构体指针一律 c_void_p。"""
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k32.Process32FirstW.restype = wt.BOOL
    k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.Process32NextW.restype = wt.BOOL
    k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


_setup_toolhelp()


def snapshot_processes() -> dict[str, tuple[int, int]]:
    """返回 {exe 小写(去 .exe): (pid, 父 pid)}。"""
    k32 = ctypes.windll.kernel32
    handle = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return {}
    result: dict[str, tuple[int, int]] = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    h = ctypes.c_void_p(handle)
    ptr = ctypes.cast(ctypes.byref(entry), ctypes.c_void_p)
    try:
        if k32.Process32FirstW(h, ptr):
            while True:
                name = str(entry.szExeFile or "").strip().lower()
                if name.endswith(".exe"):
                    name = name[:-4]
                if name:
                    result.setdefault(name, (int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
                if not k32.Process32NextW(h, ptr):
                    break
    except Exception:
        return {}
    finally:
        k32.CloseHandle(h)
    return result


# 常见启动器：自动建议靠「启动器新拉起子进程」来识别
LAUNCHER_EXES = {
    "steam", "steamwebhelper", "epicgameslauncher", "wegame",
    "battle.net", "agent",          # 暴雪 Agent
    "eadesktop", "origin",          # EA
    "upc", "ubisoftconnect",        # 育碧
    "rockstarservice", "socialclub",
    "qqgameloading", "txplatform",  # 腾讯系辅助进程（宽松匹配）
}

# 自动建议时跳过的明显系统/常用进程（父进程可能是 explorer 等误判兜底）
_SUGGEST_BLOCKLIST = {
    "explorer", "chrome", "msedge", "firefox", "qq", "wechat", "weixin",
    "wps", "wpscloudsvr", "potplayer", "vlc", "cloudmusic", "qqmusic",
    "bilibili", "dingtalk", "feishu", "obs64", "svchost", "conhost",
    "python", "pythonw", "powershell", "cmd", "runtimebroker",
}

_SUGGEST_COOLDOWN = 600  # 两条自动建议之间至少隔 10 分钟


# ---------------------------------------------------------------------------
# 提醒引擎
# ---------------------------------------------------------------------------

class ReminderEngine:
    """三档提醒 + 每日汇总 + 游戏前提醒。

    数据来源：``ingest``（悬浮球 30 秒轮询顺便喂）。
    状态持久化在 prefs.json 的 "reminders" 键下。
    """

    READ_RETENTION_DAYS = 45
    SENT_RETENTION_DAYS = 14

    def __init__(
        self,
        data_dir: Path,
        context,
        log: logging.Logger,
        icon_path: Path | None = None,
        on_show_main=None,
        on_state_changed=None,
        on_game_red=None,
    ) -> None:
        self.data_dir = data_dir
        self.context = context
        self.log = log
        self.icon_path = icon_path
        self.on_show_main = on_show_main
        self.on_state_changed = on_state_changed
        self.on_game_red = on_game_red

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._assignments: list[dict] = []
        self._lock = threading.Lock()
        self._last_prune = time.time()
        self._last_force = time.time()
        self._last_suggest = 0.0

        # 游戏检测状态（内存态）
        self._prev_procs: set[str] = set()
        self._game_running: set[str] = set()
        self._pid_map: dict[int, str] = {}

        self.state = self._load_state()
        self.log.info(
            "提醒引擎初始化：enabled=%s game_alert=%s games=%s 已阅 %d 条",
            self.state.get("enabled", True),
            self.state.get("game_alert_enabled", False),
            len(self.state.get("games") or []),
            len(self.state.get("read") or {}),
        )

    # -------------------------------------------------------------- 持久化

    def _load_state(self) -> dict:
        try:
            prefs = self.context.store.load_prefs()
            state = prefs.get("reminders")
            if isinstance(state, dict):
                return state
        except Exception:
            self.log.exception("读取提醒状态失败")
        return {
            "enabled": True,
            "sent": {},
            "read": {},
            "digest_date": "",
            "game_alert_enabled": False,
            "games": [],
            "game_ignored": [],
        }

    def _save_state(self) -> None:
        try:
            prefs = self.context.store.load_prefs()
            prefs["reminders"] = self.state
            self.context.store.save_prefs(prefs)
        except Exception:
            self.log.exception("保存提醒状态失败")

    # -------------------------------------------------------------- 对外接口

    def start(self) -> None:
        register_toast_identity(self.icon_path, self.log)
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="homework-reminders", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def ingest(self, assignments: list[dict]) -> None:
        """悬浮球轮询喂进来的最新待办（引擎不自己拉数据）。

        注意：这里绝不能触发 ``on_state_changed`` —— 本方法在悬浮球快照
        内部被调用，快照又由 request_refresh 触发，一旦发通知就会形成
        「刷新 → ingest → 通知 → 刷新」的自激循环，把轮询间隔打到 0、
        GIL 吃满，悬浮球动画直接卡死（2.2.0 踩过的坑）。
        """
        with self._lock:
            self._assignments = list(assignments or [])
            self._evaluate_locked()

    def read_ids(self) -> set[str]:
        return set(self.state.get("read") or {})

    def status(self) -> dict:
        return {
            "enabled": bool(self.state.get("enabled", True)),
            "gameAlertEnabled": bool(self.state.get("game_alert_enabled", False)),
            "autostart": get_autostart(),
            "games": list(self.state.get("games") or []),
            "gameIgnored": list(self.state.get("game_ignored") or []),
            "read": dict(self.state.get("read") or {}),
            "digestDate": str(self.state.get("digest_date") or ""),
        }

    def set_settings(
        self,
        enabled: bool | None = None,
        autostart: bool | None = None,
        game_alert_enabled: bool | None = None,
    ) -> dict:
        if enabled is not None:
            self.state["enabled"] = bool(enabled)
        if game_alert_enabled is not None:
            self.state["game_alert_enabled"] = bool(game_alert_enabled)
            if not self.state["game_alert_enabled"]:
                self._game_running.clear()
        if autostart is not None:
            if not set_autostart(bool(autostart)):
                self.log.warning("开机自启写入注册表失败")
        self._save_state()
        return self.status()

    def set_games(self, games: list[str]) -> dict:
        cleaned: list[str] = []
        for name in games or []:
            name = str(name).strip().lower()
            if name.endswith(".exe"):
                name = name[:-4]
            if name and _ID_RE.fullmatch(name) and name not in cleaned and len(cleaned) < 50:
                cleaned.append(name)
        self.state["games"] = cleaned
        self._game_running.clear()  # 名单变化后重新判定会话
        self._save_state()
        return {"games": cleaned}

    def mark_read(self, ids: list[str] | None = None, mark_all: bool = False) -> dict:
        read: dict = self.state.setdefault("read", {})
        now_iso = datetime.now().isoformat(timespec="seconds")
        if mark_all:
            for item in self._window_items():
                read[str(item.get("id"))] = now_iso
        else:
            for aid in ids or []:
                aid = str(aid)
                if _ID_RE.fullmatch(aid):
                    read[aid] = now_iso
        sent: dict = self.state.setdefault("sent", {})
        for aid in read:
            sent.pop(aid, None)  # 已阅后连已发档位记录一起清（不会再发，留着无用）
        self._save_state()
        if self.on_state_changed:
            self.on_state_changed()
        return {"read": len(read)}

    def handle_url(self, url: str) -> None:
        """处理 Toast 按钮 / 协议回传。"""
        try:
            parsed = urlparse(url)
            action = (parsed.netloc or "").strip().lower()
            arg = (parsed.path or "").strip("/")
            self.log.info("收到 Toast 回传：%s / %s", action, arg)
            if action == "read" and arg and _ID_RE.fullmatch(arg):
                self.mark_read([arg])
            elif action == "readall":
                self.mark_read(mark_all=True)
            elif action == "show":
                if self.on_show_main:
                    self.on_show_main()
            elif action == "addgame" and arg and _ID_RE.fullmatch(arg):
                name = arg.lower()
                if name.endswith(".exe"):
                    name = name[:-4]
                games = list(self.state.get("games") or [])
                if name not in games:
                    games.append(name)
                self.set_games(games)
            elif action == "ignoregame" and arg and _ID_RE.fullmatch(arg):
                name = arg.lower()
                if name.endswith(".exe"):
                    name = name[:-4]
                ignored = list(self.state.get("game_ignored") or [])
                if name not in ignored and len(ignored) < 100:
                    ignored.append(name)
                    self.state["game_ignored"] = ignored
                    self._save_state()
            else:
                self.log.debug("忽略未知回传：%s", url)
        except Exception:
            self.log.exception("处理 Toast 回传失败：%s", url)

    # -------------------------------------------------------------- 主循环

    def _loop(self) -> None:
        """10 秒一跳：游戏快照 + 兜底评估 + 3 小时强制同步。"""
        while not self._stop.wait(10.0):
            try:
                self._tick()
            except Exception:
                self.log.exception("提醒引擎循环异常")

    def _tick(self) -> None:
        # 1) 游戏检测（开关打开才扫进程表）
        if self.state.get("game_alert_enabled") and self.state.get("enabled", True):
            self._game_tick()
        # 2) 兜底评估（正常情况下 ingest 每 30 秒都会触发；这里兜悬停球挂掉/数据静止）
        with self._lock:
            self._evaluate_locked()
        # 3) 3 小时强制同步一次远端，随后让悬浮球重新取快照 → ingest 回来
        if time.time() - self._last_force >= 3 * 3600:
            self._last_force = time.time()
            self.log.info("后台定时同步作业数据")
            try:
                self.context.homework(force=True)
            except Exception:
                self.log.debug("后台同步失败（可能未登录），稍后再试")
            if self.on_state_changed:
                self.on_state_changed()

    # -------------------------------------------------------------- 评估与发送

    def _pending_items(self) -> list[dict]:
        """当前待办（去掉已阅），按剩余时间升序。"""
        read = self.read_ids()
        now = datetime.now()
        rows: list[dict] = []
        for item in self._assignments:
            if item.get("completed") or str(item.get("id")) in read:
                continue
            try:
                due = datetime.fromisoformat(str(item.get("dueDate")))
            except Exception:
                continue
            if due.tzinfo is not None:
                due = due.astimezone().replace(tzinfo=None)
            remaining = (due - now).total_seconds()
            rows.append({
                "id": str(item.get("id")),
                "name": str(item.get("name") or item.get("title") or "未命名作业").strip(),
                "course": str(item.get("course") or ""),
                "due": due,
                "remaining": remaining,
            })
        rows.sort(key=lambda row: row["remaining"])
        return rows

    def _window_items(self) -> list[dict]:
        """红标统计窗口（三天内 + 逾期）内的待办。"""
        return [row for row in self._pending_items() if row["remaining"] <= TIER_SOON_SECONDS]

    def _evaluate_locked(self) -> None:
        if not self.state.get("enabled", True):
            return
        self._maybe_prune()
        self._maybe_digest()
        self._evaluate_tiers()

    def _maybe_prune(self) -> None:
        if time.time() - self._last_prune < 3600:
            return
        self._last_prune = time.time()
        cutoff_read = (datetime.now() - timedelta(days=self.READ_RETENTION_DAYS)).isoformat(timespec="seconds")
        cutoff_sent = (datetime.now() - timedelta(days=self.SENT_RETENTION_DAYS)).isoformat(timespec="seconds")
        read = self.state.get("read") or {}
        self.state["read"] = {k: v for k, v in read.items() if str(v) >= cutoff_read}
        sent = self.state.get("sent") or {}
        cleaned: dict = {}
        for aid, tiers in sent.items():
            if isinstance(tiers, dict):
                tiers = {t: ts for t, ts in tiers.items() if str(ts) >= cutoff_sent}
                if tiers:
                    cleaned[aid] = tiers
        self.state["sent"] = cleaned

    def _maybe_digest(self) -> None:
        """每日汇总：8 点后第一次评估补发。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state.get("digest_date") == today:
            return
        if datetime.now().hour < 8:
            return
        self.state["digest_date"] = today
        self._save_state()
        items = self._window_items()
        if not items:
            return
        lines = []
        for row in items[:3]:
            when = "已逾期" if row["remaining"] <= 0 else fmt_remaining(row["remaining"])
            lines.append(f"{row['name']} · {when}")
        extra = len(items) - 3
        body = "\n".join(lines) + (f"\n…等 {len(items)} 项" if extra > 0 else "")
        send_toast(
            f"今天有 {len(items)} 项作业需要注意",
            body,
            [
                ("全部已阅", f"{TOAST_SCHEME}://readall"),
                ("打开主窗口", f"{TOAST_SCHEME}://show"),
            ],
        )
        self.log.info("已发每日汇总：%d 项", len(items))

    def _evaluate_tiers(self) -> None:
        sent: dict = self.state.setdefault("sent", {})
        for row in self._pending_items():
            tier = reminder_tier(row["remaining"])
            if tier is None:
                continue
            aid = row["id"]
            tiers = sent.setdefault(aid, {})
            if tier in tiers:
                continue
            self._send_tier_toast(row, tier)
            tiers[tier] = datetime.now().isoformat(timespec="seconds")
            self._save_state()

    def _send_tier_toast(self, row: dict, tier: str) -> None:
        due_text = row["due"].strftime("%m月%d日 %H:%M")
        actions = [
            ("已阅，别再提醒了", f"{TOAST_SCHEME}://read/{quote(row['id'])}"),
            ("打开主窗口", f"{TOAST_SCHEME}://show"),
        ]
        if tier == TIER_OVERDUE:
            title = f"「{row['name']}」已逾期"
            body = f"原定 {due_text} 截止，请尽快处理"
        elif tier == TIER_H1:
            title = f"「{row['name']}」还有 1 小时截止"
            body = f"{due_text} 截止" + (f" · {row['course']}" if row["course"] else "")
        else:
            title = f"「{row['name']}」明天这个时候截止"
            body = f"{due_text} 截止 · {fmt_remaining(row['remaining'])}"
        ok = send_toast(title, body, actions)
        self.log.info("发送%s提醒：%s（%s）", tier, row["name"], "成功" if ok else "失败")

    # -------------------------------------------------------------- 游戏检测

    def _game_tick(self) -> None:
        procs = snapshot_processes()
        if not procs:
            return
        names = set(procs.keys())
        games = set(self.state.get("games") or [])
        ignored = set(self.state.get("game_ignored") or [])

        # 名单命中：新出现的游戏进程 → 触发一次提醒
        hitting = names & games
        newly = hitting - self._game_running
        self._game_running = hitting
        if newly:
            self._fire_game_alert()

        # 自动建议：启动器新拉起的子进程
        launchers = {n for n in names if n in LAUNCHER_EXES}
        if launchers:
            self._pid_map = {
                info[0]: name for name, info in procs.items()
            }
            known = games | ignored | LAUNCHER_EXES | _SUGGEST_BLOCKLIST
            candidates: list[str] = []
            for name in names:
                if name in known:
                    continue
                pid, parent = procs[name]
                parent_name = self._pid_map.get(parent, "")
                if parent_name in LAUNCHER_EXES:
                    candidates.append(name)
            if candidates and time.time() - self._last_suggest >= _SUGGEST_COOLDOWN:
                self._last_suggest = time.time()
                self._send_suggest(candidates[0], self._pid_map.get(procs[candidates[0]][1], "?"))

    def _fire_game_alert(self) -> None:
        red = [row for row in self._pending_items() if row["remaining"] < TIER_RED_SECONDS]
        if not red:
            self.log.debug("游戏启动，但无红档作业，不打扰")
            return
        self.log.info("检测到游戏启动，弹游戏前提醒：%d 条红档作业", len(red))
        if self.on_game_red:
            try:
                self.on_game_red(red)
            except Exception:
                self.log.exception("游戏前提醒弹窗调用失败")

    def _send_suggest(self, exe: str, launcher: str) -> None:
        self.log.info("自动建议：%s（由 %s 启动）", exe, launcher)
        send_toast(
            "发现新游戏？",
            f"检测到 {exe} 刚由 {launcher} 启动，加入「打开游戏前提醒」名单吗？",
            [
                ("加入名单", f"{TOAST_SCHEME}://addgame/{quote(exe)}"),
                ("忽略", f"{TOAST_SCHEME}://ignoregame/{quote(exe)}"),
            ],
        )
