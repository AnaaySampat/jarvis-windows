"""Google Gemini provider.

A thin REST client for Google's Generative Language API that mirrors the small
surface ``groq_bridge`` needs: send / stream / vision / image. ``groq_bridge``
keeps owning config, history, the system prompt and live context; when the
selected model is a Gemini/Gemma one it just hands the already-assembled
OpenAI-style ``messages`` to the functions here, which translate them into
Gemini's ``contents`` shape and call the API.

Endpoint: when Vertex/ADC is active (the primary path here) chat/vision calls go
to Vertex AI's **global** endpoint (aiplatform.googleapis.com); without ADC they
fall back to the AI Studio Developer API (generativelanguage.googleapis.com) with
an API key. The model id is identical on both.

Models this app drives (all verified callable on this project's Vertex global
endpoint — the regional endpoint 404s the 3.x ones):
  • gemini-3.1-flash-lite   — fast voice default (no thinking channel; lowest latency)
  • gemini-3.5-flash        — flagship: rich/structured/agentic turns (thinking-capable)
  • gemini-2.5-flash        — capable mid-tier; native-audio REST fallback
  • gemini-2.5-flash-image  — inline image generation (TEXT+IMAGE generateContent)
  • imagen-4.0-generate-001 — dedicated image model (regional :predict; preferred)

Image generation uses **gemini-3.1-flash-image** via the normal `generateContent`
endpoint (responseModalities TEXT+IMAGE, image comes back as inline_data). It
works whenever a Gemini key is set, regardless of which chat model is selected
(see gemini_generate_image). (Imagen's `:predict` path was dropped — it required
a paid plan.)

Auth: Gemini Developer API keys go on the ``?key=`` query param. This covers the
``AIza…`` keys AND the newer ``AQ.…`` keys (verified: ``AQ.`` keys are rejected
as Bearer tokens but accepted as ``?key=``). Only a genuine OAuth2 access token
(``ya29.…``) is sent as a Bearer header.
"""

from __future__ import annotations

import json
import re
from typing import AsyncGenerator, Optional

import httpx

from . import quota
from . import vertex_auth

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
# Split timeout: fail fast on a dead network / slow DNS (connect) while still
# allowing a long generation to stream (read). A single scalar would let a stuck
# connect block a whole turn for the full 60s.
_TIMEOUT = httpx.Timeout(60.0, connect=5.0, pool=5.0)

# One shared async client so consecutive Gemini turns reuse the same keep-alive
# TCP+TLS connection instead of paying a fresh DNS+handshake every call (mirrors
# groq_bridge._client()). Created lazily on the running loop; closed via aclose().
_http_client: "httpx.AsyncClient | None" = None


def _client() -> "httpx.AsyncClient":
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _http_client


