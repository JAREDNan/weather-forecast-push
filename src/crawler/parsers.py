# -*- coding: utf-8 -*-
"""
共享解析工具：从中国天气网的 JS 变量响应中提取 JSON，以及安全的数值转换。

中国天气网的 d1.weather.com.cn 系列接口返回的不是纯 JSON，而是形如：
    var cityDZ ={...};var alarmDZ ={...};var dataSK ={...};
的 JS 赋值语句。本模块用括号配对法精确截取变量值，避免正则贪婪匹配
在嵌套对象上出错，也避免引入 BeautifulSoup（提示词 §4.2 要求优先 API）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# 中国天气网接口失效时返回 HTTP 200 + 一个「网页无法访问」的 HTML 页面。
# 必须识别，否则会把 404 页面当成数据解析。
SOFT_404_MARKERS = ("网页无法访问",)

# cityDZ.weatherinfo.temp 等字段用 "999" 表示「无数据」，不是真实温度。
SENTINEL_VALUES = {"999", "-999", "9999", "", "-", "--", "null", "None"}


def is_soft_404(text: str, head_limit: int = 3000) -> bool:
    """判断响应是否为「状态 200 但内容是错误页」。"""
    if not text:
        return False
    head = text[:head_limit]
    return any(m in head for m in SOFT_404_MARKERS)


def extract_js_var(text: str, name: str) -> Optional[Any]:
    """从 JS 响应中提取 `var <name> = {...}` 或 `[...]` 并解析为 Python 对象。

    使用括号配对而非贪婪正则，正确处理嵌套对象与字符串内的括号。
    解析失败返回 None（调用方据此降级，不猜测）。
    """
    if not text:
        return None
    m = re.search(r"var\s+" + re.escape(name) + r"\s*=\s*", text)
    if not m:
        return None

    start = m.end()
    while start < len(text) and text[start] in " \t\r\n":
        start += 1
    if start >= len(text) or text[start] not in "{[":
        return None

    open_ch = text[start]
    close_ch = {"{": "}", "[": "]"}[open_ch]

    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        c = text[i]
        if escaped:
            escaped = False
            continue
        if c == "\\":
            escaped = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                snippet = text[start:i + 1]
                try:
                    return json.loads(snippet)
                except json.JSONDecodeError as exc:
                    log.warning("JS 变量 %s 解析失败：%s", name, exc)
                    return None
    log.warning("JS 变量 %s 括号未闭合", name)
    return None


def extract_all_js_vars(text: str) -> List[str]:
    """列出响应中所有 JS 变量名，用于诊断接口结构变化。"""
    if not text:
        return []
    return re.findall(r"var\s+(\w+)\s*=", text)


def parse_json_safely(text: str) -> Optional[Any]:
    """解析纯 JSON 响应；失败返回 None。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("JSON 解析失败：%s", exc)
        return None


def to_float(value: Any, *, sentinel_check: bool = True) -> Optional[float]:
    """安全转 float。

    遇到 "999"、""、"-"、None 等哨兵/缺失值一律返回 None，
    绝不把哨兵值当真实数据入库（否则会出现「聊城 999℃」这类事故）。

    ⚠️ 单位剥离顺序有讲究：必须先剥「km/h」「m/s」这类含符号的复合单位，
       再去掉 ℃/% 等符号。若先去符号，"8km/h" 会变成 "8kmh"，
       导致后续单位正则无法匹配、最终解析失败返回 None。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        if sentinel_check and f in (999.0, -999.0, 9999.0):
            return None
        return f

    s = str(value).strip()
    if not s or s in SENTINEL_VALUES:
        return None

    # 1) 先剥复合单位（含符号的必须排在纯字母单位之前）
    s = re.sub(r"(km/h|m/s|hPa|mb|mmHg|mm|km|cm|m|%)$", "", s).strip()
    # 2) 再去掉残留符号
    s = re.sub(r"[%℃°/]", "", s).strip()

    if not s or s in SENTINEL_VALUES:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if sentinel_check and f in (999.0, -999.0, 9999.0):
        return None
    return f


def to_int(value: Any) -> Optional[int]:
    f = to_float(value)
    return None if f is None else int(f)


def clean_text(value: Any) -> Optional[str]:
    """清洗文本字段：去 HTML 标签、压缩空白；空值/哨兵返回 None。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s in SENTINEL_VALUES:
        return None
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def norm_date8(value: Any) -> Optional[str]:
    """把 YYYYMMDD / YYYYMMDDHHMM 规范为 YYYY-MM-DD。"""
    s = clean_text(value)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def norm_datetime(value: Any) -> Optional[str]:
    """把 YYYYMMDDHHMM 规范为 YYYY-MM-DD HH:MM。"""
    s = clean_text(value)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 12:
        return (f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]} "
                f"{digits[8:10]}:{digits[10:12]}")
    if len(digits) >= 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def norm_iso_hour(value: Any) -> Optional[str]:
    """把 YYYYMMDDHHMM 规范为 ISO 风格 YYYY-MM-DDTHH:MM。"""
    s = clean_text(value)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 10:
        return (f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
                f"T{digits[8:10]}:{digits[10:12]}")
    return None


def split_turn_text(text: Optional[str]) -> List[str]:
    """把「多云转阴」拆成 ["多云", "阴"]；无「转」则返回单元素列表。"""
    s = clean_text(text)
    if not s:
        return []
    parts = [p.strip() for p in s.split("转") if p.strip()]
    return parts or [s]


def strip_html(text: str) -> str:
    """去除 HTML 标签并压缩空白（用于从页面提取中文文本）。"""
    if not text:
        return ""
    s = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;?", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def build_dict(*pairs) -> Dict[str, Any]:
    """便捷构造：忽略值为 None 的键，减少入库噪音。"""
    return {k: v for k, v in pairs if v is not None}
