import json

import requests

from aion_logging import get_logger
from config import CONFIG
from ollama_endpoints import ollama_base_urls, ollama_display_target

# Singleton OpenAI client — avoids re-instantiating (connection pool setup) on every request.
_openai_client = None
logger = get_logger("llm")


class OllamaResponseError(RuntimeError):
    """Ollama was reachable but returned a non-2xx HTTP status.

    Distinct from a connection failure: the server answered, so the request
    itself is at fault (bad payload, unknown model, template error). Callers
    should surface this rather than treating it as an unreachable backend.
    """

    def __init__(self, base_url: str, status_code: int, model: str, body: str):
        self.base_url = base_url
        self.status_code = status_code
        self.model = model
        self.body = body
        super().__init__(
            f"Ollama at {base_url} returned HTTP {status_code} for model {model}: {body[:600]}"
        )


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        try:
            import openai
            api_key = CONFIG.get("openai_api_key")
            if not api_key or api_key.startswith("sk-xxxx"):
                raise RuntimeError("OpenAI API key not configured.")
            _openai_client = openai.OpenAI(api_key=api_key)
        except ImportError as e:
            raise RuntimeError("openai package not installed.") from e
    return _openai_client


def ask_llm_chat(messages: list) -> str:
    """Send a multi-turn conversation to the LLM.

    Routing:
      backend=ollama  → local Ollama (GPU), falls back to OpenAI on failure
      backend=openai  → OpenAI API, falls back to Ollama on failure
    """
    backend = CONFIG.get('backend', 'ollama')
    if backend == 'ollama':
        try:
            return _ollama_chat(messages)
        except OllamaResponseError:
            # The server was reachable and returned an error (e.g. a 400 from a
            # bad payload). Falling back to OpenAI would just silently paper over
            # a real request bug, so surface it instead.
            raise
        except Exception as e:
            openai_key = CONFIG.get("openai_api_key", "")
            if openai_key and not openai_key.startswith("sk-xxxx"):
                logger.warning("Ollama failed, falling back to OpenAI: %s", e)
                return _openai_chat(messages)
            raise
    elif backend == 'openai':
        try:
            return _openai_chat(messages)
        except Exception as e:
            logger.warning("OpenAI failed, falling back to Ollama: %s", e)
            return _ollama_chat(messages)
    else:
        raise ValueError(f"Unsupported backend: {backend}")


def ask_llm(prompt: str) -> str:
    """Legacy single-turn interface. Wraps ask_llm_chat."""
    return ask_llm_chat([{"role": "user", "content": prompt}])


def _ollama_payload(messages: list, *, stream: bool) -> dict:
    payload = {
        "model": CONFIG.get('model', 'brian-mistral'),
        "messages": messages,
        "stream": stream,
        # Keep the model resident so idle gaps don't trigger a multi-second
        # reload on the next request (the biggest latency outlier).
        "keep_alive": CONFIG.get("llm_keep_alive", "30m"),
        # qwen3.5 is a reasoning model: without think:false it emits its
        # answer into message.thinking and returns empty message.content.
        # Ignored by non-thinking models, so it's safe to always send.
        "think": bool(CONFIG.get("llm_think", False)),
    }
    options = CONFIG.get("llm_options")
    if options:
        # App-level options override the Modelfile so response length and
        # context window can be tuned without rebuilding the model.
        payload["options"] = options
    return payload


def stream_llm_chat(messages: list):
    """Yield content tokens from Ollama as they arrive.

    Same endpoint selection and payload as `_ollama_chat`, but streaming. There
    is no OpenAI fallback: by the time the first token is on the wire the
    response has already started, so a mid-stream swap would produce a spliced
    answer. Callers should surface the error instead.
    """
    payload = _ollama_payload(messages, stream=True)
    model = payload["model"]
    attempted = []
    last_error = None
    for base_url in ollama_base_urls():
        attempted.append(base_url)
        try:
            with requests.post(f"{base_url}/api/chat", json=payload,
                               stream=True, timeout=300) as resp:
                if resp.status_code >= 400:
                    raise OllamaResponseError(base_url, resp.status_code, model,
                                              resp.text.strip())
                for line in resp.iter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    token = (data.get("message") or {}).get("content", "")
                    if token:
                        yield token
                    if data.get("done"):
                        return
            return
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            continue

    attempted_text = ", ".join(attempted) if attempted else ollama_display_target()
    raise RuntimeError(
        f"Ollama not reachable for model {model}. Tried: {attempted_text}. Last error: {last_error}"
    )


def _ollama_chat(messages: list) -> str:
    payload = _ollama_payload(messages, stream=False)
    model = payload["model"]
    attempted = []
    last_error = None

    for base_url in ollama_base_urls():
        attempted.append(base_url)
        try:
            resp = requests.post(
                f"{base_url}/api/chat",
                json=payload,
                timeout=300,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            # Genuinely unreachable / slow address — try the next candidate.
            last_error = exc
            continue
        # The host answered. A non-2xx is a real server-side error (bad payload,
        # unknown model, template failure) that will fail identically on every
        # address, so surface it immediately instead of masking it behind a later
        # address's connection error.
        if resp.status_code >= 400:
            raise OllamaResponseError(base_url, resp.status_code, model, resp.text.strip())
        return resp.json()["message"]["content"]

    attempted_text = ", ".join(attempted) if attempted else ollama_display_target()
    raise RuntimeError(
        f"Ollama not reachable for model {model}. Tried: {attempted_text}. Last error: {last_error}"
    )


def _openai_chat(messages: list) -> str:
    client = _get_openai_client()
    response = client.chat.completions.create(
        model=CONFIG.get("openai_model", "gpt-4o"),
        messages=messages,
    )
    return response.choices[0].message.content

def _strix_chat(messages: list) -> str:
    """Strix backend: Bridges to Gemini 1.5 Pro for high-reasoning security planning."""
    import google.generativeai as genai
    api_key = CONFIG.get("google_api_key")
    if not api_key:
        raise RuntimeError("Strix (Gemini) API key not configured in config_local.py.")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-pro")
    
    history = []
    for m in messages[:-1]:
        role = "user" if m["role"] == "user" else "model"
        history.append({"role": role, "parts": [m["content"]]})
    
    chat = model.start_chat(history=history)
    response = chat.send_message(messages[-1]["content"])
    return response.text
