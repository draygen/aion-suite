"""
Kali Pentest Routes — Aion Flask Blueprint
/api/kali/* endpoints for dispatching tasks to Kali Sensor Agent on Draydev.
"""
from __future__ import annotations

import uuid
import logging
from datetime import datetime, timezone

import httpx
from flask import Blueprint, request, jsonify

from config import CONFIG

log = logging.getLogger("aion.kali")

kali_bp = Blueprint("kali", __name__, url_prefix="/api/kali")

# In-memory result cache (keyed by task_id)
_results: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sensor_url() -> str:
    return str(CONFIG.get("kali_sensor_url", "http://192.168.0.200:7000"))

def _token() -> str:
    return str(CONFIG.get("kali_sensor_token", "kali-sensor-draydev-2026"))

def _dispatch(task_id: str, payload: dict):
    """POST task to Kali Sensor Agent."""
    try:
        r = httpx.post(
            f"{_sensor_url()}/task",
            json=payload,
            headers={"X-Agent-Token": _token()},
            timeout=10,
        )
        r.raise_for_status()
        return jsonify({"task_id": task_id, "accepted": r.json()})
    except Exception as exc:
        log.warning("Sensor dispatch failed: %s", exc)
        return jsonify({"task_id": task_id, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@kali_bp.get("/health")
def kali_health():
    """Check connectivity to Kali Sensor Agent."""
    try:
        r = httpx.get(f"{_sensor_url()}/health", timeout=5)
        return jsonify({"status": "ok", "sensor": r.json()})
    except Exception as exc:
        return jsonify({"status": "unreachable", "error": str(exc)}), 503


@kali_bp.post("/scan")
def kali_scan():
    """Quick nmap scan. Body: { target, ports? }"""
    body = request.get_json(silent=True) or {}
    target = body.get("target", "").strip()
    if not target:
        return jsonify({"error": "target required"}), 400
    ports = body.get("ports", "22,80,443,8080,8443")
    task_id = f"scan-{uuid.uuid4().hex[:8]}"
    payload = {
        "task_id": task_id,
        "action": "scan",
        "target": target,
        "commands": [f"nmap -sV -p {ports} {target}"],
    }
    return _dispatch(task_id, payload)


@kali_bp.post("/task")
def kali_task():
    """Dispatch arbitrary task. Body: { action, target?, commands[] }"""
    body = request.get_json(silent=True) or {}
    commands = body.get("commands", [])
    if not commands:
        return jsonify({"error": "commands[] required"}), 400
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    payload = {
        "task_id": task_id,
        "action": body.get("action", "exec"),
        "target": body.get("target"),
        "commands": commands,
    }
    return _dispatch(task_id, payload)


@kali_bp.get("/results/<task_id>")
def kali_results(task_id: str):
    """Retrieve results for a previously dispatched task."""
    result = _results.get(task_id)
    if not result:
        return jsonify({"status": "pending", "task_id": task_id}), 202
    return jsonify(result)


@kali_bp.post("/results")
def receive_results():
    """Endpoint for Kali Sensor Agent to POST results back to Aion."""
    data = request.get_json(silent=True) or {}
    task_id = data.get("task_id") or (data.get("data") or {}).get("task_id")
    if task_id:
        _results[task_id] = {**data, "received_at": datetime.now(timezone.utc).isoformat()}
        log.info("Received kali results for task %s", task_id)
    return jsonify({"ok": True})
