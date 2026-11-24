"""Analyze CloudWatch Logs and AWS/Lambda/Web metrics for the FastAPI benchmark,
separating cold starts, warm invocations, latency percentiles, and cost projections.
"""

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import statistics

import boto3

# AWS Lambda pricing in eu-west-1 (USD, excluding Always Free Tier).
CLASSIC_ARM64_GB_SECOND_USD = 0.0000133334
CLASSIC_REQUEST_USD = 0.20 / 1_000_000

WEB_ACTIVE_VCPU_SECOND_USD = 0.0000276944
WEB_MEMORY_GB_SECOND_USD = 0.0000036667
WEB_MEMORY_GB_FIXED = 2.0
WEB_REQUEST_USD = 0.30 / 1_000_000

REPORT_RE = re.compile(
    r"REPORT RequestId:\s+(?P<request_id>[a-f0-9-]+)\s+"
    r"Duration:\s+(?P<duration_ms>[\d.]+)\s+ms\s+"
    r"Billed Duration:\s+(?P<billed_ms>\d+)\s+ms\s+"
    r"Memory Size:\s+(?P<memory_mb>\d+)\s+MB\s+"
    r"Max Memory Used:\s+(?P<max_memory_mb>\d+)\s+MB"
    r"(?:\s+Init Duration:\s+(?P<init_ms>[\d.]+)\s+ms)?"
)


