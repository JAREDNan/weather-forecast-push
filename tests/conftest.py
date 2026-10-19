# -*- coding: utf-8 -*-
"""pytest 共享配置：把项目根加入 sys.path，并提供可复用的夹具。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import AppConfig, CrawlerConfig, LocationConfig   # noqa: E402


@pytest.fixture
def location() -> LocationConfig:
    """聊城市东昌府区的标准地点配置。"""
    return LocationConfig()


@pytest.fixture
def crawler_cfg() -> CrawlerConfig:
    """测试用限速配置（把间隔调小以加快测试，但仍 >= 合规下限）。"""
    return CrawlerConfig(min_interval_seconds=2.0, max_retries=0,
                         request_timeout=5)


@pytest.fixture
def app_cfg(tmp_path: Path) -> AppConfig:
    """指向临时目录的配置，避免测试污染正式数据库。"""
    cfg = AppConfig()
    cfg.db_path = tmp_path / "test.sqlite"
    cfg.raw_dir = tmp_path / "raw"
    cfg.logs_dir = tmp_path / "logs"
    for d in (cfg.raw_dir, cfg.logs_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg


# ============================================================
# 实测采样的官方响应片段（2026-10-03 真实取自聊城市接口）
# 用作测试夹具，保证测试数据与真实结构一致，而非凭空编造
# ============================================================
WEATHER_INDEX_RESPONSE = '''var cityDZ ={"weatherinfo":{"city":"聊城","cityname":"liaocheng","temp":"17","tempn":"12","weather":"多云转阴","wd":"南风转北风","ws":"4-5级转3-4级","weathercode":"d1","weathercoden":"n2","fctime":"202610030800"}};var alarmDZ ={"w":[]};var dataSK ={"nameen":"liaocheng","cityname":"聊城","city":"101121701","temp":"18.3","tempf":"65","WD":"南风","wde":"S","WS":"2级","wse":"8km/h","SD":"58%","sd":"58%","qy":"1016","njd":"21km","time":"08:50","rain":"0","rain24h":"0","aqi":"78","aqi_pm25":"78","weather":"多云","weathere":"Cloudy","weathercode":"d01","limitnumber":"","date":"10月03日(星期六)"};var dataZS ={"zs":{"date":"2026100308","ct_name":"穿衣指数","ct_hint":"较舒适","ct_des_s":"建议穿薄外套或牛仔裤等服装。","gm_name":"感冒指数","gm_hint":"较易发","gm_des_s":"风较大，较易发生感冒，注意防护。","uv_name":"紫外线强度指数","uv_hint":"强","uv_des_s":"涂擦SPF大于15、PA+防晒护肤品。"},"cn":"聊城"};var fc ={"f":[{"fa":"01","fb":"02","fc":"23","fd":"12","fe":"南风","ff":"北风","fg":"4-5级","fh":"3-4级","fk":"4","fl":"8","fm":"85.7","fn":"45.7","fi":"10/3","fj":"今天"},{"fa":"00","fb":"00","fc":"21","fd":"10","fe":"北风","ff":"北风","fg":"3-4级","fh":"<3级","fk":"8","fl":"8","fm":"84.3","fn":"24.3","fi":"10/4","fj":"星期日"},{"fa":"00","fb":"00","fc":"23","fd":"10","fe":"南风","ff":"南风","fg":"3-4级","fh":"<3级","fk":"4","fl":"4","fm":"81.4","fn":"22","fi":"10/5","fj":"星期一"}]};'''

# 官方 7 天预报页面的对应真值，用于断言解析结果与官方一致
OFFICIAL_TRUTH = {
    "2026-10-03": {"text": "多云转阴", "tmax": 23.0, "tmin": 12.0},
    "2026-10-04": {"text": "晴", "tmax": 21.0, "tmin": 10.0},
    "2026-10-05": {"text": "晴", "tmax": 23.0, "tmin": 10.0},
}

# nmc.cn findAlarm 的真实响应结构（2026-10-03 实测）
NMC_ALARM_RESPONSE = '''{"msg":"success","code":0,"data":{"page":{"pageNo":1,"pageSize":5,"count":690,"prev":1,"next":2,"list":[{"alertid":"13012741600000_20261003080722","issuetime":"2026-10-03 08:07","title":"河北省石家庄市高邑县气象台发布大风蓝色预警信号","url":"/publish/alarm/13012741600000_20261003080722.html","pic":"https://image.nmc.cn/assets/img/alarm/p0007004.png"},{"alertid":"37150041600000_20261003070000","issuetime":"2026-10-03 07:00","title":"山东省聊城市气象台发布大风黄色预警信号","url":"/publish/alarm/37150041600000_20261003070000.html","pic":"https://image.nmc.cn/assets/img/alarm/p0003003.png"}],"totalPage":138},"provinceAlarms":[],"stat":{"province":{"r":0,"b":0,"y":0,"o":0}}}}'''

# 软 404 页面（源站下线接口时的真实响应特征）
SOFT_404_RESPONSE = '''<!DOCTYPE HTML>
<html><head><title>非常抱歉，网页无法访问</title>
<script>var t=4;function refer(){if(t==0){location="http://www.weather.com.cn/#404";}}</script>
</head><body><div id="show">4</div></body></html>'''
