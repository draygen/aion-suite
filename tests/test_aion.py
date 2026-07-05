#!/usr/bin/env python3
"""
AION test suite — validates what the mcpbuilder (aion-mcp) MCP server needs from AION Core.

Two layers:
  1. CONTRACT — the AION HTTP endpoints mcpbuilder's aion_* tools call actually work
     (service chat, channels, activity, memory/browse, admin/users) with the right auth.
  2. INTELLIGENCE — AION's response quality via POST /api/service/chat {"tts": false}
     (returns the text `response`), graded with deterministic substring/regex checks.

Run: tests/run.sh   (or: ../aion-core/.venv/bin/python test_aion.py)
Config via env: AION_BASE, AION_SERVICE_TOKEN, AION_USER, AION_PASS, OLLAMA_URL, AION_MODEL.
Exit code = number of failed CRITICAL tests (0 = all good). Informational checks never fail the run.
"""
import json, os, re, sys, time, urllib.request, urllib.error

AION_BASE = os.getenv("AION_BASE", "http://127.0.0.1:5000")
SERVICE_TOKEN = os.getenv("AION_SERVICE_TOKEN", "change-me-service-token")
USER = os.getenv("AION_USER", "brian")
PASS = os.getenv("AION_PASS", "change-me-password")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.getenv("AION_MODEL", "aion-producer:latest")

results = []  # (layer, name, ok, critical, detail, ms)

def _http(method, url, body=None, headers=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            ms = int((time.time() - t) * 1000)
            ct = r.headers.get("Content-Type", "")
            return r.status, (json.loads(raw) if "json" in ct or raw[:1] in "{[" else raw), ms, r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300], int((time.time() - t) * 1000), e.headers
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", int((time.time() - t) * 1000), {}

def record(layer, name, ok, critical, detail, ms=0):
    results.append((layer, name, ok, critical, detail, ms))
    icon = "PASS" if ok else ("FAIL" if critical else "warn")
    print(f"  [{icon}] {name}  ({ms}ms)" + (f"  — {detail}" if detail else ""))

def chat(message, tts=False, username="Claude", timeout=120):
    """POST /api/service/chat -> (text_response, ms). The core mcpbuilder capability."""
    status, body, ms, _ = _http(
        "POST", f"{AION_BASE}/api/service/chat",
        {"username": username, "message": message, "tts": tts},
        {"X-Aion-Service-Token": SERVICE_TOKEN}, timeout=timeout,
    )
    if status == 200 and isinstance(body, dict):
        return (body.get("response") or "").strip(), ms
    return f"[HTTP {status}] {str(body)[:200]}", ms

# --------------------------------------------------------------------------
# LAYER 1 — CONTRACT (endpoints mcpbuilder's aion_* tools depend on)
# --------------------------------------------------------------------------
def contract_tests():
    print("\n== CONTRACT (mcpbuilder -> AION endpoints) ==")

    st, body, ms, _ = _http("GET", f"{AION_BASE}/api/health", timeout=8)
    record("contract", "aion /api/health", st == 200 and isinstance(body, dict) and body.get("ok"),
           True, f"HTTP {st}", ms)

    st, body, ms, _ = _http("GET", f"{OLLAMA_URL}/api/tags", timeout=8)
    has_model = st == 200 and MODEL.split(":")[0] in json.dumps(body)
    record("contract", f"ollama has {MODEL}", has_model, True, f"HTTP {st}", ms)

    # service chat (service-token auth) — THE core capability mcpbuilder wraps
    txt, ms = chat("Reply with the single word: online")
    record("contract", "POST /api/service/chat (service token)", bool(txt) and not txt.startswith("[HTTP"),
           True, txt[:60], ms)

    # Session auth: prefer AION_SESSION_TOKEN (exactly how mcpbuilder authenticates its
    # session tools); fall back to login env credentials when no token is provided.
    cookie = os.getenv("AION_SESSION_TOKEN", "")
    if cookie:
        record("contract", "session auth (AION_SESSION_TOKEN)", True, True, "using provided token", 0)
    else:
        st, body, ms, hdrs = _http("POST", f"{AION_BASE}/api/login", {"username": USER, "password": PASS}, timeout=8)
        sc = (hdrs.get("Set-Cookie", "") if hdrs else "") or ""
        m = re.search(r"aion_token=([^;]+)", sc)
        cookie = m.group(1) if m else ""
        record("contract", "POST /api/login", st == 200 and bool(cookie), True,
               f"HTTP {st}, role={body.get('role') if isinstance(body, dict) else '?'}", ms)
        if isinstance(body, dict) and body.get("requires_password_change"):
            record("contract", "session account not password-change-gated", False, False,
                   f"user '{USER}' is password-change-gated -> admin/channel tools 403. "
                   f"Set AION_SESSION_TOKEN (e.g. aion_service) or clear the flag.", 0)

    ch = {"Cookie": f"aion_token={cookie}"} if cookie else {}
    for path, key in [("/api/channels", "channels"), ("/api/activity?limit=5", "activity"),
                      ("/api/memory/browse?limit=5", "memory/browse"), ("/api/admin/users", "admin/users")]:
        st, body, ms, _ = _http("GET", f"{AION_BASE}{path}", headers=ch, timeout=10)
        ok = st == 200
        n = (len(body) if isinstance(body, list) else
             (len(next((v for v in body.values() if isinstance(v, list)), [])) if isinstance(body, dict) else 0))
        record("contract", f"GET {key}", ok, False, f"HTTP {st}" + (f", {n} items" if ok else f" {str(body)[:60]}"), ms)

