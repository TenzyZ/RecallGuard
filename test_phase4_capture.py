"""Offline deterministic tests for RecallGuard Phase 4 capture."""

import copy
import inspect
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import phase2_preflight
import phase3_loop
import phase4_capture as capture

REPO = "synthetic/phase4"
TASK = "capture an objective cache failure"


def eligible_run():
    return {
        "repository": REPO,
        "task": TASK,
        "terminal": "verification_failed",
        "memory_enabled": False,
        "verification_attempted": True,
        "verification_result": {
            "ok": False,
            "returncode": 1,
            "stdout": "",
            "stderr": "first diagnostic\nsecond diagnostic",
        },
        "authorized": True,
        "action_executed": True,
        "planner_output_valid": True,
        "planner_output": {
            "status": "propose",
            "action": {
                "verb": "set_assignment",
                "target": "target.py",
                "symbol": "CACHE_MAXSIZE",
                "value": "200",
            },
            "memory_refs": [],
        },
        "target_sha256_before": "a" * 64,
        "target_sha256_after": "b" * 64,
        "workspace_path": "C:/secret/temporary/workspace",
    }


def success_run():
    run = eligible_run()
    run.update(
        terminal="success",
        verification_result={"ok": True, "returncode": 0, "stdout": "", "stderr": ""},
    )
    return run


class FakeWriter:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, **values):
        self.calls.append(values)
        if self.result is not None:
            return self.result
        return {
            "write_outcome": "ok",
            "memory_key": phase2_preflight.experience_key(
                values["repository"], values["task"], values["memory_type"],
                values["lesson"], values["recommended_action"],
            ),
            "entity_id": "entity-4",
        }


class ObjectiveIncidentTests(unittest.TestCase):
    def test_supported_failure_produces_one_bounded_incident(self):
        events = capture.capture_events(eligible_run())
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["memory_type"], "incident")
        self.assertEqual(
            event["lesson"],
            "set_assignment CACHE_MAXSIZE=200 in target.py failed objective verification (exit 1).",
        )
        self.assertEqual(event["verification"], "check exit 1: second diagnostic")
        self.assertIn("source=environment", event["evidence"])
        self.assertIn("memory_enabled=false", event["evidence"])
        self.assertNotIn("recommended_action", event)
        self.assertNotIn("workspace", json.dumps(event).lower())

    def test_every_required_evidence_element_is_fail_closed(self):
        def remove_returncode(run):
            run["verification_result"].pop("returncode")

        cases = (
            ("terminal mismatch", lambda run: run.update(terminal="success")),
            ("verification not attempted", lambda run: run.update(verification_attempted=False)),
            ("verification ok", lambda run: run["verification_result"].update(ok=True)),
            ("returncode zero", lambda run: run["verification_result"].update(returncode=0)),
            ("missing returncode", remove_returncode),
            ("boolean returncode", lambda run: run["verification_result"].update(returncode=True)),
            ("noninteger returncode", lambda run: run["verification_result"].update(returncode="1")),
            ("verification error", lambda run: run["verification_result"].update(error="verification timed out")),
            ("unauthorized", lambda run: run.update(authorized=False)),
            ("action not executed", lambda run: run.update(action_executed=False)),
            ("planner output invalid", lambda run: run.update(planner_output_valid=False)),
            ("malformed action", lambda run: run["planner_output"].update(action={"verb": "set_assignment"})),
            ("empty action field", lambda run: run["planner_output"]["action"].update(value=" ")),
            ("unchanged hash", lambda run: run.update(target_sha256_after=run["target_sha256_before"])),
        )
        for name, break_evidence in cases:
            with self.subTest(name=name):
                run = copy.deepcopy(eligible_run())
                break_evidence(run)
                self.assertIsNone(capture._verification_failure_event(run))

    def test_infrastructure_failure_and_success_are_not_incidents(self):
        infrastructure = eligible_run()
        infrastructure["verification_result"] = {
            "ok": False,
            "error": "verification timed out",
        }
        self.assertEqual(capture.capture_events(infrastructure), [])
        self.assertEqual(capture.capture_events(success_run()), [])

    def test_absolute_diagnostic_paths_are_sanitized_or_skipped(self):
        run = eligible_run()
        workspace = str(Path.home() / "Temp" / "recallguard-phase3-synthetic")
        run["workspace_path"] = workspace
        run["verification_result"]["stderr"] = (
            f"{sys.executable}: can't open file '{workspace}\\check.py': "
            "[Errno 2] No such file"
        )
        verification = capture._verification_failure_event(run)["verification"]
        self.assertIn("can't open file", verification)
        self.assertIn("<python>", verification)
        self.assertIn("<workspace>", verification)
        self.assertNotIn(str(Path.home()), verification)
        self.assertNotRegex(verification, r"(?i)\b[a-z]:[\\/]")
        self.assertEqual(
            capture._first_diagnostic(
                {"stderr": r"C:\unknown\private\check.py", "stdout": "safe stdout"},
            ),
            "safe stdout",
        )

    def test_planner_claims_never_establish_or_override_verification(self):
        claimed_failure = success_run()
        claimed_failure["planner_output"]["verification_failed"] = True
        self.assertEqual(capture.capture_events(claimed_failure), [])

        claimed_success = eligible_run()
        claimed_success["planner_output"]["verification_passed"] = True
        self.assertEqual(len(capture.capture_events(claimed_success)), 1)

        unsupported_failure = eligible_run()
        unsupported_failure["verification_result"].update(ok=True, returncode=0)
        unsupported_failure["planner_output"]["verification_failed"] = True
        self.assertEqual(capture.capture_events(unsupported_failure), [])


