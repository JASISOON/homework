"""OJ（校内 QDUOJ 判题系统）只读适配层。

职责只有三件事：
  1. 用「账号 + 密码 + 图形验证码」换取 Django 的 sessionid，加密存本机；
  2. 把实验/作业列表与提交结果，转换成主程序统一的待办结构；
  3. 任何失败都抛 OjError 子类，由主程序降级展示，绝不影响畅课数据。

不做任何写操作：不提交代码、不撤销提交、不改分数。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import requests

LOG = logging.getLogger("homeworkdesk.oj")

DEFAULT_BASE = "http://172.31.221.62"
SUBMITTED_KEEP_DAYS = 7      # 已交的实验保留几天（能看到得分）
MISSING_KEEP_DAYS = 30       # 过期未交的实验保留几天，避免陈年旧账一直挂着
CACHE_TTL = 90               # 列表缓存，秒
MAX_SITES = 6                # 最多同时挂几个 OJ 站点（一学期一个，够用了）

_CONNECT_TIMEOUT = 4
_READ_TIMEOUT = 10


class OjError(Exception):
    """OJ 相关错误的基类。"""

    code = "OJ_ERROR"

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class OjAuthRequired(OjError):
    """没有登录，或 sessionid 已经失效。"""

    code = "OJ_AUTH_REQUIRED"


class OjCaptchaWrong(OjError):
    """验证码输错了，换一张再来。"""

    code = "OJ_CAPTCHA_WRONG"


class OjLoginFailed(OjError):
    """账号密码不对，或学校侧拒绝了这次登录。"""

    code = "OJ_LOGIN_FAILED"


class OjUnreachable(OjError):
    """当前网络到不了 OJ（只有校内网 / VPN 能访问）。"""

    code = "OJ_UNREACHABLE"


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Django 有时会带 6 位微秒，fromisoformat 到 3.11 才全兼容，这里做一次兜底
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _count_problems(problem_json: Any) -> int:
    """problem_json 形如 {"0": [题, 题], "1": []}，也见过直接给 list 的版本。"""
    if isinstance(problem_json, dict):
        total = 0
        for value in problem_json.values():
            if isinstance(value, list):
                total += len(value)
        return total
    if isinstance(problem_json, list):
        return len(problem_json)
    return 0


def site_key(base: str) -> str:
    """站点短 id：由地址算出来，删了再加回来还是同一个，完成记录不会错位。"""
    digest = hashlib.sha1(str(base).rstrip("/").encode("utf-8")).hexdigest()
    return digest[:6]


def _host_of(base: str) -> str:
    return str(urlparse(str(base or "")).hostname or base or "")


def site_short(base: str) -> str:
    """卡片徽标上的短名：IPv4 取末段（172.31.221.62 → 62），域名取首段。"""
    host = _host_of(base)
    parts = host.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        return parts[-1]
    head = host.split(".")[0] if host else ""
    return (head or base)[:8]


def unique_shorts(bases: list[str]) -> dict[str, str]:
    """多个站点末段重名时（221.62 / 222.62）自动加长到两段，保证能区分。"""
    first = {base: site_short(base) for base in bases}
    counts: dict[str, int] = {}
    for value in first.values():
        counts[value] = counts.get(value, 0) + 1
    result: dict[str, str] = {}
    for base, value in first.items():
        if counts.get(value, 0) <= 1:
            result[base] = value
            continue
        parts = _host_of(base).split(".")
        result[base] = ".".join(parts[-2:]) if len(parts) >= 2 else value
    return result


class OjClient:
    """一个 OJ 站点一个实例，内部持有一个 requests.Session。"""

    def __init__(
        self,
        store: Any,
        base: Optional[str] = None,
        label: str = "",
        tone: int = 0,
        session: Optional[dict[str, Any]] = None,
    ):
        self._store = store
        self._lock = threading.RLock()
        self._session: Optional[requests.Session] = None
        self._cache: Optional[tuple[float, list[dict[str, Any]]]] = None
        self._course_cache: dict[int, dict[str, str]] = {}
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.label = str(label or "").strip()
        self.tone = int(tone or 0)
        self.key = site_key(self.base)
        if session is None:
            try:
                session = store.load_oj_sessions().get(self.base)
            except Exception:  # 存储坏了不能影响启动
                session = None
        self._saved: Optional[dict[str, Any]] = session
        self.last_error = ""
        self.last_sync = 0.0
        self.online: Optional[bool] = None

    def reload_session(self) -> None:
        """站点增删、外部改写会话文件之后，重新读一次本机凭据。"""
        try:
            self._saved = self._store.load_oj_sessions().get(self.base)
        except Exception:
            self._saved = None
        self._drop_session()

    # ------------------------------------------------------------------ 状态
    @property
    def logged_in(self) -> bool:
        return bool(self._saved and self._saved.get("sessionid"))

    @property
    def account(self) -> str:
        return str((self._saved or {}).get("username") or "")

    def status(self) -> dict[str, Any]:
        return {
            "id": self.key,
            "base": self.base,
            "label": self.label,
            "short": site_short(self.base),
            "tone": self.tone,
            "configured": self.logged_in,
            "account": self.account,
            "online": self.online,
            "lastSync": self._iso(self.last_sync),
            "error": self.last_error,
        }

    @staticmethod
    def _iso(stamp: float) -> str:
        if not stamp:
            return ""
        return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")

    # ------------------------------------------------------------------ 会话
    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Referer": self.base + "/",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        return session

    def _ensure_session(self) -> requests.Session:
        if not self.logged_in:
            raise OjAuthRequired("还没有登录 OJ。")
        if self._session is None:
            session = self._new_session()
            session.cookies.set("sessionid", str(self._saved["sessionid"]), domain="")
            self._session = session
        return self._session

    def _drop_session(self) -> None:
        self._session = None
        self._cache = None

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        session = self._ensure_session()
        url = self.base + path
        try:
            return session.request(
                method, url, timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT), **kwargs
            )
        except requests.exceptions.ConnectTimeout as exc:
            raise OjUnreachable("连接 OJ 超时，可能不在校内网（或 VPN 断了）。") from exc
        except requests.exceptions.ConnectionError as exc:
            raise OjUnreachable("连不上 OJ，请确认已连上校园网或 VPN。") from exc
        except requests.exceptions.RequestException as exc:
            raise OjUnreachable(f"访问 OJ 失败：{exc}") from exc

    @staticmethod
    def _as_json(response: requests.Response) -> Any:
        """会话失效时 Django 会返回 404 + HTML，所以先看内容再看状态码。"""
        text = response.text.lstrip()
        if not text.startswith(("{", "[")):
            raise OjAuthRequired("OJ 登录状态已失效，请重新登录。")
        try:
            return response.json()
        except ValueError as exc:
            raise OjAuthRequired("OJ 返回了无法解析的内容，请重新登录。") from exc

    def _get_json(self, path: str) -> Any:
        return self._as_json(self._request("GET", path))

    def _paged(self, path: str, limit: int = 50, cap: int = 400) -> list[dict[str, Any]]:
        """跟着 next 翻页，最多翻到 cap 条。"""
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            sep = "&" if "?" in path else "?"
            page = self._get_json(f"{path}{sep}offset={offset}&limit={limit}")
            if isinstance(page, list):
                return page
            rows.extend(page.get("results") or [])
            if not page.get("next") or offset >= cap:
                return rows
            offset += limit

    # ------------------------------------------------------------ 站点地址（2.1.0）
    @staticmethod
    def normalize_base(raw: str) -> str:
        """把用户输入的地址收拾成 scheme://host[:port]，路径和参数一律丢掉。"""
        text = str(raw or "").strip()
        if not text:
            raise OjError("请填写 OJ 地址，例如 http://172.31.221.68", "OJ_BASE_INVALID")
        if "://" not in text:
            text = "http://" + text
        parsed = urlparse(text)
        if parsed.scheme not in ("http", "https"):
            raise OjError("地址只能以 http:// 或 https:// 开头。", "OJ_BASE_INVALID")
        if not parsed.hostname:
            raise OjError("地址里没有主机名，检查一下有没有写错。", "OJ_BASE_INVALID")
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

    @staticmethod
    def probe(base: str) -> dict[str, str]:
        """用临时会话要一张验证码：能拿到就说明这个地址是同款 OJ。"""
        try:
            response = requests.post(
                base.rstrip("/") + "/api/captcha/",
                json={},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                    ),
                    "Accept": "application/json, text/plain, */*",
                },
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
        except requests.exceptions.ConnectTimeout as exc:
            raise OjUnreachable("连接这个地址超时，确认它在校园网 / VPN 内。") from exc
        except requests.exceptions.ConnectionError as exc:
            raise OjUnreachable("连不上这个地址，检查有没有打错，或者是不是不在校内网。") from exc
        except requests.exceptions.RequestException as exc:
            raise OjUnreachable(f"访问这个地址失败：{exc}") from exc

        text = response.text.lstrip()
        if not text.startswith("{"):
            raise OjError("这个地址不是同款 OJ：验证码接口没有返回数据。", "OJ_BASE_NOT_OJ")
        try:
            data = response.json()
        except ValueError as exc:
            raise OjError("这个地址不是同款 OJ：返回内容解析不了。", "OJ_BASE_NOT_OJ") from exc
        key = str(data.get("captcha_key") or "")
        image = str(data.get("captcha_image") or "")
        if not key or not image:
            raise OjError("这个地址不是同款 OJ：没有返回验证码。", "OJ_BASE_NOT_OJ")
        return {"key": key, "image": "data:image/png;base64," + image}

    # ------------------------------------------------------------------ 登录
    def new_captcha(self) -> dict[str, str]:
        """取一张验证码。用临时会话，避免污染当前登录态。"""
        session = self._new_session()
        try:
            response = session.post(
                self.base + "/api/captcha/",
                json={},
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
        except requests.exceptions.ConnectTimeout as exc:
            raise OjUnreachable("连接 OJ 超时，可能不在校内网（或 VPN 断了）。") from exc
        except requests.exceptions.ConnectionError as exc:
            raise OjUnreachable("连不上 OJ，请确认已连上校园网或 VPN。") from exc
        except requests.exceptions.RequestException as exc:
            raise OjUnreachable(f"访问 OJ 失败：{exc}") from exc

        data = self._as_json(response)
        key = str(data.get("captcha_key") or "")
        image = str(data.get("captcha_image") or "")
        if not key or not image:
            raise OjError("OJ 没有返回验证码，请稍后重试。")
        return {"key": key, "image": "data:image/png;base64," + image}

    def login(
        self,
        username: str,
        password: str,
        captcha: str,
        captcha_key: str,
        base: Optional[str] = None,
    ) -> dict[str, Any]:
        with self._lock:
            if base:
                self.base = base.rstrip("/")
            session = self._new_session()
            payload = {
                "username": username,
                "password": password,
                "captcha_value": captcha,
                "captcha_key": captcha_key,
            }
            try:
                response = session.post(
                    self.base + "/api/user/login/",
                    json=payload,
                    timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                )
            except requests.exceptions.ConnectTimeout as exc:
                raise OjUnreachable("连接 OJ 超时，可能不在校内网（或 VPN 断了）。") from exc
            except requests.exceptions.ConnectionError as exc:
                raise OjUnreachable("连不上 OJ，请确认已连上校园网或 VPN。") from exc
            except requests.exceptions.RequestException as exc:
                raise OjUnreachable(f"访问 OJ 失败：{exc}") from exc

            text = response.text.lstrip()
            if not text.startswith("{"):
                raise OjError("OJ 登录接口返回了异常内容，请稍后重试。")

            try:
                data = response.json()
            except ValueError as exc:
                raise OjError("OJ 登录接口返回了异常内容，请稍后重试。") from exc

            if response.status_code >= 400:
                detail = data.get("non_field_errors") or data.get("detail") or []
                if isinstance(detail, list):
                    detail = " ".join(str(item) for item in detail)
                detail = str(detail or "")
                if "captcha" in detail.lower():
                    raise OjCaptchaWrong("验证码不对，换一张再试。")
                if "password" in detail.lower() or "username" in detail.lower():
                    raise OjLoginFailed("学号或密码不正确。")
                raise OjLoginFailed(detail or "OJ 拒绝了这次登录，请稍后重试。")

            sessionid = session.cookies.get("sessionid")
            if not sessionid:
                raise OjLoginFailed("OJ 没有下发登录凭据，请稍后重试。")

            record = {
                "sessionid": sessionid,
                "username": str(data.get("username") or username),
                "name": str(data.get("name") or ""),
                "base": self.base,
            }
            self._store.save_oj_session(record)
            self._saved = record
            self._session = session
            self._cache = None
            self._course_cache.clear()
            self.last_error = ""
            LOG.info("OJ 登录成功（用户 %s）。", record["username"])
            return {"username": record["username"], "name": record["name"]}

    def logout(self) -> None:
        with self._lock:
            try:
                if self.logged_in:
                    self._request("POST", "/api/user/logout/", json={})
            except OjError:
                pass
            if hasattr(self._store, "delete_oj_session_for"):
                self._store.delete_oj_session_for(self.base)
            else:
                self._store.delete_oj_session()
            self._saved = None
            self._drop_session()
            self._course_cache.clear()
            self.online = None
            self.last_error = ""

    # ------------------------------------------------------------------ 取数
    def _course_of_group(self, group_id: Any) -> dict[str, str]:
        """组（授课班）→ 课程名。同一门课只查一次。"""
        try:
            key = int(group_id)
        except (TypeError, ValueError):
            return {"course": "", "group": ""}
        if key in self._course_cache:
            return self._course_cache[key]
        info = {"course": "", "group": ""}
        try:
            data = self._get_json(f"/api/groups/{key}/")
            info["group"] = str(data.get("name") or "")
            course = data.get("course")
            if isinstance(course, dict):
                info["course"] = str(course.get("name") or "")
        except OjAuthRequired:
            raise
        except (OjError, ValueError):
            LOG.debug("读取 OJ 分组 %s 失败，课程名留空。", key)
        self._course_cache[key] = info
        return info

    def _fetch_all(self) -> list[dict[str, Any]]:
        contests = self._paged("/api/contests/")
        submissions = self._paged("/api/contest-submission/", limit=100, cap=1000)

        # 每场实验取最新的一条汇总（同分取 id 大的，代表最后一次判完的分）
        best: dict[int, dict[str, Any]] = {}
        for row in submissions:
            contest = row.get("contest")
            if not isinstance(contest, dict):
                continue  # 题库里单独交的练习题，不属于某场实验
            try:
                cid = int(contest.get("id"))
            except (TypeError, ValueError):
                continue
            current = best.get(cid)
            if current is None or int(row.get("id") or 0) >= int(current.get("id") or 0):
                best[cid] = row

        now = datetime.now(timezone.utc)
        items: list[dict[str, Any]] = []

        for contest in contests:
            try:
                cid = int(contest.get("id"))
            except (TypeError, ValueError):
                continue
            if not contest.get("visible", True):
                continue

            end = _parse_time(contest.get("end_time"))
            if end is None:
                continue
            begin = _parse_time(contest.get("begin_time"))
            title = str(contest.get("title") or "未命名实验").strip()

            submission = best.get(cid)
            submitted = submission is not None

            if submitted:
                # 已交：只在结束后保留一段时间，用来回看得分
                if end + timedelta(days=SUBMITTED_KEEP_DAYS) < now:
                    continue
            elif end + timedelta(days=MISSING_KEEP_DAYS) < now:
                # 很久以前就没交的实验，不再一直挂在逾期里
                continue

            group_info = self._course_of_group(contest.get("group"))
            course_name = group_info["course"] or group_info["group"] or "数据结构"

            problem_count = 0
            if not submitted or contest.get("is_exam"):
                # 未交的实验要知道有几道题：详情接口才有 problem_json
                try:
                    detail = self._get_json(f"/api/contests/{cid}/")
                    problem_count = _count_problems(detail.get("problem_json"))
                    course = detail.get("course")
                    if isinstance(course, dict) and course.get("name"):
                        course_name = str(course["name"])
                except OjAuthRequired:
                    raise
                except OjError:
                    LOG.debug("读取 OJ 实验 %s 详情失败，题目数留空。", cid)

            full_score = contest.get("total_score") or 0
            if submitted:
                score = submission.get("total_score")
                judged = bool(submission.get("judged"))
                notes = f"判题完成 · 满分 {full_score}" if judged else f"判题中 · 满分 {full_score}"
            else:
                parts = []
                if problem_count:
                    parts.append(f"{problem_count} 道题")
                if full_score:
                    parts.append(f"满分 {full_score}")
                if end < now:
                    parts.append("已截止")
                notes = " · ".join(parts) or "OJ 未提交"

            # 标题里带「作业」的是课后作业，其余按实验处理；考试单独标出来。
            # 来源已经由卡片上的 OJ 徽标表达，这里不再重复「OJ」前缀。
            if contest.get("is_exam"):
                kind = "考试"
            elif "作业" in title:
                kind = "作业"
            else:
                kind = "实验"

            items.append(
                {
                    # 带站点 key：不同学期/不同 OJ 上，实验 id 会重复
                    "id": f"oj-{self.key}-{cid}",
                    "name": title,
                    "course": course_name,
                    # 精确到秒：到期时间常常是 23:59:59，截到分钟会让倒计时早 59 秒归零
                    "dueDate": end.isoformat(timespec="seconds"),
                    "completed": False,
                    "notes": notes,
                    "type": kind,
                    "source": "OJ",
                    "oj": {
                        "contestId": cid,
                        "groupId": contest.get("group"),
                        "groupName": group_info["group"],
                        "courseName": course_name,
                        "submitted": submitted,
                        "judged": bool(submission.get("judged")) if submitted else False,
                        "score": submission.get("total_score") if submitted else None,
                        "totalScore": full_score,
                        "problemCount": problem_count,
                        "beginTime": begin.isoformat(timespec="minutes") if begin else "",
                        "url": f"{self.base}/#/contest/{cid}",
                        "siteId": self.key,
                        "siteBase": self.base,
                        "siteLabel": self.label,
                        "siteShort": site_short(self.base),
                        "siteTone": self.tone,
                    },
                }
            )

        items.sort(key=lambda item: item["dueDate"])
        return items

    def todos(self, force: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            if not self.logged_in:
                raise OjAuthRequired("还没有登录 OJ。")
            if not force and self._cache and time.time() - self._cache[0] < CACHE_TTL:
                return self._cache[1]

            try:
                items = self._fetch_all()
            except OjAuthRequired:
                # 会话没了：不清本机凭据（网络抖动、服务端返错页都会走到这里，
                # 盲删会让用户白白丢一次登录），只标记需要重新登录，并短暂冷却，
                # 免得每次刷新都去撞一次墙。
                self._drop_session()
                self.online = True
                self.last_error = "OJ 登录已过期，需要重新登录"
                self._cache = (time.time(), [])
                raise
            except OjUnreachable as exc:
                self.online = False
                self.last_error = exc.message
                raise
            except OjError as exc:
                self.online = True
                self.last_error = exc.message
                raise

            self.online = True
            self.last_error = ""
            self.last_sync = time.time()
            self._cache = (time.time(), items)
            return items


# ============================================================================
# 多站点管理（2.1.0）
# ============================================================================


class OjHub:
    """一个用户可能同时挂着好几个 OJ（每学期换一台机器、或者不同课各有一个）。

    站点列表存在 ``prefs.json`` 的 ``ojSites``；每个站点一个 :class:`OjClient`，
    各自独立登录、独立缓存。汇总时任何一个站点挂掉都只降级它自己，
    不影响其他站点，也不影响畅课那份清单。
    """

    TONES = 3  # 徽标配色循环几种

    def __init__(self, store: Any):
        self._store = store
        self._lock = threading.RLock()
        self._clients: dict[str, OjClient] = {}
        self._sites = self._load_sites()
        self._sync_clients()
        self._migrate_completed_ids()
        # 统一落一次盘：旧版的 ojBase 换成 ojSites，配置文件里不留两份
        self._save_sites()

    # ------------------------------------------------------------------ 配置
    def _read_prefs(self) -> dict[str, Any]:
        try:
            prefs = self._store.load_prefs()
        except Exception:
            prefs = {}
        return prefs if isinstance(prefs, dict) else {}

    def _load_sites(self) -> list[dict[str, str]]:
        prefs = self._read_prefs()
        sites: list[dict[str, str]] = []
        seen: set[str] = set()

        raw = prefs.get("ojSites")
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                try:
                    base = OjClient.normalize_base(str(entry.get("base") or ""))
                except OjError:
                    continue
                if base in seen:
                    continue
                seen.add(base)
                sites.append(
                    {"base": base, "label": str(entry.get("label") or "").strip()[:24]}
                )

        # 旧版本只存了一个 ojBase：升级时当成第一个站点，不丢登录态
        legacy = str(prefs.get("ojBase") or "").strip()
        if legacy:
            try:
                legacy_base = OjClient.normalize_base(legacy)
            except OjError:
                legacy_base = ""
            if legacy_base and legacy_base not in seen:
                sites.insert(0, {"base": legacy_base, "label": ""})

        if not sites:
            sites = [{"base": DEFAULT_BASE, "label": ""}]
        return sites[:MAX_SITES]

    def _save_sites(self) -> None:
        prefs = self._read_prefs()
        prefs["ojSites"] = [
            {"base": site["base"], "label": site["label"]} for site in self._sites
        ]
        prefs.pop("ojBase", None)  # 迁移完成，别留两份
        try:
            self._store.save_prefs(prefs)
        except Exception as exc:
            LOG.warning("保存 OJ 站点列表失败：%s", exc)

    def _tone_map(self) -> dict[str, int]:
        return {
            site["base"]: index % self.TONES for index, site in enumerate(self._sites)
        }

    def _sync_clients(self) -> None:
        """按站点列表补齐 / 回收 client。已有的保留，缓存和在线状态不丢。"""
        wanted = [site["base"] for site in self._sites]
        for base in list(self._clients):
            if base not in wanted:
                self._clients.pop(base, None)
        tones = self._tone_map()
        for site in self._sites:
            base = site["base"]
            client = self._clients.get(base)
            if client is None:
                client = OjClient(
                    self._store,
                    base=base,
                    label=site["label"],
                    tone=tones.get(base, 0),
                )
                self._clients[base] = client
            else:
                client.label = site["label"]
                client.tone = tones.get(base, 0)

    def _migrate_completed_ids(self) -> None:
        """2.0.0 的完成记录是 ``oj-45``，2.1.0 变成 ``oj-<站点>-45``。

        只做一次；迁移时把旧 key 换成第一个站点的 key，用户的勾选不会白丢。
        """
        prefs = self._read_prefs()
        if prefs.get("ojIdVersion") == 2:
            return
        primary = self._sites[0]["base"] if self._sites else DEFAULT_BASE
        key = site_key(primary)
        try:
            state = self._store.load_state()
        except Exception:
            state = {}
        completed = state.get("completed")
        if not isinstance(completed, dict):
            completed = {}
        moved: dict[str, Any] = {}
        for homework_id, record in list(completed.items()):
            match = re.fullmatch(r"oj-(\d+)", str(homework_id))
            if match:
                moved[f"oj-{key}-{match.group(1)}"] = record
                completed.pop(homework_id, None)
        if moved:
            completed.update(moved)
            state["completed"] = completed
            try:
                self._store.save_state(state)
                LOG.info("已迁移 %d 条 OJ 完成记录到多站点 ID。", len(moved))
            except Exception as exc:
                LOG.warning("迁移 OJ 完成记录失败：%s", exc)
        prefs["ojIdVersion"] = 2
        try:
            self._store.save_prefs(prefs)
        except Exception:
            pass

    # ------------------------------------------------------------------ 站点
    def sites(self) -> list[dict[str, Any]]:
        return self.status()["sites"]

    def client(self, base: Optional[str] = None) -> OjClient:
        with self._lock:
            if base:
                key = str(base).rstrip("/")
                found = self._clients.get(key)
                if found is None:
                    raise OjError("这个站点不在列表里，先添加一下。", "OJ_SITE_NOT_FOUND")
                return found
            for site in self._sites:
                candidate = self._clients.get(site["base"])
                if candidate and candidate.logged_in:
                    return candidate
            return self._clients[self._sites[0]["base"]]

    def add_site(self, raw: str, label: str = "") -> dict[str, Any]:
        base = OjClient.normalize_base(raw)
        clean_label = str(label or "").strip()[:24]
        with self._lock:
            existing = next((s for s in self._sites if s["base"] == base), None)
            if existing is not None:
                if clean_label and clean_label != existing["label"]:
                    existing["label"] = clean_label
                    self._save_sites()
                    self._sync_clients()
                return {
                    "base": base,
                    "added": False,
                    "changed": False,
                    "captcha": self.client(base).new_captcha(),
                }

            if len(self._sites) >= MAX_SITES:
                raise OjError(
                    f"最多同时挂 {MAX_SITES} 个 OJ 站点，先移除一个再添加。",
                    "OJ_TOO_MANY_SITES",
                )
            captcha = OjClient.probe(base)  # 先探通再落地，避免把死地址存进去

            # 只剩一个「出厂默认、没改过名、从没登录过」的站点时，用户来加新站点
            # 多半是想换学期，直接把它顶替掉，别凭空多出一个用不上的空站点。
            if len(self._sites) == 1:
                only = self._sites[0]
                only_client = self._clients.get(only["base"])
                untouched_default = (
                    only["base"] == DEFAULT_BASE.rstrip("/")
                    and not only["label"]
                    and (only_client is None or not only_client.logged_in)
                )
                if untouched_default:
                    self._sites = []
                    LOG.info("用新站点替换了从未登录过的出厂默认站点。")

            self._sites.append({"base": base, "label": clean_label})
            self._save_sites()
            self._sync_clients()
            LOG.info("已添加 OJ 站点 %s", base)
            return {"base": base, "added": True, "changed": True, "captcha": captcha}

    def remove_site(self, raw: str) -> dict[str, Any]:
        base = OjClient.normalize_base(raw)
        with self._lock:
            if len(self._sites) <= 1:
                raise OjError("至少保留一个 OJ 站点。", "OJ_LAST_SITE")
            target = next((s for s in self._sites if s["base"] == base), None)
            if target is None:
                raise OjError("这个站点不在列表里。", "OJ_SITE_NOT_FOUND")
            client = self._clients.get(base)
            if client is not None:
                try:
                    client.logout()  # 顺手登出，别在服务端留僵尸会话
                except Exception as exc:
                    LOG.debug("移除站点时登出失败：%s", exc)
            self._sites = [s for s in self._sites if s["base"] != base]
            self._save_sites()
            self._sync_clients()
            LOG.info("已移除 OJ 站点 %s", base)
            return {"base": base, "removed": True}

    def rename_site(self, raw: str, label: str) -> dict[str, Any]:
        base = OjClient.normalize_base(raw)
        clean_label = str(label or "").strip()[:24]
        with self._lock:
            target = next((s for s in self._sites if s["base"] == base), None)
            if target is None:
                raise OjError("这个站点不在列表里。", "OJ_SITE_NOT_FOUND")
            target["label"] = clean_label
            self._save_sites()
            self._sync_clients()
            return {"base": base, "label": clean_label}

    # ------------------------------------------------------------------ 登录
    def new_captcha(self, base: Optional[str] = None) -> dict[str, str]:
        return self.client(base).new_captcha()

    def login(
        self,
        username: str,
        password: str,
        captcha: str,
        captcha_key: str,
        base: Optional[str] = None,
    ) -> dict[str, Any]:
        return self.client(base).login(username, password, captcha, captcha_key)

    def logout(self, base: Optional[str] = None) -> None:
        self.client(base).logout()

    # ------------------------------------------------------------------ 汇总
    def status(self) -> dict[str, Any]:
        with self._lock:
            shorts = unique_shorts([site["base"] for site in self._sites])
            sites: list[dict[str, Any]] = []
            for site in self._sites:
                client = self._clients.get(site["base"])
                info = client.status() if client is not None else {}
                info["short"] = shorts.get(site["base"], site["base"])
                info["label"] = site["label"]
                sites.append(info)

            configured = any(item.get("configured") for item in sites)
            primary = next((item for item in sites if item.get("configured")), None)
            if primary is None:
                primary = sites[0] if sites else {}

            marks = [item.get("online") for item in sites if item.get("configured")]
            online: Optional[bool] = None
            if marks:
                if any(mark is False for mark in marks):
                    online = False
                elif all(mark is True for mark in marks):
                    online = True
                # 剩下的是「还没探测过」，保持 None，别谎报在线

            errors = [
                f"{item['short']}：{item['error']}"
                for item in sites
                if item.get("error")
            ]
            return {
                "configured": configured,
                "sites": sites,
                "siteCount": len(sites),
                "multi": len(sites) > 1,
                # 兼容单站点时代的字段，取「第一台已登录的站点」
                "base": str(primary.get("base") or ""),
                "account": str(primary.get("account") or ""),
                "online": online,
                "lastSync": max((str(item.get("lastSync") or "") for item in sites), default=""),
                "error": "；".join(errors)[:200],
            }

    def todos(self, force: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            clients = [
                self._clients[site["base"]]
                for site in self._sites
                if site["base"] in self._clients
            ]
        clients = [client for client in clients if client.logged_in]
        if not clients:
            return []

        def fetch(client: OjClient) -> list[dict[str, Any]]:
            try:
                return client.todos(force=force)
            except OjAuthRequired:
                LOG.debug("OJ %s 未登录或登录已过期，跳过。", client.base)
            except OjError as exc:
                LOG.warning("读取 OJ %s 失败：%s", client.base, exc.message)
            except Exception:  # 单站点的意外不能拖垮整块看板
                LOG.exception("读取 OJ %s 时出现未预期的错误", client.base)
            return []

        if len(clients) == 1:
            items = fetch(clients[0])
        else:
            # 并行拉：一台离线会卡在连接超时上，串行会把其他站点一起拖慢
            items = []
            with ThreadPoolExecutor(max_workers=min(len(clients), MAX_SITES)) as pool:
                for chunk in pool.map(fetch, clients):
                    items.extend(chunk)
        items.sort(key=lambda item: item["dueDate"])
        return items