async def aclose() -> None:
    """Close the shared Gemini HTTP client (call on backend shutdown)."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


class GeminiError(Exception):
    """A hard Gemini failure (no key / transport error / non-200). Carries a
    friendly, user-facing message as its string. It is RAISED rather than returned
    so callers that record conversation history (send_prompt / stream_prompt) can
    surface the message to the user WITHOUT persisting it as a genuine assistant
    reply — which would otherwise be re-fed as context on later turns.

    ``status`` / ``retry_after`` / ``detail`` carry the STRUCTURED failure so the
    route ladder can tell a spent quota (bench for a while, rotate to another key)
    apart from a timeout (bench briefly, never escalate) without pattern-matching
    the friendly sentence above — which is translated for humans, not for code.
    ``status`` is 0 when the call never reached the server."""

    def __init__(self, message: str, status: int = 0,
                 retry_after: "float | None" = None, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.detail = detail

# Models that *can* do native audio dialogue via Google's Live API. We don't
# drive the Live API yet, so selecting one of these still works — it answers in
# text and we speak it with the existing TTS engine.
NATIVE_AUDIO_MODELS = {
    "gemini-live-2.5-flash-native-audio",               # Vertex Live API id
    "gemini-2.5-flash-native-audio-preview-12-2025",   # Google AI Studio variant
    "gemini-2.5-flash-native-audio",                    # short alias (older configs)
}

# Image generation is action-routed to IMAGE_GEN_MODEL, not picked in the chat
# dropdown, so this stays empty (is_image_model → False for all chat models).
IMAGE_MODELS: set = set()

# Plain text/vision model used as the REST fallback for native-audio models.
TEXT_FALLBACK_MODEL = "gemini-2.5-flash"

# Flash 2.5 image-generation model (generateContent, TEXT+IMAGE modalities) — the
# fallback / Developer-API image path.
IMAGE_GEN_MODEL = "gemini-2.5-flash-image"

# Imagen 4 — the dedicated, higher-quality image model. Vertex-only (:predict
# endpoint, instances/parameters). Preferred whenever Vertex/ADC is active.
IMAGEN_MODEL = "imagen-4.0-generate-001"


def is_gemini_model(model: str) -> bool:
    m = (model or "").lower()
    return m.startswith("gemini") or m.startswith("gemma")


def is_image_model(model: str) -> bool:
    return (model or "").lower() in IMAGE_MODELS


def is_native_audio_model(model: str) -> bool:
    return (model or "").lower() in NATIVE_AUDIO_MODELS


def rest_model(model: str) -> str:
    """The model id to actually call on the generateContent REST endpoint.

    Native-audio models only work over the Live API (not generateContent), so
    until that's wired up we transparently answer with a normal text model.
    """
    if is_native_audio_model(model):
        return TEXT_FALLBACK_MODEL
    # A pinned id Google has since retired (the ladder remaps its own routes; this
    # catches the direct callers — greeting, translate, pre-warm).
    return quota.live_model(model)


def _auth(api_key: str) -> "tuple[dict, dict]":
    """Return (query_params, headers) appropriate for this credential type.

    Gemini Developer API keys (``AIza…`` and ``AQ.…``) authenticate via the
    ``?key=`` query param. Only a genuine OAuth2 access token (``ya29.…``) uses a
    Bearer header. (Sending an ``AQ.`` key as Bearer gets a 401 — verified.)"""
    key = (api_key or "").strip()
    if key.startswith("ya29."):                    # genuine OAuth2 access token
        return {}, {"Authorization": f"Bearer {key}"}
    return {"key": key}, {}                         # API key (AIza… / AQ.…)


# Gemini generative calls (generateContent / streamGenerateContent) target the
# multi-region "global" endpoint, NOT the configured regional one. Two reasons,
# both verified against this project:
#   • the newer models (gemini-3.x) are served ONLY from global — regional
#     endpoints 404 them ("model not found / no access"); and
#   • global is markedly faster here — measured ~3× lower latency than
#     us-central1 for flash-lite (~0.6s vs ~1.6s to first byte).
# Imagen's :predict path stays regional (see vertex_imagen_generate) — Imagen is
# region-scoped and reads vertex_auth.region() directly.
_GEMINI_LOCATION = "global"


def _endpoint(model: str, action: str) -> str:
    """Full URL for a generateContent-family call.

    Vertex AI when ADC is active (so usage bills to the user's GCP project +
    credits): a project-scoped publisher-model path on the **global** endpoint.
    Otherwise the Developer API path. ``action`` is e.g. ``generateContent`` /
    ``streamGenerateContent``."""
    if vertex_auth.enabled():
        proj = vertex_auth.project()
        return (f"https://aiplatform.googleapis.com/v1/projects/{proj}"
                f"/locations/{_GEMINI_LOCATION}/publishers/google/models/{model}:{action}")
    return f"{API_BASE}/models/{model}:{action}"


async def _request_auth(api_key: str) -> "tuple[dict, dict]":
    """(query params, headers) for the active credential. Vertex → a fresh ADC
    bearer token (no key needed) plus the quota-project header so usage bills to
    the user's GCP project; otherwise the API-key form from :func:`_auth`."""
    if vertex_auth.enabled():
        token = await vertex_auth.get_access_token_async()
        headers = {"Authorization": f"Bearer {token}"}
        proj = vertex_auth.project()
        if proj:
            headers["x-goog-user-project"] = proj
        return {}, headers
    return _auth(api_key)


