"""A small OpenRouter client (OpenAI-style chat completions) that rotates through several API keys.

It has the same face as GeminiClient (generate / check_key / forget / status), so the agent and the AI
chats do not care which provider is behind it. The talk is kept in the Gemini layout everywhere else
(contents with "parts"); the two functions below turn it into OpenAI messages and the answer back.

A key that hits its limit (429) rests for a minute; a key OpenRouter rejects (401) or that has no credit
(402) is set aside until it is added again."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

from telegram_manager.ai.gemini import GeminiError, NoKeysError, _error_text

logger = logging.getLogger(__name__)

API_ROOT = "https://openrouter.ai/api/v1"
CHAT_URL = f"{API_ROOT}/chat/completions"
KEY_URL = f"{API_ROOT}/key"
RATE_LIMIT_REST_SECONDS = 60
REQUEST_TIMEOUT = 45
CONNECT_TIMEOUT = 6
REASONING_EFFORT = "low"  # reasoning models think less, so the answer comes sooner (models without it ignore the setting)
APP_TITLE = "Telegram Manager"

Post = Callable[[str, dict, dict], Awaitable[tuple[int, dict]]]
Get = Callable[[str, dict], Awaitable[tuple[int, dict]]]

_client: Any = None
_client_loop: Any = None


def _shared_client():
    """One connection pool for every request (a fresh TLS handshake per call costs a few hundred ms)."""
    global _client, _client_loop
    import httpx

    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT),
            limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=120),
        )
        _client_loop = loop
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _httpx_post(url: str, headers: dict, body: dict) -> tuple[int, dict]:
    response = await _shared_client().post(url, headers=headers, json=body)
    try:
        return response.status_code, response.json()
    except Exception:
        return response.status_code, {}


async def _httpx_get(url: str, headers: dict) -> tuple[int, dict]:
    response = await _shared_client().get(url, headers=headers)
    try:
        return response.status_code, response.json()
    except Exception:
        return response.status_code, {}


# --- format conversion -----------------------------------------------------------------------------
def to_messages(system: str, contents: list[dict]) -> list[dict]:
    """Gemini-layout contents -> OpenAI messages. A tool answer is matched to its call by id (or, failing
    that, by name and order), because OpenAI-style APIs insist that every tool call has its answer."""
    messages: list[dict] = [{"role": "system", "content": system}]
    waiting: list[tuple[str, str]] = []  # (call id, tool name) of the last assistant turn, still unanswered
    counter = 0
    for turn in contents:
        parts = turn.get("parts") or []
        if turn.get("role") == "model":
            text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
            calls: list[dict] = []
            waiting = []
            for part in parts:
                call = part.get("functionCall")
                if not call:
                    continue
                counter += 1
                call_id = call.get("id") or f"call_{counter}"
                name = call.get("name", "")
                waiting.append((call_id, name))
                calls.append({
                    "id": call_id, "type": "function",
                    "function": {"name": name, "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False)},
                })
            if calls:
                messages.append({"role": "assistant", "content": text or None, "tool_calls": calls})
            elif text:
                messages.append({"role": "assistant", "content": text})
        else:
            for part in parts:
                answer = part.get("functionResponse")
                if not answer:
                    continue
                call_id = answer.get("id")
                if not call_id:
                    index = next((i for i, (_cid, name) in enumerate(waiting) if name == answer.get("name")), None)
                    call_id = waiting.pop(index)[0] if index is not None else f"call_{counter}"
                messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(answer.get("response") or {}, ensure_ascii=False)})
            text = "\n".join(p["text"] for p in parts if "text" in p)
            if text:
                messages.append({"role": "user", "content": text})
    return messages


def to_tools(declarations: list[dict] | None) -> list[dict]:
    """Function declarations (already plain JSON schema) -> OpenAI tools."""
    return [
        {"type": "function", "function": {"name": d["name"], "description": d.get("description", ""), "parameters": d.get("parameters") or {"type": "object", "properties": {}}}}
        for d in declarations or []
    ]


def to_content(data: dict) -> dict:
    """OpenAI-style answer -> {"role": "model", "parts": [...]} like Gemini's, with each call's id kept."""
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = message.get("content")
    if isinstance(text, list):  # some providers answer with a list of text blocks
        text = "".join(block.get("text", "") for block in text if isinstance(block, dict))
    parts: list[dict] = []
    if text:
        parts.append({"text": text})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            args = json.loads(function.get("arguments") or "{}")
        except (TypeError, ValueError):
            args = {}
        parts.append({"functionCall": {"name": function.get("name", ""), "args": args if isinstance(args, dict) else {}, "id": call.get("id") or ""}})
    if not parts:
        raise GeminiError(f"The model gave no answer ({choice.get('finish_reason') or 'empty reply'}). Try again or pick another model.")
    return {"role": "model", "parts": parts}


# --- the client ---------------------------------------------------------------------------------------
class OpenRouterClient:
    def __init__(self, post: Post | None = None, get: Get | None = None) -> None:
        self._post = post or _httpx_post
        self._get = get or _httpx_get
        self._rest_until: dict[str, float] = {}  # key -> time it may be used again
        self._rejected: set[str] = set()
        self._no_reasoning: set[str] = set()  # models that refused the reasoning setting: asked without it from now on
        self._next = 0

    def forget(self, key: str) -> None:
        self._rest_until.pop(key, None)
        self._rejected.discard(key)

    def status(self, key: str) -> str:
        """'ok', 'resting' (hit its limit, back within a minute) or 'rejected' (OpenRouter refused it)."""
        if key in self._rejected:
            return "rejected"
        return "resting" if self._rest_until.get(key, 0) > time.monotonic() else "ok"

    def _usable(self, keys: list[str]) -> list[str]:
        now = time.monotonic()
        usable = [k for k in keys if k not in self._rejected and self._rest_until.get(k, 0) <= now]
        if not usable:
            return []
        start = self._next % len(usable)
        self._next += 1
        return usable[start:] + usable[:start]  # take turns, so no key is worn out first

    @staticmethod
    def _headers(key: str) -> dict:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "X-Title": APP_TITLE}

    async def generate(
        self,
        keys: list[str],
        model: str,
        system: str,
        contents: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.4,
        thinking: str | None = REASONING_EFFORT,
    ) -> dict:
        """Returns the model's content: {"role": "model", "parts": [...]} (same as GeminiClient)."""
        if not keys:
            raise NoKeysError("No OpenRouter API key is saved. Add one in /aisettings → Keys.")
        model = model.strip()
        messages = to_messages(system, contents)
        openai_tools = to_tools(tools)

        def build() -> dict[str, Any]:
            body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
            if openai_tools:
                body["tools"] = openai_tools
            if thinking and model not in self._no_reasoning:
                body["reasoning"] = {"effort": thinking}
            return body

        problems: list[str] = []
        for key in self._usable(keys):
            try:
                status, data = await self._post(CHAT_URL, self._headers(key), build())
                if status == 400 and thinking and model not in self._no_reasoning and "reasoning" in _error_text(data).lower():
                    self._no_reasoning.add(model)  # this model does not take that setting: ask again plainly
                    status, data = await self._post(CHAT_URL, self._headers(key), build())
            except (asyncio.TimeoutError, OSError) as exc:
                problems.append(f"network problem ({type(exc).__name__})")
                continue
            except Exception as exc:  # httpx errors and anything else the transport raises
                problems.append(f"connection problem ({type(exc).__name__})")
                continue
            if status == 200 and data.get("error") and not data.get("choices"):
                # OpenRouter sometimes answers 200 with the provider's failure inside
                code = str((data["error"] or {}).get("code", ""))
                status = int(code) if code.isdigit() else 502
            if status == 200:
                return to_content(data)
            reason = _error_text(data)
            if status in (429, 408):
                self._rest_until[key] = time.monotonic() + RATE_LIMIT_REST_SECONDS
                problems.append("a key hit its limit and is resting for a minute")
            elif status in (401, 402):
                self._rejected.add(key)
                why = "has no credit left" if status == 402 else "was rejected by OpenRouter"
                problems.append(f"a key {why} ({reason[:80] or status})")
            elif status >= 500:
                problems.append(f"OpenRouter / the model's provider is having trouble (HTTP {status})")
            elif status == 404:
                raise GeminiError(
                    f"OpenRouter said: {reason[:200] or 'model not found'}. This model may not exist or may not support tools — "
                    "pick another one in /aisettings → 🧠 Model (openrouter/free picks one that works)."
                )
            else:
                raise GeminiError(f"OpenRouter said: {reason[:300] or 'HTTP ' + str(status)}")
        if not problems:
            raise GeminiError("All your OpenRouter keys are resting or rejected. Add a new key in /aisettings → Keys.")
        raise GeminiError("Could not get an answer: " + "; ".join(dict.fromkeys(problems)) + ".")

    async def check_key(self, key: str, model: str = "") -> str | None:
        """None if the key works, otherwise why not. Asks OpenRouter about the key itself, which costs nothing."""
        try:
            status, data = await self._get(KEY_URL, self._headers(key))
        except Exception as exc:
            return f"connection problem ({type(exc).__name__})"
        finally:
            self.forget(key)
        if status == 200:
            return None
        if status in (401, 403):
            return "OpenRouter ne key reject ki"
        return f"OpenRouter said: {_error_text(data)[:100] or 'HTTP ' + str(status)}"
