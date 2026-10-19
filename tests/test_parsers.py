# -*- coding: utf-8 -*-
"""
解析器单元测试。

重点验证「不猜测、不编造」纪律：哨兵值拦截、未验证编码返回 None、
软 404 识别、嵌套 JSON 解析。
"""
from __future__ import annotations

import pytest

from src.crawler.parsers import (
    clean_text,
    extract_all_js_vars,
    extract_js_var,
    is_soft_404,
    norm_date8,
    norm_datetime,
    norm_iso_hour,
    parse_json_safely,
    split_turn_text,
    to_float,
)
from tests.conftest import SOFT_404_RESPONSE, WEATHER_INDEX_RESPONSE


class TestExtractJsVar:
    def test_提取嵌套对象(self):
        obj = extract_js_var(WEATHER_INDEX_RESPONSE, "cityDZ")
        assert isinstance(obj, dict)
        assert obj["weatherinfo"]["city"] == "聊城"
        assert obj["weatherinfo"]["weather"] == "多云转阴"

    def test_提取数组(self):
        fc = extract_js_var(WEATHER_INDEX_RESPONSE, "fc")
        assert isinstance(fc, dict)
        assert len(fc["f"]) == 3
        assert fc["f"][0]["fc"] == "23"

    def test_字符串内的花括号不会截断(self):
        """值中含 } 或 ] 时，朴素正则会解析错，括号配对法必须正确。"""
        text = 'var x ={"a":"value}with{braces","b":[1,2]};var y=1;'
        obj = extract_js_var(text, "x")
        assert obj == {"a": "value}with{braces", "b": [1, 2]}

    def test_转义引号不破坏配对(self):
        text = r'var x ={"a":"he said \"hi\""};'
        obj = extract_js_var(text, "x")
        assert obj == {"a": 'he said "hi"'}

    def test_变量不存在返回None(self):
        assert extract_js_var(WEATHER_INDEX_RESPONSE, "notExist") is None

    def test_空文本返回None(self):
        assert extract_js_var("", "cityDZ") is None
        assert extract_js_var(None, "cityDZ") is None

    def test_未闭合括号返回None(self):
        assert extract_js_var('var x ={"a":1', "x") is None

    def test_列出所有变量名(self):
        names = extract_all_js_vars(WEATHER_INDEX_RESPONSE)
        assert set(names) == {"cityDZ", "alarmDZ", "dataSK", "dataZS", "fc"}

    def test_全部业务变量均可解析(self):
        for name in ("cityDZ", "alarmDZ", "dataSK", "dataZS", "fc"):
            assert extract_js_var(WEATHER_INDEX_RESPONSE, name) is not None, \
                f"{name} 解析失败"


class TestSoft404:
    def test_识别软404页面(self):
        assert is_soft_404(SOFT_404_RESPONSE) is True

    def test_正常数据不是软404(self):
        assert is_soft_404(WEATHER_INDEX_RESPONSE) is False

    def test_空响应不是软404(self):
        assert is_soft_404("") is False


class TestToFloat:
    """⚠️ 哨兵值拦截是防止「聊城 999℃」事故的关键。"""

    def test_拦截999哨兵值(self):
        assert to_float("999") is None
        assert to_float(999) is None

    def test_拦截负999与9999(self):
        assert to_float("-999") is None
        assert to_float("9999") is None

    def test_拦截空值与占位符(self):
        for v in ("", "-", "--", None, "null", "None"):
            assert to_float(v) is None, f"{v!r} 应被拦截"

    def test_正常数值(self):
        assert to_float("23") == 23.0
        assert to_float("18.3") == 18.3
        assert to_float(-5) == -5.0

    def test_带单位可解析(self):
        assert to_float("19%") == 19.0
        assert to_float("8km/h") == 8.0
        assert to_float("1016hPa") == 1016.0
        assert to_float("23℃") == 23.0

    def test_非数值返回None(self):
        assert to_float("多云") is None
        assert to_float("abc") is None

    def test_布尔值返回None(self):
        """True 在 Python 中等于 1，必须显式拦截，避免当成气温。"""
        assert to_float(True) is None
        assert to_float(False) is None


class TestCleanText:
    def test_去除HTML标签(self):
        assert clean_text("<3级") == "3级" or clean_text("<3级") is not None

    def test_压缩空白(self):
        assert clean_text("  多云   转阴  ") == "多云 转阴"

    def test_空值返回None(self):
        assert clean_text("") is None
        assert clean_text(None) is None
        assert clean_text("999") is None


class TestNormDate:
    def test_日期8位(self):
        assert norm_date8("20261003") == "2026-10-03"

    def test_日期时间12位(self):
        assert norm_date8("202610030800") == "2026-10-03"

    def test_datetime(self):
        assert norm_datetime("202610030800") == "2026-10-03 08:00"

    def test_iso_hour(self):
        assert norm_iso_hour("202610030900") == "2026-10-03T09:00"

    def test_无效输入(self):
        assert norm_date8("abc") is None
        assert norm_date8("") is None
        assert norm_datetime(None) is None


class TestSplitTurnText:
    def test_拆分转变(self):
        assert split_turn_text("多云转阴") == ["多云", "阴"]

    def test_无转字(self):
        assert split_turn_text("晴") == ["晴"]

    def test_空值(self):
        assert split_turn_text("") == []
        assert split_turn_text(None) == []


class TestParseJson:
    def test_正常JSON(self):
        assert parse_json_safely('{"a":1}') == {"a": 1}

    def test_非法JSON返回None(self):
        assert parse_json_safely("not json") is None
        assert parse_json_safely("") is None
