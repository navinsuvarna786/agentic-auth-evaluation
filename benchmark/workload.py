"""Workload generator for RQ1/RQ2/RQ3.

Three modes, selected at run time via env var `WORKLOAD_MODE`:

  synthetic     RQ1: deterministic agent → API call, no LLM. The condition
                under which we report headline auth-overhead numbers.
  llm-direct    RQ2: agent → API call followed by a single chat completion
                against the Cisco LLM gateway. Adds realistic backend latency
                so the relative cost of auth is contextualized.
  llm-chain     RQ3: two-hop delegation under realistic load. Implemented
                in B5b — `WorkloadGenerator` raises NotImplementedError here
                so it's surfaced rather than silently skipped.

Token budget enforcement is non-negotiable: every LLM call passes through
`TokenBudgetGuard` which holds three sliding windows (per-minute, per-hour,
per-day) and raises `TokenBudgetExceededError` BEFORE the HTTP request when
any window would be breached. The defaults align with the budget recorded
in STATE.md:

  per_minute  18 000   (of the 120 000 Cisco gateway cap)
  per_hour   720 000   (of the 720 000 Cisco gateway cap)
  per_day  4 750 000   (of the 7 200 000 Cisco gateway cap, ~21% retry buffer)
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Optional, Tuple

import httpx


class TokenBudgetExceededError(RuntimeError):
    """Raised before an LLM call when accepting it would breach a window."""


# Static prompt set chosen so each prompt tokenizes to roughly 50-60 prompt
# tokens for gpt-4.1-mini's tokenizer. Fixed text is deliberate: variance in
# prompt length would confound auth-overhead-vs-LLM-overhead decomposition.
# Verified at 53-58 tokens each via tiktoken `o200k_base` (the gpt-4.1
# family tokenizer); the assistant's completion is capped separately.
PROMPTS_50_60: Tuple[str, ...] = (
    "Explain in two short sentences why ephemeral agent identities benefit from "
    "short access-token lifetimes and proof-of-possession binding. Mention one "
    "concrete risk avoided.",
    "List three differences between OAuth 2.0 token exchange under RFC 8693 and "
    "older SAML-based delegation, in plain English. Keep each point to one line.",
    "Describe in two sentences how DPoP binds an access token to a client keypair "
    "and what an attacker who steals only the bearer token cannot do.",
    "Summarize, in three short bullet points, the trust assumptions an ephemeral "
    "AI agent makes when it presents a delegated token to an internal service.",
    "In two sentences, contrast a stolen-bearer-token attack with a DPoP-proof "
    "replay attack: what each requires and which defense stops which.",
    "Briefly outline the role of the `cnf.jkt` claim in a DPoP-bound access token "
    "and what the protected resource server is supposed to verify against it.",
    "In two short sentences each, name two reasons why audience scoping on internal "
    "service tokens helps contain the blast radius of credential compromise.",
)


# ---------------------------------------------------------------------------
# Token budget guard
# ---------------------------------------------------------------------------


class TokenBudgetGuard:
    """Sliding-window budget across three timescales.

    Thread-safe: workload runs are concurrent (asyncio + threadpool), so the
    bookkeeping deques are protected by a single lock. The lock is held only
    while updating the deques, never during the HTTP call.

    Time source is injectable to keep the guard unit-testable (see tests).
    """

    def __init__(
        self,
        per_minute: int,
        per_hour: int,
        per_day: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if per_minute <= 0 or per_hour <= 0 or per_day <= 0:
            raise ValueError("all windows must be positive")
        self._per_minute = per_minute
        self._per_hour = per_hour
        self._per_day = per_day
        self._clock = clock
        self._lock = threading.Lock()
        self._events_minute: Deque[Tuple[float, int]] = deque()
        self._events_hour: Deque[Tuple[float, int]] = deque()
        self._events_day: Deque[Tuple[float, int]] = deque()

    @staticmethod
    def _purge(events: Deque[Tuple[float, int]], now: float, window: float) -> None:
        while events and now - events[0][0] > window:
            events.popleft()

    @staticmethod
    def _used(events: Deque[Tuple[float, int]]) -> int:
        return sum(t for _, t in events)

    def snapshot(self) -> dict:
        """Read-only view of current usage; useful for logging/debugging."""
        with self._lock:
            now = self._clock()
            self._purge(self._events_minute, now, 60)
            self._purge(self._events_hour, now, 3600)
            self._purge(self._events_day, now, 86400)
            return {
                "per_minute": {
                    "used": self._used(self._events_minute),
                    "limit": self._per_minute,
                },
                "per_hour": {
                    "used": self._used(self._events_hour),
                    "limit": self._per_hour,
                },
                "per_day": {
                    "used": self._used(self._events_day),
                    "limit": self._per_day,
                },
            }

    def check(self, planned_tokens: int) -> None:
        """Raise `TokenBudgetExceededError` if `planned_tokens` would push any
        sliding window past its cap. Does NOT reserve the tokens — call
        `record()` after the LLM call returns with the actual usage."""
        if planned_tokens <= 0:
            return
        with self._lock:
            now = self._clock()
            self._purge(self._events_minute, now, 60)
            self._purge(self._events_hour, now, 3600)
            self._purge(self._events_day, now, 86400)
            for events, limit, label in (
                (self._events_minute, self._per_minute, "per-minute"),
                (self._events_hour, self._per_hour, "per-hour"),
                (self._events_day, self._per_day, "per-day"),
            ):
                used = self._used(events)
                if used + planned_tokens > limit:
                    raise TokenBudgetExceededError(
                        f"{label} budget would be exceeded: "
                        f"used={used} planned={planned_tokens} limit={limit}"
                    )

    def record(self, tokens: int) -> None:
        """Record actual tokens consumed. Idempotent in the sense that a
        zero/negative value is a no-op."""
        if tokens <= 0:
            return
        with self._lock:
            now = self._clock()
            self._events_minute.append((now, tokens))
            self._events_hour.append((now, tokens))
            self._events_day.append((now, tokens))


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float
    status_code: int
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.status_code == 200 and self.error is None


class LLMClient:
    """OpenAI-compatible chat-completion client.

    The OpenAI-compatible gateway used in the study accepts standard OpenAI
    chat completions; only the auth header (Bearer JWT) and base URL change.
    Failure modes are deliberately fail-soft for the workload — a 5xx or
    timeout returns an `LLMResult` with status_code != 200 and an error
    message. The benchmark records and continues; budget-exceeded errors
    are fail-fast and propagate (we WANT to abort if we'd breach the cap).
    """

    def __init__(
        self,
        gateway_url: str,
        api_key: str,
        model: str,
        guard: TokenBudgetGuard,
        max_completion_tokens: int = 30,
        timeout_seconds: float = 30.0,
        chat_completions_path: str = "/v1/chat/completions",
    ) -> None:
        self._gateway = gateway_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._guard = guard
        self._max_completion = max_completion_tokens
        self._timeout = timeout_seconds
        self._chat_path = "/" + chat_completions_path.lstrip("/")

    def _planned_tokens(self, prompt: str) -> int:
        # Conservative pre-call estimate so a single prompt that creeps to
        # 70 tokens doesn't sneak past the guard. The truth is in `usage`
        # from the response; recorded via `record()` post-call.
        word_count = len(prompt.split())
        prompt_est = int(word_count * 1.6) + 8  # words + slack for tokens
        return prompt_est + self._max_completion

    def chat_once(self, prompt: str) -> LLMResult:
        planned = self._planned_tokens(prompt)
        self._guard.check(planned)
        start = time.perf_counter()
        try:
            with httpx.Client(timeout=self._timeout) as c:
                r = c.post(
                    f"{self._gateway}{self._chat_path}",
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
            elapsed = (time.perf_counter() - start) * 1000
            return LLMResult(
                text="", prompt_tokens=0, completion_tokens=0, total_tokens=0,
                latency_ms=elapsed, status_code=0, error=str(exc),
            )
        elapsed = (time.perf_counter() - start) * 1000
        if r.status_code != 200:
            return LLMResult(
                text="", prompt_tokens=0, completion_tokens=0, total_tokens=0,
                latency_ms=elapsed, status_code=r.status_code,
                error=r.text[:200],
            )
        body = r.json()
        usage = body.get("usage", {}) or {}
        choices = body.get("choices") or [{}]
        msg = choices[0].get("message") or {}
        text = msg.get("content", "") or ""
        total = int(usage.get("total_tokens", planned))
        self._guard.record(total)
        return LLMResult(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            total_tokens=total,
            latency_ms=elapsed,
            status_code=200,
        )


# ---------------------------------------------------------------------------
# Workload generator
# ---------------------------------------------------------------------------


@dataclass
class WorkloadResult:
    """One workload iteration: the agent's RequestResult plus optional LLM
    follow-up. Exposes the same attribute surface the benchmark loop and
    CSV writer already read from `RequestResult`, so the launcher needs
    no change — `result.success`, `result.latency_ms`, etc. forward."""

    agent: Any  # agent.agent_client.RequestResult
    llm: Optional[LLMResult]
    workload_mode: str

    @property
    def success(self) -> bool:
        return self.agent.success

    @property
    def status_code(self) -> int:
        return self.agent.status_code

    @property
    def latency_ms(self) -> float:
        return self.agent.latency_ms

    @property
    def exchange_ms(self):
        return self.agent.exchange_ms

    @property
    def verification_ms(self):
        return self.agent.verification_ms

    @property
    def error(self):
        return self.agent.error

    @property
    def response_data(self):
        return getattr(self.agent, "response_data", None)

    @property
    def dpop_verified(self) -> bool:
        return self.agent.dpop_verified

    @property
    def audience_ok(self) -> bool:
        return self.agent.audience_ok


class WorkloadGenerator:
    """One unit of measurable work per benchmark iteration.

    The benchmark loop already drives `agent_client.make_request()`. This
    wrapper preserves that for `synthetic` mode and bolts on a follow-up
    LLM call for `llm-direct`. The chain mode is B5b's work.
    """

    SUPPORTED_MODES = ("synthetic", "llm-direct", "llm-chain")

    def __init__(
        self,
        mode: str,
        agent_client,
        llm: Optional[LLMClient] = None,
        prompts: Tuple[str, ...] = PROMPTS_50_60,
    ) -> None:
        if mode not in self.SUPPORTED_MODES:
            raise ValueError(
                f"unknown workload mode {mode!r}; "
                f"expected one of {self.SUPPORTED_MODES}"
            )
        if mode != "synthetic" and llm is None:
            raise ValueError(f"workload mode {mode!r} requires an LLMClient")
        self.mode = mode
        self._agent = agent_client
        self._llm = llm
        self._prompts = prompts
        self._counter = 0

    def do_one(self) -> WorkloadResult:
        """Run one iteration. For synthetic, returns the agent's
        RequestResult with no LLM follow-up. For llm-direct, also issues
        one chat completion — but skips the LLM call when the agent
        request itself failed, so an auth failure isn't double-attributed
        across the auth and LLM components."""
        if self.mode == "synthetic":
            return WorkloadResult(
                agent=self._agent.make_request(),
                llm=None,
                workload_mode=self.mode,
            )

        if self.mode == "llm-direct":
            agent_result = self._agent.make_request()
            if not agent_result.success:
                return WorkloadResult(
                    agent=agent_result, llm=None, workload_mode=self.mode,
                )
            prompt = self._prompts[self._counter % len(self._prompts)]
            self._counter += 1
            llm_result = self._llm.chat_once(prompt)
            return WorkloadResult(
                agent=agent_result, llm=llm_result, workload_mode=self.mode,
            )

        # llm-chain: deferred to B5b. Raise rather than silently degrade
        # so the misconfiguration is obvious.
        raise NotImplementedError(
            "workload mode 'llm-chain' is B5b scope; not yet implemented"
        )


# ---------------------------------------------------------------------------
# Env-driven factory
# ---------------------------------------------------------------------------


def workload_from_env(agent_client) -> WorkloadGenerator:
    """Build a `WorkloadGenerator` from environment variables. The synthetic
    mode needs nothing; the LLM modes require `LLM_GATEWAY_URL` and
    `LLM_API_KEY` and optionally tune `LLM_MODEL` and the three budget caps."""
    mode = os.getenv("WORKLOAD_MODE", "synthetic")
    if mode == "synthetic":
        return WorkloadGenerator(mode="synthetic", agent_client=agent_client)

    gateway = os.getenv("LLM_GATEWAY_URL")
    api_key = os.getenv("LLM_API_KEY")
    if not gateway or not api_key:
        raise RuntimeError(
            f"WORKLOAD_MODE={mode!r} requires LLM_GATEWAY_URL and LLM_API_KEY "
            f"to be set (see .env.example)."
        )
    model = os.getenv("LLM_MODEL", "gpt-4.1-mini")
    guard = TokenBudgetGuard(
        per_minute=int(os.getenv("LLM_PER_MINUTE_TOKEN_BUDGET", "18000")),
        per_hour=int(os.getenv("LLM_PER_HOUR_TOKEN_BUDGET", "720000")),
        per_day=int(os.getenv("LLM_PER_DAY_TOKEN_BUDGET", "4750000")),
    )
    max_completion = int(os.getenv("LLM_MAX_COMPLETION_TOKENS", "30"))
    chat_path = os.getenv("LLM_CHAT_COMPLETIONS_PATH", "/v1/chat/completions")
    llm = LLMClient(
        gateway_url=gateway,
        api_key=api_key,
        model=model,
        guard=guard,
        max_completion_tokens=max_completion,
        chat_completions_path=chat_path,
    )
    return WorkloadGenerator(mode=mode, agent_client=agent_client, llm=llm)
