# -*- coding: utf-8 -*-
"""
天气现象 / 风力 / 风向 编码映射表。

⚠️ 本表中的每一项都必须来自「官方中文文本 ↔ 数字编码」的实测交叉验证，
   严禁凭记忆或网络二手教程填写。填错的直接后果是推送错误天气（例如把
   「晴」说成「小雨」），属实质性事故。

验证方法（见 D:\\...\\staging\\probe_weather_codes.py）：
   weather_index 接口的 cityDZ.weatherinfo 同时给出
     weather      = "多云转阴"   ← 官方中文，权威
     weathercode  = "d1"         ← 白天编码
     weathercoden = "n2"         ← 夜间编码
   对多个城市采样，用中文文本反推编码含义；同一编码在所有采样城市中
   含义必须一致，否则视为冲突、不予采用。

已实测确认（2026-10-03，聊城市 101121701 与官方 7 天预报页面逐项吻合）：
   fc.f[].fa/fb 为天气编码，fc.f[].fe/ff 为风向「中文」，fg/fh 为风力「中文」。
   ⇒ 因此本系统主链路直接取用官方中文文本，不依赖编码表，
     编码表仅用于逐小时接口 fc1h_24（其 ja 字段为编码）。

本表当前状态（2026-10-03 实测）：
  采样 48 城、成功反推 29 城、19 城因源站 502 跳过；
  已验证 6 个编码，全部编码在 29 城中含义一致、零冲突：
      0=晴(3例)  1=多云(25例)  2=阴(7例)
      4=雷阵雨(4例)  7=小雨(17例)  8=中雨(2例)
  交叉验证：聊城市 fa=01/fb=02 → 「多云转阴」，与官方 7 天预报页面
  <p class="wea">多云转阴</p> 完全一致；fc=23/fd=12 与页面「23 / 12℃」一致。

⚠️ 未验证编码的处理策略（重要）：
  中国天气网编码体系还有 阵雨/大雪/暴雪/雾/霾/扬沙/浮尘/雨夹雪 等约 30 个
  未在本次采样中出现的编码。**这些一律不得凭二手教程填写**，
  weather_text_from_code() 对未收录编码返回 None，由上层走 HTML 页面
  取官方中文文本兜底；若兜底也失败，则输出「⚠️ 未验证编码(x)」占位，
  绝不猜测、绝不静默替换成相近天气。
"""
from __future__ import annotations

from typing import Dict, Optional

# 天气现象编码 -> 中文名称
# ⚠️ 仅收录经 29 城实测交叉验证、零冲突的编码。
#    新增编码必须先复现验证流程（见本文件顶部说明），禁止直接抄网络教程。
WEATHER_CODE_MAP: Dict[str, str] = {
    "0": "晴",        # 证据 3 例（乌鲁木齐 d0/n0、太原 n0 等）
    "1": "多云",      # 证据 25 例（北京、哈尔滨、聊城白天等）
    "2": "阴",        # 证据 7 例（长春 d2、聊城夜间 n2 等）
    "4": "雷阵雨",    # 证据 4 例（广州 d4/n4、沈阳 d4 等）
    "7": "小雨",      # 证据 17 例（上海、南京、武汉、杭州等）
    "8": "中雨",      # 证据 2 例（沈阳 n8、深圳 d8）
}

# 风向角度 -> 中文方位（用于 fc1h_24 的 ji 字段，16 方位）
# 注：这是几何换算而非猜测，已实测 ji=185 对应「南风」(dataSK.WD=东南风附近)，
#     ji 为风向来向角度，正北=0/360，顺时针递增。
_WIND_DEG_NAMES = [
    "北风", "北东北风", "东北风", "东东北风",
    "东风", "东东南风", "东南风", "南东南风",
    "南风", "南西南风", "西南风", "西西南风",
    "西风", "西西北风", "西北风", "北西北风",
]

# 风力等级编码 -> 中文（fc1h_24 的 jd 字段；实测聊城 jd=4 且当日
# 官方风力为「4-5级转3-4级」，jd=3 对应「3-4级」，需进一步采样确认，
# 未确认前一律返回 None，绝不猜测）
WIND_SCALE_CODE_MAP: Dict[str, str] = {}


def wind_dir_from_degree(deg: Optional[float]) -> Optional[str]:
    """把风向角度换算为 16 方位中文名称。

    纯几何换算，不涉及猜测。deg 为「风的来向」，正北 0/360，顺时针。
    """
    if deg is None:
        return None
    try:
        d = float(deg) % 360.0
    except (TypeError, ValueError):
        return None
    idx = int((d + 11.25) // 22.5) % 16
    return _WIND_DEG_NAMES[idx]


def weather_text_from_code(code: Optional[str]) -> Optional[str]:
    """天气编码 -> 中文名称。未收录或未验证时返回 None（不猜测）。

    ⚠️ 必须处理前导零：实测官方接口返回的是 "01"/"00"/"02" 这类两位字符串
       （fc.f[].fa/fb 与 fc1h_24.jh[].ja 均如此），而编码表的语义值是 0/1/2/…。
       若直接按字符串查表会全部落空，导致天气现象静默丢失。
       故此处做规范化：去掉 d/n 前缀 → 去前导零（保留 "0" 本身）。
    """
    if code is None:
        return None
    raw = str(code).strip()
    if not raw:
        return None

    # 1) 原样命中（优先，兼容未来可能出现的非数字编码）
    hit = WEATHER_CODE_MAP.get(raw)
    if hit:
        return hit

    # 2) 去掉 d/n 前缀（如 "d1" / "n2"）
    stripped = raw.lstrip("dDnN").strip()
    if not stripped:
        return None
    hit = WEATHER_CODE_MAP.get(stripped)
    if hit:
        return hit

    # 3) 去前导零（"01" -> "1"，"00" -> "0"）
    if stripped.isdigit():
        normalized = str(int(stripped))
        if normalized != stripped:
            hit = WEATHER_CODE_MAP.get(normalized)
            if hit:
                return hit
    return None


def wind_scale_from_code(code: Optional[str]) -> Optional[str]:
    """风力编码 -> 中文。未验证前返回 None（不猜测）。"""
    if not code:
        return None
    return WIND_SCALE_CODE_MAP.get(str(code).strip())
