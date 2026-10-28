#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
作业备忘 · 本地服务端
=====================

把深大优课（TronClass / lms.szu.edu.cn）的待办作业同步到本地页面。

特点：
- 只监听 127.0.0.1，不对外网开放。
- 学号/密码使用 Windows DPAPI（当前 Windows 用户范围）加密后保存在本机。
- 首次登录成功后保存加密 Cookie；之后优先复用会话，失效时才使用保存的凭据自动登录。
- 非官方接口，页面结构/接口可能随学校系统升级变化。

运行：
    python homework_server.py

数据默认位于：
    %LOCALAPPDATA%\\HomeworkDesk\\data

可用环境变量：
    HOMEWORK_DESK_PORT       端口，默认 8765
    HOMEWORK_DESK_DATA_DIR   数据目录（测试时很有用）
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "缺少 requests 依赖。请使用已安装 requests 的 Python 运行，"
        "或在命令行执行: python -m pip install requests"
    ) from exc


APP_NAME = "HomeworkDesk"
APP_VERSION = "1.2.3"
APP_DIR = Path(__file__).resolve().parent


def resource_path(filename: str) -> Path:
    """兼容源码运行与 PyInstaller 打包后的资源路径。"""
    candidates = []
    if getattr(sys, "frozen", False):
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            candidates.append(Path(mei) / filename)
        candidates.append(Path(sys.executable).resolve().parent / filename)
    candidates.append(APP_DIR / filename)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]


APP_HTML = resource_path("homework-reminder.html")
BALL_HTML = resource_path("ball.html")
ICON_PATH = resource_path("homework-desk.ico")
DEFAULT_PORT = 8765
HOST = "127.0.0.1"

LMS_BASE = "https://lms.szu.edu.cn"
LMS_LOGIN_URL = f"{LMS_BASE}/login?next=/user/index"
TODOS_URL = f"{LMS_BASE}/api/todos"
AUTH_BASE = "https://authserver.szu.edu.cn"
CHECK_CAPTCHA_URL = f"{AUTH_BASE}/authserver/checkNeedCaptcha.htl"
CAPTCHA_IMAGE_URL = f"{AUTH_BASE}/authserver/getCaptcha.htl"

# 中国不实行夏令时，使用固定 UTC+8 可避免依赖 Windows tzdata/zoneinfo。
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

TYPE_LABELS = {
    "homework": "作业",
    "assignment": "作业",
    "exam": "考试",
    "quiz": "测验",
    "test": "测验",
    "questionnaire": "问卷",
    "survey": "问卷",
    "material": "资料",
    "document": "资料",
    "video": "视频",
    "rollcall": "签到",
    "attendance": "签到",
    "announcement": "公告",
    "discussion": "讨论",
    "forum": "讨论",
    "course": "课程",
}

TRUE_VALUES = {
    "true",
    "1",
    "yes",
    "y",
    "completed",
    "complete",
    "done",
    "finished",
    "submitted",
    "已完成",
    "完成",
}


# ============================================================================
# 路径与日志
# ============================================================================


def default_data_dir() -> Path:
    override = os.environ.get("HOMEWORK_DESK_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_NAME / "data"
    return Path.home() / "AppData" / "Local" / APP_NAME / "data"


def configure_logging(data_dir: Path, debug: bool = False) -> logging.Logger:
    data_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

    log_path = data_dir / "homework-desk.log"
    file_handler = RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # pythonw 下 stderr 可能不可用，因此只用文件日志即可。
    if sys.stderr and sys.stderr.isatty():
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)

    return logger


LOG = logging.getLogger(APP_NAME)


# ============================================================================
# Windows DPAPI：加密保存密码 / Cookie
# ============================================================================


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wt.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _make_blob(data: bytes):
    if not data:
        buffer = (ctypes.c_ubyte * 1)()
        return DATA_BLOB(0, ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return (
        DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))),
        buffer,
    )


def _dpapi_functions():
    if os.name != "nt":
        raise RuntimeError("DPAPI 仅支持 Windows。")
    if getattr(_dpapi_functions, "_loaded", False):
        return _dpapi_functions.crypt32, _dpapi_functions.kernel32

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wt.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wt.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wt.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wt.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    _dpapi_functions.crypt32 = crypt32
    _dpapi_functions.kernel32 = kernel32
    _dpapi_functions._loaded = True
    return crypt32, kernel32


def dpapi_protect(data: bytes, entropy: bytes = b"HomeworkDesk:v1") -> bytes:
    crypt32, kernel32 = _dpapi_functions()
    in_blob, _in_buffer = _make_blob(data)
    entropy_blob, _entropy_buffer = _make_blob(entropy)
    out_blob = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        APP_NAME,
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def dpapi_unprotect(data: bytes, entropy: bytes = b"HomeworkDesk:v1") -> bytes:
    crypt32, kernel32 = _dpapi_functions()
    in_blob, _in_buffer = _make_blob(data)
    entropy_blob, _entropy_buffer = _make_blob(entropy)
    out_blob = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


# ============================================================================
# 深大统一身份认证的密码加密（CryptoJS AES-CBC + PKCS7）
# ============================================================================


