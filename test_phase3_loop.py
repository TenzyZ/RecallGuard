"""Offline deterministic tests for the RecallGuard Phase 3 loop."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import phase2_preflight
import phase3_loop as loop
from phase3_planner_claude import PlannerError, claude_planner

REPO = "synthetic/phase3"
TASK = "bound the repository cache safely"


def entry(name="memory-1"):
    return {
        "memory_type": "human_correction",
        "lesson": "Synthetic repository evidence favors a bounded cache.",
        "recommended_action": "Choose a value supported by the admitted evidence.",
        "recorded_at": "2026-09-01T00:00:00Z",
        "matched_terms": 2,
        "source": {"category": "recallguard_experience", "name": name},
    }


def preflight_result(state="search_ok_no_relevant_memory", entries=None, **changes):
    value = {
        "operation": "preflight",
        "repository": REPO,
        "task": TASK,
        "memory_enabled": state != "memory_disabled",
        "state": state,
        "preflight_ok": state != "search_failed",
        "conflict": False,
        "entries": entries or [],
        "rendered_brief": "human-only rendering",
    }
    value.update(changes)
    return value


def proposed(value="320", refs=None, **action_changes):
    action = {
        "verb": "set_assignment",
        "target": "target.py",
        "symbol": "CACHE_MAXSIZE",
        "value": value,
    }
    action.update(action_changes)
    return {"status": "propose", "action": action, "memory_refs": refs or []}


def abstained():
    return {"status": "abstain", "action": None, "memory_refs": []}


def envelope(payload):
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": payload,
        "session_id": "synthetic-session",
        "total_cost_usd": 0.0,
    })


class HarnessOrderingTests(unittest.TestCase):
    def test_preflight_precedes_planner_and_request_is_minimal(self):
        events = []
        admitted = entry()

        def fake_preflight(*_):
            events.append("preflight")
            return preflight_result("search_ok_relevant_memory", [admitted])

        def planner(request):
            events.append("planner")
            self.assertEqual(
                set(request), {"repository", "task", "action_surface", "entries"},
            )
            self.assertIs(request["entries"][0], admitted)
            self.assertNotIn("rendered_brief", request)
            self.assertNotIn("baseline_value", request)
            return proposed(refs=["memory-1"])

        with tempfile.TemporaryDirectory() as root:
            result = loop.run_loop(
                REPO, TASK, True, planner=planner,
                preflight=fake_preflight, workspace_root=root,
            )
        self.assertEqual(events, ["preflight", "planner"])
        self.assertEqual(result["terminal"], "success")

    def test_memory_disabled_uses_real_phase2_bypass_and_has_no_correction(self):
        requests = []

        def disabled(repo, task, enabled):
            return phase2_preflight.run_preflight(
                repo, task, enabled,
                open_=lambda: self.fail("memory-off must not open Sibyl"),
            )

        with tempfile.TemporaryDirectory() as root:
            result = loop.run_loop(
                REPO, TASK, False,
                planner=lambda request: requests.append(request) or abstained(),
                preflight=disabled,
                workspace_root=root,
            )
        self.assertEqual(result["preflight"]["state"], "memory_disabled")
        self.assertEqual(requests[0]["entries"], [])
        self.assertNotIn("correction", json.dumps(requests[0]).lower())

    def test_empty_successful_search_allows_planning(self):
        called = []
        with tempfile.TemporaryDirectory() as root:
            result = loop.run_loop(
                REPO, TASK, True,
                planner=lambda request: called.append(request) or proposed(),
                preflight=lambda *_: preflight_result(),
                workspace_root=root,
            )
        self.assertTrue(called)
        self.assertEqual(result["terminal"], "success")

    def test_search_failure_and_conflict_block_before_planner(self):
        def forbidden(_):
            self.fail("planner must not be called")

        cases = (
            (preflight_result("search_failed"), "search_failed", False),
            (preflight_result("search_ok_relevant_memory", [entry()], conflict=True),
             "conflict_blocked", True),
        )
        for recalled, terminal, escalation in cases:
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as root:
                result = loop.run_loop(
                    REPO, TASK, True, planner=forbidden,
                    preflight=lambda *_args, value=recalled: value,
                    workspace_root=root,
                )
                self.assertEqual(result["terminal"], terminal)
                self.assertFalse(result["planner_called"])
                self.assertFalse(result["action_executed"])
                self.assertFalse(result["verification_attempted"])
                self.assertEqual(result["human_escalation"], escalation)

    def test_natural_language_memory_can_change_decision_and_hash(self):
        def planner(request):
            if request["entries"]:
                return proposed("320", ["memory-1"])
            return proposed("200")

        with tempfile.TemporaryDirectory() as root:
            off = loop.run_loop(
                REPO, TASK, False, planner=planner,
                preflight=lambda *_: preflight_result("memory_disabled"),
                workspace_root=root,
            )
            on = loop.run_loop(
                REPO, TASK, True, planner=planner,
                preflight=lambda *_: preflight_result(
                    "search_ok_relevant_memory", [entry()],
                ),
                workspace_root=root,
            )
        self.assertEqual(off["target_sha256_before"], on["target_sha256_before"])
        self.assertNotEqual(off["target_sha256_after"], on["target_sha256_after"])
        self.assertEqual(off["terminal"], "verification_failed")
        self.assertEqual(on["terminal"], "success")


class PlannerValidationTests(unittest.TestCase):
    def _run(self, output, entries=None):
        with tempfile.TemporaryDirectory() as root:
            return loop.run_loop(
                REPO, TASK, True, planner=lambda _: output,
                preflight=lambda *_: preflight_result(entries=entries),
                workspace_root=root,
            )

    def test_malformed_and_extra_outputs_are_rejected(self):
        cases = [
            None,
            [],
            object(),
            {"status": "propose", "action": None},
            proposed() | {"confidence": 1},
            {"status": "other", "action": None, "memory_refs": []},
            {"status": "propose", "action": {"verb": "set_assignment"}, "memory_refs": []},
        ]
        for output in cases:
            with self.subTest(output=output):
                result = self._run(output)
                self.assertEqual(result["terminal"], "planner_output_invalid")
                self.assertFalse(result["action_executed"])

    def test_fabricated_and_memory_off_refs_are_rejected(self):
        for entries in ([entry()], []):
            with self.subTest(entries=bool(entries)):
                result = self._run(proposed(refs=["fabricated"]), entries)
                self.assertEqual(result["terminal"], "planner_output_invalid")

    def test_planner_exception_is_planner_failed(self):
        def broken(_):
            raise RuntimeError("synthetic planner failure")

        with tempfile.TemporaryDirectory() as root:
            result = loop.run_loop(
                REPO, TASK, True, planner=broken,
                preflight=lambda *_: preflight_result(), workspace_root=root,
            )
        self.assertEqual(result["terminal"], "planner_failed")
        self.assertFalse(result["verification_attempted"])

    def test_abstention_runs_no_action_or_verification(self):
        result = self._run(abstained())
        self.assertEqual(result["terminal"], "abstained")
        self.assertTrue(result["planner_output_valid"])
        self.assertFalse(result["action_executed"])
        self.assertFalse(result["verification_attempted"])


class AuthorizationAndVerificationTests(unittest.TestCase):
    def test_unauthorized_actions_never_write_or_verify(self):
        cases = (
            proposed(verb="delete_file"),
            proposed(target="../target.py"),
            proposed(symbol="OTHER"),
            proposed("-1"),
            proposed("1000000"),
        )
        for proposal in cases:
            with self.subTest(action=proposal["action"]), tempfile.TemporaryDirectory() as root:
                result = loop.run_loop(
                    REPO, TASK, True, planner=lambda _, value=proposal: value,
                    preflight=lambda *_: preflight_result(), workspace_root=root,
                )
                self.assertEqual(result["terminal"], "action_unauthorized")
                self.assertFalse(result["action_executed"])
                self.assertFalse(result["verification_attempted"])
                self.assertEqual(result["target_sha256_before"], result["target_sha256_after"])

    def test_missing_or_ambiguous_assignment_fails_action(self):
        templates = ("VALUE = -1\n", loop.TARGET_TEMPLATE + "\nCACHE_MAXSIZE = 2\n")
        for template in templates:
            with self.subTest(template=template), patch.object(loop, "TARGET_TEMPLATE", template):
                with tempfile.TemporaryDirectory() as root:
                    result = loop.run_loop(
                        REPO, TASK, True, planner=lambda _: proposed(),
                        preflight=lambda *_: preflight_result(), workspace_root=root,
                    )
                self.assertEqual(result["terminal"], "action_failed")
                self.assertFalse(result["action_executed"])
                self.assertFalse(result["verification_attempted"])

    def test_objective_process_exit_controls_verification(self):
        for value, terminal, code in (("200", "verification_failed", 1), ("320", "success", 0)):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as root:
                result = loop.run_loop(
                    REPO, TASK, True, planner=lambda _, v=value: proposed(v),
                    preflight=lambda *_: preflight_result(), workspace_root=root,
                )
                self.assertEqual(result["terminal"], terminal)
                self.assertTrue(result["verification_attempted"])
                self.assertEqual(result["verification_result"]["returncode"], code)

    def test_planner_claim_cannot_override_failure(self):
        claimed = proposed("200") | {"verification_passed": True}
        result = PlannerValidationTests()._run(claimed)
        self.assertEqual(result["terminal"], "planner_output_invalid")
        plain = PlannerValidationTests()._run(proposed("200"))
        self.assertEqual(plain["terminal"], "verification_failed")

    def test_no_memory_write_or_competing_persistence_calls_exist(self):
        source = Path(loop.__file__).read_text(encoding="utf-8")
        self.assertNotIn("run_remember", source)
        self.assertNotIn("set_entity", source)
        self.assertNotIn("pickle", source)


class ClaudeAdapterTests(unittest.TestCase):
    def test_expected_argv_and_empty_isolated_cwd(self):
        observed = []

        def runner(argv, **kwargs):
            observed.append((argv, kwargs))
            self.assertEqual(list(Path(kwargs["cwd"]).iterdir()), [])
            self.assertNotEqual(Path(kwargs["cwd"]).resolve(), Path.cwd().resolve())
            self.assertTrue(Path(kwargs["cwd"]).name.startswith("recallguard-planner-"))
            return subprocess.CompletedProcess(
                argv, 0, envelope(json.dumps(abstained())), "",
            )

        requests = [
            {
                "repository": "synthetic/repo",
                "task": "Tune cache",
                "action_surface": {"verb": "set_assignment"},
                "entries": [],
            },
            {"repository": "synthetic/other", "task": "Different task"},
        ]
        for request in requests:
            self.assertEqual(claude_planner(request, runner=runner), abstained())
        argv = observed[0][0]
        queries = [call[0][call[0].index("-p") + 1] for call in observed]
        self.assertEqual(queries, [
            json.dumps(request, ensure_ascii=False, separators=(",", ":"))
            for request in requests
        ])
        self.assertNotEqual(queries[0], queries[1])
        for flag in (
            "-p", "--output-format", "--model", "--system-prompt",
            "--restricted", "--strict-mcp-config",
        ):
            self.assertIn(flag, argv)
        for forbidden in (
            "--continue", "--resume", "--mcp-config", "--max-turns",
            "--dangerously-skip-permissions",
        ):
            self.assertNotIn(forbidden, argv)
        self.assertIn("--tools", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertFalse(any("credential" in part.lower() or "token" in part.lower()
                             for part in argv))

    def test_nonzero_timeout_and_missing_cli_are_transport_failures(self):
        def nonzero(argv, **_):
            return subprocess.CompletedProcess(argv, 3, "", "synthetic error")

        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        def missing(*_args, **_kwargs):
            raise FileNotFoundError("claude")

        for runner in (nonzero, timeout, missing):
            with self.subTest(runner=runner.__name__), self.assertRaises(PlannerError):
                claude_planner({}, runner=runner)

    def test_invalid_outer_json_and_bad_envelopes_are_transport_failures(self):
        outputs = (
            "not json",
            "[]",
            json.dumps({"type": "result", "subtype": "error", "is_error": True}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": 7}),
        )
        for output in outputs:
            with self.subTest(output=output), self.assertRaises(PlannerError):
                claude_planner(
                    {}, runner=lambda argv, text=output, **_: subprocess.CompletedProcess(
                        argv, 0, text, "",
                    ),
                )

    def test_single_json_fence_and_abstention_are_accepted(self):
        fenced = "```json\n" + json.dumps(abstained()) + "\n```"
        result = claude_planner(
            {}, runner=lambda argv, **_: subprocess.CompletedProcess(
                argv, 0, envelope(fenced), "",
            ),
        )
        self.assertEqual(result, abstained())

    def test_invalid_payload_reaches_harness_as_invalid_output(self):
        def planner(request):
            return claude_planner(
                request,
                runner=lambda argv, **_: subprocess.CompletedProcess(
                    argv, 0, envelope("not planner json"), "",
                ),
            )

        with tempfile.TemporaryDirectory() as root:
            result = loop.run_loop(
                REPO, TASK, True, planner=planner,
                preflight=lambda *_: preflight_result(), workspace_root=root,
            )
        self.assertEqual(result["terminal"], "planner_output_invalid")


if __name__ == "__main__":
    unittest.main()
