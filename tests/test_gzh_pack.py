# -*- coding: utf-8 -*-
"""gzh_pack 模块测试：公众号推送包生成（人工手动群发辅助）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.content_generator import WeatherContent          # noqa: E402
from src.gzh_pack import GzhPackWriter, md_to_html_rich, md_to_plain  # noqa: E402


@pytest.fixture
def sample_content() -> WeatherContent:
    """构造一条含 Markdown 标记、署名完整的合规文案。"""
    return WeatherContent(
        title="10月19日 聊城市东昌府区天气：阴 11~20℃",
        body=(
            "☀️ **今日天气（10月19日 星期一）**\n"
            "【实况】当前气温 17.6℃，多云，相对湿度 57%\n"
            "【预报】官方预报：阴，天色阴沉，云层较厚\n"
            "\n"
            "⏰ **逐小时趋势**\n"
            "  18:00　19℃　晴　南风\n"
            "  19:00　17℃　晴　南风\n"
            "\n"
            "📅 **未来几天**\n"
            "  10月20日 星期二　多云　5~20℃\n"
            "\n"
            "🎒 **校园生活指数**\n"
            "  穿衣：较冷　建议着厚外套加毛衣等服装。\n"
        ),
        summary="聊城市东昌府区今日：阴，11~20℃",
        cover_hint="建议封面：阴天云层",
        attribution="📌 信息来源：中央气象台·中国天气网　发布时间：2026-10-19 20:00",
        ok=True,
    )


class TestMdToPlain:
    """Markdown → 纯文本转换。"""

    def test_去除加粗星号(self, sample_content):
        plain = md_to_plain(sample_content.full_text())
        assert "**" not in plain
        assert "今日天气（10月19日 星期一）" in plain

    def test_保留关键内容与署名(self, sample_content):
        plain = md_to_plain(sample_content.full_text())
        assert "信息来源：中央气象台·中国天气网" in plain
        assert "发布时间：2026-10-19 20:00" in plain
        assert "逐小时趋势" in plain

    def test_缩进行保留(self, sample_content):
        plain = md_to_plain(sample_content.full_text())
        # 缩进行内容保留（时间行），仅去掉多余缩进
        assert "18:00　19℃　晴　南风" in plain

    def test_空行合并(self):
        text = "a\n\n\n\nb\n\nc"
        assert md_to_plain(text) == "a\n\nb\n\nc\n"


class TestMdToHtmlRich:
    """Markdown → 富文本 HTML 转换。"""

    def test_加粗转为b标签(self, sample_content):
        html = md_to_html_rich(sample_content.full_text())
        assert "<b>今日天气（10月19日 星期一）</b>" in html
        assert "**" not in html

    def test_段落包裹(self, sample_content):
        html = md_to_html_rich(sample_content.full_text())
        assert html.startswith("<p>")
        assert html.endswith("</p>")
        assert html.count("<p>") == html.count("</p>")

    def test_署名保留(self, sample_content):
        html = md_to_html_rich(sample_content.full_text())
        assert "信息来源：中央气象台·中国天气网" in html
        assert "发布时间：2026-10-19 20:00" in html


class TestGzhPackWriter:
    """推送包文件生成。"""

    def test_生成md与html两个文件(self, tmp_path, sample_content):
        writer = GzhPackWriter(tmp_path)
        paths = writer.write(sample_content, day_label="20261019")
        assert len(paths) == 2
        md, html = paths
        assert md.exists() and md.name == "gzh_pack_20261019.md"
        assert html.exists() and html.name == "gzh_pack_20261019.html"

    def test_md含标题与指引(self, tmp_path, sample_content):
        writer = GzhPackWriter(tmp_path)
        paths = writer.write(sample_content, day_label="20261019")
        text = paths[0].read_text(encoding="utf-8")
        assert "公众号推送包" in text
        assert "发布操作指引" in text
        assert "10月19日 聊城市东昌府区天气：阴 11~20℃" in text
        # 纯文本正文不含 markdown 星号（只检查正文代码块，不含指引区）
        plain_section = text.split("## 五、纯文本正文")[1].split("## 六、")[0]
        assert "**" not in plain_section

    def test_html含标题与署名(self, tmp_path, sample_content):
        writer = GzhPackWriter(tmp_path)
        paths = writer.write(sample_content, day_label="20261019")
        text = paths[1].read_text(encoding="utf-8")
        assert "公众号推送包 2026-10-19" in text
        assert "信息来源：中央气象台·中国天气网" in text

    def test_附加说明写入(self, tmp_path, sample_content):
        writer = GzhPackWriter(tmp_path)
        paths = writer.write(sample_content, day_label="20261019",
                             extra_note="缓存数据，请核对")
        text = paths[0].read_text(encoding="utf-8")
        assert "缓存数据，请核对" in text
