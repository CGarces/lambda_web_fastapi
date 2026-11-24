"""Execute concurrent HTTP GET /dummy benchmarks against the classic FastAPI + LWA
Lambda Function URL and the native AWS Lambda Web Function endpoint.

Uses a single shared urllib3 PoolManager with a pre-loaded SSLContext and DNS
pre-resolution so Windows TLS store loading never bottlenecks concurrent threads.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import socket
import statistics
import subprocess
import time
import urllib.parse

import urllib3
from urllib3.util.ssl_ import create_urllib3_context


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def load_terraform_outputs(terraform_dir: Path) -> dict:
    """Load deployment outputs from Terraform."""
    raw = subprocess.check_output(
        ["terraform", f"-chdir={terraform_dir}", "output", "-json"],
        text=True,
    )
    return {k: v["value"] for k, v in json.loads(raw).items()}


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


def build_http_pool(concurrency: int) -> urllib3.PoolManager:
    """Create a thread-safe PoolManager with a pre-loaded SSLContext."""
    ssl_ctx = create_urllib3_context()
    ssl_ctx.load_default_certs()
    return urllib3.PoolManager(
        num_pools=4,
        maxsize=max(concurrency, 50),
        ssl_context=ssl_ctx,
        retries=False,
        timeout=urllib3.Timeout(connect=10.0, read=30.0),
    )


def fetch_once(http: urllib3.PoolManager, request_index: int, url: str) -> dict:
    """Send one HTTP GET request using the shared pool and measure client latency."""
    started_at = utc_now_iso()
    t0 = time.perf_counter()
    try:
        resp = http.request("GET", url)
        status_code = resp.status
        body_text = resp.data.decode("utf-8", errors="replace")
    except Exception as exc:
        status_code = 0
        body_text = json.dumps({"error": str(exc)})
    client_ms = round((time.perf_counter() - t0) * 1000, 2)
    ended_at = utc_now_iso()

    try:
        body = json.loads(body_text)
    except json.JSONDecodeError:
        body = {"raw_body": body_text}

    return {
        "request_index": request_index,
        "status_code": status_code,
        "client_duration_ms": client_ms,
        "started_at": started_at,
        "ended_at": ended_at,
        "server_execution_ms": body.get("execution_time_ms") if isinstance(body, dict) else None,
        "instance_id": body.get("instance_id") if isinstance(body, dict) else None,
        "response": body,
    }


def summarize_phase(results: list[dict], wall_time_ms: float) -> dict:
    """Compute latency percentiles and status distribution for a benchmark phase."""
    status_counts: dict[str, int] = {}
    for item in results:
        key = str(item["status_code"])
        status_counts[key] = status_counts.get(key, 0) + 1

    ok_runs = [r for r in results if r["status_code"] == 200]
    latencies = sorted(r["client_duration_ms"] for r in ok_runs)
    server_ms = sorted(
        r["server_execution_ms"] for r in ok_runs if r["server_execution_ms"] is not None
    )
    unique_instances = sorted({r["instance_id"] for r in ok_runs if r.get("instance_id")})

    return {
        "total_requests": len(results),
        "success_count": len(ok_runs),
        "status_counts": status_counts,
        "wall_time_ms": wall_time_ms,
        "unique_instance_ids": len(unique_instances),
        "client_latency_ms": {
            "min": latencies[0] if latencies else 0.0,
            "avg": round(statistics.mean(latencies), 2) if latencies else 0.0,
            "p50": round(percentile(latencies, 50), 2),
            "p95": round(percentile(latencies, 95), 2),
            "p99": round(percentile(latencies, 99), 2),
            "max": latencies[-1] if latencies else 0.0,
        },
        "server_execution_ms": {
            "min": server_ms[0] if server_ms else 0.0,
            "avg": round(statistics.mean(server_ms), 2) if server_ms else 0.0,
            "p95": round(percentile(server_ms, 95), 2),
            "max": server_ms[-1] if server_ms else 0.0,
        },
    }


def run_phase(
    http: urllib3.PoolManager, name: str, url: str, total_requests: int, concurrency: int
) -> dict:
    """Pre-resolve DNS and execute total_requests HTTP GET calls concurrently."""
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname:
        socket.getaddrinfo(parsed.hostname, parsed.port or 443)

    print(
        f"[{name}] launching {total_requests} requests ({concurrency} concurrent) -> {url}",
        flush=True,
    )
    start_utc = utc_now_iso()
    t0 = time.perf_counter()
    results = []

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(fetch_once, http, idx + 1, url)
            for idx in range(total_requests)
        ]
        for fut in as_completed(futures):
            results.append(fut.result())

    wall_time_ms = round((time.perf_counter() - t0) * 1000, 2)
    end_utc = utc_now_iso()
    results.sort(key=lambda r: r["request_index"])
    summary = summarize_phase(results, wall_time_ms)
    print(
        f"[{name}] done in {wall_time_ms} ms | status={summary['status_counts']} | "
        f"p50={summary['client_latency_ms']['p50']} ms | p95={summary['client_latency_ms']['p95']} ms | "
        f"max={summary['client_latency_ms']['max']} ms",
        flush=True,
    )
    return {
        "phase": name,
        "url": url,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "summary": summary,
        "runs": results,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark FastAPI on Classic Lambda vs Lambda Web Functions."
    )
    parser.add_argument("--terraform-dir", default="terraform", help="Terraform directory.")
    parser.add_argument("--events-url", help="Override classic Lambda Function URL.")
    parser.add_argument("--web-url", help="Override Lambda Web Function endpoint URL.")
    parser.add_argument("--total-requests", type=int, default=150, help="Total requests per Lambda.")
    parser.add_argument("--concurrency", type=int, default=50, help="Concurrent worker threads.")
    parser.add_argument(
        "--cooldown-seconds",
        type=int,
        default=75,
        help="Pause between phases to isolate 1-minute CloudWatch metric buckets.",
    )
    parser.add_argument("--output", default="results/benchmark_results.json", help="Output JSON path.")
    args = parser.parse_args()

    tf_outputs = load_terraform_outputs(Path(args.terraform_dir))
    events_url = f"{(args.events_url or tf_outputs['events_url']).rstrip('/')}/dummy"
    web_url = f"{(args.web_url or tf_outputs['web_url']).rstrip('/')}/dummy"

    http = build_http_pool(args.concurrency)

    overall_start = utc_now_iso()
    events_phase = run_phase(http, "events", events_url, args.total_requests, args.concurrency)

    if args.cooldown_seconds > 0:
        print(f"Cooling down {args.cooldown_seconds}s before testing Lambda Web Functions...", flush=True)
        time.sleep(args.cooldown_seconds)

    web_phase = run_phase(http, "lambdaweb", web_url, args.total_requests, args.concurrency)
    overall_end = utc_now_iso()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": {
            "region": tf_outputs.get("aws_region", "eu-west-1"),
            "total_requests_per_target": args.total_requests,
            "concurrency": args.concurrency,
            "overall_start_utc": overall_start,
            "overall_end_utc": overall_end,
            "events_function_name": tf_outputs.get("events_function_name"),
            "events_log_group": tf_outputs.get("events_log_group"),
            "web_function_name": tf_outputs.get("web_function_name"),
            "web_log_group": tf_outputs.get("web_log_group"),
        },
        "phases": {
            "events": events_phase,
            "lambdaweb": web_phase,
        },
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Saved benchmark results to {output_path}", flush=True)


if __name__ == "__main__":
    main()
