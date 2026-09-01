"""RecallGuard Phase 2: structured Sibyl experience and preflight recall."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

from phase1_spike import _slug, open_client

SCHEMA_V2 = "recallguard.experience.v2"
CATEGORY = "recallguard_experience"
MEMORY_TYPES = frozenset({
    "constraint",
    "decision",
    "incident",
    "rejected_approach",
    "successful_approach",
    "human_correction",
    "verification_result",
})
MAX_TERMS = 8
PER_TERM_LIMIT = 25
MIN_TERM_OVERLAP = 2
TOP_K = 3
STOPWORDS = frozenset({
    "a", "an", "the", "for", "to", "in", "of", "on", "and", "or",
    "not", "is", "it", "with", "fix", "add", "use",
})
TYPE_PRIORITY = {
    "constraint": 0,
    "incident": 1,
    "human_correction": 2,
    "decision": 3,
    "rejected_approach": 4,
    "successful_approach": 5,
    "verification_result": 6,
}
REQUIRED_BODY_FIELDS = (
    "schema", "repository", "task", "memory_type", "lesson", "recorded_at",
)
OPTIONAL_BODY_FIELDS = ("recommended_action", "evidence", "verification")


def normalize_task(task: str) -> list[str]:
    """Return bounded, safe, distinct search terms in first-seen order."""
    if not isinstance(task, str):
        return []
    terms: list[str] = []
    for term in re.sub(r"[^a-z0-9_]", " ", task.lower()).split():
        if len(term) < 3 or term in STOPWORDS or term in terms:
            continue
        terms.append(term)
        if len(terms) == MAX_TERMS:
            break
    return terms


def experience_key(
    repository: str,
    task: str,
    memory_type: str,
    lesson: str,
    recommended_action: str | None = None,
) -> str:
    """Return the content-addressed v2 Sibyl entity name."""
    identity = json.dumps(
        [repository, task, memory_type, lesson, recommended_action],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return f"rg2--{_slug(repository)}--{_slug(task)}--{digest}"


def _required_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _optional_text(value, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def build_body(
    repository: str,
    task: str,
    memory_type: str,
    lesson: str,
    recommended_action: str | None = None,
    evidence: str | None = None,
    verification: str | None = None,
) -> dict:
    """Validate inputs and build the exact v2 stored body."""
    repository = _required_text(repository, "repository")
    task = _required_text(task, "task")
    lesson = _required_text(lesson, "lesson")
    if memory_type not in MEMORY_TYPES:
        raise ValueError(f"unsupported memory_type: {memory_type!r}")
    optional = {
        "recommended_action": _optional_text(recommended_action, "recommended_action"),
        "evidence": _optional_text(evidence, "evidence"),
        "verification": _optional_text(verification, "verification"),
    }
    return {
        "schema": SCHEMA_V2,
        "repository": repository,
        "task": task,
        "memory_type": memory_type,
        "lesson": lesson,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **{key: value for key, value in optional.items() if value is not None},
    }


def run_remember(
    repository: str,
    task: str,
    memory_type: str,
    lesson: str,
    recommended_action: str | None = None,
    evidence: str | None = None,
    verification: str | None = None,
    open_=open_client,
) -> dict:
    """Write one validated v2 experience through the official Sibyl SDK."""
    record = {
        "operation": "remember",
        "pid": os.getpid(),
        "write_outcome": "failed",
        "memory_key": None,
        "repository": repository,
        "task": task,
        "memory_type": memory_type,
    }
    try:
        body = build_body(
            repository, task, memory_type, lesson, recommended_action,
            evidence, verification,
        )
        record["memory_key"] = experience_key(
            repository, task, memory_type, lesson, recommended_action,
        )
        entity = open_().set_entity(
            CATEGORY, record["memory_key"], body, status="active",
        )
    except Exception as exc:  # reported as a machine-readable failed write
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record
    record["write_outcome"] = "ok"
    if isinstance(entity, dict):
        for field in ("id", "updated_at"):
            if field in entity:
                record["entity_id" if field == "id" else field] = entity[field]
    return record


def _valid_body(body, repository: str) -> bool:
    if not isinstance(body, dict) or any(field not in body for field in REQUIRED_BODY_FIELDS):
        return False
    if body["schema"] != SCHEMA_V2 or body["repository"] != repository:
        return False
    if not isinstance(body["memory_type"], str) or body["memory_type"] not in MEMORY_TYPES:
        return False
    for field in ("repository", "task", "lesson", "recorded_at"):
        if not isinstance(body[field], str) or not body[field].strip():
            return False
    return all(
        field not in body or (isinstance(body[field], str) and bool(body[field].strip()))
        for field in OPTIONAL_BODY_FIELDS
    )


def _candidate_entry(candidate, repository: str, matched_terms: int) -> dict | None:
    """Validate one SDK row and return its compact planning projection."""
    if not isinstance(candidate, dict):
        return None
    if not all(field in candidate for field in ("id", "category", "name", "status", "body")):
        return None
    if not isinstance(candidate["id"], str) or not isinstance(candidate["name"], str):
        return None
    if candidate["category"] != CATEGORY or candidate["status"] != "active":
        return None
    body = candidate["body"]
    if not _valid_body(body, repository):
        return None
    entry = {
        "memory_type": body["memory_type"],
        "lesson": body["lesson"],
        "recorded_at": body["recorded_at"],
        "matched_terms": matched_terms,
        "source": {"category": candidate["category"], "name": candidate["name"]},
    }
    for field in OPTIONAL_BODY_FIELDS:
        if field in body:
            entry[field] = body[field]
    return entry


def _timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (AttributeError, OverflowError, TypeError, ValueError):
        return float("-inf")


def _rank_key(entry: dict) -> tuple:
    return (
        -entry["matched_terms"],
        TYPE_PRIORITY[entry["memory_type"]],
        -_timestamp(entry["recorded_at"]),
        entry["source"]["name"],
    )


def _normalized_action(value: str) -> str:
    return " ".join(value.lower().split())


def _has_conflict(entries: list[dict]) -> bool:
    actions = {
        _normalized_action(entry["recommended_action"])
        for entry in entries
        if entry["memory_type"] == "human_correction"
        and entry.get("recommended_action", "").strip()
    }
    return len(actions) > 1


def render_brief(entries: list[dict], conflict: bool = False) -> str:
    """Render a deterministic compact brief from already-accepted entries."""
    lines = ["Preflight Memory Brief"]
    if not entries:
        return "\n".join(lines + ["No relevant memory."])
    if conflict:
        lines.append("CONFLICT: differing human-correction actions require review.")
    for index, entry in enumerate(entries, 1):
        details = [
            f"{index}. [{entry['memory_type']}; matches={entry['matched_terms']}]",
            entry["lesson"],
        ]
        for field, label in (
            ("recommended_action", "action"),
            ("evidence", "evidence"),
            ("verification", "verification"),
        ):
            if field in entry:
                details.append(f"{label}: {entry[field]}")
        source = entry["source"]
        details.append(f"source: {source['category']}/{source['name']}")
        lines.append(" | ".join(details))
    return "\n".join(lines)


def _base_preflight(repository: str, task: str, memory_enabled: bool, terms: list[str]) -> dict:
    return {
        "operation": "preflight",
        "pid": os.getpid(),
        "repository": repository,
        "task": task,
        "memory_enabled": memory_enabled,
        "query_terms": terms,
        "state": "search_ok_no_relevant_memory",
        "preflight_ok": True,
        "candidates_seen": 0,
        "accepted": 0,
        "conflict": False,
        "entries": [],
    }


def run_preflight(
    repository: str,
    task: str,
    memory_enabled: bool,
    open_=open_client,
) -> dict:
    """Search Sibyl before planning and return one JSON-printable record."""
    terms = normalize_task(task)
    record = _base_preflight(repository, task, memory_enabled, terms)
    if not memory_enabled:
        record["state"] = "memory_disabled"
        record["rendered_brief"] = render_brief([])
        return record
    if not terms:
        record["rendered_brief"] = render_brief([])
        return record

    candidates: dict[str, dict] = {}
    matches: dict[str, set[str]] = {}
    try:
        client = open_()
        for term in terms:
            for candidate in client.search_entities(
                term, category=CATEGORY, limit=PER_TERM_LIMIT,
            ):
                if not isinstance(candidate, dict) or not isinstance(candidate.get("id"), str):
                    continue
                entity_id = candidate["id"]
                candidates.setdefault(entity_id, candidate)
                matches.setdefault(entity_id, set()).add(term)
    except Exception as exc:
        record.update({
            "state": "search_failed",
            "preflight_ok": False,
            "candidates_seen": len(candidates),
            "error": f"{type(exc).__name__}: {exc}",
            "rendered_brief": None,
        })
        return record

    record["candidates_seen"] = len(candidates)
    required_overlap = MIN_TERM_OVERLAP if len(terms) > 1 else 1
    accepted: list[dict] = []
    for entity_id, candidate in candidates.items():
        matched_terms = len(matches[entity_id])
        entry = _candidate_entry(candidate, repository, matched_terms)
        if entry is not None and matched_terms >= required_overlap:
            accepted.append(entry)
    accepted.sort(key=_rank_key)
    record["accepted"] = len(accepted)
    record["entries"] = accepted[:TOP_K]
    record["conflict"] = _has_conflict(record["entries"])
    if record["entries"]:
        record["state"] = "search_ok_relevant_memory"
    record["rendered_brief"] = render_brief(record["entries"], record["conflict"])
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    remember = sub.add_parser("remember")
    remember.add_argument("--repo", required=True)
    remember.add_argument("--task", required=True)
    remember.add_argument("--memory-type", choices=sorted(MEMORY_TYPES), required=True)
    remember.add_argument("--lesson", required=True)
    remember.add_argument("--recommended-action")
    remember.add_argument("--evidence")
    remember.add_argument("--verification")

    preflight = sub.add_parser("preflight")
    preflight.add_argument("--repo", required=True)
    preflight.add_argument("--task", required=True)
    preflight.add_argument("--memory", choices=("on", "off"), required=True)

    args = parser.parse_args(argv)
    if args.command == "remember":
        record = run_remember(
            args.repo, args.task, args.memory_type, args.lesson,
            args.recommended_action, args.evidence, args.verification,
        )
        failed = record["write_outcome"] == "failed"
    else:
        record = run_preflight(args.repo, args.task, args.memory == "on")
        failed = record["state"] == "search_failed"
    print(json.dumps(record, indent=2))
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
