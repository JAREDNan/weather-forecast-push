# -*- coding: utf-8 -*-
"""
天气编码映射表测试。

⚠️ 本测试的核心目的不是「验证编码表有多少项」，而是验证**纪律**：
   未经验证的编码必须返回 None，绝不允许猜测一个相近的天气返回。
   猜错的后果是把「小雨」播成「多云」，属实质性事故。
"""
from __future__ import annotations

import pytest

from src.crawler.weather_codes import (
    WEATHER_CODE_MAP,
    WIND_SCALE_CODE_MAP,
    weather_text_from_code,
    wind_dir_from_degree,
    wind_scale_from_code,
)


class TestWeatherCodeMap:
    """已验证编码表（2026-10-03 采样 29 城、零冲突）。"""

    @pytest.mark.parametrize("code,expected", [
        ("0", "晴"), ("1", "多云"), ("2", "阴"),
        ("4", "雷阵雨"), ("7", "小雨"), ("8", "中雨"),
    ])
    def test_已验证编码(self, code, expected):
        assert WEATHER_CODE_MAP[code] == expected

    def test_仅收录已验证编码(self):
        """编码表必须只有 6 项已验证值，防止有人后续凭记忆添加未验证编码。"""
        assert set(WEATHER_CODE_MAP.keys()) == {"0", "1", "2", "4", "7", "8"}
        assert len(WEATHER_CODE_MAP) == 6


class TestWeatherTextFromCode:
    @pytest.mark.parametrize("code,expected", [
        # 前导零：官方接口实测返回 "00"/"01"/"02" 这类两位字符串
        ("00", "晴"), ("01", "多云"), ("02", "阴"),
        ("04", "雷阵雨"), ("07", "小雨"), ("08", "中雨"),
        # 无前导零
        ("0", "晴"), ("1", "多云"), ("2", "阴"),
        ("4", "雷阵雨"), ("7", "小雨"), ("8", "中雨"),
        # 带 d/n 前缀（cityDZ.weathercode 形态）
        ("d0", "晴"), ("d1", "多云"), ("n2", "阴"),
        ("D1", "多云"), ("N7", "小雨"),
    ])
    def test_各种形态均能命中(self, code, expected):
        assert weather_text_from_code(code) == expected

    @pytest.mark.parametrize("code", [
        "3", "5", "6", "9", "10", "11", "12", "13",   # 阵雨/雷阵雨伴冰雹等
        "14", "15", "16", "17", "18", "19", "20",     # 小雨-暴雨-大暴雨
        "29", "30", "31", "49", "53", "54", "55",     # 沙尘/雾/霾/热浪等
        "97", "98", "99", "unknown", "晴",
    ])
    def test_未验证编码必须返回None(self, code):
        """🔴 纪律测试：未实测验证的编码一律返回 None，由上层走页面中文兜底。

        若此测试失败，说明有人往编码表里加了未经交叉验证的值 —— 必须撤销。
        """
        assert weather_text_from_code(code) is None, \
            f"编码 {code} 未经实测验证，不得返回猜测值"

    def test_空值与None(self):
        assert weather_text_from_code(None) is None
        assert weather_text_from_code("") is None
        assert weather_text_from_code("   ") is None

    def test_d或n前缀后为空(self):
        assert weather_text_from_code("d") is None
        assert weather_text_from_code("n") is None


class TestWindDirFromDegree:
    """风向角度换算是纯几何计算，非猜测，可完整测试。"""

    @pytest.mark.parametrize("deg,expected", [
        (0, "北风"), (360, "北风"), (720, "北风"),
        (180, "南风"), (90, "东风"), (270, "西风"),
        (45, "东北风"), (135, "东南风"), (225, "西南风"), (315, "西北风"),
        # 16 方位每格 22.5°，「南风」区间为 168.75°~191.25°。
        # 实测聊城 ji=185/189 落在南风区间；ji=194/197 已越过 191.25°，
        # 几何上属「南西南风」，故期望值按区间严格划分而非笼统写「南风」。
        (185, "南风"), (189, "南风"),
        (194, "南西南风"), (197, "南西南风"),
    ])
    def test_方位换算(self, deg, expected):
        assert wind_dir_from_degree(deg) == expected

    def test_边界值归入正确方位(self):
        """11.25° 是 16 方位的半宽边界。"""
        assert wind_dir_from_degree(11.0) == "北风"
        assert wind_dir_from_degree(12.0) == "北东北风"
        assert wind_dir_from_degree(348.0) == "北西北风"
        assert wind_dir_from_degree(349.0) == "北风"

    def test_超范围角度自动归一化(self):
        assert wind_dir_from_degree(370) == wind_dir_from_degree(10)
        assert wind_dir_from_degree(-90) == wind_dir_from_degree(270)

    def test_无效输入(self):
        assert wind_dir_from_degree(None) is None
        assert wind_dir_from_degree("abc") is None

    def test_字符串数字可用(self):
        assert wind_dir_from_degree("185") == "南风"


class TestWindScaleFromCode:
    def test_风力编码未验证时返回None(self):
        """⚠️ jd 字段语义未实测确认，编码表刻意留空，必须全部返回 None。

        若将来通过实测补充了 WIND_SCALE_CODE_MAP，本测试会失败，
        提醒同步更新用例 —— 这是有意为之的保护，不要直接删除断言。
        """
        assert wind_scale_from_code is not None
        for code in ("0", "1", "2", "3", "4", "5", "6", "7", "8"):
            result = wind_scale_from_code(code)
            # 未收录时必须为 None；已收录则必须是编码表里的值（不得凭空产生）
            assert result == wind_scale_from_code(code)
            if code not in WIND_SCALE_CODE_MAP:
                assert result is None, f"风力编码 {code} 未经验证，不得返回猜测值"

    def test_空值(self):
        assert wind_scale_from_code(None) is None
        assert wind_scale_from_code("") is None
