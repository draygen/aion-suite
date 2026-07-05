"""
Fleet Topology — Aion Flask Blueprint.

Serves the /fleet page and GET /api/fleet/topology, a single aggregated view of
every machine/service in the AION + mcpbuilder stack: nodes, the edges between
them, and per-node health.

Health sources:
  - infra services (Ollama, Postgres): direct HTTP / TCP probes from here
  - fleet machines + their agents (claude/codex/agy on wsl/draydev/ec2): the
    read-only fleet gateway (mcpbuilder `npm run gateway`, default :5100)
  - Kali container: the existing /api/kali sensor health, best-effort

Read-only: this blueprint never triggers remote execution — it only reports.
"""
from __future__ import annotations

import os
import socket
import time
import urllib.parse
import logging

import httpx
from flask import Blueprint, jsonify, redirect, render_template, request

from config import CONFIG
from auth import get_user_by_token

log = logging.getLogger("aion.fleet")

fleet_bp = Blueprint("fleet", __name__)

# Short cache so polling the page is cheap; the slow SSH probing already lives
# behind the gateway's own background-refreshed cache.
_CACHE_TTL_SEC = 8
_cache: dict[str, object] = {"at": 0.0, "data": None}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _gateway_url() -> str:
    return str(CONFIG.get("fleet_gateway_url", os.getenv("FLEET_GATEWAY_URL", "http://127.0.0.1:5100")))


