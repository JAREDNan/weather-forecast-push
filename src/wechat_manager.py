# -*- coding: utf-8 -*-
"""
微信推送管理（提示词 PDF 模块三）。

═══════════════════════════════════════════════════════════════
⚠️ 重大方案缺陷与修正说明（提示词 §3.3 强制要求先解决）
═══════════════════════════════════════════════════════════════

原 PDF 方案：已认证服务号 + wechatpy，每天 07:30 走
「上传封面图 → 创建永久图文素材 → message/mass/sendall 群发」。

该方案**必然失败**，官方依据：
  * 腾讯客服明确：服务号（认证与非认证均同）「1 个月（按自然月）内可群发 4 次消息」
    来源 https://kf.qq.com/faq/120911VrYVrA150921mUbeya.html
  * 每日 1 次 × 每月 4 次额度 ⇒ **第 5 天额度耗尽，推送中断**
  * 且群发次数官方不支持增加

同时核实到两个关键事实（详见 reports/02_爬虫合规检查.md）：
  1. 「发布能力」（draft/add + freepublish/submit）**不限次数、不消耗群发额度**，
     但**用户收不到任何推送通知**，文章仅出现在公众号主页历史消息中
     ⇒ 不能用它替代群发实现主动触达。
     来源 https://developers.weixin.qq.com/doc/service/guide/product/publish.html
  2. 订阅号**每天可群发 1 次**（每天 0 点重置，不累加）
     ⇒ 这是唯一同时满足「每日推送 + 用户收到通知 + 零成本 + 个人可注册」的路线。
     来源同上述腾讯客服 FAQ

因此本模块实现为**多路线可切换**，由用户在 .env 的 WECHAT_PUSH_CHANNEL 指定：
  subscription   订阅号每日群发（调研推荐首选，需订阅号 AppID/AppSecret）
  template       服务号模板消息（日上限 10 万，需认证服务号 + 行业类目，
                 ⚠️ 天气提醒是否属合规「服务通知」场景未经官方确认，有被判违规风险）
  wecom_webhook  企业微信群机器人（20 条/分钟、无月度额度、免费、个人可建，
                 触达仅限群成员；适合校内班级群/社团群）
  dryrun         仅生成文案写入 logs/dryrun_<日期>.md，不做任何真实发送（默认）

⚠️ 提示词 §9 明确要求：推送路线选型「强制确认，不得自行决定」。
   故本模块**默认 dryrun**，且任何真实推送前都会二次检查配置，
   未经用户显式配置渠道与凭证，绝不会发出任何消息。

⚠️ 禁止事项（提示词 §10）：不使用 itchat / wxpy / Wechaty 等微信个人号协议。
   这类方案依赖已被微信封禁的网页版个人号登录，属不可用路线且有封号风险。
   本模块仅使用微信官方 API。

⚠️ IP 白名单：调用公众号接口获取 access_token 强制要求配置服务器 IP 白名单，
   否则返回 errcode 40164。部署后需把服务器公网 IP 加入公众号后台白名单。
   来源 https://developers.weixin.qq.com/doc/oplatform/developers/basic_func/ip_whitelist.html
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import AppConfig, mask

log = logging.getLogger(__name__)

# 支持的推送渠道
CHANNEL_DRYRUN = "dryrun"
CHANNEL_SUBSCRIPTION = "subscription"
CHANNEL_TEMPLATE = "template"
CHANNEL_WECOM = "wecom_webhook"
VALID_CHANNELS = {CHANNEL_DRYRUN, CHANNEL_SUBSCRIPTION, CHANNEL_TEMPLATE,
                  CHANNEL_WECOM}

# 微信官方 API 端点
WX_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
WX_MASS_SENDALL = "https://api.weixin.qq.com/cgi-bin/message/mass/sendall"
WX_MATERIAL_ADD = "https://api.weixin.qq.com/cgi-bin/material/add_material"
WX_NEWS_ADD = "https://api.weixin.qq.com/cgi-bin/material/add_news"
WX_TEMPLATE_SEND = "https://api.weixin.qq.com/cgi-bin/message/template/send"
WX_PREVIEW = "https://api.weixin.qq.com/cgi-bin/message/mass/preview"
WECOM_RATE_LIMIT = "20 条/分钟"


@dataclass
class PushResult:
    """一次推送的结果。"""
    channel: str
    ok: bool
    dry_run: bool = False
    message: str = ""
    errcode: Optional[int] = None
    raw_response: Optional[Dict[str, Any]] = None
    log_file: Optional[str] = None
    sent_at: str = ""
    warnings: List[str] = field(default_factory=list)


class WeChatPushError(Exception):
    """微信推送异常。"""


class WeChatManager:
    """微信推送管理器（多路线）。

    设计要点：
      * wechatpy / requests 均为惰性导入：未安装依赖或未配置凭证时，
        采集与文案生成仍可正常运行，dry-run 完全可用。
      * 默认 dryrun，真实推送需显式配置渠道 + 凭证。
      * 首次真实推送前提示词 §6 要求「只发给自己」，
        本模块提供 send_preview() 走微信「预览」接口（不消耗群发额度），
        供上线前人工核对排版。
    """

    def __init__(self, cfg: AppConfig, logs_dir: Optional[Path] = None):
        self.cfg = cfg
        self.logs_dir = Path(logs_dir or cfg.logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self.channel = (cfg.wechat_push_channel or CHANNEL_DRYRUN).lower()
        if self.channel not in VALID_CHANNELS:
            log.warning("未知推送渠道 %r，回退为 dryrun", self.channel)
            self.channel = CHANNEL_DRYRUN

    # ------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------
    @property
    def will_really_send(self) -> bool:
        """是否会真实发送消息（非 dryrun 且凭证齐备）。"""
        return self.channel != CHANNEL_DRYRUN and self._channel_ready()

    def _channel_ready(self) -> bool:
        if self.channel == CHANNEL_WECOM:
            return bool(self.cfg.wecom_webhook_url)
        if self.channel == CHANNEL_TEMPLATE:
            return bool(self.cfg.wechat_app_id and self.cfg.wechat_app_secret
                        and self.cfg.wechat_template_id)
        if self.channel == CHANNEL_SUBSCRIPTION:
            return bool(self.cfg.wechat_app_id and self.cfg.wechat_app_secret)
        return False

    def status_report(self) -> str:
        """返回当前推送配置状态（脱敏），用于启动日志与运维手册。"""
        lines = [
            f"推送渠道      : {self.channel}",
            f"会真实发送    : {'是' if self.will_really_send else '否'}",
            f"AppID         : {mask(self.cfg.wechat_app_id, 6)}",
            f"AppSecret     : {mask(self.cfg.wechat_app_secret)}",
            f"企业微信 Webhook: {mask(self.cfg.wecom_webhook_url, 24) if self.cfg.wecom_webhook_url else '<未配置>'}",
            f"模板 ID       : {mask(self.cfg.wechat_template_id, 8)}",
        ]
        if not self._channel_ready() and self.channel != CHANNEL_DRYRUN:
            lines.append(f"⚠️ 渠道 {self.channel} 凭证不齐备，将自动降级为 dryrun")
        return "\n".join(lines)

    # ------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------
    def push(self, *, title: str, body: str,
             digest: str = "", cover_path: Optional[Path] = None,
             openid_list: Optional[List[str]] = None) -> PushResult:
        """推送文案。

        Args:
            title: 标题
            body: 正文（已含署名）
            digest: 摘要（订阅号图文摘要）
            cover_path: 封面图路径（订阅号群发需要永久素材 thumb_media_id）
            openid_list: 指定接收者 openid（仅预览/定向推送用）

        Returns:
            PushResult；任何失败都不抛异常，由调用方决定是否告警。
        """
        sent_at = datetime.now().astimezone().isoformat(timespec="seconds")

        # 未配置真实渠道 → dry-run
        if self.channel == CHANNEL_DRYRUN or not self._channel_ready():
            reason = "" if self.channel == CHANNEL_DRYRUN else \
                f"（渠道 {self.channel} 凭证不齐备，自动降级）"
            return self._write_dryrun(title, body, digest, cover_path,
                                      sent_at, note=reason)

        try:
            if self.channel == CHANNEL_WECOM:
                return self._push_wecom(title, body, sent_at)
            if self.channel == CHANNEL_TEMPLATE:
                return self._push_template(title, body, sent_at, openid_list)
            if self.channel == CHANNEL_SUBSCRIPTION:
                return self._push_subscription(title, body, digest,
                                               cover_path, sent_at, openid_list)
        except WeChatPushError as exc:
            log.error("推送失败：%s", exc)
            return PushResult(channel=self.channel, ok=False,
                              message=str(exc), sent_at=sent_at)
        except Exception as exc:  # noqa: BLE001
            log.exception("推送发生未预期异常")
            return PushResult(channel=self.channel, ok=False,
                              message=f"{type(exc).__name__}: {exc}",
                              sent_at=sent_at)

        return PushResult(channel=self.channel, ok=False,
                          message=f"未实现的渠道 {self.channel}", sent_at=sent_at)

    # ------------------------------------------------------------
    # dry-run：写入 logs/dryrun_<日期>.md
    # ------------------------------------------------------------
    def write_dryrun(self, title: str, body: str, digest: str = "",
                     cover_path: Optional[Path] = None,
                     sent_at: Optional[str] = None,
                     note: str = "",
                     day_label: Optional[str] = None) -> PushResult:
        """公开的干跑入口：只写文件，绝不发送任何消息。

        Args:
            day_label: 文件名日期标签（YYYYMMDD）。默认取运行当天；
                生成「以未来某日为基准日」的预演样例时应传入基准日，
                使每天样例各自成文件，便于人工审阅。
        """
        return self._write_dryrun(
            title, body, digest, cover_path,
            sent_at or datetime.now().astimezone().isoformat(timespec="seconds"),
            note=note, day_label=day_label)

    def _write_dryrun(self, title: str, body: str, digest: str,
                      cover_path: Optional[Path], sent_at: str,
                      note: str = "",
                      day_label: Optional[str] = None) -> PushResult:
        """干跑：把标题+正文+封面建议写入 logs/dryrun_<日期>.md 供人工审阅。

        对应提示词 §6.2：dry-run 必须先跑通并经用户确认，才允许开启真实推送。
        同一天多次运行会追加（保留历史），不覆盖。
        """
        day = (day_label or datetime.now().strftime("%Y%m%d")).replace("-", "")
        path = self.logs_dir / f"dryrun_{day}.md"
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cover_txt = ""
        if cover_path:
            cover_txt = f"- 封面图文件: `{cover_path}`\n"
        is_rehearsal = bool(day_label) and day != datetime.now().strftime("%Y%m%d")

        block = f"""
