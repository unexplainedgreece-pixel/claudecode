"""
AI providers behind one small interface, so every feature can use whichever
service the user has a key for.

    text(prompt, user_id)                       -> str      (captions, hashtags, ideas)
    vision(prompt, images, user_id)             -> str      (describe photos / video frames)
    image(prompt, user_id)                      -> bytes    (PNG/JPEG bytes)

Providers (set TEXT_PROVIDER / VISION_PROVIDER / IMAGE_PROVIDER in .env):
    gemini      Google Gemini API. Free tier covers text and image/video input
                (about 1,500 requests a day on 2.5 Flash at the time of writing).
                Key: GEMINI_API_KEY from https://aistudio.google.com/apikey
    openai      OpenAI. Paid. Key: OPENAI_API_KEY
    groq        Groq. Free tier (about 1,000 requests a day on Llama 3.3 70B at the
                time of writing), very fast. Text only. Key: GROQ_API_KEY from
                https://console.groq.com/keys
    cloudflare  Cloudflare Workers AI image generation (Flux Schnell). Small free
                daily allowance. Keys: R2_ACCOUNT_ID (same account id) and
                CF_API_TOKEN with the "Workers AI" permission.

Default order when a provider is not chosen: gemini if the user has a key,
else openai. All errors are raised as AIError with a user-safe message.
"""

import base64
import logging
import os
from pathlib import Path

import time

import requests

from src.security.secrets import ConfigError, get_secret

log = logging.getLogger("ai")

# Google renames and retires models every few months (for new accounts first). These are
# the starting names; when Google answers "no longer available" the tool asks for the model
# list and switches to the current one by itself (see _replacement_model).
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-3.8-flash")
GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-3.8-flash")
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_MODEL_SWITCH: dict[str, str] = {}      # requested name -> working name, learned at runtime
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "75"))   # seconds; a silent Google counts as busy
BUSY_RETRIES = 3                         # Google's free tier answers 503 "high demand" now and then
BUSY_WAIT_SECONDS = (4, 10, 20)



OPENAI_TEXT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
GROQ_TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
_GROQ_MODEL: dict[str, str] = {}        # learned replacement when Groq retires the default name
# xAI Grok: OpenAI-compatible REST. Model names change often, so the current ones are read
# from xAI's model list unless pinned with GROK_TEXT_MODEL / GROK_VISION_MODEL / GROK_IMAGE_MODEL.
GROK_URL = "https://api.x.ai/v1"
_GROK_MODELS: dict[str, str] = {}
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini")
CF_IMAGE_MODEL = os.getenv("CF_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")
CF_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
TIMEOUT = 120


class AIError(Exception):
    """A provider refused, is out of quota, or is not configured. Message is user-safe."""


class BusyError(AIError):
    """The provider is overloaded right now; worth trying again a little later."""


class QuotaError(AIError):
    """Rate limit or exhausted free tier. Worth retrying later."""


def _has_key(name: str, user_id) -> bool:
    try:
        get_secret(name, user_id)
        return True
    except ConfigError:
        return False


def pick(kind: str, user_id: int | None) -> str:
    """Which provider to use for 'text', 'vision' or 'image' for this user."""
    forced = (os.getenv(f"{kind.upper()}_PROVIDER") or "").strip().lower()
    if forced:
        return forced
    if kind == "image":
        if _has_key("CF_API_TOKEN", user_id):
            return "cloudflare"
        if _has_key("GEMINI_API_KEY", user_id):
            return "gemini"
        return "grok" if _has_key("XAI_API_KEY", user_id) else "openai"
    if kind == "text" and not _has_key("GEMINI_API_KEY", user_id) and _has_key("GROQ_API_KEY", user_id):
        return "groq"
    if _has_key("GEMINI_API_KEY", user_id):
        return "gemini"
    if _has_key("XAI_API_KEY", user_id):
        return "grok"
    return "openai"


def _mime(path: Path) -> str:
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")


# ---------------------------------------------------------------------------
# Gemini (REST, no SDK)
# ---------------------------------------------------------------------------

def _model_version(name: str) -> float:
    import re
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def gemini_models(user_id) -> list[str]:
    """Names of the models this key may call with generateContent."""
    key = get_secret("GEMINI_API_KEY", user_id)
    try:
        resp = requests.get(GEMINI_LIST_URL, params={"key": key, "pageSize": 200}, timeout=TIMEOUT)
        models = resp.json().get("models") or []
    except (requests.RequestException, ValueError, AttributeError) as exc:
        raise AIError(f"Could not list Gemini models: {exc}") from exc
    return [m.get("name", "").replace("models/", "") for m in models
            if "generateContent" in (m.get("supportedGenerationMethods") or [])]


