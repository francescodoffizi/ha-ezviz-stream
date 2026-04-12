#!/usr/bin/with-contenv bashio

# Read configuration from /data/options.json
# bashio is available in HA add-on containers and provides helper functions

bashio::log.info "Starting Ezviz Camera Proxy..."

# Export config values as environment variables for the Python app
export EZVIZ_USERNAME="$(bashio::config 'ezviz_username')"
export EZVIZ_PASSWORD="$(bashio::config 'ezviz_password')"
export EZVIZ_REGION="$(bashio::config 'ezviz_region')"
export CAMERA_SERIAL="$(bashio::config 'camera_serial')"
export CAMERA_PASSWORD="$(bashio::config 'camera_password')"
export SNAPSHOT_INTERVAL="$(bashio::config 'snapshot_interval')"
export ENABLE_MQTT_EVENTS="$(bashio::config 'enable_mqtt_events')"

# HA Supervisor / Ingress environment
export INGRESS_ENTRY="$(bashio::addon.ingress_entry 2>/dev/null || echo '/')"
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-}"
export HA_SUPERVISOR_URL="http://supervisor"

# MQTT Service details provided by HA if configured
bashio::log.info "Checking for MQTT service..."

# Initialize with empty values to avoid unbound variable errors
MQTT_HOST=""
MQTT_PORT=""
MQTT_USER=""
MQTT_PASSWORD=""

if bashio::services.available "mqtt"; then
    MQTT_HOST="$(bashio::services.mqtt "host")"
    MQTT_PORT="$(bashio::services.mqtt "port")"
    MQTT_USER="$(bashio::services.mqtt "username")"
    MQTT_PASSWORD="$(bashio::services.mqtt "password")"
    bashio::log.info "MQTT service detected (via bashio): ${MQTT_HOST}:${MQTT_PORT}"
else
    bashio::log.warning "MQTT service not detected via bashio"
fi

# Fallback to internal name common in HA if still empty
if [ -z "${MQTT_HOST}" ]; then
   bashio::log.info "Checking if 'core-mosquitto' is reachable..."
   if ping -c 1 core-mosquitto &> /dev/null; then
       bashio::log.info "Fallback: using core-mosquitto as MQTT host"
       MQTT_HOST="core-mosquitto"
       MQTT_PORT="1883"
   fi
fi

# Export to environment for Python
export MQTT_HOST
export MQTT_PORT
export MQTT_USER
export MQTT_PASSWORD

# Data directory for token caching and snapshots
export DATA_PATH="/data"
mkdir -p /data/snapshots

bashio::log.info "Camera serial: ${CAMERA_SERIAL}"
bashio::log.info "Ezviz region: ${EZVIZ_REGION}"
bashio::log.info "Snapshot interval: ${SNAPSHOT_INTERVAL}s"
bashio::log.info "Ingress entry: ${INGRESS_ENTRY}"

# Start Flask application
exec python3 /app/server.py
