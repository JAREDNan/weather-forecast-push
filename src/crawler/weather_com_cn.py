# -*- coding: utf-8 -*-
"""
中国天气网采集器（weather.com.cn / d1.weather.com.cn）。

主办方：中国气象局公共气象服务中心 —— 属《气象法》第二十五条所指
「气象主管机构所属的气象台站提供」的合法信息来源。
robots.txt 实测为 `User-agent: * / Allow: /`，抓取其数据接口不违反 robots。

本模块使用的接口均经 2026-10-02/03 实测验证（详见 reports/02_爬虫合规检查.md）：

  ✅ http://d1.weather.com.cn/sk_2d/{city}.html
       返回 var dataSK   —— 实时观测（温/湿/风/压/能见度/AQI）
  ✅ http://d1.weather.com.cn/dingzhi/{city}.html
       返回 var cityDZ + alarmDZ —— 今日预报 + 本地预警（含官方发布时间 fctime）
  ✅ http://d1.weather.com.cn/weather_index/{city}.html
       返回 var cityDZ + alarmDZ + dataSK + dataZS + fc —— 一次请求聚合全部
       其中 fc.f[] 为未来 5 天预报，**与官方 7 天预报页面逐日完全吻合**
       dataZS.zs 为 32 项生活指数中文原文
  ✅ http://d1.weather.com.cn/wap_40d/{city}.html
       返回 var fc40 + index3d + fc1h_24 + cwx
       其中 fc1h_24.jh[] 为未来 48 小时逐小时预报
       index3d.i[] 为 32 项指数的未来 3 日等级与描述
       ⚠️ fc40 与官方 7 天页面数据矛盾（属较早一次发布），本项目不使用

关键工程约束：
  * 风向、风力、天气现象在主链路一律取用接口返回的**官方中文文本**
    （fc.f[].fe/ff/fg/fh、cityDZ.weatherinfo.wd/ws/weather），
    不做编码换算，从根本上避免编码表不全导致推送错误天气。
  * 仅 fc1h_24 逐小时数据是纯编码，才走 weather_codes 映射；
    映射未命中时返回 None 并记日志，绝不猜测。
  * "999" 是官方哨兵值（表示无数据），由 parsers.to_float 统一拦截。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import LocationConfig
from .http_client import FetchResult, PoliteHttpClient
from .parsers import (
    clean_text,
    extract_js_var,
    is_soft_404,
    norm_date8,
    norm_datetime,
    norm_iso_hour,
    split_turn_text,
    to_float,
)
from .weather_codes import weather_text_from_code, wind_dir_from_degree

log = logging.getLogger(__name__)

SOURCE = "weather_com_cn"
BASE = "http://d1.weather.com.cn"
# 接口需要 Referer 才稳定返回数据
REFERER_TPL = "http://www.weather.com.cn/weather/{city}.shtml"
ROBOTS_URL = "https://www.weather.com.cn/robots.txt"


@dataclass
class WccSnapshot:
    """一次采集的完整结果。

    各字段独立可缺省：任一子接口失效不影响其余数据落库。
    """
    city_id: str
    source: str = SOURCE
    collected_at: str = ""
    # 实时观测
    realtime: Optional[Dict[str, Any]] = None
    # 今日预报（官方原文）
    today: Optional[Dict[str, Any]] = None
    # 未来多日预报（官方原文）
    daily: List[Dict[str, Any]] = field(default_factory=list)
    # 逐小时预报
    hourly: List[Dict[str, Any]] = field(default_factory=list)
    # 生活指数（中文原文）
    indices: List[Dict[str, Any]] = field(default_factory=list)
    # 预警
    alarms: List[Dict[str, Any]] = field(default_factory=list)
    # 官方发布时间（《气象法》要求标注）
    publish_time: Optional[str] = None
    # 诊断信息
    warnings: List[str] = field(default_factory=list)
    # 原始报文（用于计算 hash 与归档）
    raw_payload: Optional[Dict[str, Any]] = None
    raw_sha256: str = ""
    raw_file: Optional[str] = None
    ok: bool = False

    @property
    def has_content(self) -> bool:
        return bool(self.realtime or self.today or self.daily)


class WeatherComCnCrawler:
    """中国天气网采集器。"""

    def __init__(self, client: PoliteHttpClient, location: LocationConfig):
        self.client = client
        self.location = location

    # ------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------
    def collect(self) -> WccSnapshot:
        """采集实时 + 预报 + 指数 + 预警。

        优先用 weather_index 一次拿全（省请求数、更礼貌）；
        若 weather_index 缺失某些部分，再用 dingzhi / sk_2d 补齐。
        """
        snap = WccSnapshot(city_id=self.location.wcc_city_id,
                           collected_at=datetime.now().astimezone().isoformat(
                               timespec="seconds"))
        referer = REFERER_TPL.format(city=self.location.wcc_city_id)

        # ---------- 1) 聚合接口 ----------
        agg_raw: Dict[str, Any] = {}
        res = self.client.get(
            f"{BASE}/weather_index/{self.location.wcc_city_id}.html",
            referer=referer,
        )
        if self._usable(res, "weather_index"):
            agg_raw = self._collect_js_vars(res.text, [
                "cityDZ", "alarmDZ", "dataSK", "dataZS", "fc",
            ])
        else:
            snap.warnings.append("weather_index 不可用，改用分接口补齐")

        # ---------- 2) 聚合缺失则补齐 ----------
        if "dataSK" not in agg_raw:
            r2 = self.client.get(
                f"{BASE}/sk_2d/{self.location.wcc_city_id}.html",
                referer=referer,
            )
            if self._usable(r2, "sk_2d"):
                agg_raw.update(self._collect_js_vars(r2.text, ["dataSK"]))
            else:
                snap.warnings.append("sk_2d 实时观测不可用")

        if "cityDZ" not in agg_raw:
            r3 = self.client.get(
                f"{BASE}/dingzhi/{self.location.wcc_city_id}.html",
                referer=referer,
            )
            if self._usable(r3, "dingzhi"):
                agg_raw.update(
                    self._collect_js_vars(r3.text, ["cityDZ", "alarmDZ"]))
            else:
                snap.warnings.append("dingzhi 定制预报不可用")

        # ---------- 3) 逐小时（独立接口，失败不影响主链路）----------
        r4 = self.client.get(
            f"{BASE}/wap_40d/{self.location.wcc_city_id}.html",
            referer=referer,
        )
        extra: Dict[str, Any] = {}
        if self._usable(r4, "wap_40d"):
            extra = self._collect_js_vars(r4.text, ["fc1h_24", "index3d"])
        else:
            snap.warnings.append("wap_40d 不可用，逐小时预报缺失")

        # ---------- 4) 解析 ----------
        raw_all = dict(agg_raw)
        raw_all.update(extra)
        snap.raw_payload = raw_all

        snap.realtime = self._parse_dataSK(agg_raw.get("dataSK"))
        today_res = self._parse_cityDZ(agg_raw.get("cityDZ"))
        snap.today, snap.publish_time = today_res
        snap.daily = self._parse_fc(agg_raw.get("fc"), snap.publish_time)
        snap.hourly = self._parse_hourly(extra.get("fc1h_24"))
        snap.indices = self._parse_indices(
            agg_raw.get("dataZS"), extra.get("index3d"), snap.publish_time)
        snap.alarms = self._parse_alarms(agg_raw.get("alarmDZ"))

        # 🔴 今日气温必须以 fc.f[0] 为准（已与官方「今天」页面逐字验证一致）；
        #    cityDZ.temp 语义不明（接近实时气温），不得用作当日最高气温。
        self._merge_today_from_fc(snap)

        if not snap.daily and today_res[0]:
            # fc 缺失时用今日预报兜一条，保证文案不至于完全空白
            snap.daily = [dict(today_res[0], forecast_date=(
                norm_date8(snap.publish_time)
                or datetime.now().strftime("%Y-%m-%d")))]

        snap.ok = snap.has_content
        if snap.ok:
            log.info("中国天气网采集成功：实时=%s 今日=%s 多日=%d 逐时=%d 指数=%d 预警=%d",
                     bool(snap.realtime), bool(snap.today), len(snap.daily),
                     len(snap.hourly), len(snap.indices), len(snap.alarms))
        else:
            snap.warnings.append("中国天气网全部接口未取到有效数据")
            log.error("中国天气网采集失败：%s", snap.warnings)
        return snap

    # ------------------------------------------------------------
    # 内部：请求与变量提取
    # ------------------------------------------------------------
    @staticmethod
    def _usable(res: FetchResult, label: str) -> bool:
        if not res.ok:
            log.warning("接口 %s 请求失败：%s", label, res.error)
            return False
        if is_soft_404(res.text):
            log.warning("接口 %s 返回软 404（源站已下线该接口）", label)
            return False
        return True

    @staticmethod
    def _collect_js_vars(text: str, names: List[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for n in names:
            v = extract_js_var(text, n)
            if v is not None:
                out[n] = v
        return out

    # ------------------------------------------------------------
    # 内部：各数据结构解析
    # ------------------------------------------------------------
    def _parse_dataSK(self, sk: Any) -> Optional[Dict[str, Any]]:
        """实时观测。

        实测字段：temp/tempf/WD(风向中文)/WS(风力中文)/SD,sd(湿度)/qy(气压)
                 njd(能见度)/time(观测时刻)/rain,rain24h/aqi/aqi_pm25
                 weather(天气现象中文)/date(如「10月02日(星期五)」)
        风向风力直接用官方中文，不做编码换算。
        """
        if not isinstance(sk, dict):
            return None
        observed = clean_text(sk.get("time"))
        date_txt = clean_text(sk.get("date"))
        return {
            "source": SOURCE,
            "city_id": clean_text(sk.get("city")) or self.location.wcc_city_id,
            "observed_at": observed,
            "observed_date_text": date_txt,
            "temp": to_float(sk.get("temp")),
            "temp_f": to_float(sk.get("tempf")),
            "humidity": to_float(sk.get("SD") or sk.get("sd")),
            "wind_dir": clean_text(sk.get("WD")),
            "wind_scale": clean_text(sk.get("WS")),
            "wind_speed_kmh": to_float(sk.get("wse")),
            "pressure": to_float(sk.get("qy")),
            "visibility": to_float(sk.get("njd")),
            "rain_1h": to_float(sk.get("rain")),
            "rain_24h": to_float(sk.get("rain24h")),
            "aqi": to_float(sk.get("aqi")),
            "aqi_pm25": to_float(sk.get("aqi_pm25")),
            "text": clean_text(sk.get("weather")),
            "text_en": clean_text(sk.get("weathere")),
            "city_name": clean_text(sk.get("cityname")),
        }

    def _parse_cityDZ(self, dz: Any):
        """今日「定制」区块 —— 仅用于取发布时间与天气现象文本，**不取气温**。

        实测结构：{"weatherinfo": {"city","cityname","temp","tempn",
                   "weather"(如「多云转阴」),"wd"(如「南风转北风」),
                   "ws"(如「4-5级转3-4级」),"weathercode","weathercoden","fctime"}}

        🔴 重要实测发现（2026-10-03，务必勿改回）：
           cityDZ.temp **不是当日最高气温**。当日实测三方比对：
             cityDZ.temp / tempn        = 17 / 12
             fc.f[0].fc / fd            = 23 / 12
             官方「今天」页面           = 3日白天 多云 23°C ／ 3日夜间 阴 12°C
             dataSK.temp（实时观测）    = 18.3（08:45）
           ⇒ fc.f[0] 与官方页面**完全一致**，cityDZ.temp 则接近实时气温，
             语义不明（可能是「当前时刻预报值」或较早一次发布的残留值；
             前一日实测该字段曾为哨兵值 "999"，进一步说明它不可靠）。
           若把 cityDZ.temp 当最高气温，会把 23℃ 错报成 17℃，偏差 6℃，
           属实质性播报错误。
           ⇒ 因此本函数**不再从 cityDZ 取 temp/tempn**，
             今日气温一律以 fc.f[] 为准（见 _parse_fc 与 collect）。
             cityDZ 仅保留：发布时间 fctime（《气象法》要求标注）、
             天气现象文本与风向风力（这三项实测与官方页面一致，用于交叉校验）。

        ⚠️ temp 若为哨兵 "999" 也会被 to_float 拦截，不会污染数据。
        """
        if not isinstance(dz, dict):
            return None, None
        wi = dz.get("weatherinfo")
        if not isinstance(wi, dict):
            return None, None

        publish = norm_datetime(wi.get("fctime"))
        weather_txt = clean_text(wi.get("weather"))
        parts = split_turn_text(weather_txt)
        wind_parts = split_turn_text(clean_text(wi.get("wd")))
        scale_parts = split_turn_text(clean_text(wi.get("ws")))
        pdate = norm_date8(wi.get("fctime")) or datetime.now().strftime("%Y-%m-%d")

        # cityDZ 的气温仅作诊断留档，绝不写入 temp_max/temp_min
        dz_temp = to_float(wi.get("temp"))
        dz_tempn = to_float(wi.get("tempn"))

        rec = {
            "source": SOURCE,
            "city_id": self.location.wcc_city_id,
            "station_id": self.location.station_id,
            "forecast_date": pdate,
            # 🔴 刻意留空：今日气温由 fc.f[0] 提供（已与官方页面验证一致）
            "temp_max": None,
            "temp_min": None,
            "text_day": parts[0] if len(parts) >= 1 else None,
            "text_night": parts[1] if len(parts) >= 2 else (parts[0] if parts else None),
            "text_full": weather_txt,
            "wind_dir_day": wind_parts[0] if wind_parts else None,
            "wind_dir_night": wind_parts[1] if len(wind_parts) >= 2 else (
                wind_parts[0] if wind_parts else None),
            "wind_scale_day": scale_parts[0] if scale_parts else None,
            "wind_scale_night": scale_parts[1] if len(scale_parts) >= 2 else (
                scale_parts[0] if scale_parts else None),
            "wind_dir_full": clean_text(wi.get("wd")),
            "wind_scale_full": clean_text(wi.get("ws")),
            "publish_time": publish,
            "is_observation": 0,
            "city_name": clean_text(wi.get("city")),
            # 诊断字段（不入 weather_daily，仅供交叉校验与排障）
            "_dz_temp_diag": dz_temp,
            "_dz_tempn_diag": dz_tempn,
        }
        if not rec["text_full"] and dz_temp is None and dz_tempn is None:
            log.warning("cityDZ 天气现象与气温均为空（疑似哨兵值 999），丢弃该记录")
            return None, publish
        return rec, publish

    def _merge_today_from_fc(self, snap: "WccSnapshot") -> None:
        """用 fc.f[0]（已验证与官方页面一致）回填今日气温，并做交叉校验。

        背景（2026-10-03 实测）：
          cityDZ.temp/tempn = 17/12，而官方「今天」页面与 fc.f[0] 均为 23/12。
          cityDZ.temp 语义不明（接近实时观测 18.3℃），若当作当日最高气温，
          会把 23℃ 错报成 17℃，偏差达 6℃ —— 属实质性播报错误。
        故今日气温一律取 fc 数组首日；cityDZ 仅提供发布时间与天气现象文本。
        """
        if not snap.daily:
            if snap.today is None:
                return
            # 无 fc 数据时无法回填，标记警告但不编造数值
            if snap.today.get("temp_max") is None:
                snap.warnings.append(
                    "fc 多日预报缺失，今日气温无法确定（不使用语义不明的 "
                    "cityDZ.temp，避免播报错误气温）")
            return

        first = snap.daily[0]
        if snap.today is None:
            # cityDZ 缺失时，直接以 fc 首日作为今日记录
            snap.today = dict(first)
            log.info("cityDZ 缺失，今日预报改用 fc 首日：%s %s~%s℃",
                     first.get("text_full"), first.get("temp_min"),
                     first.get("temp_max"))
            return

        # ---------- 回填气温（以 fc 为权威） ----------
        fc_max, fc_min = first.get("temp_max"), first.get("temp_min")
        if fc_max is not None:
            snap.today["temp_max"] = fc_max
        if fc_min is not None:
            snap.today["temp_min"] = fc_min

        # ---------- 交叉校验：天气现象文本 ----------
        dz_text = snap.today.get("text_full")
        fc_text = self._join_text(first.get("text_day"), first.get("text_night"))
        if dz_text and fc_text and dz_text != fc_text:
            snap.warnings.append(
                f"⚠️ 交叉校验：cityDZ 与 fc 的天气现象不一致"
                f"（cityDZ={dz_text} / fc={fc_text}），已采用与官方页面一致的 fc 值")
            log.warning(snap.warnings[-1])
            snap.today["text_full"] = fc_text
            snap.today["text_day"] = first.get("text_day")
            snap.today["text_night"] = first.get("text_night")
        elif fc_text and not dz_text:
            snap.today["text_full"] = fc_text
            snap.today["text_day"] = first.get("text_day")
            snap.today["text_night"] = first.get("text_night")

        # ---------- 交叉校验：风向风力 ----------
        for field_name in ("wind_dir_day", "wind_dir_night",
                           "wind_scale_day", "wind_scale_night"):
            if not snap.today.get(field_name) and first.get(field_name):
                snap.today[field_name] = first.get(field_name)
        if not snap.today.get("wind_dir_full") and first.get("wind_dir_day"):
            snap.today["wind_dir_full"] = self._join_text(
                first.get("wind_dir_day"), first.get("wind_dir_night"))
        if not snap.today.get("wind_scale_full") and first.get("wind_scale_day"):
            snap.today["wind_scale_full"] = self._join_text(
                first.get("wind_scale_day"), first.get("wind_scale_night"))

        # ---------- 诊断：记录 cityDZ 气温与 fc 的差异 ----------
        dz_t = snap.today.get("_dz_temp_diag")
        if dz_t is not None and fc_max is not None and abs(dz_t - fc_max) >= 3:
            log.info("诊断：cityDZ.temp=%s 与 fc 当日高温=%s 相差 %.0f℃，"
                     "已按 fc（与官方页面一致）取值", dz_t, fc_max,
                     abs(dz_t - fc_max))

        # 清理诊断字段，不入库
        for k in ("_dz_temp_diag", "_dz_tempn_diag"):
            snap.today.pop(k, None)

        log.info("今日预报已回填（以 fc 为权威）：%s %s~%s℃",
                 snap.today.get("text_full"), snap.today.get("temp_min"),
                 snap.today.get("temp_max"))

    @staticmethod
    def _join_text(day: Optional[str], night: Optional[str]) -> Optional[str]:
        """把白天/夜间文本合成「A转B」形式，与官方页面表述一致。"""
        day = clean_text(day)
        night = clean_text(night)
        if day and night and day != night:
            return f"{day}转{night}"
        return day or night

    def _parse_fc(self, fc: Any, publish_time: Optional[str]) -> List[Dict[str, Any]]:
        """未来多日预报。

        实测 fc.f[] 每项字段（已用聊城官方 7 天预报页面逐日交叉验证一致）：
            fa 白天天气编码   fb 夜间天气编码
            fc 最高气温       fd 最低气温
            fe 白天风向(中文) ff 夜间风向(中文)
            fg 白天风力(中文) fh 夜间风力(中文)
            fi 日期「10/3」   fj 星期「今天」
            fk/fl 紫外线?     fm/fn 湿度?(⚠️ 未验证，不采用)
        ⚠️ 设计红线：风向风力**直接用中文 fe/ff/fg/fh**，不走编码。
           天气现象优先用编码表翻译 fa/fb（已实测验证 0/1/2/4/7/8），
           未命中时置 None 并由上层用页面中文兜底，绝不猜测。
        ⚠️ fk/fl/fm/fn 语义未经验证，本项目不使用，避免把湿度当紫外线播报。
        """
        if not isinstance(fc, dict):
            return []
        items = fc.get("f")
        if not isinstance(items, list):
            return []

        out: List[Dict[str, Any]] = []
        today = datetime.now()
        for idx, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            # 日期：fi 形如 "10/3"，用「今天 + idx」推算更稳（源站按顺序给未来日）
            fdate = self._resolve_fc_date(it.get("fi"), idx, today)
            if not fdate:
                continue

            code_d = clean_text(it.get("fa"))
            code_n = clean_text(it.get("fb"))
            text_d = weather_text_from_code(code_d)
            text_n = weather_text_from_code(code_n)
            if code_d and text_d is None:
                log.warning("遇到未验证的天气编码 fa=%s（日期 %s），置空不猜测",
                            code_d, fdate)
            if code_n and text_n is None:
                log.warning("遇到未验证的天气编码 fb=%s（日期 %s），置空不猜测",
                            code_n, fdate)

            out.append({
                "source": SOURCE,
                "city_id": self.location.wcc_city_id,
                "station_id": self.location.station_id,
                "forecast_date": fdate,
                "temp_max": to_float(it.get("fc")),
                "temp_min": to_float(it.get("fd")),
                "text_day": text_d,
                "text_night": text_n,
                "text_full": self._join_text(text_d, text_n),
                "code_day": code_d,
                "code_night": code_n,
                "wind_dir_day": clean_text(it.get("fe")),
                "wind_dir_night": clean_text(it.get("ff")),
                "wind_scale_day": clean_text(it.get("fg")),
                "wind_scale_night": clean_text(it.get("fh")),
                "weekday_cn": clean_text(it.get("fj")),
                "date_label": clean_text(it.get("fi")),
                "publish_time": publish_time,
                "is_observation": 0,
            })
        log.info("fc 解析出 %d 天预报", len(out))
        return out

    @staticmethod
    def _resolve_fc_date(label: Any, idx: int, today: datetime) -> Optional[str]:
        """把 fi（如 "10/3"）解析为 YYYY-MM-DD，失败时用 今天+idx 兜底。"""
        s = clean_text(label)
        if s and "/" in s:
            try:
                mon, day = s.split("/")[:2]
                mon_i, day_i = int(mon), int(day)
                year = today.year
                cand = datetime(year, mon_i, day_i)
                # 跨年修正：若推出的日期比今天早很多，说明是明年
                if (today - cand).days > 180:
                    cand = datetime(year + 1, mon_i, day_i)
                return cand.strftime("%Y-%m-%d")
            except (ValueError, IndexError):
                pass
        # 兜底：源站按顺序返回从今天起的连续日期
        from datetime import timedelta
        return (today + timedelta(days=idx)).strftime("%Y-%m-%d")

    def _parse_hourly(self, h24: Any) -> List[Dict[str, Any]]:
        """逐小时预报。

        实测 fc1h_24.jh[] 字段：
            ja 天气编码   jb 气温(℃)   jc ?(⚠️未验证)   jd 风力编码(⚠️未验证)
            je 相对湿度(%) jf 时间 YYYYMMDDHHMM
            jg 降水(mm)   jh 体感/风速?(⚠️未验证)  ji 风向角度(度)
            jj 气压(hPa)  jk 能见度(km)
        ⚠️ 严格纪律：只采用已实测确认语义的字段
           jb(气温)/je(湿度)/jf(时间)/ji(风向角度)/jj(气压)/jk(能见度)/jg(降水)。
           jc/jd/jh 语义未验证 —— 一律不入库、不展示，避免播报错误信息。
           风向由 ji 几何换算为 16 方位中文（纯数学，非猜测）。
        """
        if not isinstance(h24, dict):
            return []
        rows = h24.get("jh")
        if not isinstance(rows, list):
            return []

        out: List[Dict[str, Any]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            ts = norm_iso_hour(r.get("jf"))
            if not ts:
                continue
            deg = to_float(r.get("ji"))
            code = clean_text(r.get("ja"))
            text = weather_text_from_code(code)
            if code and text is None:
                log.debug("逐小时未验证天气编码 ja=%s @ %s，置空", code, ts)
            out.append({
                "source": SOURCE,
                "city_id": self.location.wcc_city_id,
                "hour_ts": ts,
                "temp": to_float(r.get("jb")),
                "humidity": to_float(r.get("je")),
                "text": text,
                "weather_code": code,
                "wind_dir": wind_dir_from_degree(deg),
                "wind_scale": None,      # jd 语义未验证，不猜测
                "wind_speed": None,      # jh 语义未验证，不猜测
                "wind_deg": deg,
                "pressure": to_float(r.get("jj")),
                "visibility": to_float(r.get("jk")),
                "rain": to_float(r.get("jg")),
            })
        log.info("逐小时预报解析出 %d 条", len(out))
        return out

    def _parse_indices(self, zs: Any, idx3d: Any,
                       publish_time: Optional[str]) -> List[Dict[str, Any]]:
        """生活指数（官方中文原文）。

        两个来源：
          dataZS.zs —— 实测 32 项，每项 <code>_name/<code>_hint/<code>_des_s
                       （名称/等级/简述），为当日指数
          index3d.i[] —— 实测 32 项，每项 i1=code i2=名称
                        i4/i6/i8=今/明/后日等级，i5/i7/i9=对应描述
        ⚠️ 合规要求：指数描述为官方原文，直接引用不改写
           （《气象预报发布与传播管理办法》第九条：不得自行更改内容和结论）。
        """
        out: List[Dict[str, Any]] = []
        seen = set()

        # ---- 来源 1：dataZS（当日）----
        if isinstance(zs, dict):
            inner = zs.get("zs")
            if isinstance(inner, dict):
                idx_date = norm_date8(inner.get("date")) \
                    or datetime.now().strftime("%Y-%m-%d")
                for code in sorted(k for k in inner if k.endswith("_name")):
                    c = code[:-len("_name")]
                    name = clean_text(inner.get(code))
                    level = clean_text(inner.get(f"{c}_hint"))
                    desc = clean_text(inner.get(f"{c}_des_s"))
                    if not name:
                        continue
                    key = (idx_date, c)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "source": SOURCE,
                        "city_id": self.location.wcc_city_id,
                        "index_date": idx_date,
                        "code": c, "name": name,
                        "level": level, "description": desc,
                        "publish_time": publish_time,
                    })

        # ---- 来源 2：index3d（今/明/后日，补齐缺失日期）----
        if isinstance(idx3d, dict):
            items = idx3d.get("i")
            base_pub = norm_datetime(idx3d.get("i0")) or publish_time
            base_date = norm_date8(idx3d.get("i0")) \
                or datetime.now().strftime("%Y-%m-%d")
            if isinstance(items, list):
                for day_off in range(3):
                    # i4/i5 = 第0天, i6/i7 = 第1天, i8/i9 = 第2天
                    lvl_k, desc_k = f"i{4 + day_off * 2}", f"i{5 + day_off * 2}"
                    d = self._shift_date(base_date, day_off)
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        c = clean_text(it.get("i1"))
                        name = clean_text(it.get("i2"))
                        level = clean_text(it.get(lvl_k))
                        desc = clean_text(it.get(desc_k))
                        if not c or not name or not level:
                            continue
                        key = (d, c)
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append({
                            "source": SOURCE,
                            "city_id": self.location.wcc_city_id,
                            "index_date": d, "code": c, "name": name,
                            "level": level, "description": desc,
                            "publish_time": base_pub,
                        })
        log.info("生活指数解析出 %d 条", len(out))
        return out

    @staticmethod
    def _shift_date(ymd: str, days: int) -> str:
        from datetime import timedelta
        try:
            d = datetime.strptime(ymd, "%Y-%m-%d")
        except (ValueError, TypeError):
            d = datetime.now()
        return (d + timedelta(days=days)).strftime("%Y-%m-%d")

    def _parse_alarms(self, az: Any) -> List[Dict[str, Any]]:
        """本地预警。

        实测结构：{"w": []} —— 无预警时为空数组（聊城 2026-10-02/03 实测为空）。
        有预警时的字段结构本轮未取到真实样本，故解析采取「保守策略」：
        仅提取能确认的通用键（title/level/type/pubtime/url 及其常见别名），
        取不到 title 的记录直接丢弃，绝不编造预警内容。
        ⚠️ 预警信息关系人身安全，宁可漏报（走 nmc 兜底）也不可错报。
        """
        if not isinstance(az, dict):
            return []
        items = az.get("w")
        if not isinstance(items, list) or not items:
            return []

        out: List[Dict[str, Any]] = []
        for a in items:
            if not isinstance(a, dict):
                continue
            title = None
            for k in ("title", "alertname", "name", "content", "txt", "text"):
                title = clean_text(a.get(k))
                if title:
                    break
            if not title:
                log.warning("预警记录缺少标题字段，丢弃（原始键：%s）",
                            list(a.keys()))
                continue
            level = None
            for k in ("level", "signallevel", "alarmlevel", "degree"):
                level = clean_text(a.get(k))
                if level:
                    break
            stype = None
            for k in ("type", "signaltype", "alarmtype", "category"):
                stype = clean_text(a.get(k))
                if stype:
                    break
            issue = None
            for k in ("pubtime", "issuetime", "pub_time", "time", "fptime"):
                issue = norm_datetime(a.get(k)) or clean_text(a.get(k))
                if issue:
                    break
            url = clean_text(a.get("url"))
            aid = clean_text(a.get("alertid")) or clean_text(a.get("id"))
            out.append({
                "source": SOURCE,
                "alarm_id": aid or title,
                "city_id": self.location.wcc_city_id,
                "title": title, "level": level, "signal_type": stype,
                "issue_time": issue, "url": url,
            })
        log.info("本地预警解析出 %d 条", len(out))
        return out
