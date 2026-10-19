# -*- coding: utf-8 -*-
"""
定时调度入口（提示词 PDF 模块四）。

调度器：APScheduler BlockingScheduler，每日按 config.ini 设定时刻执行。
时区：Asia/Shanghai（显式指定，避免服务器时区不一致导致推送时间漂移）

⚠️ 关键设计：采集与推送拆成两个时刻
   原 PDF 只设了一个 07:30 的任务，在任务内部「先请求外部站点再生成再推送」。
   这样一旦外部站点响应慢或超时，07:30 的推送就会延迟甚至失败。
   本实现拆为：
     06:50  collect_job —— 采集 + 落库（失败可重试，不影响推送时刻）
     07:30  push_job    —— 读当日数据 + 生成文案 + 推送
   两个时刻的间隔由 config.ini 的 collect_minute_offset 控制（默认 40 分钟）。

⚠️ 部署环境提醒（详见 reports/05_部署与运维手册.md）：
   PythonAnywhere 免费版**只允许出站访问白名单内的域名**，
   d1.weather.com.cn / www.nmc.cn 均不在其白名单中，
   因此免费版**无法运行本项目的采集功能**。
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

try:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    HAS_APSCHEDULER = True
except ImportError:  # 未安装依赖时仍可跑 main.py 的单次模式
    HAS_APSCHEDULER = False

from .config import PROJECT_ROOT, AppConfig, load_config
from .daily_job import DailyJob
from .alerting import AlertManager

log = logging.getLogger(__name__)

# 日志文件（按大小轮转，单文件 5MB，保留 10 份）
LOG_FILE = PROJECT_ROOT / "logs" / "scheduler.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 10


# ============================================================
# 日志配置
# ============================================================
def setup_logging(level: str = "INFO", log_file: Optional[Path] = None) -> None:
    """同时输出到控制台与轮转日志文件。"""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # 清理已有 handler，避免重复输出
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    path = Path(log_file or LOG_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            str(path), maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:
        log.warning("无法写入日志文件 %s：%s（仅输出到控制台）", path, exc)

    # 压制第三方库的噪音日志
    for noisy in ("urllib3", "requests", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ============================================================
# 调度任务
# ============================================================
class SchedulerRunner:
    """调度运行器：封装任务定义与生命周期。"""

    def __init__(self, cfg: Optional[AppConfig] = None):
        self.cfg = cfg or load_config()
        self.alert = AlertManager(self.cfg)
        self.job = DailyJob(self.cfg, alert=self.alert)
        self._consecutive_failures = 0
        self._max_consecutive_failures = 3

    # ---------- 任务体 ----------
    def run_daily(self, *, dry_run: bool = False,
                  preview_openids=None) -> bool:
        """执行一次完整的每日任务，返回是否成功。"""
        log.info("=" * 62)
        log.info("开始执行每日气象播报任务（dry_run=%s）", dry_run)
        log.info("=" * 62)
        started = datetime.now()
        try:
            result = self.job.run(dry_run=dry_run,
                                  preview_openids=preview_openids)
            print(result.summary())
            log.info(result.summary())

            if result.ok:
                self._consecutive_failures = 0
                # 用了缓存虽算成功，但说明实时源不可用，需要提醒
                if result.from_cache:
                    self.alert.send(
                        title="气象播报使用了缓存数据",
                        detail="实时数据源全部不可达，本次推送使用的是本地缓存。"
                               "请检查网络与目标站点是否变更。\n"
                               + "；".join(result.warnings[:6]),
                        level="warning")
                return True

            self._consecutive_failures += 1
            self._escalate_if_needed(result)
            return False
        except Exception as exc:  # noqa: BLE001
            log.exception("任务执行体发生未捕获异常")
            self._consecutive_failures += 1
            self.alert.send(
                title="气象播报任务崩溃",
                detail=f"{type(exc).__name__}: {exc}",
                level="critical")
            return False
        finally:
            elapsed = (datetime.now() - started).total_seconds()
            log.info("任务结束，耗时 %.1f 秒；连续失败次数 %d",
                     elapsed, self._consecutive_failures)

    def _escalate_if_needed(self, result) -> None:
        """连续失败达阈值时升级为严重告警。

        避免「每天都失败但只发普通告警、长期无人察觉」的静默劣化。
        """
        if self._consecutive_failures >= self._max_consecutive_failures:
            self.alert.send(
                title=f"气象播报已连续失败 {self._consecutive_failures} 次",
                detail=(
                    "任务已连续多次失败，可能存在系统性问题（网络/接口变更/凭证失效）。\n"
                    f"最近失败阶段：{result.stage}\n"
                    f"错误：{'；'.join(result.errors[:3])}\n"
                    "请尽快人工介入检查。"
                ),
                level="critical",
                force=True,   # 绕过去重，确保送达
            )

    # ---------- 调度器 ----------
    def build_scheduler(self, *, dry_run: bool = False,
                        preview_openids=None) -> "BlockingScheduler":
        if not HAS_APSCHEDULER:
            raise RuntimeError(
                "未安装 APScheduler。请先执行 pip install -r requirements.txt，"
                "或改用 main.py 的单次执行模式（--once / --dry-run）")

        tz = self.cfg.schedule.timezone
        scheduler = BlockingScheduler(timezone=tz)

        # ---- 推送任务 ----
        push_h = self.cfg.schedule.push_hour
        push_m = self.cfg.schedule.push_minute
        scheduler.add_job(
            self.run_daily,
            CronTrigger(hour=push_h, minute=push_m, timezone=tz),
            kwargs={"dry_run": dry_run, "preview_openids": preview_openids},
            id="daily_weather_push",
            name=f"每日气象播报 {push_h:02d}:{push_m:02d}",
            # 错过执行（如机器休眠）后 1 小时内仍补跑，超过则放弃
            misfire_grace_time=3600,
            coalesce=True,          # 多次错过只补跑一次
            max_instances=1,        # 严禁并发执行同一任务
        )

        # ---- 采集任务（提前执行，让推送时刻直接用已落库数据）----
        offset = max(5, int(self.cfg.schedule.collect_minute_offset))
        total_min = push_h * 60 + push_m - offset
        if total_min < 0:
            total_min += 24 * 60     # 跨天（如 00:10 推送、提前 40 分钟）
        col_h, col_m = divmod(total_min, 60)
        scheduler.add_job(
            self._collect_only,
            CronTrigger(hour=col_h, minute=col_m, timezone=tz),
            id="daily_weather_collect",
            name=f"每日数据采集 {col_h:02d}:{col_m:02d}",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )

        log.info("调度已注册：采集 %02d:%02d，推送 %02d:%02d（时区 %s）",
                 col_h, col_m, push_h, push_m, tz)
        return scheduler

    def _collect_only(self) -> None:
        """仅采集落库，不生成文案、不推送。"""
        from .weather_client import WeatherClient
        log.info("开始定时采集任务")
        client = None
        try:
            client = WeatherClient(self.cfg)
            bundle = client.fetch()
            if bundle.ok:
                log.info("定时采集成功：源=%s 缓存=%s 多日=%d 逐时=%d 指数=%d 预警=%d",
                         bundle.sources_ok, bundle.from_cache, len(bundle.daily),
                         len(bundle.hourly), len(bundle.indices), len(bundle.alarms))
                if bundle.from_cache:
                    self.alert.send(
                        title="定时采集回退到了缓存",
                        detail="；".join(bundle.warnings[:6]), level="warning")
            else:
                log.error("定时采集失败：%s", bundle.warnings)
                self.alert.send(
                    title="定时数据采集失败",
                    detail="；".join(bundle.warnings[:6]) or "所有源均无有效数据",
                    level="error")
        except Exception as exc:  # noqa: BLE001
            log.exception("定时采集异常")
            self.alert.send(title="定时数据采集崩溃",
                            detail=f"{type(exc).__name__}: {exc}", level="error")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

    # ---------- 启动 ----------
    def serve(self, *, dry_run: bool = False, preview_openids=None) -> int:
        """启动阻塞式调度器。"""
        scheduler = self.build_scheduler(dry_run=dry_run,
                                         preview_openids=preview_openids)

        print("=" * 62)
        print("校园气象预报自动编撰系统 —— 调度器已启动")
        print("=" * 62)
        print(f"目标地点    : {self.cfg.location.display_name}")
        print(f"城市代码    : {self.cfg.location.wcc_city_id}")
        print(f"气象站号    : {self.cfg.location.station_id}")
        print(f"推送时间    : {self.cfg.schedule.push_hour:02d}:"
              f"{self.cfg.schedule.push_minute:02d} {self.cfg.schedule.timezone}")
        print(f"采集时间    : 提前 {self.cfg.schedule.collect_minute_offset} 分钟")
        print(f"模式        : {'🧪 DRY-RUN（不真实推送）' if dry_run else '⚠️ 真实推送'}")
        print(self.job.pusher.status_report())
        print(self.alert.status_report())
        print(f"和风天气    : {'已启用' if self.cfg.qweather_enabled else '未配置（将仅用官方源）'}")
        print(f"日志文件    : {LOG_FILE}")
        print("-" * 62)
        print("按 Ctrl+C 停止")
        print("=" * 62)

        # 启动前告警渠道检查
        problems = self.job.pusher.preflight()
        for p in problems:
            log.warning("上线前检查：%s", p)

        # 优雅退出
        def _graceful(signum, _frame):
            log.info("收到信号 %s，正在关闭调度器…", signum)
            try:
                scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _graceful)
            except (ValueError, OSError, AttributeError):
                pass   # Windows 下部分信号不支持，忽略

        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("调度器已停止")
        return 0


# ============================================================
# 命令行入口
# ============================================================
def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="校园气象预报自动编撰系统 —— 调度入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 启动每日定时调度（默认真实推送，需先配置渠道与凭证）
  python -m src.scheduler_main

  # 以干跑模式启动调度（每天定时跑，但只写 logs/dryrun_*.md，不推送）
  python -m src.scheduler_main --dry-run

  # 立即执行一次（不等待定时），推荐先用这个验证
  python -m src.scheduler_main --once --dry-run

  # 打印任务清单后退出
  python -m src.scheduler_main --list-jobs

  # 上线前检查配置（不发送任何消息）
  python -m src.scheduler_main --check
""")
    parser.add_argument("--dry-run", action="store_true",
                        help="干跑模式：走完全流程但不真实推送，"
                             "文案写入 logs/dryrun_<日期>.md")
    parser.add_argument("--once", action="store_true",
                        help="立即执行一次后退出（不启动定时器）")
    parser.add_argument("--list-jobs", action="store_true",
                        help="列出已注册的定时任务后退出")
    parser.add_argument("--check", action="store_true",
                        help="只做配置自检，不执行任务")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="日志级别，默认 INFO")
    parser.add_argument("--preview-openid", action="append", default=None,
                        help="预览接收者 openid，可多次指定（最多 5 人）。"
                             "走微信预览接口，不消耗群发额度，"
                             "用于首次真实推送前核对排版")
    args = parser.parse_args(argv)

    setup_logging(args.log_level)
    cfg = load_config()
    runner = SchedulerRunner(cfg)

    if args.check:
        print("【配置自检】")
        print(f"目标地点  : {cfg.location.display_name}")
        print(f"城市代码  : {cfg.location.wcc_city_id}")
        print(f"气象站号  : {cfg.location.station_id}")
        print(f"和风天气  : {'已启用' if cfg.qweather_enabled else '未配置'}")
        print(runner.job.pusher.status_report())
        print(runner.alert.status_report())
        print("\n【上线前检查】")
        problems = runner.job.pusher.preflight()
        if problems:
            for p in problems:
                print("  •", p)
        else:
            print("  ✅ 无阻塞问题")
        return 0

    if args.once:
        ok = runner.run_daily(dry_run=args.dry_run,
                              preview_openids=args.preview_openid)
        return 0 if ok else 1

    if args.list_jobs:
        try:
            sch = runner.build_scheduler(dry_run=args.dry_run)
        except RuntimeError as exc:
            print(f"⚠️ {exc}")
            return 1
        print("【已注册的定时任务】")
        for j in sch.get_jobs():
            print(f"  • {j.id}: {j.name}  触发器={j.trigger}")
        sch.shutdown(wait=False)
        return 0

    return runner.serve(dry_run=args.dry_run,
                        preview_openids=args.preview_openid)


if __name__ == "__main__":
    sys.exit(main())
