import os
import sys
import time
import logging

logging.basicConfig(level=logging.DEBUG)

# Add the vendor library to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "ezviz-camera-proxy/rootfs/app")))

from ezviz_client import EzvizClient
import yaml

def main():
    # Parse credentials from config.yaml
    with open("ezviz-camera-proxy/config.yaml", "r") as f:
        config = yaml.safe_load(f)
    options = config.get("options", {})
    
    username = options.get("ezviz_username", os.environ.get("EZVIZ_USERNAME"))
    password = options.get("ezviz_password", os.environ.get("EZVIZ_PASSWORD"))
    region = options.get("ezviz_region", "apiieu.ezvizlife.com")
    
    if not username:
        print("Set EZVIZ_USERNAME or put it in config.yaml options to test.")
        return

    client = EzvizClient(username=username, password=password, region=region)
    print("Logging in...")
    client.login()
    print("Logged in!")

    def on_message(msg):
        print("\n" + "="*50)
        print("REAL-TIME MQTT EVENT RECEIVED!")
        print(msg)
        print("="*50 + "\n")

    mqtt_client = client._client.get_mqtt_client(on_message_callback=on_message)
    print("Connecting to Ezviz MQTT push server...")
    mqtt_client.connect()
    print("Connected! Waiting for events. Ring the doorbell or trigger motion...")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
        mqtt_client.stop()

if __name__ == "__main__":
    main()