def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 string into an aware UTC datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def percentile(sorted_values: list[float], pct: float) -> float:
    """Compute linear-interpolated percentile from a sorted list of floats."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * (pct / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return sorted_values[int(rank)]
    weight = rank - low
    return sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight


def summarize_numbers(values: list[float]) -> dict:
    """Compute count, min, avg, p50, p95, p99, max for a numeric list."""
    if not values:
        return {"count": 0, "min": 0.0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    s = sorted(values)
    return {
        "count": len(s),
        "min": round(s[0], 2),
        "avg": round(statistics.mean(s), 2),
        "p50": round(percentile(s, 50), 2),
        "p95": round(percentile(s, 95), 2),
        "p99": round(percentile(s, 99), 2),
        "max": round(s[-1], 2),
    }


def fetch_log_events(logs_client, log_group: str, start_dt: datetime, end_dt: datetime) -> list[dict]:
    """Fetch all CloudWatch log events inside [start_dt, end_dt]."""
    events = []
    kwargs = {
        "logGroupName": log_group,
        "startTime": int(start_dt.timestamp() * 1000),
        "endTime": int(end_dt.timestamp() * 1000),
    }
    while True:
        resp = logs_client.filter_log_events(**kwargs)
        events.extend(resp.get("events", []))
        next_token = resp.get("nextToken")
        if not next_token:
            break
        kwargs["nextToken"] = next_token
    return events


def analyze_classic_logs(events: list[dict]) -> dict:
    """Parse REPORT lines from classic Lambda logs, separating cold starts and warm runs."""
    reports = []
    for ev in events:
        match = REPORT_RE.search(ev["message"])
        if not match:
            continue
        reports.append(
            {
                "request_id": match.group("request_id"),
                "duration_ms": float(match.group("duration_ms")),
                "billed_ms": int(match.group("billed_ms")),
                "memory_mb": int(match.group("memory_mb")),
                "max_memory_mb": int(match.group("max_memory_mb")),
                "init_ms": float(match.group("init_ms")) if match.group("init_ms") else 0.0,
            }
        )

    cold = [r for r in reports if r["init_ms"] > 0]
    warm = [r for r in reports if r["init_ms"] == 0]

    total_billed_ms = sum(r["billed_ms"] for r in reports)
    memory_mb = reports[0]["memory_mb"] if reports else 512
    memory_gb = memory_mb / 1024.0
    total_gb_seconds = (total_billed_ms / 1000.0) * memory_gb
    compute_cost = total_gb_seconds * CLASSIC_ARM64_GB_SECOND_USD
    request_cost = len(reports) * CLASSIC_REQUEST_USD

    return {
        "invocations": len(reports),
        "memory_mb": memory_mb,
        "max_memory_used_mb": max((r["max_memory_mb"] for r in reports), default=0),
        "cold_starts": {
            "count": len(cold),
            "init_duration_ms": summarize_numbers([r["init_ms"] for r in cold]),
            "handler_duration_ms": summarize_numbers([r["duration_ms"] for r in cold]),
            "billed_duration_ms": summarize_numbers([float(r["billed_ms"]) for r in cold]),
        },
        "warm_invocations": {
            "count": len(warm),
            "handler_duration_ms": summarize_numbers([r["duration_ms"] for r in warm]),
            "billed_duration_ms": summarize_numbers([float(r["billed_ms"]) for r in warm]),
        },
        "all_invocations": {
            "handler_duration_ms": summarize_numbers([r["duration_ms"] for r in reports]),
            "billed_duration_ms": summarize_numbers([float(r["billed_ms"]) for r in reports]),
        },
        "total_duration_ms": round(sum(r["duration_ms"] for r in reports), 2),
        "total_billed_ms": total_billed_ms,
        "total_gb_seconds": round(total_gb_seconds, 4),
        "compute_cost_usd": round(compute_cost, 8),
        "request_cost_usd": round(request_cost, 8),
        "total_cost_usd": round(compute_cost + request_cost, 8),
    }


def analyze_web_logs(events: list[dict]) -> dict:
    """Parse structured JSON logs from Lambda Web Function for init reports and app timings."""
    instances = set()
    init_reports = []
    app_durations = []

    for ev in events:
        msg = ev["message"].strip()
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            continue

        ev_type = data.get("type")
        if ev_type == "platform.initStart":
            inst = data.get("record", {}).get("instanceId")
            if inst:
                instances.add(inst)
        elif ev_type == "platform.initReport":
            rec = data.get("record", {})
            inst = rec.get("instanceId")
            if inst:
                instances.add(inst)
            init_reports.append(
                {
                    "instance_id": inst,
                    "metrics": rec.get("metrics", {}),
                }
            )
        elif "message" in data:
            try:
                inner = json.loads(data["message"])
                if inner.get("event") == "dummy_request" and "execution_time_ms" in inner:
                    app_durations.append(float(inner["execution_time_ms"]))
            except (TypeError, json.JSONDecodeError):
                pass

    init_durations = [float(r["metrics"].get("totalDurationMs", 0.0)) for r in init_reports]
    init_cpus = [float(r["metrics"].get("totalCpuTimeMs", 0.0)) for r in init_reports]

    return {
        "unique_environments": len(instances),
        "instance_ids": sorted(instances),
        "cold_starts_count": len(init_reports),
        "init_duration_ms": summarize_numbers(init_durations),
        "init_cpu_ms": summarize_numbers(init_cpus),
        "init_reports": init_reports,
        "app_logged_requests": len(app_durations),
        "app_execution_time_ms": summarize_numbers(app_durations),
    }


def fetch_web_metrics(
    cw_client,
    function_name: str,
    endpoint_name: str,
    start_dt: datetime,
    end_dt: datetime,
    tail_minutes: int = 5,
) -> dict:
    """Query AWS/Lambda/Web metrics aligned to exact 60-second buckets."""
    aligned_start = start_dt.replace(second=0, microsecond=0)
    aligned_end = end_dt.replace(second=0, microsecond=0) + timedelta(minutes=tail_minutes)

    fn_dims = [{"Name": "FunctionName", "Value": function_name}]
    ep_dims = fn_dims + [{"Name": "EndpointName", "Value": endpoint_name}]

    def get_stat(metric_name: str, dims: list[dict], stat: str) -> list[dict]:
        resp = cw_client.get_metric_statistics(
            Namespace="AWS/Lambda/Web",
            MetricName=metric_name,
            Dimensions=dims,
            StartTime=aligned_start,
            EndTime=aligned_end,
            Period=60,
            Statistics=[stat],
        )
        return sorted(resp.get("Datapoints", []), key=lambda d: d["Timestamp"])

    cpu_pts = get_stat("EnvironmentCPUTime", fn_dims, "Sum")
    active_pts = get_stat("EnvironmentActiveTime", fn_dims, "Sum")
    mem_pts = get_stat("EnvironmentMaxMemoryUsage", fn_dims, "Maximum")
    conc_pts = get_stat("EnvironmentConcurrentRequests", fn_dims, "Maximum")
    req_pts = get_stat("ExecutedRequests", ep_dims, "Sum")

    cpu_ms = sum(p["Sum"] for p in cpu_pts)
    active_ms = sum(p["Sum"] for p in active_pts)
    executed_requests = int(sum(p["Sum"] for p in req_pts))
    max_memory_mb = max((p["Maximum"] for p in mem_pts), default=0.0)
    max_concurrent = max((p["Maximum"] for p in conc_pts), default=0.0)

    vcpu_seconds = cpu_ms / 1000.0
    env_active_seconds = active_ms / 1000.0
    memory_gb_seconds = env_active_seconds * WEB_MEMORY_GB_FIXED

    cpu_cost = vcpu_seconds * WEB_ACTIVE_VCPU_SECOND_USD
    mem_cost = memory_gb_seconds * WEB_MEMORY_GB_SECOND_USD
    req_cost = executed_requests * WEB_REQUEST_USD

    return {
        "window_utc": [aligned_start.isoformat(), aligned_end.isoformat()],
        "executed_requests": executed_requests,
        "cpu_time_ms": round(cpu_ms, 2),
        "vcpu_seconds": round(vcpu_seconds, 4),
        "environment_active_time_ms": round(active_ms, 2),
        "environment_active_seconds": round(env_active_seconds, 4),
        "memory_gb_seconds": round(memory_gb_seconds, 4),
        "max_memory_usage_mb": round(max_memory_mb, 2),
        "max_concurrent_requests": round(max_concurrent, 2),
        "cpu_cost_usd": round(cpu_cost, 8),
        "memory_cost_usd": round(mem_cost, 8),
        "request_cost_usd": round(req_cost, 8),
        "total_cost_usd": round(cpu_cost + mem_cost + req_cost, 8),
        "datapoints": {
            "EnvironmentCPUTime": [
                {"ts": p["Timestamp"].isoformat(), "sum_ms": p["Sum"]} for p in cpu_pts
            ],
            "EnvironmentActiveTime": [
                {"ts": p["Timestamp"].isoformat(), "sum_ms": p["Sum"]} for p in active_pts
            ],
            "ExecutedRequests": [
                {"ts": p["Timestamp"].isoformat(), "sum": p["Sum"]} for p in req_pts
            ],
        },
    }


def build_monthly_projections(
    classic_avg_billed_s: float,
    web_cpu_s_per_req: float,
    web_sustained_active_s_per_req: float,
    web_burst_active_s_per_req: float,
) -> list[dict]:
    """Project monthly costs across 10k, 100k, and 1M requests in eu-west-1."""
    rows = []
    for volume in (10_000, 100_000, 1_000_000):
        row = {"monthly_requests": volume}
        for mem_mb in (256, 512, 1024, 2048):
            mem_gb = mem_mb / 1024.0
            cost = volume * (
                classic_avg_billed_s * mem_gb * CLASSIC_ARM64_GB_SECOND_USD + CLASSIC_REQUEST_USD
            )
            row[f"classic_{mem_mb}mb_usd"] = round(cost, 2)

        row["web_sustained_50x_usd"] = round(
            volume
            * (
                web_cpu_s_per_req * WEB_ACTIVE_VCPU_SECOND_USD
                + web_sustained_active_s_per_req * WEB_MEMORY_GB_FIXED * WEB_MEMORY_GB_SECOND_USD
                + WEB_REQUEST_USD
            ),
            2,
        )
        row["web_burst_with_keepalive_usd"] = round(
            volume
            * (
                web_cpu_s_per_req * WEB_ACTIVE_VCPU_SECOND_USD
                + web_burst_active_s_per_req * WEB_MEMORY_GB_FIXED * WEB_MEMORY_GB_SECOND_USD
                + WEB_REQUEST_USD
            ),
            2,
        )
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description="Analyze CloudWatch logs and metrics for FastAPI benchmark.")
    parser.add_argument("--benchmark-file", default="results/benchmark_results.json")
    parser.add_argument("--profile", default="cuenta-personal")
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--output", default="results/cost_analysis.json")
    args = parser.parse_args()

    bench = json.loads(Path(args.benchmark_file).read_text(encoding="utf-8"))
    meta = bench["metadata"]
    events_phase = bench["phases"]["events"]
    web_phase = bench["phases"]["lambdaweb"]

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    logs_client = session.client("logs")
    cw_client = session.client("cloudwatch")

    # 1. Classic Lambda + LWA CloudWatch Logs (filtered strictly to benchmark window)
    ev_start = parse_iso_utc(events_phase["start_utc"]) - timedelta(seconds=2)
    ev_end = parse_iso_utc(events_phase["end_utc"]) + timedelta(seconds=20)
    ev_log_events = fetch_log_events(logs_client, meta["events_log_group"], ev_start, ev_end)
    classic_analysis = analyze_classic_logs(ev_log_events)

    # 2. Lambda Web Function CloudWatch Logs + AWS/Lambda/Web Metrics
    web_start = parse_iso_utc(web_phase["start_utc"])
    web_end = parse_iso_utc(web_phase["end_utc"])
    web_log_events = fetch_log_events(
        logs_client,
        meta["web_log_group"],
        web_start - timedelta(seconds=5),
        web_end + timedelta(minutes=5),
    )
    web_logs = analyze_web_logs(web_log_events)
    web_metrics = fetch_web_metrics(
        cw_client, meta["web_function_name"], "default", web_start, web_end, tail_minutes=6
    )

    # Separate active load memory cost vs full keep-alive memory cost for Web Functions
    web_reqs = max(web_metrics["executed_requests"] or web_phase["summary"]["success_count"], 1)
    env_count = max(web_logs["unique_environments"], 1)
    wall_s = web_phase["summary"]["wall_time_ms"] / 1000.0
    active_only_env_s = wall_s * env_count
    active_only_mem_cost = active_only_env_s * WEB_MEMORY_GB_FIXED * WEB_MEMORY_GB_SECOND_USD
    active_only_total_cost = (
        web_metrics["cpu_cost_usd"] + active_only_mem_cost + web_metrics["request_cost_usd"]
    )

    # 3. Client-side cold vs warm latency separation
    ev_runs = events_phase["summary"]
    web_runs = web_phase["summary"]

    # 4. Monthly projections
    classic_avg_billed_s = classic_analysis["all_invocations"]["billed_duration_ms"]["avg"] / 1000.0
    web_cpu_s_per_req = web_metrics["vcpu_seconds"] / web_reqs
    web_sustained_active_s_per_req = active_only_env_s / web_reqs
    web_burst_active_s_per_req = web_metrics["environment_active_seconds"] / web_reqs

    projections = build_monthly_projections(
        classic_avg_billed_s=classic_avg_billed_s,
        web_cpu_s_per_req=web_cpu_s_per_req,
        web_sustained_active_s_per_req=web_sustained_active_s_per_req,
        web_burst_active_s_per_req=web_burst_active_s_per_req,
    )

    output = {
        "pricing_eu_west_1_usd": {
            "classic_arm64_gb_second": CLASSIC_ARM64_GB_SECOND_USD,
            "classic_request": CLASSIC_REQUEST_USD,
            "web_active_vcpu_second": WEB_ACTIVE_VCPU_SECOND_USD,
            "web_memory_gb_second": WEB_MEMORY_GB_SECOND_USD,
            "web_fixed_memory_gb": WEB_MEMORY_GB_FIXED,
            "web_request": WEB_REQUEST_USD,
        },
        "client_benchmark_summary": {
            "events": ev_runs,
            "lambdaweb": web_runs,
        },
        "classic_events": classic_analysis,
        "lambda_web": {
            "logs": web_logs,
            "metrics": web_metrics,
            "active_load_only_estimate": {
                "environments": env_count,
                "wall_time_seconds": round(wall_s, 3),
                "env_active_seconds": round(active_only_env_s, 3),
                "memory_gb_seconds": round(active_only_env_s * WEB_MEMORY_GB_FIXED, 4),
                "memory_cost_usd": round(active_only_mem_cost, 8),
                "total_cost_usd": round(active_only_total_cost, 8),
            },
        },
        "per_request_metrics": {
            "classic_avg_billed_seconds": round(classic_avg_billed_s, 4),
            "web_cpu_seconds_per_req": round(web_cpu_s_per_req, 6),
            "web_sustained_env_seconds_per_req": round(web_sustained_active_s_per_req, 6),
            "web_burst_env_seconds_per_req": round(web_burst_active_s_per_req, 6),
        },
        "monthly_projections": projections,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Saved cost analysis to {output_path}")

    print("\n## Cold Starts & Execution Times (CloudWatch)\n")
    print(
        f"- Classic + LWA: {classic_analysis['cold_starts']['count']} cold starts "
        f"(avg init {classic_analysis['cold_starts']['init_duration_ms']['avg']} ms, "
        f"max {classic_analysis['cold_starts']['init_duration_ms']['max']} ms) | "
        f"warm avg handler {classic_analysis['warm_invocations']['handler_duration_ms']['avg']} ms | "
        f"overall avg billed {classic_analysis['all_invocations']['billed_duration_ms']['avg']} ms | "
        f"total cost ${classic_analysis['total_cost_usd']:.6f}"
    )
    print(
        f"- Lambda Web: {web_logs['cold_starts_count']} cold starts "
        f"(avg init {web_logs['init_duration_ms']['avg']} ms) | "
        f"active CPU {web_metrics['vcpu_seconds']} s | "
        f"env active {web_metrics['environment_active_seconds']} s (with keep-alive) vs "
        f"{round(active_only_env_s, 2)} s (active load only) | "
        f"cost ${web_metrics['total_cost_usd']:.6f} (with keep-alive) / ${active_only_total_cost:.6f} (active load)"
    )

    print("\n## Monthly Cost Projections (eu-west-1, excluding Free Tier)\n")
    print("| Requests/Month | Classic 256 MB | Classic 512 MB | Classic 1024 MB | Classic 2048 MB | Web (Sustained 50x) | Web (Burst w/ Keep-Alive) |")
    print("|---:|---:|---:|---:|---:|---:|---:|")
    for r in projections:
        print(
            f"| {r['monthly_requests']:,} | "
            f"${r['classic_256mb_usd']:,.2f} | "
            f"${r['classic_512mb_usd']:,.2f} | "
            f"${r['classic_1024mb_usd']:,.2f} | "
            f"${r['classic_2048mb_usd']:,.2f} | "
            f"${r['web_sustained_50x_usd']:,.2f} | "
            f"${r['web_burst_with_keepalive_usd']:,.2f} |"
        )


if __name__ == "__main__":
    main()
