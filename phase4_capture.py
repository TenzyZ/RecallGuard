"""RecallGuard Phase 4: capture grounded failure and human events."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

from phase2_preflight import run_remember

SOURCE_ENVIRONMENT = "environment"
SOURCE_HUMAN = "human"
DIAGNOSTIC_LIMIT = 400
ACTION_KEYS = {"verb", "target", "symbol", "value"}


def _bounded(text) -> str:
    """Return one bounded line, replacing control characters with spaces."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    cleaned = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in text
    )
    return " ".join(cleaned.split())[:DIAGNOSTIC_LIMIT]


def _required_text(value, field: str) -> str:
    try:
        value = _bounded(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a non-empty string") from exc
    if not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _scope_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def validate_run_record(record, repository=None, task=None) -> str | None:
    """Return a compact rejection reason, or None for a usable run record."""
    if not isinstance(record, dict):
        return "run_record_not_object"
    for field in ("repository", "task", "terminal"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            return f"invalid_{field}"
    if repository is not None and repository != record["repository"]:
        return "repository_mismatch"
    if task is not None and task != record["task"]:
        return "task_mismatch"
    return None


def _first_diagnostic(result: dict, workspace_path=None) -> str | None:
    for field in ("stderr", "stdout"):
        value = result.get(field)
        if not isinstance(value, str):
            continue
        lines = value.splitlines() or [value]
        if field == "stderr":
            lines.reverse()
        for line in lines:
            bounded = _bounded(line)
            for path, replacement in (
                (workspace_path, "<workspace>"),
                (sys.executable, "<python>"),
                (str(Path.home()), "<home>"),
            ):
                if isinstance(path, str) and path:
                    bounded = bounded.replace(path, replacement)
            if re.search(r"(?i)\b[a-z]:[\\/]", bounded) or re.search(
                r"(?:^|[\s'\"])/(?:home|users|tmp)/", bounded,
            ):
                continue
            if bounded:
                return _bounded(bounded)
    return None


def _verification_failure_event(run) -> dict | None:
    """Build the sole machine-authority event when all evidence is present."""
    if not isinstance(run, dict) or run.get("terminal") != "verification_failed":
        return None
    result = run.get("verification_result")
    action = run.get("planner_output", {}).get("action") if isinstance(
        run.get("planner_output"), dict
    ) else None
    before = run.get("target_sha256_before")
    after = run.get("target_sha256_after")
    if not (
        run.get("verification_attempted") is True
        and isinstance(result, dict)
        and result.get("ok") is False
        and type(result.get("returncode")) is int
        and result["returncode"] != 0
        and "error" not in result
        and run.get("authorized") is True
        and run.get("action_executed") is True
        and run.get("planner_output_valid") is True
        and isinstance(action, dict)
        and set(action) == ACTION_KEYS
        and all(isinstance(value, str) and value.strip() for value in action.values())
        and isinstance(before, str) and before.strip()
        and isinstance(after, str) and after.strip()
        and before != after
    ):
        return None

    returncode = result["returncode"]
    lesson = _bounded(
        f"{action['verb']} {action['symbol']}={action['value']} in "
        f"{action['target']} failed objective verification (exit {returncode})."
    )
    memory_enabled = run.get("memory_enabled")
    memory_state = str(memory_enabled).lower() if isinstance(memory_enabled, bool) else "unknown"
    evidence = _bounded(
        f"source={SOURCE_ENVIRONMENT} terminal=verification_failed "
        f"target_sha256_before={before} target_sha256_after={after} "
        f"memory_enabled={memory_state}"
    )
    verification = f"check exit {returncode}"
    diagnostic = _first_diagnostic(result, run.get("workspace_path"))
    if diagnostic:
        verification += f": {diagnostic}"
    return {
        "memory_type": "incident",
        "lesson": lesson,
        "evidence": evidence,
        "verification": _bounded(verification),
    }


def _human_events(human, terminal) -> list[dict]:
    if human is None:
        return []
    if not isinstance(human, dict):
        raise ValueError("human_input_not_object")

    evidence = f"source={SOURCE_HUMAN}"
    if terminal is not None:
        evidence += f" terminal={_required_text(terminal, 'terminal')}"
    evidence = _bounded(evidence)
    events = []
    pairs = (
        ("human_correction", "recommended_action"),
        ("rejected_approach", "rejection_reason"),
        ("decision", "decision_basis"),
    )
    values = {}
    for first, second in pairs:
        first_present = human.get(first) is not None
        second_present = human.get(second) is not None
        if first_present != second_present:
            raise ValueError(f"incomplete_{first}")
        if first_present:
            values[first] = _required_text(human[first], first)
            values[second] = _required_text(human[second], second)

    if "human_correction" in values:
        events.append({
            "memory_type": "human_correction",
            "lesson": values["human_correction"],
            "recommended_action": values["recommended_action"],
            "evidence": evidence,
        })
    if "rejected_approach" in values:
        events.append({
            "memory_type": "rejected_approach",
            "lesson": _bounded(
                f"Rejected: {values['rejected_approach']}. "
                f"Reason: {values['rejection_reason']}."
            ),
            "evidence": evidence,
        })
    if "decision" in values:
        events.append({
            "memory_type": "decision",
            "lesson": _bounded(
                f"Decision: {values['decision']}. Basis: {values['decision_basis']}."
            ),
            "evidence": evidence,
        })
    return events


def capture_events(run=None, human=None) -> list[dict]:
    """Purely validate evidence and construct deterministic capture events."""
    terminal = None
    events = []
    if run is not None:
        reason = validate_run_record(run)
        if reason:
            raise ValueError(reason)
        terminal = run["terminal"]
        incident = _verification_failure_event(run)
        if incident:
            events.append(incident)
    events.extend(_human_events(human, terminal))
    return events


def _result(repository, task, terminal) -> dict:
    return {
        "operation": "capture",
        "pid": os.getpid(),
        "repository": repository,
        "task": task,
        "run_terminal": terminal,
        "capture_outcome": "ok",
        "eligible": 0,
        "events": [],
    }


def run_capture(
    run=None,
    human=None,
    repository=None,
    task=None,
    remember=run_remember,
) -> dict:
    """Validate capture inputs, persist eligible events, and report outcomes."""
    terminal = run.get("terminal") if isinstance(run, dict) else None
    record = _result(repository, task, terminal)
    if run is not None:
        reason = validate_run_record(run, repository, task)
        if reason:
            record.update(capture_outcome="invalid_run_record", reason=reason)
            return record
        repository, task, terminal = run["repository"], run["task"], run["terminal"]
        record.update(repository=repository, task=task, run_terminal=terminal)
    else:
        try:
            repository = _scope_text(repository, "repository")
            task = _scope_text(task, "task")
        except ValueError as exc:
            record.update(capture_outcome="invalid_input", reason=str(exc))
            return record
        record.update(repository=repository, task=task)

    try:
        events = capture_events(run, human)
    except ValueError as exc:
        record.update(capture_outcome="invalid_input", reason=str(exc))
        return record
    if run is None and not events:
        record.update(capture_outcome="invalid_input", reason="human_event_required")
        return record

    record["eligible"] = len(events)
    stored_repository = _scope_text(repository, "repository")
    stored_task = _scope_text(task, "task")
    for event in events:
        captured = {"memory_type": event["memory_type"], "lesson": event["lesson"]}
        try:
            written = remember(
                repository=stored_repository,
                task=stored_task,
                memory_type=event["memory_type"],
                lesson=event["lesson"],
                recommended_action=event.get("recommended_action"),
                evidence=event.get("evidence"),
                verification=event.get("verification"),
            )
            if not isinstance(written, dict):
                written = {"write_outcome": "failed", "error": "invalid writer result"}
        except Exception as exc:
            written = {
                "write_outcome": "failed",
                "error": _bounded(f"{type(exc).__name__}: {exc}"),
            }
        for field in ("memory_key", "write_outcome", "entity_id", "error"):
            if field in written:
                value = written[field]
                captured[field] = _bounded(value) if isinstance(value, str) else value
        if written.get("write_outcome") != "ok":
            record["capture_outcome"] = "write_failed"
            captured["write_outcome"] = "failed"
        record["events"].append(captured)
    return record


def _invalid_cli_result(repository, task, reason) -> dict:
    record = _result(repository, task, None)
    record.update(capture_outcome="invalid_run_record", reason=_bounded(reason))
    return record


def main(argv=None, *, remember=run_remember) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-record")
    parser.add_argument("--repo")
    parser.add_argument("--task")
    parser.add_argument("--human-correction")
    parser.add_argument("--recommended-action")
    parser.add_argument("--rejected-approach")
    parser.add_argument("--rejection-reason")
    parser.add_argument("--decision")
    parser.add_argument("--decision-basis")
    args = parser.parse_args(argv)

    run = None
    if args.run_record is not None:
        try:
            if args.run_record == "-":
                run = json.load(sys.stdin)
            else:
                run = json.loads(Path(args.run_record).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            record = _invalid_cli_result(args.repo, args.task, f"{type(exc).__name__}: {exc}")
            print(json.dumps(record, indent=2))
            return 2

    human = {
        "human_correction": args.human_correction,
        "recommended_action": args.recommended_action,
        "rejected_approach": args.rejected_approach,
        "rejection_reason": args.rejection_reason,
        "decision": args.decision,
        "decision_basis": args.decision_basis,
    }
    record = run_capture(run, human, args.repo, args.task, remember=remember)
    print(json.dumps(record, indent=2))
    return 0 if record["capture_outcome"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
