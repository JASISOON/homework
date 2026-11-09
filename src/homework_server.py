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
import io
import json
import logging
import os
import re
import secrets
import socket
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape, unescape
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

import themes as theme_mod
from oj_client import (
    OjCaptchaWrong,
    OjError,
    OjHub,
    OjLoginFailed,
    OjUnreachable,
)


APP_NAME = "HomeworkDesk"
APP_VERSION = "2.2.1"
APP_DIR = Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    """兼容源码运行与 PyInstaller 打包后的资源路径。

    源码布局下资源在仓库根的 ``web/``、``assets/`` 里，而本文件位于 ``src/``，
    所以要往上跳一层；打包后资源按 spec 里的同名目录落到 ``_MEIPASS``。
    """
    relative = Path(*parts)
    candidates = []
    if getattr(sys, "frozen", False):
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            candidates.append(Path(mei) / relative)
        candidates.append(Path(sys.executable).resolve().parent / relative)
    candidates.append(APP_DIR.parent / relative)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]


APP_HTML = resource_path("web", "homework-reminder.html")
BALL_HTML = resource_path("web", "ball.html")
ICON_PATH = resource_path("assets", "homework-desk.ico")
DEFAULT_PORT = 8765
HOST = "127.0.0.1"

LMS_BASE = "https://lms.szu.edu.cn"
LMS_LOGIN_URL = f"{LMS_BASE}/login?next=/user/index"
TODOS_URL = f"{LMS_BASE}/api/todos"
# 待办列表只有标题和截止时间，正文与附件要去活动详情接口拿。
ACTIVITY_URL = LMS_BASE + "/api/activities/{activity_id}"
# 附件下载要先换一个 media 的临时直链，两条接口给的东西不一样：
#   /api/uploads/{id}/url            → 原文件，文件名和格式都不变（首选）
#   /api/uploads/document/{id}/url   → 平台转码后的 PDF，只用于原文件接口不可用时兜底
UPLOAD_ORIGINAL_URL_API = LMS_BASE + "/api/uploads/{upload_id}/url"
UPLOAD_URL_API = LMS_BASE + "/api/uploads/document/{upload_id}/url"
# 交作业（1.5.0）：先建上传记录 → 传字节 → 把 upload id 塞进提交体。
# 这几个路径取自学生端网页自己发的请求，改动前请对照 HANDOVER.md。
PROFILE_URL = LMS_BASE + "/api/profile"
SUBMISSION_URL = LMS_BASE + "/api/course/activities/{activity_id}/students/{student_id}/submission"
SUBMISSION_LIST_URL = (
    LMS_BASE + "/api/activities/{activity_id}/students/{student_id}/submission_list"
)
SUBMISSION_CREATE_URL = LMS_BASE + "/api/course/activities/{activity_id}/submissions"
SUBMISSION_RETRACT_URL = LMS_BASE + "/api/submissions/{submission_id}/retract"
UPLOAD_CREATE_URL = LMS_BASE + "/api/uploads"
UPLOAD_CALLBACK_URL = LMS_BASE + "/internal-api/upload/callback/{upload_id}"
# 上传记录要挂一个父对象；作业附件在网页上是挂在活动上的。
UPLOAD_PARENT_TYPES = ("homework", "learning_activity")
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
        self.oj_session_path = root / "oj_session.dat"
        self.prefs_path = root / "prefs.json"
        self.state_path = root / "state.json"
        self.themes_path = root / "themes.json"

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

    # ------------------------------------------------------------------ OJ 会话
    # 2.1.0 起一个用户可能同时挂着多个 OJ 站点，文件里存 {"sites": {base: record}}。
    # 旧版本写的是扁平的单条 record，读的时候自动升级，不丢已有登录状态。
    def load_oj_sessions(self) -> dict[str, dict[str, Any]]:
        if not self.oj_session_path.exists():
            return {}
        try:
            raw = dpapi_unprotect(self.oj_session_path.read_bytes())
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            LOG.warning("读取 OJ 会话失败：%s", exc)
            return {}
        if not isinstance(data, dict):
            return {}
        sites = data.get("sites")
        if isinstance(sites, dict):
            return {
                str(base).rstrip("/"): record
                for base, record in sites.items()
                if isinstance(record, dict) and record.get("sessionid")
            }
        # 旧格式：单条 record
        if data.get("sessionid"):
            base = str(data.get("base") or "").rstrip("/")
            return {base: data} if base else {}
        return {}

    def save_oj_sessions(self, sessions: dict[str, dict[str, Any]]) -> None:
        payload = json.dumps({"sites": sessions}, ensure_ascii=False).encode("utf-8")
        self._atomic_write(self.oj_session_path, dpapi_protect(payload))

    def delete_oj_session_for(self, base: str) -> None:
        """只清掉某一个站点的会话，其余站点保持登录。"""
        key = str(base or "").rstrip("/")
        sessions = self.load_oj_sessions()
        if key in sessions:
            sessions.pop(key, None)
            if sessions:
                self.save_oj_sessions(sessions)
            else:
                self.delete_oj_session()
        elif not key:
            self.delete_oj_session()

    def load_oj_session(self) -> Optional[dict[str, Any]]:
        for record in self.load_oj_sessions().values():
            return record
        return None

    def save_oj_session(self, record: dict[str, Any]) -> None:
        base = str(record.get("base") or "").rstrip("/")
        sessions = self.load_oj_sessions()
        sessions[base] = record
        self.save_oj_sessions(sessions)

    def delete_oj_session(self) -> None:
        try:
            self.oj_session_path.unlink(missing_ok=True)
        except Exception as exc:
            LOG.warning("删除 OJ 会话失败：%s", exc)

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

    def completed_records(self) -> list[dict[str, Any]]:
        """本机「已完成」记录，按完成时间倒序。"""
        state = self.load_state()
        completed = state.get("completed", {})
        rows: list[dict[str, Any]] = []
        for homework_id, info in completed.items():
            payload = info if isinstance(info, dict) else {}
            rows.append(
                {
                    "id": str(homework_id),
                    "title": str(payload.get("title") or ""),
                    "completedAt": str(payload.get("completedAt") or ""),
                }
            )
        rows.sort(key=lambda row: row["completedAt"], reverse=True)
        return rows

    def unmark_completed(self, homework_id: str) -> bool:
        """撤销本机完成标记；返回是否真的删掉了记录。"""
        state = self.load_state()
        completed = state.get("completed", {})
        if homework_id not in completed:
            return False
        completed.pop(homework_id, None)
        self.save_state(state)
        return True

    def load_custom_themes(self) -> list[dict[str, Any]]:
        data = self._read_json(self.themes_path, {})
        items = data.get("custom") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        cleaned: list[dict[str, Any]] = []
        for raw in items:
            try:
                cleaned.append(theme_mod.normalize_theme(raw, keep_id=True))
            except theme_mod.ThemeError as exc:
                LOG.warning("跳过损坏的自定义主题：%s", exc)
        return cleaned

    def save_custom_themes(self, items: list[dict[str, Any]]) -> None:
        payload = json.dumps({"custom": items}, ensure_ascii=False, indent=2).encode("utf-8")
        self._atomic_write(self.themes_path, payload)


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


# ----------------------------------------------------------------------------
# 作业正文 / 附件（1.4.1）
# ----------------------------------------------------------------------------

# 正文来自老师编辑的富文本，只保留排版类标签，脚本、样式、表单、外链样式一律丢弃。
_DESC_ALLOWED_TAGS = {
    "p", "div", "span", "br", "hr", "b", "strong", "i", "em", "u", "s", "del",
    "sub", "sup", "ol", "ul", "li", "table", "thead", "tbody", "tfoot", "tr",
    "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "code",
    "a", "img",
}
_DESC_BLOCK_TAGS = {
    "p", "div", "br", "hr", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "table", "ul", "ol",
}
_DESC_DROP_TAGS = {
    "script", "style", "iframe", "object", "embed", "link", "meta", "head",
    "title", "form", "input", "button", "textarea", "select", "svg", "math",
    "noscript", "base", "video", "audio", "source", "canvas",
}
_SAFE_URL_PREFIXES = ("http://", "https://")
_OFFICE_EXTS = {"doc", "docx", "ppt", "pptx", "xls", "xlsx", "wps", "et", "dps", "rtf", "odt", "odp", "ods"}


