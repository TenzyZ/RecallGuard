"""Deterministic Phase 2 application tests; no real Sibyl store is used."""

import os
import re
import unittest
from unittest.mock import patch

import phase1_spike
import phase2_preflight as preflight

REPO = "synthetic/repo"


def body(**changes):
    value = {
        "schema": preflight.SCHEMA_V2,
        "repository": REPO,
        "task": "fix profile caching",
        "memory_type": "constraint",
        "lesson": "Keep cache entries repository-scoped.",
        "recorded_at": "2026-09-01T08:00:00+00:00",
    }
    value.update(changes)
    return value


def candidate(entity_id="e1", name="memory-1", **body_changes):
    return {
        "id": entity_id,
        "category": preflight.CATEGORY,
        "name": name,
        "status": "active",
        "body": body(**body_changes),
    }


class FakeSearchClient:
    def __init__(self, rows=None, error=None):
        self.rows = rows or {}
        self.error = error
        self.calls = []

    def search_entities(self, term, *, category, limit):
        self.calls.append((term, category, limit))
        if self.error:
            raise self.error
        return self.rows.get(term, [])


class FakeWriteClient:
    def __init__(self):
        self.calls = []

    def set_entity(self, category, name, stored_body, *, status):
        self.calls.append((category, name, stored_body, status))
        return {"id": "entity-7", "updated_at": "2026-09-01T08:30:00Z"}


class SchemaTests(unittest.TestCase):
    def test_missing_required_field_and_malformed_body_are_rejected(self):
        missing = candidate()
        del missing["body"]["lesson"]
        malformed = candidate(entity_id="e2")
        malformed["body"] = "not-a-dict"
        bad_type = candidate(entity_id="e3", memory_type=[])
        bad_name = candidate(entity_id="e4")
        bad_name["name"] = 4
        for row in (missing, malformed, bad_type, bad_name):
            with self.subTest(row=row["id"]):
                self.assertIsNone(preflight._candidate_entry(row, REPO, 2))

    def test_all_supported_types_are_accepted_and_unknown_type_is_rejected(self):
        for memory_type in preflight.MEMORY_TYPES:
            with self.subTest(memory_type=memory_type):
                row = candidate(memory_type=memory_type)
                self.assertIsNotNone(preflight._candidate_entry(row, REPO, 2))
        self.assertIsNone(preflight._candidate_entry(
            candidate(memory_type="unsupported"), REPO, 2,
        ))

    def test_v1_and_non_active_candidates_are_rejected(self):
        self.assertIsNone(preflight._candidate_entry(
            candidate(schema=phase1_spike.SCHEMA), REPO, 2,
        ))
        inactive = candidate()
        inactive["status"] = "archived"
        self.assertIsNone(preflight._candidate_entry(inactive, REPO, 2))


class IdentityTests(unittest.TestCase):
    def test_identity_is_content_addressed_but_not_timestamp_addressed(self):
        args = (REPO, "task", "constraint", "lesson", "action")
        key = preflight.experience_key(*args)
        self.assertEqual(key, preflight.experience_key(*args))
        self.assertNotEqual(key, preflight.experience_key(REPO, "task", "constraint", "other", "action"))
        self.assertNotEqual(key, preflight.experience_key(REPO, "task", "incident", "lesson", "action"))
        self.assertNotEqual(key, preflight.experience_key(REPO, "task", "constraint", "lesson", "other"))
        first = preflight.build_body(*args)
        second = preflight.build_body(*args)
        first["recorded_at"] = "2000-01-01T00:00:00Z"
        second["recorded_at"] = "2099-01-01T00:00:00Z"
        self.assertEqual(
            preflight.experience_key(
                first["repository"], first["task"], first["memory_type"],
                first["lesson"], first["recommended_action"],
            ),
            preflight.experience_key(
                second["repository"], second["task"], second["memory_type"],
                second["lesson"], second["recommended_action"],
            ),
        )

    def test_v2_key_cannot_equal_equivalent_v1_key(self):
        v2 = preflight.experience_key(REPO, "task", "constraint", "lesson")
        self.assertTrue(v2.startswith("rg2--"))
        self.assertNotEqual(v2, phase1_spike.experience_key(REPO, "task"))


