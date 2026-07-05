#!/usr/bin/env python3
"""
End-to-end pipeline test for Aion + SonChat.
Tails logs while sending a test prompt and traces the full request path.

Usage:
  python3 e2e_test.py "your message here"
  python3 e2e_test.py  # uses default OSINT test
"""
import sys
import time
import threading
import subprocess
import requests
import json
import os
from datetime import datetime

AION_URL  = "http://127.0.0.1:5000"
OLLAMA_URL = "http://127.0.0.1:11434"
SERVICE_TOKEN = os.getenv("AION_SERVICE_TOKEN", "change-me-service-token")
AION_TEST_USER = os.getenv("AION_TEST_USER", "playwright_test")
AION_TEST_PASS = os.getenv("AION_TEST_PASS", "change-me-password")
AION_LOG = "/tmp/aion_web.log"
SONCHAT_LOG = "/mnt/c/projects/drayhub-platform/services/sonchat/sonchat.log"

RESET  = "\033[0m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"

def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

def banner(text, color=BOLD):
    print(f"\n{color}{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}{RESET}")

def check(label, ok, detail=""):
    sym = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
    suffix = f" — {detail}" if detail else ""
    print(f"  {sym} {label}{suffix}")
    return ok

# ── Log tail thread ──────────────────────────────────────────

stop_tail = threading.Event()

def tail_file(path, label, color):
    try:
        with open(path, "r") as f:
            f.seek(0, 2)  # jump to end
            while not stop_tail.is_set():
                line = f.readline()
                if line:
                    print(f"{color}[{label}]{RESET} {line.rstrip()}")
                else:
                    time.sleep(0.1)
    except FileNotFoundError:
        pass

# ── Pre-flight checks ─────────────────────────────────────────

def preflight():
    banner("PRE-FLIGHT CHECKS")

    # Aion
    try:
        r = requests.get(f"{AION_URL}/api/health", timeout=3)
        check("Aion :5000", r.ok, r.json().get("service",""))
    except Exception as e:
        check("Aion :5000", False, str(e))
        print(f"  {RED}→ Restarting Aion...{RESET}")
        subprocess.Popen(
            ["/mnt/c/projects/drayhub-platform/services/aion/.venv/bin/python", "web.py"],
            cwd="/mnt/c/projects/drayhub-platform/services/aion",
            stdout=open(AION_LOG, "a"), stderr=subprocess.STDOUT
        )
        time.sleep(4)

    # SonChat
    try:
        r = requests.get("http://127.0.0.1:3000/", timeout=3)
        check("SonChat :3000", r.status_code < 500)
    except Exception as e:
        check("SonChat :3000", False, str(e))

    # Ollama
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        models = [m["name"] for m in r.json().get("models", [])]
        check("Ollama :11434", True, ", ".join(models[:3]))
    except Exception as e:
        check("Ollama :11434", False, str(e))

    # Login
    try:
        r = requests.post(f"{AION_URL}/api/login",
                          json={"username": AION_TEST_USER, "password": AION_TEST_PASS}, timeout=5)
        check(f"Login {AION_TEST_USER}", r.ok)
    except Exception as e:
        check("Login playwright_test", False, str(e))

# ── Send test message ─────────────────────────────────────────

def send_message(message):
    banner(f"SENDING: {message[:70]}", CYAN)
    print(f"  {YELLOW}Starting log tails...{RESET}")

    # Start tailing logs
    t_aion = threading.Thread(target=tail_file, args=(AION_LOG, "AION", YELLOW), daemon=True)
    t_chat = threading.Thread(target=tail_file, args=(SONCHAT_LOG, "CHAT", CYAN), daemon=True)
    t_aion.start()
    t_chat.start()

    time.sleep(0.2)  # let tails attach

    print(f"\n  {BOLD}[{ts()}] → POST /api/service/chat{RESET}")
    t0 = time.time()

    try:
        r = requests.post(
            f"{AION_URL}/api/service/chat",
            headers={
                "Content-Type": "application/json",
                "X-Aion-Service-Token": SERVICE_TOKEN,
            },
            json={
                "message": message,
                "username": AION_TEST_USER,
                "channel": "global",
                "thread_id": "lobby",
                "session_id": "global:lobby",
                "tts": False,
            },
            timeout=120,
        )
        elapsed = time.time() - t0
        data = r.json()
    except Exception as e:
        print(f"\n  {RED}Request failed: {e}{RESET}")
        stop_tail.set()
        return

    time.sleep(0.5)  # let log lines flush
    stop_tail.set()

    # ── Result ──
    banner(f"RESULT  ({elapsed:.2f}s)", GREEN if r.ok else RED)

    response_text = data.get("response", "")
    if response_text:
        print(f"\n{BOLD}Aion reply:{RESET}")
        print(response_text[:1000])
        if len(response_text) > 1000:
            print(f"  ... ({len(response_text)} chars total)")
    else:
        print(f"  {RED}No response field — full JSON:{RESET}")
        print(json.dumps(data, indent=2)[:500])

    sess = data.get("session", {})
    print(f"\n  tool: {data.get('tool_id','(LLM)')}")
    print(f"  channel: {sess.get('channel')} / thread: {sess.get('thread_id')}")
    print(f"  message_id: {sess.get('message_id')}")
    print(f"  elapsed: {elapsed:.2f}s")

# ── Ollama GPU check ──────────────────────────────────────────

def gpu_check():
    banner("OLLAMA GPU SMOKE TEST")
    print("  Sending short prompt directly to brian-mistral...")
    t0 = time.time()
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={"model":"brian-mistral","messages":[{"role":"user","content":"say ok"}],"stream":False},
            timeout=60,
        )
        data = r.json()
        elapsed = time.time() - t0
        tokens = data.get("eval_count", 0)
        tps = tokens / (data.get("eval_duration", 1) / 1e9) if data.get("eval_duration") else 0
        load_s = data.get("load_duration", 0) / 1e9
        check("Model responded", True, data.get("message",{}).get("content","")[:60])
        check(f"Tokens/sec {tps:.0f} t/s", tps > 0, f"{tokens} tokens in {elapsed:.2f}s, load {load_s:.1f}s")
        if tps > 20:
            print(f"  {GREEN}→ GPU likely active ({tps:.0f} t/s){RESET}")
        else:
            print(f"  {YELLOW}→ Low throughput — may be CPU-only ({tps:.0f} t/s){RESET}")
    except Exception as e:
        check("Ollama chat", False, str(e))

# ── Main ──────────────────────────────────────────────────────

if __name__ == "__main__":
    message = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else \
        "Hey Aion, what can you tell me about yourself and your capabilities?"

    preflight()
    gpu_check()
    send_message(message)

    print(f"\n{BOLD}Done.{RESET}\n")
