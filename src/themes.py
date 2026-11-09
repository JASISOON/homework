"""主题定义、校验与导入。

一个主题就是一组 CSS 变量（主界面）+ 一条悬浮球调色板推导规则。
内置三套：warm（暖色，默认，也就是 1.2.x 的观感）、light（亮色）、dark（深色）。
自定义主题通过导入 JSON 得到，只允许覆盖下面 ``THEME_VARS`` 里列出的变量，
其余一律走 ``SCHEME_DEFAULTS`` 兜底，避免把界面改成不可读的样子。
"""

from __future__ import annotations

import colorsys
import hashlib
import re
import time
import uuid
from typing import Any

SCHEMA_VERSION = 1

# 主题可以覆盖的 CSS 变量。键是变量名，值是校验提示用的说明。
THEME_VARS: dict[str, str] = {
    "--bg": "窗口底色",
    "--bg-deep": "更深的底色（渐变/衬底）",
    "--surface": "卡片表面",
    "--surface-soft": "次级卡片表面",
    "--surface-tint": "暖白衬底",
    "--gloss": "卡片高光渐变",
    "--edge": "卡片内高光边",
    "--chip": "胶囊底色",
    "--ink": "主文字",
    "--ink-soft": "次级文字",
    "--muted": "弱化文字",
    "--muted-strong": "稍强的弱化文字",
    "--muted-weak": "最弱的辅助文字",
    "--line": "分隔线",
    "--line-soft": "更淡的分隔线",
    "--line-strong": "强调分隔线",
    "--green": "主色（按钮底色/图标）",
    "--green-dark": "主色深调",
    "--green-soft": "主色浅底",
    "--green-tint": "主色衬底",
    "--green-line": "主色描边",
    "--accent-ink": "主色文字",
    "--accent-ink-strong": "主色文字（重）",
    "--red": "危险色（底色）",
    "--red-strong": "危险色深调",
    "--red-soft": "危险色浅底",
    "--red-tint": "危险色衬底",
    "--red-line": "危险色描边",
    "--danger-ink": "危险色文字",
    "--orange": "提醒色（底色）",
    "--orange-strong": "提醒色深调",
    "--orange-soft": "提醒色浅底",
    "--orange-tint": "提醒色衬底",
    "--orange-line": "提醒色描边",
    "--warn-ink": "提醒色文字",
    "--on-accent": "彩色底上的文字",
    "--glow-a": "背景光晕（冷）",
    "--glow-b": "背景光晕（暖）",
    "--grid-rgb": "网格线 RGB",
    "--shadow-rgb": "阴影 RGB",
    "--green-rgb": "主色 RGB",
    "--green-dark-rgb": "主色深调 RGB",
    "--red-rgb": "危险色 RGB",
    "--red-strong-rgb": "危险色深调 RGB",
    "--muted-rgb": "弱化文字 RGB",
    "--surface-rgb": "表面 RGB",
}

HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
RGB_TRIPLE_RE = re.compile(r"^\d{1,3}, ?\d{1,3}, ?\d{1,3}$")
COLOR_FN_RE = re.compile(r"^(?:rgba?|hsla?)\([0-9., %]+\)$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")


