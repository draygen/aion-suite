"""Conservative, project-local repair proposals for recognized runtime failures.

Repairs are deterministic recipes, not arbitrary model-generated shell commands.
Diagnosis and proposal generation are read-only. Exact file changes are applied
only after a user supplies the one-time approval token, and predefined tests run
without a shell. Failed verification rolls the files back to their prior content.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PENDING_TTL_SEC = 900
_PENDING: dict[tuple[str, str], dict[str, Any]] = {}
_PROJECT_ROOT_OVERRIDE: Path | None = None
_LESSONS_PATH_OVERRIDE: Path | None = None


@dataclass(frozen=True)
class RepairChange:
    path: str
    old: str
    new: str


@dataclass(frozen=True)
class RepairRecipe:
    recipe_id: str
    title: str
    diagnosis: str
    failure_patterns: tuple[str, ...]
    tool_ids: tuple[str, ...]
    relevant_paths: tuple[str, ...]
    changes: tuple[RepairChange, ...]
    verification_targets: tuple[str, ...]


@dataclass
class RepairOutcome:
    response: str
    events: list[dict[str, Any]]
    staged: bool = False


_MIXED_FAMILY_OLD = """\
            if network.subnet_of(allowed) or network == allowed:
                return True
"""

_MIXED_FAMILY_NEW = """\
            if network.version != allowed.version:
                continue
            if network.subnet_of(allowed) or network == allowed:
                return True
