# -*- coding: utf-8 -*-
"""
合规 HTTP 客户端：限速、熔断、退避、重试。

对应提示词 §4.2 的硬性要求：
  * 单域名请求间隔 >= 2 秒
  * 全局并发 <= 2（本项目采集串行，天然满足）
  * 真实但非欺骗性的 User-Agent
  * 连续 3 次失败即熔断退避，不得暴力重试

所有对外请求必须经由本模块，禁止在采集器中直接调用 requests。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional
from urllib.parse import urlparse

import requests

from ..config import CrawlerConfig

log = logging.getLogger(__name__)


class CrawlerError(Exception):
    """采集层基础异常。"""


class CircuitOpenError(CrawlerError):
    """熔断器处于打开状态，请求被拒绝。"""


@dataclass
class _DomainState:
    """单域名的限速与熔断状态。"""
    last_request_at: float = 0.0
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0

    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self.circuit_open_until


@dataclass
class FetchResult:
    """一次抓取的结果。

    ok=False 时 text 为空、error 有值；调用方据此走降级链。
    """
    url: str
    status_code: Optional[int]
    text: str
    ok: bool
    error: Optional[str] = None
    elapsed_ms: int = 0
    encoding: str = "utf-8"

    @property
    def is_soft_404(self) -> bool:
        """中国天气网部分接口失效时返回 200 + 一个「网页无法访问」的 HTML。

        仅凭 status_code==200 判断会误认为成功，必须识别这种软 404，
        否则解析器会拿到一整个 404 页面却以为拿到了数据。
        """
        if self.ok and self.text:
            head = self.text[:3000]
            return "网页无法访问" in head and "<!DOCTYPE HTML" in head
        return False


class PoliteHttpClient:
    """带限速与熔断的 HTTP 客户端。

    线程安全：内部状态由锁保护，可安全用于 APScheduler 的多任务场景。
    """

    def __init__(self, config: Optional[CrawlerConfig] = None,
                 session: Optional[requests.Session] = None):
        self.config = config or CrawlerConfig()
        self._session = session or requests.Session()
        self._session.headers.update({
            "User-Agent": self.config.user_agent(),
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        })
        self._states: Dict[str, _DomainState] = {}
        self._lock = threading.RLock()
        # 统计信息，用于运行报告
        self._stats: Dict[str, int] = {
            "requests": 0, "success": 0, "failed": 0,
            "throttled_waits": 0, "circuit_rejects": 0, "soft_404": 0,
        }

    # ------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------
    @staticmethod
    def _domain_of(url: str) -> str:
        host = urlparse(url).netloc.lower()
        return host or url

    def _state(self, domain: str) -> _DomainState:
        with self._lock:
            st = self._states.get(domain)
            if st is None:
                st = _DomainState()
                self._states[domain] = st
            return st

    def _throttle(self, domain: str) -> None:
        """按域名限速：确保距上次请求 >= min_interval_seconds。"""
        st = self._state(domain)
        min_iv = max(2.0, float(self.config.min_interval_seconds))
        with self._lock:
            now = time.monotonic()
            wait = min_iv - (now - st.last_request_at)
            if wait > 0:
                self._stats["throttled_waits"] += 1
                log.debug("限速等待 %.2f 秒（域名 %s）", wait, domain)
                time.sleep(wait)
            st.last_request_at = time.monotonic()

    def _check_circuit(self, domain: str) -> None:
        st = self._state(domain)
        with self._lock:
            if st.circuit_open:
                remain = st.circuit_open_until - time.monotonic()
                self._stats["circuit_rejects"] += 1
                raise CircuitOpenError(
                    f"域名 {domain} 已熔断（连续 {st.consecutive_failures} 次失败），"
                    f"剩余冷却 {int(remain)} 秒"
                )

    def _record_outcome(self, domain: str, success: bool) -> None:
        st = self._state(domain)
        with self._lock:
            if success:
                st.consecutive_failures = 0
                st.circuit_open_until = 0.0
                self._stats["success"] += 1
            else:
                st.consecutive_failures += 1
                self._stats["failed"] += 1
                if st.consecutive_failures >= self.config.circuit_break_threshold:
                    st.circuit_open_until = (
                        time.monotonic() + float(self.config.circuit_break_cooldown)
                    )
                    log.warning(
                        "域名 %s 连续失败 %d 次，熔断 %d 秒",
                        domain, st.consecutive_failures,
                        int(self.config.circuit_break_cooldown),
                    )

    @property
    def stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def reset_circuit(self, domain: Optional[str] = None) -> None:
        """手动复位熔断（用于长驻进程或测试）。"""
        with self._lock:
            if domain is None:
                for st in self._states.values():
                    st.consecutive_failures = 0
                    st.circuit_open_until = 0.0
            else:
                st = self._states.get(self._domain_of(domain)
                                      if "://" in domain else domain)
                if st is not None:
                    st.consecutive_failures = 0
                    st.circuit_open_until = 0.0

    # ------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------
    def get(self, url: str, *, referer: Optional[str] = None,
            params: Optional[Dict[str, str]] = None,
            encoding: Optional[str] = None,
            allow_redirects: bool = True) -> FetchResult:
        """发起一次合规的 GET 请求。

        Args:
            url: 目标地址。
            referer: 部分数据接口需要 Referer 才返回内容。
            params: 查询参数。
            encoding: 强制指定响应编码；None 则自动探测。
            allow_redirects: 是否跟随重定向。

        Returns:
            FetchResult；失败时 ok=False，绝不抛异常（熔断除外，
            熔断属预期控制流，调用方可捕获后走降级链）。
        """
        domain = self._domain_of(url)

        try:
            self._check_circuit(domain)
        except CircuitOpenError as exc:
            log.warning("熔断拒绝请求 %s：%s", url, exc)
            return FetchResult(url=url, status_code=None, text="", ok=False,
                               error=str(exc))

        self._throttle(domain)

        headers: Dict[str, str] = {}
        if referer:
            headers["Referer"] = referer
        # ⚠️ HTTP 头必须 latin-1 可编码（RFC 7230）。
        # 曾因 UA 含中文联系方式导致 UnicodeEncodeError 使全部请求失败，
        # 此处做兜底过滤：非 ASCII 的头值直接剔除该项，并记录告警。
        for k in list(headers.keys()):
            try:
                str(headers[k]).encode("latin-1")
            except UnicodeEncodeError:
                log.warning("请求头 %s 含非 ASCII 字符，已剔除以免请求失败", k)
                headers.pop(k, None)

        last_err: Optional[str] = None
        attempts = 1 + max(0, int(self.config.max_retries))
        started = time.monotonic()

        for attempt in range(1, attempts + 1):
            try:
                with self._lock:
                    self._stats["requests"] += 1
                resp = self._session.get(
                    url, params=params, headers=headers,
                    timeout=self.config.request_timeout,
                    allow_redirects=allow_redirects,
                )
                # 编码处理：requests 对 text/* 默认 ISO-8859-1，需修正
                if encoding:
                    resp.encoding = encoding
                elif not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                    resp.encoding = resp.apparent_encoding or "utf-8"
                text = resp.text
                elapsed = int((time.monotonic() - started) * 1000)

                if 200 <= resp.status_code < 300:
                    self._record_outcome(domain, success=True)
                    result = FetchResult(
                        url=str(resp.url), status_code=resp.status_code,
                        text=text, ok=True, elapsed_ms=elapsed,
                        encoding=resp.encoding or "utf-8",
                    )
                    if result.is_soft_404:
                        # 200 但内容是 404 页面 —— 记为失败，触发降级
                        self._record_outcome(domain, success=False)
                        with self._lock:
                            self._stats["soft_404"] += 1
                        log.warning("软 404（状态 200 但内容为错误页）：%s", url)
                        result.ok = False
                        result.error = "soft_404"
                    return result

                # 4xx：客户端错误，重试无意义（限速导致的 429 除外）
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    self._record_outcome(domain, success=False)
                    return FetchResult(
                        url=url, status_code=resp.status_code, text="",
                        ok=False, error=f"HTTP {resp.status_code}",
                        elapsed_ms=elapsed,
                    )

                last_err = f"HTTP {resp.status_code}"
                log.warning("第 %d/%d 次请求失败 %s：%s",
                            attempt, attempts, url, last_err)

            except (requests.Timeout, requests.ConnectionError) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                log.warning("第 %d/%d 次请求异常 %s：%s",
                            attempt, attempts, url, last_err)
            except requests.RequestException as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                log.warning("第 %d/%d 次请求错误 %s：%s",
                            attempt, attempts, url, last_err)

            # 指数退避：1s、2s、4s…（不超过限速间隔的 4 倍）
            if attempt < attempts:
                backoff = min(2.0 ** (attempt - 1),
                              max(4.0, self.config.min_interval_seconds * 4))
                time.sleep(backoff)

        self._record_outcome(domain, success=False)
        return FetchResult(
            url=url, status_code=None, text="", ok=False,
            error=last_err or "unknown error",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "PoliteHttpClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