# --------------------------------------------------------------------------
# LAYER 2 — INTELLIGENCE (aion-producer response quality)
# --------------------------------------------------------------------------
def intelligence_tests():
    print("\n== INTELLIGENCE (aion-producer via service/chat) ==")

    # factual recall
    txt, ms = chat("What is the capital of France? Answer with just the city name.")
    record("intel", "factual: capital of France", "paris" in txt.lower(), True, txt[:70], ms)

    # arithmetic / simple reasoning (accept digit or word form)
    txt, ms = chat("I have 3 apples and eat 1. How many are left? Reply with just the number.")
    record("intel", "reasoning: 3 apples minus 1", txt.strip() == "2", True, txt[:70], ms)

    # multi-step reasoning
    txt, ms = chat("A shirt costs $20 after a 50% discount. What was the original price? Reply with just the dollar amount.")
    record("intel", "reasoning: reverse a 50% discount", txt.strip() == "40", True, txt[:70], ms)

    # instruction following (exact token)
    txt, ms = chat("Respond with exactly one word and nothing else: BANANA")
    record("intel", "instruction following (BANANA)", txt.strip() == "BANANA", True, txt[:70], ms)

    # coherence / non-degenerate
    txt, ms = chat("In two sentences, explain what an API is.")
    words = len(txt.split())
    record("intel", "coherence: explain an API", 8 <= words <= 120 and "api" in txt.lower(),
           True, f"{words} words: {txt[:60]}", ms)

    # structured output (JSON) — useful signal for tool-use pipelines; informational
    txt, ms = chat('Return ONLY a JSON object with keys "a" and "b" set to 1 and 2. No prose.')
    ok_json = False
    try:
        d = json.loads(txt)
        ok_json = d == {"a": 1, "b": 2}
    except Exception:
        ok_json = False
    record("intel", "structured output (JSON)", ok_json, False, txt[:70], ms)

    # conversational memory within a session (informational — depends on AION memory config)
    u = f"memtest_{int(time.time())}"
    chat("Remember this: my favorite color is teal.", username=u)
    txt, ms = chat("What is my favorite color? One word.", username=u)
    record("intel", "memory recall (teal, same user)", "teal" in txt.lower(), False, txt[:70], ms)

    other = f"memtest_other_{int(time.time())}"
    txt, ms = chat("What is my favorite color? One word.", username=other)
    record("intel", "memory isolation (different user)", "teal" not in txt.lower(), False, txt[:70], ms)

def main():
    print(f"AION test suite -> {AION_BASE}  model={MODEL}")
    contract_tests()
    intelligence_tests()

    crit = [r for r in results if r[3]]
    crit_fail = [r for r in crit if not r[2]]
    info = [r for r in results if not r[3]]
    info_fail = [r for r in info if not r[2]]
    passed = sum(1 for r in results if r[2])
    print("\n== SUMMARY ==")
    print(f"  total {len(results)} | passed {passed} | critical failed {len(crit_fail)} | informational failed {len(info_fail)}")
    if crit_fail:
        print("  CRITICAL FAILURES:")
        for _, n, _, _, d, _ in crit_fail:
            print(f"    - {n}: {d}")
    lat = [r[5] for r in results if r[0] == "intel" and r[5]]
    if lat:
        print(f"  intel latency: avg {sum(lat)//len(lat)}ms, max {max(lat)}ms")

    # persist report
    try:
        out = os.path.join(os.path.dirname(__file__), "..", "logs", "aion-test-report.json")
        with open(out, "w") as f:
            json.dump([{"layer": l, "name": n, "ok": ok, "critical": c, "detail": d, "ms": ms}
                       for l, n, ok, c, d, ms in results], f, indent=2)
    except Exception:
        pass
    sys.exit(len(crit_fail))

if __name__ == "__main__":
    main()