class NormalizationTests(unittest.TestCase):
    def test_cleanup_stopwords_short_terms_and_stable_deduplication(self):
        self.assertEqual(
            preflight.normalize_task("Fix THE Cache, cache! for API_profiles + API_profiles"),
            ["cache", "api_profiles"],
        )

    def test_terms_are_capped_and_search_safe(self):
        terms = preflight.normalize_task(
            '"C:\\Repo\\x.py" AND alpha|beta NOT gamma; delta:epsilon '
            "zeta eta theta iota kappa lambda"
        )
        self.assertEqual(len(terms), preflight.MAX_TERMS)
        self.assertTrue(all(re.fullmatch(r"[a-z0-9_]+", term) for term in terms))
        self.assertNotIn("and", terms)
        self.assertNotIn("not", terms)

    def test_punctuation_only_never_opens_client(self):
        self.assertEqual(preflight.normalize_task("!!! \\\\ ///"), [])
        record = preflight.run_preflight(
            REPO, "!!!", True,
            open_=lambda: self.fail("client must not open for zero terms"),
        )
        self.assertEqual(record["state"], "search_ok_no_relevant_memory")
        self.assertEqual(record["entries"], [])


class RetrievalTests(unittest.TestCase):
    def test_per_term_union_overlap_and_related_wording(self):
        relevant = candidate()
        wrong_repo = candidate("wrong", repository="other/repo")
        low_overlap = candidate("low", task="unrelated", lesson="Only one term matched.")
        old_schema = candidate("v1", schema=phase1_spike.SCHEMA)
        client = FakeSearchClient({
            "caching": [relevant, wrong_repo, low_overlap, old_schema],
            "profiles": [relevant, wrong_repo, old_schema],
        })
        record = preflight.run_preflight(
            REPO, "implement caching for user profiles", True, open_=lambda: client,
        )
        self.assertEqual(
            [call[0] for call in client.calls],
            ["implement", "caching", "user", "profiles"],
        )
        self.assertTrue(all(
            call[1:] == (preflight.CATEGORY, preflight.PER_TERM_LIMIT)
            for call in client.calls
        ))
        self.assertEqual(record["candidates_seen"], 4)
        self.assertEqual(record["accepted"], 1)
        self.assertEqual(record["state"], "search_ok_relevant_memory")
        self.assertEqual(record["entries"][0]["matched_terms"], 2)
        self.assertEqual(record["entries"][0]["lesson"], relevant["body"]["lesson"])

    def test_single_term_requires_one_match_and_union_uses_entity_id(self):
        first = candidate(name="first-copy")
        second = candidate(name="second-copy")
        client = FakeSearchClient({"caching": [first, second]})
        record = preflight.run_preflight(REPO, "caching", True, open_=lambda: client)
        self.assertEqual(record["candidates_seen"], 1)
        self.assertEqual(record["accepted"], 1)
        self.assertEqual(record["entries"][0]["matched_terms"], 1)
        self.assertEqual(record["entries"][0]["source"]["name"], "first-copy")

    def test_all_rejected_is_a_successful_empty_search(self):
        client = FakeSearchClient({
            "alpha": [candidate(repository="other/repo")],
            "beta": [candidate(repository="other/repo")],
        })
        record = preflight.run_preflight(REPO, "alpha beta", True, open_=lambda: client)
        self.assertEqual(record["candidates_seen"], 1)
        self.assertEqual(record["accepted"], 0)
        self.assertEqual(record["state"], "search_ok_no_relevant_memory")
        self.assertTrue(record["preflight_ok"])


class FailureTests(unittest.TestCase):
    def test_search_failure_is_fail_closed_and_does_not_render(self):
        client = FakeSearchClient(error=RuntimeError("search unavailable"))
        with patch.object(preflight, "render_brief", side_effect=AssertionError("must not render")):
            record = preflight.run_preflight(REPO, "alpha beta", True, open_=lambda: client)
        self.assertEqual(record["state"], "search_failed")
        self.assertFalse(record["preflight_ok"])
        self.assertEqual(record["entries"], [])
        self.assertIsNone(record["rendered_brief"])
        self.assertEqual(record["error"], "RuntimeError: search unavailable")

    def test_memory_disabled_never_opens_client(self):
        record = preflight.run_preflight(
            REPO, "alpha beta", False,
            open_=lambda: self.fail("disabled path must not open client"),
        )
        self.assertEqual(record["state"], "memory_disabled")
        self.assertTrue(record["preflight_ok"])
        self.assertEqual(record["candidates_seen"], 0)
        self.assertEqual(record["accepted"], 0)
        self.assertEqual(record["entries"], [])


