import json
import time
import random
import math
import os
from datetime import datetime

import paho.mqtt.client as mqtt

MQTT_HOST = os.getenv("MQTT_HOST", "mosquitto.monitoring.svc.cluster.local")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
PUBLISH_INTERVAL = int(os.getenv("PUBLISH_INTERVAL", "5"))
NUM_DEVICES = int(os.getenv("NUM_DEVICES", "5"))
ANOMALY_CHANCE = float(os.getenv("ANOMALY_CHANCE", "0.05"))

DEVICES = [
    {"id": f"device-{i:02d}", "location": loc}
    for i, loc in enumerate([
        "server-room-A", "warehouse-B", "office-1F",
        "lab-2F", "rooftop"
    ])
][:NUM_DEVICES]

def generate_sensor_data(device, t):
    """Generate realistic sensor data with daily patterns."""
    hour = datetime.now().hour
    daily_cycle = math.sin(2 * math.pi * (hour - 6) / 24)

    base_temp = 22 + 5 * daily_cycle
    base_humidity = 55 - 10 * daily_cycle
    base_pressure = 1013.25
    base_power = 150 + 50 * abs(daily_cycle)

    is_anomaly = random.random() < ANOMALY_CHANCE

    data = {
        "device_id": device["id"],
        "location": device["location"],
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "sensors": {
            "temperature": round(
                (base_temp + random.gauss(0, 1.5) + (25 if is_anomaly else 0)), 2
            ),
            "humidity": round(
                max(0, min(100, base_humidity + random.gauss(0, 3))), 2
            ),
            "pressure": round(
                base_pressure + random.gauss(0, 0.5), 2
            ),
            "power": round(
                max(0, base_power + random.gauss(0, 10) + (500 if is_anomaly else 0)), 2
            ),
        },
        "anomaly": is_anomaly,
    }
    return data


def on_connect(client, userdata, flags, rc, properties=None):
    print(f"[MQTT] Connected with result code {rc}", flush=True)


def main():
    print(f"[SIM] Starting IoT simulator: {NUM_DEVICES} devices, interval={PUBLISH_INTERVAL}s", flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect

    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            break
        except Exception as e:
            print(f"[SIM] Waiting for MQTT broker: {e}", flush=True)
            time.sleep(5)

    client.loop_start()
    t = 0

    while True:
        for device in DEVICES:
            data = generate_sensor_data(device, t)

            for sensor_name, value in data["sensors"].items():
                topic = f"iot/{device['id']}/{sensor_name}"
                client.publish(topic, json.dumps({"value": value, **{k: data[k] for k in ["device_id", "location", "timestamp", "anomaly"]}}))

            full_topic = f"iot/{device['id']}/all"
            client.publish(full_topic, json.dumps(data))

            if data["anomaly"]:
                print(f"[ANOMALY] {device['id']} @ {device['location']}: temp={data['sensors']['temperature']}C power={data['sensors']['power']}W", flush=True)

        t += 1
        time.sleep(PUBLISH_INTERVAL)


if __name__ == "__main__":
    main()