def _replacement_model(model: str, message: str, user_id) -> str | None:
    """When Google says a model is gone, find the current one: the name Google suggests
    in its own message, otherwise the newest model of the same kind from the list."""
    import re
    # Google's message names the dead model first, then the one to use instead.
    suggested = next((n for n in re.findall(r"models/([\w.\-]+)", message or "") if n != model), None)
    try:
        available = gemini_models(user_id)
    except AIError:
        available = []
    if suggested and (not available or suggested in available):
        return suggested
    wants_image = "image" in model
    pool = [n for n in available if ("image" in n) == wants_image and "flash" in n
            and not any(x in n for x in ("tts", "live", "audio", "embedding", "lite", "thinking", "exp"))]
    pool = pool or [n for n in available if ("image" in n) == wants_image]
    if not pool:
        return None
    pool.sort(key=lambda n: (_model_version(n), "preview" not in n), reverse=True)
    return pool[0]


_BUSY_ATTEMPTS: dict[int, int] = {}


def _gemini_call(model: str, parts: list, user_id, generation_config: dict | None = None, _retry: bool = True) -> dict:
    key = get_secret("GEMINI_API_KEY", user_id)
    model = _MODEL_SWITCH.get(model, model)
    body = {"contents": [{"parts": parts}]}
    if generation_config:
        body["generationConfig"] = generation_config
    try:
        resp = requests.post(GEMINI_URL.format(model=model), params={"key": key}, json=body, timeout=GEMINI_TIMEOUT)
    except (requests.Timeout, requests.ConnectionError) as exc:
        # Silence from Google is the same as "high demand": let a backup writer take over.
        raise BusyError(f"Gemini did not answer in time ({exc.__class__.__name__}); trying another writer.") from exc
    except requests.RequestException as exc:
        raise AIError(f"Could not reach Gemini: {exc}") from exc
    try:
        payload = resp.json()
    except ValueError:
        raise AIError(f"Gemini returned something unreadable (HTTP {resp.status_code}).")
    if resp.status_code == 429:
        raise QuotaError("Gemini free quota is used up for now. It resets daily; try again later.")
    if resp.status_code in (500, 502, 503, 504):
        attempt = (generation_config or {}).pop("_attempt", 0) if False else _BUSY_ATTEMPTS.get(id(parts), 0)
        if attempt < BUSY_RETRIES:
            _BUSY_ATTEMPTS[id(parts)] = attempt + 1
            wait = BUSY_WAIT_SECONDS[min(attempt, len(BUSY_WAIT_SECONDS) - 1)]
            log.warning("Gemini is busy (HTTP %s); trying again in %ss (%s/%s)", resp.status_code, wait, attempt + 1, BUSY_RETRIES)
            time.sleep(wait)
            return _gemini_call(model, parts, user_id, generation_config, _retry)
        _BUSY_ATTEMPTS.pop(id(parts), None)
        raise BusyError("Gemini is overloaded right now (Google's free tier does this at peak hours). "
                        "The post will be tried again on a later cycle.")
    _BUSY_ATTEMPTS.pop(id(parts), None)
    if resp.status_code >= 400 or "error" in payload:
        msg = (payload.get("error") or {}).get("message", payload) if isinstance(payload, dict) else str(payload)
        if resp.status_code in (401, 403):
            raise AIError(f"Gemini rejected the API key: {msg}")
        if resp.status_code == 404 and _retry:
            replacement = _replacement_model(model, str(msg), user_id)
            if replacement and replacement != model:
                log.warning("Gemini model %s is gone; switching to %s", model, replacement)
                _MODEL_SWITCH[model] = replacement
                for alias, target in list(_MODEL_SWITCH.items()):   # keep the chain flat
                    if target == model:
                        _MODEL_SWITCH[alias] = replacement
                return _gemini_call(replacement, parts, user_id, generation_config, _retry=False)
        raise AIError(f"Gemini error (HTTP {resp.status_code}): {msg}")
    return payload


