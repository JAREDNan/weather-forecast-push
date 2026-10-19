# -*- coding: utf-8 -*-
"""
文案生成与法规合规测试。

⚠️ 这是本项目最关键的一组测试：文案一旦违反《气象法》第二十五条
   （未标明发布时间与气象台站名称）或《气象预报发布与传播管理办法》
   第九条（自行更改预报内容和结论）、第十条（发布自主预报结论），
   可能面临警告乃至 5 万元以下罚款。

   因此这些测试验证的不只是「文案好不好看」，而是「是否合法、是否忠于官方数据」。
   任何一项失败都必须阻止上线。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from src.content_generator import ContentGenerator, WeatherContent
from src.daily_job import DailyJob

TODAY = {
    "forecast_date": "2026-10-03", "temp_max": 23.0, "temp_min": 12.0,
    "text_day": "多云", "text_night": "阴", "text_full": "多云转阴",
    "wind_dir_full": "南风转北风", "wind_scale_full": "4-5级转3-4级",
    "publish_time": "2026-10-03 08:00",
}
DAILY = [TODAY, {
    "forecast_date": "2026-10-04", "temp_max": 21.0, "temp_min": 10.0,
    "text_day": "晴", "text_night": "晴", "text_full": "晴",
    "wind_scale_day": "3-4级", "publish_time": "2026-10-03 08:00",
}]
REALTIME = {
    "temp": 18.3, "humidity": 58.0, "text": "多云", "wind_dir": "南风",
    "wind_scale": "2级", "aqi": 78.0, "observed_at": "08:50",
}
INDICES = [
    {"index_date": datetime.now().strftime("%Y-%m-%d"), "code": "ct",
     "name": "穿衣指数", "level": "较舒适",
     "description": "建议穿薄外套或牛仔裤等服装。"},
    {"index_date": datetime.now().strftime("%Y-%m-%d"), "code": "gm",
     "name": "感冒指数", "level": "较易发",
     "description": "风较大，较易发生感冒，注意防护。"},
]
ALARM = [{"title": "山东省聊城市气象台发布大风黄色预警信号",
          "level": "黄色", "signal_type": "大风",
          "issue_time": "2026-10-03 07:00"}]


@pytest.fixture
def gen() -> ContentGenerator:
    # base_date 固定为测试数据日期（TODAY=2026-10-03），
    # 使标题日期与预报数据自洽；生产路径运行日=预报日，无需传参。
    return ContentGenerator(display_name="聊城市东昌府区",
                            attribution_name="中央气象台·中国天气网",
                            base_date=datetime(2026, 10, 3))


@pytest.fixture
def content(gen) -> WeatherContent:
    return gen.generate(today=TODAY, daily=DAILY, realtime=REALTIME,
                        indices=INDICES, publish_time="2026-10-03 08:00")


# ============================================================
# 法规合规（硬性要求）
# ============================================================
class TestLegalCompliance:
    """《气象法》第二十五条 + 《办法》第九/十条 + 第三十八条罚则。"""

    def test_必须标明气象台站名称(self, content):
        """《气象法》第二十五条：必须标明气象台站的名称。"""
        full = content.full_text()
        assert "信息来源" in full
        assert "中央气象台" in full

    def test_必须标明发布时间(self, content):
        """《气象法》第二十五条：必须标明发布时间。"""
        full = content.full_text()
        assert "发布时间" in full
        assert "2026-10-03 08:00" in full

    def test_署名在正文末尾(self, content):
        """署名必须是正文的最后一部分，不能被淹没在中间。"""
        assert content.attribution.strip()
        assert content.full_text().strip().endswith(
            content.attribution.strip())

    def test_官方天气现象原样保留(self, content):
        """《办法》第九条：不得自行更改气象预报的内容和结论。"""
        full = content.full_text()
        assert "多云转阴" in full, "官方天气现象文本被改写了"

    def test_官方气温原样保留(self, content):
        full = content.full_text()
        assert "23" in full and "12" in full, "官方气温值丢失"

    def test_官方指数描述原样引用(self, content):
        full = content.full_text()
        assert "建议穿薄外套或牛仔裤等服装。" in full, "官方指数描述被改写"
        assert "较舒适" in full

    def test_不得出现自主预报表述(self, content):
        """《气象法》第二十二条：非气象台站不得发布气象预报。"""
        full = content.full_text()
        for bad in ("本系统预测", "AI 预测", "模型预测", "我们预计",
                    "我方预报", "自主预报", "预测明天"):
            assert bad not in full, f"出现自主预报表述：{bad}"

    def test_缺失发布时间时必须有告警(self, gen):
        """拿不到官方发布时间时必须显式告警，不能静默用假时间。

        注意：生成器会从 today / daily 记录里推断发布时间，
        故必须传入**不含 publish_time 的数据**才能真正触发该分支。
        """
        today_no_pub = {k: v for k, v in TODAY.items() if k != "publish_time"}
        daily_no_pub = [{k: v for k, v in d.items() if k != "publish_time"}
                        for d in DAILY]
        c = gen.generate(today=today_no_pub, daily=daily_no_pub,
                         publish_time=None)
        assert c.ok is True, "无发布时间不应导致整体失败，但必须告警"
        assert c.warnings, "缺失官方发布时间却未告警"
        assert any("发布时间" in w for w in c.warnings)
        # 兜底后署名中仍须存在「发布时间」字样，以满足法定署名格式
        assert "发布时间" in c.full_text()

    def test_无数据时拒绝生成(self, gen):
        """提示词 §4.2：任何情况下都不推送空内容或假数据。"""
        c = gen.generate(today=None, daily=[], publish_time="2026-10-03 08:00")
        assert c.ok is False
        assert c.warnings

    def test_缓存数据必须明示更新时间(self, gen):
        """降级用缓存时必须在文案中说明，避免用户误以为是最新预报。"""
        c = gen.generate(today=TODAY, daily=DAILY,
                         publish_time="2026-10-02 08:00",
                         data_time="2026-10-02 08:00", is_stale=True)
        assert c.is_stale is True
        assert "缓存" in c.body or "缓存" in c.full_text()
        assert "以最新官方发布为准" in c.full_text()


# ============================================================
# 合规硬性拦截（推送前最后一道闸门）
# ============================================================
class TestComplianceGate:
    """DailyJob._check_compliance 是推送前的最后防线。"""

    def _good(self) -> WeatherContent:
        return WeatherContent(
            title="t", body="官方预报：多云转阴，12~23℃。风力 4-5级。",
            attribution="📌 信息来源：中央气象台·中国天气网　"
                        "发布时间：2026-10-03 08:00",
            ok=True)

    def test_合规文案通过(self):
        assert DailyJob._check_compliance(self._good()) == []

    def test_缺署名被拦截(self):
        bad = self._good()
        bad.attribution = ""
        problems = DailyJob._check_compliance(bad)
        assert problems, "缺署名的文案通过了检查 —— 严重漏洞"
        assert any("信息来源" in p for p in problems)

    def test_缺发布时间被拦截(self):
        bad = self._good()
        bad.attribution = "📌 信息来源：中央气象台"
        problems = DailyJob._check_compliance(bad)
        assert problems
        assert any("发布时间" in p for p in problems)

    def test_发布时间为未知值被拦截(self):
        bad = self._good()
        bad.attribution = "📌 信息来源：中央气象台　发布时间：未知"
        problems = DailyJob._check_compliance(bad)
        assert problems, "发布时间为「未知」却通过了检查"

    def test_自主预报表述被拦截(self):
        """《气象法》第二十二条 + 《办法》第十条：不得发布自主预报。"""
        for phrase in ("本系统预测", "AI 预测", "模型预测", "我们预计"):
            bad = self._good()
            bad.body = f"{phrase}明天气温 25℃。" + bad.body
            problems = DailyJob._check_compliance(bad)
            assert problems, f"含「{phrase}」的文案通过了检查"
            assert any("第二十二条" in p or "第十条" in p for p in problems)

    def test_未验证编码占位符被拦截(self):
        bad = self._good()
        bad.body += "\n今日天气：⚠️ 未验证编码(53)"
        problems = DailyJob._check_compliance(bad)
        assert problems, "含未验证编码占位符的文案通过了检查"

    def test_空正文被拦截(self):
        bad = self._good()
        bad.body = ""
        assert DailyJob._check_compliance(bad)

    def test_过短正文被拦截(self):
        bad = self._good()
        bad.body = "多云。"
        assert DailyJob._check_compliance(bad)


# ============================================================
# 内容正确性
# ============================================================
class TestContentAccuracy:
    def test_标题含城市与日期(self, content):
        assert "聊城市东昌府区" in content.title
        assert "10月03日" in content.title

    def test_标题含天气与气温(self, content):
        assert "多云转阴" in content.title
        assert "12~23" in content.title

    def test_有预警时标题置顶提示(self, gen):
        c = gen.generate(today=TODAY, daily=DAILY, alarms=ALARM,
                         publish_time="2026-10-03 08:00")
        assert c.title.startswith("【气象预警】")

    def test_预警置于正文最前(self, gen):
        """预警关系人身安全，必须在正文最前面。"""
        c = gen.generate(today=TODAY, daily=DAILY, alarms=ALARM,
                         publish_time="2026-10-03 08:00")
        body = c.body
        assert body.startswith("🚨"), f"预警未置顶，正文开头为：{body[:20]}"
        assert body.index("大风黄色预警") < body.index("今日天气")

    def test_预警标题原样引用不改写(self, gen):
        c = gen.generate(today=TODAY, daily=DAILY, alarms=ALARM,
                         publish_time="2026-10-03 08:00")
        assert "山东省聊城市气象台发布大风黄色预警信号" in c.body

    def test_实时观测正确呈现(self, content):
        assert "18.3℃" in content.body
        assert "08:50" in content.body, "应标明观测时刻"

    def test_未来几天不含今天(self, content):
        """今天已在「今日天气」区块，未来几天不应重复。"""
        future_block = content.body.split("📅 **未来几天**")[1] \
            if "未来几天" in content.body else ""
        assert "10月4日" in future_block
        # 今天（10月03日）不应再出现在未来几天区块
        assert "10月03日" not in future_block

    def test_风力提示与等级匹配(self, gen):
        c = gen.generate(today=dict(TODAY, wind_scale_full="5-6级"),
                         publish_time="2026-10-03 08:00")
        assert "风较大" in c.body

    def test_AQI等级转译(self, gen):
        for aqi, level in ((30, "优"), (78, "良"), (120, "轻度污染"),
                           (180, "中度污染"), (250, "重度污染"),
                           (400, "严重污染")):
            c = gen.generate(today=TODAY, realtime=dict(REALTIME, aqi=aqi),
                             publish_time="2026-10-03 08:00")
            assert level in c.body, f"AQI={aqi} 应判为 {level}"

    def test_数据缺失时显示占位符而非编造(self, gen):
        """官方数据缺失必须显示「⚠️ 数据暂缺」，绝不编造数值。"""
        c = gen.generate(today={"forecast_date": "2026-10-03",
                                "temp_max": None, "temp_min": None,
                                "text_full": None},
                         daily=[], publish_time="2026-10-03 08:00")
        assert "⚠️ 数据暂缺" in c.body or "None" not in c.body
        assert "None" not in c.body, "Python 的 None 泄漏到了用户文案中"
        assert "nan" not in c.body.lower()

    def test_摘要简短可用(self, content):
        assert content.summary
        assert len(content.summary) < 120
        assert "聊城市东昌府区" in content.summary

    def test_封面建议含官方天气依据(self, content):
        assert content.cover_hint
        assert "多云" in content.cover_hint or "官方预报" in content.cover_hint


# ============================================================
# 措辞纪律：转译不得改变结论
# ============================================================
class TestWordingDiscipline:
    """《办法》第九条：可做生活化转译，但不得改变预报结论。"""

    def test_口语化描述与官方文本并存(self, gen):
        """必须是「官方预报：X」+ 口语解释，而非用口语替换官方文本。"""
        c = gen.generate(today=TODAY, publish_time="2026-10-03 08:00")
        assert "官方预报：多云转阴" in c.body, \
            "必须先给出官方原文，再给口语化解释"

    def test_不断言确定性降水(self, gen):
        """即使官方说「小雨」，也不得加码成「肯定下雨」「必降暴雨」。"""
        c = gen.generate(today=dict(TODAY, text_full="小雨",
                                    text_day="小雨", text_night="小雨"),
                         publish_time="2026-10-03 08:00")
        for bad in ("肯定下雨", "一定会下", "必降", "保证"):
            assert bad not in c.body

    def test_不擅自修正气温(self, gen):
        """不得自行加减官方气温（如「体感更冷所以写低 2 度」）。"""
        c = gen.generate(today=TODAY, publish_time="2026-10-03 08:00")
        # 官方 12~23℃，文案中不得出现被修正后的其他温度组合
        assert "12" in c.body and "23" in c.body
        assert "25℃" not in c.body.split("未来几天")[0]

    def test_穿衣建议不冒充预报(self, gen):
        """官方指数缺失时的兜底建议应标明是「参考」。"""
        c = gen.generate(today=TODAY, indices=[],
                         publish_time="2026-10-03 08:00")
        if "穿衣" in c.body:
            assert "参考" in c.body, "兜底穿衣建议应标明「参考」，避免被当成官方结论"

    def test_官方有穿衣指数时不重复给建议(self, gen):
        c = gen.generate(today=TODAY, indices=INDICES,
                         publish_time="2026-10-03 08:00")
        # 官方已有穿衣指数，不应再出现「穿衣参考」兜底区块
        assert "穿衣参考" not in c.body
