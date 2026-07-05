"""
Fleet control hook — lets AION chat drive mcpbuilder's fleet from a message.

Recognised commands (must start with `fleet`; anything unrecognised returns None
so normal chat/LLM handling proceeds):

  fleet status                          → machine/agent health (read-only)
  fleet run <machine> <agent>: <task>   → run an agent on a machine
  fleet run <agent> on <machine>: <task>
  fleet review: <task>                  → fan the task to codex + agy
  fleet yes | fleet confirm             → execute the pending run/review
  fleet cancel | fleet no               → discard the pending action
  fleet help                            → this list

Guardrails: read (`status`) is immediate; anything that executes on a machine is
staged and requires an explicit `fleet yes` within a short window. All calls go
through the localhost gateway (never SSH from here), gated by `fleet_control_enabled`.
"""
from __future__ import annotations

import re
import time
import logging

import httpx

from config import CONFIG

log = logging.getLogger("aion.fleet_control")

_MACHINES = ("wsl", "draydev", "ec2")
_AGENTS = ("claude", "codex", "agy")
_PENDING_TTL_SEC = 120

# client_ip -> {"action", "params", "desc", "at"}
_pending: dict[str, dict] = {}

# command patterns
_RE_STATUS = re.compile(r"^fleet\s+(status|health)\s*$", re.I)
_RE_HELP = re.compile(r"^fleet(\s+help)?\s*$", re.I)
_RE_YES = re.compile(r"^fleet\s+(yes|confirm)\s*$", re.I)
_RE_NO = re.compile(r"^fleet\s+(cancel|no)\s*$", re.I)
_RE_RUN_A = re.compile(r"^fleet\s+run\s+(\w+)\s+(\w+)\s*:\s*(.+)$", re.I | re.S)          # run <machine> <agent>: task
_RE_RUN_B = re.compile(r"^fleet\s+run\s+(\w+)\s+on\s+(\w+)\s*:\s*(.+)$", re.I | re.S)     # run <agent> on <machine>: task
_RE_REVIEW = re.compile(r"^fleet\s+review\s*:\s*(.+)$", re.I | re.S)


def _url() -> str:
    return str(CONFIG.get("fleet_gateway_url", "http://127.0.0.1:5100")).rstrip("/")


def _headers() -> dict:
    tok = CONFIG.get("fleet_gateway_token") or ""
    return {"x-fleet-token": tok} if tok else {}


def _help() -> str:
    return (
        "**Fleet control**\n"
        "- `fleet status` — machine & agent health\n"
        "- `fleet run <machine> <agent>: <task>` — e.g. `fleet run draydev codex: check disk usage`\n"
        "- `fleet review: <task>` — fan the task to codex + agy\n"
        "- `fleet yes` / `fleet cancel` — confirm or discard a staged run\n"
        f"Machines: {', '.join(_MACHINES)} · Agents: {', '.join(_AGENTS)}"
    )


def _format_status() -> str:
    try:
        r = httpx.get(f"{_url()}/fleet/status", timeout=5.0)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        return f"⚠ Fleet gateway unreachable at {_url()} ({type(exc).__name__}). Start it with `npm run gateway` in mcpbuilder."

    checks = data.get("checks", [])
    if not checks:
        return "Fleet gateway is up but has no probe data yet — it's checking the machines now; try `fleet status` again in a moment."

    by_machine: dict[str, list] = {m: [] for m in _MACHINES}
    for c in checks:
        by_machine.setdefault(c.get("machine", "?"), []).append(c)
    lines = ["**Fleet status**"]
    for m, cs in by_machine.items():
        if not cs:
            continue
        agents = " ".join(f"{c.get('agent')}{'✓' if c.get('ok') else '✗'}" for c in cs)
        up = sum(1 for c in cs if c.get("ok"))
        lines.append(f"- **{m}** ({up}/{len(cs)}): {agents}")
    when = data.get("updated_at") or "—"
    lines.append(f"_checked {when}_")
    return "\n".join(lines)


def _stage(client_ip: str, action: str, params: dict, desc: str) -> str:
    _pending[client_ip] = {"action": action, "params": params, "desc": desc, "at": time.time()}
    return (
        f"⚠ **Confirm:** {desc}\n"
        f"Reply `fleet yes` within {_PENDING_TTL_SEC // 60} min to execute, or `fleet cancel`."
    )


def _execute(action: str, params: dict) -> str:
    path = "/fleet/run" if action == "run" else "/fleet/review"
    try:
        r = httpx.post(f"{_url()}{path}", json=params, headers=_headers(), timeout=210.0)
        data = r.json()
    except Exception as exc:
        return f"⚠ Fleet execution failed: {type(exc).__name__}: {exc}"
    if r.status_code == 401:
        return "⚠ Gateway rejected the control token. Set FLEET_GATEWAY_TOKEN to the same value on both the gateway and AION."
    if r.status_code == 403:
        return "⚠ Gateway writes are disabled (FLEET_GATEWAY_WRITE=off)."
    if not data.get("ok"):
        return f"⚠ {data.get('output') or data.get('error') or 'unknown error'}"
    return data.get("output", "(no output)")


def handle_fleet_command(message: str, client_ip: str) -> str | None:
    """Return a reply string if `message` is a fleet command, else None."""
    text = (message or "").strip()
    if not text.lower().startswith("fleet"):
        return None
    if not CONFIG.get("fleet_control_enabled", True):
        return None

    # confirm / cancel a staged action
    if _RE_YES.match(text):
        pend = _pending.pop(client_ip, None)
        if not pend:
            return "Nothing staged to confirm. Start with `fleet run …` or `fleet review: …`."
        if time.time() - pend["at"] > _PENDING_TTL_SEC:
            return "That staged action expired. Re-issue the `fleet run`/`fleet review` command."
        return f"Executing — {pend['desc']}\n\n{_execute(pend['action'], pend['params'])}"
    if _RE_NO.match(text):
        return "Cancelled." if _pending.pop(client_ip, None) else "Nothing staged."

    if _RE_STATUS.match(text):
        return _format_status()

    m = _RE_RUN_A.match(text) or _RE_RUN_B.match(text)
    if m:
        a, b, task = m.group(1).lower(), m.group(2).lower(), m.group(3).strip()
        # RUN_A is "<machine> <agent>", RUN_B is "<agent> on <machine>"
        if _RE_RUN_B.match(text):
            agent, machine = a, b
        else:
            machine, agent = a, b
        if machine not in _MACHINES:
            return f"Unknown machine `{machine}`. Choose one of: {', '.join(_MACHINES)}."
        if agent not in _AGENTS:
            return f"Unknown agent `{agent}`. Choose one of: {', '.join(_AGENTS)}."
        desc = f"run **{agent}** on **{machine}**:\n> {task}"
        return _stage(client_ip, "run", {"machine": machine, "agent": agent, "prompt": task}, desc)

    m = _RE_REVIEW.match(text)
    if m:
        task = m.group(1).strip()
        desc = f"review across **codex + agy**:\n> {task}"
        return _stage(client_ip, "review", {"prompt": task}, desc)

    if _RE_HELP.match(text):
        return _help()

    # starts with "fleet" but not a recognised command → let normal chat handle it
    return None
