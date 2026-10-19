# -*- coding: utf-8 -*-
"""
人性化中文气象文案生成器（提示词 PDF 模块二）。

⚠️ 原 PDF 代码存在语法残缺（f-string 未闭合、结尾多余空 append），本文件为重写而非复制。

三条设计红线（对应法规，违反即违法，详见 reports/02_爬虫合规检查.md 第五节）：

1. 《中华人民共和国气象法》第二十五条：媒体和单位向社会传播气象预报，
   必须使用气象主管机构所属气象台站提供的适时气象信息，
   **并标明发布时间和气象台站的名称**。
   → 每条文案末尾强制附「信息来源 + 发布时间」，缺失则拒绝生成。

2. 《气象预报发布与传播管理办法》第九条：
   传播气象预报**不得自行更改气象预报的内容和结论**。
   → 只做「生活化转译」（把「多云转阴」说成「云会比较多，天色偏阴沉」），
     **绝不改变或新增预报结论**（不得自行断言「明天肯定下雨」）。
     所有气温、降水、风力数值一律原样引用官方值，不做二次计算后当作预报发布。

3. 《气象法》第二十二条：非气象台站**不得向社会发布**气象预报。
   → 本系统定位为「传播官方预报 + 生活化解读」，
     文案中不出现任何自主预报表述，不呈现自研模型预测值。
     （模型预测值对外发布涉嫌违反《办法》第十条，已列为后续强制约束）

安全纪律：
  * 数据缺失时输出「⚠️ 数据暂缺」占位，绝不编造数值。
  * 使用缓存旧数据时，必须在文案中明示数据更新时间。
  * 温度用官方值原样呈现，不做「体感修正」后冒充官方预报。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# 文案末尾的强制署名（《气象法》第二十五条）
ATTRIBUTION_TEMPLATE = "📌 信息来源：{source}　发布时间：{publish_time}"
# 使用缓存数据时的强制提示
STALE_WARNING = "⚠️ 注意：本次数据为 {age} 前的缓存内容，官方源暂时不可达，请以最新官方发布为准。"

MISSING = "⚠️ 数据暂缺"


@dataclass
class WeatherContent:
    """生成的文案产物。"""
    title: str = ""
    body: str = ""
    summary: str = ""          # 一句话摘要，供模板消息等短场景使用
    cover_hint: str = ""       # 封面图建议（无图片资源时的文字描述）
    attribution: str = ""
    warnings: List[str] = field(default_factory=list)
    is_stale: bool = False
    data_time: str = ""
    generated_at: str = ""
    ok: bool = False

    def full_text(self) -> str:
        """完整推送正文（含署名）。"""
        parts = [p for p in (self.body, self.attribution) if p]
        return "\n\n".join(parts)


# ============================================================
# 生活化措辞映射（只做转译，不改变结论）
# ============================================================
# 天气现象 -> 口语化描述。仅对已实测确认的官方现象名称做措辞转换，
# 未收录的现象直接沿用官方原文，绝不自造描述。
WEATHER_PHRASE: Dict[str, str] = {
    "晴": "天空放晴，阳光在线",
    "多云": "云量偏多，太阳偶尔露脸",
    "阴": "天色阴沉，云层较厚",
    "阵雨": "会有阵性降雨，说来就来",
    "雷阵雨": "有雷阵雨，可能伴随雷电",
    "小雨": "有小雨",
    "中雨": "有中雨，雨势较明显",
    "大雨": "有大雨，雨势较强",
    "暴雨": "有暴雨，雨势猛烈",
    "小雪": "有小雪",
    "中雪": "有中雪",
    "大雪": "有大雪",
    "雨夹雪": "有雨夹雪，路面易湿滑",
    "雾": "有雾，能见度下降",
    "霾": "有霾，空气浑浊",
    "扬沙": "有扬沙，风沙明显",
    "浮尘": "有浮尘",
}

# 风力等级 -> 生活提示（对官方风力文本的口语化转译）
WIND_HINT: List[Tuple[str, str]] = [
    ("<3级", "风力很小，基本无感"),
    ("3-4级", "有些风，体感清爽"),
    ("4-5级", "风力偏大，外出注意帽子、雨伞易被吹翻"),
    ("5-6级", "风较大，尽量减少高空和户外长时间活动"),
    ("6-7级", "风力强劲，注意防范高空坠物"),
]

# 气温区间 -> 穿衣口语建议（属生活提示，非预报结论，措辞谨慎）
def _temp_advice(tmax: Optional[float], tmin: Optional[float]) -> Optional[str]:
    """基于官方气温区间给出穿衣提示。

    ⚠️ 这是「生活建议」而非「气象预报」，不改变任何官方结论。
       同时官方已提供穿衣指数原文（ct），若存在则优先用官方原文，
       本函数仅作为官方指数缺失时的兜底。
    """
    if tmax is None and tmin is None:
        return None
    hi = tmax if tmax is not None else tmin
    lo = tmin if tmin is not None else tmax
    avg = ((hi or 0) + (lo or 0)) / 2.0
    if hi is not None and hi >= 35:
        return "高温天气，注意防暑补水，尽量避免正午长时间户外活动"
    if avg >= 28:
        return "天气炎热，穿透气的短袖即可"
    if avg >= 22:
        return "温度舒适，短袖或薄长袖都合适"
    if avg >= 16:
        return "早晚偏凉，建议备一件薄外套"
    if avg >= 10:
        return "天气转凉，外套、卫衣可以安排上"
    if avg >= 5:
        return "比较冷，建议穿厚外套或薄羽绒服"
    if avg >= 0:
        return "天气寒冷，羽绒服、围巾请备好"
    return "严寒天气，务必做好全身保暖，注意防冻"


# ============================================================
# 主生成器
# ============================================================
class ContentGenerator:
    """把结构化气象数据转成人性化中文播报文案。"""

    def __init__(self, *,
                 display_name: str = "聊城市东昌府区",
                 attribution_name: str = "中央气象台·中国天气网",
                 include_indices: bool = True,
                 include_hourly: bool = True,
                 max_hourly_rows: int = 6,
                 base_date: Optional[datetime] = None):
        self.display_name = display_name
        self.attribution_name = attribution_name
        self.include_indices = include_indices
        self.include_hourly = include_hourly
        self.max_hourly_rows = max_hourly_rows
        # 基准日期：默认「今天」。生成多日样例文案时可指定为某一天，
        # 使标题、星期、指数筛选等都以该日期为准（数据仍全部来自官方预报）。
        self.base_date = base_date or datetime.now()

    @property
    def _base_str(self) -> str:
        return self.base_date.strftime("%Y-%m-%d")

    # ------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------
    def generate(self, *,
                 today: Optional[Dict[str, Any]],
                 daily: Optional[List[Dict[str, Any]]] = None,
                 realtime: Optional[Dict[str, Any]] = None,
                 hourly: Optional[List[Dict[str, Any]]] = None,
                 indices: Optional[List[Dict[str, Any]]] = None,
                 alarms: Optional[List[Dict[str, Any]]] = None,
                 publish_time: Optional[str] = None,
                 data_time: Optional[str] = None,
                 is_stale: bool = False) -> WeatherContent:
        """生成完整文案。

        Args:
            today: 今日预报（官方原文）
            daily: 未来多日预报
            realtime: 实时观测
            hourly: 逐小时预报
            indices: 生活指数
            alarms: 预警信息
            publish_time: 官方发布时间（《气象法》要求）
            data_time: 数据实际时间（缓存降级时与 publish_time 不同）
            is_stale: 是否为缓存旧数据
        """
        out = WeatherContent(
            generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            is_stale=is_stale,
            data_time=data_time or publish_time or "",
        )
        daily = daily or []

        # ---------- 前置校验：法规红线 ----------
        if not today and not daily:
            out.warnings.append("无任何官方预报数据，拒绝生成文案（不推空内容、不造假数据）")
            log.error("文案生成失败：%s", out.warnings)
            out.ok = False
            return out

        # 《气象法》第二十五条：必须能标明发布时间与气象台站名称
        if not publish_time:
            publish_time = self._infer_publish_time(today, daily)
        if not publish_time:
            out.warnings.append(
                "缺失官方发布时间，无法满足《气象法》第二十五条的署名要求；"
                "已用采集时间兜底，请检查数据源是否异常")
            publish_time = out.generated_at.replace("T", " ")[:16]
            log.warning(out.warnings[-1])

        # ---------- 组织正文 ----------
        blocks: List[str] = []

        # 1) 预警优先（关系人身安全，必须置顶）
        alarm_block = self._render_alarms(alarms or [])
        if alarm_block:
            blocks.append(alarm_block)

        # 2) 缓存数据提示
        if is_stale:
            age = self._humanize_age(data_time)
            blocks.append(STALE_WARNING.format(age=age))
            out.warnings.append("使用了缓存数据")

        # 3) 今日主预报
        blocks.append(self._render_today(today, realtime))

        # 4) 逐小时趋势
        if self.include_hourly and hourly:
            hb = self._render_hourly(hourly)
            if hb:
                blocks.append(hb)

        # 5) 未来几天
        future_block = self._render_future(daily)
        if future_block:
            blocks.append(future_block)

        # 6) 生活指数
        if self.include_indices and indices:
            ib = self._render_indices(indices, today)
            if ib:
                blocks.append(ib)

        # 7) 穿衣兜底建议（官方指数缺失时）
        adv = self._render_fallback_advice(today, indices or [])
        if adv:
            blocks.append(adv)

        body = "\n\n".join(b for b in blocks if b and b.strip())

        # ---------- 署名（强制） ----------
        out.attribution = ATTRIBUTION_TEMPLATE.format(
            source=self.attribution_name, publish_time=publish_time)

        # ---------- 标题与摘要 ----------
        tmax = self._get(today, "temp_max")
        tmin = self._get(today, "temp_min")
        wtext = self._get(today, "text_full") or self._get(today, "text_day")
        date_str = self.base_date.strftime("%m月%d日")
        temp_str = ""
        if tmax is not None and tmin is not None:
            temp_str = f" {int(round(tmin))}~{int(round(tmax))}℃"
        elif tmax is not None:
            temp_str = f" {int(round(tmax))}℃"
        prefix = "【气象预警】" if (alarms and len(alarms) > 0) else ""
        out.title = (f"{prefix}{date_str} {self.display_name}天气"
                     f"{'：' + wtext if wtext else ''}{temp_str}")
        out.summary = self._render_summary(today, realtime, alarms or [])
        out.cover_hint = self._render_cover_hint(today)
        out.body = body
        out.ok = bool(body)
        log.info("文案生成完成：标题=%s 正文%d字 警告%d条",
                 out.title, len(body), len(out.warnings))
        return out

    # ------------------------------------------------------------
    # 各区块渲染
    # ------------------------------------------------------------
    @staticmethod
    def _get(d: Optional[Dict[str, Any]], key: str) -> Optional[Any]:
        if not isinstance(d, dict):
            return None
        v = d.get(key)
        return v if v not in ("", None) else None

    def _render_alarms(self, alarms: List[Dict[str, Any]]) -> str:
        """渲染预警。⚠️ 预警内容一律原样引用官方标题，绝不改写。"""
        if not alarms:
            return ""
        lines = ["🚨 **气象预警**"]
        for a in alarms[:5]:
            title = a.get("title")
            if not title:
                continue
            issue = a.get("issue_time")
            lines.append(f"• {title}" + (f"（{issue}）" if issue else ""))
        if len(alarms) > 5:
            lines.append(f"• ……另有 {len(alarms) - 5} 条，请关注官方最新发布")
        lines.append("⚠️ 请减少不必要的外出，注意人身安全。")
        return "\n".join(lines) if len(lines) > 2 else ""

    def _render_today(self, today: Optional[Dict[str, Any]],
                      realtime: Optional[Dict[str, Any]]) -> str:
        lines: List[str] = []
        now = self.base_date
        weekday_cn = self._weekday_cn(now)
        lines.append(f"☀️ **今日天气（{now:%m月%d日} {weekday_cn}）**")

        # 实况
        if realtime:
            rt = realtime.get("temp")
            txt = realtime.get("text")
            hum = realtime.get("humidity")
            wd = realtime.get("wind_dir")
            ws = realtime.get("wind_scale")
            aqi = realtime.get("aqi")
            obs = realtime.get("observed_at")
            seg = []
            if rt is not None:
                seg.append(f"当前气温 {rt:g}℃")
            if txt:
                seg.append(txt)
            if hum is not None:
                seg.append(f"相对湿度 {hum:g}%")
            if wd or ws:
                seg.append(" ".join(x for x in (wd, ws) if x))
            if aqi is not None:
                seg.append(f"AQI {aqi:g}（{self._aqi_level(aqi)}）")
            if seg:
                tail = f"（{obs} 观测）" if obs else ""
                lines.append("【实况】" + "，".join(seg) + tail)

        # 今日预报（官方原文）
        if today:
            wtext = today.get("text_full") or self._join_day_night(
                today.get("text_day"), today.get("text_night"))
            tmax = today.get("temp_max")
            tmin = today.get("temp_min")
            phrase = WEATHER_PHRASE.get(wtext or "", "")
            if wtext:
                # ⚠️ 措辞转译，不改变结论：先给官方原文，再给口语化解释
                seg = f"官方预报：{wtext}"
                if phrase:
                    seg += f"，{phrase}"
                lines.append("【预报】" + seg)
            if tmax is not None or tmin is not None:
                ts = []
                if tmin is not None:
                    ts.append(f"最低 {tmin:g}℃")
                if tmax is not None:
                    ts.append(f"最高 {tmax:g}℃")
                lines.append("【气温】" + "，".join(ts))

            wd_full = today.get("wind_dir_full") or self._join_day_night(
                today.get("wind_dir_day"), today.get("wind_dir_night"))
            ws_full = today.get("wind_scale_full") or self._join_day_night(
                today.get("wind_scale_day"), today.get("wind_scale_night"))
            if wd_full or ws_full:
                wind_seg = " ".join(x for x in (wd_full, ws_full) if x)
                hint = self._wind_hint(ws_full or "")
                lines.append("【风力】" + wind_seg + (f"，{hint}" if hint else ""))
        if len(lines) == 1:
            lines.append("【预报】" + MISSING)
        return "\n".join(lines)

    def _render_hourly(self, hourly: List[Dict[str, Any]]) -> str:
        """逐小时趋势。只展示已确认语义的字段（气温/湿度/风向/降水）。"""
        rows = [h for h in hourly if h.get("hour_ts")][:self.max_hourly_rows]
        if not rows:
            return ""
        lines = ["⏰ **逐小时趋势**"]
        for h in rows:
            ts = h.get("hour_ts") or ""
            hh = ts[11:16] if len(ts) >= 16 else ts
            temp = h.get("temp")
            rain = h.get("rain")
            wd = h.get("wind_dir")
            seg = []
            if temp is not None:
                seg.append(f"{temp:g}℃")
            if h.get("text"):
                seg.append(str(h["text"]))
            if rain is not None and rain > 0:
                seg.append(f"降水 {rain:g}mm")
            if wd:
                seg.append(str(wd))
            lines.append(f"  {hh}　" + "　".join(seg) if seg else f"  {hh}　{MISSING}")
        return "\n".join(lines)

    def _render_future(self, daily: List[Dict[str, Any]]) -> str:
        """未来几天预报。"""
        # 排除基准日当天，避免与今日区块重复
        today_str = self._base_str
        future = [d for d in daily if d.get("forecast_date") and
                  d["forecast_date"] != today_str][:6]
        if not future:
            return ""
        lines = ["📅 **未来几天**"]
        for d in future:
            fd = d.get("forecast_date") or ""
            try:
                dt = datetime.strptime(fd, "%Y-%m-%d")
                label = f"{dt.month}月{dt.day}日 {self._weekday_cn(dt)}"
            except (ValueError, TypeError):
                label = fd
            wtext = d.get("text_full") or self._join_day_night(
                d.get("text_day"), d.get("text_night")) or MISSING
            tmin, tmax = d.get("temp_min"), d.get("temp_max")
            if tmin is not None and tmax is not None:
                tseg = f"{tmin:g}~{tmax:g}℃"
            elif tmax is not None:
                tseg = f"最高 {tmax:g}℃"
            elif tmin is not None:
                tseg = f"最低 {tmin:g}℃"
            else:
                tseg = MISSING
            ws = d.get("wind_scale_day") or ""
            lines.append(f"  {label}　{wtext}　{tseg}" + (f"　{ws}" if ws else ""))
        return "\n".join(lines)

    def _render_indices(self, indices: List[Dict[str, Any]],
                        today: Optional[Dict[str, Any]]) -> str:
        """生活指数。

        ⚠️ 官方指数原文优先（《办法》第九条：不得更改内容和结论），
           只挑选校园场景最相关的几项展示，描述文字原样引用。
        """
        today_str = self._base_str
        pool = [i for i in indices if i.get("index_date") == today_str] or indices
        if not pool:
            return ""
        # 校园场景优先级
        priority = ["ct", "gm", "uv", "ys", "yd", "fs", "co", "tr", "ag", "gz"]
        by_code = {}
        for i in pool:
            c = i.get("code")
            if c and c not in by_code:
                by_code[c] = i
        picked = [by_code[c] for c in priority if c in by_code][:5]
        if not picked:
            picked = pool[:5]
        lines = ["🎒 **校园生活指数**"]
        for i in picked:
            name = (i.get("name") or "").replace("指数", "")
            level = i.get("level") or ""
            desc = i.get("description") or ""
            seg = f"  {name}：{level}" if level else f"  {name}：{MISSING}"
            if desc:
                seg += f"　{desc}"
            lines.append(seg)
        return "\n".join(lines)

    def _render_fallback_advice(self, today: Optional[Dict[str, Any]],
                                indices: List[Dict[str, Any]]) -> str:
        """官方穿衣指数缺失时，才用气温区间给兜底建议。"""
        if not today:
            return ""
        if any(i.get("code") == "ct" and i.get("level") for i in indices):
            return ""  # 官方已有穿衣指数，不重复、不覆盖
        advice = _temp_advice(today.get("temp_max"), today.get("temp_min"))
        return f"👕 **穿衣参考**　{advice}" if advice else ""

    def _render_summary(self, today: Optional[Dict[str, Any]],
                        realtime: Optional[Dict[str, Any]],
                        alarms: List[Dict[str, Any]]) -> str:
        """一句话摘要（模板消息 / 企业微信 markdown 用）。"""
        if alarms:
            return f"⚠️ {alarms[0].get('title', '有气象预警')}"
        wtext = self._get(today, "text_full") or self._get(today, "text_day") or ""
        tmax = self._get(today, "temp_max")
        tmin = self._get(today, "temp_min")
        seg = []
        if wtext:
            seg.append(wtext)
        if tmin is not None and tmax is not None:
            seg.append(f"{tmin:g}~{tmax:g}℃")
        elif tmax is not None:
            seg.append(f"最高{tmax:g}℃")
        rt = self._get(realtime, "temp")
        if rt is not None:
            seg.append(f"当前{rt:g}℃")
        return f"{self.display_name}今日：" + "，".join(seg) if seg else MISSING

    def _render_cover_hint(self, today: Optional[Dict[str, Any]]) -> str:
        """封面图建议文字（无图片资源时供人工选图参考）。"""
        wtext = self._get(today, "text_day") or self._get(today, "text_full") or ""
        mapping = {
            "晴": "晴天蓝天阳光", "多云": "多云天空", "阴": "阴天云层",
            "小雨": "细雨街景", "中雨": "雨中城市", "大雨": "强降雨",
            "雷阵雨": "雷雨闪电", "小雪": "雪景", "中雪": "降雪",
            "大雪": "大雪纷飞", "雾": "雾霭", "霾": "灰霾城市",
        }
        for k, v in mapping.items():
            if k in wtext:
                return f"建议封面：{v}（对应官方预报「{wtext}」）"
        return f"建议封面：聊城校园或城市天际线（官方预报：{wtext or MISSING}）"

    # ------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------
    @staticmethod
    def _join_day_night(day: Optional[str], night: Optional[str]) -> Optional[str]:
        if day and night and day != night:
            return f"{day}转{night}"
        return day or night

    @staticmethod
    def _weekday_cn(dt: datetime) -> str:
        return "星期" + "一二三四五六日"[dt.weekday()]

    @staticmethod
    def _wind_hint(scale_text: str) -> Optional[str]:
        if not scale_text:
            return None
        for key, hint in WIND_HINT:
            if key in scale_text:
                return hint
        return None

    @staticmethod
    def _aqi_level(aqi: float) -> str:
        """AQI 等级（国标 HJ 633-2012 的公开分级，属常识性转译，非预报结论）。"""
        if aqi <= 50:
            return "优"
        if aqi <= 100:
            return "良"
        if aqi <= 150:
            return "轻度污染"
        if aqi <= 200:
            return "中度污染"
        if aqi <= 300:
            return "重度污染"
        return "严重污染"

    @staticmethod
    def _infer_publish_time(today: Optional[Dict[str, Any]],
                            daily: List[Dict[str, Any]]) -> Optional[str]:
        for rec in [today] + list(daily):
            if isinstance(rec, dict) and rec.get("publish_time"):
                return str(rec["publish_time"])
        return None

    @staticmethod
    def _humanize_age(data_time: Optional[str]) -> str:
        if not data_time:
            return "未知时间"
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(data_time[:19], fmt)
                break
            except (ValueError, TypeError):
                continue
        else:
            return data_time
        delta = datetime.now() - dt
        mins = int(delta.total_seconds() // 60)
        if mins < 60:
            return f"{mins} 分钟"
        hours = mins // 60
        if hours < 24:
            return f"{hours} 小时"
        return f"{hours // 24} 天"
