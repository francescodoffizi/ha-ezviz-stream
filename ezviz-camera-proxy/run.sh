#!/usr/bin/with-contenv bashio

# Read configuration from /data/options.json
# bashio is available in HA add-on containers and provides helper functions

bashio::log.info "Starting Ezviz Camera Proxy..."

# Export config values as environment variables for the Python app
export EZVIZ_USERNAME="$(bashio::config 'ezviz_username')"
export EZVIZ_PASSWORD="$(bashio::config 'ezviz_password')"
export EZVIZ_REGION="$(bashio::config 'ezviz_region')"
export EZVIZ_OPEN_DOMAIN="$(bashio::config 'ezviz_open_domain')"
export EZVIZ_APP_KEY="$(bashio::config 'ezviz_app_key')"
export EZVIZ_APP_SECRET="$(bashio::config 'ezviz_app_secret')"
export EZVIZ_ACCESS_TOKEN="$(bashio::config 'ezviz_access_token')"
export CAMERA_SERIAL="$(bashio::config 'camera_serial')"
export CAMERA_PASSWORD="$(bashio::config 'camera_password')"
export EZVIZ_ENCRYPTION_KEY="$(bashio::config 'ezviz_encryption_key')"
export SNAPSHOT_INTERVAL="$(bashio::config 'snapshot_interval')"
export ENABLE_MQTT_EVENTS="$(bashio::config 'enable_mqtt_events')"
export SAVE_EVENT_CLIPS="$(bashio::config 'save_event_clips')"
export VIDEO_RETENTION_DAYS="$(bashio::config 'video_retention_days')"
export MAX_STORED_EVENTS="$(bashio::config 'max_stored_events')"

# HA Supervisor / Ingress environment
export INGRESS_ENTRY="$(bashio::addon.ingress_entry 2>/dev/null || echo '/')"
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-}"
export HA_SUPERVISOR_URL="http://supervisor"

# MQTT Service details provided by HA if configured
bashio::log.info "Checking for MQTT service..."

# Initialize with manual config values first
MQTT_HOST="$(bashio::config 'mqtt_host')"
MQTT_PORT="$(bashio::config 'mqtt_port')"
MQTT_USER="$(bashio::config 'mqtt_user')"
MQTT_PASSWORD="$(bashio::config 'mqtt_password')"

if [ -n "${MQTT_HOST}" ]; then
    bashio::log.info "Using manual MQTT configuration: ${MQTT_HOST}:${MQTT_PORT}"
else
    # Try different bashio syntaxes for service detection
    if bashio::services.available "mqtt"; then
        bashio::log.info "MQTT service available via bashio"
        # Try multiple ways to get the host as some versions differ
        MQTT_HOST=$(bashio::service "mqtt" "host" 2>/dev/null || bashio::services.mqtt "host" 2>/dev/null || echo "")
        MQTT_PORT=$(bashio::service "mqtt" "port" 2>/dev/null || bashio::services.mqtt "port" 2>/dev/null || echo "1883")
        MQTT_USER=$(bashio::service "mqtt" "username" 2>/dev/null || bashio::services.mqtt "username" 2>/dev/null || echo "")
        MQTT_PASSWORD=$(bashio::service "mqtt" "password" 2>/dev/null || bashio::services.mqtt "password" 2>/dev/null || echo "")
        
        if [ -n "${MQTT_HOST}" ]; then
            bashio::log.info "MQTT service auto-detected: ${MQTT_HOST}:${MQTT_PORT}"
        fi
    else
        bashio::log.warning "MQTT service not detected via bashio"
    fi

    # Final fallback to core-mosquitto if still empty
    if [ -z "${MQTT_HOST}" ]; then
       bashio::log.info "Checking if 'core-mosquitto' is reachable..."
       if ping -c 1 core-mosquitto &> /dev/null; then
           bashio::log.info "Fallback: using core-mosquitto as MQTT host"
           MQTT_HOST="core-mosquitto"
           MQTT_PORT="1883"
       fi
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
bashio::log.info "Retention days: ${VIDEO_RETENTION_DAYS:-7}d, Max stored events: ${MAX_STORED_EVENTS:-100}"
bashio::log.info "Ingress entry: ${INGRESS_ENTRY}"

# Start Flask application
exec python3 /app/server.py
