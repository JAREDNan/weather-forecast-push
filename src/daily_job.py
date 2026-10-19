# -*- coding: utf-8 -*-
"""
每日任务主流程（提示词 PDF 模块四的执行体）。

被 main.py（单次执行 / 干跑）与 scheduler_main.py（定时调度）共同调用，
保证「手动跑一次」与「定时自动跑」走的是**完全相同的代码路径**，
避免两条路径行为不一致导致上线后翻车。

流程：采集 → 交叉校验 → 落库 → 生成文案 → 推送 → 记录结果
"""
from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .alerting import AlertManager
from .config import PROJECT_ROOT, AppConfig, load_config
from .content_generator import WeatherContent
from .wechat_manager import PushResult, WeChatManager
from .weather_client import WeatherBundle, WeatherClient

log = logging.getLogger(__name__)


@dataclass
class JobResult:
    """一次每日任务的完整结果。"""
    started_at: str = ""
    finished_at: str = ""
    ok: bool = False
    dry_run: bool = False
    stage: str = "init"          # 失败发生在哪个阶段
    # 采集
    sources_ok: List[str] = field(default_factory=list)
    sources_fail: List[str] = field(default_factory=list)
    from_cache: bool = False
    # 文案
    title: str = ""
    body_length: int = 0
    attribution_ok: bool = False
    content_warnings: List[str] = field(default_factory=list)
    # 推送
    push: Optional[PushResult] = None
    # 诊断
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            "=" * 62,
            f"每日任务结果：{'✅ 成功' if self.ok else '❌ 失败'}"
            f"{'（dry-run）' if self.dry_run else ''}",
            "=" * 62,
            f"开始时间  : {self.started_at}",
            f"结束时间  : {self.finished_at}",
            f"失败阶段  : {self.stage if not self.ok else '—'}",
            f"成功数据源: {self.sources_ok or '—'}",
            f"失败数据源: {self.sources_fail or '—'}",
            f"使用缓存  : {'是' if self.from_cache else '否'}",
            f"文案标题  : {self.title or '—'}",
            f"正文字数  : {self.body_length}",
            f"合规署名  : {'✅ 含信息来源与发布时间' if self.attribution_ok else '❌ 缺失'}",
            f"推送结果  : {(self.push.message if self.push else '未推送')}",
        ]
        if self.content_warnings:
            lines.append("文案警告  : " + "；".join(self.content_warnings[:5]))
        if self.warnings:
            lines.append("运行警告  : " + "；".join(self.warnings[:8]))
        if self.errors:
            lines.append("错误      : " + "；".join(self.errors[:5]))
        return "\n".join(lines)


