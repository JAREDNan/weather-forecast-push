# -*- coding: utf-8 -*-
"""
数据落库模块：原始报文归档 + SQLite 结构化存储。

对应提示词 §4.2 的落库要求：
  * 原始 JSON 按日期分文件写入 data/raw/（只增不改）
  * 结构化数据写入 data/db/weather.sqlite
  * 每条记录含 采集时间戳 / 数据源 / 原始报文 SHA-256，保证可溯源

设计原则：
  * 原始报文永不覆盖、永不修改（append-only 按日期命名）
  * 结构化表用 UPSERT，同一 (数据源, 数据日期, 站点) 重复采集时更新而非插重
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
-- 实时/逐日观测与预报（结构化）
CREATE TABLE IF NOT EXISTS weather_daily (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,             -- 数据源标识：weather_com_cn / nmc_cn / qweather
    city_id       TEXT NOT NULL,             -- 城市代码（weather.com.cn 体系）
    station_id    TEXT,                      -- 气象站号（如有）
    forecast_date TEXT NOT NULL,             -- 预报/观测所属日期 YYYY-MM-DD
    temp_max      REAL,
    temp_min      REAL,
    text_day      TEXT,                      -- 白天天气现象（官方原文，不改写）
    text_night    TEXT,                      -- 夜间天气现象（官方原文，不改写）
    wind_dir_day  TEXT,
    wind_scale_day TEXT,
    wind_dir_night TEXT,
    wind_scale_night TEXT,
    sunrise       TEXT,
    sunset        TEXT,
    is_observation INTEGER DEFAULT 0,        -- 1=实测值 0=预报值（区分极重要，禁止混用）
    publish_time  TEXT,                      -- 官方发布时间（《气象法》要求标注）
    collected_at  TEXT NOT NULL,             -- 本地采集时间（UTC ISO8601）
    raw_sha256    TEXT NOT NULL,             -- 原始报文哈希，可溯源
    raw_file      TEXT,                      -- 原始报文归档相对路径
    updated_at    TEXT NOT NULL,
    UNIQUE (source, city_id, forecast_date, is_observation)
);

-- 实时观测快照（每次采集一条，不覆盖，便于分析实况变化）
CREATE TABLE IF NOT EXISTS weather_realtime (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    city_id      TEXT NOT NULL,
    observed_at  TEXT,                       -- 源站给出的观测时间
    temp         REAL,
    humidity     REAL,
    wind_dir     TEXT,
    wind_scale   TEXT,
    pressure     REAL,
    visibility   REAL,
    rain_1h      REAL,
    rain_24h     REAL,
    aqi          REAL,
    text         TEXT,
    collected_at TEXT NOT NULL,
    raw_sha256   TEXT NOT NULL,
    raw_file     TEXT
);

-- 逐小时预报
CREATE TABLE IF NOT EXISTS weather_hourly (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    city_id      TEXT NOT NULL,
    hour_ts      TEXT NOT NULL,              -- 该小时的时间 YYYY-MM-DDTHH:MM
    temp         REAL,
    humidity     REAL,
    text         TEXT,
    wind_dir     TEXT,
    wind_scale   TEXT,
    wind_speed   REAL,
    wind_deg     REAL,
    pressure     REAL,
    visibility   REAL,
    rain         REAL,
    collected_at TEXT NOT NULL,
    raw_sha256   TEXT NOT NULL,
    UNIQUE (source, city_id, hour_ts)
);

-- 生活指数
CREATE TABLE IF NOT EXISTS weather_index (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    city_id      TEXT NOT NULL,
    index_date   TEXT NOT NULL,              -- 指数适用日期 YYYY-MM-DD
    code         TEXT NOT NULL,              -- 指数代码：ct(穿衣)/gm(感冒)/uv(紫外线)...
    name         TEXT NOT NULL,              -- 指数中文名
    level        TEXT,                       -- 等级，如「较舒适」
    description  TEXT,                       -- 官方描述原文
    publish_time TEXT,
    collected_at TEXT NOT NULL,
    raw_sha256   TEXT NOT NULL,
    UNIQUE (source, city_id, index_date, code)
);

-- 气象预警
CREATE TABLE IF NOT EXISTS weather_alarm (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    alarm_id     TEXT,                       -- 源站预警唯一标识
    city_id      TEXT,
    title        TEXT NOT NULL,
    level        TEXT,                       -- 蓝/黄/橙/红
    signal_type  TEXT,                       -- 大风/暴雨/高温...
    issue_time   TEXT,
    url          TEXT,
    collected_at TEXT NOT NULL,
    raw_sha256   TEXT NOT NULL,
    UNIQUE (source, alarm_id, title)
);

-- 采集运行日志（用于运维排查与报告统计）
CREATE TABLE IF NOT EXISTS collect_run (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT,                       -- success / partial / failed
    sources_ok   TEXT,                       -- 成功的数据源，逗号分隔
    sources_fail TEXT,
    message      TEXT,
    raw_files    INTEGER DEFAULT 0,
    rows_written INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_daily_date ON weather_daily (forecast_date);
CREATE INDEX IF NOT EXISTS idx_daily_src_date ON weather_daily (source, forecast_date);
CREATE INDEX IF NOT EXISTS idx_rt_collected ON weather_realtime (collected_at);
CREATE INDEX IF NOT EXISTS idx_hourly_ts ON weather_hourly (hour_ts);
CREATE INDEX IF NOT EXISTS idx_index_date ON weather_index (index_date);
CREATE INDEX IF NOT EXISTS idx_alarm_issue ON weather_alarm (issue_time);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow_iso() -> str:
    """当前 UTC 时间，ISO8601。统一用它做采集时间戳，避免时区歧义。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class WeatherStorage:
    """SQLite + 原始报文归档的统一存储层。"""

    def __init__(self, db_path: Path, raw_dir: Path):
        self.db_path = Path(db_path)
        self.raw_dir = Path(raw_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    # ------------------------------------------------------------
    # 原始报文归档（append-only，只增不改）
    # ------------------------------------------------------------
    def save_raw(self, source: str, payload: Any, *,
                 tag: str = "data",
                 fmt: str = "json") -> Optional[str]:
        """把原始报文写入 data/raw/<YYYY>/<YYYY-MM-DD>/ 下。

        文件名含采集时刻与内容哈希前 12 位，天然不冲突、不覆盖。
        返回相对于项目根 data/ 的路径字符串（入库用），失败返回 None。
        """
        try:
            if isinstance(payload, (dict, list)):
                text = json.dumps(payload, ensure_ascii=False, indent=2,
                                  sort_keys=True)
            elif isinstance(payload, bytes):
                text = payload.decode("utf-8", errors="replace")
            else:
                text = str(payload)
        except (TypeError, ValueError) as exc:
            log.error("原始报文序列化失败 %s/%s: %s", source, tag, exc)
            return None

        now = datetime.now()
        digest = sha256_text(text)[:12]
        sub = self.raw_dir / f"{now:%Y}" / f"{now:%Y-%m-%d}"
        try:
            sub.mkdir(parents=True, exist_ok=True)
            fname = f"{source}__{tag}__{now:%Y%m%dT%H%M%S}__{digest}.{fmt}"
            fpath = sub / fname
            # 绝不覆盖：同名（哈希+时刻完全一致）时追加序号
            seq = 1
            while fpath.exists():
                fpath = sub / (
                    f"{source}__{tag}__{now:%Y%m%dT%H%M%S}__{digest}_{seq}.{fmt}"
                )
                seq += 1
            fpath.write_text(text, encoding="utf-8")
            try:
                return str(fpath.relative_to(self.raw_dir.parent))
            except ValueError:
                return str(fpath)
        except OSError as exc:
            log.error("原始报文落盘失败 %s/%s: %s", source, tag, exc)
            return None

    # ------------------------------------------------------------
    # 结构化写入
    # ------------------------------------------------------------
    def upsert_daily(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        now = utcnow_iso()
        sql = """
        INSERT INTO weather_daily (
            source, city_id, station_id, forecast_date, temp_max, temp_min,
            text_day, text_night, wind_dir_day, wind_scale_day,
            wind_dir_night, wind_scale_night, sunrise, sunset,
            is_observation, publish_time, collected_at, raw_sha256, raw_file,
            updated_at)
        VALUES (
            :source, :city_id, :station_id, :forecast_date, :temp_max, :temp_min,
            :text_day, :text_night, :wind_dir_day, :wind_scale_day,
            :wind_dir_night, :wind_scale_night, :sunrise, :sunset,
            :is_observation, :publish_time, :collected_at, :raw_sha256, :raw_file,
            :updated_at)
        ON CONFLICT (source, city_id, forecast_date, is_observation) DO UPDATE SET
            temp_max=excluded.temp_max, temp_min=excluded.temp_min,
            text_day=excluded.text_day, text_night=excluded.text_night,
            wind_dir_day=excluded.wind_dir_day, wind_scale_day=excluded.wind_scale_day,
            wind_dir_night=excluded.wind_dir_night,
            wind_scale_night=excluded.wind_scale_night,
            sunrise=excluded.sunrise, sunset=excluded.sunset,
            publish_time=excluded.publish_time, collected_at=excluded.collected_at,
            raw_sha256=excluded.raw_sha256, raw_file=excluded.raw_file,
            updated_at=excluded.updated_at
        """
        n = 0
        with self._lock, self._conn:
            for r in rows:
                rec = {
                    "source": r.get("source"), "city_id": r.get("city_id"),
                    "station_id": r.get("station_id"),
                    "forecast_date": r.get("forecast_date"),
                    "temp_max": r.get("temp_max"), "temp_min": r.get("temp_min"),
                    "text_day": r.get("text_day"), "text_night": r.get("text_night"),
                    "wind_dir_day": r.get("wind_dir_day"),
                    "wind_scale_day": r.get("wind_scale_day"),
                    "wind_dir_night": r.get("wind_dir_night"),
                    "wind_scale_night": r.get("wind_scale_night"),
                    "sunrise": r.get("sunrise"), "sunset": r.get("sunset"),
                    "is_observation": int(bool(r.get("is_observation"))),
                    "publish_time": r.get("publish_time"),
                    "collected_at": r.get("collected_at") or now,
                    "raw_sha256": r.get("raw_sha256", ""),
                    "raw_file": r.get("raw_file"),
                    "updated_at": now,
                }
                if not rec["source"] or not rec["forecast_date"] \
                        or not rec["city_id"]:
                    log.warning("跳过字段不完整的 daily 记录: %s", rec)
                    continue
                self._conn.execute(sql, rec)
                n += 1
        log.info("weather_daily 写入 %d 条", n)
        return n

    def insert_realtime(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        now = utcnow_iso()
        sql = """
        INSERT INTO weather_realtime (
            source, city_id, observed_at, temp, humidity, wind_dir, wind_scale,
            pressure, visibility, rain_1h, rain_24h, aqi, text,
            collected_at, raw_sha256, raw_file)
        VALUES (
            :source, :city_id, :observed_at, :temp, :humidity, :wind_dir,
            :wind_scale, :pressure, :visibility, :rain_1h, :rain_24h, :aqi,
            :text, :collected_at, :raw_sha256, :raw_file)
        """
        n = 0
        with self._lock, self._conn:
            for r in rows:
                if not r.get("source") or not r.get("city_id"):
                    continue
                rec = dict(r)
                rec.setdefault("collected_at", now)
                # 所有可空字段必须显式给默认值，否则 sqlite3 会因
                # 缺少绑定参数而抛 ProgrammingError
                for k in ("temp", "humidity", "pressure", "visibility",
                          "rain_1h", "rain_24h", "aqi", "observed_at",
                          "wind_dir", "wind_scale", "text",
                          "raw_sha256", "raw_file"):
                    rec.setdefault(k, None)
                rec.setdefault("raw_sha256", "")
                self._conn.execute(sql, rec)
                n += 1
        log.info("weather_realtime 写入 %d 条", n)
        return n

    def upsert_hourly(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        now = utcnow_iso()
        sql = """
        INSERT INTO weather_hourly (
            source, city_id, hour_ts, temp, humidity, text, wind_dir,
            wind_scale, wind_speed, wind_deg, pressure, visibility, rain,
            collected_at, raw_sha256)
        VALUES (
            :source, :city_id, :hour_ts, :temp, :humidity, :text, :wind_dir,
            :wind_scale, :wind_speed, :wind_deg, :pressure, :visibility, :rain,
            :collected_at, :raw_sha256)
        ON CONFLICT (source, city_id, hour_ts) DO UPDATE SET
            temp=excluded.temp, humidity=excluded.humidity, text=excluded.text,
            wind_dir=excluded.wind_dir, wind_scale=excluded.wind_scale,
            wind_speed=excluded.wind_speed, wind_deg=excluded.wind_deg,
            pressure=excluded.pressure, visibility=excluded.visibility,
            rain=excluded.rain, collected_at=excluded.collected_at,
            raw_sha256=excluded.raw_sha256
        """
        n = 0
        with self._lock, self._conn:
            for r in rows:
                if not r.get("hour_ts"):
                    continue
                rec = dict(r)
                rec.setdefault("collected_at", now)
                for k in ("temp", "humidity", "wind_speed", "wind_deg",
                          "pressure", "visibility", "rain"):
                    rec.setdefault(k, None)
                self._conn.execute(sql, rec)
                n += 1
        log.info("weather_hourly 写入 %d 条", n)
        return n

    def upsert_index(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        now = utcnow_iso()
        sql = """
        INSERT INTO weather_index (
            source, city_id, index_date, code, name, level, description,
            publish_time, collected_at, raw_sha256)
        VALUES (
            :source, :city_id, :index_date, :code, :name, :level, :description,
            :publish_time, :collected_at, :raw_sha256)
        ON CONFLICT (source, city_id, index_date, code) DO UPDATE SET
            name=excluded.name, level=excluded.level,
            description=excluded.description, publish_time=excluded.publish_time,
            collected_at=excluded.collected_at, raw_sha256=excluded.raw_sha256
        """
        n = 0
        with self._lock, self._conn:
            for r in rows:
                if not r.get("code") or not r.get("name"):
                    continue
                rec = dict(r)
                rec.setdefault("collected_at", now)
                self._conn.execute(sql, rec)
                n += 1
        log.info("weather_index 写入 %d 条", n)
        return n

    def upsert_alarm(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        now = utcnow_iso()
        sql = """
        INSERT INTO weather_alarm (
            source, alarm_id, city_id, title, level, signal_type, issue_time,
            url, collected_at, raw_sha256)
        VALUES (
            :source, :alarm_id, :city_id, :title, :level, :signal_type,
            :issue_time, :url, :collected_at, :raw_sha256)
        ON CONFLICT (source, alarm_id, title) DO UPDATE SET
            level=excluded.level, signal_type=excluded.signal_type,
            issue_time=excluded.issue_time, url=excluded.url,
            collected_at=excluded.collected_at, raw_sha256=excluded.raw_sha256
        """
        n = 0
        with self._lock, self._conn:
            for r in rows:
                if not r.get("title"):
                    continue
                rec = dict(r)
                rec.setdefault("collected_at", now)
                rec.setdefault("alarm_id", "")
                self._conn.execute(sql, rec)
                n += 1
        log.info("weather_alarm 写入 %d 条", n)
        return n

    # ------------------------------------------------------------
    # 运行日志
    # ------------------------------------------------------------
    def begin_run(self) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO collect_run (started_at, status) VALUES(?, ?)",
                (utcnow_iso(), "running"),
            )
            return int(cur.lastrowid)

    def end_run(self, run_id: int, status: str, sources_ok: List[str],
                sources_fail: List[str], message: str = "",
                raw_files: int = 0, rows_written: int = 0) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE collect_run SET finished_at=?, status=?, sources_ok=?,
                   sources_fail=?, message=?, raw_files=?, rows_written=?
                   WHERE id=?""",
                (utcnow_iso(), status, ",".join(sources_ok),
                 ",".join(sources_fail), message, raw_files, rows_written,
                 run_id),
            )

    # ------------------------------------------------------------
    # 查询（供文案生成与缓存降级使用）
    # ------------------------------------------------------------
    def latest_realtime(self, city_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM weather_realtime WHERE city_id=?
                   ORDER BY collected_at DESC LIMIT 1""", (city_id,),
            ).fetchone()
        return dict(row) if row else None

    def latest_daily(self, city_id: str, days: int = 7) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM weather_daily
                   WHERE city_id=? AND is_observation=0
                     AND forecast_date >= date('now', 'localtime')
                   ORDER BY forecast_date ASC LIMIT ?""",
                (city_id, days),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_alarms(self, city_id: Optional[str] = None,
                      limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            if city_id:
                rows = self._conn.execute(
                    """SELECT * FROM weather_alarm WHERE city_id=?
                       ORDER BY issue_time DESC LIMIT ?""",
                    (city_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """SELECT * FROM weather_alarm
                       ORDER BY issue_time DESC LIMIT ?""", (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_indices(self, city_id: str, index_date: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM weather_index WHERE city_id=? AND index_date=?
                   ORDER BY code""", (city_id, index_date),
            ).fetchall()
        return [dict(r) for r in rows]

    def daily_stats(self) -> Dict[str, Any]:
        """统计各表数据量，用于数据体检报告。"""
        out: Dict[str, Any] = {}
        tables = ("weather_daily", "weather_realtime", "weather_hourly",
                  "weather_index", "weather_alarm", "collect_run")
        with self._lock:
            for t in tables:
                try:
                    n = self._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    out[t] = int(n)
                except sqlite3.Error as exc:
                    out[t] = f"error: {exc}"
            try:
                rng = self._conn.execute(
                    """SELECT MIN(forecast_date), MAX(forecast_date)
                       FROM weather_daily"""
                ).fetchone()
                out["daily_date_range"] = [rng[0], rng[1]]
            except sqlite3.Error:
                out["daily_date_range"] = None
        return out

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            except sqlite3.Error:
                pass
            self._conn.close()

    def __enter__(self) -> "WeatherStorage":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