class HumanAuthorityTests(unittest.TestCase):
    def test_correction_requires_explicit_text_and_action(self):
        human = {
            "human_correction": "Keep the working set intact.",
            "recommended_action": "Use a limit above the working set.",
        }
        event = capture.capture_events(success_run(), human)[0]
        self.assertEqual(event["memory_type"], "human_correction")
        self.assertEqual(event["lesson"], human["human_correction"])
        self.assertEqual(event["recommended_action"], human["recommended_action"])
        self.assertIn("terminal=success", event["evidence"])
        self.assertFalse(any(
            item["memory_type"] == "human_correction"
            for item in capture.capture_events(eligible_run())
        ))

    def test_incomplete_or_blank_human_pairs_never_write(self):
        invalid = (
            {"human_correction": "Correction"},
            {"recommended_action": "Action"},
            {"human_correction": "   ", "recommended_action": "Action"},
            {"rejected_approach": "Approach"},
            {"decision": "Decision", "decision_basis": "\n\t"},
        )
        for human in invalid:
            with self.subTest(human=human):
                writer = FakeWriter()
                result = capture.run_capture(success_run(), human, remember=writer)
                self.assertEqual(result["capture_outcome"], "invalid_input")
                self.assertEqual(writer.calls, [])

    def test_rejection_and_decision_require_human_authority(self):
        human = {
            "rejected_approach": "Set the cache below the working set",
            "rejection_reason": "Objective verification failed",
            "decision": "Keep capture downstream from execution",
            "decision_basis": "The run record is the evidence boundary",
        }
        events = capture.capture_events(success_run(), human)
        self.assertEqual(
            [event["memory_type"] for event in events],
            ["rejected_approach", "decision"],
        )
        self.assertEqual(
            events[0]["lesson"],
            "Rejected: Set the cache below the working set. Reason: Objective verification failed.",
        )
        self.assertEqual(
            events[1]["lesson"],
            "Decision: Keep capture downstream from execution. Basis: The run record is the evidence boundary.",
        )
        automatic = capture.capture_events(eligible_run())
        self.assertNotIn("rejected_approach", [event["memory_type"] for event in automatic])
        planner_only = success_run()
        planner_only["planner_output"]["decision"] = "Adopt this proposal"
        self.assertEqual(capture.capture_events(planner_only), [])

    def test_event_order_is_fixed(self):
        human = {
            "human_correction": "Correction",
            "recommended_action": "Action",
            "rejected_approach": "Approach",
            "rejection_reason": "Reason",
            "decision": "Decision",
            "decision_basis": "Basis",
        }
        self.assertEqual(
            [event["memory_type"] for event in capture.capture_events(eligible_run(), human)],
            ["incident", "human_correction", "rejected_approach", "decision"],
        )


