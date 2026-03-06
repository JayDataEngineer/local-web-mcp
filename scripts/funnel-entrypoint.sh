#!/bin/sh
set -e

echo "Starting Tailscale..."
tailscaled --state=/var/lib/tailscale/tailscaled.state --socket=/tmp/tailscale/tailscaled.sock &
TS_PID=$!

# Wait for tailscaled to be ready
echo "Waiting for Tailscale to start..."
for i in $(seq 1 30); do
    if tailscale status >/dev/null 2>&1; then
        echo "Tailscale is running."
        break
    fi
    echo "Waiting... ($i/30)"
    sleep 1
done

# Check if we need to authenticate
if ! tailscale status >/dev/null 2>&1; then
    echo "Tailscale not authenticated, starting with auth key..."
    tailscale up --authkey=${TS_AUTHKEY} --hostname=mcp-research-server
fi

# Wait for Funnel to be enabled
echo "Enabling Tailscale Funnel..."
tailscale funnel --https=443 --bg --yes http://127.0.0.1:80

# Keep the container running
echo "Tailscale Funnel is running. Keeping container alive."
wait $TS_PID