def _data_url_to_inline(data_url: str) -> "Optional[dict]":
    """data:image/png;base64,XXXX → Gemini inline_data part."""
    try:
        head, b64 = data_url.split(",", 1)
        mime = head.split(";")[0].split(":", 1)[1] or "image/png"
        return {"inline_data": {"mime_type": mime, "data": b64}}
    except Exception:  # noqa: BLE001
        return None


def _to_gemini(messages: list) -> "tuple[str, list]":
    """Translate OpenAI-style messages into (system_instruction_text, contents).

    system → merged into the system instruction. user → role 'user',
    assistant → role 'model'. String content becomes one text part; list content
    (our vision shape: [{type:text…},{type:image_url…}]) becomes mixed parts.
    """
    system_bits: list[str] = []
    contents: list[dict] = []
    for m in messages or []:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            if isinstance(content, str):
                system_bits.append(content)
            continue
        g_role = "model" if role == "assistant" else "user"
        parts: list[dict] = []
        if isinstance(content, list):
            for piece in content:
                if piece.get("type") == "text":
                    parts.append({"text": piece.get("text", "")})
                elif piece.get("type") == "image_url":
                    url = (piece.get("image_url") or {}).get("url", "")
                    part = _data_url_to_inline(url)
                    if part:
                        parts.append(part)
        else:
            parts.append({"text": str(content or "")})
        if parts:
            if contents and contents[-1]["role"] == g_role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": g_role, "parts": parts})
    # generateContent requires the first content role to be user.
    if contents and contents[0]["role"] == "model":
        contents.insert(0, {"role": "user", "parts": [{"text": "(earlier context)"}]})
    return "\n\n".join(system_bits).strip(), contents


def _body(messages: list, max_tokens: int, temperature: float,
          thinking_budget: "Optional[int]" = None, grounding: bool = False,
          tools: "Optional[list]" = None) -> dict:
    system_text, contents = _to_gemini(messages)
    gen = {"maxOutputTokens": max_tokens, "temperature": temperature}
    # Server-side "thinking" is the dominant latency lever on the flash models:
    # thinkingBudget=0 turns a ~1.6s reply into ~0.9s. Sent only when the caller
    # passes a budget (it gates on model support — lite models have no thinking
    # channel and pro keeps its default), so a model that can't take it is never
    # handed one.
    if thinking_budget is not None:
        gen["thinkingConfig"] = {"thinkingBudget": thinking_budget}
    body: dict = {"contents": contents, "generationConfig": gen}
    if system_text:
        body["system_instruction"] = {"parts": [{"text": system_text}]}
    # Grounding with Google Search: the model autonomously runs Google searches,
    # grounds its answer in live results and returns citations in
    # groundingMetadata. This is how JARVIS answers current-events / real-time
    # questions WITHOUT opening a browser. Gemini 2.x/3.x take the bare
    # `google_search` tool (the older `google_search_retrieval` shape is 1.5-only).
    if grounding:
        body["tools"] = [{"google_search": {}}]
    # Native function calling: advertise the tool palette as functionDeclarations.
    # Mutually exclusive with grounding in practice (chat never grounds), so we
    # don't merge — grounding wins if both are somehow requested.
    elif tools:
        body["tools"] = [{"functionDeclarations": tools}]
    return body


def _extract_text(data: dict) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError):
        return ""


def _extract_function_calls(data: dict) -> list:
    """Pull any ``functionCall`` parts from a (streamed) candidate as a list of
    ``{"name","args"}``. Gemini delivers each call complete in one part (no
    fragment reassembly, unlike OpenAI), so this can run per chunk."""
    calls: list = []
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return calls
    for p in parts:
        fc = p.get("functionCall") or p.get("function_call")
        if fc and fc.get("name"):
            calls.append({"name": fc["name"], "args": fc.get("args") or {}})
    return calls


def _extract_grounding(data: dict) -> "tuple[list, list]":
    """(sources, search_queries) from a grounded reply's groundingMetadata.

    sources is a list of {"title", "uri"} for the web pages the answer was
    grounded in (deduped, order-preserved); search_queries is what the model
    actually searched. Both empty when the model answered from its own knowledge
    without searching."""
    sources: list = []
    queries: list = []
    try:
        gm = data["candidates"][0].get("groundingMetadata") or {}
    except (KeyError, IndexError, TypeError):
        return sources, queries
    seen = set()
    for chunk in gm.get("groundingChunks") or []:
        web = chunk.get("web") or {}
        uri = (web.get("uri") or "").strip()
        title = (web.get("title") or "").strip()
        key = uri or title
        if not key or key in seen:
            continue
        seen.add(key)
        sources.append({"title": title or uri, "uri": uri})
    queries = [q for q in (gm.get("webSearchQueries") or []) if q]
    return sources, queries


