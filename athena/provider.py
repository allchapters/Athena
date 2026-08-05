"""Day 1 — the provider: the one file in Athena that speaks to a model.

Concept: the *adapter boundary*. Above it, a small neutral message format;
here alone, Gemini's wire protocol and failure modes. Design rules:
  * One boundary — swap this file and the harness runs on another provider.
  * Neutral in, neutral out: plain dicts cross it, never a "candidate".
  * Signatures survive the round trip — Gemini 3 wants its thoughtSignature back.
  * Transport trouble stops here, so callers assume a call returns or raises.
"""

import json
import os
import time
import urllib.error
import urllib.request

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-3.1-pro-preview"


def api_key() -> str:
    """Return the key: ATHENA_API_KEY, else GEMINI_API_KEY, else raise.

    ATHENA_API_KEY wins so a workshop key can coexist with an existing one.
    """
    key = os.environ.get("ATHENA_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("No API key. Set ATHENA_API_KEY, or: source Athena-key.sh")
    return key


def complete(model: str, system: str, messages: list, tools: list | None = None) -> dict:
    """Run one model turn.

    Returns {"text", "tool_calls": [{"name", "args", "signature"}], "usage":
    {"input", "output"}}. `tools` is a list of {"schema": ...} specs; None
    forbids tool use, which is how the loop forces a closing answer.
    """
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": _to_wire(messages),
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 65536},
    }
    if tools:
        body["tools"] = [{"functionDeclarations": [t["schema"] for t in tools]}]
    return _from_wire(_post(f"{API_ROOT}/{model}:generateContent?key={api_key()}", body))


def _to_wire(messages: list) -> list:
    """Translate neutral messages into Gemini `contents`.

    Three neutral shapes and no more: {"role": "user", "text"}; {"role":
    "assistant", "text", "tool_calls"}; {"role": "tool", "name", "text"}.
    """
    out = []
    for msg in messages:
        role = msg["role"]
        if role == "user":
            out.append({"role": "user", "parts": [{"text": msg["text"]}]})
        elif role == "assistant":
            # Skip empty text: Gemini rejects a part whose text is "".
            parts = [{"text": msg["text"]}] if msg.get("text") else []
            for call in msg.get("tool_calls") or []:
                part = {"functionCall": {"name": call["name"], "args": call["args"]}}
                # Gemini 3 checks that the signature it issued with a call comes
                # back on that same part, or the reasoning behind it is lost.
                if call.get("signature"):
                    part["thoughtSignature"] = call["signature"]
                parts.append(part)
            out.append({"role": "model", "parts": parts})
        elif role == "tool":
            # Tool output re-enters as a *user* turn: the model didn't author it.
            out.append({"role": "user", "parts": [{"functionResponse": {
                "name": msg["name"], "response": {"result": msg["text"]}}}]})
    return out


def _from_wire(data: dict) -> dict:
    """Flatten one Gemini response into the neutral completion dict."""
    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    chunks, calls = [], []
    for part in parts:
        # Thought parts are private reasoning, not answer text. Never surface them.
        if part.get("thought"):
            continue
        if "text" in part:
            chunks.append(part["text"])
        if "functionCall" in part:
            fc = part["functionCall"]
            calls.append({"name": fc.get("name", ""), "args": fc.get("args") or {},
                          "signature": part.get("thoughtSignature")})
    usage = data.get("usageMetadata") or {}
    return {"text": "".join(chunks), "tool_calls": calls,
            "usage": {"input": usage.get("promptTokenCount", 0),
                      "output": usage.get("candidatesTokenCount", 0)}}


def _post(url: str, body: dict, retries: int = 5) -> dict:
    """POST JSON and decode JSON, retrying only what deserves a retry."""
    payload = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    for attempt in range(retries):
        last = attempt == retries - 1
        try:
            req = urllib.request.Request(url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=600) as response:
                return json.loads(response.read())
        # 429/5xx are transient; any other status is a bug in our own request and
        # must surface at once. HTTPError subclasses URLError — catch it first.
        except urllib.error.HTTPError as exc:
            if last or exc.code not in (429, 500, 502, 503):
                detail = exc.read().decode("utf-8", "replace")[:400]
                raise RuntimeError(f"Gemini HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if last:
                raise RuntimeError(f"Gemini unreachable ({retries} tries): {exc}") from exc
        time.sleep(2 ** attempt * 2)
