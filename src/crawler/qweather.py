# -*- coding: utf-8 -*-
"""
和风天气 QWeather 采集器（商业 API，优先级 5，可选源）。

用途：提供生活指数补充与官方源失败时的降级备份。
     **不是主数据源** —— 主数据源为中国气象局系统的 weather.com.cn / nmc.cn。

⚠️⚠️ 本模块的验证状态声明（重要，请勿误读）：
  由于尚未获得用户的 API Key 与专属 API Host，本模块**无法实测验证**。
  字段名与端点路径依据和风天气官方文档
  （https://dev.qweather.com/docs/api/ ，2026-10-03 实测文档站可访问）编写，
  但「文档描述」不等于「实测确认」。每个解析函数都已做防御性处理：
  字段缺失/类型异常时返回 None 并记日志，绝不因结构不符而崩溃或编造数值。
  拿到 Key 后请先运行 `python -m src.crawler.qweather --selftest` 校验实际返回结构。

⚠️ 实测已确认的两件事（2026-10-03）：
  1. 旧共享域名已不可用：裸调 https://devapi.qweather.com/v7/weather/3d 返回 HTTP 403，
     https://geoapi.qweather.com/v2/city/lookup 返回 HTTP 404。
     ⇒ 必须使用账号专属 API Host（形如 xxxxxx.qweatherapi.com）。
  2. 官方鉴权支持两种方式：API Key 或 JWT。
     来源：https://dev.qweather.com/docs/configuration/authentication/

⚠️ 合规：和风天气免费版数据许可用于非商业开发测试，不得声明为自有预报。
     使用其生活指数时同样需在文案中注明数据来源。
     具体额度与商用条款需用户登录控制台确认（本轮未核实）。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import AppConfig, LocationConfig
from .http_client import FetchResult, PoliteHttpClient
from .parsers import clean_text, norm_date8, norm_iso_hour, to_float

log = logging.getLogger(__name__)

SOURCE = "qweather"

# 官方文档中的端点路径（挂在专属 Host 下）
EP_NOW = "/v7/weather/now"
EP_3D = "/v7/weather/3d"
EP_7D = "/v7/weather/7d"
EP_24H = "/v7/weather/24h"
EP_INDICES_1D = "/v7/indices/1d"
EP_WARNING_NOW = "/v7/warning/now"
EP_GEO_LOOKUP = "/geo/v2/city/lookup"

# 成功响应码。文档称 "200" 为成功，其余为各类错误。
OK_CODES = {"200"}


@dataclass
class QWeatherSnapshot:
    """和风天气采集结果。"""
    source: str = SOURCE
    collected_at: str = ""
    enabled: bool = False
    realtime: Optional[Dict[str, Any]] = None
    daily: List[Dict[str, Any]] = field(default_factory=list)
    hourly: List[Dict[str, Any]] = field(default_factory=list)
    indices: List[Dict[str, Any]] = field(default_factory=list)
    alarms: List[Dict[str, Any]] = field(default_factory=list)
    publish_time: Optional[str] = None
    location_name: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    raw_payload: Optional[Dict[str, Any]] = None
    raw_sha256: str = ""
    raw_file: Optional[str] = None
    ok: bool = False


class QWeatherCrawler:
    """和风天气采集器。未配置凭证时 enabled=False，所有方法优雅返回空结果。"""

    def __init__(self, client: PoliteHttpClient, location: LocationConfig,
                 api_host: str = "", api_key: str = "",
                 jwt_token: str = ""):
        self.client = client
        self.location = location
        self.api_host = (api_host or "").rstrip("/")
        self.api_key = api_key or ""
        self.jwt_token = jwt_token or ""

    # ------------------------------------------------------------
    # 可用性
    # ------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """凭证是否齐备。缺 Host / 缺 Key 或缺 LocationID 都算未启用。"""
        if not self.api_host:
            return False
        if not (self.api_key or self.jwt_token):
            return False
        return bool(self.location.qweather_location_id)

    def disabled_reason(self) -> str:
        if not self.api_host:
            return "未配置 QWEATHER_API_HOST（新版为账号专属域名）"
        if not (self.api_key or self.jwt_token):
            return "未配置 QWEATHER_API_KEY"
        if not self.location.qweather_location_id:
            return ("未配置 QWEATHER_LOCATION_ID —— 东昌府区的 LocationID 尚未核实，"
                    "需用 GeoAPI 查询后填入 config.ini，详见本模块 docstring")
        return ""

    @classmethod
    def from_config(cls, client: PoliteHttpClient, cfg: AppConfig) -> "QWeatherCrawler":
        return cls(client, cfg.location,
                   api_host=cfg.qweather_api_host,
                   api_key=cfg.qweather_api_key)

    # ------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.api_host}{path}"

    def _auth_headers(self) -> Dict[str, str]:
        """构造鉴权头。

        官方文档：API Key 用 X-QW-Api-Key 头，JWT 用 Authorization: Bearer。
        """
        if self.jwt_token:
            return {"Authorization": f"Bearer {self.jwt_token}"}
        return {"X-QW-Api-Key": self.api_key}

    def _request(self, path: str, params: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """发起一次带鉴权的请求，返回已校验 code 的 JSON 对象。"""
        if not self.api_host or not (self.api_key or self.jwt_token):
            log.debug("和风天气未配置，跳过 %s", path)
            return None
        full = self._url(path)
        res = self.client.get(full, params=params,
                              referer="https://dev.qweather.com/")
        if not res.ok:
            log.warning("和风天气 %s 请求失败：%s", path, res.error)
            return None
        obj = self._loads(res.text)
        if obj is None:
            log.warning("和风天气 %s 返回非 JSON（可能被网关拦截）", path)
            return None
        code = clean_text(obj.get("code"))
        if code not in OK_CODES:
            log.warning("和风天气 %s 返回错误码 code=%s（%s）", path, code,
                        self._explain_code(code))
            return None
        return obj

    @staticmethod
    def _loads(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        # 和风天气可能返回 gzip 压缩后的 JSON；requests 会自动解压，
        # 但若拿到二进制串则此处会解析失败，交由上层降级。
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def _explain_code(code: Optional[str]) -> str:
        """官方文档给出的常见错误码释义，便于排障（非猜测，属公开文档）。"""
        table = {
            "400": "请求错误，参数可能有误",
            "401": "认证失败，Key/JWT 无效或过期",
            "402": "超出访问额度或余额不足",
            "403": "无访问权限，可能是免费订阅不支持该接口",
            "404": "查询的数据或地区不存在",
            "429": "超出限流次数，请降低访问频率",
            "500": "服务端错误",
        }
        return table.get(code or "", "未知错误码，请查阅官方文档")

    # ------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------
    def collect(self) -> QWeatherSnapshot:
        snap = QWeatherSnapshot(
            collected_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        if not self.enabled:
            reason = self.disabled_reason()
            snap.warnings.append(f"和风天气未启用：{reason}")
            snap.enabled = False
            snap.ok = False
            log.info("和风天气跳过（%s）", reason)
            return snap

        snap.enabled = True
        loc = self.location.qweather_location_id
        raw: Dict[str, Any] = {}

        now_obj = self._request(EP_NOW, {"location": loc})
        if now_obj:
            raw["now"] = now_obj.get("now")
            snap.realtime = self._parse_now(now_obj)
            snap.publish_time = self._extract_time(now_obj)
            snap.location_name = self._extract_location_name(now_obj)

        day_obj = self._request(EP_3D, {"location": loc})
        if day_obj:
            raw["daily"] = day_obj.get("daily")
            snap.daily = self._parse_daily(day_obj)
            if not snap.publish_time:
                snap.publish_time = self._extract_time(day_obj)

        hour_obj = self._request(EP_24H, {"location": loc})
        if hour_obj:
            raw["hourly"] = hour_obj.get("hourly")
            snap.hourly = self._parse_hourly(hour_obj)

        # 生活指数：type=0 表示全部指数
        idx_obj = self._request(EP_INDICES_1D, {"location": loc, "type": "0"})
        if idx_obj:
            raw["indices"] = idx_obj.get("daily")
            snap.indices = self._parse_indices(idx_obj)

        warn_obj = self._request(EP_WARNING_NOW, {"location": loc})
        if warn_obj:
            raw["warning"] = warn_obj.get("warning")
            snap.alarms = self._parse_warnings(warn_obj)

        snap.raw_payload = raw
        snap.ok = bool(snap.realtime or snap.daily)
        if not snap.ok:
            snap.warnings.append("和风天气已配置但所有接口均未取到有效数据，"
                                 "请检查 LocationID 是否正确、免费订阅是否覆盖该接口")
        log.info("和风天气采集：实时=%s 多日=%d 逐时=%d 指数=%d 预警=%d",
                 bool(snap.realtime), len(snap.daily), len(snap.hourly),
                 len(snap.indices), len(snap.alarms))
        return snap

    # ------------------------------------------------------------
    # LocationID 查询（帮助用户补齐 config.ini 的待填项）
    # ------------------------------------------------------------
    def lookup_location(self, keyword: str = "东昌府",
                        adm: str = "聊城") -> List[Dict[str, Any]]:
        """用 GeoAPI 查询 LocationID。

        ⚠️ 这是补齐 config.ini 中 qweather_location_id 的**唯一正确途径**。
           不要凭空猜测或从二手博客抄一个 ID —— 填错会导致每天推送别的城市天气。

        用法：
            python -m src.crawler.qweather --lookup 东昌府 --adm 聊城
        """
        if not self.api_host or not (self.api_key or self.jwt_token):
            log.error("无法查询：请先在 .env 中配置 QWEATHER_API_HOST 与 QWEATHER_API_KEY")
            return []
        obj = self._request(EP_GEO_LOOKUP, {
            "location": keyword, "adm": adm, "range": "cn",
            "number": "10",
        })
        if not obj:
            return []
        items = obj.get("location")
        if not isinstance(items, list):
            log.warning("GeoAPI 返回中无 location 数组")
            return []
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            out.append({
                "id": clean_text(it.get("id")),
                "name": clean_text(it.get("name")),
                "adm1": clean_text(it.get("adm1")),
                "adm2": clean_text(it.get("adm2")),
                "lat": to_float(it.get("lat")),
                "lon": to_float(it.get("lon")),
                "tz": clean_text(it.get("tz")),
                "utcOffset": clean_text(it.get("utcOffset")),
            })
        return out

    # ------------------------------------------------------------
    # 解析（防御式，字段缺失一律返回 None）
    # ------------------------------------------------------------
    @staticmethod
    def _extract_time(obj: Dict[str, Any]) -> Optional[str]:
        """提取官方观测/发布时间。文档字段为 obsTime / fxTime，ISO8601 带时区。"""
        for k in ("obsTime", "fxTime", "updateTime", "pubTime"):
            v = clean_text(obj.get(k))
            if v:
                # 统一为 "YYYY-MM-DD HH:MM"
                return v.replace("T", " ")[:16]
            node = obj.get("now") if isinstance(obj.get("now"), dict) else None
            if node:
                v = clean_text(node.get("obsTime"))
                if v:
                    return v.replace("T", " ")[:16]
        return None

    @staticmethod
    def _extract_location_name(obj: Dict[str, Any]) -> Optional[str]:
        for k in ("name", "city", "location"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict):
                n = clean_text(v.get("name"))
                if n:
                    return n
        return None

    def _parse_now(self, obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """实时天气。文档字段：obsTime/temp/feelsLike/icon/text/
        wind360/windDir/windScale/windSpeed/humidity/precip/pressure/vis/cloud/dew。
        ⚠️ 未实测，字段缺失即返回 None。"""
        now = obj.get("now")
        if not isinstance(now, dict):
            log.warning("和风天气 now 字段缺失或非对象")
            return None
        return {
            "source": SOURCE,
            "city_id": self.location.qweather_location_id,
            "observed_at": self._extract_time(obj) or clean_text(now.get("obsTime")),
            "temp": to_float(now.get("temp")),
            "feels_like": to_float(now.get("feelsLike")),
            "humidity": to_float(now.get("humidity")),
            "wind_dir": clean_text(now.get("windDir")),
            "wind_scale": clean_text(now.get("windScale")),
            "wind_speed": to_float(now.get("windSpeed")),
            "wind_deg": to_float(now.get("wind360")),
            "pressure": to_float(now.get("pressure")),
            "visibility": to_float(now.get("vis")),
            "rain_1h": to_float(now.get("precip")),
            "rain_24h": None,
            "aqi": None,   # AQI 属 /v7/air/now 接口，本项目不额外请求
            "text": clean_text(now.get("text")),
            "icon": clean_text(now.get("icon")),
            "cloud": to_float(now.get("cloud")),
            "dew": to_float(now.get("dew")),
        }

    def _parse_daily(self, obj: Dict[str, Any]) -> List[Dict[str, Any]]:
        """逐日预报。文档字段：fxDate/sunrise/sunset/tempMax/tempMin/
        textDay/textNight/iconDay/iconNight/windDirDay/windScaleDay/
        windSpeedDay/humidity/precip/pressure/vis/cloud/uvIndex。"""
        items = obj.get("daily")
        if not isinstance(items, list):
            log.warning("和风天气 daily 字段缺失或非数组")
            return []
        out: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            fdate = norm_date8(it.get("fxDate")) or clean_text(it.get("fxDate"))
            if not fdate:
                continue
            out.append({
                "source": SOURCE,
                "city_id": self.location.qweather_location_id,
                "station_id": None,
                "forecast_date": fdate,
                "temp_max": to_float(it.get("tempMax")),
                "temp_min": to_float(it.get("tempMin")),
                "text_day": clean_text(it.get("textDay")),
                "text_night": clean_text(it.get("textNight")),
                "wind_dir_day": clean_text(it.get("windDirDay")),
                "wind_scale_day": clean_text(it.get("windScaleDay")),
                "wind_dir_night": clean_text(it.get("windDirNight")),
                "wind_scale_night": clean_text(it.get("windScaleNight")),
                "sunrise": clean_text(it.get("sunrise")),
                "sunset": clean_text(it.get("sunset")),
                "uv_index": to_float(it.get("uvIndex")),
                "humidity": to_float(it.get("humidity")),
                "precip": to_float(it.get("precip")),
                "publish_time": self._extract_time(obj),
                "is_observation": 0,
            })
        return out

    def _parse_hourly(self, obj: Dict[str, Any]) -> List[Dict[str, Any]]:
        """逐小时预报。文档字段：fxTime/temp/icon/text/wind360/windDir/
        windScale/windSpeed/humidity/precip/pop/pressure/cloud/dew。"""
        items = obj.get("hourly")
        if not isinstance(items, list):
            return []
        out: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            ts = norm_iso_hour(it.get("fxTime")) or clean_text(it.get("fxTime"))
            if not ts:
                continue
            out.append({
                "source": SOURCE,
                "city_id": self.location.qweather_location_id,
                "hour_ts": ts,
                "temp": to_float(it.get("temp")),
                "humidity": to_float(it.get("humidity")),
                "text": clean_text(it.get("text")),
                "wind_dir": clean_text(it.get("windDir")),
                "wind_scale": clean_text(it.get("windScale")),
                "wind_speed": to_float(it.get("windSpeed")),
                "wind_deg": to_float(it.get("wind360")),
                "pressure": to_float(it.get("pressure")),
                "visibility": None,
                "rain": to_float(it.get("precip")),
                "pop": to_float(it.get("pop")),   # 降水概率
            })
        return out

    def _parse_indices(self, obj: Dict[str, Any]) -> List[Dict[str, Any]]:
        """生活指数。文档字段：date/type/name/level/category/text/icon。"""
        items = obj.get("daily")
        if not isinstance(items, list):
            return []
        out: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = clean_text(it.get("name"))
            if not name:
                continue
            out.append({
                "source": SOURCE,
                "city_id": self.location.qweather_location_id,
                "index_date": norm_date8(it.get("date")) or clean_text(it.get("date")),
                "code": clean_text(it.get("type")) or "",
                "name": name,
                "level": clean_text(it.get("category")) or clean_text(it.get("level")),
                "description": clean_text(it.get("text")),
                "publish_time": self._extract_time(obj),
            })
        return out

    def _parse_warnings(self, obj: Dict[str, Any]) -> List[Dict[str, Any]]:
        """灾害预警。文档字段：id/pubTime/title/sender/text/typeName/type/
        severityColor/severityLevel/status/level。
        ⚠️ 预警关系人身安全：解析不到标题就丢弃该条，绝不编造。"""
        items = obj.get("warning")
        if not isinstance(items, list):
            return []
        out: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            title = clean_text(it.get("title"))
            if not title:
                log.warning("和风天气预警记录缺少 title，丢弃（键：%s）", list(it.keys()))
                continue
            out.append({
                "source": SOURCE,
                "alarm_id": clean_text(it.get("id")) or title,
                "city_id": self.location.qweather_location_id,
                "title": title,
                "level": clean_text(it.get("severityColor"))
                         or clean_text(it.get("level")),
                "signal_type": clean_text(it.get("typeName"))
                               or clean_text(it.get("type")),
                "issue_time": clean_text(it.get("pubTime")),
                "url": clean_text(it.get("sender")),
                "detail": clean_text(it.get("text")),
            })
        return out


# ============================================================
# 命令行自检：拿到 Key 后先跑这个，确认实际返回结构再投入使用
# ============================================================
def _selftest(cfg: AppConfig, keyword: str, adm: str) -> int:
    client = PoliteHttpClient(cfg.crawler)
    qw = QWeatherCrawler.from_config(client, cfg)

    print("=" * 62)
    print("和风天气模块自检")
    print("=" * 62)
    print(f"API Host     : {cfg.qweather_api_host or '<未配置>'}")
    print(f"API Key      : {'已配置(已脱敏)' if cfg.qweather_api_key else '<未配置>'}")
    print(f"LocationID   : {cfg.location.qweather_location_id or '<未配置>'}")
    print(f"模块启用状态 : {'✅ 已启用' if qw.enabled else '❌ 未启用'}")
    if not qw.enabled:
        print(f"未启用原因   : {qw.disabled_reason()}")

    print("\n--- 1) 尝试用 GeoAPI 查询 LocationID ---")
    if cfg.qweather_api_host and cfg.qweather_api_key:
        hits = qw.lookup_location(keyword, adm)
        if hits:
            for h in hits:
                print(f"  id={h['id']}  name={h['name']}  "
                      f"adm1={h['adm1']}  adm2={h['adm2']}  "
                      f"经纬度={h['lat']},{h['lon']}")
            print("\n👉 请把上面「东昌府区」对应的 id 填入 config.ini 的 "
                  "[location] qweather_location_id")
        else:
            print("  查询无结果。请检查 API Host / Key 是否正确、"
                  "免费订阅是否包含 GeoAPI 权限。")
    else:
        print("  跳过（未配置 Host / Key）")

    print("\n--- 2) 尝试采集一次完整数据 ---")
    if qw.enabled:
        snap = qw.collect()
        print(f"  ok={snap.ok}  实时={'有' if snap.realtime else '无'}  "
              f"多日={len(snap.daily)}  逐时={len(snap.hourly)}  "
              f"指数={len(snap.indices)}  预警={len(snap.alarms)}")
        if snap.realtime:
            print(f"  实时样例: {json.dumps(snap.realtime, ensure_ascii=False)}")
        if snap.daily:
            print(f"  首日样例: {json.dumps(snap.daily[0], ensure_ascii=False)}")
        for w in snap.warnings:
            print(f"  ⚠️ {w}")
    else:
        print("  跳过（模块未启用）")

    client.close()
    print("\n说明：本模块因缺少 API Key 尚未经过实测验证。")
    print("     自检通过后，如实际字段与解析结果不符，请对照官方文档修正解析函数。")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    from ..config import load_config
    parser = argparse.ArgumentParser(
        description="和风天气模块自检与 LocationID 查询")
    parser.add_argument("--lookup", default="东昌府",
                        help="GeoAPI 查询关键词，默认「东昌府」")
    parser.add_argument("--adm", default="聊城",
                        help="上级行政区，默认「聊城」")
    parser.add_argument("--selftest", action="store_true",
                        help="运行自检（本模块的唯一入口行为，加不加均执行）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = load_config()
    return _selftest(cfg, args.lookup, args.adm)


if __name__ == "__main__":
    sys.exit(main())