def _extract_image(data: dict) -> "Optional[str]":
    """Pull the first inline image (as a data: URL) from a generateContent reply."""
    try:
        for p in data["candidates"][0]["content"]["parts"]:
            inline = p.get("inline_data") or p.get("inlineData")
            if inline and inline.get("data"):
                mime = inline.get("mime_type") or inline.get("mimeType") or "image/png"
                return f"data:{mime};base64,{inline['data']}"
    except (KeyError, IndexError, TypeError):
        pass
    return None


def _http_error_message(status: int) -> str:
    if status == 429:
        return "I've hit Gemini's rate limit. Give me a moment and try again."
    if status in (401, 403):
        return "My Gemini API key was rejected. Please check the key in Settings."
    if status == 400:
        return "Gemini rejected that request (the model ID or input may be invalid)."
    return "Sorry, the Gemini API returned an error. Please try again."


def _retry_after_seconds(resp) -> "float | None":
    """The provider's own requested wait, when it sent one. Gemini answers a spent
    quota with a plain Retry-After on some paths and a RetryInfo block in the error
    body on others, so check both rather than trusting one shape."""
    raw = resp.headers.get("retry-after") if getattr(resp, "headers", None) else None
    if raw:
        try:
            return float(str(raw).strip())
        except ValueError:
            pass
    try:
        body = resp.text or ""
    except Exception:  # noqa: BLE001 — a streamed body may not be readable here
        return None
    found = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body)
    return float(found.group(1)) if found else None


def _http_error(resp) -> GeminiError:
    """A GeminiError carrying both the friendly sentence and the raw facts the
    route ladder needs to decide how long to bench this route."""
    status = resp.status_code
    try:
        # Long enough to reach the quotaId ("...PerMinute..." / "...PerDay...") deep in
        # a 429's details block — it decides whether the route benches for seconds.
        detail = (resp.text or "")[:2000]
    except Exception:  # noqa: BLE001
        detail = ""
    return GeminiError(_http_error_message(status), status=status,
                       retry_after=_retry_after_seconds(resp), detail=detail)


async def gemini_send(messages: list, model: str, api_key: str,
                      max_tokens: int = 1024, temperature: float = 0.7,
                      thinking_budget: "Optional[int]" = None,
                      timeout: "Optional[float]" = None) -> str:
    if not api_key and not vertex_auth.enabled():
        raise GeminiError("I'm not connected to Gemini yet — no API key is configured.")
    params, headers = await _request_auth(api_key)
    headers["Content-Type"] = "application/json"
    url = _endpoint(model, "generateContent")
    # Per-call timeout override: the autopilot's short mechanical calls fail fast
    # (so a stalled API hands off to the fallback in seconds, not 60s), while chat
    # keeps the client default (a long thinking answer can legitimately run ~60s).
    post_kw = {"params": params, "headers": headers,
               "json": _body(messages, max_tokens, temperature, thinking_budget)}
    if timeout is not None:
        post_kw["timeout"] = timeout
    try:
        resp = await _client().post(url, **post_kw)
    except Exception as exc:  # noqa: BLE001
        # Some httpx errors (notably ReadTimeout) stringify to "" — name the type
        # so a timeout is distinguishable from a connect/DNS failure in the log.
        print(f"[Gemini] request failed: {type(exc).__name__}: {exc}", flush=True)
        raise GeminiError("Sorry, I couldn't reach Gemini just now. Please try again.") from exc
    if resp.status_code != 200:
        print(f"[Gemini] HTTP {resp.status_code}: {resp.text[:300]}", flush=True)
        raise _http_error(resp)
    # An empty completion is returned as "" — NOT a canned "could you rephrase?",
    # which callers took for a real answer (the autopilot parsed it as its next step,
    # chat saved it to history). Every caller treats "" as no answer.
    return _extract_text(resp.json())