def _bcrypt_library():
    if os.name != "nt":
        raise RuntimeError("Windows CNG 仅支持 Windows。")
    if getattr(_bcrypt_library, "_loaded", False):
        return _bcrypt_library.bcrypt

    bcrypt = ctypes.WinDLL("bcrypt", use_last_error=True)
    handle = ctypes.c_void_p
    ulong = wt.ULONG

    bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(handle),
        wt.LPCWSTR,
        wt.LPCWSTR,
        ulong,
    ]
    bcrypt.BCryptOpenAlgorithmProvider.restype = ctypes.c_long
    bcrypt.BCryptCloseAlgorithmProvider.argtypes = [handle, ulong]
    bcrypt.BCryptCloseAlgorithmProvider.restype = ctypes.c_long
    bcrypt.BCryptSetProperty.argtypes = [
        handle,
        wt.LPCWSTR,
        ctypes.POINTER(ctypes.c_ubyte),
        ulong,
        ulong,
    ]
    bcrypt.BCryptSetProperty.restype = ctypes.c_long
    bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        handle,
        ctypes.POINTER(handle),
        ctypes.POINTER(ctypes.c_ubyte),
        ulong,
        ctypes.POINTER(ctypes.c_ubyte),
        ulong,
        ulong,
    ]
    bcrypt.BCryptGenerateSymmetricKey.restype = ctypes.c_long
    bcrypt.BCryptEncrypt.argtypes = [
        handle,
        ctypes.POINTER(ctypes.c_ubyte),
        ulong,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ubyte),
        ulong,
        ctypes.POINTER(ctypes.c_ubyte),
        ulong,
        ctypes.POINTER(ulong),
        ulong,
    ]
    bcrypt.BCryptEncrypt.restype = ctypes.c_long
    bcrypt.BCryptDestroyKey.argtypes = [handle]
    bcrypt.BCryptDestroyKey.restype = ctypes.c_long

    _bcrypt_library.bcrypt = bcrypt
    _bcrypt_library._loaded = True
    return bcrypt


def aes_cbc_encrypt_pkcs7(data: bytes, key: bytes, iv: bytes) -> bytes:
    bcrypt = _bcrypt_library()
    handle = ctypes.c_void_p
    ulong = wt.ULONG

    h_alg = handle()
    status = bcrypt.BCryptOpenAlgorithmProvider(
        ctypes.byref(h_alg), "AES", None, 0
    )
    if status:
        raise OSError(f"BCryptOpenAlgorithmProvider failed: 0x{status & 0xFFFFFFFF:08X}")

    try:
        mode = ctypes.create_unicode_buffer("ChainingModeCBC")
        status = bcrypt.BCryptSetProperty(
            h_alg,
            "ChainingMode",
            ctypes.cast(mode, ctypes.POINTER(ctypes.c_ubyte)),
            ctypes.sizeof(mode),
            0,
        )
        if status:
            raise OSError(f"BCryptSetProperty failed: 0x{status & 0xFFFFFFFF:08X}")

        key_buffer = (ctypes.c_ubyte * len(key)).from_buffer_copy(key)
        h_key = handle()
        status = bcrypt.BCryptGenerateSymmetricKey(
            h_alg,
            ctypes.byref(h_key),
            None,
            0,
            key_buffer,
            len(key),
            0,
        )
        if status:
            raise OSError(f"BCryptGenerateSymmetricKey failed: 0x{status & 0xFFFFFFFF:08X}")

        try:
            data_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            iv_buffer = (ctypes.c_ubyte * 16).from_buffer_copy(iv)
            output_len = ulong()

            # 第一遍获取带 PKCS7 填充后的长度。
            status = bcrypt.BCryptEncrypt(
                h_key,
                data_buffer,
                len(data),
                None,
                iv_buffer,
                16,
                None,
                0,
                ctypes.byref(output_len),
                0x1,  # BCRYPT_BLOCK_PADDING
            )
            if status:
                raise OSError(f"BCryptEncrypt(size) failed: 0x{status & 0xFFFFFFFF:08X}")

            output = (ctypes.c_ubyte * output_len.value)()
            # BCryptEncrypt 会修改 IV 缓冲，重新复制一遍。
            iv_buffer = (ctypes.c_ubyte * 16).from_buffer_copy(iv)
            written = ulong()
            status = bcrypt.BCryptEncrypt(
                h_key,
                data_buffer,
                len(data),
                None,
                iv_buffer,
                16,
                output,
                output_len.value,
                ctypes.byref(written),
                0x1,
            )
            if status:
                raise OSError(f"BCryptEncrypt failed: 0x{status & 0xFFFFFFFF:08X}")

            return bytes(output[: written.value])
        finally:
            bcrypt.BCryptDestroyKey(h_key)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


def _random_aes_string(length: int) -> str:
    return "".join(secrets.choice(AES_CHARS) for _ in range(length))


def encrypt_szu_password(password: str, salt: str) -> str:
    """
    复刻学校登录页 encrypt.js 的逻辑：
      base64(AES-CBC(randomString(64) + password, key=salt, iv=randomString(16)))
    """
    if not salt:
        raise ValueError("登录页缺少 pwdEncryptSalt，可能页面结构已变化。")
    prefix = _random_aes_string(64)
    iv = _random_aes_string(16)
    plaintext = (prefix + password).encode("utf-8")
    ciphertext = aes_cbc_encrypt_pkcs7(plaintext, salt.encode("utf-8"), iv.encode("utf-8"))
    return base64.b64encode(ciphertext).decode("ascii")


# ============================================================================
# HTML 表单解析
# ============================================================================


