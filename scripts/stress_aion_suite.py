#!/usr/bin/env python3
"""Load-test AION service chat, session endpoints, and local GPU/server metrics."""
from __future__ import annotations

import argparse
import json
import os
import queue
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


PROMPTS = [
    "Reply with exactly one concise paragraph: explain why local AI memory helps software development.",
    "Reply with just JSON: {\"ok\": true, \"score\": 42}",
    "In two sentences, describe how a GPU accelerates transformer inference.",
    "Reply with just the number: 144 / 12",
    "Summarize the tradeoff between speed and correctness in agentic coding.",
    "Give three compact bullets on how MCP tools improve AI orchestration.",
    "Reply with exactly one word and nothing else: GREEN",
    "Explain how persistent memory changes long-running project work.",
]


@dataclass
class Result:
    phase: str
    idx: int
    ok: bool
    status: int | None
    ms: float
    bytes: int
    error: str = ""


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((len(ordered) - 1) * p)))
    return ordered[idx]


def http_json(method: str, url: str, body: dict | None = None, headers: dict | None = None, timeout: float = 60.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
        return response.status, raw


def sample_gpu(samples: list[dict], stop: threading.Event, interval: float = 0.25) -> None:
    while not stop.is_set():
        now = time.perf_counter()
        try:
            proc = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,power.draw,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                parts = [p.strip() for p in proc.stdout.strip().split(",")]
                samples.append(
                    {
                        "t": now,
                        "gpu_util_pct": float(parts[0]),
                        "gpu_mem_mib": float(parts[1]),
                        "gpu_power_w": float(parts[2]) if len(parts) > 2 and parts[2] != "[Not Supported]" else 0.0,
                        "gpu_temp_c": float(parts[3]) if len(parts) > 3 else 0.0,
                    }
                )
        except Exception:
            pass
        time.sleep(interval)


def sample_process(pid: str, samples: list[dict], stop: threading.Event, interval: float = 0.5) -> None:
    while not stop.is_set():
        now = time.perf_counter()
        try:
            proc = subprocess.run(
                ["ps", "-p", pid, "-o", "%cpu=,%mem=,rss=,nlwp="],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                cpu, mem, rss, threads = proc.stdout.strip().split()[:4]
                samples.append(
                    {
                        "t": now,
                        "cpu_pct": float(cpu),
                        "mem_pct": float(mem),
                        "rss_kib": int(rss),
                        "threads": int(threads),
                    }
                )
        except Exception:
            pass
        time.sleep(interval)


def chat_worker(
    phase: str,
    base_url: str,
    service_token: str,
    jobs: queue.Queue[tuple[int, str]],
    results: list[Result],
    lock: threading.Lock,
    timeout: float,
) -> None:
    while True:
        try:
            idx, prompt = jobs.get_nowait()
        except queue.Empty:
            return
        started = time.perf_counter()
        try:
            status, raw = http_json(
                "POST",
                f"{base_url}/api/service/chat",
                {
                    "username": f"stress_{phase}_{idx % 8}",
                    "message": prompt,
                    "tts": False,
                },
                {"X-Aion-Service-Token": service_token},
                timeout=timeout,
            )
            elapsed = (time.perf_counter() - started) * 1000
            ok = status == 200 and bool(raw)
            result = Result(phase, idx, ok, status, elapsed, len(raw))
        except urllib.error.HTTPError as exc:
            elapsed = (time.perf_counter() - started) * 1000
            result = Result(phase, idx, False, exc.code, elapsed, 0, exc.read(200).decode("utf-8", "replace"))
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            result = Result(phase, idx, False, None, elapsed, 0, f"{type(exc).__name__}: {exc}")
        with lock:
            results.append(result)
        jobs.task_done()


def run_phase(base_url: str, service_token: str, phase: str, requests: int, concurrency: int, timeout: float) -> dict:
    jobs: queue.Queue[tuple[int, str]] = queue.Queue()
    for i in range(requests):
        jobs.put((i, PROMPTS[i % len(PROMPTS)]))
    results: list[Result] = []
    lock = threading.Lock()
    started = time.perf_counter()
    threads = [
        threading.Thread(
            target=chat_worker,
            args=(phase, base_url, service_token, jobs, results, lock, timeout),
            daemon=True,
        )
        for _ in range(concurrency)
    ]
    for thread in threads:
        thread.start()
    jobs.join()
    for thread in threads:
        thread.join(timeout=1)
    wall_ms = (time.perf_counter() - started) * 1000
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    latencies = [r.ms for r in ok]
    return {
        "phase": phase,
        "requests": requests,
        "concurrency": concurrency,
        "wall_ms": wall_ms,
        "success": len(ok),
        "failed": len(failed),
        "rps": len(ok) / (wall_ms / 1000) if wall_ms else 0,
        "latency_ms": {
            "avg": statistics.mean(latencies) if latencies else 0,
            "p50": pct(latencies, 0.50),
            "p95": pct(latencies, 0.95),
            "p99": pct(latencies, 0.99),
            "min": min(latencies) if latencies else 0,
            "max": max(latencies) if latencies else 0,
        },
        "errors": [r.__dict__ for r in failed[:10]],
    }


def check_session_endpoints(base_url: str, session_token: str) -> list[dict]:
    checks = []
    if not session_token:
        return [{"path": "session", "ok": False, "error": "AION_SESSION_TOKEN not set"}]
    for path in ["/api/channels", "/api/activity?limit=5", "/api/memory/browse", "/api/admin/users"]:
        started = time.perf_counter()
        try:
            req = urllib.request.Request(f"{base_url}{path}", headers={"Cookie": f"aion_token={session_token}"})
            with urllib.request.urlopen(req, timeout=8) as response:
                raw = response.read()
                checks.append(
                    {
                        "path": path,
                        "ok": response.status == 200,
                        "status": response.status,
                        "ms": (time.perf_counter() - started) * 1000,
                        "bytes": len(raw),
                    }
                )
        except Exception as exc:
            checks.append(
                {
                    "path": path,
                    "ok": False,
                    "status": None,
                    "ms": (time.perf_counter() - started) * 1000,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return checks


def summarize_samples(samples: list[dict], prefix: str) -> dict:
    if not samples:
        return {"samples": 0}
    keys = [k for k in samples[0].keys() if k != "t"]
    out = {"samples": len(samples)}
    for key in keys:
        vals = [float(s[key]) for s in samples if key in s]
        out[key] = {
            "avg": statistics.mean(vals) if vals else 0,
            "max": max(vals) if vals else 0,
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("AION_BASE", "http://127.0.0.1:5000"))
    parser.add_argument("--pid-file", default="pids/aion-core.pid")
    parser.add_argument("--dev-requests", type=int, default=12)
    parser.add_argument("--dev-concurrency", type=int, default=3)
    parser.add_argument("--prod-requests", type=int, default=40)
    parser.add_argument("--prod-concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    service_token = os.getenv("AION_SERVICE_TOKEN", "")
    session_token = os.getenv("AION_SESSION_TOKEN", "")
    if not service_token:
        raise SystemExit("AION_SERVICE_TOKEN is required")

    pid = Path(args.pid_file).read_text().strip() if Path(args.pid_file).exists() else ""
    gpu_samples: list[dict] = []
    process_samples: list[dict] = []
    stop = threading.Event()
    samplers = [
        threading.Thread(target=sample_gpu, args=(gpu_samples, stop), daemon=True),
    ]
    if pid:
        samplers.append(threading.Thread(target=sample_process, args=(pid, process_samples, stop), daemon=True))
    for sampler in samplers:
        sampler.start()

    started = datetime.now(timezone.utc).isoformat()
    phases = []
    phases.append(run_phase(args.base_url, service_token, "warmup", 2, 1, args.timeout))
    phases.append(run_phase(args.base_url, service_token, "dev", args.dev_requests, args.dev_concurrency, args.timeout))
    phases.append(run_phase(args.base_url, service_token, "prod", args.prod_requests, args.prod_concurrency, args.timeout))
    session_checks = check_session_endpoints(args.base_url, session_token)
    stop.set()
    for sampler in samplers:
        sampler.join(timeout=2)

    report = {
        "started": started,
        "ended": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "pid": pid,
        "phases": phases,
        "session_checks": session_checks,
        "gpu": summarize_samples(gpu_samples, "gpu"),
        "aion_process": summarize_samples(process_samples, "process"),
    }

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
