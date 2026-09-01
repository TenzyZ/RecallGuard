"""Unit guards for the Phase 1 causal claim.

These use test doubles for determinism only. They do NOT prove the Phase 1
gate - that requires the live four-run Sibyl experiment against the real store.
All values here are obviously synthetic; the real correction never lives in
this repository.
"""

import unittest

from sibyl_memory_client import NotFoundError

import phase1_spike as spike

REPO = "example/repo"
TASK = "example task"


def experience(repo=REPO, task=TASK, recommended="recalled-action"):
    return {
        "schema": spike.SCHEMA,
        "repository": repo,
        "task": task,
        "memory_type": "human_correction",
        "recommended_action": recommended,
    }


class FakeClient:
    """Returns one stored entity, or raises NotFoundError."""

    def __init__(self, body=None):
        self.body = body
        self.requested = []

    def get_entity(self, category, name):
        self.requested.append((category, name))
        if self.body is None:
            raise NotFoundError("no such entity")
        return {"body": self.body}


def exploding_client():
    raise AssertionError("memory read path must not be opened when disabled")


def broken_client():
    raise RuntimeError("sibyl unavailable")


class KeyTests(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(spike.experience_key(REPO, TASK),
                         spike.experience_key(REPO, TASK))

    def test_scoped_to_repo_and_task(self):
        base = spike.experience_key(REPO, TASK)
        self.assertNotEqual(base, spike.experience_key("other/repo", TASK))
        self.assertNotEqual(base, spike.experience_key(REPO, "other task"))


class RelevanceTests(unittest.TestCase):
    def test_wrong_repository_is_irrelevant(self):
        self.assertFalse(spike.is_relevant(experience(repo="other/repo"), REPO, TASK))

    def test_wrong_task_is_irrelevant(self):
        self.assertFalse(spike.is_relevant(experience(task="other task"), REPO, TASK))

    def test_wrong_schema_is_irrelevant(self):
        body = experience() | {"schema": "something.else"}
        self.assertFalse(spike.is_relevant(body, REPO, TASK))

    def test_matching_scope_is_relevant(self):
        self.assertTrue(spike.is_relevant(experience(), REPO, TASK))


class DecisionTests(unittest.TestCase):
    def test_memory_disabled_never_reads(self):
        rec = spike.run_decide(REPO, TASK, "baseline-action", False,
                               open_=exploding_client)
        self.assertEqual(rec["memory_outcome"], "disabled")
        self.assertFalse(rec["memory_read_attempted"])
        self.assertEqual(rec["final_action"], "baseline-action")
        self.assertFalse(rec["changed_by_memory"])

    def test_missing_memory_falls_back_to_baseline(self):
        rec = spike.run_decide(REPO, TASK, "baseline-action", True,
                               open_=lambda: FakeClient(None))
        self.assertEqual(rec["memory_outcome"], "read_ok_no_entry")
        self.assertEqual(rec["final_action"], "baseline-action")
        self.assertFalse(rec["changed_by_memory"])

    def test_relevant_memory_overrides_baseline(self):
        rec = spike.run_decide(REPO, TASK, "baseline-action", True,
                               open_=lambda: FakeClient(experience()))
        self.assertEqual(rec["memory_outcome"], "read_ok_relevant")
        self.assertEqual(rec["final_action"], "recalled-action")
        self.assertTrue(rec["changed_by_memory"])

    def test_other_scope_memory_cannot_change_decision(self):
        # A body stored under this key but describing another repository/task.
        rec = spike.run_decide(REPO, TASK, "baseline-action", True,
                               open_=lambda: FakeClient(experience(repo="other/repo")))
        self.assertEqual(rec["memory_outcome"], "read_ok_irrelevant")
        self.assertEqual(rec["final_action"], "baseline-action")
        self.assertFalse(rec["changed_by_memory"])

    def test_read_failure_emits_no_decision(self):
        """Fail closed: a Sibyl outage must not yield a usable action."""
        rec = spike.run_decide(REPO, TASK, "baseline-action", True, open_=broken_client)
        self.assertEqual(rec["memory_outcome"], "read_failed")     # not read_ok_no_entry
        self.assertIn("RuntimeError", rec["error"])
        self.assertIn("sibyl unavailable", rec["error"])
        self.assertFalse(rec["relevant_memory_found"])
        self.assertEqual(rec["baseline_action"], "baseline-action")
        self.assertIsNone(rec["final_action"])
        self.assertFalse(rec["changed_by_memory"])

    def test_read_failure_does_not_call_the_decision_path(self):
        original = spike.decide
        spike.decide = lambda *a, **kw: self.fail(
            "decide() must not run after a Sibyl read failure")
        try:
            rec = spike.run_decide(REPO, TASK, "baseline-action", True,
                                   open_=broken_client)
        finally:
            spike.decide = original
        self.assertIsNone(rec["final_action"])


if __name__ == "__main__":
    unittest.main()
