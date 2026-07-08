# Model swap: Mistral 7B → qwen3.5:9b — benchmark

**Date:** 2026-07-08 · **Host:** Windows Ollama 0.31.1 (RTX 4060 Ti), reached from WSL at `localhost:11434`

## What changed

- **Primary base model is now `qwen3.5:9b`.** `brian-mistral` and `aion-producer` were
  rebuilt on it (same tag names — the app config is unchanged).
- **Template swapped Mistral `[INST]/<<SYS>>` → Qwen ChatML** (`<|im_start|>role … <|im_end|>`).
  Stop tokens changed from `[INST]`/`[/INST]` to `<|im_start|>`/`<|im_end|>`/`<|endoftext|>`
  (plus `<END_ANALYSIS>` for the producer).
- **`mistral:7b-instruct` kept** as the fast second opinion for `fleet review`.
- Old Mistral builds preserved as `brian-mistral-m7b` / `aion-producer-m7b` (rollback + reproducible "before").
- Reproducible Modelfiles committed: `aion-core/Modelfile.brian-mistral`, `aion-core/Modelfile.aion-producer`.

### Required code change (not optional)

`qwen3.5:9b` is a **reasoning model**. By default it routes the answer into
`message.thinking` and returns an **empty `message.content`** — which is the only field
AION reads. A naive FROM-swap would have made AION reply with blank strings.

Fix: `_ollama_chat` now sends `think:false` (config `llm_think`, default off; `LLM_THINK=1` to re-enable).
`/no_think` in the system prompt does **not** work for this variant; the request flag does.
The flag is silently ignored by non-thinking models, so it's safe on the Mistral path too.

## Before / after (same 4 prompts, temp 0.4, num_predict 1024)

| Model tag        | Base            | Avg throughput | Verdict |
|------------------|-----------------|---------------:|---------|
| `aion-producer`  | Mistral 7B (before) | **54.8 tok/s** | fast, shallower, hallucinated CLI flags |
| `aion-producer`  | qwen3.5:9b (after)  | **43.0 tok/s** | ~22% slower/token, grounded + far more thorough |
| `brian-mistral`  | Mistral 7B (before) | **55.0 tok/s** | fast, terse |
| `brian-mistral`  | qwen3.5:9b (after)  | **43.5 tok/s** | slower/token, richer, stronger persona voice |

Throughput drop is the expected cost of 9.7B vs 7B params. Qwen also uses far more of the
token budget (it hit the 1024-token cap on long answers where Mistral stopped early), so
**wall-clock per answer is longer** — worth it for the depth, but keep `num_predict` capped.

### Quality delta (representative)

On *"WSL keeps eating RAM, how do I cap it?"*:
- **Mistral (before)** invented non-existent commands — `wsl --set-default-memory`, `wsl config`.
- **Qwen (after)** correctly described the real mechanism (WSL2 grabs up to ~80% of RAM; cap it
  via `.wslconfig`). No hallucinated flags.

Persona also held up under the base swap — the `brian-mistral` build spontaneously grounded an
emotional reply in Brian's own context ("go make some pho, listen to a track").

## Pair mode — `fleet review` cross-audit

Same buggy `transfer()` snippet reviewed by both models in parallel (fan-out → gather):

| Reviewer | Time | Unique catches |
|----------|-----:|----------------|
| **qwen3.5:9b** (primary) | 20.1s | partial-update inconsistency / **no rollback**, integer overflow |
| **mistral:7b-instruct** (2nd) | **2.35s** | **silent-failure return contract** (returns wrong state when `amount > balance`, no signal), naming/style |

Both independently caught the shared big three (missing-key `KeyError`, negative-amount abuse,
thread-safety). But each also caught something the other missed — exactly the point of pairing
**architecturally different** families instead of two same-family models.

- **Pair latency:** parallel fan-out **20.08s** vs sequential 22.42s → the Mistral second opinion
  is effectively **free** (bounded by the slower model). At 2.35s it's a ~9× faster sanity pass.

## Takeaway

Qwen buys accuracy and depth at ~22% lower throughput and longer answers. Mistral stays as the
4.4GB speed king for a near-free, architecturally-independent second opinion. Keep both.

### Rollback

```bash
# point AION back at the old Mistral build
#   config_local.py: "model": "aion-producer-m7b:latest"
# or restore the tag itself:
curl -s localhost:11434/api/copy -d '{"source":"aion-producer-m7b","destination":"aion-producer"}'
curl -s localhost:11434/api/copy -d '{"source":"brian-mistral-m7b","destination":"brian-mistral"}'
```
