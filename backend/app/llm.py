"""
One function to call any of Sandy's LLMs. litellm gives every provider
the same interface â€” no hand-written HTTP client per provider.

Cap enforcement lives here (checked before every call) so it's
impossible for a router/orchestrator to accidentally bypass it.
"""
import datetime
import logging
import os
import re
import time

from litellm import completion

import config
import observability

logging.basicConfig(
    filename="/tmp/sandy.log", level=logging.INFO, format="%(asctime)s %(message)s"
)


def log(msg: str) -> None:
    """Every diagnostic message goes through here instead of a bare
    print(): still prints (unchanged, visible in HF's live log viewer),
    AND writes to /tmp/sandy.log so Sandy can read her own recent logs
    when Ruk asks what went wrong."""
    print(msg)
    logging.info(msg)

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_+-]*\s*\n?(.*?)\n?```$", re.DOTALL)


def strip_fence(raw: str) -> str:
    """LLMs asked for 'raw content only, no fences' commonly wrap the
    response in ```language ... ``` fences anyway. Strip that before
    using the content for anything -- otherwise the literal fence
    markers end up written into whatever the content becomes (a file,
    a parsed JSON blob, etc)."""
    m = _FENCE_RE.match(raw.strip())
    return m.group(1) if m else raw


def strip_json_fence(raw: str) -> str:
    """Same as strip_fence() -- kept as a separate name at JSON call
    sites so it's clear what's being extracted there."""
    return strip_fence(raw)

MODELS = {
    "groq": "groq/llama-3.3-70b-versatile",
    "gemini": "gemini/gemini-3.5-flash",
    "cerebras": "cerebras/gpt-oss-120b",
    "deepseek": "deepseek/deepseek-chat",
    "mistral": "mistral/mistral-large-latest",
    "cohere": "cohere/command-r-plus",
    "moonshot": "moonshot/kimi-k2-0711-preview",
    "zhipu": "zai/glm-4.7",
    "cloudflare": "cloudflare/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "nvidia": "nvidia_nim/meta/llama-3.3-70b-instruct",
    "novita": "novita/deepseek/deepseek-r1",
    "deepinfra": "deepinfra/meta-llama/Llama-3.3-70B-Instruct",
    "siliconflow": "siliconflow/deepseek-ai/DeepSeek-V3",
    "openrouter": "openrouter/meta-llama/llama-3.3-70b-instruct:free",
    "github": "github/gpt-4.1-mini",
}

_API_KEY_ENV = {
    "zhipu": "ZHIPU_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "github": "GITHUB_TOKEN",
}
_NVIDIA_NIM_BASE = "https://integrate.api.nvidia.com/v1"

LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "90"))

PROVIDER_API_KEY_ENV = {
    "groq": "GROQ_API_KEY", "gemini": "GOOGLE_API_KEY", "cerebras": "CEREBRAS_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY", "mistral": "MISTRAL_API_KEY", "cohere": "COHERE_API_KEY",
    "moonshot": "MOONSHOT_API_KEY", "cloudflare": "CLOUDFLARE_API_KEY",
    "novita": "NOVITA_API_KEY", "deepinfra": "DEEPINFRA_API_KEY",
    "siliconflow": "SILICONFLOW_API_KEY", "openrouter": "OPENROUTER_API_KEY",
    **_API_KEY_ENV,
}

CONTEXT_LIMITS = {
    "cerebras": 8_192,
    "groq": 128_000,
    "gemini": 1_000_000,
    "deepseek": 64_000,
    "mistral": 128_000,
    "cohere": 128_000,
    "moonshot": 131_072,
    "zhipu": 128_000,
    "cloudflare": 24_000,
    "nvidia": 128_000,
    "novita": 64_000,
    "deepinfra": 64_000,
    "siliconflow": 64_000,
    "openrouter": 128_000,
    "github": 128_000,
}
_DEFAULT_CONTEXT_LIMIT = 128_000

PROFILES = {
    "groq":        {"latency": "ultra-fast", "strength": "generalist",
                    "roles": ["classify", "simple", "worker"]},
    "gemini":      {"latency": "fast", "strength": "long-context research",
                    "roles": ["research", "judge", "worker", "orchestrator"]},
    "cerebras":    {"latency": "ultra-fast", "strength": "reasoning",
                    "roles": ["worker"], "caveat": "8k context -- short subtasks only"},
    "deepseek":    {"latency": "medium", "strength": "deep reasoning + code",
                    "roles": ["worker", "judge"]},
    "mistral":     {"latency": "fast", "strength": "generalist EU, strong instruction following",
                    "roles": ["worker"]},
    "cohere":      {"latency": "fast", "strength": "RAG / grounded synthesis",
                    "roles": ["worker", "judge"]},
    "moonshot":    {"latency": "medium", "strength": "long-context agentic",
                    "roles": ["worker", "research"]},
    "zhipu":       {"latency": "fast", "strength": "generalist + tool use",
                    "roles": ["worker"]},
    "cloudflare":  {"latency": "fast", "strength": "edge fallback generalist",
                    "roles": ["worker"], "caveat": "24k context -- trim history hard"},
    "nvidia":      {"latency": "fast", "strength": "solid llama generalist",
                    "roles": ["worker"]},
    "novita":      {"latency": "slow", "strength": "deepseek-r1 chain-of-thought",
                    "roles": ["worker"], "caveat": "reasoning model -- slow, burns output tokens"},
    "deepinfra":   {"latency": "fast", "strength": "cheap reliable llama",
                    "roles": ["worker"]},
    "siliconflow": {"latency": "fast", "strength": "DeepSeek-V3 generalist",
                    "roles": ["worker"]},
    "openrouter":  {"latency": "variable", "strength": "free llama fallback route",
                    "roles": ["worker"], "caveat": ":free routes rate-limited unpredictably"},
    "github":      {"latency": "fast", "strength": "gpt-4.1-mini class generalist",
                    "roles": ["worker"]},
}

RATE_LIMITS = {
    "groq":        {"rpm": 30,    "tpm": None,   "daily": 14_400},
    "gemini":      {"rpm": 15,    "tpm": 250_000, "daily": None},
    "cerebras":    {"rpm": 30,    "tpm": 60_000,  "daily": None},
    "deepseek":    {"rpm": None,  "tpm": None,    "daily": None},
    "mistral":     {"rpm": 1,     "tpm": None,    "daily": None},
    "cohere":      {"rpm": 20,    "tpm": 40_000,  "daily": 1_000},
    "moonshot":    {"rpm": 3,     "tpm": 32_000,  "daily": None},
    "zhipu":       {"rpm": 5,     "tpm": None,    "daily": None},
    "cloudflare":  {"rpm": 300,   "tpm": None,    "daily": 10_000},
    "nvidia":      {"rpm": 40,    "tpm": None,    "daily": None},
    "novita":      {"rpm": None,  "tpm": None,    "daily": None},
    "deepinfra":   {"rpm": None,  "tpm": None,    "daily": None},
    "siliconflow": {"rpm": None,  "tpm": None,    "daily": None},
    "openrouter":  {"rpm": 20,    "tpm": None,    "daily": 50},
    "github":      {"rpm": 15,    "tpm": None,    "daily": 150},
}


def key_audit() -> dict[str, bool]:
    return {p: bool(os.environ.get(env)) for p, env in PROVIDER_API_KEY_ENV.items()}


def _provider_healthy(provider: str) -> bool:
    if not key_audit().get(provider):
        return False
    c = _CIRCUITS.get(provider)
    return bool(c is None or c.get("state") in ("closed", "half_open"))


def _cap_headroom(provider: str) -> int:
    big = 10**9
    try:
        caps = config.get_config("caps") or {}
        cap = caps.get(provider)
        if cap is None:
            return big
        usage = config.get_config("usage") or {}
        used = usage.get(provider, 0) if usage.get("date") == datetime.date.today().isoformat() else 0
        return max(0, cap - used)
    except Exception:
        return big


_EXHAUST_FRACTION = 0.8
_EXHAUST_WINDOW_S = 600


def _projected_exhaustion(provider: str) -> bool:
    try:
        rl = (RATE_LIMITS.get(provider) or {}).get("daily")
        if not rl:
            return False
        import config as _config
        usage = _config.get_config("usage") or {}
        used = usage.get(provider, 0) if usage.get("date") == datetime.date.today().isoformat() else 0
        if used < rl * _EXHAUST_FRACTION:
            return False
        import observability as _obs
        rate = _obs.burn_rate(provider)
        if rate <= 0:
            return False
        remaining_calls = rl - used
        projected_s = remaining_calls / rate
        return projected_s <= _EXHAUST_WINDOW_S
    except Exception:
        return False


def extended_pool(need: str | None = None) -> list[str]:
    matched, rest = [], []
    for p in MODELS:
        if not _provider_healthy(p) or _projected_exhaustion(p):
            continue
        roles = PROFILES.get(p, {}).get("roles", [])
        if need and need in roles:
            matched.append(p)
        else:
            rest.append(p)
    stable = lambda seq: [p for p in MODELS if p in set(seq)]
    matched.sort(key=_cap_headroom, reverse=True)
    rest.sort(key=_cap_headroom, reverse=True)
    return stable(matched) + stable(rest)
_RESPONSE_RESERVE = 1_500


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def fit_to_budget(messages: list[dict], provider: str) -> list[dict]:
    budget = CONTEXT_LIMITS.get(provider, _DEFAULT_CONTEXT_LIMIT) - _RESPONSE_RESERVE
    if len(messages) <= 2 or budget <= 0:
        return messages
    head, tail = messages[0], messages[-1]
    used = _estimate_tokens(head["content"]) + _estimate_tokens(tail["content"])
    kept = []
    for m in reversed(messages[1:-1]):
        t = _estimate_tokens(m["content"])
        if used + t > budget:
            break
        kept.append(m)
        used += t
    kept.reverse()
    return [head] + kept + [tail]


class CapExceeded(Exception):
    pass


def _today() -> str:
    return datetime.date.today().isoformat()


def _check_and_bump_cap(provider: str) -> None:
    try:
        caps = config.get_config("caps") or {}
        cap = caps.get(provider)
        if cap is None:
            return

        def _bump(usage):
            usage = usage or {}
            if usage.get("date") != _today():
                usage = {"date": _today()}
            used = usage.get(provider, 0)
            if used >= cap:
                raise CapExceeded(f"{provider} hit its cap of {cap} calls today")
            usage[provider] = used + 1
            return usage

        config.atomic_update("usage", _bump)
    except CapExceeded:
        raise
    except Exception as e:
        log(f"[_check_and_bump_cap] cap check itself failed for {provider} (infra issue, not a real cap) -- failing OPEN: {e!r}")
        return


_CIRCUITS: dict[str, dict] = {}


def _classify_failure(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, CapExceeded):
        return "quota_exhausted", False
    msg = str(exc).lower()
    if "401" in msg or "unauthorized" in msg or ("invalid" in msg and "key" in msg):
        return "authentication_error", False
    if "403" in msg or "permission" in msg or "forbidden" in msg:
        return "permission_error", False
    if "429" in msg or "rate limit" in msg or "rate_limit" in msg:
        return "rate_limit", True
    if "timeout" in msg or "timed out" in msg:
        return "timeout", True
    if any(code in msg for code in ("500", "502", "503", "504")):
        return "provider_5xx", True
    return "unknown", True


def _circuit_allows(provider: str) -> bool:
    c = _CIRCUITS.get(provider)
    if not c or c["state"] == "closed":
        return True
    if c["state"] == "open":
        if time.time() >= c["next_probe_at"]:
            c["state"] = "half_open"
            return True
        return False
    return True


def _circuit_observe(provider: str, success: bool, retryable: bool = True) -> None:
    c = _CIRCUITS.setdefault(provider, {"state": "closed", "failure_count": 0})
    if success:
        c["state"] = "closed"
        c["failure_count"] = 0
        c.pop("next_probe_at", None)
        return
    c["failure_count"] += 1
    threshold = 1 if not retryable else 3
    cooldown = 600 if not retryable else 60
    if c["state"] == "half_open" or c["failure_count"] >= threshold:
        c["state"] = "open"
        c["next_probe_at"] = time.time() + cooldown


def call_llm(provider: str, messages: list[dict], caller: str = "unknown", **kwargs) -> str:
    if provider not in MODELS:
        raise ValueError(f"unknown provider: {provider}")
    _check_and_bump_cap(provider)
    messages = fit_to_budget(messages, provider)
    env_name = _API_KEY_ENV.get(provider)
    if env_name:
        kwargs.setdefault("api_key", os.environ.get(env_name))
    if provider == "nvidia":
        kwargs.setdefault("api_base", _NVIDIA_NIM_BASE)
    kwargs.setdefault("timeout", LLM_TIMEOUT_S)
    response = completion(model=MODELS[provider], messages=messages, **kwargs)
    text = response.choices[0].message.content
    try:
        observability.record_call(provider, caller, messages, text)
    except Exception as e:
        log(f"observability.record_call failed (non-fatal): {e}")
    return text


def call_llm_with_fallback(provider: str, messages: list[dict], caller: str = "unknown", **kwargs) -> str:
    _core = ("groq", "gemini", "cerebras")
    order = [provider] + [p for p in _core if p != provider]
    last_err = None
    attempted = False
    for p in order:
        if not _circuit_allows(p):
            continue
        attempted = True
        try:
            result = call_llm(p, messages, caller=caller, **kwargs)
            _circuit_observe(p, success=True)
            return result
        except Exception as e:
            _, retryable = _classify_failure(e)
            _circuit_observe(p, success=False, retryable=retryable)
            last_err = e
    if not attempted:
        return call_llm(provider, messages, caller=caller, **kwargs)
    raise last_err


if __name__ == "__main__":
    _CIRCUITS.clear()
    assert _circuit_allows("gemini"), "a provider with no history must be allowed"

    for _ in range(3):
        _circuit_observe("gemini", success=False, retryable=True)
    assert not _circuit_allows("gemini"), "circuit should be OPEN after 3 retryable failures"
    print("llm.py: circuit opens after 3 retryable failures -> OK")

    _CIRCUITS.clear()
    _circuit_observe("gemini", success=False, retryable=False)
    assert not _circuit_allows("gemini"), "one non-retryable failure must open the circuit immediately"
    print("llm.py: circuit opens on a single non-retryable failure -> OK")

    _CIRCUITS["gemini"]["next_probe_at"] = time.time() - 1
    assert _circuit_allows("gemini"), "circuit should allow exactly one probe after cooldown"
    assert _CIRCUITS["gemini"]["state"] == "half_open"
    print("llm.py: circuit half-opens for one probe after cooldown -> OK")

    _circuit_observe("gemini", success=True)
    assert _circuit_allows("gemini") and _CIRCUITS["gemini"]["state"] == "closed"
    print("llm.py: circuit closes again after a successful probe -> OK")

    assert _classify_failure(CapExceeded("groq hit its cap")) == ("quota_exhausted", False)
    assert _classify_failure(Exception("401 Unauthorized: invalid api key"))[1] is False
    assert _classify_failure(Exception("Request timed out"))[1] is True
    print("llm.py: failure classification (retryable vs not) -> OK")

    reply = call_llm("groq", [{"role": "user", "content": "reply with exactly: pong"}])
    assert "pong" in reply.lower(), f"unexpected reply: {reply}"
    print("llm.py: groq call OK ->", reply)