class _DescriptionSanitizer(HTMLParser):
    """把上游正文 HTML 收敛成白名单标签，顺带剥掉所有行内样式和事件属性。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._stack: list[str] = []

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _clean_attr_url(value: str) -> str:
        value = (value or "").strip()
        lowered = value.lower()
        if lowered.startswith(_SAFE_URL_PREFIXES) or lowered.startswith("data:image/"):
            return value
        return ""

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        tag = tag.lower()
        if tag in _DESC_DROP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "font":
            tag = "span"
        if tag not in _DESC_ALLOWED_TAGS:
            return

        if tag == "br":
            self.parts.append("<br>")
            return
        if tag == "hr":
            self.parts.append("<hr>")
            return
        if tag == "img":
            src = ""
            alt = ""
            for key, value in attrs:
                key = (key or "").lower()
                if key == "src":
                    src = self._clean_attr_url(value or "")
                elif key == "alt":
                    alt = value or ""
            if src:
                self.parts.append(
                    '<img src="{}" alt="{}" loading="lazy" referrerpolicy="no-referrer">'.format(
                        escape(src, quote=True), escape(alt, quote=True)
                    )
                )
            return
        if tag == "a":
            href = ""
            for key, value in attrs:
                if (key or "").lower() == "href":
                    href = self._clean_attr_url(value or "")
            if href:
                self.parts.append(
                    '<a href="{}" target="_blank" rel="noopener noreferrer">'.format(
                        escape(href, quote=True)
                    )
                )
            else:
                self.parts.append("<span>")
            self._stack.append("a" if href else "span")
            return

        self.parts.append(f"<{tag}>")
        self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DESC_DROP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "font":
            tag = "span"
        if tag not in _DESC_ALLOWED_TAGS or tag in {"br", "hr", "img"}:
            return
        # 上游 HTML 可能不闭合；按栈回退到最近的同名标签，顺带补上中间漏掉的收尾
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index] in {tag, "span" if tag == "a" else tag}:
                while len(self._stack) > index:
                    self.parts.append(f"</{self._stack.pop()}>")
                return
        return

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        self.parts.append(escape(data, quote=False))


def sanitize_description_html(raw: str) -> str:
    """上游富文本 -> 安全 HTML（保留段落/换行/列表/表格/图片）。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    # 纯文本（没有标签）也能正常显示，顺手把换行变成 <br>
    if "<" not in text:
        return escape(text).replace("\r\n", "\n").replace("\n", "<br>")
    parser = _DescriptionSanitizer()
    try:
        parser.feed(text)
        parser.close()
        while parser._stack:
            parser.parts.append(f"</{parser._stack.pop()}>")
    except Exception:  # 上游 HTML 畸形时不至于让整个详情接口挂掉
        LOG.debug("正文 HTML 解析失败，退回纯文本")
        plain = re.sub(r"<[^>]+>", " ", text)
        return escape(unescape(plain)).replace("\n", "<br>")
    return "".join(parser.parts).strip()


def description_text(html_text: str) -> str:
    """给列表预览用的纯文本（去标签、收敛空白）。"""
    plain = re.sub(r"<br\s*/?>", "\n", html_text or "", flags=re.I)
    plain = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n", plain, flags=re.I)
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = unescape(plain).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in plain.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def normalize_attachments(raw_uploads: Any) -> list[dict[str, Any]]:
    """把活动详情里的 uploads 收敛成前端要的附件列表。"""
    if not isinstance(raw_uploads, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_uploads:
        if not isinstance(item, dict) or item.get("deleted"):
            continue
        upload_id = item.get("id")
        name = str(item.get("name") or "").strip()
        if upload_id is None or not name:
            continue
        key = str(upload_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        result.append(
            {
                "id": key,
                "name": name,
                "size": max(0, size),
                "kind": str(item.get("type") or ""),
                "downloadable": bool(item.get("origin_allow_download", item.get("allow_download", True)) is not False),
            }
        )
    return result


def desktop_dir() -> Path:
    """取当前用户的桌面目录（跟随 OneDrive 重定向；取不到就退回 USERPROFILE\\Desktop）。"""
    try:
        buf = ctypes.create_unicode_buffer(260)
        shell32 = ctypes.windll.shell32
        shell32.SHGetFolderPathW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wt.DWORD, wt.LPWSTR]
        shell32.SHGetFolderPathW.restype = ctypes.c_long
        # CSIDL_DESKTOPDIRECTORY = 0x0010
        if shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0 and buf.value:
            path = Path(buf.value)
            if path.is_dir():
                return path
    except Exception:
        LOG.debug("SHGetFolderPath 取桌面失败，改用环境变量")
    home = os.environ.get("USERPROFILE") or str(Path.home())
    fallback = Path(home) / "Desktop"
    if fallback.is_dir():
        return fallback
    return Path(home)


_FILENAME_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_FILENAME_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str, fallback: str = "作业附件") -> str:
    """把上游文件名清成 Windows 能落盘的名字（去掉路径分隔符与保留字）。"""
    text = unescape(str(name or "")).strip()
    text = _FILENAME_BAD_CHARS.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = fallback
    stem = text.rsplit(".", 1)[0] if "." in text else text
    if stem.upper() in _FILENAME_RESERVED:
        text = f"_{text}"
    if len(text) > 120:
        ext = ""
        if "." in text:
            base, ext = text.rsplit(".", 1)
            ext = "." + ext[:12]
        text = text[: max(1, 120 - len(ext))] + ext
    return text


def unique_path(folder: Path, filename: str) -> Path:
    """桌面已有同名文件时自动加 (1)(2)，绝覆盖用户已有文件。"""
    candidate = folder / filename
    if not candidate.exists():
        return candidate
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    index = 1
    while True:
        suffix = f" ({index})" + (f".{ext}" if ext else "")
        candidate = folder / f"{stem}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def guess_saved_name(original_name: str, content_type: str, data: bytes) -> tuple[str, bool]:
    """按实际拿到的文件类型修正扩展名。

    平台对 office 文档只提供转好的 PDF，其余（zip/图片/md 等）给原文件。
    返回 (文件名, 是否被转成了 PDF)。
    """
    name = safe_filename(original_name)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    is_pdf = data[:5] == b"%PDF-" or "application/pdf" in (content_type or "").lower()
    if is_pdf and ext != "pdf":
        base = name.rsplit(".", 1)[0] if ext else name
        return safe_filename(base + ".pdf"), ext in _OFFICE_EXTS
    return name, False


def _filename_from_header(header: str) -> str:
    """从 Content-Disposition 里取文件名，顺带修上游的 UTF-8 当 latin-1 的乱码。"""
    if not header:
        return ""
    match = re.search(r"filename\*\s*=\s*(?:UTF-8'')?([^;]+)", header, re.I)
    value = ""
    if match:
        value = match.group(1).strip().strip('"')
    else:
        match = re.search(r'filename\s*=\s*"?([^";]+)"?', header, re.I)
        if match:
            value = match.group(1).strip()
    if not value:
        return ""
    try:
        # 服务端把 utf-8 字节直接塞进了 header，requests 按 latin-1 解出来就是乱码
        repaired = value.encode("latin-1", "ignore").decode("utf-8", "ignore")
        if repaired and any("\u4e00" <= ch <= "\u9fff" for ch in repaired):
            return repaired
    except Exception:
        pass
    return value


@dataclass
class FormFile:
    """multipart 里的一个文件域。"""

    field: str
    filename: str
    content_type: str
    data: bytes


def parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], list[FormFile]]:
    """自己解 multipart/form-data（3.13 起标准库的 cgi 模块已经删掉了）。"""
    match = re.search(r'boundary="?([^";]+)"?', content_type or "", re.I)
    if not match:
        raise ValueError("上传请求缺少 multipart 边界。")
    delimiter = b"--" + match.group(1).encode("utf-8")
    fields: dict[str, str] = {}
    files: list[FormFile] = []
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):
            break  # 结束标记
        part = chunk
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers: dict[str, str] = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name_match = re.search(r'name="([^"]*)"', disposition)
        file_match = re.search(r'filename="([^"]*)"', disposition)
        field = name_match.group(1) if name_match else ""
        if file_match is not None:
            files.append(
                FormFile(
                    field=field,
                    filename=file_match.group(1),
                    content_type=headers.get("content-type", ""),
                    data=data,
                )
            )
        elif field:
            fields[field] = data.decode("utf-8", "replace")
    return fields, files