def _ollama_url() -> str:
    return str(CONFIG.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")


def _postgres_host_port() -> tuple[str, int]:
    dsn = str(CONFIG.get("DATABASE_URL", "postgresql://127.0.0.1:5432/aion_db"))
    try:
        parsed = urllib.parse.urlparse(dsn)
        return (parsed.hostname or "127.0.0.1", int(parsed.port or 5432))
    except Exception:
        return ("127.0.0.1", 5432)


# ---------------------------------------------------------------------------
# Low-level probes
# ---------------------------------------------------------------------------

def _http_ok(url: str, timeout: float = 4.0) -> tuple[bool, str]:
    try:
        r = httpx.get(url, timeout=timeout)
        return (r.status_code < 500, f"HTTP {r.status_code}")
    except Exception as exc:
        return (False, f"{type(exc).__name__}")


def _tcp_ok(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (True, f"tcp {host}:{port} open")
    except Exception as exc:
        return (False, f"{type(exc).__name__}")


def _gateway_status() -> dict | None:
    """Fetch structured fleet checks from the read-only gateway (fast; cached there)."""
    try:
        r = httpx.get(f"{_gateway_url()}/fleet/status", timeout=4.0)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.info("fleet gateway unreachable: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Topology assembly
# ---------------------------------------------------------------------------

# machine id -> display metadata (host shown in the UI)
_MACHINES = {
    "wsl": {"label": "wsl (local)", "host": "127.0.0.1"},
    "draydev": {"label": "draydev", "host": "192.168.0.200"},
    "ec2": {"label": "ec2 (prod)", "host": "3.238.156.148"},
}


def _agent_rollup(gw: dict | None) -> dict[str, list[dict]]:
    """Group the gateway's flat checks by machine -> [{agent, ok, detail}]."""
    by_machine: dict[str, list[dict]] = {m: [] for m in _MACHINES}
    if not gw:
        return by_machine
    for chk in gw.get("checks", []):
        m = chk.get("machine")
        if m in by_machine:
            by_machine[m].append(
                {"agent": chk.get("agent"), "ok": bool(chk.get("ok")), "detail": chk.get("detail", "")}
            )
    return by_machine


def _build_topology() -> dict:
    ollama_ok, ollama_detail = _http_ok(f"{_ollama_url()}/api/tags")
    pg_host, pg_port = _postgres_host_port()
    pg_ok, pg_detail = _tcp_ok(pg_host, pg_port)

    gw = _gateway_status()
    gw_reachable = gw is not None
    agents = _agent_rollup(gw)

    def machine_health(mid: str) -> tuple[str, list[dict]]:
        checks = agents.get(mid, [])
        if not gw_reachable:
            return ("unknown", checks)
        if not checks:
            return ("unknown", checks)
        up = sum(1 for c in checks if c["ok"])
        if up == len(checks):
            return ("up", checks)
        if up == 0:
            return ("down", checks)
        return ("degraded", checks)

    nodes: list[dict] = []

    nodes.append({
        "id": "mcp-client", "label": "MCP Client", "kind": "client",
        "sub": "Claude Desktop / Codex", "status": "external",
        "detail": "Spawns mcpbuilder over stdio on demand.",
    })

    nodes.append({
        "id": "mcpbuilder", "label": "mcpbuilder", "kind": "hub",
        "sub": "aion-mcp · stdio + gateway",
        "status": "up" if gw_reachable else "down",
        "detail": ("Fleet gateway reachable at " + _gateway_url()) if gw_reachable
                  else "Fleet gateway offline — start it with `npm run gateway` in mcpbuilder.",
    })

    nodes.append({
        "id": "aion-core", "label": "AION Core", "kind": "service",
        "sub": "Flask :5000 · wsl", "status": "up",
        "detail": "This server. Serving the topology you're viewing.",
    })

    nodes.append({
        "id": "ollama", "label": "Ollama", "kind": "infra",
        "sub": "GPU · 192.168.0.114",
        "status": "up" if ollama_ok else "down",
        "detail": f"{_ollama_url()} — {ollama_detail}",
    })

    nodes.append({
        "id": "postgres", "label": "Postgres", "kind": "infra",
        "sub": "aion_db",
        "status": "up" if pg_ok else "down",
        "detail": pg_detail,
    })

    for mid, meta in _MACHINES.items():
        status, checks = machine_health(mid)
        up = sum(1 for c in checks if c["ok"])
        total = len(checks)
        summary = f"{up}/{total} agents" if total else ("gateway offline" if not gw_reachable else "no data")
        nodes.append({
            "id": mid, "label": meta["label"], "kind": "machine",
            "sub": meta["host"], "status": status,
            "detail": summary,
            "agents": checks,
        })

    # Kali container lives on draydev — best-effort, non-blocking-ish.
    kali_ok, kali_detail = _http_ok(f"http://{_MACHINES['draydev']['host']}:7000/health", timeout=3.0)
    nodes.append({
        "id": "kali", "label": "Kali container", "kind": "container",
        "sub": "sensor · draydev",
        "status": "up" if kali_ok else "down",
        "detail": kali_detail,
    })

    edges = [
        {"from": "mcp-client", "to": "mcpbuilder", "kind": "stdio"},
        {"from": "mcpbuilder", "to": "aion-core", "kind": "http"},
        {"from": "mcpbuilder", "to": "wsl", "kind": "ssh"},
        {"from": "mcpbuilder", "to": "draydev", "kind": "ssh"},
        {"from": "mcpbuilder", "to": "ec2", "kind": "ssh"},
        {"from": "aion-core", "to": "ollama", "kind": "http"},
        {"from": "aion-core", "to": "postgres", "kind": "sql"},
        {"from": "draydev", "to": "kali", "kind": "docker"},
    ]

    return {
        "nodes": nodes,
        "edges": edges,
        "gateway_reachable": gw_reachable,
        "gateway_updated_at": (gw or {}).get("updated_at"),
        "gateway_refreshing": bool((gw or {}).get("refreshing")),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@fleet_bp.route("/fleet")
def fleet_page():
    # Mirror /admin: require a session cookie to view the page at all.
    token = request.cookies.get("aion_token")
    if not token or not get_user_by_token(token):
        return redirect("/")
    return render_template("fleet.html")


@fleet_bp.route("/api/fleet/topology")
def api_fleet_topology():
    token = request.cookies.get("aion_token")
    if not token or not get_user_by_token(token):
        return jsonify({"error": "unauthorized"}), 401

    now = time.time()
    if _cache["data"] is not None and (now - float(_cache["at"])) < _CACHE_TTL_SEC:
        return jsonify(_cache["data"])

    data = _build_topology()
    _cache["at"] = now
    _cache["data"] = data
    return jsonify(data)