class DailyJob:
    """每日气象播报任务。"""

    def __init__(self, cfg: Optional[AppConfig] = None,
                 alert: Optional[AlertManager] = None,
                 push_manager: Optional[WeChatManager] = None):
        self.cfg = cfg or load_config()
        self.alert = alert or AlertManager(self.cfg)
        self.pusher = push_manager or WeChatManager(self.cfg)

    # ------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------
    def run(self, *, dry_run: bool = False,
            cover_path: Optional[Path] = None,
            preview_openids: Optional[List[str]] = None,
            include_hourly: bool = True,
            include_indices: bool = True) -> JobResult:
        """执行一次完整流程。

        Args:
            dry_run: True 则只生成文案写入 logs/dryrun_<日期>.md，绝不真实推送。
                     对应提示词 §6.2：dry-run 必须先跑通并经用户确认，
                     才允许开启真实推送。
            cover_path: 封面图路径（仅订阅号群发需要）
            preview_openids: 预览接收者 openid（首次真实推送前只发给自己，
                     对应提示词 §6.3；走预览接口不消耗群发额度）
        """
        result = JobResult(
            started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            dry_run=dry_run,
        )
        # dry-run 强制生效：即使配置了真实渠道也不发送
        effective_dry_run = dry_run or (self.pusher.channel == "dryrun")

        client: Optional[WeatherClient] = None
        try:
            # ---------- 阶段 1：采集 ----------
            result.stage = "collect"
            client = WeatherClient(self.cfg)
            bundle = client.fetch()
            result.sources_ok = list(bundle.sources_ok)
            result.sources_fail = list(bundle.sources_fail)
            result.from_cache = bundle.from_cache
            result.warnings.extend(bundle.warnings)

            if not bundle.ok:
                result.errors.append(
                    "所有数据源与缓存均无有效数据，本次不生成文案、不推送")
                self._fail(result, bundle=bundle,
                           reason="采集失败：无任何有效数据")
                return result

            # ---------- 阶段 2：生成文案 ----------
            result.stage = "content"
            content = client.generate_content(
                bundle, include_hourly=include_hourly,
                include_indices=include_indices)
            result.content_warnings = list(content.warnings)
            result.title = content.title
            result.body_length = len(content.body)

            if not content.ok:
                result.errors.append("文案生成失败（无官方预报数据，拒绝编造）")
                self._fail(result, bundle=bundle, content=content,
                           reason="文案生成失败")
                return result

            # ---------- 阶段 3：合规校验（硬性拦截）----------
            result.stage = "compliance"
            compliance_problems = self._check_compliance(content)
            if compliance_problems:
                result.errors.extend(compliance_problems)
                result.attribution_ok = False
                # ⚠️ 缺少法定署名属违法（《气象法》第三十八条、
                #    《办法》第十四条可处罚款），必须拦截而非带病推送
                self._fail(result, bundle=bundle, content=content,
                           reason="合规校验未通过：" + "；".join(compliance_problems))
                return result
            result.attribution_ok = True

            # ---------- 阶段 4：推送 ----------
            result.stage = "push"
            body = content.full_text()
            if effective_dry_run:
                push_res = self.pusher.write_dryrun(
                    content.title, body, content.summary,
                    cover_path or self._find_cover(),
                    result.started_at,
                    note="" if dry_run else "（渠道未配置）")
            else:
                push_res = self.pusher.push(
                    title=content.title, body=body, digest=content.summary,
                    cover_path=cover_path or self._find_cover(),
                    openid_list=preview_openids)
            result.push = push_res

            if not push_res.ok:
                result.errors.append(f"推送失败：{push_res.message}")
                self._fail(result, bundle=bundle, content=content,
                           reason=f"推送失败：{push_res.message}")
                return result

            result.stage = "done"
            result.ok = True
            log.info("每日任务完成：%s", result.title)
            return result

        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            log.exception("每日任务发生未预期异常")
            result.errors.append(f"{type(exc).__name__}: {exc}")
            result.warnings.append("异常栈已记录到日志")
            self._fail(result, reason=f"任务异常（阶段 {result.stage}）：{exc}",
                       detail=tb)
            return result
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            result.finished_at = datetime.now().astimezone().isoformat(
                timespec="seconds")

    # ------------------------------------------------------------
    # 合规校验
    # ------------------------------------------------------------
    @staticmethod
    def _check_compliance(content: WeatherContent) -> List[str]:
        """推送前的法规合规硬性检查。

        依据（详见 reports/02_爬虫合规检查.md 第五节）：
          * 《气象法》第二十五条：传播气象预报必须标明发布时间和气象台站名称
          * 《气象预报发布与传播管理办法》第十四条：不注明发布单位名称和
            发布时间的，可并处 3 万元以下罚款
          * 《办法》第九条：不得自行更改气象预报的内容和结论

        返回问题清单；非空则必须拦截推送。
        """
        problems: List[str] = []
        text = content.full_text()

        # 1) 必须有「信息来源」字样（气象台站名称）
        if "信息来源" not in text and "来源" not in content.attribution:
            problems.append(
                "文案缺少「信息来源」署名，违反《气象法》第二十五条，禁止推送")

        # 2) 必须有发布时间
        if not content.attribution or "发布时间" not in content.attribution:
            problems.append(
                "文案缺少「发布时间」，违反《气象法》第二十五条，禁止推送")
        elif "未知" in content.attribution or "None" in content.attribution:
            problems.append(
                "文案署名的发布时间无效（值为「未知」或 None），禁止推送")

        # 3) 不得出现自主预报/模型预测的表述
        forbidden = ("本系统预测", "AI 预测", "模型预测", "我们预计",
                     "我方预报", "自主预报")
        hit = [w for w in forbidden if w in text]
        if hit:
            problems.append(
                f"文案含自主预报表述 {hit}，违反《气象法》第二十二条"
                f"（非气象台站不得发布气象预报）与《办法》第十条，禁止推送")

        # 4) 不得出现明显的占位/编造痕迹
        if "⚠️ 未验证编码" in text:
            problems.append("文案含未验证编码占位符，说明数据源结构变化，禁止推送")

        # 5) 空正文
        if not content.body or len(content.body.strip()) < 20:
            problems.append("正文过短或为空，禁止推送空内容")

        return problems

    # ------------------------------------------------------------
    # 失败处理
    # ------------------------------------------------------------
    def _fail(self, result: JobResult, reason: str, detail: str = "",
              bundle: Optional[WeatherBundle] = None,
              content: Optional[WeatherContent] = None) -> None:
        """记录失败并发告警。⚠️ 绝不静默失败（提示词 §6.4）。"""
        result.ok = False
        if detail:
            log.error("%s\n%s", reason, detail)
        else:
            log.error("%s", reason)

        # 汇总上下文，便于排查
        ctx_parts = [reason]
        if bundle:
            ctx_parts.append(f"成功源={bundle.sources_ok} 失败源={bundle.sources_fail} "
                             f"缓存={bundle.from_cache}")
            if bundle.warnings:
                ctx_parts.append("采集警告：" + "；".join(bundle.warnings[:6]))
        if content and content.warnings:
            ctx_parts.append("文案警告：" + "；".join(content.warnings[:6]))
        if result.push:
            ctx_parts.append(f"推送：{result.push.message}")
        if detail:
            ctx_parts.append("异常栈：\n" + detail[-1500:])

        self.alert.send(
            title=f"气象播报任务失败（{result.stage} 阶段）",
            detail="\n".join(ctx_parts),
            level="error",
        )

    # ------------------------------------------------------------
    # 封面图
    # ------------------------------------------------------------
    def _find_cover(self) -> Optional[Path]:
        """查找封面图：优先 config.ini 指定路径，其次 assets/ 下默认封面。

        相对路径基于项目根目录解析，不依赖运行时 cwd。
        """
        candidates: List[Path] = []
        configured = (self.cfg.cover_image_path or "").strip()
        if configured:
            p = Path(configured)
            candidates.append(p if p.is_absolute() else PROJECT_ROOT / p)
        assets = PROJECT_ROOT / "assets"
        for name in ("cover.jpg", "cover.jpeg", "cover.png"):
            candidates.append(assets / name)

        for c in candidates:
            try:
                if c.exists() and c.is_file():
                    return c
            except OSError:
                continue
        if configured:
            log.warning("配置的封面图不存在：%s（将以无封面方式推送）", configured)
        return None