class ValidationAndPersistenceTests(unittest.TestCase):
    def test_text_is_single_line_trimmed_bounded_and_excludes_workspace(self):
        long_text = "  first\n\x00second\t" + "x" * 500
        events = capture.capture_events(None, {
            "human_correction": long_text,
            "recommended_action": long_text,
        })
        for value in events[0].values():
            if isinstance(value, str):
                self.assertLessEqual(len(value), capture.DIAGNOSTIC_LIMIT)
                self.assertNotRegex(value, r"[\r\n\t\x00]")
                self.assertEqual(value, value.strip())
        self.assertNotIn("workspace_path", json.dumps(events))

    def test_scope_and_malformed_inputs_fail_before_write(self):
        cases = (
            ("malformed", dict(run="not a record")),
            ("repo mismatch", dict(run=success_run(), repository="other/repo")),
            ("task mismatch", dict(run=success_run(), task="other task")),
            ("human-only repo missing", dict(human={"decision": "D", "decision_basis": "B"}, task=TASK)),
            ("human-only task missing", dict(human={"decision": "D", "decision_basis": "B"}, repository=REPO)),
            ("human-only event missing", dict(repository=REPO, task=TASK)),
        )
        for name, arguments in cases:
            with self.subTest(name=name):
                writer = FakeWriter()
                result = capture.run_capture(remember=writer, **arguments)
                self.assertIn(result["capture_outcome"], {"invalid_input", "invalid_run_record"})
                self.assertEqual(writer.calls, [])

    def test_valid_noop_and_human_only_capture(self):
        writer = FakeWriter()
        noop = capture.run_capture(success_run(), remember=writer)
        self.assertEqual(noop["capture_outcome"], "ok")
        self.assertEqual(noop["eligible"], 0)
        self.assertEqual(noop["events"], [])
        self.assertEqual(writer.calls, [])

        human = capture.run_capture(
            human={"decision": "Use the existing writer", "decision_basis": "One writer is enough"},
            repository=REPO,
            task=TASK,
            remember=writer,
        )
        self.assertEqual(human["capture_outcome"], "ok")
        self.assertEqual(human["run_terminal"], None)
        self.assertEqual(human["eligible"], 1)
        self.assertEqual(len(writer.calls), 1)

    def test_long_scope_identity_is_preserved_exactly(self):
        repository = "repository/" + "r" * 500
        task = "task " + "t" * 500
        writer = FakeWriter()
        result = capture.run_capture(
            human={"decision": "Keep exact scope", "decision_basis": "Retrieval uses equality"},
            repository=repository,
            task=task,
            remember=writer,
        )
        self.assertEqual(result["repository"], repository)
        self.assertEqual(result["task"], task)
        self.assertEqual(writer.calls[0]["repository"], repository)
        self.assertEqual(writer.calls[0]["task"], task)

    def test_writer_field_mapping_and_real_signature_compatibility(self):
        incident_writer = FakeWriter()
        incident_event = capture.capture_events(eligible_run())[0]
        capture.run_capture(eligible_run(), remember=incident_writer)
        incident_call = incident_writer.calls[0]
        self.assertEqual(incident_call, {
            "repository": REPO,
            "task": TASK,
            "memory_type": "incident",
            "lesson": incident_event["lesson"],
            "recommended_action": None,
            "evidence": incident_event["evidence"],
            "verification": incident_event["verification"],
        })
        inspect.signature(phase2_preflight.run_remember).bind(**incident_call)

        correction_writer = FakeWriter()
        correction = {
            "human_correction": "Preserve the admitted working set.",
            "recommended_action": "Use the verified cache bound.",
        }
        capture.run_capture(success_run(), correction, remember=correction_writer)
        correction_call = correction_writer.calls[0]
        self.assertEqual(correction_call["recommended_action"], correction["recommended_action"])
        self.assertEqual(correction_call["lesson"], correction["human_correction"])
        inspect.signature(phase2_preflight.run_remember).bind(**correction_call)

    def test_write_failures_are_machine_readable_and_preserve_terminal(self):
        writers = (
            FakeWriter({"write_outcome": "failed", "error": "synthetic failure"}),
            lambda **_: (_ for _ in ()).throw(RuntimeError("writer unavailable")),
        )
        for writer in writers:
            with self.subTest(writer=type(writer).__name__):
                result = capture.run_capture(eligible_run(), remember=writer)
                self.assertEqual(result["capture_outcome"], "write_failed")
                self.assertEqual(result["run_terminal"], "verification_failed")
                self.assertEqual(result["events"][0]["write_outcome"], "failed")
                self.assertIn("error", result["events"][0])

    def test_approved_writer_and_no_competing_carrier(self):
        source = Path(capture.__file__).read_text(encoding="utf-8")
        default = inspect.signature(capture.run_capture).parameters["remember"].default
        self.assertIs(default, phase2_preflight.run_remember)
        self.assertIn("from phase2_preflight import run_remember", source)
        self.assertNotIn("phase3_loop", source)
        for forbidden in ("set_entity", "sqlite", "pickle", "shelve", "write_text"):
            self.assertNotIn(forbidden, source.lower())

    def test_phase2_identity_remains_the_idempotency_policy(self):
        first = capture._verification_failure_event(eligible_run())
        second = capture._verification_failure_event(copy.deepcopy(eligible_run()))
        first_key = phase2_preflight.experience_key(
            REPO, TASK, first["memory_type"], first["lesson"], first.get("recommended_action"),
        )
        second_key = phase2_preflight.experience_key(
            REPO, TASK, second["memory_type"], second["lesson"], second.get("recommended_action"),
        )
        changed = eligible_run()
        changed["planner_output"]["action"]["value"] = "201"
        changed_event = capture._verification_failure_event(changed)
        changed_key = phase2_preflight.experience_key(
            REPO, TASK, changed_event["memory_type"], changed_event["lesson"],
            changed_event.get("recommended_action"),
        )
        self.assertEqual(first_key, second_key)
        self.assertNotEqual(first_key, changed_key)

    def test_cli_stdin_and_invalid_input_emit_one_json_record(self):
        for argv, stdin in (([], ""), (["--run-record", "-"], json.dumps(success_run()))):
            with self.subTest(argv=argv), patch("sys.stdin", io.StringIO(stdin)), patch(
                "sys.stdout", new_callable=io.StringIO,
            ) as output:
                code = capture.main(argv)
                record = json.loads(output.getvalue())
                self.assertEqual(record["operation"], "capture")
                self.assertEqual(code, 0 if record["capture_outcome"] == "ok" else 2)

    def test_cli_eligible_capture_uses_injected_writer(self):
        writer = FakeWriter()
        with patch("sys.stdin", io.StringIO(json.dumps(eligible_run()))), patch(
            "sys.stdout", new_callable=io.StringIO,
        ) as output:
            code = capture.main(["--run-record", "-"], remember=writer)
        record = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(record["capture_outcome"], "ok")
        self.assertEqual(record["eligible"], 1)
        self.assertEqual(len(writer.calls), 1)