class PasswordFormParser(HTMLParser):
    """只提取 id=pwdFromId 的账号密码表单。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found = False
        self._inside = False
        self.action = ""
        self.inputs: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag.lower() == "form":
            self._inside = attributes.get("id") == "pwdFromId"
            if self._inside:
                self.found = True
                self.action = attributes.get("action", "")
        elif tag.lower() == "input" and self._inside:
            self.inputs.append(
                {
                    "id": attributes.get("id", ""),
                    "name": attributes.get("name", ""),
                    "value": attributes.get("value", ""),
                }
            )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self._inside:
            self._inside = False

    def get_input(self, name: str, default: str = "") -> str:
        for item in self.inputs:
            if item["name"] == name:
                return item["value"]
        return default

    def get_by_id(self, input_id: str, default: str = "") -> str:
        for item in self.inputs:
            if item["id"] == input_id:
                return item["value"]
        return default


class LoginPage:
    def __init__(
        self,
        url: str,
        action: str,
        service: str,
        username: str,
        password: str,
        captcha: str,
        remember: bool,
        switch: str,
        salt: str,
        lt: str,
        execution: str,
        event_id: str,
    ) -> None:
        self.url = url
        self.action = action
        self.service = service
        self.username = username
        self.password = password
        self.captcha = captcha
        self.remember = remember
        self.switch = switch
        self.salt = salt
        self.lt = lt
        self.execution = execution
        self.event_id = event_id


class CaptchaRequired(Exception):
    def __init__(self, captcha_type: str, image_data_url: str = "", message: str = ""):
        self.captcha_type = captcha_type
        self.image_data_url = image_data_url
        self.message = message or "学校登录页要求完成人机验证。"
        super().__init__(self.message)


class AuthRequired(Exception):
    def __init__(self, message: str = "请先登录深大优课。"):
        self.message = message
        super().__init__(message)


class AuthFailed(Exception):
    def __init__(self, message: str = "登录失败，请检查学号和密码。"):
        self.message = message
        super().__init__(message)


class UpstreamError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


# ============================================================================
# 本地数据存储
# ============================================================================


class DataStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.credentials_path = root / "credentials.dat"
        self.session_path = root / "session.dat"
        self.prefs_path = root / "prefs.json"
        self.state_path = root / "state.json"

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            if not path.exists():
                return default
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            LOG.warning("读取 %s 失败：%s", path.name, exc)
            return default

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + f".{secrets.token_hex(4)}.tmp")
        with temp.open("wb") as handle:
            handle.write(data)
        os.replace(temp, path)

    def load_prefs(self) -> dict[str, Any]:
        data = self._read_json(self.prefs_path, {})
        return data if isinstance(data, dict) else {}

    def save_prefs(self, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self._atomic_write(self.prefs_path, encoded)

    def load_credentials(self) -> Optional[dict[str, Any]]:
        if not self.credentials_path.exists():
            return None
        try:
            raw = dpapi_unprotect(self.credentials_path.read_bytes())
            data = json.loads(raw.decode("utf-8"))
            if not data.get("username") or not data.get("password"):
                return None
            return data
        except Exception as exc:
            LOG.warning("读取加密凭据失败：%s", exc)
            return None

    def save_credentials(self, username: str, password: str) -> None:
        payload = json.dumps(
            {"username": username, "password": password},
            ensure_ascii=False,
        ).encode("utf-8")
        self._atomic_write(self.credentials_path, dpapi_protect(payload))

    def delete_credentials(self) -> None:
        try:
            self.credentials_path.unlink(missing_ok=True)
        except Exception as exc:
            LOG.warning("删除凭据失败：%s", exc)

    def load_session(self) -> list[dict[str, Any]]:
        if not self.session_path.exists():
            return []
        try:
            raw = dpapi_unprotect(self.session_path.read_bytes())
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, list) else []
        except Exception as exc:
            LOG.warning("读取加密会话失败：%s", exc)
            return []

    def save_session(self, cookies: list[dict[str, Any]]) -> None:
        payload = json.dumps(cookies, ensure_ascii=False).encode("utf-8")
        self._atomic_write(self.session_path, dpapi_protect(payload))

    def delete_session(self) -> None:
        try:
            self.session_path.unlink(missing_ok=True)
        except Exception as exc:
            LOG.warning("删除会话失败：%s", exc)

    def load_state(self) -> dict[str, Any]:
        data = self._read_json(self.state_path, {})
        if not isinstance(data, dict):
            return {}
        if not isinstance(data.get("completed"), dict):
            data["completed"] = {}
        return data

    def save_state(self, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self._atomic_write(self.state_path, encoded)

    def mark_completed(self, homework_id: str, title: str = "") -> None:
        state = self.load_state()
        state["completed"][homework_id] = {
            "title": title,
            "completedAt": now_iso(),
        }
        self.save_state(state)

    def completed_ids(self) -> set[str]:
        state = self.load_state()
        return set(state.get("completed", {}).keys())


# ============================================================================
# TronClass / 深大优课
# ============================================================================


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone(SHANGHAI).isoformat(timespec="seconds")


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if value is None:
        return False
    return str(value).strip().lower() in TRUE_VALUES


def parse_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1e12:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=SHANGHAI)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10,13}", text):
        return parse_datetime(int(text))

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    else:
        parsed = parsed.astimezone(SHANGHAI)
    return parsed


def normalize_todos(payload: Any, completed_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise UpstreamError("深大优课返回的数据格式不正确。")

    rows = payload.get("todo_list")
    if rows is None:
        rows = payload.get("todos")
    if not isinstance(rows, list):
        raise UpstreamError("深大优课待办列表中缺少 todo_list 字段。")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue

        homework_id = row.get("id") or row.get("activity_id") or row.get("todo_id")
        if homework_id is None:
            homework_id = hashlib.sha1(
                json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
        homework_id = str(homework_id)

        if homework_id in seen or homework_id in completed_ids:
            continue
        seen.add(homework_id)

        if to_bool(row.get("completed") or row.get("is_completed") or row.get("done")):
            continue

        due = parse_datetime(
            row.get("end_time")
            or row.get("due_at")
            or row.get("deadline")
            or row.get("due_date")
            or row.get("end_at")
        )
        if due is None:
            LOG.debug("跳过无截止时间的待办：%s", homework_id)
            continue

        title = str(
            row.get("title")
            or row.get("name")
            or row.get("activity_name")
            or "未命名作业"
        ).strip()

        course = str(
            row.get("course_name")
            or row.get("course_title")
            or row.get("course")
            or "深大优课"
        ).strip()

        raw_type = str(row.get("type") or row.get("activity_type") or "").strip()
        type_label = TYPE_LABELS.get(raw_type.lower(), raw_type or "待办")

        notes = (
            row.get("description")
            or row.get("remark")
            or row.get("content")
            or ""
        )
        notes = re.sub(r"<[^>]+>", " ", str(notes))
        notes = re.sub(r"\s+", " ", unescape(notes)).strip()

        normalized.append(
            {
                "id": homework_id,
                "name": title,
                "course": course,
                "dueDate": due.isoformat(timespec="minutes"),
                "completed": False,
                "notes": notes,
                "type": type_label,
                "source": "深大优课",
            }
        )

    return normalized


def cookies_to_json(cookies) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for cookie in cookies:
        result.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": bool(cookie.secure),
                "expires": cookie.expires,
                "http_only": bool(cookie._rest.get("HttpOnly")) if hasattr(cookie, "_rest") else False,
            }
        )
    return result


def load_cookies_into_session(session: requests.Session, cookies: list[dict[str, Any]]) -> None:
    from http.cookiejar import Cookie

    now = time.time()
    for item in cookies:
        try:
            expires = item.get("expires")
            if expires is not None and float(expires) < now:
                continue
            domain = str(item.get("domain") or "")
            if not domain:
                continue
            rest = {}
            if item.get("http_only"):
                rest["HttpOnly"] = True
            cookie = Cookie(
                version=0,
                name=str(item["name"]),
                value=str(item["value"]),
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=domain.startswith("."),
                domain_initial_dot=domain.startswith("."),
                path=str(item.get("path") or "/"),
                path_specified=True,
                secure=bool(item.get("secure")),
                expires=int(expires) if expires is not None else None,
                discard=False,
                comment=None,
                comment_url=None,
                rest=rest,
                rfc2109=False,
            )
            session.cookies.set_cookie(cookie)
        except Exception as exc:
            LOG.debug("恢复 Cookie 失败：%s", exc)


class SzuAuth:
    def __init__(self, store: DataStore):
        self.store = store
        self.lock = threading.RLock()
        self.session = self._new_session()
        self.credentials = store.load_credentials()
        self.prefs = store.load_prefs()
        self.username = str(
            (self.credentials or {}).get("username")
            or self.prefs.get("last_username")
            or ""
        )
        self.logged_in = False
        self.last_error = ""
        self.last_verified_at = 0.0
        self.last_failure_at = 0.0
        self.busy = False
        self.pending_login: Optional[dict[str, Any]] = None
        self._todos_cache: Optional[tuple[float, dict[str, Any]]] = None
        self._auto_started = False

        saved_cookies = store.load_session()
        self.has_saved_session = bool(saved_cookies)
        if saved_cookies:
            load_cookies_into_session(self.session, saved_cookies)

    @staticmethod
    def _new_session() -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            }
        )
        return session

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def has_credentials(self) -> bool:
        return bool(self.credentials and self.credentials.get("username"))

    @property
    def has_session_cookie(self) -> bool:
        return bool(self.has_saved_session or self.logged_in)

    def status(self) -> dict[str, Any]:
        return {
            "authenticated": bool(self.logged_in),
            "configured": bool(self.has_credentials or self.has_saved_session or self.logged_in),
            "autoLogin": bool(self.has_credentials),
            "username": self.username,
            "busy": self.busy,
            "lastError": self.last_error,
            "lastVerifiedAt": (
                datetime.fromtimestamp(self.last_verified_at, tz=SHANGHAI).isoformat(
                    timespec="seconds"
                )
                if self.last_verified_at
                else None
            ),
        }

    def _set_error(self, message: str) -> None:
        self.last_error = message

    def _clear_error(self) -> None:
        self.last_error = ""

    def _set_logged_in(self, username: str = "") -> None:
        self.logged_in = True
        self.last_verified_at = time.time()
        self.last_failure_at = 0.0
        self._clear_error()
        if username:
            self.username = username

    def _invalidate_session(self) -> None:
        self.logged_in = False
        self.last_verified_at = 0.0
        self.session = self._new_session()
        self._todos_cache = None
        self.has_saved_session = False
        self.store.delete_session()

    def _save_session(self) -> None:
        try:
            self.store.save_session(cookies_to_json(self.session.cookies))
            self.has_saved_session = True
        except Exception as exc:
            LOG.warning("保存登录会话失败：%s", exc)

    # ------------------------------------------------------------------
    # 启动时自动登录
    # ------------------------------------------------------------------
    def start_auto_login(self) -> None:
        with self.lock:
            if self._auto_started:
                return
            self._auto_started = True
            if not (self.has_credentials or self.has_session_cookie):
                return
        thread = threading.Thread(
            target=self._warm_up_worker, name="auto-login", daemon=True
        )
        thread.start()

    def _warm_up_worker(self) -> None:
        # 给 HTTP 服务一点启动时间；同时避免与页面请求争抢。
        time.sleep(0.25)
        try:
            LOG.info("开始自动登录深大优课…")
            todos = self.get_todos(force=True)
            LOG.info("自动登录成功，待办 %s 项。", len(todos.get("todo_list", [])))
        except AuthRequired:
            LOG.info("自动登录跳过：本机尚未保存账号。")
        except CaptchaRequired as exc:
            self._set_error(exc.message)
            LOG.warning("自动登录需要人机验证：%s", exc.captcha_type)
        except AuthFailed as exc:
            self._set_error(exc.message)
            LOG.warning("自动登录失败：%s", exc.message)
        except Exception as exc:
            self._set_error(f"自动登录失败：{exc}")
            LOG.exception("自动登录异常")

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    def _load_login_page(self, username: str, remember: bool) -> LoginPage:
        response = self.session.get(
            LMS_LOGIN_URL,
            timeout=30,
            allow_redirects=True,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        response.raise_for_status()
        page_url = response.url
        text = response.content.decode("utf-8", errors="replace")

        parser = PasswordFormParser()
        parser.feed(text)
        if not parser.found:
            if "lms.szu.edu.cn" in page_url and "authserver" not in page_url:
                # 可能已有有效会话，交给 /api/todos 验证。
                raise AuthFailed("当前会话可能已登录，正在验证。")
            raise AuthFailed("未找到深大统一身份认证的登录表单，学校登录页可能已改版。")

        service = ""
        query = parse_qs(urlparse(page_url).query)
        if query.get("service"):
            service = query["service"][0]
        else:
            match = re.search(r"var\s+service\s*=\s*(\[[^\n;]*\])\s*;", text)
            if match:
                try:
                    services = json.loads(match.group(1))
                    if services:
                        service = str(services[0])
                except Exception:
                    pass

        switch_match = re.search(r"captchaSwitch\s*=\s*[\"']([^\"']*)[\"']", text)
        switch = switch_match.group(1) if switch_match else "0"
        salt = parser.get_by_id("pwdEncryptSalt")
        lt = parser.get_input("lt")
        execution = parser.get_input("execution", "e1s1")
        event_id = parser.get_input("_eventId", "submit")
        action = urljoin(page_url, parser.action or "/authserver/login")

        return LoginPage(
            url=page_url,
            action=action,
            service=service,
            username=username,
            password="",
            captcha="",
            remember=remember,
            switch=switch,
            salt=salt,
            lt=lt,
            execution=execution,
            event_id=event_id,
        )

    def _check_need_captcha(self, page: LoginPage) -> None:
        try:
            response = self.session.get(
                CHECK_CAPTCHA_URL,
                params={"username": page.username},
                timeout=20,
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": page.url,
                },
            )
            data = response.json()
        except Exception as exc:
            LOG.debug("检查是否需要验证码失败（忽略）：%s", exc)
            return

        if not data.get("isNeed"):
            return

        if page.switch == "2":
            raise CaptchaRequired(
                "slider",
                message=(
                    "学校登录页要求滑块验证。请稍后重试；如果持续出现，"
                    "需要先在浏览器中完成一次登录。"
                ),
            )

        captcha_type = "image"
        image_data_url = ""
        try:
            image_response = self.session.get(
                CAPTCHA_IMAGE_URL,
                params={"_": str(int(time.time() * 1000))},
                timeout=20,
                headers={"Referer": page.url},
            )
            image_response.raise_for_status()
            content_type = image_response.headers.get("Content-Type", "image/png").split(";")[0]
            encoded = base64.b64encode(image_response.content).decode("ascii")
            image_data_url = f"data:{content_type};base64,{encoded}"
        except Exception as exc:
            LOG.warning("获取图形验证码失败：%s", exc)

        raise CaptchaRequired(captcha_type, image_data_url, "请输入登录页显示的图形验证码。")

    @staticmethod
    def _decode_response(response: requests.Response) -> str:
        content = response.content
        text = content.decode("utf-8", errors="replace")
        if text.count("\ufffd") > max(2, len(text) // 100):
            for encoding in ("gb18030", "gbk"):
                try:
                    text = content.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
        return text

    def _extract_login_error(self, response: requests.Response) -> str:
        text = self._decode_response(response)
        patterns = [
            r'id=["\']showErrorTip["\'][^>]*>(.*?)</span>',
            r'id=["\']formErrorTip["\'][^>]*>(.*?)</div>',
            r'class=["\'][^"\']*error[^"\']*["\'][^>]*>(.*?)</',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.S | re.I)
            if match:
                message = re.sub(r"<[^>]+>", " ", match.group(1))
                message = re.sub(r"\s+", " ", unescape(message)).strip()
                if message:
                    return message

        if "密码错误" in text or "用户名或密码" in text:
            return "学号或密码错误。"
        if "验证码" in text:
            return "验证码校验失败，请重试。"
        if "锁定" in text or "冻结" in text:
            return "账号可能已被临时锁定，请稍后或联系学校信息中心。"
        if "captcha" in text.lower():
            return "需要完成登录验证码。"
        return "登录失败，请检查学号和密码。"

    def _post_login(self, page: LoginPage) -> None:
        if not page.salt:
            raise AuthFailed("登录页缺少密码加密参数，学校登录页可能已改版。")

        encrypted_password = encrypt_szu_password(page.password, page.salt)
        payload: dict[str, str] = {
            "username": page.username,
            "password": encrypted_password,
            "_eventId": page.event_id or "submit",
            "cllt": "userNameLogin",
            "dllt": "generalLogin",
            "execution": page.execution or "e1s1",
        }
        if page.lt:
            payload["lt"] = page.lt
        if page.remember:
            payload["rememberMe"] = "true"
        if page.captcha:
            payload["captcha"] = page.captcha

        response = self.session.post(
            page.action,
            params={"service": page.service} if page.service else None,
            data=payload,
            timeout=40,
            allow_redirects=True,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": AUTH_BASE,
                "Referer": page.url,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        if response.status_code >= 400:
            raise AuthFailed(f"统一身份认证返回异常（HTTP {response.status_code}）。")

        # 最终仍停留在 authserver 登录页，通常意味着密码/验证码错误。
        final_url = response.url
        if "authserver.szu.edu.cn" in final_url and "/authserver/login" in final_url:
            raise AuthFailed(self._extract_login_error(response))

    def _fetch_todos_payload(self) -> dict[str, Any]:
        response = self.session.get(
            TODOS_URL,
            timeout=30,
            allow_redirects=False,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{LMS_BASE}/user/index",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "")
            if "login" in location or "authserver" in location or "identity" in location:
                raise AuthRequired("登录会话已失效，请重新登录。")
            raise UpstreamError("深大优课返回了意外的重定向。")
        if response.status_code in (401, 403):
            raise AuthRequired("登录会话已失效，请重新登录。")
        if response.status_code != 200:
            raise UpstreamError(f"深大优课接口请求失败（HTTP {response.status_code}）。")

        content_type = response.headers.get("Content-Type", "")
        if "json" not in content_type.lower():
            raise AuthRequired("深大优课返回了登录页面，登录会话已失效。")

        try:
            data = response.json()
        except ValueError as exc:
            raise UpstreamError("深大优课返回的数据不是有效 JSON。") from exc
        if not isinstance(data, dict):
            raise UpstreamError("深大优课返回的数据格式不正确。")
        return data

    def _do_login(self, username: str, password: str, remember: bool, captcha: str = "") -> None:
        cached_pending = self.pending_login
        can_reuse_pending = bool(
            captcha
            and cached_pending
            and cached_pending["page"].username == username
        )
        if can_reuse_pending:
            page = cached_pending["page"]
            # 凭据可能已在 UI 中修改，使用最新值。
            page.password = password
            page.remember = remember
        else:
            self.pending_login = None
            self.session = self._new_session()
            page = self._load_login_page(username, remember)
            page.password = password

        self.pending_login = {"page": page}

        if not captcha:
            # 首次登录前检查学校是否要求验证码。若要求，抛出异常让前端展示验证码。
            self._check_need_captcha(page)
        else:
            page.captcha = captcha

        self._post_login(page)

        try:
            todos = self._fetch_todos_payload()
        except AuthRequired:
            # 有些情况下登录仍在 Keycloak 中完成，等待后再试一次。
            time.sleep(0.8)
            try:
                todos = self._fetch_todos_payload()
            except AuthRequired as exc:
                raise AuthFailed(
                    "统一身份认证已通过，但未能进入深大优课。"
                    "学校可能新增了二次确认，请稍后重试或先打开深大优课完成一次登录。"
                ) from exc

        self.pending_login = None
        self._set_logged_in(username)
        self.prefs["last_username"] = username
        self.store.save_prefs(self.prefs)
        if remember:
            self.store.save_credentials(username, password)
            self.credentials = {"username": username, "password": password}
        else:
            self.store.delete_credentials()
            self.credentials = None
        self._save_session()
        self._todos_cache = (time.time(), todos)
        LOG.info("深大优课登录成功（用户 %s）。", username)

    def manual_login(
        self, username: str, password: str, remember: bool, captcha: str = ""
    ) -> None:
        with self.lock:
            self.busy = True
            self.last_failure_at = 0.0
            try:
                # 只有“带验证码继续”时才复用待验证会话；普通登录从干净会话开始。
                if not (captcha and self.pending_login):
                    self.session = self._new_session()
                    self.pending_login = None
                self._do_login(username, password, remember, captcha)
            except CaptchaRequired:
                self.logged_in = False
                raise
            except AuthFailed as exc:
                self.logged_in = False
                self.pending_login = None
                self._set_error(exc.message)
                self.last_failure_at = time.time()
                raise
            except Exception as exc:
                self.logged_in = False
                self.pending_login = None
                self._set_error(f"登录异常：{exc}")
                raise
            finally:
                self.busy = False

    def _auto_login(self) -> None:
        if not self.credentials:
            raise AuthRequired("尚未保存登录凭据。")
        if time.time() - self.last_failure_at < 300:
            raise AuthFailed(self.last_error or "自动登录刚刚失败，请稍后手动重试。")

        self.busy = True
        try:
            self.session = self._new_session()
            username = str(self.credentials["username"])
            password = str(self.credentials["password"])
            self._do_login(username, password, remember=True)
        finally:
            self.busy = False

    # ------------------------------------------------------------------
    # 取数
    # ------------------------------------------------------------------
    def get_todos(self, force: bool = False) -> dict[str, Any]:
        with self.lock:
            if not force and self._todos_cache:
                cached_at, cached_payload = self._todos_cache
                if time.time() - cached_at < 20:
                    return cached_payload

            if not self.logged_in:
                if self.has_saved_session:
                    try:
                        payload = self._fetch_todos_payload()
                        self._set_logged_in(self.username)
                        self._save_session()
                        self._todos_cache = (time.time(), payload)
                        return payload
                    except AuthRequired:
                        self.logged_in = False
                        self.has_saved_session = False
                        self._todos_cache = None
                        self.store.delete_session()
                        self.session = self._new_session()
                    except UpstreamError:
                        raise

                if self.credentials:
                    self._auto_login()
                    if self._todos_cache:
                        return self._todos_cache[1]
                    payload = self._fetch_todos_payload()
                    self._todos_cache = (time.time(), payload)
                    return payload

                raise AuthRequired("请先登录深大优课，之后会自动同步作业。")

            try:
                payload = self._fetch_todos_payload()
            except AuthRequired:
                self._invalidate_session()
                if self.credentials:
                    self._auto_login()
                    if self._todos_cache:
                        return self._todos_cache[1]
                    payload = self._fetch_todos_payload()
                else:
                    raise AuthRequired("登录已过期，请重新登录深大优课。")

            self.last_verified_at = time.time()
            self._clear_error()
            self._todos_cache = (time.time(), payload)
            self._save_session()
            return payload

    def logout(self, forget: bool = False) -> None:
        with self.lock:
            self.logged_in = False
            self.last_verified_at = 0.0
            self._todos_cache = None
            self.pending_login = None
            self.session = self._new_session()
            self.store.delete_session()
            self.has_saved_session = False
            if forget:
                self.store.delete_credentials()
                self.credentials = None
                self.username = ""
                prefs = self.store.load_prefs()
                prefs.pop("last_username", None)
                self.store.save_prefs(prefs)
                self.prefs = prefs
            self._clear_error()


# ============================================================================
# 本地 API 服务
# ============================================================================


class AppContext:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.store = DataStore(data_dir)
        self.auth = SzuAuth(self.store)
        self.started_at = now_iso()

    def homework(self, force: bool = False) -> dict[str, Any]:
        payload = self.auth.get_todos(force=force)
        completed = self.store.completed_ids()
        assignments = normalize_todos(payload, completed)
        return {
            "assignments": assignments,
            "updatedAt": now_iso(),
            "user": {
                "username": self.auth.username,
            },
            "source": "深大优课",
            "completionMode": "local",
            "count": len(assignments),
        }


class ApiHandler(BaseHTTPRequestHandler):
    server_version = f"HomeworkDesk/{APP_VERSION}"
    protocol_version = "HTTP/1.1"

    @property
    def context(self) -> AppContext:
        return self.server.context  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # 不记录 Cookie / 表单内容，只记录请求行和状态码。
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _check_host(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in {"127.0.0.1", "localhost", "::1"}

    def _check_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.scheme in {"http", "https"} and parsed.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _send_html(self, body: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 64 * 1024:
            raise ValueError("请求体过大。")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("请求体不是有效的 JSON。") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象。")
        return data

    def _guard(self, require_origin: bool = False) -> bool:
        if not self._check_host():
            self._send_json(403, {"code": "FORBIDDEN", "message": "只允许本机访问。"})
            return False
        if require_origin and not self._check_origin():
            self._send_json(403, {"code": "FORBIDDEN", "message": "请求来源不被允许。"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard():
            return

        parsed = urlparse(self.path)
        path = parsed.path

        page_map = {
            "/": APP_HTML,
            "/index.html": APP_HTML,
            "/homework-reminder.html": APP_HTML,
            "/ball": BALL_HTML,
            "/ball.html": BALL_HTML,
        }
        if path in page_map:
            page_path = page_map[path]
            try:
                if not page_path.exists():
                    self._send_json(500, {"code": "MISSING_UI", "message": "找不到页面文件。"})
                    return
                self._send_html(page_path.read_bytes())
            except Exception as exc:
                LOG.exception("读取页面失败")
                self._send_json(500, {"code": "SERVER_ERROR", "message": str(exc)})
            return

        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/api/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "app": APP_NAME,
                    "version": APP_VERSION,
                    "startedAt": self.context.started_at,
                },
            )
            return

        if path == "/api/status":
            payload = self.context.auth.status()
            payload["source"] = "深大优课"
            payload["completionMode"] = "local"
            self._send_json(200, payload)
            return

        if path == "/api/homework":
            query = parse_qs(parsed.query)
            force = query.get("refresh", ["0"])[0] in {"1", "true", "yes"}
            try:
                payload = self.context.homework(force=force)
                payload["status"] = self.context.auth.status()
                self._send_json(200, payload)
            except AuthRequired as exc:
                self._send_json(
                    401,
                    {
                        "code": "AUTH_REQUIRED",
                        "message": exc.message,
                        "status": self.context.auth.status(),
                    },
                )
            except CaptchaRequired as exc:
                self._send_json(
                    401,
                    {
                        "code": "CAPTCHA_REQUIRED",
                        "message": exc.message,
                        "captchaType": exc.captcha_type,
                        "captchaImage": exc.image_data_url,
                        "status": self.context.auth.status(),
                    },
                )
            except AuthFailed as exc:
                self._send_json(
                    401,
                    {
                        "code": "AUTH_FAILED",
                        "message": exc.message,
                        "status": self.context.auth.status(),
                    },
                )
            except UpstreamError as exc:
                self._send_json(
                    502,
                    {
                        "code": "UPSTREAM_ERROR",
                        "message": exc.message,
                        "status": self.context.auth.status(),
                    },
                )
            except requests.RequestException as exc:
                LOG.warning("连接深大优课失败：%s", exc)
                self._send_json(
                    502,
                    {
                        "code": "UPSTREAM_ERROR",
                        "message": "无法连接深大优课，请检查网络后重试。",
                        "status": self.context.auth.status(),
                    },
                )
            except Exception as exc:
                LOG.exception("获取作业失败")
                self._send_json(
                    500,
                    {
                        "code": "SERVER_ERROR",
                        "message": f"本地服务处理失败：{exc}",
                        "status": self.context.auth.status(),
                    },
                )
            return

        self._send_json(404, {"code": "NOT_FOUND", "message": "页面不存在。"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard(require_origin=True):
            return

        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/login":
            try:
                data = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"code": "BAD_REQUEST", "message": str(exc)})
                return

            username = str(data.get("username") or "").strip()
            password = str(data.get("password") or "")
            captcha = str(data.get("captcha") or "").strip()
            remember = bool(data.get("remember", True))

            if not username or not password:
                self._send_json(
                    400,
                    {"code": "VALIDATION_ERROR", "message": "请输入学号和密码。"},
                )
                return
            if len(username) > 100 or len(password) > 256 or len(captcha) > 10:
                self._send_json(
                    400,
                    {"code": "VALIDATION_ERROR", "message": "输入内容长度不正确。"},
                )
                return

            try:
                self.context.auth.manual_login(username, password, remember, captcha)
                try:
                    payload = self.context.homework(force=True)
                    count = len(payload["assignments"])
                except Exception:
                    count = 0
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "message": "登录成功。",
                        "user": {"username": username},
                        "count": count,
                        "status": self.context.auth.status(),
                    },
                )
            except CaptchaRequired as exc:
                self._send_json(
                    428,
                    {
                        "code": "CAPTCHA_REQUIRED",
                        "message": exc.message,
                        "captchaType": exc.captcha_type,
                        "captchaImage": exc.image_data_url,
                        "status": self.context.auth.status(),
                    },
                )
            except AuthFailed as exc:
                self._send_json(
                    401,
                    {
                        "code": "AUTH_FAILED",
                        "message": exc.message,
                        "status": self.context.auth.status(),
                    },
                )
            except requests.RequestException as exc:
                LOG.warning("登录时连接失败：%s", exc)
                self._send_json(
                    502,
                    {
                        "code": "UPSTREAM_ERROR",
                        "message": "连接学校登录服务失败，请检查网络。",
                        "status": self.context.auth.status(),
                    },
                )
            except Exception as exc:
                LOG.exception("登录异常")
                self._send_json(
                    500,
                    {
                        "code": "SERVER_ERROR",
                        "message": f"登录过程出错：{exc}",
                        "status": self.context.auth.status(),
                    },
                )
            return

        if path == "/api/logout":
            try:
                data = self._read_json_body()
            except ValueError:
                data = {}
            forget = bool(data.get("forget", False))
            self.context.auth.logout(forget=forget)
            self._send_json(200, {"ok": True, "status": self.context.auth.status()})
            return

        match = re.fullmatch(r"/api/homework/(.+)/complete", path)
        if match:
            homework_id = unquote(match.group(1))
            if not homework_id or len(homework_id) > 200:
                self._send_json(400, {"code": "BAD_REQUEST", "message": "作业 ID 无效。"})
                return
            try:
                data = self._read_json_body()
            except ValueError:
                data = {}
            title = str(data.get("name") or "")
            self.context.store.mark_completed(homework_id, title)
            self._send_json(
                200,
                {
                    "ok": True,
                    "id": homework_id,
                    "message": "已标记完成（仅保存在本应用）。",
                    "completionMode": "local",
                },
            )
            return

        self._send_json(404, {"code": "NOT_FOUND", "message": "接口不存在。"})


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, context: AppContext):
        self.context = context
        super().__init__(server_address, RequestHandlerClass)


# ============================================================================
# 入口
# ============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="作业备忘本地服务")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("HOMEWORK_DESK_PORT", DEFAULT_PORT)),
        help="监听端口，默认 8765",
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("HOMEWORK_DESK_DATA_DIR", str(default_data_dir())),
        help="数据目录",
    )
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir).expanduser().resolve()
    global LOG
    LOG = configure_logging(data_dir, debug=args.debug)

    if not APP_HTML.exists():
        LOG.error("页面文件不存在：%s", APP_HTML)
        return 1

    LOG.info("启动作业备忘 v%s，端口 %s，数据目录 %s", APP_VERSION, args.port, data_dir)
    context = AppContext(data_dir)
    context.auth.start_auto_login()

    try:
        server = LocalServer((HOST, args.port), ApiHandler, context)
    except OSError as exc:
        # 端口已占用通常表示应用已经在运行，启动器仍会打开页面。
        LOG.info("端口 %s 不可用，可能应用已在运行：%s", args.port, exc)
        return 0

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOG.info("收到退出信号。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
