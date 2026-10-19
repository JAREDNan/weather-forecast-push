# -*- coding: utf-8 -*-
"""
校园气象预报自动编撰系统 —— 命令行主入口。

对应提示词 §6.2：「main.py --dry-run 走完全流程但不真实群发，
把要推送的标题+正文+封面写到 logs\\dryrun_<日期>.md 供人工审阅。
这个模式必须先跑通并经用户确认，才允许开启真实推送。」

常用命令：
    python main.py --dry-run          # 干跑：采集+生成文案，写 logs/dryrun_*.md，不推送
    python main.py --collect          # 只采集落库，不生成文案
    python main.py --status           # 查看数据库现状与配置状态
    python main.py --serve            # 启动每日定时调度
    python main.py --selftest         # 全链路自检（不联网、不推送）

⚠️ 默认行为是 --dry-run，即不加任何参数运行也不会发出任何消息。
   要真实推送必须显式使用 --serve 或 --push，且需先在 .env 配好渠道与凭证。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# 允许以 `python main.py` 直接运行（把项目根加入 sys.path）
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import load_config, mask           # noqa: E402
from src.daily_job import DailyJob                 # noqa: E402
from src.scheduler_main import SchedulerRunner, setup_logging   # noqa: E402

log = logging.getLogger(__name__)


# ============================================================
# 各子命令
# ============================================================
def cmd_dryrun(runner: SchedulerRunner, args) -> int:
    """干跑：走完全流程但不真实推送。"""
    print("=" * 62)
    print("🧪 DRY-RUN 模式 —— 不会发送任何真实消息")
    print("=" * 62)
    ok = runner.run_daily(dry_run=True, preview_openids=args.preview_openid)
    logs = sorted(Path(runner.cfg.logs_dir).glob("dryrun_*.md"))
    if logs:
        print(f"\n📄 干跑文案已写入：{logs[-1]}")
        print("   请人工审阅后再决定是否开启真实推送。")
    return 0 if ok else 1


def cmd_push(runner: SchedulerRunner, args) -> int:
    """真实推送（需已配置渠道与凭证）。"""
    pusher = runner.job.pusher
    print("=" * 62)
    print("⚠️ 真实推送模式")
    print("=" * 62)
    print(pusher.status_report())
    problems = pusher.preflight()
    if problems:
        print("\n【上线前检查】")
        for p in problems:
            print("  •", p)
    if not pusher.will_really_send:
        print("\n❌ 当前渠道不会真实发送（未配置凭证或渠道为 dryrun）。")
        print("   请在 .env 中配置 WECHAT_PUSH_CHANNEL 与对应凭证后重试。")
        print("   或改用：python main.py --dry-run")
        return 1
    print("\n提示：首次真实推送建议加 --preview-openid <你的openid>，")
    print("     走微信预览接口只发给自己、不消耗群发额度（提示词 §6.3 要求）。")
    ok = runner.run_daily(dry_run=False, preview_openids=args.preview_openid)
    return 0 if ok else 1


def cmd_collect(runner: SchedulerRunner, _args) -> int:
    """只采集落库。"""
    print("=" * 62)
    print("📥 仅采集模式")
    print("=" * 62)
    from src.weather_client import WeatherClient
    client = None
    try:
        client = WeatherClient(runner.cfg)
        bundle = client.fetch()
        print(f"采集结果    : {'✅ 成功' if bundle.ok else '❌ 失败'}")
        print(f"成功数据源  : {bundle.sources_ok or '—'}")
        print(f"失败数据源  : {bundle.sources_fail or '—'}")
        print(f"使用缓存    : {'是' if bundle.from_cache else '否'}")
        print(f"官方发布时间: {bundle.publish_time or '—'}")
        print(f"气象站号    : {bundle.station_id or '—'}")
        print(f"数据量      : 多日={len(bundle.daily)} 逐时={len(bundle.hourly)} "
              f"指数={len(bundle.indices)} 预警={len(bundle.alarms)}")
        if bundle.today:
            t = bundle.today
            print(f"今日预报    : {t.get('text_full') or t.get('text_day') or '—'} "
                  f"{t.get('temp_min')}~{t.get('temp_max')}℃")
        if bundle.warnings:
            print("\n【警告】")
            for w in bundle.warnings:
                print("  •", w)
        return 0 if bundle.ok else 1
    finally:
        if client is not None:
            client.close()


def cmd_status(runner: SchedulerRunner, _args) -> int:
    """查看配置与数据库状态。"""
    cfg = runner.cfg
    print("=" * 62)
    print("📊 系统状态")
    print("=" * 62)
    print("\n【目标地点】")
    print(f"  展示名称  : {cfg.location.display_name}")
    print(f"  城市代码  : {cfg.location.wcc_city_id}  (weather.com.cn 体系)")
    print(f"  气象站号  : {cfg.location.station_id}")
    print(f"  nmc 预报页: {cfg.location.nmc_forecast_url}")
    print(f"  ⚠️ 已纠正: 蓝本写的 101120701 实为济宁市，正确值为 101121701")

    print("\n【数据源】")
    print(f"  和风天气  : {'✅ 已启用' if cfg.qweather_enabled else '❌ 未配置（仅用官方源，主流程不受影响）'}")
    print(f"    API Host: {mask(cfg.qweather_api_host, 8) if cfg.qweather_api_host else '<未配置>'}")
    print(f"    API Key : {mask(cfg.qweather_api_key)}")
    print(f"    LocationID: {cfg.location.qweather_location_id or '<⚠️ 未核实，需 GeoAPI 查询>'}")

    print("\n【推送】")
    print("  " + runner.job.pusher.status_report().replace("\n", "\n  "))

    print("\n【告警】")
    print("  " + runner.alert.status_report())

    print("\n【调度】")
    print(f"  推送时间  : {cfg.schedule.push_hour:02d}:{cfg.schedule.push_minute:02d} {cfg.schedule.timezone}")
    print(f"  采集时间  : 提前 {cfg.schedule.collect_minute_offset} 分钟")
    print(f"  限速      : {cfg.crawler.min_interval_seconds} 秒/域名")

    print("\n【数据库】")
    print(f"  路径      : {cfg.db_path}")
    if cfg.db_path.exists():
        from src.storage.storage import WeatherStorage
        st = WeatherStorage(cfg.db_path, cfg.raw_dir)
        try:
            stats = st.daily_stats()
            for k, v in stats.items():
                print(f"  {k:<20}: {v}")
        finally:
            st.close()
        size_kb = cfg.db_path.stat().st_size / 1024
        print(f"  文件大小  : {size_kb:.1f} KB")
    else:
        print("  尚未创建（首次采集后生成）")

    print("\n【原始报文归档】")
    raw = Path(cfg.raw_dir)
    if raw.exists():
        files = list(raw.rglob("*.json"))
        print(f"  目录      : {raw}")
        print(f"  文件数    : {len(files)}")
        if files:
            print(f"  最新      : {max(files, key=lambda p: p.stat().st_mtime).name}")
    else:
        print("  尚未创建")

    print("\n【上线前检查】")
    problems = runner.job.pusher.preflight()
    if problems:
        for p in problems:
            print("  •", p)
    else:
        print("  ✅ 无阻塞问题")
    return 0


def cmd_samples(runner: SchedulerRunner, args) -> int:
    """生成多天干跑样例文案（满足交付清单「至少 3 天的干跑样例」）。

    ⚠️ 性质说明（必须如实告知用户，不得误导）：
      这些样例的**数据全部来自官方真实预报**（weather.com.cn / nmc.cn），
      但生成日期是「以未来某日为基准日的预演」，并非当天真实运行产生的记录。
      原因：实时采集从今天才开始，不可能有 3 天真实运行历史。
      文件内会显式标注「预演样例」字样，避免与真实运行记录混淆。
      真实运行的干跑记录由 --dry-run 每天自动生成，同一天多次运行会追加。
    """
    from datetime import datetime as _dt

    from src.content_generator import ContentGenerator
    from src.weather_client import WeatherClient

    days = max(1, int(args.samples))
    print("=" * 62)
    print(f"📝 生成 {days} 天干跑样例文案（预演，数据来自官方真实预报）")
    print("=" * 62)

    client = None
    try:
        client = WeatherClient(runner.cfg)
        bundle = client.fetch()
        if not bundle.ok:
            print("❌ 采集失败，无法生成样例：", bundle.warnings)
            return 1
        if not bundle.daily:
            print("❌ 无多日预报数据，无法生成样例")
            return 1

        pusher = runner.job.pusher
        written: List[Path] = []
        by_date = {d["forecast_date"]: d for d in bundle.daily}

        for offset in range(days):
            day_rec = bundle.daily[offset] if offset < len(bundle.daily) else None
            if not day_rec:
                print(f"  ⚠️ 第 {offset + 1} 天无预报数据，跳过")
                continue
            base = _dt.strptime(day_rec["forecast_date"], "%Y-%m-%d")
            gen = ContentGenerator(
                display_name=bundle.display_name,
                attribution_name=runner.cfg.attribution_name,
                include_hourly=(offset == 0),   # 逐小时只对当天有意义
                include_indices=True,
                base_date=base,
            )
            # 以该日为基准日：today 取该日记录，未来几天取其后各日
            future = [d for d in bundle.daily if d["forecast_date"] > base.strftime("%Y-%m-%d")]
            content = gen.generate(
                today=day_rec,
                daily=[day_rec] + future,
                realtime=(bundle.realtime if offset == 0 else None),
                hourly=(bundle.hourly if offset == 0 else None),
                indices=bundle.indices,
                alarms=(bundle.alarms if offset == 0 else []),
                publish_time=bundle.publish_time,
                data_time=bundle.publish_time,
            )
            if not content.ok:
                print(f"  ⚠️ {day_rec['forecast_date']} 文案生成失败：{content.warnings}")
                continue

            # 合规校验：样例同样必须通过，否则说明生成器有缺陷
            problems = DailyJob._check_compliance(content)   # noqa: SLF001
            if problems:
                print(f"  ❌ {day_rec['forecast_date']} 未通过合规校验：{problems}")
                return 1

            note = (f"（预演样例：基准日 {base:%Y-%m-%d}，"
                    f"数据为官方 {bundle.publish_time or '—'} 发布的真实预报）")
            res = pusher.write_dryrun(content.title, content.full_text(),
                                      content.summary, None, note=note,
                                      day_label=base.strftime("%Y%m%d"))
            if res.ok and res.log_file:
                p = Path(res.log_file)
                if p not in written:
                    written.append(p)
                print(f"  ✅ {base:%Y-%m-%d} → {p.name}"
                      f"（{content.title[:34]}…）")
            else:
                print(f"  ❌ {base:%Y-%m-%d} 写入失败：{res.message}")

        print("\n" + "=" * 62)
        if written:
            print(f"✅ 已生成/更新 {len(written)} 个样例文件：")
            for p in written:
                print(f"   {p}")
            print("\n⚠️ 请注意区分：")
            print("   • 标注「预演样例」的条目 = 用官方预报数据按未来某日基准日预演生成")
            print("   • 无该标注的条目 = --dry-run 当天真实运行产生的记录")
            print("   两者数据均来自官方真实预报，区别仅在于运行的实际日期。")
        else:
            print("❌ 未生成任何样例")
        print("=" * 62)
        return 0 if written else 1
    finally:
        if client is not None:
            client.close()


def cmd_gzh_pack(runner: SchedulerRunner, _args) -> int:
    """生成「公众号推送包」：采集 → 生成 → 合规 → 产出可粘贴图文文件。

    背景（2026-10-19 确认）：当前公众号为未认证订阅号，API 群发权限已被
    微信回收（48001），但网页后台手动群发每天 1 次仍可用。本命令产出
    logs/gzh_pack_<日期>.md（操作指引+纯文本正文）与 .html（富文本正文），
    供人工约 1 分钟完成群发，覆盖全部关注者。
    """
    from src.gzh_pack import GzhPackWriter
    from src.weather_client import WeatherClient

    print("=" * 62)
    print("📦 生成公众号推送包（人工手动群发辅助，不发送任何消息）")
    print("=" * 62)

    client = None
    try:
        client = WeatherClient(runner.cfg)
        bundle = client.fetch()
        if not bundle.ok:
            print("❌ 采集失败，无法生成推送包：", bundle.warnings)
            return 1
        if not bundle.today and not bundle.daily:
            print("❌ 无官方预报数据，无法生成推送包")
            return 1

        # 复用主流程的文案生成（与 --dry-run 相同路径）
        content = client.generate_content(bundle)
        if not content.ok:
            print("❌ 文案生成失败：", content.warnings)
            return 1

        # 合规校验：与推送路径完全一致（含法定署名硬性检查）
        problems = DailyJob._check_compliance(content)   # noqa: SLF001
        if problems:
            print("❌ 未通过合规校验，禁止生成推送包：")
            for p in problems:
                print("   •", p)
            return 1

        writer = GzhPackWriter(runner.cfg.logs_dir)
        extra = ""
        if bundle.from_cache:
            extra = "本次使用了缓存数据，请注意核对数据时间"
        if bundle.warnings:
            extra = (extra + "；" if extra else "") + "；".join(bundle.warnings[:3])
        paths = writer.write(content, extra_note=extra)

        print(f"  文案标题  : {content.title}")
        print(f"  正文字数  : {len(content.full_text())}")
        print(f"  合规署名  : {'✅ 含信息来源与发布时间' if content.attribution else '❌ 缺失'}")
        print("\n" + "=" * 62)
        print("✅ 公众号推送包已生成：")
        for p in paths:
            print(f"   {p}")
        print("\n👉 发布步骤（约 1 分钟）：")
        print("   1) 打开 mp.weixin.qq.com → 新建图文消息")
        print("   2) 复制 .md 文件中的【标题】【摘要】，按【封面建议】传图")
        print("   3) 正文：打开 .html 文件全选复制粘贴（保留加粗排版），")
        print("      或直接复制 .md 中的【纯文本正文】")
        print("   4) 确认文末「信息来源+发布时间」署名完整后 → 群发")
        print("=" * 62)
        return 0
    finally:
        if client is not None:
            client.close()


def cmd_serve(runner: SchedulerRunner, args) -> int:
    """启动定时调度。"""
    return runner.serve(dry_run=False, preview_openids=args.preview_openid)


def cmd_selftest(runner: SchedulerRunner, _args) -> int:
    """全链路自检：不联网、不推送，用构造数据验证文案生成与合规拦截。

    这是新人 clone 项目后第一个该跑的命令，能在不消耗任何外部请求的前提下
    确认代码结构完好。
    """
    print("=" * 62)
    print("🔍 全链路自检（离线，不联网、不推送）")
    print("=" * 62)
    failures: List[str] = []

    # --- 1. 配置加载 ---
    print("\n[1/6] 配置加载")
    try:
        cfg = runner.cfg
        assert cfg.location.wcc_city_id == "101121701", \
            f"城市代码错误：{cfg.location.wcc_city_id}（应为 101121701）"
        assert cfg.crawler.min_interval_seconds >= 2.0, "限速间隔小于合规下限 2 秒"
        print(f"  ✅ 城市代码 = {cfg.location.wcc_city_id}（聊城市，已纠正蓝本的 101120701）")
        print(f"  ✅ 限速间隔 = {cfg.crawler.min_interval_seconds}s >= 2s")
    except AssertionError as exc:
        failures.append(f"配置加载：{exc}")
        print(f"  ❌ {exc}")

    # --- 2. 解析器 ---
    print("\n[2/6] JS 变量解析器")
    try:
        from src.crawler.parsers import extract_js_var, to_float, norm_datetime
        sample = 'var cityDZ ={"a":{"b":[1,2,{"c":"x}y"}]}};var z=1;'
        obj = extract_js_var(sample, "cityDZ")
        assert obj == {"a": {"b": [1, 2, {"c": "x}y"}]}}, f"嵌套解析错误：{obj}"
        assert to_float("999") is None, "哨兵值 999 未被拦截"
        assert to_float("") is None, "空值未被拦截"
        assert to_float("23.5") == 23.5, "正常数值解析错误"
        assert norm_datetime("202610030800") == "2026-10-03 08:00", \
            f"时间规范化错误：{norm_datetime('202610030800')}"
        print("  ✅ 嵌套 JSON / 字符串内括号 解析正确")
        print("  ✅ 哨兵值 999、空串 已拦截（不会出现「聊城 999℃」）")
        print("  ✅ 发布时间规范化 202610030800 → 2026-10-03 08:00")
    except (AssertionError, ImportError) as exc:
        failures.append(f"解析器：{exc}")
        print(f"  ❌ {exc}")

    # --- 3. 编码表纪律 ---
    print("\n[3/6] 天气编码表纪律")
    try:
        from src.crawler.weather_codes import (
            WEATHER_CODE_MAP, weather_text_from_code, wind_dir_from_degree)
        assert WEATHER_CODE_MAP["0"] == "晴"
        assert WEATHER_CODE_MAP["1"] == "多云"
        assert WEATHER_CODE_MAP["2"] == "阴"
        assert weather_text_from_code("01") == "多云", "前导零处理错误"
        # 未收录编码必须返回 None（不猜测）
        assert weather_text_from_code("53") is None, \
            "未验证编码竟然返回了值 —— 违反不猜测纪律"
        assert wind_dir_from_degree(185) == "南风", \
            f"风向换算错误：{wind_dir_from_degree(185)}"
        assert wind_dir_from_degree(0) == "北风"
        print(f"  ✅ 已验证编码 {len(WEATHER_CODE_MAP)} 个（29 城实测、零冲突）")
        print("  ✅ 未收录编码返回 None（不猜测，走页面中文兜底）")
        print("  ✅ 风向角度换算正确：185° → 南风")
    except (AssertionError, ImportError) as exc:
        failures.append(f"编码表：{exc}")
        print(f"  ❌ {exc}")

    # --- 4. 文案生成 ---
    print("\n[4/6] 文案生成与法规署名")
    try:
        from src.content_generator import ContentGenerator
        gen = ContentGenerator(display_name="聊城市东昌府区",
                               attribution_name="中央气象台·中国天气网")
        fake = {
            "today": {
                "forecast_date": "2026-10-03", "temp_max": 23.0,
                "temp_min": 12.0, "text_day": "多云", "text_night": "阴",
                "text_full": "多云转阴", "wind_dir_full": "南风转北风",
                "wind_scale_full": "4-5级转3-4级",
                "publish_time": "2026-10-03 08:00",
            },
            "daily": [
                {"forecast_date": "2026-10-03", "temp_max": 23.0,
                 "temp_min": 12.0, "text_day": "多云", "text_night": "阴",
                 "text_full": "多云转阴", "wind_scale_day": "4-5级",
                 "publish_time": "2026-10-03 08:00"},
                {"forecast_date": "2026-10-04", "temp_max": 21.0,
                 "temp_min": 10.0, "text_day": "晴", "text_night": "晴",
                 "text_full": "晴", "wind_scale_day": "3-4级",
                 "publish_time": "2026-10-03 08:00"},
            ],
            "realtime": {"temp": 17.0, "humidity": 45.0, "text": "多云",
                         "wind_dir": "南风", "wind_scale": "3级",
                         "aqi": 51.0, "observed_at": "17:00"},
            "indices": [
                {"index_date": "2026-10-03", "code": "ct", "name": "穿衣指数",
                 "level": "较舒适", "description": "建议穿薄外套或牛仔裤等服装。"},
                {"index_date": "2026-10-03", "code": "gm", "name": "感冒指数",
                 "level": "少发", "description": "无明显降温，感冒几率较低。"},
            ],
        }
        content = gen.generate(publish_time="2026-10-03 08:00", **fake)
        assert content.ok, f"文案生成失败：{content.warnings}"
        full = content.full_text()
        # 《气象法》第二十五条硬性要求
        assert "信息来源" in full, "缺少信息来源署名"
        assert "发布时间" in full, "缺少发布时间"
        assert "中央气象台" in full, "缺少气象台站名称"
        # 官方结论必须原样保留，不得改写
        assert "多云转阴" in full, "官方天气现象文本被改写了"
        assert "23" in full and "12" in full, "官方气温值丢失"
        # 不得出现自主预报表述
        for bad in ("本系统预测", "AI 预测", "我们预计"):
            assert bad not in full, f"出现自主预报表述：{bad}"
        print(f"  ✅ 文案生成成功，标题：{content.title}")
        print(f"  ✅ 署名合规：{content.attribution}")
        print("  ✅ 官方天气现象与气温原样保留（未改写结论）")
        print("  ✅ 无自主预报表述")
        print("\n  --- 文案样例（前 500 字）---")
        for line in full[:500].splitlines():
            print("  │ " + line)
    except (AssertionError, ImportError) as exc:
        failures.append(f"文案生成：{exc}")
        print(f"  ❌ {exc}")

    # --- 5. 合规拦截 ---
    print("\n[5/6] 合规硬性拦截（缺署名必须拒绝推送）")
    try:
        from src.content_generator import ContentGenerator, WeatherContent
        from src.daily_job import DailyJob
        bad = WeatherContent(title="t", body="今天多云，23℃。" * 3,
                             attribution="", ok=True)
        problems = DailyJob._check_compliance(bad)   # noqa: SLF001
        assert problems, "缺少署名的文案竟然通过了合规检查 —— 严重漏洞"
        print(f"  ✅ 缺署名被拦截：{problems[0][:50]}…")

        bad2 = WeatherContent(
            title="t", body="本系统预测明天气温 25℃。" * 2,
            attribution="📌 信息来源：中央气象台　发布时间：2026-10-03 08:00",
            ok=True)
        problems2 = DailyJob._check_compliance(bad2)   # noqa: SLF001
        assert problems2, "含自主预报表述的文案竟然通过了检查"
        print(f"  ✅ 自主预报表述被拦截：{problems2[0][:50]}…")

        good = WeatherContent(
            title="t", body="官方预报：多云转阴，12~23℃。" * 2,
            attribution="📌 信息来源：中央气象台·中国天气网　发布时间：2026-10-03 08:00",
            ok=True)
        problems3 = DailyJob._check_compliance(good)   # noqa: SLF001
        assert not problems3, f"合规文案被误拦：{problems3}"
        print("  ✅ 合规文案正常通过")
    except (AssertionError, ImportError) as exc:
        failures.append(f"合规拦截：{exc}")
        print(f"  ❌ {exc}")

    # --- 6. 存储 ---
    print("\n[6/6] 存储层（临时库，不污染正式数据）")
    try:
        import tempfile
        from src.storage.storage import WeatherStorage, sha256_text
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            st = WeatherStorage(tmp / "t.sqlite", tmp / "raw")
            try:
                # forecast_date 用「今天」，与 latest_daily 的
                # forecast_date >= date('now','localtime') 过滤条件自洽，
                # 避免测试数据过期后查不到。
                today = datetime.now().strftime("%Y-%m-%d")
                rid = st.begin_run()
                n = st.upsert_daily([{
                    "source": "selftest", "city_id": "101121701",
                    "forecast_date": today, "temp_max": 23.0,
                    "temp_min": 12.0, "text_day": "多云", "text_night": "阴",
                    "raw_sha256": sha256_text("selftest"),
                }])
                assert n == 1, f"写入失败 n={n}"
                # UPSERT 幂等性：重复写同一条不应产生第二行
                st.upsert_daily([{
                    "source": "selftest", "city_id": "101121701",
                    "forecast_date": today, "temp_max": 24.0,
                    "temp_min": 13.0, "text_day": "晴", "text_night": "晴",
                    "raw_sha256": sha256_text("selftest2"),
                }])
                rows = st.latest_daily("101121701", days=9999)
                assert len(rows) == 1, \
                    f"UPSERT 失效，产生 {len(rows)} 行重复数据"
                assert rows[0]["temp_max"] == 24.0, "UPSERT 未更新字段"
                st.end_run(rid, "success", ["selftest"], [], "自检", 0, 1)
                # 原始报文归档
                p = st.save_raw("selftest", {"a": 1})
                assert p, "原始报文归档失败"
                stats = st.daily_stats()
            finally:
                # 无论断言是否失败都要关闭连接，
                # 否则 Windows 下 sqlite 句柄未释放会阻止临时目录清理。
                st.close()
        print("  ✅ 建表、写入、UPSERT 幂等、运行日志、原始归档 均正常")
        print(f"  ✅ 库内统计：{stats}")
    except (AssertionError, ImportError, OSError) as exc:
        failures.append(f"存储层：{exc}")
        print(f"  ❌ {exc}")

    # --- 汇总 ---
    print("\n" + "=" * 62)
    if failures:
        print(f"❌ 自检未通过，{len(failures)} 项失败：")
        for f in failures:
            print("  •", f)
        print("=" * 62)
        return 1
    print("✅ 全链路自检通过（6/6）")
    print("=" * 62)
    print("\n下一步建议：")
    print("  1) python main.py --collect   # 联网采集一次，确认数据源可达")
    print("  2) python main.py --dry-run   # 生成文案，人工审阅 logs/dryrun_*.md")
    print("  3) 审阅通过后，在 .env 配置推送渠道，再用 --push 上线")
    return 0


# ============================================================
# 入口
# ============================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="校园气象预报自动编撰系统（聊城市东昌府区）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
典型使用顺序：
  python main.py --selftest     # 离线自检，确认代码完好
  python main.py --collect      # 联网采集一次，验证数据源
  python main.py --dry-run      # 生成文案供人工审阅（不推送）
  python main.py --gzh-pack     # 生成公众号推送包（人工手动群发辅助）
  python main.py --status       # 查看配置与数据库状态
  python main.py --serve        # 启动每日定时调度（真实推送）

⚠️ 不加任何参数时默认执行 --dry-run，不会发送任何消息。
""")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true",
                   help="干跑：采集+生成文案，写 logs/dryrun_<日期>.md，不推送（默认）")
    g.add_argument("--push", action="store_true",
                   help="真实推送一次（需已配置渠道与凭证）")
    g.add_argument("--collect", action="store_true", help="只采集落库")
    g.add_argument("--status", action="store_true", help="查看配置与数据库状态")
    g.add_argument("--serve", action="store_true", help="启动每日定时调度")
    g.add_argument("--selftest", action="store_true",
                   help="离线全链路自检（不联网、不推送）")
    g.add_argument("--samples", type=int, default=0, metavar="N",
                   help="生成 N 天干跑样例文案（预演，数据来自官方真实预报），"
                        "如 --samples 3")
    g.add_argument("--gzh-pack", action="store_true",
                   help="生成公众号推送包（未认证订阅号人工手动群发辅助，"
                        "产出可粘贴图文文件，不发送消息）")
    p.add_argument("--preview-openid", action="append", default=None,
                   help="预览接收者 openid（最多 5 人），走预览接口不消耗群发额度")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    cfg = load_config()
    runner = SchedulerRunner(cfg)

    if args.selftest:
        return cmd_selftest(runner, args)
    if args.samples:
        return cmd_samples(runner, args)
    if args.gzh_pack:
        return cmd_gzh_pack(runner, args)
    if args.collect:
        return cmd_collect(runner, args)
    if args.status:
        return cmd_status(runner, args)
    if args.serve:
        return cmd_serve(runner, args)
    if args.push:
        return cmd_push(runner, args)
    # 默认：干跑
    return cmd_dryrun(runner, args)


if __name__ == "__main__":
    sys.exit(main())
