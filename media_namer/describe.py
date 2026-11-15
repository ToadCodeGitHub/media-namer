"""AI backends that turn images into a short description.

Two backends:
  * Ollama  - runs on your own computer; nothing leaves the house.
  * Gemini  - Google's API; images are sent to Google.
"""
from __future__ import annotations

import base64
import json
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

OLLAMA = "ollama"
GEMINI = "gemini"

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5vl:7b"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"


class DescribeError(Exception):
    """Won't succeed on retry (bad key, unknown model, blocked content)."""


class TransientError(DescribeError):
    """Might succeed on retry (rate limit, server busy, timeout)."""

    def __init__(self, msg: str, retry_after: float = 0.0):
        super().__init__(msg)
        self.retry_after = retry_after        # seconds the server asked us to wait


class QuotaError(DescribeError):
    """Daily quota used up, or no quota for this model on this key: retrying today won't help."""


def _seconds(text: str) -> float:
    m = re.search(r"([\d.]+)\s*s", text or "")
    return float(m.group(1)) if m else 0.0


def _quota_details(e) -> tuple[list[str], list[str], float]:
    """(quota ids, quota values, retry delay) from a Google API error body."""
    body = getattr(e, "details", None)
    err = body.get("error", {}) if isinstance(body, dict) else {}
    ids, values, delay = [], [], 0.0
    for d in err.get("details", []) or []:
        kind = str(d.get("@type", ""))
        if kind.endswith("QuotaFailure"):
            for v in d.get("violations", []) or []:
                ids.append(str(v.get("quotaId", "")))
                values.append(str(v.get("quotaValue", "")))
        elif kind.endswith("RetryInfo"):
            delay = _seconds(str(d.get("retryDelay", "")))
    if not delay:
        m = re.search(r"retry in ([\d.]+)\s*s", getattr(e, "message", "") or str(e), re.I)
        delay = float(m.group(1)) if m else 0.0
    return ids, values, delay


class RateLimiter:
    """Spaces out requests across all worker threads (e.g. 10 per minute = one every 6 s)."""

    _shared: dict[str, "RateLimiter"] = {}
    _shared_lock = threading.Lock()

    def __init__(self, per_minute: float):
        self.per_minute = per_minute
        self.interval = 60.0 / per_minute
        self._next = 0.0
        self._lock = threading.Lock()

    @classmethod
    def shared(cls, key: str, per_minute: float) -> "RateLimiter | None":
        if per_minute <= 0:
            return None
        with cls._shared_lock:
            lim = cls._shared.get(key)
            if lim is None or lim.per_minute != per_minute:
                lim = cls._shared[key] = cls(per_minute)
            return lim

    def wait(self, cancelled: Callable[[], bool] = lambda: False, sleep: Callable[[float], None] = time.sleep) -> None:
        with self._lock:
            slot = max(time.monotonic(), self._next)
            self._next = slot + self.interval
        while (remaining := slot - time.monotonic()) > 0:
            if cancelled():
                raise Cancelled()
            sleep(min(0.25, remaining))

    def push_back(self, seconds: float) -> None:
        """After a rate-limit error, make every thread wait at least this long."""
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


class Cancelled(Exception):
    pass


def _join_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def build_prompt(kind: str, frame_count: int, context: str = "", people: list[str] | None = None) -> str:
    what = (f"these {frame_count} frames from one home video" if kind == "video" and frame_count > 1
            else "this frame from a home video" if kind == "video" else "this photo")
    lines = [
        "You are naming files in a family photo and video archive.",
        f"Describe the main subject or activity in {what} as a short title of 3 to 7 words.",
        "Use sentence case. Do not include dates, file extensions, quotes, or ending punctuation.",
        "Do not guess names of people or places unless they appear in the context below.",
    ]
    if people:
        lines.append(f"Face recognition identified these people: {_join_names(people)}. "
                     "Use their names in the title. Do not name anyone else.")
    if context.strip():
        lines.append(f"Context from the family: {context.strip()}")
    lines.append("Reply with the title only.")
    return "\n".join(lines)


# ----------------------------------------------------------------- Ollama

