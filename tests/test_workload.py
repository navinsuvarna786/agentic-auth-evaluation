"""Unit tests for benchmark.workload.

These run without any network access or live LLM gateway: the budget guard
uses an injectable clock, and the LLM client is exercised via httpx's
MockTransport. Run with: `pytest tests/test_workload.py -v`.
"""
import json
from types import SimpleNamespace

import httpx
import pytest

from benchmark.workload import (
    LLMClient,
    LLMResult,
    TokenBudgetExceededError,
    TokenBudgetGuard,
    WorkloadGenerator,
    WorkloadResult,
)


# ---------------------------------------------------------------------------
# Fake clock helper
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# TokenBudgetGuard
# ---------------------------------------------------------------------------


def test_guard_allows_within_per_minute():
    clock = FakeClock()
    g = TokenBudgetGuard(per_minute=100, per_hour=1000, per_day=10_000, clock=clock)
    g.check(50)
    g.record(50)
    g.check(40)
    g.record(40)
    snap = g.snapshot()
    assert snap["per_minute"]["used"] == 90


def test_guard_blocks_per_minute_breach():
    clock = FakeClock()
    g = TokenBudgetGuard(per_minute=100, per_hour=1000, per_day=10_000, clock=clock)
    g.record(80)
    # 80 used + 30 planned = 110 > 100 → must raise
    with pytest.raises(TokenBudgetExceededError, match="per-minute"):
        g.check(30)


def test_guard_blocks_per_hour_breach_even_when_minute_ok():
    clock = FakeClock()
    g = TokenBudgetGuard(per_minute=1000, per_hour=500, per_day=10_000, clock=clock)
    g.record(400)
    with pytest.raises(TokenBudgetExceededError, match="per-hour"):
        g.check(200)  # 400+200=600 > 500


def test_guard_blocks_per_day_breach():
    clock = FakeClock()
    g = TokenBudgetGuard(per_minute=10_000, per_hour=10_000, per_day=100, clock=clock)
    g.record(80)
    with pytest.raises(TokenBudgetExceededError, match="per-day"):
        g.check(30)


def test_guard_purges_old_events_minute_window():
    clock = FakeClock()
    g = TokenBudgetGuard(per_minute=100, per_hour=1000, per_day=10_000, clock=clock)
    g.record(80)
    # 61 seconds later: minute window has elapsed, so 80 should be purged
    clock.advance(61)
    g.check(80)  # must NOT raise
    snap = g.snapshot()
    assert snap["per_minute"]["used"] == 0
    # Hour window still holds the 80
    assert snap["per_hour"]["used"] == 80


def test_guard_rejects_invalid_construction():
    with pytest.raises(ValueError):
        TokenBudgetGuard(per_minute=0, per_hour=1, per_day=1)


def test_guard_zero_planned_is_noop():
    clock = FakeClock()
    g = TokenBudgetGuard(per_minute=10, per_hour=10, per_day=10, clock=clock)
    g.record(10)  # fully used
    g.check(0)  # zero planned must not raise
    g.record(0)  # zero record must not change usage
    assert g.snapshot()["per_minute"]["used"] == 10


# ---------------------------------------------------------------------------
# LLMClient (with MockTransport — no network)
# ---------------------------------------------------------------------------


def _make_llm_client(handler, guard_caps=(10_000, 100_000, 1_000_000)):
    guard = TokenBudgetGuard(*guard_caps)
    # Patch the http call by injecting a MockTransport into httpx.Client.
    # Since LLMClient constructs its own httpx.Client, monkey-patch via
    # a subclass that overrides chat_once to use the mock transport.
    transport = httpx.MockTransport(handler)

    class _PatchedLLMClient(LLMClient):
        def chat_once(self, prompt: str):
            planned = self._planned_tokens(prompt)
            self._guard.check(planned)
            import time as _time
            start = _time.perf_counter()
            try:
                with httpx.Client(transport=transport, timeout=self._timeout) as c:
                    r = c.post(
                        f"{self._gateway}/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self._model,
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": self._max_completion,
                        },
                    )
            except httpx.HTTPError as exc:
                elapsed = (_time.perf_counter() - start) * 1000
                return LLMResult(text="", prompt_tokens=0, completion_tokens=0,
                                 total_tokens=0, latency_ms=elapsed,
                                 status_code=0, error=str(exc))
            elapsed = (_time.perf_counter() - start) * 1000
            if r.status_code != 200:
                return LLMResult(text="", prompt_tokens=0, completion_tokens=0,
                                 total_tokens=0, latency_ms=elapsed,
                                 status_code=r.status_code, error=r.text[:200])
            body = r.json()
            usage = body.get("usage", {}) or {}
            choices = body.get("choices") or [{}]
            msg = choices[0].get("message") or {}
            total = int(usage.get("total_tokens", planned))
            self._guard.record(total)
            return LLMResult(
                text=msg.get("content", "") or "",
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                total_tokens=total,
                latency_ms=elapsed,
                status_code=200,
            )

    return _PatchedLLMClient(
        gateway_url="https://fake-gateway.test",
        api_key="fake-key",
        model="gpt-4.1-mini",
        guard=guard,
        max_completion_tokens=30,
    ), guard


