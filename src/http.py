"""Resilient HTTP layer shared by every provider.

Features that the reliability requirements ask for, in one place:
timeouts, bounded retries with exponential backoff + jitter, ``Retry-After``
aware 429 handling, a token-bucket rate limiter per host, and a small in-process
response cache so a daily-changing endpoint is not hit once per venue.
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

from .logging_utils import get_logger

log = get_logger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 509, 522, 524})


class HttpError(RuntimeError):
    """Non-retryable, or retries exhausted."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body[:500]


class RateLimitError(HttpError):
    """The remote asked us to slow down and we ran out of retries."""


class RateLimiter:
    """Simple thread-safe token bucket; ``rps<=0`` disables limiting."""

    def __init__(self, rps: float, burst: Optional[float] = None) -> None:
        self.rps = float(rps)
        self.capacity = float(burst if burst is not None else max(1.0, rps))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        if self.rps <= 0:
            return 0.0
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rps)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                sleep_for = deficit / self.rps
            time.sleep(min(sleep_for, 5.0))
            waited += sleep_for


@dataclass
class _CacheEntry:
    expires_at: float
    status: int
    payload: Any


class HttpClient:
    """Thin wrapper over :mod:`requests` with retry/caching policy baked in."""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        connect_timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 1.5,
        backoff_max: float = 30.0,
        user_agent: str = "valve-food-monitor/1.0",
        cache_ttl: int = 300,
        rate_limit_rps: float = 0.0,
        session: Optional[requests.Session] = None,
        sleep=time.sleep,
    ) -> None:
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.user_agent = user_agent
        self.cache_ttl = cache_ttl
        self.limiter = RateLimiter(rate_limit_rps)
        self.session = session or requests.Session()
        self._sleep = sleep
        self._cache: Dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()
        self.request_count = 0

    # -- cache ------------------------------------------------------------- #

    @staticmethod
    def _cache_key(method: str, url: str, params: Any, body: Any) -> str:
        blob = json.dumps([method, url, params, body], sort_keys=True, default=str)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> Optional[_CacheEntry]:
        with self._lock:
            entry = self._cache.get(key)
            if entry and entry.expires_at > time.monotonic():
                return entry
            if entry:
                self._cache.pop(key, None)
        return None

    def _cache_put(self, key: str, status: int, payload: Any, ttl: Optional[int]) -> None:
        ttl = self.cache_ttl if ttl is None else ttl
        if ttl <= 0:
            return
        with self._lock:
            self._cache[key] = _CacheEntry(time.monotonic() + ttl, status, payload)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # -- core -------------------------------------------------------------- #

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        cache_ttl: Optional[int] = None,
        expect_json: bool = True,
    ) -> Any:
        """Perform a request and return the decoded JSON body.

        Raises :class:`HttpError` when the request cannot be completed; callers
        are expected to catch it and degrade gracefully rather than crash the run.
        """
        method = method.upper()
        cache_key = self._cache_key(method, url, params, json_body or data)
        cacheable = method == "GET" or (cache_ttl or 0) > 0
        if cacheable:
            hit = self._cache_get(cache_key)
            if hit is not None:
                log.debug("http cache hit", extra={"url": url})
                return hit.payload

        all_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            all_headers.update(headers)

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            try:
                self.request_count += 1
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    data=data,
                    headers=all_headers,
                    timeout=(self.connect_timeout, self.timeout),
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise HttpError("network error for {}: {}".format(url, exc)) from exc
                self._backoff(attempt, None, url, str(exc))
                continue

            status = response.status_code
            if status in RETRYABLE_STATUS:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                last_error = HttpError("HTTP {}".format(status), status, response.text)
                if attempt >= self.max_retries:
                    if status == 429:
                        raise RateLimitError(
                            "rate limited by {} after {} attempts".format(url, attempt + 1),
                            status,
                            response.text,
                        )
                    raise HttpError(
                        "HTTP {} from {} after {} attempts".format(status, url, attempt + 1),
                        status,
                        response.text,
                    )
                self._backoff(attempt, retry_after, url, "HTTP {}".format(status))
                continue

            if status >= 400:
                raise HttpError(
                    "HTTP {} from {}".format(status, url), status, response.text
                )

            if not expect_json:
                payload: Any = response.text
            else:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise HttpError(
                        "non-JSON response from {}: {}".format(url, response.text[:200]),
                        status,
                        response.text,
                    ) from exc

            if cacheable:
                self._cache_put(cache_key, status, payload, cache_ttl)
            return payload

        raise HttpError("unreachable retry loop for {}: {}".format(url, last_error))

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.request_json("GET", url, **kwargs)

    def post_json(self, url: str, **kwargs: Any) -> Any:
        return self.request_json("POST", url, **kwargs)

    def _backoff(self, attempt: int, retry_after: Optional[float], url: str, reason: str) -> None:
        delay = min(self.backoff_max, self.backoff_base ** (attempt + 1))
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.backoff_max))
        delay += random.uniform(0, delay * 0.25)  # jitter avoids lock-step retries
        log.warning(
            "http retry",
            extra={"url": url, "attempt": attempt + 1, "delay_s": round(delay, 2), "reason": reason},
        )
        self._sleep(delay)


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:  # HTTP-date form
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(value)
        if target is None:
            return None
        import datetime as _dt

        now = _dt.datetime.now(_dt.timezone.utc)
        if target.tzinfo is None:
            target = target.replace(tzinfo=_dt.timezone.utc)
        return max(0.0, (target - now).total_seconds())
    except Exception:  # pragma: no cover - malformed header
        return None


def client_from_settings(settings: Any, *, rate_limit_rps: float = 0.0) -> HttpClient:
    http = settings.http
    return HttpClient(
        timeout=http.timeout_seconds,
        connect_timeout=http.connect_timeout_seconds,
        max_retries=http.max_retries,
        backoff_base=http.backoff_base_seconds,
        backoff_max=http.backoff_max_seconds,
        user_agent=http.user_agent,
        cache_ttl=http.cache_ttl_seconds,
        rate_limit_rps=rate_limit_rps,
    )

