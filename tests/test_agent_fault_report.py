import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/agent_fault_report.py"
spec = importlib.util.spec_from_file_location("agent_fault_report", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def evidence():
    root = ET.Element("testsuites")
    for scenario in module.SCENARIOS:
        suite = ET.SubElement(root, "testsuite")
        for key in scenario["tests"]:
            classname, name = key.split("::")
            case = ET.SubElement(suite, "testcase", classname=classname, name=name, time="0.25")
            if scenario.get("measurements_required"):
                props = ET.SubElement(case, "properties")
                ET.SubElement(props, "property", name="easel_fault_metrics", value=json.dumps({
                    "prompt_submissions": 2, "duplicate_prompt_submissions": 0, "recovery_latency_seconds": 0.1}))
    return root


def convert(root):
    return module.report(ET.tostring(root), revision="example-revision")


def test_complete_report_is_repeatable_and_does_not_invent_metrics():
    root = evidence()
    result = convert(root)
    assert result == convert(root) and result["complete"] is True
    assert len(result["scenarios"]) == 4 and result["revision_verified"] is False
    assert result["scenarios"][0]["test_duration_seconds"] == 0.5
    for scenario in result["scenarios"]:
        assert scenario["model_calls"] is None
        assert scenario["duplicate_external_effects"] is None
        assert scenario["recovery_latency_seconds"] is None


@pytest.mark.parametrize("tag,status", [("failure", "failed"), ("error", "error"), ("skipped", "skipped")])
def test_failures_and_skips_are_not_passes_and_raw_details_are_omitted(tag, status):
    root = evidence()
    ET.SubElement(next(root.iter("testcase")), tag, message="private-tool-output").text = "private-token"
    result = convert(root)
    assert result["complete"] is False and result["scenarios"][0]["status"] == status
    assert "private-" not in json.dumps(result)


def test_missing_or_partial_evidence_is_explicitly_not_run():
    root = evidence()
    root[0].remove(root[0][0])
    result = convert(root)
    assert result["scenarios"][0]["status"] == "not_run"
    assert len(result["scenarios"][0]["missing_tests"]) == 1
    assert not module.report(b"<testsuites/>")["complete"]


def test_recovery_suite_requires_every_registered_variant():
    root = evidence()
    definitions = module.scenario_definitions("recovery")
    assert len({s["id"] for s in definitions}) == len(definitions)
    keys = [key for definition in definitions for key in definition["tests"]]
    assert len(set(keys)) == len(keys)
    assert not module.report(ET.tostring(root), suite="recovery")["complete"]
    for definition in definitions[len(module.SCENARIOS):]:
        suite = ET.SubElement(root, "testsuite")
        for key in definition["tests"]:
            classname, name = key.split("::")
            ET.SubElement(suite, "testcase", classname=classname, name=name)
    assert module.report(ET.tostring(root), suite="recovery")["complete"]
    root[-1].remove(root[-1][0])
    assert not module.report(ET.tostring(root), suite="recovery")["complete"]
    with pytest.raises(ValueError):
        module.report(ET.tostring(root), suite="unknown")


def test_repeated_case_cannot_hide_an_earlier_failure():
    root = evidence()
    duplicate = ET.SubElement(root[0], "testcase", **root[0][0].attrib)
    ET.SubElement(duplicate, "failure")
    assert convert(root)["scenarios"][0]["status"] == "failed"


def test_missing_duration_is_not_reported_as_zero():
    root = evidence()
    del root[0][0].attrib["time"]
    assert convert(root)["scenarios"][0]["test_duration_seconds"] is None
    assert module.report(b"<testsuites/>")["scenarios"][0]["test_duration_seconds"] is None


def test_missing_measurements_cannot_pass_and_samples_are_preserved():
    root = evidence()
    measured = next(case for case in root.iter("testcase") if case.find("properties") is not None)
    result = convert(root)
    samples = next(s["measurements"] for s in result["scenarios"] if s.get("measurements_required"))
    assert len(samples) == 2 and samples[0]["prompt_submissions"] == 2
    measured.remove(measured.find("properties"))
    result = convert(root)
    assert not result["complete"]
    assert any(s["status"] == "missing_measurements" for s in result["scenarios"])


@pytest.mark.parametrize("value", ["null", "{}", "broken", '{"secret":"PRIVATE"}',
    '{"prompt_submissions":true,"duplicate_prompt_submissions":0,"recovery_latency_seconds":1}',
    '{"prompt_submissions":2,"duplicate_prompt_submissions":3,"recovery_latency_seconds":1}',
    '{"prompt_submissions":2,"duplicate_prompt_submissions":0,"recovery_latency_seconds":NaN}'])
def test_invalid_measurements_are_rejected(value):
    root = evidence()
    prop = next(root.iter("property"))
    prop.set("value", value)
    with pytest.raises(ValueError):
        convert(root)


def test_duplicate_measurements_and_overflowing_durations_are_rejected():
    root = evidence()
    props = next(root.iter("properties"))
    ET.SubElement(props, "property", **props[0].attrib)
    with pytest.raises(ValueError):
        convert(root)
    root = evidence()
    for case in root[0]:
        case.set("time", "1e308")
    with pytest.raises(ValueError):
        convert(root)


@pytest.mark.parametrize("value", ["NaN", "inf", "-1", "invalid"])
def test_invalid_durations_are_rejected(value):
    root = evidence()
    root[0][0].set("time", value)
    with pytest.raises(ValueError):
        convert(root)


@pytest.mark.parametrize("payload", [b"broken", b"<unrelated/>", b'<!DOCTYPE x [<!ENTITY e "x">]><testsuites/>'])
def test_cli_rejects_invalid_input_without_emitting_a_success_report(tmp_path, payload):
    source = tmp_path / "input.xml"
    source.write_bytes(payload)
    result = subprocess.run([sys.executable, str(SCRIPT), str(source)], capture_output=True, text=True)
    assert result.returncode == 2 and not result.stdout
    assert source.read_bytes() == payload


@pytest.mark.parametrize("complete,expected", [(True, 0), (False, 1)])
def test_cli_status_agrees_with_its_report(tmp_path, complete, expected):
    source = tmp_path / "input.xml"
    source.write_bytes(ET.tostring(evidence()) if complete else b"<testsuites/>")
    result = subprocess.run([sys.executable, str(SCRIPT), str(source), "--revision", "abc"], capture_output=True, text=True)
    assert result.returncode == expected
    assert json.loads(result.stdout)["complete"] is complete


@pytest.mark.parametrize("source,code", [(b"broken", 2), (b"<testsuites/>", 1)])
def test_output_file_is_written_only_for_valid_input(tmp_path, source, code):
    junit, output = tmp_path / "in.xml", tmp_path / "out.json"
    junit.write_bytes(source)
    result = subprocess.run([sys.executable, str(SCRIPT), str(junit), "--output", str(output)],
                            capture_output=True, text=True)
    assert result.returncode == code and not result.stdout
    assert output.exists() == (code == 1)
    if output.exists():
        assert json.loads(output.read_text(encoding="utf-8"))["complete"] is False
