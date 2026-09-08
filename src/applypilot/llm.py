"""
Unified LLM client for ApplyPilot.

Auto-detects provider from environment:
  GEMINI_API_KEY  -> Google Gemini (default: gemini-3.1-flash-lite)
  OPENAI_API_KEY  -> OpenAI (default: gpt-4o-mini)
  LLM_URL         -> Local llama.cpp / Ollama compatible endpoint

LLM_MODEL env var overrides the model name for any provider.

If OPENROUTER_API_KEY is also set, a Gemini client falls back to a free
OpenRouter model once Gemini's retries are truly exhausted (see
_OPENROUTER_FALLBACK_MODEL below) -- this only fires after 429/503 survives
every retry, which in practice means the daily quota is gone, not a
transient per-minute limit (those already resolve via the existing
backoff). The fallback model is hardcoded, not configurable, specifically
so this can never silently switch to a paid model.
"""

import logging
import os
import threading
import time

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_provider() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    local_url = os.environ.get("LLM_URL", "")
    model_override = os.environ.get("LLM_MODEL", "")

    if gemini_key and not local_url:
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            model_override or "gemini-3.1-flash-lite",
            gemini_key,
        )

    if openai_key and not local_url:
        return (
            "https://api.openai.com/v1",
            model_override or "gpt-4o-mini",
            openai_key,
        )

    if local_url:
        return (
            local_url.rstrip("/"),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )

    raise RuntimeError(
        "No LLM provider configured. "
        "Set GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL in your environment."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds

# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Gemini free tier is 15 RPM = 4s minimum between requests; 10s gives headroom.
_RATE_LIMIT_BASE_WAIT = 10

# Proactive pacing floor between consecutive Gemini calls (see LLMClient._pace).
# 15 RPM = 4.0s minimum; a little headroom for clock/network jitter.
_GEMINI_MIN_CALL_INTERVAL = 4.3


_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
# Hardcoded, not read from any env var -- this is the one thing standing
# between "fall back automatically" and "silently start spending money".
# "openrouter/free" is OpenRouter's own auto-router scoped to free models
# only (unlike "openrouter/auto", which can pick paid ones) -- it picks
# whichever free model is best available/least saturated at request time,
# so this rides out any single free model's daily cap instead of pinning to
# one (verified with a live 200 response, cost=0, this session). If this
# ever needs to change, change the literal here, not a config value someone
# could accidentally point at a paid model.
_OPENROUTER_FALLBACK_MODEL = "openrouter/free"


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    For Gemini keys, starts on the OpenAI-compat layer. On a 403 (which
    happens with preview/experimental models not exposed via compat), it
    automatically switches to the native generateContent API and stays there
    for the lifetime of the process.
    """

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=_TIMEOUT)
        # True once we've confirmed the native Gemini API works for this model
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)
        # GLM burns hidden chain-of-thought tokens before its visible answer
        # (a worst-case call measured 4027 reasoning tokens alone -- see
        # scoring/scorer.py:score_job) even for short structured-extraction
        # prompts that don't need it. Model-scoped, not call-site-scoped: this
        # only ever matches the GLM model configured for scoring/enrichment,
        # never Gemini/OpenAI, and never reaches the apply backends at all --
        # Claude Code and Goose run as separate CLI subprocesses that don't
        # go through this client.
        self._suppress_reasoning: bool = "glm" in model.lower()
        # Proactive pacing (Gemini free tier = 15 RPM = one call per 4s).
        # Shared across threads since discovery/apply can run with --workers > 1
        # and all of them share this one client instance.
        self._pace_lock = threading.Lock()
        self._last_call_at: float = 0.0
        # True once Gemini's retries have been exhausted and we've switched
        # to the free OpenRouter fallback for the rest of this process.
        self._openrouter_fallback_active: bool = False

    def _switch_to_openrouter_fallback(self) -> bool:
        """Switch this client to the free OpenRouter fallback, once.

        Returns False (does nothing) if no OPENROUTER_API_KEY is configured
        or we're not on Gemini in the first place -- callers should re-raise
        the original error in that case, exactly like before this existed.
        """
        if self._openrouter_fallback_active or not self._is_gemini:
            return False
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            return False

        log.warning(
            "Gemini retries exhausted -- likely the daily free-tier quota, "
            "not a transient rate limit. Falling back to OpenRouter's free "
            "'%s' for the rest of this run.",
            _OPENROUTER_FALLBACK_MODEL,
        )
        self.base_url = _OPENROUTER_BASE
        self.model = _OPENROUTER_FALLBACK_MODEL
        self.api_key = key
        self._is_gemini = False
        self._use_native_gemini = False
        self._openrouter_fallback_active = True
        return True

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the native Gemini generateContent API.

        Used automatically when the OpenAI-compat endpoint returns 403,
        which happens for preview/experimental models not exposed via compat.

        Converts OpenAI-style messages to Gemini's contents/systemInstruction
        format transparently.
        """
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                # Gemini uses "model" instead of "assistant"
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # Gemini 3.x models think by default, which can silently eat the
        # entire max_tokens budget on invisible reasoning and leave no room
        # for the actual answer (empty/missing `content`). "minimal" keeps
        # responses fast and non-empty for the short, structured outputs
        # this codebase asks for (JSON, SCORE:/KEYWORDS: lines, etc).
        if self._is_gemini:
            payload["reasoning_effort"] = "minimal"
        elif self._suppress_reasoning:
            payload["reasoning"] = {"effort": "none"}

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        # 403 on Gemini compat = model not available on compat layer.
        # Raise a specific sentinel so chat() can switch to native API.
        if resp.status_code == 403 and self._is_gemini:
            raise _GeminiCompatForbidden(resp)

        return self._handle_compat_response(resp)

    @staticmethod
    def _handle_compat_response(resp: httpx.Response) -> str:
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"].get("content")
        if not content:
            finish_reason = data["choices"][0].get("finish_reason", "?")
            raise RuntimeError(
                f"LLM returned no content (finish_reason={finish_reason}). "
                f"This usually means max_tokens was too low for the model's reasoning "
                f"overhead -- raise it or check the reasoning_effort setting."
            )
        return content

    # -- public API ---------------------------------------------------------

    def _pace(self) -> None:
        """Proactively space out calls to stay under Gemini's free-tier 15 RPM.

        Reactive backoff-after-429 (below) still exists as a safety net, but
        pacing up front avoids the wasted 10s/20s/40s/60s retry storms that
        happen when several calls fire back-to-back (e.g. judging a dozen
        intercepted API responses one after another).
        """
        if not self._is_gemini:
            return
        with self._pace_lock:
            elapsed = time.monotonic() - self._last_call_at
            wait = _GEMINI_MIN_CALL_INTERVAL - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_call_at = time.monotonic()

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the assistant message text."""
        # Qwen3 optimization: prepend /no_think to skip chain-of-thought
        # reasoning, saving tokens on structured extraction tasks.
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + messages[1:]

        self._pace()

        for attempt in range(_MAX_RETRIES):
            try:
                # Route to native Gemini if we've already confirmed it's needed
                if self._use_native_gemini:
                    return self._chat_native_gemini(messages, temperature, max_tokens)

                return self._chat_compat(messages, temperature, max_tokens)

            except _GeminiCompatForbidden:
                # Model not available on OpenAI-compat layer — switch to native.
                log.warning(
                    "Gemini compat endpoint returned 403 for model '%s'. "
                    "Switching to native generateContent API. "
                    "(Preview/experimental models are often compat-only on native.)",
                    self.model,
                )
                self._use_native_gemini = True
                # Retry immediately with native — don't count as a rate-limit wait
                try:
                    return self._chat_native_gemini(messages, temperature, max_tokens)
                except httpx.HTTPStatusError as native_exc:
                    raise RuntimeError(
                        f"Both Gemini endpoints failed. Compat: 403 Forbidden. "
                        f"Native: {native_exc.response.status_code} — "
                        f"{native_exc.response.text[:200]}"
                    ) from native_exc

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    # Respect Retry-After header if provided (Gemini sends this).
                    retry_after = (
                        resp.headers.get("Retry-After")
                        or resp.headers.get("X-RateLimit-Reset-Requests")
                    )
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d. "
                        "Tip: Gemini free tier = 15 RPM. Consider a paid account "
                        "or switching to a local model.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue

                # Retries exhausted. In practice this means the DAILY quota
                # is gone, not the per-minute limit (that already resolved
                # via the backoff above) -- switch providers and retry fresh
                # instead of hard-stopping the rest of the run.
                if resp.status_code in (429, 503) and self._switch_to_openrouter_fallback():
                    return self.chat(messages, temperature, max_tokens)
                raise

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # TransportError covers ConnectError/"No route to host", DNS
                # blips, network resets -- anything below the HTTP layer.
                # These used to propagate straight past this retry loop as a
                # permanent failure after one bad network moment, which is
                # how a single Wi-Fi hiccup turned into hundreds of jobs
                # getting a score=0 error sentinel written to the DB.
                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning(
                        "LLM request failed (%s: %s), retrying in %ds (attempt %d/%d)",
                        type(exc).__name__, exc, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None


def get_client() -> LLMClient:
    """Return (or create) the module-level LLMClient singleton."""
    global _instance
    if _instance is None:
        base_url, model, api_key = _detect_provider()
        log.info("LLM provider: %s  model: %s", base_url, model)
        _instance = LLMClient(base_url, model, api_key)
    return _instance