class OllamaBackend:
    name = OLLAMA

    def __init__(self, host: str = DEFAULT_OLLAMA_HOST, model: str = DEFAULT_OLLAMA_MODEL,
                 timeout: float = 300):
        self.host = (host or DEFAULT_OLLAMA_HOST).rstrip("/")
        self.model = model or DEFAULT_OLLAMA_MODEL
        self.timeout = timeout

    def _request(self, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.host + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except ValueError:
                pass
            if e.code >= 500 or e.code == 429:
                raise TransientError(f"Ollama HTTP {e.code}: {detail}") from e
            raise DescribeError(f"Ollama HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ConnectionRefusedError):
                raise DescribeError(f"Ollama isn't running at {self.host}. Start Ollama and try again.") from e
            raise TransientError(f"Ollama connection problem: {reason}") from e

    def describe(self, images: list[bytes], prompt: str) -> str:
        body = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "user", "content": prompt,
                          "images": [base64.b64encode(b).decode() for b in images]}],
            "options": {"temperature": 0.2},
        }
        reply = self._request("/api/chat", body)
        text = (reply.get("message") or {}).get("content", "")
        if not text.strip():
            raise TransientError("Ollama returned an empty answer")
        return text

    def list_models(self) -> list[str]:
        return sorted(m["name"] for m in self._request("/api/tags", timeout=10).get("models", []))


# ----------------------------------------------------------------- Gemini

class GeminiBackend:
    name = GEMINI

    def __init__(self, api_key: str, model: str = DEFAULT_GEMINI_MODEL):
        if not api_key:
            raise DescribeError("No Gemini API key. Add one in Settings or set GEMINI_API_KEY.")
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise DescribeError("The google-genai package isn't installed: pip install google-genai") from e
        self._types = types
        self._client = genai.Client(api_key=api_key)
        self.model = model or DEFAULT_GEMINI_MODEL

    def _call(self, fn):
        from google.genai import errors
        try:
            return fn()
        except errors.APIError as e:
            code = getattr(e, "code", 0) or 0
            text = getattr(e, "message", "") or str(e)
            msg = f"Gemini HTTP {code}: {text}"
            if code == 429:
                ids, values, delay = _quota_details(e)
                if any("PerDay" in q for q in ids):
                    raise QuotaError(
                        f"Gemini's daily limit for {self.model} is used up. It resets at midnight Pacific time. "
                        "Finish later, switch to another model (↻), use Ollama, or add billing in AI Studio.") from e
                if "0" in values or re.search(r"limit:\s*0\b", text):
                    raise QuotaError(
                        f"{self.model} has no free quota on this API key. Pick another model (↻) "
                        "or enable billing in AI Studio.") from e
                raise TransientError(f"Gemini rate limit (too many requests per minute): {text[:160]}",
                                     retry_after=delay) from e
            if code in (408, 500, 502, 503, 504):
                raise TransientError(msg) from e
            raise DescribeError(msg) from e
        except (ConnectionError, TimeoutError, OSError) as e:
            raise TransientError(f"Gemini connection problem: {e}") from e

    def describe(self, images: list[bytes], prompt: str) -> str:
        t = self._types
        parts = [t.Part.from_bytes(data=b, mime_type="image/jpeg") for b in images] + [prompt]
        resp = self._call(lambda: self._client.models.generate_content(
            model=self.model, contents=parts,
            config=t.GenerateContentConfig(temperature=0.2)))
        text = getattr(resp, "text", None) or ""
        if not text.strip():
            reason = ""
            try:
                reason = str(resp.candidates[0].finish_reason)
            except Exception:  # noqa: BLE001
                pass
            raise DescribeError(f"Gemini returned no text {reason}".strip())
        return text

    def list_models(self) -> list[str]:
        models = self._call(lambda: list(self._client.models.list()))
        names = []
        for m in models:
            actions = getattr(m, "supported_actions", None) or []
            name = (m.name or "").removeprefix("models/")
            if "gemini" in name and (not actions or "generateContent" in actions):
                names.append(name)
        return sorted(names)


def make_backend(kind: str, *, ollama_host: str = "", ollama_model: str = "",
                 gemini_key: str = "", gemini_model: str = ""):
    if kind == GEMINI:
        return GeminiBackend(gemini_key, gemini_model)
    return OllamaBackend(ollama_host, ollama_model)


def with_retry(fn: Callable[[], str], attempts: int = 5, base_delay: float = 2.0,
               cancelled: Callable[[], bool] = lambda: False,
               on_retry: Callable[[int, float, Exception], None] | None = None,
               sleep: Callable[[float], None] = time.sleep) -> str:
    """Run fn, backing off exponentially on TransientError."""
    for attempt in range(1, attempts + 1):
        if cancelled():
            raise Cancelled()
        try:
            return fn()
        except TransientError as e:
            if attempt == attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1)) * (0.75 + random.random() / 2)
            if e.retry_after:                       # the server said how long to wait
                delay = max(delay, e.retry_after + random.random())
            if on_retry:
                on_retry(attempt, delay, e)
            waited = 0.0
            while waited < delay:
                if cancelled():
                    raise Cancelled() from e
                step = min(0.25, delay - waited)
                sleep(step)
                waited += step
    raise AssertionError("unreachable")