class Phase3ContractIntegrationTests(unittest.TestCase):
    @staticmethod
    def _preflight(repository, task, memory_enabled):
        return {
            "repository": repository,
            "task": task,
            "memory_enabled": memory_enabled,
            "preflight_ok": True,
            "conflict": False,
            "entries": [],
        }

    @staticmethod
    def _planner(value):
        return lambda _: {
            "status": "propose",
            "action": {
                "verb": "set_assignment",
                "target": "target.py",
                "symbol": "CACHE_MAXSIZE",
                "value": value,
            },
            "memory_refs": [],
        }

    def test_real_phase3_failure_captures_and_success_does_not(self):
        with tempfile.TemporaryDirectory() as root:
            failed = phase3_loop.run_loop(
                REPO, TASK, False,
                planner=self._planner("200"),
                preflight=self._preflight,
                workspace_root=root,
            )
            succeeded = phase3_loop.run_loop(
                REPO, TASK, False,
                planner=self._planner("320"),
                preflight=self._preflight,
                workspace_root=root,
            )

        writer = FakeWriter()
        captured = capture.run_capture(failed, remember=writer)
        noop = capture.run_capture(succeeded, remember=writer)
        self.assertEqual(failed["terminal"], "verification_failed")
        self.assertEqual(captured["capture_outcome"], "ok")
        self.assertEqual(captured["eligible"], 1)
        self.assertEqual(captured["events"][0]["memory_type"], "incident")
        self.assertEqual(
            writer.calls[0]["verification"],
            "check exit 1: AssertionError: hot working set was evicted",
        )
        self.assertEqual(succeeded["terminal"], "success")
        self.assertEqual(noop["eligible"], 0)
        self.assertEqual(len(writer.calls), 1)


if __name__ == "__main__":
    unittest.main()
