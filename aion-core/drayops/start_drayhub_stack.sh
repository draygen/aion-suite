#!/usr/bin/env bash
set -euo pipefail

PID_FILE="/tmp/drayhub-stack.pid"
LOG_FILE="/tmp/drayhub-stack.log"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNNER="$ROOT_DIR/drayops/drayhub_stack_run.sh"

if [[ -f "${PID_FILE}" ]]; then
  PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${PID}" ]] && kill -0 "${PID}" 2>/dev/null; then
    echo "drayhub stack already running (pid ${PID})"
    exit 0
  fi
fi

nohup "${RUNNER}" >>"${LOG_FILE}" 2>&1 &
PID=$!
echo "${PID}" > "${PID_FILE}"
echo "started drayhub stack (pid ${PID})"
