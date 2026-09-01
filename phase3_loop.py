"""RecallGuard Phase 3 bounded coding-agent loop."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from phase2_preflight import run_preflight

TARGET_TEMPLATE = """CACHE_MAXSIZE = -1


class Cache:
    def __init__(self):
        self._items = {}

    def put(self, key, value):
        self._items[key] = value
        if CACHE_MAXSIZE >= 0 and len(self._items) > CACHE_MAXSIZE:
            self._items.pop(next(iter(self._items)))

    def __contains__(self, key):
        return key in self._items

    def __len__(self):
        return len(self._items)
"""

CHECK_TEMPLATE = """from target import Cache

cache = Cache()
for key in range(300):
    cache.put(key, key)
assert all(key in cache for key in range(300)), "hot working set was evicted"
for key in range(300, 401):
    cache.put(key, key)
assert len(cache) <= 400, "downstream shard budget exceeded"
"""

ACTION_SURFACE = {
    "verb": "set_assignment",
    "target": "target.py",
    "symbol": "CACHE_MAXSIZE",
    "current_value": "-1",
    "value_pattern": r"^[0-9]{1,6}$",
}
ACTION_KEYS = frozenset({"verb", "target", "symbol", "value"})
ASSIGNMENT = re.compile(r"^CACHE_MAXSIZE\s*=\s*.+$", re.MULTILINE)
DIAGNOSTIC_LIMIT = 400
VERIFY_TIMEOUT = 10


def _error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:DIAGNOSTIC_LIMIT]}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_workspace(workspace_root=None) -> Path:
    workspace = Path(tempfile.mkdtemp(
        prefix="recallguard-phase3-",
        dir=workspace_root,
    )).resolve()
    (workspace / "target.py").write_text(TARGET_TEMPLATE, encoding="utf-8")
    (workspace / "check.py").write_text(CHECK_TEMPLATE, encoding="utf-8")
    return workspace


def _admitted_names(entries: list) -> set[str]:
    names = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), dict):
            continue
        name = entry["source"].get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def validate_planner_output(output, entries: list) -> str | None:
    if not isinstance(output, dict) or set(output) != {"status", "action", "memory_refs"}:
        return "invalid_top_level_shape"
    if output["status"] not in ("propose", "abstain"):
        return "invalid_status"
    refs = output["memory_refs"]
    if not isinstance(refs, list) or not all(isinstance(ref, str) and ref for ref in refs):
        return "invalid_memory_refs"
    if not set(refs).issubset(_admitted_names(entries)):
        return "fabricated_memory_ref"
    if output["status"] == "abstain":
        if output["action"] is not None or refs:
            return "invalid_abstention"
        return None
    action = output["action"]
    if not isinstance(action, dict) or set(action) != ACTION_KEYS:
        return "invalid_action_shape"
    if not all(isinstance(value, str) and value for value in action.values()):
        return "invalid_action_field"
    return None


def authorize_action(action: dict, workspace: Path) -> str | None:
    if action["verb"] != ACTION_SURFACE["verb"]:
        return "verb_not_allowed"
    if action["target"] != ACTION_SURFACE["target"]:
        return "target_not_allowed"
    target = (workspace / action["target"]).resolve()
    try:
        target.relative_to(workspace.resolve())
    except ValueError:
        return "target_outside_workspace"
    if action["symbol"] != ACTION_SURFACE["symbol"]:
        return "symbol_not_allowed"
    if re.fullmatch(ACTION_SURFACE["value_pattern"], action["value"]) is None:
        return "value_not_allowed"
    return None


def execute_action(workspace: Path, action: dict) -> str | None:
    target = workspace / action["target"]
    try:
        source = target.read_text(encoding="utf-8")
        matches = list(ASSIGNMENT.finditer(source))
        if len(matches) != 1:
            return "assignment_missing_or_ambiguous"
        updated = ASSIGNMENT.sub(
            f"{action['symbol']} = {action['value']}", source, count=1,
        )
        target.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return _error(exc)
    return None


def verify_workspace(workspace: Path) -> dict:
    try:
        completed = subprocess.run(
            [sys.executable, "check.py"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "verification timed out"}
    except OSError as exc:
        return {"ok": False, "error": _error(exc)}
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout[:DIAGNOSTIC_LIMIT],
        "stderr": completed.stderr[:DIAGNOSTIC_LIMIT],
    }


def _record(repository: str, task: str, memory_enabled: bool) -> dict:
    return {
        "operation": "run",
        "pid": os.getpid(),
        "repository": repository,
        "task": task,
        "memory_enabled": memory_enabled,
        "preflight": None,
        "planner_called": False,
        "planner_output": None,
        "planner_output_valid": False,
        "admitted_memory_provenance": [],
        "authorized": False,
        "authorization_failure": None,
        "action_executed": False,
        "workspace_path": None,
        "target_sha256_before": None,
        "target_sha256_after": None,
        "verification_attempted": False,
        "verification_result": None,
        "terminal": None,
        "human_escalation": False,
    }


def run_loop(
    repository,
    task,
    memory_enabled,
    *,
    planner,
    preflight=run_preflight,
    workspace_root=None,
) -> dict:
    """Run one fresh, bounded preflight-planning-action-verification chain."""
    record = _record(repository, task, memory_enabled)
    try:
        workspace = build_workspace(workspace_root)
        target = workspace / ACTION_SURFACE["target"]
        record["workspace_path"] = str(workspace)
        record["target_sha256_before"] = _sha256(target)
        record["target_sha256_after"] = record["target_sha256_before"]
    except Exception as exc:
        record["terminal"] = "action_failed"
        record["error"] = _error(exc)
        return record

    try:
        recalled = preflight(repository, task, memory_enabled)
    except Exception as exc:
        recalled = {
            "state": "search_failed",
            "preflight_ok": False,
            "entries": [],
            "conflict": False,
            "error": _error(exc),
        }
    record["preflight"] = recalled
    if not isinstance(recalled, dict) or recalled.get("preflight_ok") is not True:
        record["terminal"] = "search_failed"
        return record
    if recalled.get("conflict") is True:
        record["terminal"] = "conflict_blocked"
        record["human_escalation"] = True
        return record
    entries = recalled.get("entries")
    if not isinstance(entries, list):
        record["terminal"] = "search_failed"
        return record
    record["admitted_memory_provenance"] = sorted(_admitted_names(entries))
    request = {
        "repository": repository,
        "task": task,
        "action_surface": dict(ACTION_SURFACE),
        "entries": entries,
    }

    record["planner_called"] = True
    try:
        proposal = planner(request)
    except Exception as exc:
        record["terminal"] = "planner_failed"
        record["error"] = _error(exc)
        return record
    try:
        json.dumps(proposal)
    except (TypeError, ValueError):
        record["terminal"] = "planner_output_invalid"
        record["planner_validation_failure"] = "non_json_planner_output"
        return record
    record["planner_output"] = proposal
    invalid = validate_planner_output(proposal, entries)
    if invalid:
        record["terminal"] = "planner_output_invalid"
        record["planner_validation_failure"] = invalid
        return record
    record["planner_output_valid"] = True
    if proposal["status"] == "abstain":
        record["terminal"] = "abstained"
        return record

    failure = authorize_action(proposal["action"], workspace)
    if failure:
        record["terminal"] = "action_unauthorized"
        record["authorization_failure"] = failure
        return record
    record["authorized"] = True

    failure = execute_action(workspace, proposal["action"])
    try:
        record["target_sha256_after"] = _sha256(target)
    except OSError as exc:
        record["terminal"] = "action_failed"
        record["error"] = _error(exc)
        return record
    if failure:
        record["terminal"] = "action_failed"
        record["error"] = failure
        return record
    record["action_executed"] = True

    record["verification_attempted"] = True
    result = verify_workspace(workspace)
    record["verification_result"] = result
    record["terminal"] = "success" if result["ok"] else "verification_failed"
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--memory", choices=("on", "off"), required=True)
    parser.add_argument("--planner-model", default="claude-opus-5")
    args = parser.parse_args(argv)

    from phase3_planner_claude import claude_planner

    result = run_loop(
        args.repo,
        args.task,
        args.memory == "on",
        planner=lambda request: claude_planner(request, model=args.planner_model),
    )
    result["planner_metadata"] = {
        "kind": "claude_cli",
        "model": args.planner_model,
    }
    print(json.dumps(result, indent=2))
    return 0 if result["terminal"] == "success" else 2


if __name__ == "__main__":
    sys.exit(main())
