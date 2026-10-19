# -*- coding: utf-8 -*-
"""
配置加载模块。

设计要点：
1. 密钥只从 .env 读取，绝不硬编码、绝不写入日志（提供 mask() 脱敏）。
2. 自研 .env 解析，不引入 python-dotenv 依赖。
3. config.ini 存非敏感的可调参数（城市、时间、限速等）。
4. 全部路径基于项目根目录解析，不依赖运行时 cwd。
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

# ---------- 路径常量 ----------
# 本文件位于 <root>/src/config.py，故根目录为上两级
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

DIR_DATA = PROJECT_ROOT / "data"
DIR_RAW = DIR_DATA / "raw"
DIR_PROCESSED = DIR_DATA / "processed"
DIR_DB = DIR_DATA / "db"
DIR_LOGS = PROJECT_ROOT / "logs"
DIR_MODELS = PROJECT_ROOT / "models"
DIR_REPORTS = PROJECT_ROOT / "reports"
DIR_TESTS = PROJECT_ROOT / "tests"

PATH_ENV = PROJECT_ROOT / ".env"
PATH_INI = PROJECT_ROOT / "config.ini"
PATH_DB = DIR_DB / "weather.sqlite"

# 确保运行所需目录存在（幂等，不触碰已有文件）
for _d in (DIR_RAW, DIR_PROCESSED, DIR_DB, DIR_LOGS, DIR_MODELS, DIR_REPORTS):
    _d.mkdir(parents=True, exist_ok=True)


# ============================================================
# .env 解析
# ============================================================
def parse_dotenv(path: Path) -> Dict[str, str]:
    """极简 .env 解析器。

    支持：KEY=VALUE、# 注释、行内 # 注释（值未加引号时）、单/双引号包裹、
    export 前缀、空值。不支持多行值与变量插值（本项目用不到）。
    """
    result: Dict[str, str] = {}
    if not path.exists():
        return result

    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        # 去引号
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        else:
            # 未加引号时截断行内注释
            hash_idx = val.find(" #")
            if hash_idx >= 0:
                val = val[:hash_idx].strip()
        result[key] = val
    return result


def mask(value: Optional[str], keep: int = 3) -> str:
    """密钥脱敏，用于日志与报告输出。"""
    if not value:
        return "<未配置>"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * (len(value) - keep)}"


# ============================================================
# 配置对象
# ============================================================
@dataclass
class LocationConfig:
    """目标地点标识。

    ⚠️ 重要纠正：需求蓝本写的 101120701 实际是「济宁市」。
    聊城市（含东昌府区主城区）的正确代码是 101121701，
    已通过实测 http://d1.weather.com.cn/sk_2d/101121701.html
    返回 cityname=聊城 确认。详见 reports/02_爬虫合规检查.md 第一节。
    """
    display_name: str = "聊城市东昌府区"
    # 中国天气网 / 中央气象台体系（9 位城市代码）
    wcc_city_id: str = "101121701"
    # 中央气象台：山东省代码 + 聊城页面路径
    nmc_province_code: str = "ASD"
    nmc_city_path: str = "liaocheng"
    # 气象站号（WMO 编号），实测从 nmc.cn 聊城预报页提取
    station_id: str = "54806"
    # 和风天气 LocationID —— ⚠️ 未核实，必须由用户提供 Key 后经 GeoAPI 查询
    qweather_location_id: str = ""

    @property
    def nmc_forecast_url(self) -> str:
        return f"http://www.nmc.cn/publish/forecast/{self.nmc_province_code}/{self.nmc_city_path}.html"


@dataclass
class CrawlerConfig:
    """爬虫合规参数（对应提示词 §4.2）。"""
    min_interval_seconds: float = 2.3      # 单域名最小请求间隔，要求 >= 2
    global_max_concurrency: int = 2        # 全局并发上限
    request_timeout: int = 25
    max_retries: int = 2                   # 单次请求重试次数
    circuit_break_threshold: int = 3       # 连续失败 N 次即熔断
    circuit_break_cooldown: float = 300.0  # 熔断冷却秒数
    # 真实但非欺骗性的 UA；建议填入真实联系方式
    contact_info: str = "campus-weather-project, contact: 请填写你的邮箱"

    def user_agent(self) -> str:
        """构造真实但非欺骗性的 User-Agent。

        ⚠️ HTTP 请求头必须是 latin-1 可编码的（RFC 7230），
           若 contact_info 含中文会直接抛 UnicodeEncodeError 导致所有请求失败。
           故此处强制剥离非 ASCII 字符：中文联系方式改为 percent-encode 保留，
           既不丢失信息，也不违反 HTTP 头编码约束。
        """
        contact = (self.contact_info or "").strip()
        try:
            contact.encode("ascii")
        except UnicodeEncodeError:
            # 非 ASCII 部分做 URL 编码，保留可读性又不破坏头编码
            from urllib.parse import quote
            contact = quote(contact, safe=" ,;@.-_:")
        # 兜底：仍含非 ASCII 则彻底剔除
        contact = "".join(ch for ch in contact if ord(ch) < 128).strip()
        contact = contact or "campus-weather-project"
        return f"Mozilla/5.0 (compatible; CampusWeatherBot/1.0; {contact})"


@dataclass
class ScheduleConfig:
    """调度参数。"""
    timezone: str = "Asia/Shanghai"
    push_hour: int = 7
    push_minute: int = 30
    # 采集提前量：先采集再推送，避免推送时才请求外部站点导致超时
    collect_minute_offset: int = 40


@dataclass
class AppConfig:
    """全局配置聚合。"""
    location: LocationConfig = field(default_factory=LocationConfig)
    crawler: CrawlerConfig = field(default_factory=CrawlerConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)

    # ---- 和风天气（可选，缺失则自动降级）----
    qweather_api_host: str = ""
    qweather_api_key: str = ""

    # ---- 微信推送 ----
    wechat_app_id: str = ""
    wechat_app_secret: str = ""
    # 推送路线（已选定：subscription 订阅号每日群发，2026-10-07 确认，见 reports/06）：
    # subscription / template / wecom_webhook / dryrun（默认，未配置凭证绝不发送）
    wechat_push_channel: str = "dryrun"
    wecom_webhook_url: str = ""
    wechat_template_id: str = ""

    # ---- 异常告警 ----
    alert_wecom_webhook: str = ""
    alert_serverchan_key: str = ""
    alert_email_host: str = ""
    alert_email_port: int = 465
    alert_email_user: str = ""
    alert_email_password: str = ""
    alert_email_to: str = ""

    # ---- 文案 ----
    # 《气象法》第二十五条要求：传播气象预报必须标明发布时间与气象台站名称
    attribution_name: str = "中央气象台·中国天气网"
    # 封面图路径（仅订阅号图文群发需要；留空则在 assets/ 下查找默认封面）
    cover_image_path: str = ""

    # ---- 运行态 ----
    db_path: Path = PATH_DB
    raw_dir: Path = DIR_RAW
    logs_dir: Path = DIR_LOGS

    @property
    def qweather_enabled(self) -> bool:
        return bool(self.qweather_api_host and self.qweather_api_key)

    @property
    def wechat_enabled(self) -> bool:
        if self.wechat_push_channel == "wecom_webhook":
            return bool(self.wecom_webhook_url)
        if self.wechat_push_channel == "template":
            return bool(self.wechat_app_id and self.wechat_app_secret
                        and self.wechat_template_id)
        if self.wechat_push_channel == "subscription":
            return bool(self.wechat_app_id and self.wechat_app_secret)
        return False


# ============================================================
# 加载入口
# ============================================================
def _ini_get(cp: configparser.ConfigParser, section: str, key: str,
            default: Any) -> Any:
    """带类型推断的 ini 取值。"""
    if not cp.has_option(section, key):
        return default
    raw = cp.get(section, key).strip()
    if raw == "":
        return default
    if isinstance(default, bool):
        return raw.lower() in ("1", "true", "yes", "on", "y")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return default
    return raw


def load_config(env_path: Optional[Path] = None,
                ini_path: Optional[Path] = None) -> AppConfig:
    """加载配置：config.ini（非敏感） + .env（敏感）。

    环境变量优先级最高，便于容器/CI 覆盖。
    """
    cfg = AppConfig()

    # ---------- .env ----------
    env = parse_dotenv(env_path or PATH_ENV)

    def pick(key: str, default: str = "") -> str:
        """环境变量 > .env > 默认值"""
        v = os.environ.get(key)
        if v:
            return v.strip()
        v = env.get(key)
        if v:
            return v.strip()
        return default

    cfg.qweather_api_host = pick("QWEATHER_API_HOST").rstrip("/")
    cfg.qweather_api_key = pick("QWEATHER_API_KEY")
    cfg.wechat_app_id = pick("WECHAT_APP_ID")
    cfg.wechat_app_secret = pick("WECHAT_APP_SECRET")
    cfg.wechat_push_channel = (pick("WECHAT_PUSH_CHANNEL", "dryrun")
                               or "dryrun").lower()
    cfg.wecom_webhook_url = pick("WECOM_WEBHOOK_URL")
    cfg.wechat_template_id = pick("WECHAT_TEMPLATE_ID")

    cfg.alert_wecom_webhook = pick("ALERT_WECOM_WEBHOOK")
    cfg.alert_serverchan_key = pick("ALERT_SERVERCHAN_KEY")
    cfg.alert_email_host = pick("ALERT_EMAIL_SMTP_HOST")
    cfg.alert_email_user = pick("ALERT_EMAIL_USER")
    cfg.alert_email_password = pick("ALERT_EMAIL_PASSWORD")
    cfg.alert_email_to = pick("ALERT_EMAIL_TO")
    _port = pick("ALERT_EMAIL_SMTP_PORT", "")
    if _port.isdigit():
        cfg.alert_email_port = int(_port)

    # ---------- config.ini ----------
    cp = configparser.ConfigParser(interpolation=None)
    ini_file = ini_path or PATH_INI
    if ini_file.exists():
        try:
            cp.read(ini_file, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError) as exc:
            # 配置文件损坏不应导致程序崩溃，回退默认值
            print(f"[config] 读取 config.ini 失败，使用默认配置：{exc}")
            cp = configparser.ConfigParser(interpolation=None)

    loc = cfg.location
    loc.display_name = _ini_get(cp, "location", "display_name", loc.display_name)
    loc.wcc_city_id = _ini_get(cp, "location", "wcc_city_id", loc.wcc_city_id)
    loc.nmc_province_code = _ini_get(cp, "location", "nmc_province_code",
                                     loc.nmc_province_code)
    loc.nmc_city_path = _ini_get(cp, "location", "nmc_city_path",
                                 loc.nmc_city_path)
    loc.station_id = _ini_get(cp, "location", "station_id", loc.station_id)
    # LocationID 允许在 ini 中配置（非敏感），但仍以环境变量为优先
    loc.qweather_location_id = pick(
        "QWEATHER_LOCATION_ID",
        _ini_get(cp, "location", "qweather_location_id", ""),
    )

    cr = cfg.crawler
    cr.min_interval_seconds = float(_ini_get(
        cp, "crawler", "min_interval_seconds", cr.min_interval_seconds))
    cr.request_timeout = _ini_get(cp, "crawler", "request_timeout",
                                  cr.request_timeout)
    cr.max_retries = _ini_get(cp, "crawler", "max_retries", cr.max_retries)
    cr.circuit_break_threshold = _ini_get(
        cp, "crawler", "circuit_break_threshold", cr.circuit_break_threshold)
    cr.circuit_break_cooldown = float(_ini_get(
        cp, "crawler", "circuit_break_cooldown", cr.circuit_break_cooldown))
    cr.contact_info = _ini_get(cp, "crawler", "contact_info", cr.contact_info)
    # 合规硬下限：即使配置文件写小了也不允许低于 2 秒
    if cr.min_interval_seconds < 2.0:
        cr.min_interval_seconds = 2.0

    sc = cfg.schedule
    sc.timezone = _ini_get(cp, "schedule", "timezone", sc.timezone)
    sc.push_hour = _ini_get(cp, "schedule", "push_hour", sc.push_hour)
    sc.push_minute = _ini_get(cp, "schedule", "push_minute", sc.push_minute)
    sc.collect_minute_offset = _ini_get(
        cp, "schedule", "collect_minute_offset", sc.collect_minute_offset)

    cfg.attribution_name = _ini_get(cp, "content", "attribution_name",
                                    cfg.attribution_name)
    cfg.cover_image_path = _ini_get(cp, "content", "cover_image_path",
                                    cfg.cover_image_path)

    return cfg


if __name__ == "__main__":
    # 自检：打印脱敏后的配置概览
    c = load_config()
    print("项目根目录:", PROJECT_ROOT)
    print("目标地点  :", c.location.display_name)
    print("城市代码  :", c.location.wcc_city_id, "(weather.com.cn 体系)")
    print("气象站号  :", c.location.station_id)
    print("nmc 预报页:", c.location.nmc_forecast_url)
    print("和风天气  :", "已启用" if c.qweather_enabled else "未配置（将降级到官方源）")
    print("  API Host:", mask(c.qweather_api_host, 8) if c.qweather_api_host else "<未配置>")
    print("  API Key :", mask(c.qweather_api_key))
    print("  LocationID:", c.location.qweather_location_id or "<⚠️ 未核实，需 GeoAPI 查询>")
    print("推送路线  :", c.wechat_push_channel)
    print("推送可用  :", c.wechat_enabled)
    print("限速间隔  :", c.crawler.min_interval_seconds, "秒/域名")
    print("推送时间  :", f"{c.schedule.push_hour:02d}:{c.schedule.push_minute:02d}", c.schedule.timezone)
    print("数据落盘  :", c.db_path)
