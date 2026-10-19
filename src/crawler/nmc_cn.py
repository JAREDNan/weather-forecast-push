# -*- coding: utf-8 -*-
"""
中央气象台采集器（www.nmc.cn，国家气象中心）。

权威性最高，本项目用它做两件事：
  1. 全国/本地气象预警信息（rest/findAlarm，实测可用）
  2. 城市索引与站号校验（rest/province/{省代码}，实测可用）
  3. 从预报页 HTML 提取「发布时间」与「气象站号」，用于交叉校验
     weather.com.cn 的数据是否同源同批次

⚠️ 实测已失效的接口（2026-10-03，本模块一律不调用）：
   http://www.nmc.cn/rest/weather?stationid=54806   → {"data":""} 空
   http://www.nmc.cn/rest/real/54806                → 所有字段为空串
   http://www.nmc.cn/rest/real?stationid=54806       → HTTP 404
   http://www.nmc.cn/rest/tempchart/54806            → [] 空
   http://www.nmc.cn/rest/alarm                      → HTTP 417
   网上大量教程仍在引用前两个端点，照抄必然拿到空数据。
   因此**预报数据以 weather.com.cn 的 d1 系列接口为准**
   （该站页面同样标注「数据来源：中央气象台」，属同源）。

⚠️ nmc.cn 页面底部声明「本站所刊登的信息、数据和各种专栏材料，
   未经授权禁止下载使用」。本模块仅做低频（每日 1~2 次）、
   单点（仅聊城）的实时取用与署名标注，不批量下载、不镜像、
   不二次分发原始数据文件。详见 reports/02_爬虫合规检查.md 第四节。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import LocationConfig
from .http_client import FetchResult, PoliteHttpClient
from .parsers import clean_text, norm_datetime, parse_json_safely

log = logging.getLogger(__name__)

SOURCE = "nmc_cn"
BASE = "http://www.nmc.cn"
ROBOTS_URL = f"{BASE}/robots.txt"   # 实测 404，即无 robots 限制
# 预警接口：实测 pageSize 最大可用 100，按发布时间倒序返回全国预警
ALARM_URL = f"{BASE}/rest/findAlarm"
# 页面声明的版权信息，用于合规日志
COPYRIGHT_NOTE = "中央气象台页面声明「未经授权禁止下载使用」，本模块仅低频实时取用并署名"


@dataclass
class NmcSnapshot:
    """中央气象台采集结果。"""
    source: str = SOURCE
    collected_at: str = ""
    station_id: Optional[str] = None
    publish_time: Optional[str] = None       # 页面标注的预报发布时间
    page_update_time: Optional[str] = None   # 页面动态时间戳
    alarms: List[Dict[str, Any]] = field(default_factory=list)
    national_alarm_count: Optional[int] = None
    city_index: Optional[List[Dict[str, Any]]] = None
    warnings: List[str] = field(default_factory=list)
    raw_payload: Optional[Dict[str, Any]] = None
    raw_sha256: str = ""
    raw_file: Optional[str] = None
    ok: bool = False


class NmcCrawler:
    """中央气象台采集器。"""

    # 用于从预警标题中筛出本地相关条目
    LOCAL_KEYWORDS = ("聊城", "东昌府")
    PROVINCE_KEYWORDS = ("山东",)

    def __init__(self, client: PoliteHttpClient, location: LocationConfig):
        self.client = client
        self.location = location

    # ------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------
    def collect(self, *, fetch_alarms: bool = True,
                fetch_page: bool = True,
                fetch_city_index: bool = False) -> NmcSnapshot:
        """采集预警 + 页面元信息。

        Args:
            fetch_alarms: 是否拉取预警（默认拉取，关系人身安全）
            fetch_page: 是否请求预报页 HTML（提取站号与发布时间）
            fetch_city_index: 是否拉取山东省城市索引（默认关闭，
                只在需要校验城市代码时开启，避免多余请求）
        """
        snap = NmcSnapshot(
            collected_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        raw: Dict[str, Any] = {}

        if fetch_alarms:
            alarms, count = self._collect_alarms()
            snap.alarms = alarms
            snap.national_alarm_count = count
            raw["alarms"] = alarms
            if count is None:
                snap.warnings.append("预警接口未取到数据")

        if fetch_page:
            page_info = self._collect_page_meta()
            if page_info:
                snap.station_id = page_info.get("station_id")
                snap.publish_time = page_info.get("publish_time")
                snap.page_update_time = page_info.get("page_update_time")
                raw["page_meta"] = page_info
            else:
                snap.warnings.append("预报页元信息提取失败")

        if fetch_city_index:
            idx = self._collect_city_index()
            snap.city_index = idx
            raw["city_index"] = idx

        snap.raw_payload = raw
        snap.ok = bool(snap.alarms is not None and
                       (snap.station_id or snap.publish_time or
                        snap.national_alarm_count is not None))
        log.info("中央气象台采集：本地预警=%d 全国预警=%s 站号=%s 发布时间=%s",
                 len(snap.alarms), snap.national_alarm_count,
                 snap.station_id, snap.publish_time)
        return snap

    # ------------------------------------------------------------
    # 预警
    # ------------------------------------------------------------
    def _collect_alarms(self, page_size: int = 100) -> tuple[List[Dict[str, Any]], Optional[int]]:
        """拉取全国预警列表，本地筛选聊城/山东相关条目。

        实测返回结构：
          {"msg":"success","code":0,"data":{
             "page":{"pageNo":1,"pageSize":N,"count":607,"list":[
                 {"alertid":"...","issuetime":"2026-10-03 08:07",
                  "title":"河北省石家庄市高邑县气象台发布大风蓝色预警信号",
                  "url":"/publish/alarm/xxx.html",
                  "pic":"https://image.nmc.cn/assets/img/alarm/p0007004.png"}],
                 "totalPage":203},
             "provinceAlarms":[{"alertid":...,"title":"湖南省气象台发布暴雨蓝色预警"}],
             "stat":{"province":{"r":0,"b":0,"y":0,"o":0},"city":{...},"county":{...}}}}

        ⚠️ province=山东省 与 province=ASD 参数实测均返回 count=0，
           即该接口不支持按省过滤，必须全量拉取后在本地筛选。
        """
        res = self.client.get(ALARM_URL, params={
            "pageNo": "1", "pageSize": str(page_size),
            "signaltype": "", "signallevel": "",
        }, referer=f"{BASE}/publish/alarm.html")

        if not res.ok:
            log.warning("nmc 预警接口请求失败：%s", res.error)
            return [], None
        obj = parse_json_safely(res.text)
        if not isinstance(obj, dict):
            log.warning("nmc 预警接口返回非 JSON")
            return [], None
        data = obj.get("data")
        if not isinstance(data, dict):
            # data 可能为空串（该站部分接口失效时的表现）
            log.warning("nmc 预警接口 data 字段异常：%r", data)
            return [], None

        page = data.get("page") or {}
        items = page.get("list") or []
        total = page.get("count")
        province_items = data.get("provinceAlarms") or []

        matched: List[Dict[str, Any]] = []
        # 1) 县级/市级预警：按标题关键词匹配
        for a in list(items) + list(province_items):
            if not isinstance(a, dict):
                continue
            title = clean_text(a.get("title"))
            if not title:
                continue
            if not any(k in title for k in self.LOCAL_KEYWORDS) and \
                    not any(k in title for k in self.PROVINCE_KEYWORDS):
                continue
            level, stype = self._parse_alarm_title(title)
            url = clean_text(a.get("url"))
            matched.append({
                "source": SOURCE,
                "alarm_id": clean_text(a.get("alertid")) or title,
                "city_id": self.location.wcc_city_id,
                "title": title,
                "level": level,
                "signal_type": stype,
                "issue_time": norm_datetime(a.get("issuetime"))
                              or clean_text(a.get("issuetime")),
                "url": (BASE + url) if url and url.startswith("/") else url,
                "pic": clean_text(a.get("pic")),
                "scope": "local" if any(k in title for k in self.LOCAL_KEYWORDS)
                         else "province",
            })

        # 本地预警优先于省级预警
        matched.sort(key=lambda x: (0 if x.get("scope") == "local" else 1,
                                    x.get("issue_time") or ""), reverse=False)
        log.info("nmc 全国预警 %s 条，筛出聊城/山东相关 %d 条", total, len(matched))
        return matched, (int(total) if isinstance(total, int) else None)

    @staticmethod
    def _parse_alarm_title(title: str) -> tuple[Optional[str], Optional[str]]:
        """从预警标题中解析等级与类型。

        实测标题形如「河北省石家庄市高邑县气象台发布大风蓝色预警信号」
                     「湖南省气象台发布暴雨蓝色预警」
        ⚠️ 只做字符串切分，不改写标题原文（标题仍完整保留用于展示）。
        """
        level = None
        for lv in ("红色", "橙色", "黄色", "蓝色"):
            if lv in title:
                level = lv
                break
        stype = None
        for st in ("暴雨", "暴雪", "寒潮", "大风", "沙尘暴", "高温", "干旱",
                   "雷电", "冰雹", "霜冻", "大雾", "霾", "道路结冰", "森林火险",
                   "雷雨大风", "春季沙尘天气", "沙尘", "低温", "浓浮尘",
                   "龙卷风", "冻雨", "强对流", "臭氧", "大雪", "雨夹雪"):
            if st in title:
                stype = st
                break
        return level, stype

    # ------------------------------------------------------------
    # 预报页元信息（站号 + 发布时间）
    # ------------------------------------------------------------
    def _collect_page_meta(self) -> Optional[Dict[str, Any]]:
        """从聊城预报页 HTML 提取气象站号与官方发布时间。

        实测页面含内嵌 JS 变量 stationid（值 54806），
        以及文本「发布时间：10-03 08:00」与动态时间戳「2026-10-03 08:26」。
        """
        res = self.client.get(self.location.nmc_forecast_url,
                              referer=f"{BASE}/publish/forecast.html")
        if not res.ok or not res.text:
            log.warning("nmc 预报页请求失败：%s", res.error)
            return None
        text = res.text

        out: Dict[str, Any] = {"url": res.url}

        # 站号：优先取内嵌 JS 变量，其次正则兜底
        m = re.search(r"var\s+stationid\s*=\s*[\"']?(\d{5})", text)
        if not m:
            hits = re.findall(r"station[iI]d\D{0,8}?(\d{5})", text)
            uniq = sorted(set(hits))
            if len(uniq) == 1:
                m = None
                out["station_id"] = uniq[0]
            elif len(uniq) > 1:
                log.warning("nmc 页面出现多个候选站号 %s，不予采用（不猜测）", uniq)
        else:
            out["station_id"] = m.group(1)
        if "station_id" not in out and m:
            out["station_id"] = m.group(1)

        # 官方发布时间：「发布时间：10-03 08:00」
        m2 = re.search(r"发布时间[：:]\s*(\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})", text)
        if m2:
            out["publish_time_raw"] = clean_text(m2.group(1))
            out["publish_time"] = self._expand_mmdd(out["publish_time_raw"])

        # 页面动态时间戳：「2026-10-03 08:26」
        m3 = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", text)
        if m3:
            out["page_update_time"] = m3.group(1)

        # 站号与配置不一致时告警（防止站点变更导致取错城市）
        sid = out.get("station_id")
        if sid and self.location.station_id and sid != self.location.station_id:
            log.warning("⚠️ nmc 页面站号 %s 与配置 %s 不一致，请核实目标城市是否变更",
                        sid, self.location.station_id)
            out["station_id_mismatch"] = True

        return out if len(out) > 1 else None

    @staticmethod
    def _expand_mmdd(raw: Optional[str]) -> Optional[str]:
        """把「10-03 08:00」补全为「YYYY-MM-DD HH:MM」。

        跨年处理：若补全后的日期在未来超过 30 天，说明属于上一年。
        """
        if not raw:
            return None
        m = re.match(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", raw.strip())
        if not m:
            return None
        mon, day, hh, mi = (int(x) for x in m.groups())
        now = datetime.now()
        try:
            cand = datetime(now.year, mon, day, hh, mi)
        except ValueError:
            return None
        if (cand - now).days > 30:
            try:
                cand = datetime(now.year - 1, mon, day, hh, mi)
            except ValueError:
                return None
        return cand.strftime("%Y-%m-%d %H:%M")

    # ------------------------------------------------------------
    # 城市索引（校验用，默认不请求）
    # ------------------------------------------------------------
    def _collect_city_index(self) -> List[Dict[str, Any]]:
        """拉取本省城市索引。

        实测 http://www.nmc.cn/rest/province/ASD 返回纯 JSON 数组，
        山东共 122 个站点，其中聊城为：
          {"code":"XXeBL","province":"山东省","city":"聊城",
           "url":"/publish/forecast/ASD/liaocheng.html"}
        可用于校验 config.ini 中 nmc_city_path 是否正确。
        """
        res = self.client.get(
            f"{BASE}/rest/province/{self.location.nmc_province_code}",
            referer=f"{BASE}/publish/forecast.html")
        if not res.ok:
            log.warning("nmc 城市索引请求失败：%s", res.error)
            return []
        arr = parse_json_safely(res.text)
        if not isinstance(arr, list):
            log.warning("nmc 城市索引返回非数组")
            return []
        log.info("nmc 城市索引取到 %d 条", len(arr))
        return [x for x in arr if isinstance(x, dict)]

    def verify_city_path(self) -> Optional[str]:
        """校验配置中的 nmc_city_path 是否与官方索引一致，返回正确路径。"""
        idx = self._collect_city_index()
        for item in idx:
            city = clean_text(item.get("city"))
            url = clean_text(item.get("url"))
            if city and url and (self.location.nmc_province_code in url):
                # 匹配配置的城市名或路径
                if url.endswith(f"/{self.location.nmc_city_path}.html"):
                    log.info("✅ nmc_city_path=%s 校验通过（城市：%s）",
                             self.location.nmc_city_path, city)
                    return url
        # 未匹配到，列出候选供人工核对
        cands = [(clean_text(i.get("city")), clean_text(i.get("url")))
                 for i in idx if clean_text(i.get("city"))]
        log.warning("⚠️ nmc_city_path=%s 未在官方索引中找到，候选：%s",
                    self.location.nmc_city_path, cands[:20])
        return None