def _gemini_parts_text(payload: dict) -> str:
    try:
        parts = payload["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        reason = (payload.get("candidates") or [{}])[0].get("finishReason") if payload.get("candidates") else payload.get("promptFeedback")
        raise AIError(f"Gemini returned no text ({reason or 'empty answer'}).")
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not text.strip():
        raise AIError("Gemini returned an empty answer.")
    return text.strip()


def gemini_text(prompt: str, user_id, json_mode: bool = False) -> str:
    cfg = {"responseMimeType": "application/json"} if json_mode else None
    return _gemini_parts_text(_gemini_call(GEMINI_TEXT_MODEL, [{"text": prompt}], user_id, cfg))


def gemini_vision(prompt: str, images: list[Path], user_id, json_mode: bool = False) -> str:
    parts = [{"text": prompt}]
    for path in images:
        parts.append({"inline_data": {"mime_type": _mime(path),
                                      "data": base64.b64encode(path.read_bytes()).decode()}})
    cfg = {"responseMimeType": "application/json"} if json_mode else None
    return _gemini_parts_text(_gemini_call(GEMINI_VISION_MODEL, parts, user_id, cfg))


def gemini_image(prompt: str, user_id) -> bytes:
    payload = _gemini_call(GEMINI_IMAGE_MODEL, [{"text": prompt}], user_id,
                           {"responseModalities": ["IMAGE"]})
    try:
        for part in payload["candidates"][0]["content"]["parts"]:
            data = part.get("inlineData") or part.get("inline_data")
            if data and data.get("data"):
                return base64.b64decode(data["data"])
    except (KeyError, IndexError, TypeError):
        pass
    raise AIError("Gemini returned no image. Image generation may not be on the free tier for this model.")


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

def _openai_client(user_id):
    import openai
    return openai.OpenAI(api_key=get_secret("OPENAI_API_KEY", user_id)), openai


def _openai_wrap(fn):
    try:
        return fn()
    except Exception as exc:  # map SDK errors without importing them at module import time
        name = type(exc).__name__
        msg = getattr(exc, "message", None) or str(exc)
        if name == "RateLimitError":
            raise QuotaError(f"OpenAI quota or rate limit: {msg}") from exc
        if name == "AuthenticationError":
            raise AIError("OpenAI rejected the API key. Check OPENAI_API_KEY.") from exc
        if name in ("BadRequestError", "APIConnectionError", "APIError", "APIStatusError"):
            raise AIError(f"OpenAI error: {msg}") from exc
        raise


def openai_text(prompt: str, user_id, json_mode: bool = False) -> str:
    client, _ = _openai_client(user_id)
    kwargs = {"model": OPENAI_TEXT_MODEL, "messages": [{"role": "user", "content": prompt}]}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _openai_wrap(lambda: client.chat.completions.create(**kwargs))
    return (resp.choices[0].message.content or "").strip()


def openai_vision(prompt: str, images: list[Path], user_id, json_mode: bool = False) -> str:
    client, _ = _openai_client(user_id)
    content = [{"type": "text", "text": prompt}]
    for path in images:
        b64 = base64.b64encode(path.read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:{_mime(path)};base64,{b64}"}})
    kwargs = {"model": OPENAI_VISION_MODEL, "messages": [{"role": "user", "content": content}]}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _openai_wrap(lambda: client.chat.completions.create(**kwargs))
    return (resp.choices[0].message.content or "").strip()


def openai_image(prompt: str, user_id) -> bytes:
    client, _ = _openai_client(user_id)
    model = os.getenv("OPENAI_IMAGE_MODEL", "dall-e-3")
    resp = _openai_wrap(lambda: client.images.generate(model=model, prompt=prompt, n=1,
                                                        size=os.getenv("OPENAI_IMAGE_SIZE", "1024x1024")))
    item = resp.data[0]
    if getattr(item, "b64_json", None):
        return base64.b64decode(item.b64_json)
    if getattr(item, "url", None):
        r = requests.get(item.url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.content
    raise AIError("OpenAI returned no image data.")


# ---------------------------------------------------------------------------
# xAI Grok (text, vision, images; OpenAI-compatible REST)
# ---------------------------------------------------------------------------

def _grok_request(method: str, path: str, user_id, **kwargs) -> dict:
    key = get_secret("XAI_API_KEY", user_id)
    try:
        resp = requests.request(method, f"{GROK_URL}{path}", headers={"Authorization": f"Bearer {key}"},
                                timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise AIError(f"Could not reach xAI: {exc}") from exc
    try:
        payload = resp.json()
    except ValueError:
        raise AIError(f"xAI returned something unreadable (HTTP {resp.status_code}).")
    if resp.status_code == 429:
        raise QuotaError("xAI rate limit or spending limit reached. Check console.x.ai.")
    if resp.status_code in (500, 502, 503, 504):
        raise BusyError("xAI is overloaded right now; the post will be tried again later.")
    if resp.status_code >= 400 or (isinstance(payload, dict) and payload.get("error")):
        err = payload.get("error") if isinstance(payload, dict) else payload
        msg = err.get("message", err) if isinstance(err, dict) else err
        if resp.status_code in (401, 403):
            raise AIError(f"xAI rejected the API key: {msg}")
        if resp.status_code == 402:
            raise QuotaError(f"xAI has no credits left: {msg}. Top up at console.x.ai.")
        raise AIError(f"xAI error (HTTP {resp.status_code}): {msg}")
    return payload


def grok_models(user_id) -> list[str]:
    data = _grok_request("GET", "/models", user_id).get("data") or []
    return [m.get("id", "") for m in data if m.get("id")]


def grok_model(kind: str, user_id) -> str:
    """The model to use for 'text', 'vision' or 'image': the pinned env name, else the
    newest suitable one from xAI's list (cheap 'fast'/'mini' variants first for text)."""
    pinned = (os.getenv(f"GROK_{kind.upper()}_MODEL") or "").strip()
    if pinned:
        return pinned
    if kind in _GROK_MODELS:
        return _GROK_MODELS[kind]
    try:
        names = grok_models(user_id)
    except AIError:
        names = []
    if kind == "image":
        pool = [n for n in names if "image" in n and "vision" not in n]
        fallback = "grok-2-image"
    else:
        pool = [n for n in names if n.startswith("grok") and not any(x in n for x in ("image", "vision", "code", "beta"))]
        fallback = "grok-4"
    if kind == "text":
        cheap = [n for n in pool if "fast" in n or "mini" in n]
        pool = cheap or pool
    elif kind == "vision":                     # photo description: the full model reads pictures best
        full = [n for n in pool if "fast" not in n and "mini" not in n]
        pool = full or pool
    pool.sort(key=lambda n: (_model_version(n.replace("grok", "gemini", 1)), -len(n)), reverse=True)
    _GROK_MODELS[kind] = pool[0] if pool else fallback
    return _GROK_MODELS[kind]


def _grok_chat(model: str, content, user_id, json_mode: bool) -> str:
    body = {"model": model, "messages": [{"role": "user", "content": content}], "temperature": 0.8}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    payload = _grok_request("POST", "/chat/completions", user_id, json=body)
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AIError(f"xAI returned no text: {payload}")
    if not (text or "").strip():
        raise AIError("xAI returned an empty answer.")
    return text.strip()


def grok_text(prompt: str, user_id, json_mode: bool = False) -> str:
    return _grok_chat(grok_model("text", user_id), prompt, user_id, json_mode)


def grok_vision(prompt: str, images: list[Path], user_id, json_mode: bool = False) -> str:
    content = [{"type": "text", "text": prompt}]
    for path in images:
        b64 = base64.b64encode(path.read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:{_mime(path)};base64,{b64}"}})
    return _grok_chat(grok_model("vision", user_id), content, user_id, json_mode)


def grok_image(prompt: str, user_id) -> bytes:
    payload = _grok_request("POST", "/images/generations", user_id,
                            json={"model": grok_model("image", user_id), "prompt": prompt, "n": 1,
                                  "response_format": "b64_json"})
    item = (payload.get("data") or [{}])[0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        r = requests.get(item["url"], timeout=TIMEOUT)
        r.raise_for_status()
        return r.content
    raise AIError(f"xAI returned no image data: {payload}")


# ---------------------------------------------------------------------------
# Groq (text, OpenAI-compatible REST)
# ---------------------------------------------------------------------------

def groq_models(user_id) -> list[str]:
    key = get_secret("GROQ_API_KEY", user_id)
    try:
        resp = requests.get(GROQ_MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
        return [m.get("id", "") for m in (resp.json().get("data") or []) if m.get("id")]
    except (requests.RequestException, ValueError, AttributeError) as exc:
        raise AIError(f"Could not list Groq models: {exc}") from exc


def groq_pick_model(user_id) -> str:
    """A current large chat model from Groq's list (Groq retires names every few months):
    biggest Llama first, then anything else that is not audio/guard/vision."""
    import re
    names = [n for n in groq_models(user_id)
             if not any(x in n.lower() for x in ("whisper", "tts", "guard", "vision", "embed", "audio", "safeguard", "prompt-guard"))]
    if not names:
        raise AIError("Groq lists no chat model for this key.")

    def size(n):
        m = re.search(r"(\d+)b", n.lower())
        return int(m.group(1)) if m else 0
    names.sort(key=lambda n: ("llama" in n.lower(), "versatile" in n or "instruct" in n, size(n)), reverse=True)
    return names[0]


def groq_text(prompt: str, user_id, json_mode: bool = False, _retry: bool = True) -> str:
    key = get_secret("GROQ_API_KEY", user_id)
    model = _GROQ_MODEL.get("text", GROQ_TEXT_MODEL)
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.8}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        resp = requests.post(GROQ_URL, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise AIError(f"Could not reach Groq: {exc}") from exc
    try:
        payload = resp.json()
    except ValueError:
        raise AIError(f"Groq returned something unreadable (HTTP {resp.status_code}).")
    if resp.status_code == 429:
        raise QuotaError("Groq free quota is used up for now. It resets daily.")
    if resp.status_code in (500, 502, 503, 504):
        raise BusyError("Groq is overloaded right now; the post will be tried again later.")
    if resp.status_code >= 400 or "error" in payload:
        msg = (payload.get("error") or {}).get("message", payload)
        if resp.status_code in (401, 403):
            raise AIError(f"Groq rejected the API key: {msg}")
        if resp.status_code in (400, 404) and "model" in str(msg).lower() and _retry:
            replacement = groq_pick_model(user_id)
            if replacement != model:
                log.warning("Groq model %s is gone; switching to %s", model, replacement)
                _GROQ_MODEL["text"] = replacement
                return groq_text(prompt, user_id, json_mode, _retry=False)
        raise AIError(f"Groq error (HTTP {resp.status_code}): {msg}")
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AIError("Groq returned no text.")
    if not (text or "").strip():
        raise AIError("Groq returned an empty answer.")
    return text.strip()


# ---------------------------------------------------------------------------
# Cloudflare Workers AI (image generation)
# ---------------------------------------------------------------------------

def cloudflare_image(prompt: str, user_id) -> bytes:
    account = get_secret("R2_ACCOUNT_ID", user_id)
    token = get_secret("CF_API_TOKEN", user_id)
    try:
        resp = requests.post(CF_URL.format(account=account, model=CF_IMAGE_MODEL),
                             headers={"Authorization": f"Bearer {token}"},
                             json={"prompt": prompt, "steps": 4}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise AIError(f"Could not reach Cloudflare: {exc}") from exc
    if resp.status_code == 429:
        raise QuotaError("Cloudflare Workers AI free allowance is used up for today.")
    ctype = resp.headers.get("content-type", "")
    if ctype.startswith("image/"):
        return resp.content
    try:
        payload = resp.json()
    except ValueError:
        raise AIError(f"Cloudflare returned something unreadable (HTTP {resp.status_code}).")
    if not payload.get("success", resp.status_code < 400):
        errors = payload.get("errors") or [{"message": payload}]
        raise AIError(f"Cloudflare error: {errors[0].get('message', errors[0])}")
    result = payload.get("result") or {}
    data = result.get("image") if isinstance(result, dict) else None
    if not data:
        raise AIError("Cloudflare returned no image data.")
    return base64.b64decode(data)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def text(prompt: str, user_id: int | None = None, json_mode: bool = False, provider: str | None = None) -> str:
    provider = provider or pick("text", user_id)
    if provider == "gemini":
        try:
            return gemini_text(prompt, user_id, json_mode)
        except BusyError:
            for other, fn in (("groq", groq_text), ("grok", grok_text), ("openai", openai_text)):
                if _has_key({"groq": "GROQ_API_KEY", "grok": "XAI_API_KEY", "openai": "OPENAI_API_KEY"}[other], user_id):
                    log.warning("Gemini busy; writing with %s instead", other)
                    return fn(prompt, user_id, json_mode)
            raise
    if provider == "openai":
        return openai_text(prompt, user_id, json_mode)
    if provider == "groq":
        return groq_text(prompt, user_id, json_mode)
    if provider == "grok":
        return grok_text(prompt, user_id, json_mode)
    raise AIError(f"Unknown TEXT_PROVIDER '{provider}'.")


def vision(prompt: str, images: list[Path], user_id: int | None = None, json_mode: bool = False,
           provider: str | None = None) -> str:
    provider = provider or pick("vision", user_id)
    images = [Path(p) for p in images]
    if not images:
        raise AIError("No images to look at.")
    if provider == "gemini":
        return gemini_vision(prompt, images, user_id, json_mode)
    if provider == "openai":
        return openai_vision(prompt, images, user_id, json_mode)
    if provider == "grok":
        return grok_vision(prompt, images, user_id, json_mode)
    raise AIError(f"Unknown VISION_PROVIDER '{provider}'.")


def image(prompt: str, user_id: int | None = None, provider: str | None = None) -> bytes:
    provider = provider or pick("image", user_id)
    if provider == "cloudflare":
        return cloudflare_image(prompt, user_id)
    if provider == "gemini":
        return gemini_image(prompt, user_id)
    if provider == "openai":
        return openai_image(prompt, user_id)
    if provider == "grok":
        return grok_image(prompt, user_id)
    raise AIError(f"Unknown IMAGE_PROVIDER '{provider}'.")
