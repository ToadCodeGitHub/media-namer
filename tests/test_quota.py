"""Gemini quota handling: per-minute limits wait and retry; daily/zero limits stop with a clear reason."""
import threading
import time

import pytest
from google.genai import errors

from media_namer import describe


def quota_error(quota_id, value="20", delay="37s", msg=None):
    body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                      "message": msg or f"You exceeded your current quota. limit: {value}",
                      "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                   "violations": [{"quotaId": quota_id, "quotaValue": value}]},
                                  {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}}
    return errors.ClientError(429, body)


def backend_raising(exc):
    be = describe.GeminiBackend("fake-key", "gemini-3.5-flash-lite")

    class Models:
        def generate_content(self, **kw):
            raise exc
    be._client = type("C", (), {"models": Models()})()
    return be


def test_per_minute_limit_is_transient_with_server_delay():
    be = backend_raising(quota_error("GenerateRequestsPerMinutePerProjectPerModel-FreeTier"))
    with pytest.raises(describe.TransientError) as ei:
        be.describe([b"x"], "p")
    assert ei.value.retry_after == 37 and "per minute" in str(ei.value)


def test_daily_limit_stops():
    be = backend_raising(quota_error("GenerateRequestsPerDayPerProjectPerModel-FreeTier", value="250"))
    with pytest.raises(describe.QuotaError, match="daily limit .* midnight Pacific"):
        be.describe([b"x"], "p")


def test_zero_quota_model_stops():
    be = backend_raising(quota_error("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", value="0"))
    with pytest.raises(describe.QuotaError, match="no free quota"):
        be.describe([b"x"], "p")


def test_retry_delay_from_message_when_no_details():
    body = {"error": {"code": 429, "message": "Quota exceeded. Please retry in 12.5s.", "status": "RESOURCE_EXHAUSTED"}}
    be = backend_raising(errors.ClientError(429, body))
    with pytest.raises(describe.TransientError) as ei:
        be.describe([b"x"], "p")
    assert ei.value.retry_after == 12.5


def test_with_retry_waits_at_least_server_delay():
    slept = []
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise describe.TransientError("rate", retry_after=30)
        return "ok"
    assert describe.with_retry(fn, sleep=slept.append) == "ok"
    assert 30 <= sum(slept) <= 32


def test_quota_error_is_not_retried():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise describe.QuotaError("daily")
    with pytest.raises(describe.QuotaError):
        describe.with_retry(fn, sleep=lambda s: None)
    assert calls["n"] == 1


def test_rate_limiter_spaces_requests_across_threads():
    lim = describe.RateLimiter(600)              # one every 0.1 s
    stamps = []
    lock = threading.Lock()

    def worker():
        for _ in range(3):
            lim.wait()
            with lock:
                stamps.append(time.monotonic())
    threads = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 6 and min(gaps) > 0.08


def test_rate_limiter_push_back_and_sharing():
    lim = describe.RateLimiter.shared("test:model", 6000)
    assert describe.RateLimiter.shared("test:model", 6000) is lim
    assert describe.RateLimiter.shared("test:model", 0) is None
    lim.push_back(0.3)
    t0 = time.monotonic()
    lim.wait()
    assert time.monotonic() - t0 >= 0.28
    with pytest.raises(describe.Cancelled):
        lim.push_back(5)
        lim.wait(cancelled=lambda: True)
