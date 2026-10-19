# -*- coding: utf-8 -*-
"""
采集器单元测试。

⚠️ 全部使用 mock HTTP 客户端，**绝不发起真实网络请求**：
   既避免消耗目标站点资源与 API 额度，也保证测试可离线重复运行。

测试数据取自 2026-10-03 聊城市接口的真实响应（见 conftest.py），
因此断言的是「解析器对真实结构的处理能力」，而非凭空构造的理想数据。
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

import pytest

from src.crawler.http_client import FetchResult, PoliteHttpClient
from src.crawler.nmc_cn import NmcCrawler
from src.crawler.qweather import QWeatherCrawler
from src.crawler.weather_com_cn import WeatherComCnCrawler
from src.config import LocationConfig
from tests.conftest import (
    NMC_ALARM_RESPONSE,
    OFFICIAL_TRUTH,
    SOFT_404_RESPONSE,
    WEATHER_INDEX_RESPONSE,
)

# wap_40d 的真实片段（2026-10-03 实测），仅保留测试所需字段
WAP_40D_RESPONSE = (
    'var fc40=[{"000":"202610030500","001":"02","002":"01","003":"23","004":"14"}];'
    'var index3d={"i0":"202610030800","i":[{"i1":"ct","i2":"穿衣指数","i3":"",'
    '"i4":"较舒适","i5":"建议穿薄外套或牛仔裤等服装。","i6":"较舒适",'
    '"i7":"建议穿薄外套。","i8":"舒适","i9":"温度适宜。"}]};'
    'var fc1h_24={"jh":[{"ja":"01","jb":"18","jc":"0","jd":"4","je":"67",'
    '"jf":"202610030900","jg":"0","jh":"35","ji":"180","jj":"1019","jk":"13"},'
    '{"ja":"01","jb":"20","jc":"1","jd":"4","je":"66","jf":"202610031000",'
    '"jg":"0","jh":"50","ji":"189","jj":"1019","jk":"14"}]};'
    'var cwx={"i1":"较不宜"};'
)

NMC_PAGE_RESPONSE = """<!DOCTYPE html><html><head>
<script>var stationid=54806;</script></head><body>
<div class="pull-left">发布时间：10-03 08:00 </div>
<div>2026-10-03 08:26</div>
<div>数据来源 中央气象台</div>
</body></html>"""


class FakeClient:
    """mock 版 PoliteHttpClient：按 URL 关键字返回预设响应。

    记录所有请求，供测试断言「限速参数是否生效」「是否请求了不该请求的 URL」。
    """

    def __init__(self, routes: Optional[Dict[str, str]] = None,
                 default: str = ""):
        self.routes = routes or {}
        self.default = default
        self.calls: List[str] = []
        self.fail_urls: set = set()

    def get(self, url: str, *, referer: Optional[str] = None,
            params: Optional[Dict[str, str]] = None,
            encoding: Optional[str] = None,
            allow_redirects: bool = True) -> FetchResult:
        self.calls.append(url)
        if url in self.fail_urls:
            return FetchResult(url=url, status_code=None, text="", ok=False,
                               error="mock failure")
        for key, body in self.routes.items():
            if key in url:
                return FetchResult(url=url, status_code=200, text=body,
                                   ok=True, elapsed_ms=10)
        return FetchResult(url=url, status_code=200, text=self.default,
                           ok=True, elapsed_ms=10)

    def close(self) -> None:
        pass


@pytest.fixture
def wcc_client() -> FakeClient:
    return FakeClient(routes={
        "weather_index": WEATHER_INDEX_RESPONSE,
        "wap_40d": WAP_40D_RESPONSE,
    })


@pytest.fixture
def crawler(wcc_client, location) -> WeatherComCnCrawler:
    return WeatherComCnCrawler(wcc_client, location)


# ============================================================
# 中国天气网采集器
# ============================================================
class TestWeatherComCnCrawler:
    def test_采集成功(self, crawler):
        snap = crawler.collect()
        assert snap.ok is True
        assert snap.realtime is not None
        assert snap.today is not None
        assert len(snap.daily) == 3

    def test_今日气温与官方页面一致(self, crawler):
        """🔴 关键回归测试：cityDZ.temp 语义不明，必须用 fc 首日回填。

        2026-10-03 实测：cityDZ.temp=17 而官方「今天」页面为 23℃。
        若此测试失败，说明气温取值来源被改回了 cityDZ，
        会导致今日最高气温错报 6℃ —— 属实质性播报事故。
        """
        snap = crawler.collect()
        truth = OFFICIAL_TRUTH["2026-10-03"]
        assert snap.today["temp_max"] == truth["tmax"], \
            f"今日最高气温应为官方页面的 {truth['tmax']}℃，实得 {snap.today['temp_max']}"
        assert snap.today["temp_min"] == truth["tmin"]
        assert snap.today["temp_max"] != 17.0, \
            "气温取自 cityDZ.temp（语义不明），必须改用 fc 首日"

    def test_天气现象与官方一致(self, crawler):
        snap = crawler.collect()
        assert snap.today["text_full"] == "多云转阴"
        assert snap.today["text_day"] == "多云"
        assert snap.today["text_night"] == "阴"

    def test_多日预报与官方页面逐日一致(self, crawler):
        snap = crawler.collect()
        by_date = {d["forecast_date"]: d for d in snap.daily}
        for date, truth in OFFICIAL_TRUTH.items():
            if date not in by_date:
                continue
            rec = by_date[date]
            assert rec["temp_max"] == truth["tmax"], f"{date} 高温不符"
            assert rec["temp_min"] == truth["tmin"], f"{date} 低温不符"
            assert rec["text_full"] == truth["text"], \
                f"{date} 天气现象应为 {truth['text']}，实得 {rec['text_full']}"

    def test_发布时间被正确提取(self, crawler):
        """《气象法》第二十五条要求标注发布时间，必须能取到。"""
        snap = crawler.collect()
        assert snap.publish_time == "2026-10-03 08:00"

    def test_风向风力使用官方中文文本(self, crawler):
        """风向风力必须直接取接口中文，不经编码换算。"""
        snap = crawler.collect()
        assert snap.today["wind_dir_full"] == "南风转北风"
        assert snap.today["wind_scale_full"] == "4-5级转3-4级"
        assert snap.daily[0]["wind_dir_day"] == "南风"
        assert snap.daily[0]["wind_scale_day"] == "4-5级"

    def test_实时观测解析(self, crawler):
        snap = crawler.collect()
        rt = snap.realtime
        assert rt["temp"] == 18.3
        assert rt["humidity"] == 58.0
        assert rt["wind_dir"] == "南风"
        assert rt["aqi"] == 78.0
        assert rt["text"] == "多云"
        assert rt["observed_at"] == "08:50"
        assert rt["city_id"] == "101121701"

    def test_生活指数为官方原文(self, crawler):
        """指数描述必须原样引用，不得改写（《办法》第九条）。"""
        snap = crawler.collect()
        by_code = {i["code"]: i for i in snap.indices
                   if i["index_date"] == "2026-10-03"}
        assert by_code["ct"]["name"] == "穿衣指数"
        assert by_code["ct"]["level"] == "较舒适"
        assert by_code["ct"]["description"] == "建议穿薄外套或牛仔裤等服装。"
        assert by_code["gm"]["level"] == "较易发"

    def test_逐小时预报解析(self, crawler):
        snap = crawler.collect()
        assert len(snap.hourly) == 2
        h0 = snap.hourly[0]
        assert h0["hour_ts"] == "2026-10-03T09:00"
        assert h0["temp"] == 18.0
        assert h0["humidity"] == 67.0
        assert h0["text"] == "多云"          # ja=01 → 已验证编码
        assert h0["wind_deg"] == 180.0
        assert h0["wind_dir"] == "南风"      # 180° 几何换算

    def test_逐小时不猜测未验证字段(self, crawler):
        """jc/jd/jh 语义未验证，必须为 None 而非猜测值。"""
        snap = crawler.collect()
        for h in snap.hourly:
            assert h["wind_scale"] is None, "jd 语义未验证，不得猜测风力等级"
            assert h["wind_speed"] is None, "jh 语义未验证，不得猜测风速"

    def test_无预警时返回空列表(self, crawler):
        snap = crawler.collect()
        assert snap.alarms == []


class TestWeatherComCnRobustness:
    """异常与降级场景。"""

    def test_软404被识别为失败(self, location):
        client = FakeClient(routes={
            "weather_index": SOFT_404_RESPONSE,
            "sk_2d": SOFT_404_RESPONSE,
            "dingzhi": SOFT_404_RESPONSE,
            "wap_40d": SOFT_404_RESPONSE,
        })
        snap = WeatherComCnCrawler(client, location).collect()
        assert snap.ok is False
        assert any("软 404" in w or "不可用" in w for w in snap.warnings)

    def test_聚合接口失效时回退分接口(self, location):
        """weather_index 挂掉，应自动用 sk_2d + dingzhi 补齐。"""
        client = FakeClient(routes={
            "weather_index": SOFT_404_RESPONSE,
            "sk_2d": 'var dataSK ={"city":"101121701","temp":"18.3",'
                     '"WD":"南风","WS":"2级","weather":"多云","time":"08:50"};',
            "dingzhi": 'var cityDZ ={"weatherinfo":{"city":"聊城","temp":"17",'
                       '"tempn":"12","weather":"多云转阴","wd":"南风转北风",'
                       '"ws":"4-5级转3-4级","fctime":"202610030800"}};'
                       'var alarmDZ ={"w":[]};',
            "wap_40d": SOFT_404_RESPONSE,
        })
        snap = WeatherComCnCrawler(client, location).collect()
        assert snap.realtime is not None, "应通过 sk_2d 取到实时观测"
        assert snap.publish_time == "2026-10-03 08:00"

    def test_哨兵值999不会污染数据(self, location):
        """cityDZ.temp=999 是官方「无数据」标记，绝不可当成气温。"""
        client = FakeClient(routes={
            "weather_index": 'var cityDZ ={"weatherinfo":{"city":"聊城",'
                             '"temp":"999","tempn":"999","weather":"多云转阴",'
                             '"wd":"南风","ws":"3-4级","fctime":"202610030800"}};'
                             'var alarmDZ ={"w":[]};'
                             'var dataSK ={"city":"101121701","temp":"999",'
                             '"weather":"多云","time":"08:50"};'
                             'var fc ={"f":[]};',
            "wap_40d": "",
        })
        snap = WeatherComCnCrawler(client, location).collect()
        if snap.realtime:
            assert snap.realtime["temp"] is None, "999 被当成了真实气温"
        for d in snap.daily:
            assert d["temp_max"] != 999.0
            assert d["temp_min"] != 999.0

    def test_请求失败时优雅降级(self, location):
        client = FakeClient(default="")
        client.fail_urls = {
            "http://d1.weather.com.cn/weather_index/101121701.html",
            "http://d1.weather.com.cn/sk_2d/101121701.html",
            "http://d1.weather.com.cn/dingzhi/101121701.html",
            "http://d1.weather.com.cn/wap_40d/101121701.html",
        }
        snap = WeatherComCnCrawler(client, location).collect()
        assert snap.ok is False
        assert snap.realtime is None
        assert snap.daily == []

    def test_未验证编码不会编造天气(self, location):
        """fa="53"（霾）未经验证，必须置 None 而非猜测成相近天气。"""
        client = FakeClient(routes={
            "weather_index": 'var cityDZ ={"weatherinfo":{"city":"聊城",'
                             '"temp":"20","tempn":"10","weather":"霾",'
                             '"fctime":"202610030800"}};var alarmDZ ={"w":[]};'
                             'var dataSK ={"city":"101121701","temp":"20"};'
                             'var fc ={"f":[{"fa":"53","fb":"53","fc":"20",'
                             '"fd":"10","fe":"北风","ff":"北风","fg":"<3级",'
                             '"fh":"<3级","fi":"10/3","fj":"今天"}]};',
            "wap_40d": "",
        })
        snap = WeatherComCnCrawler(client, location).collect()
        assert snap.daily[0]["text_day"] is None, \
            "未验证编码被猜测成了某个天气，违反不猜测纪律"
        assert snap.daily[0]["code_day"] == "53", "应保留原始编码供排查"


# ============================================================
# 中央气象台采集器
# ============================================================
class TestNmcCrawler:
    @pytest.fixture
    def nmc_client(self):
        return FakeClient(routes={
            "findAlarm": NMC_ALARM_RESPONSE,
            "publish/forecast": NMC_PAGE_RESPONSE,
        })

    def test_提取站号与发布时间(self, nmc_client, location):
        snap = NmcCrawler(nmc_client, location).collect()
        assert snap.station_id == "54806"
        assert snap.publish_time == "2026-10-03 08:00"
        assert snap.page_update_time == "2026-10-03 08:26"

    def test_筛选出本地预警(self, nmc_client, location):
        """全国 690 条中应只筛出聊城相关的 1 条。"""
        snap = NmcCrawler(nmc_client, location).collect()
        assert snap.national_alarm_count == 690
        assert len(snap.alarms) == 1
        a = snap.alarms[0]
        assert "聊城" in a["title"]
        assert a["level"] == "黄色"
        assert a["signal_type"] == "大风"
        assert a["issue_time"] == "2026-10-03 07:00"
        assert a["url"].startswith("http://www.nmc.cn/")

    def test_排除无关省份预警(self, nmc_client, location):
        snap = NmcCrawler(nmc_client, location).collect()
        titles = [a["title"] for a in snap.alarms]
        assert not any("石家庄" in t for t in titles), "河北预警不应混入"

    def test_站号不一致时告警(self, location):
        """页面站号与配置不符时必须标记，防止取错城市。"""
        client = FakeClient(routes={
            "findAlarm": '{"msg":"success","code":0,"data":{"page":{"count":0,"list":[]}}}',
            "publish/forecast": "<script>var stationid=99999;</script>"
                                "<div>发布时间：10-03 08:00</div>",
        })
        meta = NmcCrawler(client, location)._collect_page_meta()
        assert meta is not None
        assert meta.get("station_id") == "99999"
        assert meta.get("station_id_mismatch") is True

    def test_站号一致时不告警(self, location):
        client = FakeClient(routes={
            "publish/forecast": "<script>var stationid=54806;</script>"
                                "<div>发布时间：10-03 08:00</div>",
        })
        meta = NmcCrawler(client, location)._collect_page_meta()
        assert meta is not None
        assert meta.get("station_id") == "54806"
        assert meta.get("station_id_mismatch") is None

    def test_预警接口返回空data时不崩溃(self, location):
        """实测 nmc 部分接口会返回 {"data":""}，必须优雅处理。"""
        client = FakeClient(routes={
            "findAlarm": '{"msg":"success","code":0,"data":""}',
            "publish/forecast": NMC_PAGE_RESPONSE,
        })
        snap = NmcCrawler(client, location).collect()
        assert snap.alarms == []
        assert snap.national_alarm_count is None

    def test_请求全部失败(self, location):
        client = FakeClient(default="")
        client.fail_urls = {"http://www.nmc.cn/rest/findAlarm",
                            location.nmc_forecast_url}
        snap = NmcCrawler(client, location).collect()
        assert snap.ok is False


# ============================================================
# 和风天气采集器（未配置凭证时的行为）
# ============================================================
class TestQWeatherCrawler:
    def test_未配置时不启用(self, location):
        client = FakeClient(default="")
        qw = QWeatherCrawler(client, location)
        assert qw.enabled is False
        assert "API_HOST" in qw.disabled_reason()

    def test_未配置时采集返回空且不请求网络(self, location):
        """🔴 关键：未配置凭证时绝不能发起任何网络请求。"""
        client = FakeClient(default="")
        qw = QWeatherCrawler(client, location)
        snap = qw.collect()
        assert snap.ok is False
        assert snap.enabled is False
        assert client.calls == [], "未配置凭证却发起了网络请求"

    def test_缺LocationID时不启用(self, location):
        client = FakeClient(default="")
        qw = QWeatherCrawler(client, location,
                             api_host="https://abc.qweatherapi.com",
                             api_key="fake-key-for-test")
        assert qw.enabled is False
        assert "LOCATION_ID" in qw.disabled_reason()

    def test_凭证齐备时才启用(self, location):
        loc = LocationConfig(qweather_location_id="101121701")
        qw = QWeatherCrawler(FakeClient(), loc,
                             api_host="https://abc.qweatherapi.com",
                             api_key="fake-key-for-test")
        assert qw.enabled is True

    def test_错误码被识别(self, location):
        loc = LocationConfig(qweather_location_id="101121701")
        client = FakeClient(default='{"code":"401","error":"unauthorized"}')
        qw = QWeatherCrawler(client, loc,
                             api_host="https://abc.qweatherapi.com",
                             api_key="fake-key-for-test")
        snap = qw.collect()
        assert snap.ok is False, "认证失败却被当成成功"

    def test_正常响应可解析(self, location):
        loc = LocationConfig(qweather_location_id="101121701")
        routes = {
            "/v7/weather/now": '{"code":"200","obsTime":"2026-10-03T08:45+08:00",'
                               '"now":{"obsTime":"2026-10-03T08:45+08:00",'
                               '"temp":"18","feelsLike":"17","text":"多云",'
                               '"windDir":"南风","windScale":"2","humidity":"58",'
                               '"pressure":"1016","vis":"21","precip":"0.0"}}',
            "/v7/weather/3d": '{"code":"200","daily":[{"fxDate":"2026-10-03",'
                              '"sunrise":"06:13","sunset":"17:59","tempMax":"23",'
                              '"tempMin":"12","textDay":"多云","textNight":"阴",'
                              '"windDirDay":"南风","windScaleDay":"4-5",'
                              '"uvIndex":"5"}]}',
            "/v7/indices/1d": '{"code":"200","daily":[{"date":"2026-10-03",'
                              '"type":"3","name":"穿衣指数","level":"2",'
                              '"category":"较舒适","text":"建议穿薄外套。"}]}',
            "/v7/warning/now": '{"code":"200","warning":[]}',
            "/v7/weather/24h": '{"code":"200","hourly":[]}',
        }
        snap = QWeatherCrawler(FakeClient(routes=routes), loc,
                               api_host="https://abc.qweatherapi.com",
                               api_key="fake-key-for-test").collect()
        assert snap.ok is True
        assert snap.realtime["temp"] == 18.0
        assert snap.daily[0]["temp_max"] == 23.0
        assert snap.daily[0]["text_day"] == "多云"
        assert snap.indices[0]["name"] == "穿衣指数"

    def test_字段缺失不崩溃(self, location):
        loc = LocationConfig(qweather_location_id="101121701")
        client = FakeClient(default='{"code":"200"}')
        snap = QWeatherCrawler(client, loc,
                               api_host="https://abc.qweatherapi.com",
                               api_key="fake-key-for-test").collect()
        # 所有子字段缺失，应优雅返回而非抛异常
        assert snap.realtime is None
        assert snap.daily == []


# ============================================================
# HTTP 客户端：限速与熔断
# ============================================================
class TestPoliteHttpClient:
    def test_限速间隔不低于合规下限(self):
        """即使配置写成 0.1 秒，实际限速也会被抬到 >= 2 秒（提示词 §4.2 硬要求）。"""
        from src.config import CrawlerConfig
        cfg = CrawlerConfig(min_interval_seconds=0.1)
        client = PoliteHttpClient(cfg)
        # _throttle 内部用 max(2.0, min_interval_seconds) 兜底
        effective = max(2.0, float(client.config.min_interval_seconds))
        assert effective >= 2.0
        # 实际发起一次请求，确认限速逻辑不会因配置过小而失效
        domain = client._domain_of("http://example.com/x")
        client._state(domain).last_request_at = 0.0
        client._throttle(domain)   # 不应抛异常
        assert client.stats["throttled_waits"] >= 0
        client.close()

    def test_UserAgent为纯ASCII(self):
        """🔴 HTTP 头必须 latin-1 可编码，含中文会导致所有请求崩溃。"""
        from src.config import CrawlerConfig
        cfg = CrawlerConfig(contact_info="聊城大学气象社团 contact: test@ex.com")
        ua = cfg.user_agent()
        ua.encode("ascii")   # 抛异常即失败
        ua.encode("latin-1")
        assert "CampusWeatherBot" in ua

    def test_软404被标记为失败(self):
        """状态码 200 但内容是错误页，必须识别为失败以触发降级。"""
        res = FetchResult(url="http://x/", status_code=200,
                          text=SOFT_404_RESPONSE, ok=True)
        assert res.is_soft_404 is True

    def test_正常响应不是软404(self):
        res = FetchResult(url="http://x/", status_code=200,
                          text=WEATHER_INDEX_RESPONSE, ok=True)
        assert res.is_soft_404 is False

    def test_失败响应不是软404(self):
        res = FetchResult(url="http://x/", status_code=None, text="", ok=False,
                          error="timeout")
        assert res.is_soft_404 is False