def test_llm_client_success_records_usage():
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "Hello, world."}}],
            "usage": {"prompt_tokens": 55, "completion_tokens": 12, "total_tokens": 67},
        })
    c, guard = _make_llm_client(handler)
    res = c.chat_once("Test prompt please respond briefly.")
    assert res.success
    assert res.text == "Hello, world."
    assert res.prompt_tokens == 55
    assert res.completion_tokens == 12
    assert res.total_tokens == 67
    assert guard.snapshot()["per_minute"]["used"] == 67


def test_llm_client_non_200_returns_failed_result_and_does_not_record():
    def handler(req):
        return httpx.Response(429, text="rate limited")
    c, guard = _make_llm_client(handler)
    res = c.chat_once("Test prompt please respond briefly.")
    assert not res.success
    assert res.status_code == 429
    assert "rate limited" in res.error
    assert guard.snapshot()["per_minute"]["used"] == 0


def test_llm_client_transport_error_returns_failed_result():
    def handler(req):
        raise httpx.ConnectError("dns failure")
    c, guard = _make_llm_client(handler)
    res = c.chat_once("Test prompt please respond briefly.")
    assert not res.success
    assert res.status_code == 0
    assert "dns failure" in res.error
    assert guard.snapshot()["per_minute"]["used"] == 0


def test_llm_client_pre_call_budget_check_fires():
    # Tight cap: 50 per-minute. A 7-word prompt + 30 completion = ~49 planned.
    # A second call would push us over.
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
        })
    c, guard = _make_llm_client(handler, guard_caps=(50, 50_000, 500_000))
    res = c.chat_once("Short prompt one two three four five.")
    assert res.success
    # Second call must trip the guard
    with pytest.raises(TokenBudgetExceededError):
        c.chat_once("Short prompt one two three four five.")


# ---------------------------------------------------------------------------
# WorkloadGenerator
# ---------------------------------------------------------------------------


def _fake_request_result(success=True, latency_ms=5.0, status_code=200):
    """SimpleNamespace shaped like agent.agent_client.RequestResult so the
    full WorkloadResult attribute-forwarding surface can be exercised."""
    return SimpleNamespace(
        success=success,
        status_code=status_code if success else 401,
        latency_ms=latency_ms,
        exchange_ms=1.2,
        verification_ms=0.4,
        error=None if success else "auth_failed",
        response_data={"auth_metadata": {"actor": None}},
        dpop_verified=False,
        audience_ok=success,
    )


class FakeAgentClient:
    def __init__(self, success=True, latency_ms=5.0):
        self._success = success
        self._latency_ms = latency_ms

    def make_request(self):
        return _fake_request_result(
            success=self._success, latency_ms=self._latency_ms,
        )


def test_workload_synthetic_returns_forwarding_result():
    w = WorkloadGenerator(mode="synthetic", agent_client=FakeAgentClient())
    out = w.do_one()
    assert isinstance(out, WorkloadResult)
    assert out.workload_mode == "synthetic"
    assert out.llm is None
    # Forwarded attributes match the underlying agent result
    assert out.success is True
    assert out.status_code == 200
    assert out.latency_ms == 5.0
    assert out.exchange_ms == 1.2
    assert out.verification_ms == 0.4
    assert out.error is None
    assert out.dpop_verified is False
    assert out.audience_ok is True


def test_workload_llm_direct_calls_llm_after_success():
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
        })
    llm, _ = _make_llm_client(handler)
    w = WorkloadGenerator(mode="llm-direct", agent_client=FakeAgentClient(), llm=llm)
    out = w.do_one()
    assert isinstance(out, WorkloadResult)
    assert out.workload_mode == "llm-direct"
    assert out.success
    assert out.llm is not None
    assert out.llm.success
    assert out.llm.total_tokens == 55


def test_workload_llm_direct_skips_llm_when_agent_fails():
    def handler(req):  # would explode if reached
        raise AssertionError("LLM must not be called when agent failed")
    llm, _ = _make_llm_client(handler)
    w = WorkloadGenerator(
        mode="llm-direct",
        agent_client=FakeAgentClient(success=False),
        llm=llm,
    )
    out = w.do_one()
    assert not out.success
    assert out.llm is None
    assert out.workload_mode == "llm-direct"


def test_workload_llm_chain_raises_not_implemented():
    def handler(req):
        return httpx.Response(200, json={})
    llm, _ = _make_llm_client(handler)
    w = WorkloadGenerator(mode="llm-chain", agent_client=FakeAgentClient(), llm=llm)
    with pytest.raises(NotImplementedError, match="B5b"):
        w.do_one()


def test_workload_unknown_mode_rejected():
    with pytest.raises(ValueError, match="unknown workload mode"):
        WorkloadGenerator(mode="nonsense", agent_client=FakeAgentClient())


def test_workload_llm_mode_requires_client():
    with pytest.raises(ValueError, match="requires an LLMClient"):
        WorkloadGenerator(mode="llm-direct", agent_client=FakeAgentClient())


def test_workload_result_failed_agent_forwards_fields():
    """A failed agent still produces a fully-formed WorkloadResult; the
    benchmark CSV writer relies on every attribute being readable even on
    failure rows."""
    w = WorkloadGenerator(
        mode="synthetic", agent_client=FakeAgentClient(success=False),
    )
    out = w.do_one()
    assert out.success is False
    assert out.status_code == 401
    assert out.error == "auth_failed"
    assert out.audience_ok is False
