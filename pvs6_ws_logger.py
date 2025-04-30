import argparse
import time
import json
import websocket
import ssl
from pprint import pprint

from influxdb import InfluxDBClient

DEFAULT_WS_URL = "ws://172.27.153.1:9002"

# ############################################################################

def influxdb_publish(measurement, fields, timestamp=None):
    if not fields:
        print("Not publishing empty data for:", measurement)
        return

    try:
        client = InfluxDBClient(host=args.influxdb_host,
                                port=args.influxdb_port,
                                username=args.influxdb_user,
                                password=args.influxdb_pass,
                                database=args.influxdb_db)

        payload = {
            'measurement': measurement,
            'time': timestamp if timestamp else int(time.time()),
            'fields': fields
        }

        if args.verbose:
            print(f"Publishing to InfluxDB: {payload}")

        client.write_points([payload], time_precision='s')

    except Exception as e:
        print("Failed to connect to InfluxDB:", e)
        print("  Payload was:", fields)


def on_message(ws, message):
    try:
        data = json.loads(message)
        if args.raw or args.verbose:
            pprint(data)

        if args.influxdb and data.get("notification") == "power":
            ts = data["params"].get("time")
            if ts:
                influxdb_publish("sunpower_power", data["params"], timestamp=int(ts))
            else:
                influxdb_publish("sunpower_power", data["params"])

    except json.JSONDecodeError:
        print("Invalid JSON received:", message)


def on_error(ws, error):
    print("WebSocket Error:", error)


def on_close(ws, close_status_code, close_msg):
    print("WebSocket closed:", close_status_code, close_msg)


def on_open(ws):
    if args.verbose:
        print("WebSocket connection opened")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ws_url", default=DEFAULT_WS_URL, help="WebSocket URL of the PVS6")
    parser.add_argument("--raw", action="store_true", help="Print raw data to stdout")
    parser.add_argument("--influxdb", action="store_true", help="Publish to InfluxDB")
    parser.add_argument("--influxdb_host", default="localhost", help="InfluxDB hostname")
    parser.add_argument("--influxdb_port", type=int, default=8086, help="InfluxDB port")
    parser.add_argument("--influxdb_user", help="InfluxDB username")
    parser.add_argument("--influxdb_pass", help="InfluxDB password")
    parser.add_argument("--influxdb_db", default="pvs6", help="InfluxDB database name")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    while True:
        try:
            ws = websocket.WebSocketApp(args.ws_url,
                                        on_open=on_open,
                                        on_message=on_message,
                                        on_error=on_error,
                                        on_close=on_close)

            ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        except Exception as e:
            print("Exception in WebSocket loop:", e)

        print("WebSocket connection lost or failed to start. Retrying in 5 minutes...")
        time.sleep(300)
