"""Offline flat-rate estimates for task reports; never a provider bill or spend limit."""
from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.model_tool_probe import decode

SCENARIOS = ("parameter_extraction", "tool_dependency", "unknown_outcome")
STATUSES = {"passed", "http_error", "transport_error", "timeout", "response_too_large",
            "invalid_response", "tool_contract_failed", "unexpected_tool_call", "answer_contract_failed"}


def rate(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,9})?", value):
        raise ValueError("Invalid flat rate")
    return Decimal(value)


def amount(value):
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")


def estimate(report, pricing):
    if (not isinstance(pricing, dict) or set(pricing) != {"schema_version", "version", "effective_date", "currency",
            "deployment_fingerprint", "input_per_million", "output_per_million"}
            or type(pricing["schema_version"]) is not int or pricing["schema_version"] != 1
            or not isinstance(pricing["version"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", pricing["version"])
            or not isinstance(pricing["currency"], str) or not re.fullmatch(r"[A-Z]{3}", pricing["currency"])
            or not isinstance(pricing["effective_date"], str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", pricing["effective_date"])):
        raise ValueError("Invalid pricing metadata")
    date.fromisoformat(pricing["effective_date"])
    input_rate, output_rate = rate(pricing["input_per_million"]), rate(pricing["output_per_million"])
    fingerprint = pricing["deployment_fingerprint"]
    if (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            or not isinstance(report, dict) or type(report.get("schema_version")) is not int or report.get("schema_version") != 1
            or report.get("suite") != "synthetic-agent-tasks-v1"
            or report.get("deployment_fingerprint") != fingerprint):
        raise ValueError("Report deployment does not match pricing")
    samples, tasks = report.get("samples_per_scenario"), report.get("tasks")
    if (type(samples) is not int or not 1 <= samples <= 5 or not isinstance(tasks, list)
            or len(tasks) > samples * len(SCENARIOS) or type(report.get("complete")) is not bool):
        raise ValueError("Invalid report shape")
    seen, output = set(), []
    with localcontext() as context:
        context.prec = 50
        for task in tasks:
            scenario, sample, status = task.get("scenario"), task.get("sample"), task.get("status")
            if (scenario not in SCENARIOS or type(sample) is not int or not 1 <= sample <= samples
                    or (scenario, sample) in seen or status not in STATUSES):
                raise ValueError("Invalid or duplicate task")
            seen.add((scenario, sample))
            maximum = 3 if scenario == "tool_dependency" else 2
            started, received = task.get("requests_started"), task.get("responses_received")
            if (type(started) is not int or not 1 <= started <= maximum or type(received) is not int
                    or not 0 <= received <= started or status == "passed" and (started != maximum or received != started)):
                raise ValueError("Invalid request accounting")
            values = [task.get(key) for key in ("prompt_tokens", "completion_tokens")]
            if any(value is not None and (type(value) is not int or not 0 <= value <= 10**12) for value in values):
                raise ValueError("Invalid token accounting")
            cost = None
            # Incomplete responses cannot support a complete usage total, even if a malformed report says otherwise.
            if received == started and all(value is not None for value in values):
                cost = (values[0] * input_rate + values[1] * output_rate) / Decimal(1000000)
            output.append({"scenario": scenario, "sample": sample, "status": status,
                           "estimated_cost": amount(cost) if cost is not None else None})
        if report["complete"] != (len(seen) == samples * len(SCENARIOS)):
            raise ValueError("Inconsistent report completeness")

        def total(rows):
            return (amount(sum((Decimal(row["estimated_cost"]) for row in rows), Decimal(0)))
                    if all(row["estimated_cost"] is not None for row in rows) else None)

        known = [row for row in output if row["estimated_cost"] is not None]
        failed = [row for row in output if row["status"] != "passed"]
        return {"schema_version": 1, "kind": "declared_flat_rate_estimate", "pricing": dict(pricing),
                "source_complete": report["complete"], "tasks": output,
                "priced_tasks": len(known), "unknown_cost_tasks": len(output) - len(known),
                "known_subtotal_estimate": total(known), "attempted_total_estimate": total(output),
                "planned_total_estimate": total(output) if report["complete"] else None,
                "failed_task_estimate": total(failed), "failed_tasks": len(failed),
                "is_provider_bill": False}


def read_json(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Input too large")
    return decode(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--pricing", required=True)
    args = parser.parse_args()
    try:
        output = estimate(read_json(args.report), read_json(args.pricing))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        print("Invalid report or pricing configuration; no estimate produced.", file=sys.stderr)
        return 2
    print(json.dumps(output, sort_keys=True, allow_nan=False))
    return 0 if output["planned_total_estimate"] is not None else 1


if __name__ == "__main__":
    sys.exit(main())