async def gemini_stream(messages: list, model: str, api_key: str,
                        max_tokens: int = 1024,
                        temperature: float = 0.7,
                        thinking_budget: "Optional[int]" = None,
                        tools: "Optional[list]" = None,
                        tool_sink: "Optional[list]" = None) -> AsyncGenerator:
    """Yield (delta, full) chunks, matching groq_bridge.stream_prompt.

    When ``tools`` are advertised, any ``functionCall`` the model emits is
    appended to ``tool_sink`` as ``{"id","name","args"}`` (ids synthesised, since
    Gemini calls carry none). The text yield is unchanged."""
    if not api_key and not vertex_auth.enabled():
        raise GeminiError("I'm not connected to Gemini yet — no API key is configured.")
    params, headers = await _request_auth(api_key)
    params["alt"] = "sse"
    headers["Content-Type"] = "application/json"
    url = _endpoint(model, "streamGenerateContent")
    full = ""
    try:
        async with _client().stream("POST", url, params=params, headers=headers,
                                    json=_body(messages, max_tokens, temperature,
                                               thinking_budget, tools=tools)) as resp:
            if resp.status_code != 200:
                await resp.aread()
                print(f"[Gemini] stream HTTP {resp.status_code}: {resp.text[:300]}", flush=True)
                # Nothing streamed yet → a hard error the caller must not record.
                raise _http_error(resp)
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw = line[len("data:"):].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if tool_sink is not None:
                    for fc in _extract_function_calls(obj):
                        tool_sink.append({"id": f"call_{len(tool_sink)}",
                                          "name": fc["name"], "args": fc["args"]})
                delta = _extract_text(obj)
                if delta:
                    full += delta
                    yield delta, full
    except GeminiError:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[Gemini] stream failed: {exc}", flush=True)
        # If the failure happened before any content was delivered, it's a hard
        # error; surface it via GeminiError so it isn't saved as a reply. If we'd
        # already streamed partial content, just stop quietly.
        if not full:
            raise GeminiError("Sorry, I couldn't reach Gemini just now. Please try again.") from exc


async def gemini_vision(image_data_url: str, question: str, model: str,
                        api_key: str) -> str:
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": question or "Describe what's on this screen."},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ],
    }]
    return await gemini_send(messages, model, api_key, max_tokens=600, temperature=0.3)


async def gemini_grounded(messages: list, model: str, api_key: str,
                          max_tokens: int = 800, temperature: float = 0.3,
                          thinking_budget: "Optional[int]" = None,
                          timeout: "Optional[float]" = None
                          ) -> "tuple[str, list, list]":
    """One grounded completion: Gemini runs Google Search itself and grounds the
    answer in live results. Returns (answer_text, sources, search_queries).

    ``sources`` is a list of {"title","uri"} citations; ``search_queries`` is what
    the model searched. Raises :class:`GeminiError` on a hard failure so callers
    can surface a friendly message (mirrors ``gemini_send``)."""
    if not api_key and not vertex_auth.enabled():
        raise GeminiError("I'm not connected to Gemini yet — no API key is configured.")
    params, headers = await _request_auth(api_key)
    headers["Content-Type"] = "application/json"
    url = _endpoint(model, "generateContent")
    body = _body(messages, max_tokens, temperature, thinking_budget, grounding=True)
    post_kw = {"params": params, "headers": headers, "json": body}
    if timeout is not None:       # a long job (the model ranking) outlives the default
        post_kw["timeout"] = timeout
    try:
        resp = await _client().post(url, **post_kw)
    except Exception as exc:  # noqa: BLE001
        print(f"[Gemini] grounded request failed: {exc}", flush=True)
        raise GeminiError("Sorry, I couldn't reach Gemini just now. Please try again.") from exc
    if resp.status_code != 200:
        print(f"[Gemini] grounded HTTP {resp.status_code}: {resp.text[:300]}", flush=True)
        raise _http_error(resp)
    data = resp.json()
    text = _extract_text(data)
    sources, queries = _extract_grounding(data)
    return text, sources, queries


