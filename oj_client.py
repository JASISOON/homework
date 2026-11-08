"""OJ（校内 QDUOJ 判题系统）只读适配层。

职责只有三件事：
  1. 用「账号 + 密码 + 图形验证码」换取 Django 的 sessionid，加密存本机；
  2. 把实验/作业列表与提交结果，转换成主程序统一的待办结构；
  3. 任何失败都抛 OjError 子类，由主程序降级展示，绝不影响畅课数据。

不做任何写操作：不提交代码、不撤销提交、不改分数。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

LOG = logging.getLogger("homeworkdesk.oj")

DEFAULT_BASE = "http://172.31.221.62"
SUBMITTED_KEEP_DAYS = 7      # 已交的实验保留几天（能看到得分）
MISSING_KEEP_DAYS = 30       # 过期未交的实验保留几天，避免陈年旧账一直挂着
CACHE_TTL = 90               # 列表缓存，秒

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


class OjClient:
    """一个账号一个实例，内部持有一个 requests.Session。"""

    def __init__(self, store: Any, base: Optional[str] = None):
        self._store = store
        self._lock = threading.RLock()
        self._session: Optional[requests.Session] = None
        self._cache: Optional[tuple[float, list[dict[str, Any]]]] = None
        self._course_cache: dict[int, dict[str, str]] = {}
        self._saved: Optional[dict[str, Any]] = store.load_oj_session()
        self.last_error = ""
        self.last_sync = 0.0
        self.online: Optional[bool] = None
        self.base = (
            base
            or (self._saved or {}).get("base")
            or DEFAULT_BASE
        ).rstrip("/")

    # ------------------------------------------------------------------ 状态
    @property
    def logged_in(self) -> bool:
        return bool(self._saved and self._saved.get("sessionid"))

    @property
    def account(self) -> str:
        return str((self._saved or {}).get("username") or "")

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.logged_in,
            "base": self.base,
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
            self._store.delete_oj_session()
            self._saved = None
            self._drop_session()
            self._course_cache.clear()
            self.online = None

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
                    "id": f"oj-{cid}",
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
