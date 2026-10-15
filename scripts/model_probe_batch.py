"""Opt-in bounded repetitions of the model tool-contract probe, not an Agent benchmark."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.model_tool_probe import endpoint, probe
from easel.timeouts import TIMEOUT_MODEL_TOOL_PROBE


async def evaluate(base_url, api_key, model, *, samples=3, max_tokens=256,
                   total_timeout=180, transport=None):
    """Sequential samples; each gets a fresh in-memory receipt and at most two requests."""
    endpoint(base_url)
    if (not api_key or not model or type(samples) is not int or not 1 <= samples <= 20
            or type(max_tokens) is not int or not 1 <= max_tokens <= 1024
            or type(total_timeout) not in (int, float)
            or not math.isfinite(total_timeout) or not 0 < total_timeout <= 600):
        raise ValueError("Invalid batch configuration")
    started = time.monotonic()
    deadline = started + total_timeout
    results = []
    stop_reason = None
    for _ in range(samples):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stop_reason = "total_timeout"
            break
        result = await probe(base_url, api_key, model, max_tokens=max_tokens,
                             timeout=min(remaining, TIMEOUT_MODEL_TOOL_PROBE), transport=transport)
        results.append(result)
        # Avoid repeatedly charging against an unavailable or rejecting endpoint.
        if result["status"] in {"http_error", "transport_error", "timeout"}:
            stop_reason = result["status"]
            break
    counts = Counter(item["status"] for item in results)
    latencies = sorted(item["elapsed_seconds"] for item in results)
    completed = len(results) == samples
    report = {
        "schema_version": 1, "scenario": "model-tool-contract-batch-v1",
        "samples_planned": samples, "samples_attempted": len(results),
        "samples_not_run": samples - len(results), "complete": completed,
        "stop_reason": stop_reason, "status_counts": dict(sorted(counts.items())),
        "passed": counts["passed"],
        "pass_rate_attempted": counts["passed"] / len(results) if results else None,
        "max_requests": 2 * samples, "max_completion_tokens_per_request": max_tokens,
        "total_timeout_seconds": total_timeout,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "latency_seconds": {
            "population": "all_attempted_samples", "count": len(latencies),
            "median": statistics.median(latencies) if latencies else None,
            "p95_nearest_rank": latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else None,
        },
        "cost": None,
    }
    for key in ("requests_started", "responses_received", "fixture_tool_calls"):
        report[key] = sum(item[key] for item in results)
    for key in ("prompt_tokens", "completion_tokens"):
        report[key] = (sum(item[key] for item in results)
                       if results and all(item[key] is not None for item in results) else None)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly permit model requests")
    parser.add_argument("--model", required=True)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--total-timeout", type=float, default=180)
    args = parser.parse_args()
    if not args.live:
        parser.error("Live requests require --live")
    try:
        report = asyncio.run(evaluate(os.environ["EASEL_PROBE_BASE_URL"],
                                      os.environ["EASEL_PROBE_API_KEY"], args.model,
                                      samples=args.samples, max_tokens=args.max_tokens,
                                      total_timeout=args.total_timeout))
    except (KeyError, ValueError):
        print("Invalid configuration; check limits and EASEL_PROBE environment variables.", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["complete"] and report["passed"] == report["samples_planned"] else 1


if __name__ == "__main__":
    sys.exit(main())
