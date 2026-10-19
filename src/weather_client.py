# -*- coding: utf-8 -*-
"""
多源天气数据客户端（提示词 PDF 模块一）。

职责：按优先级编排各数据源，实现失败降级链与交叉校验，对外只暴露
一个统一的 WeatherBundle 结构，屏蔽各源字段差异。

⚠️ 原 PDF 代码问题已修正：
  * pyqweather 的 import 路径与 QWeatherConfig 拼写在原文中已损坏
    （QWeatherCon\x01g），且该社区 SDK 停滞 25 个月、未跟进和风天气
    新版专属 API Host 与 JWT 鉴权 ⇒ 本项目不引入该 SDK，
    改为 requests 直接封装 REST API（见 crawler/qweather.py）。
  * 类型注解 `-> Optional]` 已补全为 `-> Optional[Dict[str, Any]]`。

降级链（提示词 §4.2 要求）：
  官方源（weather.com.cn → nmc.cn）→ 和风天气 → 本地缓存最近有效数据
  ⇒ 任何情况下都不推送空内容或假数据；用缓存时必须在文案中明示数据更新时间。

交叉校验（提示词 §4.1 要求）：
  weather.com.cn 与 nmc.cn 页面均标注「数据来源：中央气象台」，属同源。
  因此两者的「发布时间」应当一致或接近；偏差过大说明有一方数据陈旧，
  此时记录告警并优先采用发布时间更新的一方。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import AppConfig, load_config
from .content_generator import ContentGenerator, WeatherContent
from .crawler.http_client import PoliteHttpClient
from .crawler.nmc_cn import NmcCrawler, NmcSnapshot
from .crawler.qweather import QWeatherCrawler, QWeatherSnapshot
from .crawler.weather_com_cn import WeatherComCnCrawler, WccSnapshot
from .storage.storage import WeatherStorage, sha256_text, utcnow_iso

log = logging.getLogger(__name__)

# 交叉校验：两源发布时间允许的最大偏差（分钟）
PUBLISH_TIME_TOLERANCE_MIN = 180


@dataclass
class WeatherBundle:
    """统一后的数据包，供文案生成与落库使用。"""
    city_id: str
    display_name: str
    collected_at: str = ""
    # 统一后的业务数据
    realtime: Optional[Dict[str, Any]] = None
    today: Optional[Dict[str, Any]] = None
    daily: List[Dict[str, Any]] = field(default_factory=list)
    hourly: List[Dict[str, Any]] = field(default_factory=list)
    indices: List[Dict[str, Any]] = field(default_factory=list)
    alarms: List[Dict[str, Any]] = field(default_factory=list)
    # 溯源与合规
    publish_time: Optional[str] = None
    primary_source: Optional[str] = None
    sources_ok: List[str] = field(default_factory=list)
    sources_fail: List[str] = field(default_factory=list)
    station_id: Optional[str] = None
    # 降级标记
    from_cache: bool = False
    cache_age_text: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    # 原始快照（用于归档）
    snapshots: Dict[str, Any] = field(default_factory=dict)
    ok: bool = False

    @property
    def has_content(self) -> bool:
        return bool(self.today or self.daily)


class WeatherClient:
    """多源采集编排器。"""

    def __init__(self, cfg: Optional[AppConfig] = None,
                 client: Optional[PoliteHttpClient] = None,
                 storage: Optional[WeatherStorage] = None):
        self.cfg = cfg or load_config()
        self.client = client or PoliteHttpClient(self.cfg.crawler)
        self.storage = storage or WeatherStorage(self.cfg.db_path,
                                                 self.cfg.raw_dir)
        self.wcc = WeatherComCnCrawler(self.client, self.cfg.location)
        self.nmc = NmcCrawler(self.client, self.cfg.location)
        self.qweather = QWeatherCrawler.from_config(self.client, self.cfg)
        # 最近一次原始报文归档路径，用于写入结构化记录的 raw_file 字段（可溯源）
        self._last_raw_file: Optional[str] = None

    # ------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------
    def fetch(self, *, use_cache_on_failure: bool = True,
              verify_station: bool = True) -> WeatherBundle:
        """按优先级采集并返回统一数据包。

        Args:
            use_cache_on_failure: 全部源失败时是否回退本地缓存
            verify_station: 是否从 nmc 页面校验站号与发布时间
        """
        bundle = WeatherBundle(
            city_id=self.cfg.location.wcc_city_id,
            display_name=self.cfg.location.display_name,
            collected_at=utcnow_iso(),
            station_id=self.cfg.location.station_id,
        )
        run_id = self.storage.begin_run()
        raw_files = 0
        rows = 0

        try:
            # ---------- 源 1：中国天气网（主采集通道）----------
            wcc_snap = self._safe_call(lambda: self.wcc.collect(), "weather.com.cn")
            if wcc_snap is not None:
                bundle.snapshots["weather_com_cn"] = self._snap_to_dict(wcc_snap)
                bundle.warnings.extend(wcc_snap.warnings)
                if wcc_snap.ok:
                    self._apply_wcc(bundle, wcc_snap)
                    bundle.sources_ok.append("weather_com_cn")
                    raw_files += self._archive("weather_com_cn", wcc_snap.raw_payload)
                    rows += self._persist_wcc(wcc_snap, bundle)
                else:
                    bundle.sources_fail.append("weather_com_cn")
            else:
                # 采集器抛异常（如网络/编码错误），同样必须记为失败源
                bundle.sources_fail.append("weather_com_cn")
                bundle.warnings.append("weather.com.cn 采集器异常，详见日志")

            # ---------- 源 2：中央气象台（预警 + 溯源校验）----------
            nmc_snap = self._safe_call(lambda: self.nmc.collect(
                fetch_alarms=True, fetch_page=verify_station,
                fetch_city_index=False), "nmc.cn")
            if nmc_snap is not None:
                bundle.snapshots["nmc_cn"] = self._snap_to_dict(nmc_snap)
                bundle.warnings.extend(nmc_snap.warnings)
                if nmc_snap.ok or nmc_snap.alarms:
                    bundle.sources_ok.append("nmc_cn")
                    self._apply_nmc(bundle, nmc_snap, wcc_snap)
                    raw_files += self._archive("nmc_cn", nmc_snap.raw_payload)
                    rows += self._persist_nmc(nmc_snap)
                else:
                    bundle.sources_fail.append("nmc_cn")
            else:
                bundle.sources_fail.append("nmc_cn")
                bundle.warnings.append("nmc.cn 采集器异常，详见日志")

            # ---------- 源 3：和风天气（可选，生活指数补充 / 降级备份）----------
            if self.qweather.enabled:
                qw_snap = self._safe_call(lambda: self.qweather.collect(),
                                          "qweather")
                if qw_snap is not None:
                    bundle.snapshots["qweather"] = self._snap_to_dict(qw_snap)
                    bundle.warnings.extend(qw_snap.warnings)
                    if qw_snap.ok:
                        bundle.sources_ok.append("qweather")
                        self._apply_qweather(bundle, qw_snap)
                        raw_files += self._archive("qweather", qw_snap.raw_payload)
                        rows += self._persist_qweather(qw_snap)
                    else:
                        bundle.sources_fail.append("qweather")
            else:
                reason = self.qweather.disabled_reason()
                bundle.warnings.append(f"和风天气未启用：{reason}")
                log.info("和风天气跳过：%s", reason)

            bundle.ok = bundle.has_content

            # ---------- 降级：本地缓存 ----------
            if not bundle.ok and use_cache_on_failure:
                log.warning("所有实时源均未取到数据，尝试回退本地缓存")
                if self._load_from_cache(bundle):
                    bundle.from_cache = True
                    bundle.ok = bundle.has_content
                    bundle.warnings.append(
                        f"已回退本地缓存（数据时间 {bundle.cache_age_text}）")
                else:
                    bundle.warnings.append(
                        "本地无可用缓存 —— 本次不生成文案、不推送（拒绝推空内容或假数据）")
                    log.error("采集彻底失败且无缓存可用")

            status = ("success" if bundle.ok and not bundle.from_cache
                      else ("partial" if bundle.ok else "failed"))
            self.storage.end_run(run_id, status, bundle.sources_ok,
                                 bundle.sources_fail,
                                 "; ".join(bundle.warnings)[:1500],
                                 raw_files, rows)
            log.info("采集完成：status=%s 成功源=%s 失败源=%s 缓存=%s",
                     status, bundle.sources_ok, bundle.sources_fail,
                     bundle.from_cache)
            return bundle

        except Exception as exc:  # noqa: BLE001
            # 任何未预期异常都不能让调度进程崩溃
            log.exception("采集流程发生未预期异常")
            bundle.warnings.append(f"采集异常：{type(exc).__name__}: {exc}")
            self.storage.end_run(run_id, "failed", bundle.sources_ok,
                                 bundle.sources_fail,
                                 f"exception: {exc}"[:1500], raw_files, rows)
            return bundle

    def generate_content(self, bundle: WeatherBundle,
                         *, include_hourly: bool = True,
                         include_indices: bool = True) -> WeatherContent:
        """把数据包转成推送文案。"""
        gen = ContentGenerator(
            display_name=bundle.display_name,
            attribution_name=self.cfg.attribution_name,
            include_indices=include_indices,
            include_hourly=include_hourly,
        )
        return gen.generate(
            today=bundle.today,
            daily=bundle.daily,
            realtime=bundle.realtime,
            hourly=bundle.hourly,
            indices=bundle.indices,
            alarms=bundle.alarms,
            publish_time=bundle.publish_time,
            data_time=bundle.publish_time,
            is_stale=bundle.from_cache,
        )

    # ------------------------------------------------------------
    # 各源结果归并
    # ------------------------------------------------------------
    def _apply_wcc(self, bundle: WeatherBundle, snap: WccSnapshot) -> None:
        bundle.realtime = snap.realtime
        bundle.today = snap.today
        bundle.daily = snap.daily
        bundle.hourly = snap.hourly
        bundle.indices = snap.indices
        bundle.alarms = list(snap.alarms)
        bundle.publish_time = snap.publish_time
        bundle.primary_source = "weather_com_cn"

    def _apply_nmc(self, bundle: WeatherBundle, snap: NmcSnapshot,
                   wcc_snap: Optional[WccSnapshot]) -> None:
        """并入 nmc 数据：预警合并 + 发布时间交叉校验。"""
        # 预警合并（nmc 覆盖全国，可能含 weather.com.cn 未给的省级预警）
        if snap.alarms:
            existing = {(a.get("title") or "") for a in bundle.alarms}
            for a in snap.alarms:
                if (a.get("title") or "") not in existing:
                    bundle.alarms.append(a)
                    existing.add(a.get("title") or "")
            log.info("并入 nmc 预警后共 %d 条", len(bundle.alarms))

        if snap.station_id and not bundle.station_id:
            bundle.station_id = snap.station_id

        # ---------- 交叉校验：发布时间一致性 ----------
        nmc_pub = snap.publish_time
        wcc_pub = bundle.publish_time
        if nmc_pub and wcc_pub:
            diff = self._publish_time_diff_minutes(nmc_pub, wcc_pub)
            if diff is None:
                bundle.warnings.append(
                    f"两源发布时间格式无法比对：nmc={nmc_pub} wcc={wcc_pub}")
            elif diff > PUBLISH_TIME_TOLERANCE_MIN:
                newer, older_name = (
                    ("nmc.cn", "weather.com.cn") if nmc_pub > wcc_pub
                    else ("weather.com.cn", "nmc.cn"))
                bundle.warnings.append(
                    f"⚠️ 交叉校验：两源发布时间相差 {int(diff)} 分钟"
                    f"（nmc={nmc_pub} / wcc={wcc_pub}），{older_name} 数据可能较旧，"
                    f"已优先采用 {newer} 的发布时间")
                bundle.publish_time = max(nmc_pub, wcc_pub)
                log.warning(bundle.warnings[-1])
            else:
                log.info("✅ 交叉校验通过：两源发布时间相差 %d 分钟（nmc=%s wcc=%s）",
                         int(diff), nmc_pub, wcc_pub)
        elif nmc_pub and not wcc_pub:
            bundle.publish_time = nmc_pub

    def _apply_qweather(self, bundle: WeatherBundle,
                        snap: QWeatherSnapshot) -> None:
        """并入和风天气：仅在官方源缺失对应数据时补位，不覆盖官方值。

        ⚠️ 官方源（中国气象局系统）优先级高于商业 API，
           因此和风天气只做「填空」，绝不覆盖已有的官方数值与结论。
        """
        filled: List[str] = []

        if bundle.realtime is None and snap.realtime:
            bundle.realtime = snap.realtime
            bundle.primary_source = "qweather"
            filled.append("实时观测")

        if not bundle.daily and snap.daily:
            bundle.daily = snap.daily
            bundle.today = snap.daily[0] if snap.daily else None
            if not bundle.primary_source:
                bundle.primary_source = "qweather"
            filled.append("逐日预报")

        if not bundle.hourly and snap.hourly:
            bundle.hourly = snap.hourly
            filled.append("逐小时预报")

        if not bundle.indices and snap.indices:
            bundle.indices = snap.indices
            filled.append("生活指数")
        elif snap.indices:
            # 官方指数已存在时，仅补充官方缺失的指数项（按 code 去重）
            have = {(i.get("index_date"), i.get("code")) for i in bundle.indices}
            added = 0
            for i in snap.indices:
                key = (i.get("index_date"), i.get("code"))
                if key not in have:
                    bundle.indices.append(i)
                    have.add(key)
                    added += 1
            if added:
                filled.append(f"生活指数补充 {added} 项")

        if not bundle.alarms and snap.alarms:
            bundle.alarms = snap.alarms
            filled.append("预警")

        if not bundle.publish_time and snap.publish_time:
            bundle.publish_time = snap.publish_time

        if filled:
            bundle.warnings.append("和风天气补位：" + "、".join(filled))
            log.info("和风天气补位：%s", filled)

    # ------------------------------------------------------------
    # 落库
    # ------------------------------------------------------------
    def _persist_wcc(self, snap: WccSnapshot, bundle: WeatherBundle) -> int:
        digest = sha256_text(json.dumps(
            snap.raw_payload or {}, ensure_ascii=False, sort_keys=True,
            default=str))
        bundle.snapshots.setdefault("_sha", {})["weather_com_cn"] = digest
        raw_file = self._last_raw_file
        n = 0
        if snap.realtime:
            rec = dict(snap.realtime)
            rec["raw_sha256"] = digest
            rec["raw_file"] = raw_file
            n += self.storage.insert_realtime([rec])
        if snap.daily:
            recs = [dict(d, raw_sha256=digest, raw_file=raw_file)
                    for d in snap.daily]
            n += self.storage.upsert_daily(recs)
        if snap.today and snap.today not in snap.daily:
            n += self.storage.upsert_daily(
                [dict(snap.today, raw_sha256=digest, raw_file=raw_file)])
        if snap.hourly:
            recs = [dict(h, raw_sha256=digest) for h in snap.hourly]
            n += self.storage.upsert_hourly(recs)
        if snap.indices:
            recs = [dict(i, raw_sha256=digest) for i in snap.indices]
            n += self.storage.upsert_index(recs)
        if snap.alarms:
            recs = [dict(a, raw_sha256=digest) for a in snap.alarms]
            n += self.storage.upsert_alarm(recs)
        return n

    def _persist_nmc(self, snap: NmcSnapshot) -> int:
        if not snap.alarms:
            return 0
        digest = sha256_text(json.dumps(
            snap.raw_payload or {}, ensure_ascii=False, sort_keys=True,
            default=str))
        recs = [dict(a, raw_sha256=digest) for a in snap.alarms]
        return self.storage.upsert_alarm(recs)

    def _persist_qweather(self, snap: QWeatherSnapshot) -> int:
        digest = sha256_text(json.dumps(
            snap.raw_payload or {}, ensure_ascii=False, sort_keys=True,
            default=str))
        n = 0
        if snap.realtime:
            n += self.storage.insert_realtime(
                [dict(snap.realtime, raw_sha256=digest)])
        if snap.daily:
            n += self.storage.upsert_daily(
                [dict(d, raw_sha256=digest) for d in snap.daily])
        if snap.hourly:
            n += self.storage.upsert_hourly(
                [dict(h, raw_sha256=digest) for h in snap.hourly])
        if snap.indices:
            n += self.storage.upsert_index(
                [dict(i, raw_sha256=digest) for i in snap.indices])
        if snap.alarms:
            n += self.storage.upsert_alarm(
                [dict(a, raw_sha256=digest) for a in snap.alarms])
        return n

    def _archive(self, source: str, payload: Any) -> int:
        """归档原始报文，并记录路径供结构化数据溯源。"""
        if payload is None:
            self._last_raw_file = None
            return 0
        path = self.storage.save_raw(source, payload)
        self._last_raw_file = path
        return 1 if path else 0

    # ------------------------------------------------------------
    # 缓存降级
    # ------------------------------------------------------------
    def _load_from_cache(self, bundle: WeatherBundle) -> bool:
        """从 SQLite 读最近一次有效数据。

        ⚠️ 必须在文案中明示数据更新时间（提示词 §4.2 要求）。
        """
        try:
            rt = self.storage.latest_realtime(bundle.city_id)
            daily = self.storage.latest_daily(bundle.city_id, days=7)
            if not rt and not daily:
                return False
            if rt:
                bundle.realtime = dict(rt)
            if daily:
                bundle.daily = [dict(d) for d in daily]
                # 今天优先作为 today
                today_str = datetime.now().strftime("%Y-%m-%d")
                for d in bundle.daily:
                    if d.get("forecast_date") == today_str:
                        bundle.today = d
                        break
                bundle.today = bundle.today or bundle.daily[0]
                pub = None
                for d in daily:
                    if d.get("publish_time"):
                        pub = d["publish_time"]
                        break
                bundle.publish_time = pub
            bundle.cache_age_text = self._age_text(
                (rt or {}).get("collected_at")
                or (bundle.daily[0].get("collected_at") if bundle.daily else None))
            bundle.primary_source = (bundle.primary_source or "cache")
            log.warning("已加载缓存数据，数据时间：%s", bundle.cache_age_text)
            return True
        except Exception as exc:  # noqa: BLE001
            log.exception("加载缓存失败：%s", exc)
            return False

    @staticmethod
    def _age_text(iso_ts: Optional[str]) -> str:
        if not iso_ts:
            return "未知"
        try:
            dt = datetime.fromisoformat(str(iso_ts))
        except (ValueError, TypeError):
            return str(iso_ts)[:16]
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        mins = int((datetime.now() - dt).total_seconds() // 60)
        if mins < 60:
            return f"{mins} 分钟前"
        hours = mins // 60
        if hours < 48:
            return f"{hours} 小时前"
        return f"{hours // 24} 天前"

    # ------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------
    @staticmethod
    def _safe_call(fn, label: str):
        """单个源失败不影响其他源。"""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            log.exception("数据源 %s 采集异常", label)
            return None

    @staticmethod
    def _snap_to_dict(snap: Any) -> Dict[str, Any]:
        """快照转 dict（便于归档 JSON 序列化）。"""
        out: Dict[str, Any] = {}
        for k in ("realtime", "today", "daily", "hourly", "indices",
                  "alarms", "publish_time", "warnings", "station_id",
                  "page_update_time", "national_alarm_count", "ok"):
            if hasattr(snap, k):
                out[k] = getattr(snap, k)
        return out

    @staticmethod
    def _publish_time_diff_minutes(a: str, b: str) -> Optional[float]:
        """比对两个「YYYY-MM-DD HH:MM」字符串，返回相差分钟数（绝对值）。"""
        fmts = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S")
        da = db = None
        for f in fmts:
            if da is None:
                try:
                    da = datetime.strptime(str(a)[:19], f)
                except (ValueError, TypeError):
                    pass
            if db is None:
                try:
                    db = datetime.strptime(str(b)[:19], f)
                except (ValueError, TypeError):
                    pass
        if da is None or db is None:
            return None
        return abs((da - db).total_seconds()) / 60.0

    def close(self) -> None:
        self.client.close()
        self.storage.close()