# 一次提交最多带多少个文件、总共多少字节：本地服务要把它们读进内存再打包/上传。
MAX_SUBMIT_FILES = 40
MAX_SUBMIT_BYTES = 300 * 1024 * 1024


def build_zip_archive(items: list[tuple[str, bytes]], note: str = "") -> bytes:
    """把若干 (相对路径, 内容) 打成一个 zip；同名冲突自动改名。"""
    buffer = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for raw_name, data in items:
            name = safe_filename(raw_name.replace("\\", "/").split("/")[-1] or "file")
            parent = raw_name.replace("\\", "/").rsplit("/", 1)[0] if "/" in raw_name else ""
            base, ext = (name.rsplit(".", 1) + [""])[:2] if "." in name else (name, "")
            candidate = f"{parent}/{name}" if parent else name
            index = 1
            while candidate.lower() in used:
                tail = f" ({index})"
                stem = f"{base}{tail}.{ext}" if ext else f"{base}{tail}"
                candidate = f"{parent}/{stem}" if parent else stem
                index += 1
            used.add(candidate.lower())
            archive.writestr(candidate, data)
        if note:
            archive.writestr("提交说明.txt", note.encode("utf-8"))
    return buffer.getvalue()


def zip_display_name(raw: str, fallback: str) -> str:
    """用户填的压缩包名 → 安全文件名（去非法字符、限长、补 .zip）。"""
    name = str(raw or "").strip()
    if not name:
        name = fallback or "作业附件"
    name = name.replace("\\", "/").split("/")[-1]
    if name.lower().endswith(".zip"):
        name = name[:-4]
    name = safe_filename(name, "作业附件").strip(" .")
    if not name:
        name = "作业附件"
    if len(name) > 60:
        name = name[:60].rstrip(" .") or "作业附件"
    return f"{name}.zip"


