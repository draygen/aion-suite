import os
from urllib.parse import urlparse, urlunparse

from config import CONFIG


def _normalize_base_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme or "http", parsed.netloc, path, "", "", "")).rstrip("/")


def _wsl_gateway_host() -> str:
    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("nameserver "):
                    return line.split()[1].strip()
    except OSError:
        return ""
    return ""


def ollama_base_urls() -> list[str]:
    configured = _normalize_base_url(CONFIG.get("OLLAMA_BASE_URL", ""))
    parsed = urlparse(configured) if configured else None
    scheme = parsed.scheme if parsed and parsed.scheme else "http"
    port = parsed.port if parsed and parsed.port else 11434

    candidates = []

    def add(host: str) -> None:
        if not host or host == "0.0.0.0":
            return
        url = f"{scheme}://{host}:{port}"
        if url not in candidates:
            candidates.append(url)

    if configured:
        host = parsed.hostname or ""
        if host and host != "0.0.0.0":
            candidates.append(configured)
        add(host)
    add("host.docker.internal")
    add("localhost")
    add("127.0.0.1")
    add(_wsl_gateway_host())
    return candidates


def ollama_display_target() -> str:
    configured = _normalize_base_url(CONFIG.get("OLLAMA_BASE_URL", ""))
    return configured or "http://localhost:11434"
