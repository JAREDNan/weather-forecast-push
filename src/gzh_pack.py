# -*- coding: utf-8 -*-
"""
公众号推送包生成器（GzhPack）。

背景（2026-10-19 确认）：
  当前公众号为「未认证订阅号」，微信 2025-07 起回收了个人主体账号的
  发布/群发 API 接口权限（errcode 48001），因此**无法用代码自动群发**；
  但公众号后台网页手动群发（每天 1 次）仍可用，认证与否均不受影响。

本模块把已生成的文案（WeatherContent）整理成「可直接粘贴到公众号后台
图文编辑器」的推送包，供人工 1 分钟完成手动群发，覆盖全部关注者。

产出两个文件（写入 logs/）：
  * gzh_pack_<日期>.md   —— 含完整操作指引 + 纯文本版正文（去 Markdown 符号，
                            复制粘贴即可用，公众号编辑器识别普通换行/空格）
  * gzh_pack_<日期>.html —— 富文本版正文（浏览器打开 → 全选复制 → 粘贴到
                            公众号编辑器，可保留加粗、列表等基础排版）

⚠️ 本文案正文仍保留「信息来源 + 发布时间」署名（《气象法》第二十五条），
   人工粘贴时不可删除。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .content_generator import WeatherContent

log = logging.getLogger(__name__)


# ============================================================
# Markdown → 公众号可粘贴文本
# ============================================================
def md_to_plain(text: str) -> str:
    """把文案中的 Markdown 标记转成公众号编辑器友好的纯文本。

    处理规则（只去除排版符号，不改动任何内容与结论）：
      * 行内 **加粗** → 保留文字，去掉星号
      * 行首 "- "（列表）→ 去掉连字符，保留内容；保持换行
      * 行首 "  "（缩进）→ 压缩为单个空格
      * 连续空行 → 合并为一个空行
      * 保留 emoji 与全角空格（公众号编辑器支持）
    """
    lines = text.splitlines()
    out: List[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            out.append("")
            continue
        # 行内加粗
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        # 列表项
        if line.lstrip().startswith("- "):
            line = line.lstrip()[2:]
        # 缩进行压缩
        if line.startswith("  ") or line.startswith("\t"):
            line = line.lstrip()
        out.append(line)
    # 合并连续空行
    merged: List[str] = []
    prev_blank = False
    for line in out:
        blank = (line.strip() == "")
        if blank and prev_blank:
            continue
        merged.append(line)
        prev_blank = blank
    return "\n".join(merged).strip() + "\n"


def md_to_html_rich(text: str) -> str:
    """把 Markdown 正文转成公众号编辑器可识别的富文本 HTML 片段。

    转换规则（不改变内容）：
      * **加粗** → <b>…</b>
      * "- " 列表行 → 输出为带行分隔的段落（公众号编辑器粘贴后为普通段落，
        避免依赖不稳定的 <ul> 解析）
      * 空行 → 段落分隔
      * emoji / 全角空格原样保留
    返回不含 <html>/<body> 的 fragment；外层由 build_html_page 包裹。
    """
    lines = text.splitlines()
    paras: List[str] = []
    buf: List[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            if buf:
                paras.append("".join(buf))
                buf = []
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        if line.lstrip().startswith("- "):
            line = line.lstrip()[2:]
        buf.append(line + "<br/>")
    if buf:
        paras.append("".join(buf))
    if not paras:
        return ""
    return "\n\n".join(f"<p>{p}</p>" for p in paras)


def _fmt_day(day_label: str) -> str:
    """YYYYMMDD → YYYY-MM-DD；容忍已带连字符的输入。"""
    d = day_label.replace("-", "")
    if len(d) == 8:
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return day_label


class GzhPackWriter:
    """生成「公众号推送包」文件（md + html）。"""

    def __init__(self, logs_dir: Path):
        self.logs_dir = Path(logs_dir)

    # ------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------
    def write(self, content: WeatherContent, *,
              day_label: Optional[str] = None,
              extra_note: str = "") -> List[Path]:
        """生成推送包文件，返回生成的路径列表。

        Args:
            content: 已通过合规校验的文案（含 title/body/attribution）
            day_label: 文件日期标签 YYYYMMDD；默认取运行当天
            extra_note: 附加说明（例如「缓存数据」等），插入操作指引区
        """
        day = (day_label or datetime.now().strftime("%Y%m%d")).replace("-", "")
        fmt = _fmt_day(day)
        body = content.full_text()          # 正文 + 署名
        plain = md_to_plain(body)
        rich_html = md_to_html_rich(body)

        md_path = self.logs_dir / f"gzh_pack_{day}.md"
        html_path = self.logs_dir / f"gzh_pack_{day}.html"

        md_text = self._render_md(
            fmt=fmt, content=content, plain=plain,
            rich_html=rich_html, extra_note=extra_note)
        html_text = self._render_html(
            fmt=fmt, content=content, rich_html=rich_html)

        md_path.write_text(md_text, encoding="utf-8")
        html_path.write_text(html_text, encoding="utf-8")
        log.info("公众号推送包已生成：%s / %s", md_path, html_path)
        return [md_path, html_path]

    # ------------------------------------------------------------
    # 渲染：Markdown 主文件
    # ------------------------------------------------------------
    def _render_md(self, *, fmt: str, content: WeatherContent,
                   plain: str, rich_html: str, extra_note: str) -> str:
        note = f"\n> ℹ️ 附加说明：{extra_note}" if extra_note else ""
        return f"""# 📦 公众号推送包 · {fmt}

> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
> 适用：**未认证订阅号网页手动群发**（每天 1 次，0 点重置；API 群发对个人主体
> 账号已回收权限，网页后台仍可用 —— 2026-10-19 核实）。
> ⚠️ 本文件为人工发布辅助材料，系统**未**发生任何真实推送。
{note}

---

## 一、发布操作指引（约 1 分钟）

1. 登录 mp.weixin.qq.com → 「内容与互动 - 创作发布 - 图文消息」（或首页「+ 新建」→ 图文消息）
2. 填写「**标题**」：直接复制下方【标题】内容
3. 「**摘要**」：复制【摘要】内容（选填，留空则取正文前 54 字）
4. 「**封面**」：按【封面建议】选择/上传图片（可复用系统封面 `assets/cover.png` 或任意天气相关图）
5. 「**正文**」：二选一粘贴 —
   - 方式 A（推荐，保留加粗排版）：打开 `gzh_pack_{fmt.replace('-','')}.html`
     → 浏览器全选（Ctrl+A）→ 复制 → 粘贴到正文编辑框
   - 方式 B：直接复制下方【纯文本正文】粘贴（格式为纯文本，需手动加粗重点）
6. 拉到文末确认「**信息来源 + 发布时间**」署名完整（《气象法》第二十五条，不可删）
7. 点「群发」→ 选择发送对象「全部用户」→ 确认发布

---

## 二、标题

{content.title}

## 三、摘要

{content.summary or '（无摘要，留空即可）'}

## 四、封面建议

{content.cover_hint or '（无自动建议，可自行上传天气/校园相关图片）'}

## 五、纯文本正文（方式 B 用，可直接粘贴）

```text
{plain}```

## 六、富文本正文（方式 A 用）

见同目录 HTML 文件：`gzh_pack_{fmt.replace('-','')}.html`

---

> ⚠️ 合规提醒：正文末尾的「信息来源：中央气象台·中国天气网 发布时间：…」
> 为《气象法》要求的法定署名，人工发布时**不得删除或修改**。
> 本系统文案中的天气现象/气温均来自官方台站，未做任何结论性更改。
"""

    # ------------------------------------------------------------
    # 渲染：富文本 HTML 页面
    # ------------------------------------------------------------
    def _render_html(self, *, fmt: str, content: WeatherContent,
                     rich_html: str) -> str:
        title_html = content.title.replace("&", "&amp;").replace("<", "&lt;")
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>公众号推送包 {fmt}</title>
<style>
  body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
          max-width: 720px; margin: 24px auto; padding: 0 20px;
          color: #222; line-height: 1.8; }}
  h2 {{ font-size: 22px; margin: 8px 0 16px; }}
  .hint {{ background: #f0f7ff; border-left: 4px solid #3b82f6;
          padding: 10px 14px; font-size: 13px; color: #334155; }}
  p {{ margin: 6px 0; font-size: 16px; }}
  b {{ color: #0f172a; }}
</style>
</head>
<body>
  <h2>{title_html}</h2>
  <p class="hint">📋 使用方法：全选本页（Ctrl+A）→ 复制 → 粘贴到公众号后台
  「图文消息」正文编辑框即可（加粗、换行会保留）。</p>
  {rich_html}
</body>
</html>
"""
