# AION Suite Stress + Feasibility Report

Date: 2026-07-05

## Scope

This test stressed the current AION stack with traffic shaped like real MCP usage:

- Concurrent `POST /api/service/chat` requests.
- AION Core Flask API on `127.0.0.1:5000`.
- Postgres-backed user/history/event path.
- Ollama inference through `aion-producer:latest`.
- Windows-host RTX 4060 Ti GPU accessed from WSL via Ollama.
- Post-load session endpoint checks for MCPBuilder session-token tools.

No live token values were printed or written to the reports.

Raw result files:

- `logs/stress-aion-suite-moderate.json`
- `logs/stress-aion-suite-overload.json`

Load driver:

- `scripts/stress_aion_suite.py`

## Executive Summary

The current infrastructure is feasible for serious dev-level AI workflows and light-to-moderate production-style interactive traffic.

It is not yet feasible as a high-throughput multi-user public inference service without queueing, request admission control, or more GPU capacity.

The system behaved well under stress:

- No AION request failures in either run.
- No Postgres/session endpoint failures after load.
- AION Core remained healthy after load.
- Ollama retained `aion-producer:latest` in VRAM after load.
- GPU was successfully saturated, averaging roughly 90-93% utilization and peaking at 100%.

The main bottleneck is GPU inference throughput, not Flask, Postgres, or MCPBuilder.

## Test 1: Moderate Production-Shaped Load

Configuration:

- Warmup: 2 requests, concurrency 1.
- Dev phase: 12 requests, concurrency 3.
- Prod phase: 40 requests, concurrency 8.

Results:

| Phase | Requests | Concurrency | Success | Failed | RPS | Avg Latency | p50 | p95 | Max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| warmup | 2 | 1 | 2 | 0 | 0.12 | 8.51s | 3.93s | 13.09s | 13.09s |
| dev | 12 | 3 | 12 | 0 | 0.45 | 6.19s | 6.21s | 8.28s | 9.10s |
| prod | 40 | 8 | 40 | 0 | 0.48 | 14.87s | 16.14s | 18.77s | 19.72s |

GPU:

- Average utilization: 92.8%.
- Peak utilization: 100%.
- Average VRAM: 7223 MiB.
- Peak VRAM: 7291 MiB.
- Average power: 124 W.
- Peak power: 140 W.
- Peak temperature: 68 C.

AION Core process:

- CPU averaged about 1%.
- RSS stayed around 29-31 MiB.
- Thread count peaked at 71.

Interpretation:

At 8 concurrent chat requests, the GPU is already saturated. Latency rises, but the system queues safely and completes everything.

## Test 2: Overload / Knee Test

Configuration:

- Warmup: 2 requests, concurrency 1.
- Dev phase: 16 requests, concurrency 4.
- Prod phase: 80 requests, concurrency 16.

Results:

| Phase | Requests | Concurrency | Success | Failed | RPS | Avg Latency | p50 | p95 | Max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| warmup | 2 | 1 | 2 | 0 | 0.43 | 2.31s | 1.80s | 2.81s | 2.81s |
| dev | 16 | 4 | 16 | 0 | 0.47 | 7.49s | 9.15s | 10.88s | 12.19s |
| prod | 80 | 16 | 80 | 0 | 0.45 | 31.58s | 36.01s | 42.56s | 43.12s |

GPU:

- Average utilization: 90.6%.
- Peak utilization: 100%.
- Average VRAM: 7281 MiB.
- Peak VRAM: 7291 MiB.
- Average power: 124 W.
- Peak power: 144 W.
- Peak temperature: 71 C.

AION Core process:

- CPU averaged about 0.94%.
- RSS stayed around 29 MiB.
- Thread count peaked at 87.

Interpretation:

At 16 concurrent chat requests, throughput does not improve. It stays around 0.45-0.48 successful chat responses per second, while p95 latency grows to roughly 43 seconds. This is the throughput knee for the current model/GPU setup.

## Post-Test Health

After both stress runs:

```text
ollama       up @127.0.0.1:11434 (aion-producer:latest present)
postgres     up @127.0.0.1:5432 (aion_db)
aion-core    healthy @127.0.0.1:5000
mcpbuilder   built (stdio, on-demand)
```

Ollama reported `aion-producer:latest` still loaded in VRAM.

Session-token endpoints remained healthy after stress:

- `/api/channels`: HTTP 200.
- `/api/activity?limit=5`: HTTP 200.
- `/api/memory/browse`: HTTP 200.
- `/api/admin/users`: HTTP 200.

## Feasibility Assessment

### Strong Fit

The current system is a strong fit for:

