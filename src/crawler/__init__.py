# -*- coding: utf-8 -*-
"""
官方气象数据采集器包。

数据源优先级（对应提示词 §4.1）：
  1. 中央气象台  www.nmc.cn          —— 权威性最高，用于文案署名来源
  2. 中国天气网  www.weather.com.cn  —— 中国气象局公共气象服务中心主办，
                                        提供 d1 系列 JS-JSON 接口，主采集通道
  3. 中国气象数据网 data.cma.cn      —— 需实名注册，本项目不自动采集
  4. 山东省气象局 sd.cma.gov.cn      —— 本地预警，本轮请求超时未纳入
  5. 和风天气 QWeather               —— 商业 API，生活指数补充（可选）

所有采集器必须经由 crawler.http_client.PoliteHttpClient 发起请求，
禁止直接调用 requests，以保证限速、熔断、退避等合规约束生效。
"""
from .http_client import (
    CircuitOpenError,
    CrawlerError,
    FetchResult,
    PoliteHttpClient,
)

__all__ = [
    "PoliteHttpClient",
    "FetchResult",
    "CrawlerError",
    "CircuitOpenError",
]
