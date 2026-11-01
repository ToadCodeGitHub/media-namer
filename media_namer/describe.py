"""AI backends that turn images into a short description.

Two backends:
  * Ollama  - runs on your own computer; nothing leaves the house.
  * Gemini  - Google's API; images are sent to Google.
"""
from __future__ import annotations

import base64
import json
import random
import socket
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


class Cancelled(Exception):
    pass


def build_prompt(kind: str, frame_count: int, context: str = "") -> str:
    what = (f"these {frame_count} frames from one home video" if kind == "video" and frame_count > 1
            else "this frame from a home video" if kind == "video" else "this photo")
    lines = [
        "You are naming files in a family photo and video archive.",
        f"Describe the main subject or activity in {what} as a short title of 3 to 7 words.",
        "Use sentence case. Do not include dates, file extensions, quotes, or ending punctuation.",
        "Do not guess names of people or places unless they appear in the context below.",
    ]
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
            msg = f"Gemini HTTP {code}: {getattr(e, 'message', '') or e}"
            if code in (408, 429, 500, 502, 503, 504):
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