---

## 🕐 {'预演样例' if is_rehearsal else '干跑记录'} {stamp}

- 基准日: {day[:4]}-{day[4:6]}-{day[6:8]}{'（预演，非当天真实运行）' if is_rehearsal else ''}
- 渠道: `dryrun`{note}
- 标题: {title}
- 摘要: {digest or '（无）'}
- 正文字数: {len(body)}
- 合规署名: {'✅ 已包含' if ('信息来源' in body and '发布时间' in body) else '❌ 缺失，需检查'}
{cover_txt}
### 推送正文预览

```text
{title}

{body}
```
"""
        try:
            header = ""
            if not path.exists():
                header = (f"# 干跑样例文案 {day[:4]}-{day[4:6]}-{day[6:8]}\n\n"
                          f"> 本文件由 `--dry-run` / `--samples` 模式生成，"
                          f"**未发生任何真实推送**。\n"
                          f"> 请人工审阅文案内容与排版，确认无误后再配置真实推送渠道。\n"
                          f"> 审阅要点：①气温/天气现象是否与官方一致 "
                          f"②是否含「信息来源 + 发布时间」署名 "
                          f"③是否误改了官方预报结论 ④措辞是否得体\n\n"
                          f"> ⚠️ 条目性质说明：标注「预演样例」的条目，是用官方真实预报数据\n"
                          f"> 按未来某日基准日预演生成（实时采集从今天才开始，"
                          f"不可能有 3 天真实运行历史）；\n"
                          f"> 标注「干跑记录」的条目才是当天真实运行产生的记录。\n"
                          f"> 两者的气象数据均来自官方真实预报，区别仅在于运行的实际日期。\n")
            with path.open("a", encoding="utf-8") as f:
                f.write(header + block)
            log.info("干跑文案已写入 %s", path)
            return PushResult(channel=CHANNEL_DRYRUN, ok=True, dry_run=True,
                              message="dry-run 完成，未真实发送",
                              log_file=str(path), sent_at=sent_at)
        except OSError as exc:
            log.error("干跑文件写入失败：%s", exc)
            return PushResult(channel=CHANNEL_DRYRUN, ok=False, dry_run=True,
                              message=f"写入失败：{exc}", sent_at=sent_at)

    # ------------------------------------------------------------
    # 路线 A：企业微信群机器人 Webhook
    # ------------------------------------------------------------
    def _push_wecom(self, title: str, body: str, sent_at: str) -> PushResult:
        """企业微信群机器人推送（免费、无月度额度、20 条/分钟）。

        优点：个人即可免费注册企业微信建群；开发极简（一个 POST）。
        局限：触达仅限群成员，需引导用户加群。
        """
        import requests  # 惰性导入

        url = self.cfg.wecom_webhook_url
        # markdown 消息在部分客户端渲染有限，text 最稳；正文超长则截断
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": f"### {title}\n\n{body}"},
        }
        content_len = len(payload["markdown"]["content"])
        if content_len > 4000:   # 企业微信 markdown 上限约 4096 字节
            payload = {"msgtype": "text",
                       "text": {"content": f"{title}\n\n{body[:3000]}\n……(内容过长已截断)"}}

        try:
            resp = requests.post(url, json=payload, timeout=20)
            data = resp.json() if resp.text else {}
        except (requests.RequestException, json.JSONDecodeError) as exc:
            raise WeChatPushError(f"企业微信 Webhook 请求失败：{exc}") from exc

        errcode = data.get("errcode")
        ok = (errcode == 0)
        msg = data.get("errmsg", "")
        if not ok:
            log.error("企业微信推送失败 errcode=%s errmsg=%s", errcode, msg)
        else:
            log.info("企业微信推送成功：%s", title)
        return PushResult(channel=CHANNEL_WECOM, ok=ok, message=msg or "success",
                          errcode=errcode, raw_response=data, sent_at=sent_at)

    # ------------------------------------------------------------
    # 路线 B：服务号模板消息
    # ------------------------------------------------------------
    def _push_template(self, title: str, body: str, sent_at: str,
                       openid_list: Optional[List[str]]) -> PushResult:
        """服务号模板消息。

        额度：日调用上限 10 万次（粉丝量更大时提升），不占群发额度。
        ⚠️ 风险提示：官方文档明确模板消息「只能用于符合其要求的服务场景」，
           不支持广告营销类消息。天气提醒是否属合规服务场景，
           官方服务类目列表中未找到「气象/天气」相关类目 ⇒ ⚠️ 未核实。
           需用户在公众号后台实际查看可选类目确认，否则有被判违规的风险。
        ⚠️ 模板消息需要接收者的 openid。本模块遵循最小必要原则：
           openid 仅本地存储、不上传第三方。当前项目未实现订阅收集流程，
           故 openid_list 为空时不会发送任何消息。
        """
        if not openid_list:
            return PushResult(
                channel=CHANNEL_TEMPLATE, ok=False, sent_at=sent_at,
                message="未提供接收者 openid，且本项目未实现订阅收集流程。"
                        "模板消息无法向全部粉丝定向发送，请先确认 openid 来源与合规性")

        token = self._get_access_token()
        if not token:
            return PushResult(channel=CHANNEL_TEMPLATE, ok=False, sent_at=sent_at,
                              message="获取 access_token 失败")

        import requests  # 惰性导入

        results: List[Dict[str, Any]] = []
        ok_count = 0
        for oid in openid_list[:100]:   # 单次批量上限保护
            payload = {
                "touser": oid,
                "template_id": self.cfg.wechat_template_id,
                "data": {
                    "first": {"value": title, "color": "#173177"},
                    "keyword1": {"value": body[:200], "color": "#173177"},
                    "remark": {"value": "详情见公众号最新推送", "color": "#999999"},
                },
            }
            try:
                resp = requests.post(
                    f"{WX_TEMPLATE_SEND}?access_token={token}",
                    json=payload, timeout=20)
                data = resp.json() if resp.text else {}
                results.append({"openid_masked": mask(oid, 4),
                                "errcode": data.get("errcode"),
                                "errmsg": data.get("errmsg")})
                if data.get("errcode") == 0:
                    ok_count += 1
            except (requests.RequestException, json.JSONDecodeError) as exc:
                results.append({"openid_masked": mask(oid, 4), "error": str(exc)})

        ok = ok_count > 0
        msg = f"模板消息：成功 {ok_count}/{len(openid_list)}"
        log.info("%s", msg)
        return PushResult(channel=CHANNEL_TEMPLATE, ok=ok, message=msg,
                          raw_response={"details": results}, sent_at=sent_at)

    # ------------------------------------------------------------
    # 路线 C：订阅号每日群发
    # ------------------------------------------------------------
    def _push_subscription(self, title: str, body: str, digest: str,
                           cover_path: Optional[Path], sent_at: str,
                           openid_list: Optional[List[str]]) -> PushResult:
        """订阅号图文群发（每天 1 次，0 点重置）。

        流程（原 PDF 三步流，已修正为订阅号可用形态）：
          1. 上传封面图为**永久素材**，取 thumb_media_id
          2. 创建图文（add_news）取 media_id
          3. message/mass/sendall 群发（filter.is_to_all=true）

        ⚠️ 额度提醒：订阅号每天仅 1 次群发，本系统每天只推 1 次，
           若当天已手动群发过，本次会失败（errcode 45009 等）。
        ⚠️ 若传了 openid_list，则走 preview 接口定向预览（不消耗群发额度），
           供上线前人工核对排版（提示词 §6.3：首次真实推送前只发给自己）。
        """
        token = self._get_access_token()
        if not token:
            return PushResult(channel=CHANNEL_SUBSCRIPTION, ok=False,
                              sent_at=sent_at, message="获取 access_token 失败")

        import requests  # 惰性导入

        # ---- 步骤 1：封面图 ----
        thumb_media_id = None
        if cover_path and Path(cover_path).exists():
            thumb_media_id = self._upload_thumb(token, Path(cover_path))
            if not thumb_media_id:
                log.warning("封面图上传失败，图文将无封面")
        else:
            log.warning("未提供封面图或文件不存在（%s），图文将无封面", cover_path)

        # ---- 步骤 2：创建图文 ----
        article = {
            "title": title[:64],
            "content": self._to_html(body),
            "digest": (digest or body[:120])[:120],
            "show_cover_pic": 1 if thumb_media_id else 0,
            "need_open_comment": 0,
            "only_fans_can_comment": 0,
        }
        if thumb_media_id:
            article["thumb_media_id"] = thumb_media_id

        if openid_list:
            # 预览模式：不消耗群发额度，仅发给指定用户核对排版
            return self._preview(token, article, openid_list, sent_at)

        try:
            resp = requests.post(
                f"{WX_NEWS_ADD}?access_token={token}",
                json={"articles": [article]}, timeout=30)
            data = resp.json() if resp.text else {}
        except (requests.RequestException, json.JSONDecodeError) as exc:
            raise WeChatPushError(f"创建图文失败：{exc}") from exc

        if data.get("errcode") not in (0, None) or not data.get("media_id"):
            raise WeChatPushError(
                f"创建图文失败：errcode={data.get('errcode')} "
                f"errmsg={data.get('errmsg')}")
        media_id = data["media_id"]

        # ---- 步骤 3：群发 ----
        try:
            resp = requests.post(
                f"{WX_MASS_SENDALL}?access_token={token}",
                json={"filter": {"is_to_all": True},
                      "mpnews": {"media_id": media_id},
                      "msgtype": "mpnews",
                      "send_ignore_reprint": 0},
                timeout=30)
            data = resp.json() if resp.text else {}
        except (requests.RequestException, json.JSONDecodeError) as exc:
            raise WeChatPushError(f"群发请求失败：{exc}") from exc

        errcode = data.get("errcode")
        ok = (errcode == 0)
        msg = data.get("errmsg", "")
        if ok:
            log.info("订阅号群发已提交 msg_id=%s（注意：每天仅 1 次额度）",
                     data.get("msg_id"))
        else:
            log.error("订阅号群发失败 errcode=%s errmsg=%s —— "
                      "若为 45009 类错误，通常是当天群发额度已用完", errcode, msg)
        return PushResult(channel=CHANNEL_SUBSCRIPTION, ok=ok,
                          message=msg or "success", errcode=errcode,
                          raw_response=data, sent_at=sent_at)

    def _preview(self, token: str, article: Dict[str, Any],
                 openid_list: List[str], sent_at: str) -> PushResult:
        """走「预览」接口发给指定用户，不消耗群发额度。

        对应提示词 §6.3：首次真实推送前必须只发给自己，确认排版无误再放开。
        """
        import requests  # 惰性导入

        # 预览接口需要先把图文存为临时素材
        try:
            resp = requests.post(
                "https://api.weixin.qq.com/cgi-bin/media/uploadnews"
                f"?access_token={token}",
                json={"articles": [article]}, timeout=30)
            data = resp.json() if resp.text else {}
        except (requests.RequestException, json.JSONDecodeError) as exc:
            raise WeChatPushError(f"预览素材上传失败：{exc}") from exc
        if not data.get("media_id"):
            raise WeChatPushError(
                f"预览素材上传失败：errcode={data.get('errcode')} "
                f"errmsg={data.get('errmsg')}")
        media_id = data["media_id"]

        ok_count = 0
        details = []
        for oid in openid_list[:5]:   # 预览人数上限 5
            try:
                r = requests.post(
                    f"{WX_PREVIEW}?access_token={token}",
                    json={"touser": oid, "mpnews": {"media_id": media_id},
                          "msgtype": "mpnews"},
                    timeout=20)
                d = r.json() if r.text else {}
                details.append({"openid_masked": mask(oid, 4),
                                "errcode": d.get("errcode"),
                                "errmsg": d.get("errmsg")})
                if d.get("errcode") == 0:
                    ok_count += 1
            except (requests.RequestException, json.JSONDecodeError) as exc:
                details.append({"openid_masked": mask(oid, 4), "error": str(exc)})

        return PushResult(
            channel=CHANNEL_SUBSCRIPTION, ok=ok_count > 0,
            message=f"预览已发送给 {ok_count} 人（未消耗群发额度）",
            raw_response={"details": details}, sent_at=sent_at)

    def _upload_thumb(self, token: str, cover_path: Path) -> Optional[str]:
        """上传封面图为永久素材，返回 thumb_media_id。"""
        import requests  # 惰性导入

        suffix = cover_path.suffix.lower().lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png", "gif": "image/gif"}.get(suffix, "image/jpeg")
        try:
            with cover_path.open("rb") as f:
                files = {"media": (cover_path.name, f, mime)}
                data = {"type": "image"}
                resp = requests.post(
                    f"{WX_MATERIAL_ADD}?access_token={token}",
                    files=files, data=data, timeout=60)
                result = resp.json() if resp.text else {}
        except (OSError, requests.RequestException, json.JSONDecodeError) as exc:
            log.error("封面图上传异常：%s", exc)
            return None
        if result.get("media_id"):
            return str(result["media_id"])
        log.error("封面图上传失败：errcode=%s errmsg=%s",
                  result.get("errcode"), result.get("errmsg"))
        return None

    # ------------------------------------------------------------
    # access_token
    # ------------------------------------------------------------
    def _get_access_token(self) -> Optional[str]:
        """获取并缓存 access_token（有效期 7200 秒，提前 300 秒刷新）。

        ⚠️ IP 白名单：服务器 IP 必须已加入公众号后台白名单，
           否则返回 errcode 40164。
        """
        import time as _time
        import requests  # 惰性导入

        now = _time.time()
        if self._access_token and now < self._token_expires_at:
            return self._access_token

        if not (self.cfg.wechat_app_id and self.cfg.wechat_app_secret):
            log.error("缺少 WECHAT_APP_ID / WECHAT_APP_SECRET，无法获取 access_token")
            return None

        try:
            resp = requests.get(WX_TOKEN_URL, params={
                "grant_type": "client_credential",
                "appid": self.cfg.wechat_app_id,
                "secret": self.cfg.wechat_app_secret,
            }, timeout=20)
            data = resp.json() if resp.text else {}
        except (requests.RequestException, json.JSONDecodeError) as exc:
            log.error("获取 access_token 请求失败：%s", exc)
            return None

        token = data.get("access_token")
        if not token:
            errcode = data.get("errcode")
            hint = ""
            if errcode == 40164:
                hint = "（IP 不在白名单内，请到公众号后台添加服务器公网 IP）"
            elif errcode in (40001, 40125):
                hint = "（AppSecret 错误或已被重置）"
            elif errcode == 40013:
                hint = "（AppID 无效）"
            log.error("获取 access_token 失败：errcode=%s errmsg=%s %s",
                      errcode, data.get("errmsg"), hint)
            return None

        self._access_token = str(token)
        expires_in = int(data.get("expires_in", 7200))
        self._token_expires_at = now + max(60, expires_in - 300)
        log.info("access_token 已刷新，有效期 %d 秒", expires_in)
        return self._access_token

    # ------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------
    @staticmethod
    def _to_html(text: str) -> str:
        """把纯文本正文转成公众号图文可用的简单 HTML。

        只做转义与换行处理，不引入富文本依赖。
        """
        import html as _html
        escaped = _html.escape(text or "")
        # Markdown 加粗 **xxx** → <strong>
        import re
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        paragraphs = escaped.split("\n\n")
        return "".join(
            f'<p style="line-height:1.8;margin:0 0 12px 0;">'
            f'{p.replace(chr(10), "<br/>")}</p>'
            for p in paragraphs if p.strip()
        )

    # ------------------------------------------------------------
    # 配置自检
    # ------------------------------------------------------------
    def preflight(self) -> List[str]:
        """上线前检查，返回问题清单（空列表表示通过）。"""
        problems: List[str] = []
        if self.channel == CHANNEL_DRYRUN:
            return problems   # dryrun 无需检查

        if self.channel == CHANNEL_WECOM:
            if not self.cfg.wecom_webhook_url:
                problems.append("缺少 WECOM_WEBHOOK_URL")
            elif "qyapi.weixin.qq.com" not in self.cfg.wecom_webhook_url:
                problems.append("WECOM_WEBHOOK_URL 看起来不是企业微信机器人地址")
            return problems

        if not self.cfg.wechat_app_id:
            problems.append("缺少 WECHAT_APP_ID")
        if not self.cfg.wechat_app_secret:
            problems.append("缺少 WECHAT_APP_SECRET")
        if self.channel == CHANNEL_TEMPLATE:
            if not self.cfg.wechat_template_id:
                problems.append("缺少 WECHAT_TEMPLATE_ID")
            problems.append(
                "⚠️ 待确认：模板消息用于天气提醒是否属官方允许的「服务通知」类目，"
                "官方类目列表中未见「气象/天气」，存在被判违规的风险")
        if self.channel == CHANNEL_SUBSCRIPTION:
            problems.append(
                "⚠️ 提醒：订阅号每天仅 1 次群发额度，当天若已手动群发则会失败")
        problems.append("⚠️ 部署前必须把服务器公网 IP 加入公众号后台 IP 白名单，"
                        "否则获取 access_token 会返回 40164")
        return problems
