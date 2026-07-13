import requests

from aion_logging import get_logger
from config import CONFIG
from ollama_endpoints import ollama_base_urls, ollama_display_target

# Singleton OpenAI client — avoids re-instantiating (connection pool setup) on every request.
_openai_client = None
logger = get_logger("llm")


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


def _ollama_chat(messages: list) -> str:
    model = CONFIG.get('model', 'brian-mistral')
    attempted = []
    last_error = None
    for base_url in ollama_base_urls():
        attempted.append(base_url)
        try:
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
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
            resp = requests.post(
                f"{base_url}/api/chat",
                json=payload,
                timeout=300,
            )
            resp.raise_for_status()
            return resp.json()["message"]["content"]
        except requests.exceptions.ConnectionError as exc:
            last_error = exc
            continue
        except Exception as exc:
            last_error = exc
            continue

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