"""


_RECIPES: tuple[RepairRecipe, ...] = (
    RepairRecipe(
        recipe_id="network_allowlist_mixed_ip_family",
        title="Ignore opposite-family networks during CIDR authorization",
        diagnosis=(
            "The network authorization loop compares an IPv4 CIDR with an IPv6 "
            "allowlist entry (or vice versa). Python's ipaddress module rejects "
            "that cross-family subnet comparison before the authorized entry is reached."
        ),
        failure_patterns=(r"not (?:of )?the same version", r"mixed.+address family"),
        tool_ids=("nmap_ping_sweep",),
        relevant_paths=("tools.py", "test_tools.py"),
        changes=(RepairChange("tools.py", _MIXED_FAMILY_OLD, _MIXED_FAMILY_NEW),),
        verification_targets=(
            "test_tools.TestTools.test_authorizes_ipv4_cidr_with_mixed_family_allowlist",
            "test_tools.TestTools.test_authorizes_ipv6_cidr_with_mixed_family_allowlist",
            "test_tools.TestTools.test_routes_ping_sweep_with_mixed_family_allowlist",
        ),
    ),
)


def _project_root() -> Path:
    return (_PROJECT_ROOT_OVERRIDE or Path(__file__).resolve().parent).resolve()


def _lessons_path() -> Path:
    return (_LESSONS_PATH_OVERRIDE or (_project_root() / "data" / "repair_lessons.jsonl")).resolve()


def _safe_project_path(relative_path: str) -> Path:
    root = _project_root()
    candidate = (root / relative_path).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("Repair paths must stay inside the Aion project.")
    return candidate


def _match_recipe(failure: str, tool_id: str | None) -> RepairRecipe | None:
    text = str(failure or "")
    for recipe in _RECIPES:
        if tool_id and recipe.tool_ids and tool_id not in recipe.tool_ids:
            continue
        if any(re.search(pattern, text, re.IGNORECASE | re.DOTALL) for pattern in recipe.failure_patterns):
            return recipe
    return None


def _inspect_recipe(recipe: RepairRecipe) -> tuple[list[dict[str, Any]], str | None]:
    inspected = []
    try:
        for relative_path in recipe.relevant_paths:
            path = _safe_project_path(relative_path)
            content = path.read_bytes()
            inspected.append(
                {
                    "path": relative_path,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest()[:12],
                }
            )
        _build_updated_files(recipe)
    except (OSError, ValueError) as exc:
        return inspected, str(exc)
    return inspected, None


def _build_updated_files(recipe: RepairRecipe) -> tuple[dict[Path, str], dict[Path, str]]:
    originals: dict[Path, str] = {}
    updated: dict[Path, str] = {}
    for change in recipe.changes:
        path = _safe_project_path(change.path)
        if not path.is_file():
            raise ValueError(f"Required project file is missing: {change.path}")
        current = updated.get(path)
        if current is None:
            current = path.read_text(encoding="utf-8")
            originals[path] = current
        occurrences = current.count(change.old)
        if occurrences != 1:
            raise ValueError(
                f"Expected one exact repair location in {change.path}; found {occurrences}. "
                "Refusing to guess."
            )
        updated[path] = current.replace(change.old, change.new, 1)
    return originals, updated


def _render_patch(recipe: RepairRecipe) -> str:
    chunks = []
    for change in recipe.changes:
        diff = difflib.unified_diff(
            change.old.splitlines(),
            change.new.splitlines(),
            fromfile=f"a/{change.path}",
            tofile=f"b/{change.path}",
            lineterm="",
        )
        chunks.append("\n".join(diff))
    return "\n".join(chunk for chunk in chunks if chunk)[:8000]


def _append_lesson(
    recipe: RepairRecipe,
    *,
    outcome: str,
    result: str,
    verification: str,
) -> None:
    if outcome not in {"approved", "rejected"}:
        raise ValueError("Repair lesson outcome must be approved or rejected.")
    path = _lessons_path()
    root = _project_root()
    if path != root / "data" / "repair_lessons.jsonl" and _LESSONS_PATH_OVERRIDE is None:
        raise ValueError("Repair lessons must stay in the project data directory.")
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "recipe_id": recipe.recipe_id,
        "outcome": outcome,
        "result": result,
        "verification": verification,
        "changed_files": sorted({change.path for change in recipe.changes}),
    }
    encoded = (json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)


def _proposal_response(recipe: RepairRecipe, token: str, inspected: list[dict[str, Any]]) -> str:
    files = ", ".join(item["path"] for item in inspected)
    tests = "\n".join(f"- `{target}`" for target in recipe.verification_targets) or "- No tests configured."
    return (
        f"Recognized repair: **{recipe.title}**\n\n"
        f"Diagnosis: {recipe.diagnosis}\n\n"
        f"Inspected project files: {files}.\n\n"
        "Proposed minimal patch (not applied):\n"
        f"```diff\n{_render_patch(recipe)}\n```\n\n"
        f"Verification plan:\n{tests}\n\n"
        f"Reply `repair approve {token}` within {PENDING_TTL_SEC // 60} minutes to apply the "
        f"exact patch and run those tests, or `repair reject {token}` to reject it. "
        "No file has been changed."
    )


def propose_repair_for_failure(
    failure: str,
    *,
    tool_id: str | None,
    username: str,
    session_id: str,
) -> RepairOutcome | None:
    """Return a read-only repair proposal for a recognized local project failure."""
    recipe = _match_recipe(failure, tool_id)
    if not recipe:
        return None
    inspected, error = _inspect_recipe(recipe)
    if error:
        return RepairOutcome(
            response=(
                f"I recognized **{recipe.title}**, but the exact repair recipe no longer "
                f"matches the current project files: {error} No changes were made."
            ),
            events=[
                {
                    "event_type": "repair_proposal_blocked",
                    "payload": {"recipe_id": recipe.recipe_id, "reason": "preflight_mismatch"},
                }
            ],
        )
    token = secrets.token_hex(3)
    _PENDING[(username, session_id)] = {
        "recipe": recipe,
        "token": token,
        "created": time.time(),
    }
    return RepairOutcome(
        response=_proposal_response(recipe, token, inspected),
        events=[
            {
                "event_type": "repair_proposed",
                "payload": {
                    "recipe_id": recipe.recipe_id,
                    "files": [item["path"] for item in inspected],
                },
            }
        ],
        staged=True,
    )


def _run_verification(recipe: RepairRecipe) -> tuple[bool, str]:
    if not recipe.verification_targets:
        return True, "not_run"
    result = subprocess.run(
        [sys.executable, "-m", "unittest", *recipe.verification_targets],
        cwd=str(_project_root()),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode == 0, f"exit_{result.returncode}"


def _apply_approved_repair(recipe: RepairRecipe) -> RepairOutcome:
    try:
        originals, updated = _build_updated_files(recipe)
    except (OSError, ValueError) as exc:
        _append_lesson(recipe, outcome="approved", result="preflight_failed", verification="not_run")
        return RepairOutcome(
            response=f"Repair approval recorded, but preflight failed: {exc} No changes were made.",
            events=[
                {
                    "event_type": "repair_approval_failed",
                    "payload": {"recipe_id": recipe.recipe_id, "result": "preflight_failed"},
                }
            ],
        )

    try:
        for path, content in updated.items():
            path.write_text(content, encoding="utf-8")
        verified, verification = _run_verification(recipe)
        if not verified:
            for path, content in originals.items():
                path.write_text(content, encoding="utf-8")
            _append_lesson(
                recipe,
                outcome="approved",
                result="rolled_back",
                verification=verification,
            )
            return RepairOutcome(
                response=(
                    f"The approved repair failed verification ({verification}), so the project "
                    "files were restored. The outcome was recorded locally."
                ),
                events=[
                    {
                        "event_type": "repair_rolled_back",
                        "payload": {
                            "recipe_id": recipe.recipe_id,
                            "verification": verification,
                        },
                    }
                ],
            )
    except Exception:
        for path, content in originals.items():
            path.write_text(content, encoding="utf-8")
        _append_lesson(recipe, outcome="approved", result="rolled_back", verification="error")
        return RepairOutcome(
            response="The approved repair could not complete, so the project files were restored.",
            events=[
                {
                    "event_type": "repair_rolled_back",
                    "payload": {"recipe_id": recipe.recipe_id, "verification": "error"},
                }
            ],
        )

    _append_lesson(recipe, outcome="approved", result="applied", verification=verification)
    return RepairOutcome(
        response=(
            f"Applied the approved repair **{recipe.title}** and completed verification "
            f"({verification}). The privacy-preserving outcome was recorded locally."
        ),
        events=[
            {
                "event_type": "repair_applied",
                "payload": {"recipe_id": recipe.recipe_id, "verification": verification},
            }
        ],
    )


def handle_repair_command(message: str, *, username: str, session_id: str) -> RepairOutcome | None:
    """Handle explicit project-local repair proposal, approval, and rejection commands."""
    text = (message or "").strip()
    key = (username, session_id)
    pending = _PENDING.get(key)

    if re.fullmatch(r"repair\s+status", text, re.IGNORECASE):
        if not pending:
            return RepairOutcome("No repair proposal is pending.", [])
        recipe = pending["recipe"]
        return RepairOutcome(
            f"Pending repair: **{recipe.title}**. Use the approval or rejection command "
            "shown with the proposal.",
            [],
            staged=True,
        )

    decision = re.fullmatch(r"repair\s+(approve|reject)\s+([a-f0-9]{6})", text, re.IGNORECASE)
    if decision:
        if not pending:
            return RepairOutcome("No repair proposal is pending.", [])
        if time.time() - pending["created"] > PENDING_TTL_SEC:
            _PENDING.pop(key, None)
            return RepairOutcome("That repair proposal expired. Reproduce or diagnose the failure again.", [])
        if decision.group(2).lower() != pending["token"]:
            return RepairOutcome("That repair token does not match the pending proposal.", [])
        _PENDING.pop(key, None)
        recipe = pending["recipe"]
        if decision.group(1).lower() == "reject":
            _append_lesson(recipe, outcome="rejected", result="not_applied", verification="not_run")
            return RepairOutcome(
                response="Repair rejected. No project files were changed; the outcome was recorded locally.",
                events=[
                    {
                        "event_type": "repair_rejected",
                        "payload": {"recipe_id": recipe.recipe_id},
                    }
                ],
            )
        return _apply_approved_repair(recipe)

    diagnose = re.fullmatch(r"repair\s+(?:diagnose|propose)\s+(.+)", text, re.IGNORECASE | re.DOTALL)
    if diagnose:
        outcome = propose_repair_for_failure(
            diagnose.group(1),
            tool_id=None,
            username=username,
            session_id=session_id,
        )
        if outcome:
            return outcome
        return RepairOutcome(
            "That failure is not in Aion's conservative repair recipe registry. "
            "No changes were proposed or made.",
            [{"event_type": "repair_unrecognized", "payload": {"result": "no_recipe"}}],
        )
    return None


def record_approved_repair(
    recipe_id: str,
    *,
    result: str,
    verification: str,
) -> bool:
    """Record an already user-approved in-scope repair without storing user input."""
    recipe = next((item for item in _RECIPES if item.recipe_id == recipe_id), None)
    if not recipe:
        return False
    _append_lesson(recipe, outcome="approved", result=result, verification=verification)
    return True