async def vertex_imagen_generate(prompt: str,
                                 model: str = IMAGEN_MODEL) -> "tuple[bool, str, Optional[str]]":
    """Generate an image via Imagen on Vertex AI — the dedicated ``:predict``
    endpoint (instances/parameters → predictions[].bytesBase64Encoded), higher
    quality than the Gemini flash-image model. Vertex/ADC only; returns
    (ok, message, data_url)."""
    prompt = (prompt or "").strip()
    if not prompt:
        return False, "Tell me what image to create.", None
    if not vertex_auth.enabled():
        return False, "Imagen needs Vertex (gcloud ADC) — not configured.", None
    token = await vertex_auth.get_access_token_async()
    region = vertex_auth.region()
    project = vertex_auth.project()
    url = (f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}"
           f"/locations/{region}/publishers/google/models/{model}:predict")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1}}
    try:
        resp = await _client().post(url, headers=headers, json=body)
    except Exception as exc:  # noqa: BLE001
        return False, f"Image generation failed: {exc}", None
    if resp.status_code != 200:
        print(f"[Imagen] HTTP {resp.status_code}: {resp.text[:300]}", flush=True)
        try:
            detail = (resp.json().get("error") or {}).get("message", "").strip()
        except Exception:  # noqa: BLE001
            detail = ""
        return False, f"Image generation failed: {detail}" if detail else _http_error_message(resp.status_code), None
    preds = resp.json().get("predictions") or []
    if not preds:
        # Imagen returns no predictions when the prompt trips a safety filter.
        return False, "That image couldn't be generated (it may have been blocked by safety filters).", None
    p0 = preds[0]
    b64 = p0.get("bytesBase64Encoded")
    mime = p0.get("mimeType") or "image/png"
    if not b64:
        # A prediction with no image bytes means Imagen produced a result but
        # filtered the image out — almost always a content / safety / IP block
        # (e.g. a trademarked character). Say that plainly instead of the opaque
        # "no image data", so the user knows to rephrase rather than retry.
        return (False, "That image couldn't be generated — the prompt was likely "
                       "blocked by content filters. Try rephrasing it.", None)
    return True, f"Here's the image for: {prompt}", f"data:{mime};base64,{b64}"


async def gemini_generate_image(prompt: str, api_key: str,
                                model: str = IMAGE_GEN_MODEL) -> "tuple[bool, str, Optional[str]]":
    """Generate an image from a text prompt. On Vertex this prefers **Imagen 4**
    (dedicated, higher quality) and falls back to the Gemini flash-image model
    (generateContent, TEXT+IMAGE) on failure or off-Vertex. Returns
    (ok, message, data_url)."""
    prompt = (prompt or "").strip()
    if not prompt:
        return False, "Tell me what image to create.", None
    # Prefer Imagen 4 when Vertex/ADC is active; on failure (e.g. safety block)
    # fall through to the Gemini flash-image path below.
    if vertex_auth.enabled():
        ok, msg, data_url = await vertex_imagen_generate(prompt)
        if ok:
            return ok, msg, data_url
        print(f"[Imagen] {msg} — falling back to {model}.", flush=True)
    if not api_key and not vertex_auth.enabled():
        return False, "No Gemini credentials configured — can't generate images.", None
    params, headers = await _request_auth(api_key)
    headers["Content-Type"] = "application/json"
    url = _endpoint(model, "generateContent")
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    try:
        resp = await _client().post(url, params=params, headers=headers, json=body)
    except Exception as exc:  # noqa: BLE001
        return False, f"Image generation failed: {exc}", None
    if resp.status_code != 200:
        print(f"[ImageGen] HTTP {resp.status_code}: {resp.text[:300]}", flush=True)
        # Surface the API's real reason (paid-plan, safety block, etc.).
        try:
            detail = (resp.json().get("error") or {}).get("message", "").strip()
        except Exception:  # noqa: BLE001
            detail = ""
        return False, f"Image generation failed: {detail}" if detail else _http_error_message(resp.status_code), None
    data = resp.json()
    data_url = _extract_image(data)
    if not data_url:
        return False, "The model didn't return an image for that prompt.", None
    return True, _extract_text(data) or f"Here's the image for: {prompt}", data_url
