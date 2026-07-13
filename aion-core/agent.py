"""Bounded agentic action loop for AION."""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from config import CONFIG
from llm import ask_llm_chat


READ_RISKS = {"read"}
WRITE_RISKS = {"personal_write", "repo_write", "external_write", "dangerous"}
PENDING_TTL_SEC = 600
_PENDING: dict[tuple[str, str], dict[str, Any]] = {}


@dataclass
class AgentOutcome:
    response: str
    handled: bool = True
    events: list[dict[str, Any]] | None = None
    staged: bool = False


def _workspace_root() -> Path:
    configured = CONFIG.get("agent_workspace_root")
    if configured:
        return Path(str(configured)).resolve()
    return Path(__file__).resolve().parent


def _safe_path(path: str) -> Path:
    root = _workspace_root()
    candidate = (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("That path is outside AION's workspace. Nice try, but no.")
    return candidate


def _agent_enabled() -> bool:
    return bool(CONFIG.get("agent_enabled", True))


def _is_yes(text: str) -> bool:
    return (text or "").strip().lower() in {"yes", "y", "confirm", "do it", "go ahead", "approved"}


def _is_cancel(text: str) -> bool:
    return (text or "").strip().lower() in {"cancel", "no", "nope", "stop", "never mind", "nevermind"}


def _looks_agentic(text: str) -> bool:
    return bool(
        re.search(
            r"(?i)\b(agent|inspect|research|investigate|debug|fix|implement|patch|edit|change|delete|remove|"
            r"run tests?|test this|why.*fail|repo|codebase|file|function|class|trace)\b",
            text or "",
        )
    )


def _json_from_text(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[index:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def _tool_repo_search(args: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "No search query provided."
    root = _workspace_root()
    result = subprocess.run(
        ["rg", "-n", "--", query, str(root)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = (result.stdout or result.stderr or "").strip()
    if not output:
        return f"No matches for `{query}`."
    lines = output.splitlines()[:80]
    return "\n".join(lines)


def _tool_repo_read_file(args: dict[str, Any]) -> str:
    path = _safe_path(str(args.get("path") or ""))
    if not path.exists() or not path.is_file():
        return f"File not found: {path}"
    max_chars = int(args.get("max_chars") or 12000)
    return path.read_text(encoding="utf-8", errors="replace")[:max(100, min(max_chars, 50000))]


def _tool_test_run_unittest(args: dict[str, Any]) -> str:
    target = str(args.get("target") or "").strip()
    command = [".venv/bin/python", "-m", "unittest"]
    if target:
        command.append(target)
    root = _workspace_root()
    result = subprocess.run(
        command,
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=int(CONFIG.get("agent_test_timeout_sec", 120)),
    )
    output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
    if len(output) > 12000:
        output = output[:12000] + "\n... (truncated)"
    return f"exit={result.returncode}\n{output}"


def _tool_repo_replace_text(args: dict[str, Any]) -> str:
    path = _safe_path(str(args.get("path") or ""))
    old = str(args.get("old") or "")
    new = str(args.get("new") or "")
    if not old:
        raise ValueError("Missing exact old text for replacement.")
    if not path.exists() or not path.is_file():
        raise ValueError(f"File not found: {path}")
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise ValueError(f"Exact text was not found in {path.name}; refusing to guess.")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"Updated {path.relative_to(_workspace_root())}."


TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "repo.search": {
        "risk": "read",
        "description": "Search the local AION repo for text using ripgrep.",
        "executor": _tool_repo_search,
    },
    "repo.read_file": {
        "risk": "read",
        "description": "Read a file from the local AION repo.",
        "executor": _tool_repo_read_file,
    },
    "test.run_unittest": {
        "risk": "read",
        "description": "Run Python unittest in the AION core workspace.",
        "executor": _tool_test_run_unittest,
    },
    "repo.replace_text": {
        "risk": "repo_write",
        "description": "Replace exact text in a repo file after confirmation.",
        "executor": _tool_repo_replace_text,
    },
}


def available_agent_tools() -> list[dict[str, str]]:
    return [
        {"tool": name, "risk": spec["risk"], "description": spec["description"]}
        for name, spec in TOOL_REGISTRY.items()
    ]


def _build_planner_messages(user_message: str, username: str, history: list | None = None) -> list[dict[str, str]]:
    tools_json = json.dumps(available_agent_tools(), indent=2)
    system = (
        "You are AION's action planner. Return ONLY JSON. No markdown.\n"
        "Decide whether to use tools for practical work. Keep normal conversation out of the agent.\n"
        "Allowed tools:\n"
        f"{tools_json}\n\n"
        "Return shape:\n"
        "{"
        '"mode":"agent"|"chat",'
        '"summary":"short human summary",'
        '"actions":[{"tool":"repo.search","args":{"query":"..."}}],'
        '"final":"optional final response if no actions are needed"'
        "}\n"
        "Use repo.replace_text only when you have exact old/new text. Writes require confirmation."
    )
    messages = [{"role": "system", "content": system}]
    for item in (history or [])[-6:]:
        role = item.get("role") if item.get("role") in {"user", "assistant"} else "user"
        messages.append({"role": role, "content": str(item.get("content") or "")})
    messages.append({"role": "user", "content": f"{username}: {user_message}"})
    return messages


def _normalize_actions(plan: dict[str, Any]) -> list[dict[str, Any]]:
    actions = plan.get("actions") or []
    if not isinstance(actions, list):
        return []
    normalized = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        tool = str(action.get("tool") or "")
        if tool not in TOOL_REGISTRY:
            if re.search(r"(?i)(shell|system|delete|rm|exec)", tool):
                normalized.append({"tool": tool, "args": action.get("args") or {}, "risk": "dangerous"})
            continue
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        risk = TOOL_REGISTRY[tool]["risk"]
        normalized.append({"tool": tool, "args": args, "risk": risk})
    return normalized[: int(CONFIG.get("agent_max_steps", 6))]


def _stage(username: str, session_id: str, actions: list[dict[str, Any]], summary: str) -> AgentOutcome:
    key = (username, session_id)
    _PENDING[key] = {
        "actions": actions,
        "summary": summary,
        "created": time.time(),
    }
    action_lines = []
    for action in actions:
        action_lines.append(f"- `{action['tool']}` with `{json.dumps(action['args'], sort_keys=True)}`")
    return AgentOutcome(
        response=(
            f"I can do that. Here's what I'd change before I touch anything:\n"
            + "\n".join(action_lines)
            + "\n\nSay `yes` and I'll run it. Say `cancel` and I'll pretend this little plan never happened."
        ),
        events=[{"event_type": "agent_action_staged", "payload": {"actions": actions, "summary": summary}}],
        staged=True,
    )


def _execute_actions(actions: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    outputs = []
    events = []
    for action in actions:
        spec = TOOL_REGISTRY[action["tool"]]
        try:
            output = spec["executor"](action.get("args") or {})
        except Exception as exc:
            output = f"Failed: {exc}"
        outputs.append(f"{action['tool']}: {output}")
        events.append({"event_type": "agent_tool_result", "tool_name": action["tool"], "content": output, "payload": action})
    return "\n".join(outputs), events


def _handle_pending_confirmation(message: str, username: str, session_id: str) -> AgentOutcome | None:
    key = (username, session_id)
    pending = _PENDING.get(key)
    if not pending:
        return None
    if time.time() - pending["created"] > PENDING_TTL_SEC:
        _PENDING.pop(key, None)
        return AgentOutcome("That staged action expired. Re-run the request and I'll rebuild the plan.")
    if _is_cancel(message):
        _PENDING.pop(key, None)
        return AgentOutcome("Cancelled. No files touched, no drama.")
    if not _is_yes(message):
        return None
    _PENDING.pop(key, None)
    output, events = _execute_actions(pending["actions"])
    return AgentOutcome(
        response=f"Done. I ran the staged action.\n\n```text\n{output}\n```",
        events=[{"event_type": "agent_action_confirmed", "payload": pending}] + events,
    )


def run_agent_turn(
    message: str,
    *,
    username: str,
    session_id: str,
    history: list | None = None,
    planner: Callable[[list], str] | None = None,
) -> AgentOutcome | None:
    if not _agent_enabled():
        return None
    pending_result = _handle_pending_confirmation(message, username, session_id)
    if pending_result:
        return pending_result
    if _is_yes(message) or _is_cancel(message):
        return None
    if not _looks_agentic(message):
        return None

    planner = planner or ask_llm_chat
    raw = planner(_build_planner_messages(message, username, history))
    plan = _json_from_text(raw)
    if not plan:
        return None
    if str(plan.get("mode") or "agent").lower() == "chat":
        final = str(plan.get("final") or "").strip()
        return AgentOutcome(final) if final else None

    actions = _normalize_actions(plan)
    summary = str(plan.get("summary") or "I found a practical path. Shocking, I know.").strip()
    if not actions:
        final = str(plan.get("final") or summary).strip()
        return AgentOutcome(final) if final else None

    events = [{"event_type": "agent_plan", "content": summary, "payload": {"actions": actions}}]
    if any(action["risk"] == "dangerous" for action in actions):
        return AgentOutcome(
            "I’m not running that. It asks for an unrestricted or destructive action, and that is exactly how machines become expensive paperweights.",
            events=events + [{"event_type": "agent_action_blocked", "payload": {"actions": actions}}],
        )
    write_actions = [action for action in actions if action["risk"] in WRITE_RISKS]
    if write_actions and CONFIG.get("agent_confirm_writes", True):
        staged = _stage(username, session_id, actions, summary)
        staged.events = events + (staged.events or [])
        return staged

    output, tool_events = _execute_actions(actions)
    return AgentOutcome(
        response=f"{summary}\n\n```text\n{output}\n```",
        events=events + tool_events + [{"event_type": "agent_final", "content": summary}],
    )
