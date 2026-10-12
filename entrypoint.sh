#!/bin/sh
set -u

UDP_SCRIPT="/app/sensmos-gw-udp.py"
MQTT_SCRIPT="/app/sensmos-gw-mqtt.py"
STATE_DIR="${SENSMOS_STATE_DIR:-/var/lib/sensmos}"
UDP_STATE="${STATE_DIR}/sensmos-gw-udp.json"
MQTT_STATE="${STATE_DIR}/sensmos-gw-mqtt.json"

mkdir -p "$STATE_DIR"

# MQTT one-shot commands: docker compose run --rm sensmos-lora --mqtt --pair ...
if [ "${1:-}" = "--mqtt" ]; then
    shift
    export SENSMOS_STATE="$MQTT_STATE"
    exec python3 "$MQTT_SCRIPT" "$@"
fi

# UDP one-shot commands: --find, --dry-run, --pair, etc.
if [ "$#" -gt 0 ]; then
    export SENSMOS_STATE="$UDP_STATE"
    exec python3 "$UDP_SCRIPT" "$@"
fi

echo "Starting Sensmos LoRa integrations..."
echo "  UDP/GWMP : $UDP_SCRIPT"
SENSMOS_STATE="$UDP_STATE" python3 "$UDP_SCRIPT" &
UDP_PID=$!
echo "  UDP PID  : $UDP_PID"

MQTT_PID=""
if [ -f "$MQTT_STATE" ]; then
    echo "  MQTT     : $MQTT_SCRIPT"
    SENSMOS_STATE="$MQTT_STATE" python3 "$MQTT_SCRIPT" &
    MQTT_PID=$!
    echo "  MQTT PID : $MQTT_PID"
else
    echo "  MQTT     : not paired, skipping"
fi

# Keep the container alive and fail if either active listener exits.
while :; do
    if ! kill -0 "$UDP_PID" 2>/dev/null; then
        echo "UDP listener exited." >&2
        exit 1
    fi
    if [ -n "$MQTT_PID" ] && ! kill -0 "$MQTT_PID" 2>/dev/null; then
        echo "MQTT listener exited." >&2
        exit 1
    fi
    sleep 5
done
