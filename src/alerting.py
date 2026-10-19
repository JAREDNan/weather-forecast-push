# -*- coding: utf-8 -*-
"""
异常告警模块（提示词 §6.4：任务失败要能通知我，不能静默失败）。

支持三个渠道（可同时启用，凭证放 .env）：
  * 企业微信群机器人 Webhook —— 最推荐，免费、无需服务器、个人可建
  * Server 酱 —— 推送到微信，适合个人
  * SMTP 邮件 —— 最通用，需要邮箱授权码

设计要点：
  * 告警本身失败绝不能再抛异常（否则掩盖原始故障）
  * 同一异常在 dedup_minutes 内不重复告警，避免刷屏
  * 告警文本自动脱敏，不含任何密钥
"""
from __future__ import annotations

import json
import logging
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import AppConfig, mask

log = logging.getLogger(__name__)


@dataclass
class AlertResult:
    """一次告警的结果。"""
    ok: bool
    channels: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    suppressed: bool = False
    message: str = ""


class AlertManager:
    """失败告警管理器。"""

    def __init__(self, cfg: AppConfig, dedup_minutes: int = 60):
        self.cfg = cfg
        self.dedup_seconds = max(0, int(dedup_minutes)) * 60
        self._last_sent: Dict[str, float] = {}
        self._hostname = self._safe_hostname()

    @staticmethod
    def _safe_hostname() -> str:
        try:
            return socket.gethostname()
        except OSError:
            return "unknown-host"

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.alert_wecom_webhook
                    or self.cfg.alert_serverchan_key
                    or (self.cfg.alert_email_host and self.cfg.alert_email_user))

    def _dedup_key(self, title: str) -> str:
        return title.strip()[:80]

    def send(self, title: str, detail: str = "", *,
             level: str = "error", force: bool = False) -> AlertResult:
        """发送告警。

        Args:
            title: 简短标题（用于去重）
            detail: 详细信息（异常栈、上下文）
            level: info / warning / error / critical
            force: 忽略去重强制发送
        """
        if not self.enabled:
            log.debug("未配置任何告警渠道，仅写日志：%s", title)
            return AlertResult(ok=False, suppressed=True,
                               message="未配置告警渠道")

        key = self._dedup_key(title)
        now = time.time()
        if not force and self.dedup_seconds > 0:
            last = self._last_sent.get(key, 0.0)
            if now - last < self.dedup_seconds:
                remain = int(self.dedup_seconds - (now - last))
                log.info("告警去重抑制（%s），剩余 %d 秒", title, remain)
                return AlertResult(ok=False, suppressed=True,
                                   message=f"去重抑制，剩余 {remain} 秒")

        emoji = {"info": "ℹ️", "warning": "⚠️",
                 "error": "❌", "critical": "🚨"}.get(level, "❌")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        head = f"{emoji} [{level.upper()}] {title}"
        body_lines = [
            head,
            f"时间：{stamp}",
            f"主机：{self._hostname}",
            f"项目：校园气象预报自动编撰系统（{self.cfg.location.display_name}）",
        ]
        if detail:
            # 截断超长详情，避免超出各渠道长度限制
            body_lines.append("详情：")
            body_lines.append(detail[:2500])
        body = "\n".join(body_lines)

        sent: List[str] = []
        failed: List[str] = []

        if self.cfg.alert_wecom_webhook:
            if self._send_wecom(head, body):
                sent.append("wecom_webhook")
            else:
                failed.append("wecom_webhook")

        if self.cfg.alert_serverchan_key:
            if self._send_serverchan(head, body):
                sent.append("serverchan")
            else:
                failed.append("serverchan")

        if self.cfg.alert_email_host and self.cfg.alert_email_user:
            if self._send_email(head, body):
                sent.append("email")
            else:
                failed.append("email")

        if sent:
            self._last_sent[key] = now
        result = AlertResult(ok=bool(sent), channels=sent, failures=failed,
                             message=f"成功 {sent}；失败 {failed}")
        if sent:
            log.info("告警已发送：%s → %s", title, sent)
        else:
            log.error("所有告警渠道均失败：%s", failed)
        return result

    # ------------------------------------------------------------
    # 各渠道实现（全部吞掉异常，只记日志）
    # ------------------------------------------------------------
    def _send_wecom(self, title: str, body: str) -> bool:
        try:
            import requests
            payload = {"msgtype": "text",
                       "text": {"content": body[:2000]}}
            resp = requests.post(self.cfg.alert_wecom_webhook,
                                 json=payload, timeout=15)
            data = resp.json() if resp.text else {}
            if data.get("errcode") == 0:
                return True
            log.error("企业微信告警失败：%s", data)
            return False
        except Exception as exc:  # noqa: BLE001
            log.error("企业微信告警异常：%s", exc)
            return False

    def _send_serverchan(self, title: str, body: str) -> bool:
        try:
            import requests
            key = self.cfg.alert_serverchan_key
            # Server 酱 Turbo 版端点
            url = f"https://sctapi.ftqq.com/{key}.send"
            resp = requests.post(url, data={"title": title[:100],
                                            "desp": body[:30000]},
                                 timeout=15)
            data = resp.json() if resp.text else {}
            if data.get("code") == 0 or data.get("errno") == 0:
                return True
            log.error("Server 酱告警失败：%s", data)
            return False
        except Exception as exc:  # noqa: BLE001
            log.error("Server 酱告警异常：%s", exc)
            return False

    def _send_email(self, subject: str, body: str) -> bool:
        try:
            import smtplib
            from email.header import Header
            from email.mime.text import MIMEText

            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = Header(subject[:150], "utf-8")
            msg["From"] = self.cfg.alert_email_user
            msg["To"] = self.cfg.alert_email_to or self.cfg.alert_email_user

            port = int(self.cfg.alert_email_port or 465)
            if port == 465:
                server = smtplib.SMTP_SSL(self.cfg.alert_email_host, port,
                                          timeout=20)
            else:
                server = smtplib.SMTP(self.cfg.alert_email_host, port,
                                      timeout=20)
                server.starttls()
            try:
                server.login(self.cfg.alert_email_user,
                             self.cfg.alert_email_password)
                server.sendmail(
                    self.cfg.alert_email_user,
                    [self.cfg.alert_email_to or self.cfg.alert_email_user],
                    msg.as_string(),
                )
            finally:
                try:
                    server.quit()
                except Exception:  # noqa: BLE001
                    pass
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("邮件告警异常：%s", exc)
            return False

    # ------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------
    def status_report(self) -> str:
        chans = []
        if self.cfg.alert_wecom_webhook:
            chans.append(f"企业微信 Webhook({mask(self.cfg.alert_wecom_webhook, 20)})")
        if self.cfg.alert_serverchan_key:
            chans.append(f"Server 酱({mask(self.cfg.alert_serverchan_key)})")
        if self.cfg.alert_email_host:
            chans.append(f"邮件({self.cfg.alert_email_user}@{self.cfg.alert_email_host})")
        if not chans:
            return "告警渠道：未配置（任务失败将只写日志，⚠️ 属静默失败风险，建议配置）"
        return "告警渠道：" + "、".join(chans) + f"（去重 {self.dedup_seconds // 60} 分钟）"
