"""HTTP resilience: retries, backoff, 429 handling, caching, rate limiting."""

from __future__ import annotations

import time

import pytest
import requests

from src.http import HttpClient, HttpError, RateLimiter, RateLimitError, _parse_retry_after


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else "{}"
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0) if self.responses else FakeResponse(200, {})
        if isinstance(item, Exception):
            raise item
        return item


def client(responses, **kwargs):
    kwargs.setdefault("max_retries", 2)
    kwargs.setdefault("cache_ttl", 0)
    return HttpClient(session=FakeSession(responses), sleep=lambda _s: None, **kwargs)


def test_successful_request_returns_the_payload():
    http = client([FakeResponse(200, {"ok": True})])
    assert http.get_json("https://x.example") == {"ok": True}


def test_transient_500_is_retried_then_succeeds():
    http = client([FakeResponse(500), FakeResponse(200, {"ok": 1})])
    assert http.get_json("https://x.example") == {"ok": 1}
    assert len(http.session.calls) == 2


def test_retries_are_bounded():
    http = client([FakeResponse(503)] * 10, max_retries=2)
    with pytest.raises(HttpError) as exc:
        http.get_json("https://x.example")
    assert exc.value.status == 503
    assert len(http.session.calls) == 3  # 1 attempt + 2 retries


def test_429_raises_a_dedicated_error_after_retries():
    http = client([FakeResponse(429, headers={"Retry-After": "1"})] * 5, max_retries=1)
    with pytest.raises(RateLimitError):
        http.get_json("https://x.example")


def test_retry_after_header_extends_the_backoff():
    delays = []
    http = HttpClient(
        session=FakeSession([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, {})]),
        sleep=delays.append,
        max_retries=2,
        cache_ttl=0,
        backoff_base=1.1,
    )
    http.get_json("https://x.example")
    assert delays and delays[0] >= 7.0


def test_client_errors_are_not_retried():
    http = client([FakeResponse(404, text="missing")] * 3)
    with pytest.raises(HttpError) as exc:
        http.get_json("https://x.example")
    assert exc.value.status == 404
    assert len(http.session.calls) == 1


def test_network_errors_are_retried_then_surface_as_httperror():
    http = client([requests.ConnectionError("boom")] * 5, max_retries=1)
    with pytest.raises(HttpError):
        http.get_json("https://x.example")
    assert len(http.session.calls) == 2


def test_non_json_body_raises_httperror():
    http = client([FakeResponse(200, None, text="<html>nope</html>")])
    with pytest.raises(HttpError):
        http.get_json("https://x.example")


def test_get_requests_are_cached_within_the_ttl():
    http = client([FakeResponse(200, {"n": 1}), FakeResponse(200, {"n": 2})], cache_ttl=60)
    assert http.get_json("https://x.example") == {"n": 1}
    assert http.get_json("https://x.example") == {"n": 1}
    assert len(http.session.calls) == 1
    http.clear_cache()
    assert http.get_json("https://x.example") == {"n": 2}


def test_different_params_are_cached_separately():
    http = client([FakeResponse(200, {"n": 1}), FakeResponse(200, {"n": 2})], cache_ttl=60)
    assert http.get_json("https://x.example", params={"a": 1}) == {"n": 1}
    assert http.get_json("https://x.example", params={"a": 2}) == {"n": 2}


def test_post_is_not_cached_by_default():
    http = client([FakeResponse(200, {"n": 1}), FakeResponse(200, {"n": 2})], cache_ttl=60)
    assert http.post_json("https://x.example") == {"n": 1}
    assert http.post_json("https://x.example") == {"n": 2}


def test_user_agent_is_always_sent():
    http = client([FakeResponse(200, {})], user_agent="valve-food-monitor/test")
    http.get_json("https://x.example")
    assert http.session.calls[0][2]["headers"]["User-Agent"] == "valve-food-monitor/test"


def test_rate_limiter_paces_requests():
    limiter = RateLimiter(20.0, burst=1)
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    assert time.monotonic() - start >= 0.15


def test_rate_limiter_disabled_when_rps_is_zero():
    limiter = RateLimiter(0)
    start = time.monotonic()
    for _ in range(100):
        limiter.acquire()
    assert time.monotonic() - start < 0.2


@pytest.mark.parametrize(
    "header,expected",
    [("5", 5.0), ("0", 0.0), (None, None), ("", None), ("not-a-date", None)],
)
def test_retry_after_parsing(header, expected):
    assert _parse_retry_after(header) == expected