class BriefTests(unittest.TestCase):
    def test_top_k_type_timestamp_and_name_ordering_are_deterministic(self):
        rows = [
            candidate("decision", "decision", memory_type="decision", recorded_at="2099-01-01T00:00:00Z"),
            candidate("z-old", "z-name", memory_type="constraint", recorded_at="2025-01-01T00:00:00Z"),
            candidate("a-old", "a-name", memory_type="constraint", recorded_at="2025-01-01T00:00:00Z"),
            candidate("new", "new-name", memory_type="constraint", recorded_at="2026-01-01T00:00:00Z"),
            candidate("bad-date", "bad-date", memory_type="constraint", recorded_at="not-a-date"),
        ]
        client = FakeSearchClient({"alpha": rows, "beta": rows})
        record = preflight.run_preflight(REPO, "alpha beta", True, open_=lambda: client)
        self.assertEqual(record["accepted"], 5)
        self.assertEqual(len(record["entries"]), preflight.TOP_K)
        self.assertEqual(
            [entry["source"]["name"] for entry in record["entries"]],
            ["new-name", "a-name", "z-name"],
        )

    def test_higher_overlap_precedes_type_priority(self):
        correction = candidate("correction", "correction", memory_type="human_correction")
        constraint = candidate("constraint", "constraint", memory_type="constraint")
        client = FakeSearchClient({
            "alpha": [correction, constraint],
            "beta": [correction, constraint],
            "gamma": [correction],
        })
        record = preflight.run_preflight(REPO, "alpha beta gamma", True, open_=lambda: client)
        self.assertEqual(record["entries"][0]["memory_type"], "human_correction")

    def test_compact_provenance_shape_and_conflict(self):
        first = candidate(
            "first", "first", memory_type="human_correction",
            recommended_action="Use bounded cache", evidence="synthetic evidence",
            verification="synthetic verification",
        )
        second = candidate(
            "second", "second", memory_type="human_correction",
            recommended_action="  Disable Cache  ",
        )
        client = FakeSearchClient({"alpha": [first, second], "beta": [first, second]})
        record = preflight.run_preflight(REPO, "alpha beta", True, open_=lambda: client)
        self.assertTrue(record["conflict"])
        self.assertIn("CONFLICT", record["rendered_brief"])
        self.assertEqual(
            set(record["entries"][0]),
            {
                "memory_type", "lesson", "recommended_action", "evidence",
                "verification", "recorded_at", "matched_terms", "source",
            },
        )
        self.assertEqual(set(record["entries"][0]["source"]), {"category", "name"})

    def test_empty_brief_is_explicit(self):
        self.assertEqual(
            preflight.render_brief([]),
            "Preflight Memory Brief\nNo relevant memory.",
        )


class ObservabilityAndWriteTests(unittest.TestCase):
    def test_results_contain_current_pid_and_write_uses_active_status(self):
        writer = FakeWriteClient()
        remembered = preflight.run_remember(
            REPO, "task", "constraint", "lesson", "action",
            open_=lambda: writer,
        )
        recalled = preflight.run_preflight(REPO, "!!!", True)
        self.assertEqual(remembered["pid"], os.getpid())
        self.assertEqual(recalled["pid"], os.getpid())
        self.assertEqual(remembered["write_outcome"], "ok")
        self.assertEqual(remembered["entity_id"], "entity-7")
        self.assertEqual(writer.calls[0][0], preflight.CATEGORY)
        self.assertEqual(writer.calls[0][3], "active")
        self.assertEqual(writer.calls[0][2]["schema"], preflight.SCHEMA_V2)

    def test_invalid_write_input_fails_without_opening_client(self):
        record = preflight.run_remember(
            REPO, "task", "unknown", "lesson",
            open_=lambda: self.fail("invalid input must not open client"),
        )
        self.assertEqual(record["write_outcome"], "failed")
        self.assertIn("ValueError", record["error"])

    def test_write_exception_is_machine_readable(self):
        record = preflight.run_remember(
            REPO, "task", "constraint", "lesson",
            open_=lambda: (_ for _ in ()).throw(RuntimeError("write unavailable")),
        )
        self.assertEqual(record["write_outcome"], "failed")
        self.assertEqual(record["error"], "RuntimeError: write unavailable")


if __name__ == "__main__":
    unittest.main()
