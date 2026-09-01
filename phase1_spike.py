"""RecallGuard Phase 1 spike: prove Sibyl Memory changes a coding-agent decision.

Two operations, run as SEPARATE OS processes:

    remember   write one structured experience to Sibyl   (SDK write)
    decide     recall that experience, then choose        (SDK read)

The decision rule below is generic. No specific correction is encoded in this
repository: the corrected action enters only as runtime input to `remember`
and thereafter survives only inside Sibyl Memory.

    python phase1_spike.py decide   --repo R --task T --baseline B --memory off
    python phase1_spike.py remember --repo R --task T --failed-action ... \
        --failure-evidence ... --human-correction ... --recommended-action ...
    python phase1_spike.py decide   --repo R --task T --baseline B --memory on
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import sibyl_memory_client as sibyl
from sibyl_memory_client import DEFAULT_TENANT, MemoryClient, NotFoundError

SCHEMA = "recallguard.experience.v1"
CATEGORY = "recallguard_experience"


# ----------------------------------------------------------------------
# Deterministic memory identity (repository + task scope only)
# ----------------------------------------------------------------------

def _slug(text: str, limit: int = 48) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "x"


def experience_key(repository: str, task: str) -> str:
    """Entity name for one (repository, task) scope.

    Derived only from non-secret scope. The correction is never part of the key,
    so a fresh process that knows only the task can reconstruct it.
    """
    digest = hashlib.sha256(f"{repository}\x1f{task}".encode()).hexdigest()[:16]
    return f"{_slug(repository)}--{_slug(task)}--{digest}"


# ----------------------------------------------------------------------
# Sibyl SDK access
# ----------------------------------------------------------------------

def open_client() -> MemoryClient:
    """Open the already-initialized local Sibyl store through the official SDK.

    Tenant resolution mirrors sibyl_memory_mcp.server so the SDK and the MCP
    server address the same tenant for one credentials.json.
    """
    db = Path(os.environ.get(
        "SIBYL_MEMORY_DB", Path.home() / ".sibyl-memory" / "memory.db"))
    cred_path = Path(os.environ.get(
        "SIBYL_CREDENTIALS", Path.home() / ".sibyl-memory" / "credentials.json"))
    creds: dict = {}
    if cred_path.exists() and not cred_path.is_symlink():
        try:
            creds = json.loads(cred_path.read_text())
        except (OSError, json.JSONDecodeError):
            creds = {}
    # ponytail: credentials_claim/signature omitted - they only unlock the
    # server-side tier recheck at the 5 MB free cap. Pass them if the store
    # ever approaches that cap.
    return MemoryClient.local(
        str(db),
        tenant_id=creds.get("tenant_id") or creds.get("account_id") or DEFAULT_TENANT,
        account_id=creds.get("account_id"),
        session_token=creds.get("session_token"),
        tier=creds.get("tier", "free"),
    )


def is_relevant(body, repository: str, task: str) -> bool:
    """A memory only counts if it is this schema, this repository, this task."""
    return (
        isinstance(body, dict)
        and body.get("schema") == SCHEMA
        and body.get("repository") == repository
        and body.get("task") == task
        and bool(body.get("recommended_action"))
    )


def recall(client, repository: str, task: str):
    """Return (outcome, experience|None). Read errors propagate - a Sibyl
    failure must never be silently reported as 'no memory found'."""
    try:
        entity = client.get_entity(CATEGORY, experience_key(repository, task))
    except NotFoundError:
        return "read_ok_no_entry", None
    body = entity.get("body")
    if not is_relevant(body, repository, task):
        return "read_ok_irrelevant", None
    return "read_ok_relevant", body


def lesson(experience: dict) -> dict:
    """Compact recalled working context - not a transcript."""
    keys = ("memory_type", "failed_action", "failure_evidence",
            "human_correction", "recommended_action", "verification")
    return {k: experience[k] for k in keys if experience.get(k)}


# ----------------------------------------------------------------------
# Decision (generic: nothing about the correction lives here)
# ----------------------------------------------------------------------

def decide(baseline_action: str, experience: dict | None) -> tuple[str, bool]:
    """Recalled recommended action wins; otherwise the runtime baseline.

    Takes the recalled experience as an argument, so the decision structurally
    cannot be made before recall has happened.
    """
    if experience and experience.get("recommended_action"):
        return experience["recommended_action"], True
    return baseline_action, False


# ----------------------------------------------------------------------
# Operations
# ----------------------------------------------------------------------

def run_decide(repository, task, baseline_action, memory_enabled, open_=open_client) -> dict:
    record = {
        "operation": "decide",
        "pid": os.getpid(),
        "sdk": f"sibyl-memory-client {sibyl.__version__}",
        "repository": repository,
        "task": task,
        "memory_enabled": memory_enabled,
        "memory_key": experience_key(repository, task),
        "memory_read_attempted": False,
    }
    experience = None
    read_failed = False
    if not memory_enabled:
        record["memory_outcome"] = "disabled"
        record["note"] = "Sibyl read path DISABLED - no memory consulted."
    else:
        record["memory_read_attempted"] = True
        try:
            record["memory_outcome"], experience = recall(open_(), repository, task)
        except Exception as exc:              # noqa: BLE001 - reported, not swallowed
            read_failed = True
            record["memory_outcome"] = "read_failed"
            record["error"] = f"{type(exc).__name__}: {exc}"

    record["relevant_memory_found"] = experience is not None
    if experience is not None:
        record["recalled_lesson"] = lesson(experience)

    record["baseline_action"] = baseline_action
    if read_failed:
        # Fail closed. The memory dependency this layer exists to provide has
        # failed, so no action is selected at all - falling back to the baseline
        # would let a Sibyl outage look like a normal memory-free decision.
        record["final_action"] = None
        record["changed_by_memory"] = False
        record["note"] = "Sibyl read FAILED - no decision emitted."
    else:
        record["final_action"], record["changed_by_memory"] = decide(
            baseline_action, experience)
    return record


def run_remember(repository, task, fields, open_=open_client) -> dict:
    record = {
        "operation": "remember",
        "pid": os.getpid(),
        "sdk": f"sibyl-memory-client {sibyl.__version__}",
        "repository": repository,
        "task": task,
        "category": CATEGORY,
        "memory_key": experience_key(repository, task),
        "write_attempted": True,
    }
    body = {
        "schema": SCHEMA,
        "repository": repository,
        "task": task,
        "memory_type": "human_correction",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **{k: v for k, v in fields.items() if v},
    }
    try:
        entity = open_().set_entity(CATEGORY, record["memory_key"], body, status="active")
    except Exception as exc:                  # noqa: BLE001 - reported, not swallowed
        record["write_outcome"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record
    record["write_outcome"] = "ok"
    record["entity_id"] = entity["id"]
    record["updated_at"] = entity["updated_at"]
    record["stored"] = lesson(body)
    return record


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("decide", "remember"):
        s = sub.add_parser(name)
        s.add_argument("--repo", required=True, help="repository identity")
        s.add_argument("--task", required=True, help="task identity/text")

    d = sub.choices["decide"]
    d.add_argument("--baseline", required=True, help="action taken without memory")
    d.add_argument("--memory", choices=("on", "off"), required=True)

    r = sub.choices["remember"]
    r.add_argument("--failed-action", required=True)
    r.add_argument("--failure-evidence", required=True)
    r.add_argument("--human-correction", required=True)
    r.add_argument("--recommended-action", required=True)
    r.add_argument("--verification", default=None)

    a = p.parse_args(argv)
    if a.cmd == "decide":
        rec = run_decide(a.repo, a.task, a.baseline, a.memory == "on")
        failed = rec["memory_outcome"] == "read_failed"
    else:
        rec = run_remember(a.repo, a.task, {
            "failed_action": a.failed_action,
            "failure_evidence": a.failure_evidence,
            "human_correction": a.human_correction,
            "recommended_action": a.recommended_action,
            "verification": a.verification,
        })
        failed = rec["write_outcome"] != "ok"

    print(json.dumps(rec, indent=2))   # ensure_ascii keeps Windows consoles safe
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