def _hex(value: str) -> tuple[int, int, int]:
    text = value.lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _hex_text(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _hls(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    r, g, b = (c / 255.0 for c in rgb)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h, l, s


def _from_hls(h: float, l: float, s: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))


def _vivid(rgb: tuple[int, int, int], dl: float, ds: float = 0.0) -> tuple[int, int, int]:
    """在 HLS 空间里提亮/加饱和，用来从主色推出悬浮球的亮面颜色。"""
    h, l, s = _hls(rgb)
    return _from_hls(h, l + dl, s + ds)


def _lighten(rgb: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return _mix(rgb, (255, 255, 255), amount)


def _darken(rgb: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return _mix(rgb, (0, 0, 0), amount)


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (c / 255.0 for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


# --------------------------------------------------------------------------- 内置主题
# warm 就是 1.2.x 的原配色，保持像素级一致；light 是中性偏冷的白；dark 是深石板。
BUILTIN_THEMES: list[dict[str, Any]] = [
    {
        "id": "warm",
        "name": "暖色",
        "description": "米白纸感 + 墨绿强调，1.2.x 的默认观感。",
        "author": "内置",
        "scheme": "light",
        "glass": "dark",
        "builtin": True,
        "vars": {
            "--bg": "#f4f1eb",
            "--bg-deep": "#ece7dc",
            "--surface": "#fffdf9",
            "--surface-soft": "#faf7f0",
            "--surface-tint": "#f0ece3",
            "--gloss": "rgba(255, 255, 255, 0.65)",
            "--edge": "rgba(255, 255, 255, 0.8)",
            "--chip": "rgba(255, 255, 255, 0.64)",
            "--ink": "#202a24",
            "--ink-soft": "#3a463f",
            "--muted": "#7a8179",
            "--muted-strong": "#5c665e",
            "--muted-weak": "#a7a095",
            "--line": "#e4dfd4",
            "--line-soft": "#ebe6dc",
            "--line-strong": "#d7d0c2",
            "--green": "#2f5b4c",
            "--green-dark": "#234438",
            "--green-soft": "#e7efe9",
            "--green-tint": "#dbe7d5",
            "--green-line": "#c5d2c7",
            "--accent-ink": "#2f5b4c",
            "--accent-ink-strong": "#234438",
            "--red": "#b52d3b",
            "--red-strong": "#8d1f2c",
            "--red-soft": "#fcecec",
            "--red-tint": "#fbe5e5",
            "--red-line": "#ebcfd0",
            "--danger-ink": "#8d1f2c",
            "--orange": "#bd650d",
            "--orange-strong": "#9d5308",
            "--orange-soft": "#fff2df",
            "--orange-tint": "#fbe9cf",
            "--orange-line": "#eadcc1",
            "--warn-ink": "#9d5308",
            "--on-accent": "#ffffff",
            "--glow-a": "rgba(170, 198, 177, 0.44)",
            "--glow-b": "rgba(235, 207, 166, 0.43)",
            "--grid-rgb": "71, 83, 73",
            "--shadow-rgb": "52, 45, 31",
            "--green-rgb": "47, 91, 76",
            "--green-dark-rgb": "35, 68, 56",
            "--red-rgb": "181, 45, 59",
            "--red-strong-rgb": "141, 31, 44",
            "--muted-rgb": "122, 129, 121",
            "--surface-rgb": "255, 253, 249",
        },
    },
    {
        "id": "light",
        "name": "亮色",
        "description": "中性冷白，边界更清晰，白天室内看着最干净。",
        "author": "内置",
        "scheme": "light",
        "glass": "dark",
        "builtin": True,
        "vars": {
            "--bg": "#f6f7f7",
            "--bg-deep": "#e9ecec",
            "--surface": "#ffffff",
            "--surface-soft": "#f4f6f6",
            "--surface-tint": "#eef1f1",
            "--gloss": "rgba(255, 255, 255, 0.66)",
            "--edge": "rgba(255, 255, 255, 0.82)",
            "--chip": "rgba(255, 255, 255, 0.66)",
            "--ink": "#1a2220",
            "--ink-soft": "#333d3a",
            "--muted": "#6e7876",
            "--muted-strong": "#4d5856",
            "--muted-weak": "#a6afad",
            "--line": "#e2e6e6",
            "--line-soft": "#ecefee",
            "--line-strong": "#d0d7d6",
            "--green": "#2b6353",
            "--green-dark": "#1e4b3e",
            "--green-soft": "#e6f0ec",
            "--green-tint": "#d8e8e1",
            "--green-line": "#bfd6cc",
            "--accent-ink": "#2b6353",
            "--accent-ink-strong": "#1e4b3e",
            "--red": "#c0323f",
            "--red-strong": "#99202c",
            "--red-soft": "#fdebed",
            "--red-tint": "#fbdde0",
            "--red-line": "#f0c7cb",
            "--danger-ink": "#99202c",
            "--orange": "#bf6a10",
            "--orange-strong": "#9a5209",
            "--orange-soft": "#fff3e3",
            "--orange-tint": "#fbe6c7",
            "--orange-line": "#efd7b4",
            "--warn-ink": "#9a5209",
            "--on-accent": "#ffffff",
            "--glow-a": "rgba(166, 205, 197, 0.40)",
            "--glow-b": "rgba(188, 205, 224, 0.38)",
            "--grid-rgb": "70, 84, 82",
            "--shadow-rgb": "36, 46, 44",
            "--green-rgb": "43, 99, 83",
            "--green-dark-rgb": "30, 75, 62",
            "--red-rgb": "192, 50, 63",
            "--red-strong-rgb": "153, 32, 44",
            "--muted-rgb": "110, 120, 118",
            "--surface-rgb": "255, 255, 255",
        },
    },
    {
        "id": "dark",
        "name": "深色",
        "description": "深石板底 + 亮薄荷绿，晚上看不刺眼。",
        "author": "内置",
        "scheme": "dark",
        "glass": "dark",
        "builtin": True,
        "vars": {
            "--bg": "#12171a",
            "--bg-deep": "#0c1113",
            "--surface": "#1a2124",
            "--surface-soft": "#161d20",
            "--surface-tint": "#212a2d",
            "--gloss": "rgba(255, 255, 255, 0.05)",
            "--edge": "rgba(255, 255, 255, 0.07)",
            "--chip": "rgba(255, 255, 255, 0.11)",
            "--ink": "#e7efeb",
            "--ink-soft": "#cbd6d1",
            "--muted": "#8e9b96",
            "--muted-strong": "#b0bcb7",
            "--muted-weak": "#6c7873",
            "--line": "#2a3336",
            "--line-soft": "#232b2e",
            "--line-strong": "#384346",
            "--green": "#2f6f5a",
            "--green-dark": "#255947",
            "--green-soft": "#1b2f28",
            "--green-tint": "#1f3a31",
            "--green-line": "#2f5448",
            "--accent-ink": "#8ad0b2",
            "--accent-ink-strong": "#b2e3cc",
            "--red": "#7a2731",
            "--red-strong": "#5f1c25",
            "--red-soft": "#33181b",
            "--red-tint": "#3d1d21",
            "--red-line": "#5c2b32",
            "--danger-ink": "#ef9aa2",
            "--orange": "#6d451a",
            "--orange-strong": "#543411",
            "--orange-soft": "#2f2416",
            "--orange-tint": "#3a2c1a",
            "--orange-line": "#57401f",
            "--warn-ink": "#e6b171",
            "--on-accent": "#ffffff",
            "--glow-a": "rgba(58, 108, 90, 0.42)",
            "--glow-b": "rgba(96, 78, 46, 0.34)",
            "--grid-rgb": "150, 170, 162",
            "--shadow-rgb": "0, 0, 0",
            "--green-rgb": "47, 111, 90",
            "--green-dark-rgb": "37, 89, 71",
            "--red-rgb": "122, 39, 49",
            "--red-strong-rgb": "95, 28, 37",
            "--muted-rgb": "142, 155, 150",
            "--surface-rgb": "26, 33, 36",
        },
    },
]

DEFAULT_THEME_ID = "warm"
BUILTIN_IDS = {theme["id"] for theme in BUILTIN_THEMES}


class ThemeError(ValueError):
    """主题数据不合法。"""


def builtin_theme(theme_id: str) -> dict[str, Any] | None:
    for theme in BUILTIN_THEMES:
        if theme["id"] == theme_id:
            return theme
    return None


def _scheme_defaults(scheme: str) -> dict[str, str]:
    base = builtin_theme("dark" if scheme == "dark" else "warm")
    assert base is not None
    return dict(base["vars"])


def _stable_digest(*parts: str) -> str:
    """给「没有 id 的主题」算一个稳定摘要，重启后仍然是同一个 id。"""
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def normalize_theme(raw: dict[str, Any], *, keep_id: bool = False) -> dict[str, Any]:
    """把任意来源的主题 JSON 收拾成内部结构；不合法直接抛 ThemeError。"""
    if not isinstance(raw, dict):
        raise ThemeError("主题必须是一个 JSON 对象。")

    name = str(raw.get("name") or "").strip()
    if not name:
        raise ThemeError("主题缺少 name 字段。")
    if len(name) > 32:
        raise ThemeError("主题名最多 32 个字符。")

    description = str(raw.get("description") or "").strip()[:80]
    author = str(raw.get("author") or "").strip()[:32]
    scheme = str(raw.get("scheme") or "").strip().lower()
    if scheme not in {"light", "dark"}:
        scheme = "light"
    glass = str(raw.get("glass") or "").strip().lower()
    if glass not in {"dark", "light"}:
        glass = "dark" if scheme == "light" else "dark"

    raw_vars = raw.get("vars")
    if not isinstance(raw_vars, dict):
        raise ThemeError("主题缺少 vars 对象。")

    theme_id = str(raw.get("id") or "").strip().lower()
    if not ID_RE.match(theme_id):
        theme_id = ""
    elif not keep_id:
        theme_id = ""
    if not theme_id and keep_id:
        # 手写的 themes.json 常常只有 name、没有 id；这时按名字算一个稳定 id，
        # 否则每次启动都会重新随机，活动主题就对不上了。
        theme_id = "custom-" + _stable_digest(name, author or "自定义")[:8]

    merged = _scheme_defaults(scheme)
    explicit: set[str] = set()
    for key, value in raw_vars.items():
        key = str(key)
        if key not in THEME_VARS:
            raise ThemeError(f"不认识的变量：{key}")
        text = str(value).strip()
        if key.endswith("-rgb"):
            if not RGB_TRIPLE_RE.match(text):
                raise ThemeError(f"{key} 需要写成 \"12, 34, 56\" 这样的 RGB 三元组。")
        elif key.startswith("--glow-"):
            if not COLOR_FN_RE.match(text):
                raise ThemeError(f"{key} 需要写成 rgba(...) 形式的颜色。")
        else:
            if not HEX_RE.match(text):
                raise ThemeError(f"{key} 需要是 #rrggbb 形式的颜色。")
        merged[key] = text
        explicit.add(key)

    if len(explicit) < 3:
        raise ThemeError("主题至少要有 3 个变量，比如 --bg、--ink、--green。")

    merged = _derive_vars(merged, explicit, scheme)

    return {
        "id": theme_id or "custom-" + uuid.uuid4().hex[:8],
        "name": name,
        "description": description,
        "author": author or "自定义",
        "scheme": scheme,
        "glass": glass,
        "builtin": False,
        "vars": merged,
        "createdAt": str(raw.get("createdAt") or "") or time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _derive_vars(merged: dict[str, str], explicit: set[str], scheme: str) -> dict[str, str]:
    """按核心色推导派生变量；用户显式给了的就不动。

    这样导入一个「只写了 bg/surface/ink/主色」的主题也能得到协调的浅底、描边、
    强调文字与悬浮球配色，而不会出现「粉色主色配墨绿文字」这种违和感。
    """
    dark = scheme == "dark"
    surface = _hex(merged["--surface"])
    bg = _hex(merged["--bg"])
    ink = _hex(merged["--ink"])
    muted = _hex(merged["--muted"])
    green = _hex(merged["--green"])
    green_dark = _hex(merged["--green-dark"])
    red = _hex(merged["--red"])
    red_strong = _hex(merged["--red-strong"])
    orange = _hex(merged["--orange"])
    orange_strong = _hex(merged["--orange-strong"])

    accent_ink = _lighten(green, 0.34) if dark else _darken(green, 0.02)
    accent_ink_strong = _lighten(green, 0.48) if dark else _darken(green_dark, 0.06)
    danger_ink = _lighten(red, 0.44) if dark else red_strong
    warn_ink = _lighten(orange, 0.40) if dark else orange_strong

    def tone(color: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
        return _mix(surface, color, amount) if dark else _mix(surface, color, amount)

    on_accent = "#ffffff" if _luminance(green) < 0.62 else _hex_text(_darken(ink, 0.1))

    derived = {
        "--bg-deep": _hex_text(_mix(bg, (0, 0, 0), 0.30) if dark else _mix(bg, ink, 0.05)),
        "--surface-tint": _hex_text(tone(ink, 0.04)),
        "--gloss": "rgba(255, 255, 255, %.2f)" % (0.05 if dark else 0.65),
        "--edge": "rgba(255, 255, 255, %.2f)" % (0.07 if dark else 0.8),
        "--chip": "rgba(255, 255, 255, %.2f)" % (0.11 if dark else 0.64),
        "--muted": _hex_text(_mix(ink, surface, 0.45)),
        "--muted-strong": _hex_text(_mix(ink, surface, 0.25)),
        "--muted-weak": _hex_text(_mix(muted, surface, 0.42)),
        "--line": _hex_text(tone(ink, 0.12)),
        "--line-soft": _hex_text(tone(ink, 0.07)),
        "--line-strong": _hex_text(tone(ink, 0.22)),
        "--green-soft": _hex_text(tone(green, 0.14)),
        "--green-tint": _hex_text(tone(green, 0.22)),
        "--green-line": _hex_text(tone(green, 0.34)),
        "--accent-ink": _hex_text(accent_ink),
        "--accent-ink-strong": _hex_text(accent_ink_strong),
        "--red-soft": _hex_text(tone(red, 0.14)),
        "--red-tint": _hex_text(tone(red, 0.22)),
        "--red-line": _hex_text(tone(red, 0.34)),
        "--danger-ink": _hex_text(danger_ink),
        "--orange-soft": _hex_text(tone(orange, 0.14)),
        "--orange-tint": _hex_text(tone(orange, 0.22)),
        "--orange-line": _hex_text(tone(orange, 0.34)),
        "--warn-ink": _hex_text(warn_ink),
        "--on-accent": on_accent,
        "--glow-a": "rgba(%d, %d, %d, %.2f)" % (*_lighten(green, 0.15), 0.40 if not dark else 0.42),
        "--glow-b": "rgba(%d, %d, %d, %.2f)" % (*_lighten(orange, 0.25), 0.30 if not dark else 0.28),
        "--grid-rgb": "%d, %d, %d" % _mix(ink, bg, 0.6),
        "--shadow-rgb": "0, 0, 0" if dark else "%d, %d, %d" % _mix(ink, bg, 0.55),
        "--green-rgb": "%d, %d, %d" % green,
        "--green-dark-rgb": "%d, %d, %d" % green_dark,
        "--red-rgb": "%d, %d, %d" % red,
        "--red-strong-rgb": "%d, %d, %d" % red_strong,
        "--orange-rgb": "%d, %d, %d" % orange,
        "--muted-rgb": "%d, %d, %d" % muted,
        "--surface-rgb": "%d, %d, %d" % surface,
    }
    for key, value in derived.items():
        if key not in explicit:
            merged[key] = value
    return merged


# --------------------------------------------------------------------------- 悬浮球调色板
def ball_palette(theme: dict[str, Any]) -> dict[str, tuple[int, int, int, int]]:
    """把主题推导成悬浮球绘制用的 ARGB 调色板。

    悬浮球的球体永远用主题的绿色系；浮层玻璃按 ``glass`` 决定是深色还是浅色，
    浅色玻璃会自动把文字换成深色，避免白字贴白底。
    """
    v = theme["vars"]
    green = _hex(v["--green"])
    green_dark = _hex(v["--green-dark"])
    red = _hex(v["--red"])
    orange = _hex(v["--orange"])
    ink = _hex(v["--ink"])
    # 球上/卡片里的“强调文字”要用 ink 版：--red / --orange 这类令牌在深色主题里
    # 是「深底上的底色调」，直接当文字色会糊成一片。没有 ink 令牌就退回原色。
    warn_ink = _hex(v.get("--warn-ink") or v["--orange"])
    danger_ink = _hex(v.get("--danger-ink") or v["--red"])
    accent_ink = _hex(v.get("--accent-ink") or v["--green"])
    muted = _hex(v["--muted"])
    surface = _hex(v["--surface"])
    surface_soft = _hex(v.get("--surface-soft", v["--surface"]))
    glass_light = theme.get("glass") == "light"

    # 球体渐变：比主色亮一点到深调，保证在浅色/深色桌面上都看得见。
    ball_a = _vivid(green, 0.16, 0.04)
    ball_b = _lighten(green_dark, 0.07)

    def argb(rgb: tuple[int, int, int], alpha: int) -> tuple[int, int, int, int]:
        return (alpha, max(0, min(255, rgb[0])), max(0, min(255, rgb[1])), max(0, min(255, rgb[2])))

    white = (255, 255, 255)
    if glass_light:
        title = _darken(ink, 0.1)
        text = ink
        sub = _mix(ink, muted, 0.45)
        hint = muted
        label = _mix(ink, muted, 0.55)
        border = argb(_darken(surface, 0.12), 90)
        divider = argb(ink, 34)
        hover = argb(ink, 14)
        top = argb(_lighten(surface, 0.02), 232)
        bottom = argb(surface_soft, 242)
        badge_ink_dark = _darken(ink, 0.2)
        close_fill = argb(surface, 250)
        close_border = argb(_darken(surface, 0.18), 200)
        close_ink = _mix(ink, muted, 0.3)
        shadow = argb(_darken(ink, 0.4), 70)
        rim = argb(_lighten(surface, 0.4), 90)
    else:
        # 深色玻璃上「白色掺一点主色」，和 1.2.x 的观感一致
        title = _mix(white, green, 0.09)
        text = _mix(white, green, 0.13)
        sub = _mix(white, green, 0.44)
        hint = _mix(white, green, 0.40)
        label = _mix(white, green, 0.47)
        border = argb(white, 56)
        divider = argb(white, 30)
        hover = argb(white, 16)
        top = (188, 27, 32, 35)
        bottom = (202, 15, 19, 22)
        badge_ink_dark = _darken(green, 0.2)
        close_fill = argb(white, 30)
        close_border = argb(white, 205)
        close_ink = _mix(white, green, 0.08)
        shadow = argb(_darken(green_dark, 0.5), 92)
        rim = argb(white, 64)

    # 角标在浅色玻璃上要用深字，深色玻璃上正常白字
    badge_text_on_danger = (255, 255, 255, 255)
    badge_text_on_warn = argb(_darken(orange, 0.62), 255)

    return {
        # 球体
        "ball_a": argb(ball_a, 255),
        "ball_b": argb(ball_b, 255),
        "ball_shadow": shadow,
        "ball_rim": rim,
        "ball_icon": (245, 255, 255, 255),
        "ball_icon_soft": (225, 255, 255, 255),
        "ring": argb(red, 255),
        # 角标
        "badge_danger": argb(red, 255),
        "badge_warn": argb(orange, 255),
        "badge_fill": (255, 255, 255, 255),
        "badge_danger_text": badge_text_on_danger,
        "badge_warn_text": badge_text_on_warn,
        "badge_text": argb(badge_ink_dark, 255),
        # 浮层玻璃
        "glass_top": top,
        "glass_bottom": bottom,
        "glass_border": border,
        "divider": divider,
        "hover": hover,
        # 文字
        "title": argb(title, 255),
        "text": argb(text, 255),
        "sub": argb(sub, 255),
        "hint": argb(hint, 255),
        "label": argb(label, 255),
        # 强调
        "accent_none": argb(_mix(muted, surface, 0.2), 255),
        "accent_normal": argb(accent_ink, 255),
        "accent_urgent": argb(warn_ink, 255),
        "accent_overdue": argb(danger_ink, 255),
        "stat_today": argb(warn_ink, 255),
        "stat_urgent": argb(_mix(warn_ink, danger_ink, 0.35), 255),
        "stat_overdue": argb(danger_ink, 255),
        # 关闭按钮
        "close_fill": close_fill,
        "close_border": close_border,
        "close_ink": argb(close_ink, 255),
        # 菜单
        "menu_danger": argb(danger_ink, 255),
    }
