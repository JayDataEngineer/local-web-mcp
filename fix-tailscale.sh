#!/bin/bash
# Fix Tailscale TUN device issue
# Run this on your HOST machine (not in container)

echo "Finding processes holding /dev/net/tun..."
lsof /dev/net/tun 2>/dev/null || echo "No processes found"

echo ""
echo "Killing any tailscale processes..."
pkill -f tailscale || echo "No tailscale processes to kill"

echo ""
echo "Removing tailscale module if loaded..."
modprobe -r tailscale 2>/dev/null || echo "Module not loaded"

echo ""
echo "Restarting Docker containers..."
cd /home/user/projects/docker-lang-tools
docker compose down
docker compose up -d

echo ""
echo "Done. Check Tailscale status:"
echo "  docker exec mcp-ts tailscale status"
