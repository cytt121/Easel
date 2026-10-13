"""Summarize deterministic agent fault regressions from pytest JUnit XML.

Does not execute tools, models or tests. Missing evidence is never a pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


SCENARIOS = (
    {
        "id": "resume-identity", "fault_stage": "native session resume",
        "expected": "reject a replacement native identity",
        "forbidden_effect": "silently replace existing conversation history",
        "tests": ["tests.test_agent_harnesses::test_codex_rejects_replacement_thread_on_resume",
                  "tests.test_agent_harnesses::test_acp_rejects_replacement_session_on_resume"],
    },
    {
        "id": "pooled-session-ownership", "fault_stage": "concurrent pooled turn and cancellation",
        "expected": "reject concurrent ownership and clean up the cancelled process",
        "forbidden_effect": "reuse a busy or cancelled process for another turn",
        "tests": ["tests.test_agent_harnesses::test_pooled_connections_reject_concurrent_turn_and_clean_cancelled_process"],
    },
    {
        "id": "idle-process-recovery", "fault_stage": "native process death between turns",
        "expected": "resume the same native session in a new process",
        "forbidden_effect": "duplicate prompt submissions or replace session identity",
        "measurements_required": True,
        "tests": ["tests.test_agent_fault_measurements::test_idle_process_death_recovery[codex]",
                  "tests.test_agent_fault_measurements::test_idle_process_death_recovery[acp]"],
    },
    {
        "id": "native-fatal-event", "fault_stage": "fatal native protocol event",
        "expected": "surface the failure without waiting for the full turn timeout",
        "forbidden_effect": "report success or automatically repeat the prompt",
        "tests": ["tests.test_agent_harnesses::test_codex_fatal_event_does_not_wait_until_timeout"],
    },
)


def measurements(case):
    values = [prop.get("value", "") for prop in case.findall("properties/property")
              if prop.get("name") == "easel_fault_metrics"]
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("Duplicate measurement record")
    value = json.loads(values[0])
    if not isinstance(value, dict) or set(value) != {
            "prompt_submissions", "duplicate_prompt_submissions", "recovery_latency_seconds"}:
        raise ValueError("Unexpected measurement fields")
    for key in ("prompt_submissions", "duplicate_prompt_submissions"):
        if type(value[key]) is not int or not 0 <= value[key] <= 1000000:
            raise ValueError("Invalid counter")
    elapsed = value["recovery_latency_seconds"]
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not 0 <= elapsed <= 86400:
        raise ValueError("Invalid recovery duration")
    if value["duplicate_prompt_submissions"] > value["prompt_submissions"]:
        raise ValueError("Inconsistent counters")
    return value


def scenario_definitions(suite):
    if suite == "core":
        return SCENARIOS
    if suite == "recovery":
        return (*SCENARIOS, *json.loads(Path(__file__).with_name("agent_recovery_scenarios.json").read_text(encoding="utf-8")))
    raise ValueError("Unknown scenario suite")


def report(source: bytes, *, revision: str | None = None, suite: str = "core") -> dict:
    definitions = scenario_definitions(suite)
    if len(source) > 32 * 1024 * 1024:
        raise ValueError("JUnit input exceeds 32 MiB")
    # pytest writes UTF-8. Do not expand external/internal document entities.
    text = source.decode("utf-8-sig")
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ValueError("JUnit document declarations are unsupported")
    root = ET.fromstring(text)
    if root.tag not in ("testsuite", "testsuites"):
        raise ValueError("Expected a JUnit testsuite or testsuites root")
    evidence: dict[str, list[tuple[str, float | None]]] = {}
    measured = {}
    for case in root.iter("testcase"):
        key = f"{case.get('classname', '')}::{case.get('name', '')}"
        if not any(key in scenario["tests"] for scenario in definitions):
            continue
        duration = float(case.get("time")) if "time" in case.attrib else None
        if duration is not None and (not math.isfinite(duration) or duration < 0):
            raise ValueError("Invalid test duration")
        status = next((state for tag, state in (("error", "error"), ("failure", "failed"),
                                                ("skipped", "skipped")) if case.find(tag) is not None), "passed")
        evidence.setdefault(key, []).append((status, duration))
        measured.setdefault(key, []).append(measurements(case))
    scenarios = []
    for definition in definitions:
        missing = [key for key in definition["tests"] if key not in evidence]
        cases = [item for key in definition["tests"] for item in evidence.get(key, [])]
        counts = {state: sum(result == state for result, _ in cases)
                  for state in ("passed", "failed", "error", "skipped")}
        status = ("error" if counts["error"] else "failed" if counts["failed"] else
                  "not_run" if missing else "skipped" if counts["skipped"] else "passed")
        samples = [{"test": key, **value} for key in definition["tests"]
                   for value in measured.get(key, []) if value is not None]
        if definition.get("measurements_required") and len(samples) != len(cases) and status == "passed":
            status = "missing_measurements"
        duration = sum(seconds for _, seconds in cases) if cases and all(
            seconds is not None for _, seconds in cases) else None
        if duration is not None and not math.isfinite(duration):
            raise ValueError("Invalid aggregate duration")
        scenarios.append({**definition, "status": status, "case_counts": counts,
                          "missing_tests": missing,
                          "measurements": samples,
                          "test_duration_seconds": round(duration, 6) if duration is not None else None,
                          "model_calls": None, "duplicate_external_effects": None,
                          "recovery_latency_seconds": None})
    return {"schema_version": 1, "kind": "deterministic_fault_regression",
            "suite": suite,
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "revision_label": revision, "revision_verified": False,
            "complete": all(item["status"] == "passed" for item in scenarios),
            "scenarios": scenarios}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=Path, help="pytest --junitxml output (UTF-8)")
    parser.add_argument("--revision", help="revision label recorded by the test runner; not independently verified")
    parser.add_argument("--suite", choices=("core", "recovery"), default="core",
                        help="recovery also requires the pending recovery/governance regression cases")
    parser.add_argument("--output", type=Path, help="write UTF-8 JSON after input validation")
    args = parser.parse_args(argv)
    try:
        with args.junit.open("rb") as handle:
            payload = report(handle.read(32 * 1024 * 1024 + 1), revision=args.revision, suite=args.suite)
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output:
            args.output.write_text(rendered + "\n", encoding="utf-8")
    except (OSError, ValueError, ET.ParseError):
        print("Cannot read a supported, valid JUnit report; preserve the original evidence.", file=sys.stderr)
        return 2
    if not args.output:
        print(rendered)
    return 0 if payload["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
