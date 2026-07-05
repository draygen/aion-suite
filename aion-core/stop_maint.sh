#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLATFORM_DIR="$(cd "$ROOT_DIR/../.." && pwd)"
AION_LOG="$ROOT_DIR/nohup.log"
JAREDSHARE_DIR="$PLATFORM_DIR/services/share"
if [ ! -x "$JAREDSHARE_DIR/start.sh" ]; then
    JAREDSHARE_DIR="/mnt/c/projects/jaredshare"
fi

run_sudo() {
    if sudo -n true 2>/dev/null; then
        sudo "$@"
    else
        echo "Warning: sudo access is required to run: $*" >&2
        return 1
    fi
}

echo "Stopping maintenance mode and starting services..."

# 1. Stop the maintenance Nginx server
echo "Stopping maintenance Nginx..."
docker stop maint-nginx 2>/dev/null || true
docker rm maint-nginx 2>/dev/null || true

# 2. Kill the maintenance Cloudflare tunnel
echo "Stopping maintenance Cloudflare tunnel..."
pkill -f "cloudflared" || true

# 3. Restore the original Cloudflared config
echo "Restoring Cloudflared configuration..."
if [ -f ~/.cloudflared/config.yml.bak ]; then
    mv ~/.cloudflared/config.yml.bak ~/.cloudflared/config.yml
fi

# 4. Start JaredShare (runs in background)
echo "Starting JaredShare..."
cd "$JAREDSHARE_DIR"
nohup ./start.sh > "$JAREDSHARE_DIR/nohup.log" 2>&1 &

# 5. Start Syncforge Tunnel Service
echo "Restarting Cloudflared System Service..."
run_sudo systemctl enable cloudflared-drayhub 2>/dev/null || true
run_sudo systemctl start cloudflared-drayhub 2>/dev/null || true

# 6. Start Aion
echo "Starting Aion..."
cd "$ROOT_DIR"
nohup ./start_web.sh > "$AION_LOG" 2>&1 &

# 6. Start MFT Docker containers
echo "Starting MFT Docker containers..."
docker start mft-server-db-1 mft-server-server-1 mft-server-proxy-1 2>/dev/null || true

echo "=========================================="
echo "Maintenance mode STOPPED."
echo "Services are starting in the background."
echo "Wait a moment for everything to initialize."
echo "=========================================="
