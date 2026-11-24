"""Minimal FastAPI application packaged for classic AWS Lambda + Lambda Web Adapter."""

import asyncio
import json
import logging
import os
import time
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("events_fastapi")

DELAY_SECONDS = float(os.environ.get("DELAY_SECONDS", "1.0"))
INSTANCE_ID = f"events-{int(time.time() * 1000)}-{os.getpid()}"

app = FastAPI(title="Classic Lambda FastAPI Benchmark")


@app.get("/")
async def health():
    """Fast readiness check endpoint used by AWS Lambda Web Adapter."""
    return {"status": "ok", "mode": "events", "instance_id": INSTANCE_ID}


@app.get("/dummy")
async def dummy():
    """Simulate async I/O work and return JSON with the measured execution time."""
    start = time.perf_counter()
    await asyncio.sleep(DELAY_SECONDS)
    execution_time_ms = round((time.perf_counter() - start) * 1000, 2)

    payload = {
        "message": "Hello from Classic Lambda + Lambda Web Adapter (FastAPI)",
        "mode": "events",
        "execution_time_ms": execution_time_ms,
        "delay_seconds": DELAY_SECONDS,
        "instance_id": INSTANCE_ID,
    }
    logger.info(json.dumps({"event": "dummy_request", **payload}))
    return payload