def normalize_submission_payload(raw: Any) -> Optional[dict[str, Any]]:
    """把上游的提交对象收敛成前端好用的样子。"""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    uploads = []
    for item in raw.get("uploads") or []:
        if not isinstance(item, dict):
            continue
        uploads.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or "附件"),
                "size": int(item.get("size") or 0),
            }
        )
    return {
        "id": str(raw.get("id")),
        "isDraft": bool(raw.get("is_draft")),
        "comment": str(raw.get("comment") or ""),
        "submittedAt": str(raw.get("submitted_at") or ""),
        "createdAt": str(raw.get("created_at") or ""),
        "uploads": uploads,
    }


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
        self._student_id = 0

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
    def restore_completion(self, homework_id: str) -> bool:
        """撤销本机完成标记，并让上次被过滤掉的那份待办缓存失效。"""
        with self.lock:
            removed = self.store.unmark_completed(homework_id)
            if removed:
                self._todos_cache = None
            return removed

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

    # ------------------------------------------------------------------
    # 作业详情 / 附件（1.4.1）
    # ------------------------------------------------------------------
    _API_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    }

    def _api_json(
        self,
        method: str,
        url: str,
        payload: Any = None,
        timeout: int = 30,
        allow_404: bool = False,
    ) -> Any:
        response = self.session.request(
            method,
            url,
            json=payload,
            timeout=timeout,
            allow_redirects=False,
            headers={**self._API_HEADERS, "Referer": f"{LMS_BASE}/user/index"},
        )
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "")
            if "login" in location or "authserver" in location or "identity" in location:
                raise AuthRequired("登录会话已失效，请重新登录。")
            raise UpstreamError("深大优课返回了意外的重定向。")
        if response.status_code in (401, 403):
            raise AuthRequired("登录会话已失效，请重新登录。")
        if response.status_code == 404:
            if allow_404:
                return None
            raise UpstreamError("深大优课找不到这条作业，可能已被老师删除。")
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                detail = str(body.get("message") or "")
                errors = body.get("errors")
                if isinstance(errors, dict):
                    first = next(iter(errors.values()), "")
                    if isinstance(first, list):
                        first = first[0] if first else ""
                    if first:
                        detail = f"{detail}：{first}" if detail else str(first)
            raise UpstreamError(
                detail or f"深大优课接口请求失败（HTTP {response.status_code}）。"
            )
        content_type = response.headers.get("Content-Type", "")
        if "json" not in content_type.lower():
            raise AuthRequired("深大优课返回了登录页面，登录会话已失效。")
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError("深大优课返回的数据不是有效 JSON。") from exc

    def _api_get_json(self, url: str) -> Any:
        return self._api_json("GET", url)

    # ------------------------------------------------------------------
    # 交作业（1.5.0）
    # ------------------------------------------------------------------
    def student_id(self) -> int:
        """当前账号的 user id：提交接口的路径里必须带它。"""
        with self.lock:
            if self._student_id:
                return self._student_id
        data = self._api_json("GET", PROFILE_URL)
        try:
            value = int((data or {}).get("id") or 0)
        except (TypeError, ValueError):
            value = 0
        if not value:
            raise UpstreamError("读不到当前账号的信息，请重新登录后再试。")
        with self.lock:
            self._student_id = value
        return value

    def submission_state(self, activity_id: str) -> dict[str, Any]:
        """我在这条作业上的提交情况：最新一版 + 是否存过草稿。"""
        try:
            sid = self.student_id()
        except AuthRequired:
            raise
        with self.lock:
            data = self._api_json(
                "GET",
                SUBMISSION_LIST_URL.format(activity_id=activity_id, student_id=sid),
                allow_404=True,
            )
        items = []
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            items = [item for item in data["list"] if isinstance(item, dict)]
        latest = None
        for item in items:
            if item.get("is_latest_version") and not item.get("is_draft"):
                latest = item
                break
        if latest is None:
            for item in items:
                if not item.get("is_draft"):
                    latest = item
        draft = None
        for item in items:
            if item.get("is_draft"):
                draft = item
        return {
            "studentId": sid,
            "submitted": normalize_submission_payload(latest),
            "draft": normalize_submission_payload(draft),
            "canRetract": bool(latest and latest.get("marked_submitted") is not False),
        }

    def upload_submission_file(
        self,
        activity_id: str,
        filename: str,
        data: bytes,
        content_type: str = "",
    ) -> dict[str, Any]:
        """把一个文件传到优课的存储里，返回 {id, name, size}。

        流程照抄网页：POST /api/uploads 建记录 → PUT 到返回的 upload_url →
        S3 还要再打一次 callback。三条通道里只有 QINIU 没实现（本站不是）。
        """
        size = len(data)
        if not size:
            raise UpstreamError(f"「{filename}」是空文件，没有可上传的内容。")
        try:
            parent_id = int(activity_id)
        except (TypeError, ValueError):
            parent_id = 0

        info: Any = None
        last_error: Optional[UpstreamError] = None
        for parent_type in UPLOAD_PARENT_TYPES:
            meta = {
                "name": filename,
                "size": size,
                "parent_type": parent_type,
                "parent_id": parent_id,
                "is_scorm": False,
                "is_wmpkg": False,
            }
            try:
                with self.lock:
                    info = self._api_json("POST", UPLOAD_CREATE_URL, meta)
                break
            except UpstreamError as exc:
                last_error = exc
                LOG.info("建上传记录失败（parent_type=%s）：%s", parent_type, exc)
                if "parent" not in str(exc).lower():
                    raise
        if isinstance(info, dict) and not info.get("id") and isinstance(info.get("data"), dict):
            info = info["data"]
        if not isinstance(info, dict) or not info.get("id"):
            raise last_error or UpstreamError("深大优课没有返回上传凭证。")

        upload_id = str(info.get("id"))
        upload_url = str(info.get("upload_url") or info.get("uploadUrl") or "")
        storage = str(info.get("storage_type") or "").upper()
        transcoder = str(info.get("transcoder") or "").upper()
        if not upload_url:
            raise UpstreamError("深大优课没有给出上传地址。")
        if storage == "QINIU":
            raise UpstreamError("这条作业的附件存在七牛云上，本应用暂时不支持，请到网页提交。")
        target = upload_url if upload_url.startswith(("http://", "https://")) else LMS_BASE + upload_url

        if transcoder == "WRPC":
            response = self.session.put(
                target,
                data=data,
                timeout=600,
                headers={"Content-Type": content_type or "application/octet-stream"},
            )
        else:
            response = self.session.put(
                target,
                files={"file": (filename, data, content_type or "application/octet-stream")},
                timeout=600,
            )
        if response.status_code >= 400:
            raise UpstreamError(
                f"上传「{filename}」失败（HTTP {response.status_code}），"
                "如果是文件类型被拦，可以勾选打包成 ZIP 再提交。"
            )
        if storage == "S3":
            file_key = info.get("file_key") or upload_id
            with self.lock:
                self._api_json(
                    "POST",
                    UPLOAD_CALLBACK_URL.format(upload_id=upload_id),
                    {"file_key": file_key},
                )
        LOG.info("已上传附件：%s（%d 字节，upload=%s，storage=%s）", filename, size, upload_id, storage)
        return {"id": upload_id, "name": filename, "size": size}

    def create_submission(
        self,
        activity_id: str,
        comment: str,
        upload_ids: list[str],
        draft: bool = False,
        draft_id: str = "",
    ) -> dict[str, Any]:
        """创建提交（draft=True 就是存草稿）。已有草稿时走 PUT 复用那一条。"""
        payload: dict[str, Any] = {
            "comment": comment,
            "uploads": upload_ids,
            "slides": [],
            "is_draft": bool(draft),
            "mode": "normal",
            "other_resources": [],
            "uploads_in_rich_text": [],
        }
        method = "POST"
        if draft_id:
            method = "PUT"
            payload["submission_id"] = draft_id
        url = SUBMISSION_CREATE_URL.format(activity_id=activity_id)
        with self.lock:
            data = self._api_json(method, url, payload, timeout=180)
        submission = None
        if isinstance(data, dict):
            submission = data.get("submission") if isinstance(data.get("submission"), dict) else None
            if submission is None and data.get("id"):
                submission = data
        LOG.info(
            "提交作业 %s：%s，附件 %d 个，草稿=%s",
            activity_id,
            "成功" if submission else "返回内容为空",
            len(upload_ids),
            draft,
        )
        return submission or {}

    def retract_submission(self, submission_id: str) -> Any:
        url = SUBMISSION_RETRACT_URL.format(submission_id=submission_id)
        with self.lock:
            return self._api_json("PUT", url, {}, timeout=60)

    def get_activity_detail(self, activity_id: str) -> dict[str, Any]:
        with self.lock:
            data = self._api_get_json(ACTIVITY_URL.format(activity_id=activity_id))
        if not isinstance(data, dict):
            raise UpstreamError("作业详情格式不正确。")
        return data

    def _upload_direct_url(self, upload_id: str, template: str) -> str:
        """用一个接口换 media 临时直链；接口不可用时返回空串，交给调用方兜底。"""
        try:
            info = self._api_get_json(template.format(upload_id=upload_id))
        except AuthRequired:
            raise
        except (UpstreamError, requests.RequestException) as exc:
            LOG.info("换附件直链失败（%s）：%s", template, exc)
            return ""
        if not isinstance(info, dict):
            return ""
        url = str(info.get("url") or "")
        if url and not url.lower().startswith(("http://", "https://")):
            return ""
        return url

    def download_upload(self, upload_id: str) -> tuple[bytes, str, str]:
        """下载一个附件，返回 (内容, Content-Type, Content-Disposition)。

        先取**原文件**（/api/uploads/{id}/url），文件名、格式、字节数都和老师上传的一致；
        只有原文件接口拿不到时才退回平台转码的 PDF（/api/uploads/document/{id}/url）。
        """
        with self.lock:
            url = self._upload_direct_url(upload_id, UPLOAD_ORIGINAL_URL_API)
            if not url:
                url = self._upload_direct_url(upload_id, UPLOAD_URL_API)
            if not url:
                raise UpstreamError("深大优课没有返回附件下载地址。")

            response = self.session.get(
                url,
                timeout=300,
                stream=True,
                headers={"Referer": f"{LMS_BASE}/"},
            )
            if response.status_code != 200:
                raise UpstreamError(f"附件下载失败（HTTP {response.status_code}）。")
            limit = 300 * 1024 * 1024
            total = 0
            chunks: list[bytes] = []
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    raise UpstreamError("附件超过 300 MB，暂不支持下载。")
                chunks.append(chunk)
            return (
                b"".join(chunks),
                response.headers.get("Content-Type", ""),
                response.headers.get("Content-Disposition", ""),
            )

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
        self.oj = OjHub(self.store)
        self.started_at = now_iso()

        self._theme_lock = threading.Lock()
        self._detail_lock = threading.Lock()
        self._detail_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._custom_themes: list[dict[str, Any]] = self.store.load_custom_themes()
        prefs = self.store.load_prefs()
        stored_id = str(prefs.get("theme") or "")
        if stored_id in {theme["id"] for theme in self.all_themes()}:
            self._active_theme_id = stored_id
        else:
            # 主题被删掉、或 themes.json 里根本没有这个 id：退回默认主题并顺手纠正
            # prefs，否则界面上会「一个都没选中」，悬浮球配色也和主界面对不上。
            self._active_theme_id = theme_mod.DEFAULT_THEME_ID
            if stored_id:
                LOG.warning("主题 %s 已不存在，退回默认主题。", stored_id)
                prefs["theme"] = self._active_theme_id
                self.store.save_prefs(prefs)
        # 桌面壳在启动时挂上来，用来让悬浮球跟着换色
        self.on_theme_changed: Any = None

    def completed_records(self) -> list[dict[str, Any]]:
        """本机「已完成」清单（纯本地，不联网）。"""
        return self.store.completed_records()

    def restore_completion(self, homework_id: str) -> bool:
        """把一条被误勾完成的作业恢复回待办。"""
        return self.auth.restore_completion(homework_id)

    def homework(self, force: bool = False) -> dict[str, Any]:
        """两个来源合并成一份待办。

        两个系统各自独立登录：只登了 OJ、还没登畅课时，不把整页糊成一张登录卡，
        而是先返回 OJ 那部分，再用 ``lmsAuth`` 告诉前端「畅课还差一次登录」。
        只有两边都拿不到东西（畅课未登录且 OJ 也没配过）才把登录异常抛给路由层。
        """
        completed = self.store.completed_ids()
        assignments: list[dict[str, Any]] = []
        lms_auth: Optional[dict[str, Any]] = None
        lms_error: Optional[Exception] = None

        try:
            payload = self.auth.get_todos(force=force)
            assignments = normalize_todos(payload, completed)
        except CaptchaRequired as exc:
            lms_error = exc
            lms_auth = {
                "code": "CAPTCHA_REQUIRED",
                "message": exc.message,
                "captchaType": exc.captcha_type,
                "captchaImage": exc.image_data_url,
            }
        except AuthFailed as exc:
            lms_error = exc
            lms_auth = {"code": "AUTH_FAILED", "message": exc.message}
        except AuthRequired as exc:
            lms_error = exc
            lms_auth = {"code": "AUTH_REQUIRED", "message": exc.message}

        # OJ 是第二个作业源，而且可能同时挂着好几个站点。
        # Hub 内部已经按站点降级了，这里只是最后一道保险。
        try:
            for item in self.oj.todos(force=force):
                if item["id"] not in completed:
                    assignments.append(item)
        except Exception:  # 兜底，别把整个看板拖下水
            LOG.exception("读取 OJ 数据时出现未预期的错误")

        # 畅课失败 + OJ 也没配过：没有任何内容可展示，维持原来的 401/428，
        # 让前端弹登录框；否则降级返回 OJ 那部分。
        if lms_error is not None and not self.oj.status()["configured"]:
            raise lms_error

        assignments.sort(key=lambda item: item["dueDate"])
        return {
            "assignments": assignments,
            "updatedAt": now_iso(),
            "user": {
                "username": self.auth.username,
            },
            "source": "深大优课",
            "completionMode": "local",
            "count": len(assignments),
            "oj": self.oj.status(),
            "lmsAuth": lms_auth,
        }

    # -------------------------------------------------------------- OJ（2.0.0 / 2.1.0）
    def oj_status(self) -> dict[str, Any]:
        return self.oj.status()

    def oj_captcha(self, base: str = "") -> dict[str, Any]:
        return self.oj.new_captcha(base or None)

    def oj_login(
        self, username: str, password: str, captcha: str, captcha_key: str, base: str = ""
    ) -> dict[str, Any]:
        return self.oj.login(username, password, captcha, captcha_key, base or None)

    def oj_logout(self, base: str = "") -> None:
        self.oj.logout(base or None)

    def oj_add_site(self, base: str, label: str = "") -> dict[str, Any]:
        """添加一个 OJ 站点（每学期 OJ 会换一台机器，也可能同时挂几个）。"""
        return self.oj.add_site(base, label)

    def oj_remove_site(self, base: str) -> dict[str, Any]:
        return self.oj.remove_site(base)

    def oj_rename_site(self, base: str, label: str) -> dict[str, Any]:
        return self.oj.rename_site(base, label)

    # ------------------------------------------------------------------ 作业详情 / 附件
    DETAIL_CACHE_TTL = 600

    def homework_detail(self, activity_id: str, force: bool = False) -> dict[str, Any]:
        """取作业正文 + 附件清单。列表里没有这两样，只能按条去活动详情接口拿。"""
        key = str(activity_id)
        stamp = time.time()
        with self._detail_lock:
            hit = self._detail_cache.get(key)
            if hit and not force and stamp - hit[0] < self.DETAIL_CACHE_TTL:
                return hit[1]

        raw = self.auth.get_activity_detail(key)
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        raw_html = data.get("description") or raw.get("description") or ""
        body_html = sanitize_description_html(str(raw_html))
        detail = {
            "id": key,
            "title": str(raw.get("title") or ""),
            "courseId": raw.get("course_id"),
            "descriptionHtml": body_html,
            "descriptionText": description_text(body_html),
            "attachments": normalize_attachments(raw.get("uploads")),
            "allowRetract": bool(data.get("allow_retract") or raw.get("allow_retract")),
            "homeworkType": str(data.get("homework_type") or ""),
            "fetchedAt": now_iso(),
        }
        with self._detail_lock:
            self._detail_cache[key] = (stamp, detail)
            if len(self._detail_cache) > 80:
                for old_key, _ in sorted(self._detail_cache.items(), key=lambda kv: kv[1][0])[:40]:
                    self._detail_cache.pop(old_key, None)
        return detail

    def download_attachment(self, activity_id: str, upload_id: str) -> dict[str, Any]:
        """把一个附件存到桌面，重名自动加序号，不覆盖已有文件。"""
        detail = self.homework_detail(str(activity_id))
        attachment = next((item for item in detail["attachments"] if item["id"] == str(upload_id)), None)
        if attachment is None:
            raise UpstreamError("这条作业里找不到这个附件，可能已被老师删除。")

        data, content_type, disposition = self.auth.download_upload(str(upload_id))
        if not data:
            raise UpstreamError("附件内容为空。")
        header_name = _filename_from_header(disposition)
        base_name = attachment["name"] or header_name
        saved_name, converted = guess_saved_name(base_name, content_type, data)

        target_dir = desktop_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = unique_path(target_dir, saved_name)
        target.write_bytes(data)
        LOG.info("附件已保存：%s（%d 字节）", target, len(data))
        return {
            "ok": True,
            "name": target.name,
            "path": str(target),
            "size": len(data),
            "converted": converted,
            "originalName": attachment["name"],
            "folder": str(target_dir),
        }

    def download_attachments(self, activity_id: str) -> dict[str, Any]:
        """一条作业的全部附件一起存到桌面，单个失败不影响其他。"""
        detail = self.homework_detail(str(activity_id))
        if not detail["attachments"]:
            raise UpstreamError("这条作业没有附件。")
        saved: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for attachment in detail["attachments"]:
            try:
                saved.append(self.download_attachment(str(activity_id), attachment["id"]))
            except (UpstreamError, requests.RequestException, OSError) as exc:
                LOG.warning("附件 %s 下载失败：%s", attachment["name"], exc)
                message = exc.message if isinstance(exc, UpstreamError) else str(exc)
                failed.append({"name": attachment["name"], "message": message})
        return {
            "ok": bool(saved),
            "saved": saved,
            "failed": failed,
            "folder": str(desktop_dir()),
        }

    # ------------------------------------------------------------------ 交作业（1.5.0）
    def submission_state(self, activity_id: str) -> dict[str, Any]:
        return self.auth.submission_state(str(activity_id))

    def submit_homework(
        self,
        activity_id: str,
        comment: str,
        files: list[FormFile],
        draft: bool = False,
        zip_enabled: bool = False,
        zip_name: str = "",
    ) -> dict[str, Any]:
        """把选中的文件（或打好的 ZIP）提交到深大优课。

        draft=True 只存草稿，老师那边看不到；zip_enabled=True 时先把所有文件
        打成一个压缩包再传，用来绕开平台对部分后缀的拦截，也方便交整个文件夹。
        """
        key = str(activity_id)
        files = [item for item in files if item.filename]
        comment = str(comment or "").strip()
        if not files and not comment:
            raise UpstreamError("还没有选择文件。")
        if len(files) > MAX_SUBMIT_FILES:
            raise UpstreamError(f"一次最多提交 {MAX_SUBMIT_FILES} 个文件，可以勾选打包成 ZIP。")
        total = sum(len(item.data) for item in files)
        if total > MAX_SUBMIT_BYTES:
            raise UpstreamError(
                f"文件总大小 {total // (1024 * 1024)} MB，超过 "
                f"{MAX_SUBMIT_BYTES // (1024 * 1024)} MB 上限。"
            )

        comment = str(comment or "").strip()
        title = ""
        try:
            title = str(self.homework_detail(key).get("title") or "")
        except Exception:  # noqa: BLE001 - 拿不到标题不影响提交，只是默认包名差一点
            title = ""

        uploads: list[dict[str, Any]] = []
        for item in files:
            relative = item.filename.replace("\\", "/")
            base = safe_filename(relative.split("/")[-1], "作业附件")
            uploads.append({"relative": relative, "base": base, "data": item.data,
                            "contentType": item.content_type, "origin": item})

        targets: list[tuple[str, bytes, str]]
        if zip_enabled and uploads:
            archive_name = zip_display_name(zip_name, title or "作业附件")
            note = f"{title}\n" if title else ""
            if comment:
                note += f"\n{comment}\n"
            archive = build_zip_archive([(item["relative"], item["data"]) for item in uploads], note)
            targets = [(archive_name, archive, "application/zip")]
        else:
            targets = [(item["base"], item["data"], item["contentType"]) for item in uploads]

        state = self.auth.submission_state(key)
        draft_id = ""
        if isinstance(state.get("draft"), dict):
            draft_id = str(state["draft"].get("id") or "")

        upload_ids: list[str] = []
        try:
            for name, data, content_type in targets:
                result = self.auth.upload_submission_file(key, name, data, content_type)
                upload_ids.append(str(result["id"]))
            raw = self.auth.create_submission(key, comment, upload_ids, draft=draft, draft_id=draft_id)
        except UpstreamError as exc:
            raise UpstreamError(exc.message) from exc

        with self._detail_lock:
            self._detail_cache.pop(key, None)
        LOG.info(
            "作业 %s 已%s，文件 %d 个（%s）",
            key,
            "存为草稿" if draft else "提交",
            len(upload_ids),
            ", ".join(name for name, _, _ in targets),
        )
        normalized = normalize_submission_payload(raw)
        return {
            "ok": True,
            "draft": bool(draft),
            "submission": normalized,
            "uploaded": [{"name": name, "size": len(data)} for name, data, _ in targets],
            "state": self.auth.submission_state(key),
            "message": (
                f"已存为草稿（{len(upload_ids)} 个文件），老师那边还看不到。"
                if draft
                else f"已提交 {len(upload_ids)} 个文件到深大优课。"
            ),
        }

    def retract_submission(self, activity_id: str, submission_id: str) -> dict[str, Any]:
        key, sid = str(activity_id), str(submission_id)
        if not sid:
            raise UpstreamError("没有可撤回的提交。")
        self.auth.retract_submission(sid)
        with self._detail_lock:
            self._detail_cache.pop(key, None)
        LOG.info("作业 %s 已撤回提交 %s", key, sid)
        return {
            "ok": True,
            "state": self.auth.submission_state(key),
            "message": "已撤回这次提交，可以重新交一份。",
        }

    # ------------------------------------------------------------------ 主题
    def all_themes(self) -> list[dict[str, Any]]:
        return [dict(theme) for theme in theme_mod.BUILTIN_THEMES] + [
            dict(theme) for theme in self._custom_themes
        ]

    def active_theme(self) -> dict[str, Any]:
        for theme in self.all_themes():
            if theme["id"] == self._active_theme_id:
                return theme
        fallback = theme_mod.builtin_theme(theme_mod.DEFAULT_THEME_ID)
        assert fallback is not None
        return dict(fallback)

    @staticmethod
    def _summary(theme: dict[str, Any]) -> dict[str, Any]:
        v = theme["vars"]
        return {
            "id": theme["id"],
            "name": theme["name"],
            "description": theme.get("description", ""),
            "author": theme.get("author", ""),
            "scheme": theme.get("scheme", "light"),
            "glass": theme.get("glass", "dark"),
            "builtin": bool(theme.get("builtin")),
            "swatch": {
                "bg": v["--bg"],
                "surface": v["--surface"],
                "accent": v["--green"],
                "danger": v["--red"],
                "warn": v["--orange"],
                "ink": v["--ink"],
            },
        }

    def themes_payload(self) -> dict[str, Any]:
        active = self.active_theme()
        return {
            "active": active["id"],
            "scheme": active.get("scheme", "light"),
            "version": theme_mod.SCHEMA_VERSION,
            "vars": active["vars"],
            "themes": [self._summary(theme) for theme in self.all_themes()],
        }

    def theme_by_id(self, theme_id: str) -> dict[str, Any] | None:
        for theme in self.all_themes():
            if theme["id"] == theme_id:
                return theme
        return None

    def _notify_theme(self, theme: dict[str, Any]) -> None:
        callback = self.on_theme_changed
        if callback is None:
            return
        try:
            callback(theme)
        except Exception:
            LOG.exception("应用主题到悬浮球失败")

    def set_active_theme(self, theme_id: str) -> dict[str, Any]:
        theme = self.theme_by_id(theme_id)
        if theme is None:
            raise theme_mod.ThemeError("找不到这个主题。")
        with self._theme_lock:
            self._active_theme_id = theme["id"]
            prefs = self.store.load_prefs()
            prefs["theme"] = theme["id"]
            self.store.save_prefs(prefs)
        self._notify_theme(theme)
        return theme

    def import_theme(self, raw: dict[str, Any]) -> dict[str, Any]:
        theme = theme_mod.normalize_theme(raw)
        with self._theme_lock:
            # 同名同作者视为「覆盖更新」，避免导入几次堆一堆重复项
            replaced = False
            for index, existing in enumerate(self._custom_themes):
                if existing["name"] == theme["name"] and existing.get("author") == theme.get("author"):
                    theme["id"] = existing["id"]
                    self._custom_themes[index] = theme
                    replaced = True
                    break
            if not replaced:
                if len(self._custom_themes) >= 24:
                    raise theme_mod.ThemeError("自定义主题最多 24 个，先删掉一些再导入。")
                self._custom_themes.append(theme)
            self.store.save_custom_themes(self._custom_themes)
            self._active_theme_id = theme["id"]
            prefs = self.store.load_prefs()
            prefs["theme"] = theme["id"]
            self.store.save_prefs(prefs)
        self._notify_theme(theme)
        return theme

    def delete_theme(self, theme_id: str) -> None:
        if theme_id in theme_mod.BUILTIN_IDS:
            raise theme_mod.ThemeError("内置主题不能删除。")
        with self._theme_lock:
            remaining = [t for t in self._custom_themes if t["id"] != theme_id]
            if len(remaining) == len(self._custom_themes):
                raise theme_mod.ThemeError("找不到这个自定义主题。")
            self._custom_themes = remaining
            self.store.save_custom_themes(remaining)
            if self._active_theme_id == theme_id:
                self._active_theme_id = theme_mod.DEFAULT_THEME_ID
                prefs = self.store.load_prefs()
                prefs["theme"] = theme_mod.DEFAULT_THEME_ID
                self.store.save_prefs(prefs)
        self._notify_theme(self.active_theme())

    def theme_style_block(self) -> str:
        """把当前主题拼成一段 <style>，在服务端注入页面，避免首屏闪一下默认色。"""
        theme = self.active_theme()
        vars_css = ";".join(f"{key}:{value}" for key, value in theme["vars"].items())
        scheme = theme.get("scheme", "light")
        return (
            f'<style id="theme-vars">:root{{{vars_css}}}</style>'
            f'<script>document.documentElement.dataset.scheme={json.dumps(scheme)};</script>'
        )


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

    def _discard_body(self) -> None:
        """下载类 POST 不关心请求体，但必须把 body 读掉，否则残留字节会污染同一条 keep-alive 连接上的下一个请求。"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if 0 < length <= 1024 * 1024:
            try:
                self.rfile.read(length)
            except Exception:
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
                body = page_path.read_bytes()
                if page_path == APP_HTML:
                    # 把当前主题直接写进首屏，避免页面先闪一下默认配色
                    markup = body.decode("utf-8")
                    markup = markup.replace("<!--THEME_STYLE-->", self.context.theme_style_block())
                    body = markup.encode("utf-8")
                self._send_html(body)
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
            payload["oj"] = self.context.oj_status()
            self._send_json(200, payload)
            return

        if path == "/api/oj/status":
            self._send_json(200, {"ok": True, "oj": self.context.oj_status()})
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

        detail_match = re.fullmatch(r"/api/homework/([0-9A-Za-z_-]{1,64})/detail", path)
        if detail_match:
            query = parse_qs(parsed.query)
            force = query.get("refresh", ["0"])[0] in {"1", "true", "yes"}
            try:
                detail = self.context.homework_detail(detail_match.group(1), force=force)
                self._send_json(200, detail)
            except AuthRequired as exc:
                self._send_json(401, {"code": "AUTH_REQUIRED", "message": exc.message})
            except UpstreamError as exc:
                self._send_json(502, {"code": "UPSTREAM_ERROR", "message": exc.message})
            except requests.RequestException as exc:
                LOG.warning("读取作业详情失败：%s", exc)
                self._send_json(502, {"code": "UPSTREAM_ERROR", "message": "无法连接深大优课，请检查网络后重试。"})
            except Exception as exc:
                LOG.exception("读取作业详情失败")
                self._send_json(500, {"code": "SERVER_ERROR", "message": f"读取作业内容失败：{exc}"})
            return

        submission_match = re.fullmatch(r"/api/homework/([0-9A-Za-z_-]{1,64})/submission", path)
        if submission_match:
            try:
                state = self.context.submission_state(submission_match.group(1))
                self._send_json(200, state)
            except AuthRequired as exc:
                self._send_json(401, {"code": "AUTH_REQUIRED", "message": exc.message})
            except UpstreamError as exc:
                self._send_json(502, {"code": "UPSTREAM_ERROR", "message": exc.message})
            except requests.RequestException as exc:
                LOG.warning("读取提交状态失败：%s", exc)
                self._send_json(502, {"code": "UPSTREAM_ERROR", "message": "无法连接深大优课，请检查网络后重试。"})
            except Exception as exc:
                LOG.exception("读取提交状态失败")
                self._send_json(500, {"code": "SERVER_ERROR", "message": f"读取提交状态失败：{exc}"})
            return

        if path == "/api/completed":
            self._send_json(
                200,
                {
                    "items": self.context.completed_records(),
                    "completionMode": "local",
                },
            )
            return

        if path == "/api/reminders":
            # 2.2.0 自动提醒：设置 + 已读列表（前端拿已读给卡片盖灰标签）。
            engine = getattr(self.context, "reminders", None)
            if engine is None:
                # 纯网页模式（没跑桌面壳）：没有引擎，返回禁用态。
                self._send_json(
                    200,
                    {
                        "available": False,
                        "enabled": False,
                        "gameAlertEnabled": False,
                        "autostart": False,
                        "games": [],
                        "gameIgnored": [],
                        "read": {},
                    },
                )
                return
            self._send_json(200, {"available": True, **engine.status()})
            return

        if path == "/api/themes":
            self._send_json(200, self.context.themes_payload())
            return

        match = re.fullmatch(r"/api/themes/([A-Za-z0-9._-]{1,64})", path)
        if match:
            theme = self.context.theme_by_id(match.group(1))
            if theme is None:
                self._send_json(404, {"code": "NOT_FOUND", "message": "找不到这个主题。"})
                return
            self._send_json(200, {"theme": theme})
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

        # ------------------------------------------------------------ OJ（2.0.0）
        # 添加一个 OJ 站点（2.1.0：支持同时挂多个）。/api/oj/base 是旧名字，保留兼容。
        if path in ("/api/oj/sites/add", "/api/oj/base"):
            try:
                data = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"code": "BAD_REQUEST", "message": str(exc)})
                return

            try:
                result = self.context.oj_add_site(
                    str(data.get("base") or ""), str(data.get("label") or "")
                )
            except OjUnreachable as exc:
                self._send_json(
                    502, {"code": exc.code, "message": exc.message, "oj": self.context.oj_status()}
                )
                return
            except OjError as exc:
                self._send_json(
                    400, {"code": exc.code, "message": exc.message, "oj": self.context.oj_status()}
                )
                return
            except Exception as exc:
                LOG.exception("添加 OJ 站点失败")
                self._send_json(
                    500, {"code": "SERVER_ERROR", "message": f"添加 OJ 站点失败：{exc}"}
                )
                return

            self._send_json(
                200,
                {
                    "ok": True,
                    "base": result["base"],
                    "added": result["added"],
                    "changed": result["changed"],
                    "captcha": result["captcha"],
                    "oj": self.context.oj_status(),
                },
            )
            return

        if path == "/api/oj/sites/remove":
            try:
                data = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"code": "BAD_REQUEST", "message": str(exc)})
                return
            try:
                result = self.context.oj_remove_site(str(data.get("base") or ""))
            except OjError as exc:
                self._send_json(
                    400, {"code": exc.code, "message": exc.message, "oj": self.context.oj_status()}
                )
                return
            except Exception as exc:
                LOG.exception("移除 OJ 站点失败")
                self._send_json(
                    500, {"code": "SERVER_ERROR", "message": f"移除 OJ 站点失败：{exc}"}
                )
                return
            self._send_json(
                200,
                {"ok": True, "base": result["base"], "oj": self.context.oj_status()},
            )
            return

        if path == "/api/oj/sites/rename":
            try:
                data = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"code": "BAD_REQUEST", "message": str(exc)})
                return
            try:
                result = self.context.oj_rename_site(
                    str(data.get("base") or ""), str(data.get("label") or "")
                )
            except OjError as exc:
                self._send_json(
                    400, {"code": exc.code, "message": exc.message, "oj": self.context.oj_status()}
                )
                return
            except Exception as exc:
                LOG.exception("重命名 OJ 站点失败")
                self._send_json(
                    500, {"code": "SERVER_ERROR", "message": f"重命名 OJ 站点失败：{exc}"}
                )
                return
            self._send_json(
                200, {"ok": True, "base": result["base"], "oj": self.context.oj_status()}
            )
            return

        if path == "/api/oj/captcha":
            try:
                data = self._read_json_body()
            except ValueError:
                data = {}
            try:
                self._send_json(
                    200,
                    {"ok": True, "captcha": self.context.oj_captcha(str(data.get("base") or ""))},
                )
            except OjUnreachable as exc:
                self._send_json(
                    502, {"code": exc.code, "message": exc.message, "oj": self.context.oj_status()}
                )
            except OjError as exc:
                self._send_json(
                    500, {"code": exc.code, "message": exc.message, "oj": self.context.oj_status()}
                )
            except Exception as exc:
                LOG.exception("获取 OJ 验证码失败")
                self._send_json(500, {"code": "SERVER_ERROR", "message": f"获取验证码失败：{exc}"})
            return

        if path == "/api/oj/login":
            try:
                data = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"code": "BAD_REQUEST", "message": str(exc)})
                return

            username = str(data.get("username") or "").strip()
            password = str(data.get("password") or "")
            captcha = str(data.get("captcha") or "").strip()
            captcha_key = str(data.get("captchaKey") or "").strip()
            site_base = str(data.get("base") or "").strip()

            if not username or not password or not captcha or not captcha_key:
                self._send_json(
                    400, {"code": "VALIDATION_ERROR", "message": "学号、密码、验证码都要填。"}
                )
                return
            if len(username) > 100 or len(password) > 256 or len(captcha) > 16:
                self._send_json(
                    400, {"code": "VALIDATION_ERROR", "message": "输入内容长度不正确。"}
                )
                return

            try:
                user = self.context.oj_login(
                    username, password, captcha, captcha_key, site_base
                )
            except OjCaptchaWrong as exc:
                self._send_json(
                    400, {"code": exc.code, "message": exc.message, "oj": self.context.oj_status()}
                )
                return
            except OjLoginFailed as exc:
                self._send_json(
                    401, {"code": exc.code, "message": exc.message, "oj": self.context.oj_status()}
                )
                return
            except OjUnreachable as exc:
                self._send_json(
                    502, {"code": exc.code, "message": exc.message, "oj": self.context.oj_status()}
                )
                return
            except OjError as exc:
                self._send_json(
                    500, {"code": exc.code, "message": exc.message, "oj": self.context.oj_status()}
                )
                return
            except Exception as exc:
                LOG.exception("OJ 登录异常")
                self._send_json(500, {"code": "SERVER_ERROR", "message": f"OJ 登录出错：{exc}"})
                return

            # 只数刚登录的这个站点，别把其他学期的实验也算进来
            count = 0
            try:
                target = site_base.rstrip("/")
                if not target:
                    target = str(self.context.oj_status().get("base") or "").rstrip("/")
                count = sum(
                    1
                    for item in self.context.oj.todos(force=True)
                    if str((item.get("oj") or {}).get("siteBase") or "").rstrip("/") == target
                )
            except OjError:
                pass
            self._send_json(
                200,
                {
                    "ok": True,
                    "message": "OJ 登录成功。",
                    "user": user,
                    "base": site_base,
                    "count": count,
                    "oj": self.context.oj_status(),
                },
            )
            return

        if path == "/api/oj/logout":
            try:
                data = self._read_json_body()
            except ValueError:
                data = {}
            self.context.oj_logout(str(data.get("base") or ""))
            self._send_json(200, {"ok": True, "oj": self.context.oj_status()})
            return

        if path == "/api/themes/active":
            try:
                data = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"code": "BAD_REQUEST", "message": str(exc)})
                return
            try:
                self.context.set_active_theme(str(data.get("id") or "").strip())
            except theme_mod.ThemeError as exc:
                self._send_json(400, {"code": "THEME_ERROR", "message": str(exc)})
                return
            self._send_json(200, {"ok": True, **self.context.themes_payload()})
            return

        if path == "/api/themes/import":
            try:
                data = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"code": "BAD_REQUEST", "message": str(exc)})
                return
            raw = data.get("theme")
            if raw is None and isinstance(data.get("vars"), dict):
                raw = data
            try:
                theme = self.context.import_theme(raw if isinstance(raw, dict) else {})
            except theme_mod.ThemeError as exc:
                self._send_json(400, {"code": "THEME_ERROR", "message": str(exc)})
                return
            LOG.info("已导入主题：%s（%s）", theme["name"], theme["id"])
            self._send_json(
                200,
                {
                    "ok": True,
                    "message": f"已导入并启用「{theme['name']}」。",
                    "theme": theme,
                    **self.context.themes_payload(),
                },
            )
            return

        if path == "/api/themes/delete":
            try:
                data = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"code": "BAD_REQUEST", "message": str(exc)})
                return
            try:
                self.context.delete_theme(str(data.get("id") or "").strip())
            except theme_mod.ThemeError as exc:
                self._send_json(400, {"code": "THEME_ERROR", "message": str(exc)})
                return
            self._send_json(
                200,
                {"ok": True, "message": "已删除该主题。", **self.context.themes_payload()},
            )
            return

        if path == "/api/reminders/settings":
            try:
                data = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"code": "BAD_REQUEST", "message": str(exc)})
                return
            engine = getattr(self.context, "reminders", None)
            if engine is None:
                self._send_json(503, {"code": "UNAVAILABLE", "message": "提醒功能仅在桌面版可用。"})
                return
            status = engine.set_settings(
                enabled=data.get("enabled"),
                autostart=data.get("autostart"),
                game_alert_enabled=data.get("gameAlertEnabled"),
            )
            LOG.info("更新提醒设置：%s", {k: v for k, v in status.items() if k != "read"})
            self._send_json(200, {"ok": True, **status})
            return

        if path == "/api/reminders/games":
            try:
                data = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"code": "BAD_REQUEST", "message": str(exc)})
                return
            engine = getattr(self.context, "reminders", None)
            if engine is None:
                self._send_json(503, {"code": "UNAVAILABLE", "message": "提醒功能仅在桌面版可用。"})
                return
            games = data.get("games")
            if not isinstance(games, list):
                self._send_json(400, {"code": "VALIDATION_ERROR", "message": "games 必须是数组。"})
                return
            result = engine.set_games([str(g) for g in games])
            LOG.info("更新游戏名单：%s", result.get("games"))
            self._send_json(200, {"ok": True, **result})
            return

        if path == "/api/reminders/read":
            try:
                data = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"code": "BAD_REQUEST", "message": str(exc)})
                return
            engine = getattr(self.context, "reminders", None)
            if engine is None:
                self._send_json(503, {"code": "UNAVAILABLE", "message": "提醒功能仅在桌面版可用。"})
                return
            mark_all = bool(data.get("all"))
            ids = data.get("ids")
            if not mark_all and not isinstance(ids, list):
                self._send_json(400, {"code": "VALIDATION_ERROR", "message": "需要 ids 数组或 all=true。"})
                return
            result = engine.mark_read([str(i) for i in ids] if ids else None, mark_all=mark_all)
            LOG.info("标记已阅：all=%s ids=%s", mark_all, ids)
            self._send_json(200, {"ok": True, **result})
            return

        match = re.fullmatch(r"/api/homework/([0-9A-Za-z_-]{1,64})/submit", path)
        if match:
            activity_id = match.group(1)
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type.lower():
                self._discard_body()
                self._send_json(
                    400, {"code": "BAD_REQUEST", "message": "提交请求必须是 multipart/form-data。"}
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0:
                self._send_json(400, {"code": "BAD_REQUEST", "message": "提交请求是空的。"})
                return
            if length > MAX_SUBMIT_BYTES + 8 * 1024 * 1024:
                self._discard_body()
                self._send_json(
                    413,
                    {
                        "code": "TOO_LARGE",
                        "message": f"文件总大小超过 {MAX_SUBMIT_BYTES // (1024 * 1024)} MB 上限。",
                    },
                )
                return
            try:
                body = self.rfile.read(length)
                fields, files = parse_multipart(body, content_type)
            except ValueError as exc:
                self._send_json(400, {"code": "BAD_REQUEST", "message": str(exc)})
                return
            except Exception as exc:
                LOG.exception("解析提交请求失败")
                self._send_json(400, {"code": "BAD_REQUEST", "message": f"解析提交请求失败：{exc}"})
                return

            draft = str(fields.get("draft") or "0").strip() in {"1", "true", "yes", "on"}
            zip_enabled = str(fields.get("zip") or "0").strip() in {"1", "true", "yes", "on"}
            zip_name = str(fields.get("zipName") or "")
            comment = str(fields.get("comment") or "")
            try:
                result = self.context.submit_homework(
                    activity_id,
                    comment=comment,
                    files=files,
                    draft=draft,
                    zip_enabled=zip_enabled,
                    zip_name=zip_name,
                )
                self._send_json(200, result)
            except AuthRequired as exc:
                self._send_json(401, {"code": "AUTH_REQUIRED", "message": exc.message})
            except UpstreamError as exc:
                self._send_json(502, {"code": "SUBMIT_FAILED", "message": exc.message})
            except requests.RequestException as exc:
                LOG.warning("提交时连接失败：%s", exc)
                self._send_json(
                    502,
                    {"code": "UPSTREAM_ERROR", "message": "上传过程中连接中断，请检查网络后重试。"},
                )
            except Exception as exc:
                LOG.exception("提交作业失败")
                self._send_json(500, {"code": "SERVER_ERROR", "message": f"提交失败：{exc}"})
            return

        match = re.fullmatch(r"/api/homework/([0-9A-Za-z_-]{1,64})/retract", path)
        if match:
            try:
                data = self._read_json_body()
            except ValueError as exc:
                self._send_json(400, {"code": "BAD_REQUEST", "message": str(exc)})
                return
            submission_id = str(data.get("submissionId") or "").strip()
            try:
                result = self.context.retract_submission(match.group(1), submission_id)
                self._send_json(200, result)
            except AuthRequired as exc:
                self._send_json(401, {"code": "AUTH_REQUIRED", "message": exc.message})
            except UpstreamError as exc:
                self._send_json(502, {"code": "RETRACT_FAILED", "message": exc.message})
            except requests.RequestException as exc:
                LOG.warning("撤回提交失败：%s", exc)
                self._send_json(502, {"code": "UPSTREAM_ERROR", "message": "无法连接深大优课，请检查网络后重试。"})
            except Exception as exc:
                LOG.exception("撤回提交失败")
                self._send_json(500, {"code": "SERVER_ERROR", "message": f"撤回失败：{exc}"})
            return

        match = re.fullmatch(r"/api/homework/(.+)/uncomplete", path)
        if match:
            self._discard_body()
            homework_id = unquote(match.group(1))
            if not homework_id or len(homework_id) > 200:
                self._send_json(400, {"code": "BAD_REQUEST", "message": "作业 ID 无效。"})
                return
            try:
                removed = self.context.restore_completion(homework_id)
            except Exception as exc:
                LOG.exception("恢复完成标记失败")
                self._send_json(500, {"code": "SERVER_ERROR", "message": f"恢复失败：{exc}"})
                return
            if not removed:
                self._send_json(404, {"code": "NOT_FOUND", "message": "这条作业没有本机完成记录。"})
                return
            self._send_json(
                200,
                {
                    "ok": True,
                    "id": homework_id,
                    "message": "已恢复，重新算作未完成。",
                    "completionMode": "local",
                },
            )
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

        match = re.fullmatch(r"/api/homework/([0-9A-Za-z_-]{1,64})/attachments/download", path)
        if match:
            self._discard_body()
            try:
                result = self.context.download_attachments(match.group(1))
                message = f"已保存 {len(result['saved'])} 个附件到桌面"
                if result["failed"]:
                    message += f"，{len(result['failed'])} 个失败"
                self._send_json(200, {**result, "message": message})
            except AuthRequired as exc:
                self._send_json(401, {"code": "AUTH_REQUIRED", "message": exc.message})
            except UpstreamError as exc:
                self._send_json(500, {"code": "DOWNLOAD_FAILED", "message": exc.message})
            except requests.RequestException as exc:
                LOG.warning("批量下载附件失败：%s", exc)
                self._send_json(502, {"code": "UPSTREAM_ERROR", "message": "无法连接深大优课，请检查网络后重试。"})
            except Exception as exc:
                LOG.exception("批量下载附件失败")
                self._send_json(500, {"code": "SERVER_ERROR", "message": f"保存附件失败：{exc}"})
            return

        match = re.fullmatch(
            r"/api/homework/([0-9A-Za-z_-]{1,64})/attachments/([0-9A-Za-z_-]{1,64})/download", path
        )
        if match:
            self._discard_body()
            try:
                result = self.context.download_attachment(match.group(1), match.group(2))
                self._send_json(200, result)
            except AuthRequired as exc:
                self._send_json(401, {"code": "AUTH_REQUIRED", "message": exc.message})
            except UpstreamError as exc:
                self._send_json(500, {"code": "DOWNLOAD_FAILED", "message": exc.message})
            except requests.RequestException as exc:
                LOG.warning("下载附件失败：%s", exc)
                self._send_json(502, {"code": "UPSTREAM_ERROR", "message": "无法连接深大优课，请检查网络后重试。"})
            except OSError as exc:
                LOG.warning("写入桌面失败：%s", exc)
                self._send_json(500, {"code": "IO_ERROR", "message": f"写入桌面失败：{exc}"})
            except Exception as exc:
                LOG.exception("下载附件失败")
                self._send_json(500, {"code": "SERVER_ERROR", "message": f"保存附件失败：{exc}"})
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