- Personal AI operating system work.
- Agent orchestration through MCPBuilder.
- Local/private AI development with memory.
- Dev-agent workflows where requests are valuable and latency of a few seconds is acceptable.
- Multi-agent code review and planning.
- Low-volume internal tools.
- Long-running project continuity and research workflows.

### Acceptable With Queueing

The current system can support:

- A few simultaneous users.
- Background agent tasks.
- Bursty dev traffic.
- Tool-call workflows where results can queue.

Recommended operating envelope:

- 1-4 concurrent AION chat requests for responsive dev use.
- Up to 8 concurrent requests for acceptable queued batch work.
- Avoid sustained 16+ concurrent chat requests unless latency of 30-45 seconds is acceptable.

### Not Yet Ready

The current system is not yet ready for:

- Public multi-user chat at high concurrency.
- Strict low-latency production SLAs.
- Many simultaneous long-context agent calls.
- Heavy autonomous swarms all calling AION at once without admission control.

The stack remains stable, but the GPU becomes the queue.

## Bottleneck Analysis

### GPU

The GPU is the hard bottleneck.

Evidence:

- GPU utilization averaged above 90%.
- GPU peaked at 100%.
- Throughput did not improve from concurrency 8 to 16.
- AION Core CPU stayed near 1%.
- Postgres/session checks remained fast.

### AION Core Flask

Flask survived the load. It spawned more request threads under concurrency but did not become CPU or memory bound.

Concern:

The Flask development server is not ideal as the production HTTP front door. It survived this test, but a production deployment should use a real WSGI server or reverse proxy strategy.

### Postgres

Postgres did not show signs of being the bottleneck from this test. Session/admin endpoints stayed fast immediately after heavy chat load.

### MCPBuilder

MCPBuilder is not in the hot path for this stress driver except conceptually. The driver hit AION directly using the same service endpoint MCPBuilder uses. Previous QA confirmed MCPBuilder can call these endpoints successfully.

## Practical Capacity Estimate

Based on the tested prompt mix:

- Sustainable throughput: about 0.45-0.50 AION chat responses/sec.
- Approximate hourly capacity: 1600-1800 short/medium responses/hour if fully saturated.
- Responsive dev capacity: 1-4 concurrent requests.
- Batch capacity: 8 concurrent requests.
- Overload capacity: 16 concurrent requests completes reliably but with high latency.

These numbers depend strongly on:

- prompt length,
- context size,
- generated token count,
- model quantization,
- Ollama scheduling,
- whether AION memory/profile context grows.

## Recommendations

### Immediate

1. Add queue/admission control in front of `/api/service/chat`.

Keep only a small number of model calls active. Return queued status, backpressure, or retry-after responses once concurrency is too high.

2. Add request timeouts and max output token limits.

This protects the GPU from long generations and makes latency more predictable.

3. Add production metrics.

Track:

- active chat requests,
- queue depth,
- p50/p95/p99 latency,
- failure rate,
- Ollama loaded model,
- GPU utilization,
- GPU VRAM,
- tokens/sec if available.

4. Run AION behind a production WSGI/server setup.

The current Flask server passed the stress test, but production should not depend on the dev server.

### Near Term

5. Separate dev and production traffic.

Use separate channels, service users, or even separate AION instances for:

- interactive user traffic,
- agent swarm traffic,
- batch jobs,
- experiments.

6. Add a scheduler for agent swarms.

Fleet agents should not all hit AION at once. A scheduler can preserve responsiveness while still letting agents use AION as shared memory/intelligence.

7. Add prompt/result caching for repeated QA probes.

Many tool checks and simple prompts can be cached for short windows.

### Scaling Options

8. Use a smaller/faster model for routing and quick tasks.

Keep `aion-producer:latest` for richer memory/personality work, but route simple exact-format tasks to a faster model.

9. Add another GPU worker.

The current GPU can be saturated. More throughput needs either:

- a faster GPU,
- another Ollama worker,
- smaller quantization/model,
- batched inference support,
- external hosted models for overflow.

10. Add an async job model for long tasks.

For ambitious agent workflows, long AI calls should be jobs with status, not blocking HTTP requests.

## Conclusion

The current infrastructure is feasible and strong for your intended private AI development platform: MCP orchestration, AION memory, local GPU inference, and multi-agent workflows.

The system is stable under load and fails gracefully in the sense that it queues rather than crashing. The limiting factor is throughput and latency once the GPU is saturated.

For your ambitious projects and deeper AI work, this is enough to support a serious daily operating environment. To turn it into a broader production platform, the next engineering step is not more basic functionality. It is scheduling, queueing, observability, and explicit traffic shaping around the GPU bottleneck.
