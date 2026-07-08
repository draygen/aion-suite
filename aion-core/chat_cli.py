#!/usr/bin/env python3
"""Tiny interactive REPL to chat with the running aion-core (qwen3.5:9b).

Talks to the local /api/service/chat endpoint (service-token auth, no web login).
Reads the token + base URL from config.py, strips the base64 TTS audio blob.

    ./.venv/bin/python chat_cli.py
    /reset   start a fresh thread     /quit    exit
"""
import json, sys, urllib.request
from config import CONFIG

BASE = "http://127.0.0.1:5000"
TOKEN = str(CONFIG.get("service_token", ""))
USER = CONFIG.get("primary_user", "brian")


def send(message, thread_id):
    body = json.dumps({"username": USER, "message": message,
                       "thread_id": thread_id}).encode()
    req = urllib.request.Request(
        BASE + "/api/service/chat", data=body,
        headers={"Content-Type": "application/json",
                 "X-Aion-Service-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.load(r)
    return d.get("response") or d.get("reply") or "<no response>"


def main():
    import time
    # Fresh thread per launch: keeps context clean and immune to any stale/poisoned
    # history in shared threads. /reset starts another fresh one mid-session.
    thread = f"cli-{USER}-{int(time.time())}"
    print(f"AION chat — model={CONFIG.get('model')}  thread={thread}  (/reset, /quit)\n")
    while True:
        try:
            msg = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); break
        if not msg:
            continue
        if msg in ("/quit", "/exit"):
            break
        if msg == "/reset":
            import time; thread = f"cli-{USER}-{int(time.time())}"
            print("(new thread)\n"); continue
        try:
            print("\naion>", send(msg, thread), "\n")
        except Exception as e:
            print(f"[error] {e}\n", file=sys.stderr)


if __name__ == "__main__":
    main()
