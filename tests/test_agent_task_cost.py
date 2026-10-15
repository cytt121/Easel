import asyncio
import copy
import json
import subprocess
import sys

import httpx
import pytest

from scripts import agent_task_cost as cost
from scripts import agent_task_eval as evaluation


def fixtures():
    fingerprint = "a" * 64
    pricing = {"schema_version": 1, "version": "fixture-v1", "effective_date": "2026-10-15", "currency": "USD",
               "deployment_fingerprint": fingerprint, "input_per_million": "2.5", "output_per_million": "10"}
    rows = [{"scenario": name, "sample": 1, "status": "passed", "requests_started": requests,
             "responses_received": requests, "prompt_tokens": 100, "completion_tokens": 20}
            for name, requests in zip(cost.SCENARIOS, (2, 3, 2))]
    report = {"schema_version": 1, "suite": "synthetic-agent-tasks-v1", "deployment_fingerprint": fingerprint,
              "samples_per_scenario": 1, "complete": True, "tasks": rows}
    return report, pricing


def test_exact_per_task_total_and_failed_task_cost_without_mutating_input():
    report, pricing = fixtures()
    report["tasks"][1]["status"] = "answer_contract_failed"
    before = copy.deepcopy(report)
    result = cost.estimate(report, pricing)
    assert result["tasks"][0]["estimated_cost"] == "0.00045"
    assert result["attempted_total_estimate"] == result["planned_total_estimate"] == "0.00135"
    assert result["failed_task_estimate"] == "0.00045" and result["failed_tasks"] == 1
    assert not result["is_provider_bill"] and report == before


def test_missing_usage_is_unknown_not_zero_or_partial_total():
    report, pricing = fixtures()
    report["tasks"][1].update(status="timeout", prompt_tokens=None)
    result = cost.estimate(report, pricing)
    assert result["known_subtotal_estimate"] == "0.0009"
    assert result["planned_total_estimate"] is None and result["attempted_total_estimate"] is None
    assert result["failed_task_estimate"] is None and result["unknown_cost_tasks"] == 1


def test_partial_responses_do_not_prove_complete_usage():
    report, pricing = fixtures()
    report["tasks"][0].update(status="timeout", responses_received=1)
    assert cost.estimate(report, pricing)["tasks"][0]["estimated_cost"] is None


def test_incomplete_batch_cannot_claim_planned_total():
    report, pricing = fixtures()
    report.update(complete=False, tasks=report["tasks"][:1])
    result = cost.estimate(report, pricing)
    assert result["attempted_total_estimate"] == "0.00045" and result["planned_total_estimate"] is None


def test_zero_prices_still_require_complete_usage():
    report, pricing = fixtures()
    pricing.update(input_per_million="0", output_per_million="0.000")
    assert cost.estimate(report, pricing)["planned_total_estimate"] == "0"
    report["tasks"][0]["prompt_tokens"] = None
    assert cost.estimate(report, pricing)["planned_total_estimate"] is None


def test_tiny_cost_is_not_rounded_to_zero_and_large_values_remain_exact():
    report, pricing = fixtures()
    pricing.update(input_per_million="0.000000001", output_per_million="0")
    for row in report["tasks"]:
        row.update(prompt_tokens=1, completion_tokens=0)
    assert cost.estimate(report, pricing)["planned_total_estimate"] == "0.000000000000003"
    pricing.update(input_per_million="999999.999999999", output_per_million="999999.999999999")
    for row in report["tasks"]:
        row.update(prompt_tokens=10**12, completion_tokens=10**12)
    assert cost.estimate(report, pricing)["planned_total_estimate"] == "5999999999999.994"


def test_no_attempts_is_not_a_zero_cost_completed_plan():
    report, pricing = fixtures()
    report.update(tasks=[], complete=False)
    result = cost.estimate(report, pricing)
    assert result["attempted_total_estimate"] == "0"
    assert result["planned_total_estimate"] is None


@pytest.mark.parametrize("value", [True, 1.2, "-1", "NaN", "Infinity", "1e2", "1000000", "0.0000000001"])
def test_invalid_or_unbounded_rates_rejected(value):
    report, pricing = fixtures()
    pricing["input_per_million"] = value
    with pytest.raises(ValueError):
        cost.estimate(report, pricing)


@pytest.mark.parametrize("value", [True, -1, 1.2, "100", 10**12 + 1])
def test_invalid_token_values_rejected(value):
    report, pricing = fixtures()
    report["tasks"][0]["prompt_tokens"] = value
    with pytest.raises(ValueError):
        cost.estimate(report, pricing)


@pytest.mark.parametrize("change", ["fingerprint", "missing_fingerprint", "duplicate", "complete", "date", "version"])
def test_price_binding_and_report_consistency(change):
    report, pricing = fixtures()
    if change == "fingerprint":
        pricing["deployment_fingerprint"] = "b" * 64
    elif change == "missing_fingerprint":
        del report["deployment_fingerprint"]
    elif change == "duplicate":
        report["tasks"][1] = report["tasks"][0].copy()
    elif change == "complete":
        report["complete"] = False
    elif change == "date":
        pricing["effective_date"] = "2026-02-30"
    else:
        pricing["version"] = ""
    with pytest.raises(ValueError):
        cost.estimate(report, pricing)


def test_deployment_fingerprint_separates_models_and_endpoints_without_exposing_them():
    async def run(base, model):
        return await evaluation.evaluate_suite(base, "private-key", model,
            transport=httpx.MockTransport(lambda _: httpx.Response(429)))

    first = asyncio.run(run("https://first.invalid/v1", "model-a"))
    second = asyncio.run(run("https://first.invalid/v1", "model-b"))
    third = asyncio.run(run("https://second.invalid/v1", "model-a"))
    assert len({r["deployment_fingerprint"] for r in (first, second, third)}) == 3
    assert all(secret not in json.dumps(first) for secret in ("private-key", "first.invalid", "model-a"))
    _, pricing = fixtures()
    pricing["deployment_fingerprint"] = first["deployment_fingerprint"]
    assert cost.estimate(first, pricing)["attempted_total_estimate"] is None


def test_cli_is_offline_and_exit_code_marks_unknown_cost(tmp_path):
    report, pricing = fixtures()
    report_path, pricing_path = tmp_path / "report.json", tmp_path / "pricing.json"
    pricing_path.write_text(json.dumps(pricing), encoding="utf-8")
    report_path.write_text(json.dumps(report), encoding="utf-8")
    args = [sys.executable, cost.__file__, "--report", str(report_path), "--pricing", str(pricing_path)]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0 and json.loads(result.stdout)["planned_total_estimate"] == "0.00135"
    report["tasks"][0]["prompt_tokens"] = None
    report_path.write_text(json.dumps(report), encoding="utf-8")
    assert subprocess.run(args, capture_output=True).returncode == 1


def test_duplicate_json_and_oversized_inputs_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"a":1,"a":2}', encoding="utf-8")
    with pytest.raises(ValueError):
        cost.read_json(path)
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ValueError):
        cost.read_json(path)
