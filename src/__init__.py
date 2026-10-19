# -*- coding: utf-8 -*-
"""
校园气象预报自动编撰系统 —— 源码包。

模块划分（对应提示词 PDF 第 III、IV 章）：
  config.py            配置加载（.env + config.ini），密钥脱敏
  weather_client.py    数据采集编排（PDF 模块一）：多源 + 降级链 + 交叉校验
  content_generator.py 文案生成（PDF 模块二）：人性化中文 + 法规署名
  wechat_manager.py    微信交互（PDF 模块三）：多路线推送，惰性导入 SDK
  scheduler_main.py    调度入口（PDF 模块四）：每日定时任务 + 异常告警
  crawler/             官方站点采集器（提示词 §4）
  storage/             SQLite 与原始报文归档
  ml/                  深度学习模块（提示词 §5）—— 本轮按用户要求暂缓

⚠️ 深度学习暂缓的原因不只是「没有服务器」：
   《气象预报发布与传播管理办法》第十条规定，科研教学单位、学术团体和个人
   研究形成的气象预报意见和结论，可以提供给气象台制作预报时参考，
   但**不得以任何形式向社会公开发布**。
   原 PDF §5.5 计划把「模型预测值」与「官方预报值」并列展示在推送文案中，
   该做法涉嫌违反此条。启动 §5 前必须先解决合规问题，
   详见 reports/02_爬虫合规检查.md 第五节。
"""

__version__ = "1.0.0"
__all__ = [
    "config",
    "weather_client",
    "content_generator",
    "wechat_manager",
    "scheduler_main",
]
