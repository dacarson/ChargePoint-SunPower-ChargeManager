#!/usr/bin/env python3
import argparse
import time
import json
import websocket
import ssl
from pprint import pprint
import requests

DEFAULT_WS_URL = "ws://172.27.153.1:9002"

# InfluxDB (HTTP Line Protocol) settings
INFLUX_URL = "http://127.0.0.1:8086/write"
INFLUX_DB = "pvs6"          # change if needed
INFLUX_PRECISION = "s"             # seconds
session = requests.Session()

# ############################################################################

def influxdb_publish(measurement, fields, timestamp=None):
    """
    Publish one measurement via InfluxDB HTTP /write endpoint using line protocol.
    This mirrors the other logging script's approach.
    """
    if not fields:
        print("Not publishing empty data for:", measurement)
        return

    # Remove any 'time' field from fields (timestamp is passed separately)
    fields = {k: v for k, v in fields.items() if k != "time"}

    def escape_key(s):
        return str(s).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")

    def format_field_value(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        # Treat ints as ints with 'i' suffix; floats as floats
        if isinstance(v, int) and not isinstance(v, bool):
            return "{}i".format(v)
        if isinstance(v, float):
            # Use repr to avoid locale; ensure finite
            return repr(v)
        # Strings must be quoted with internal quotes escaped
        return "\"{}\"".format(str(v).replace("\\", "\\\\").replace("\"", "\\\""))

    # Build field set
    field_pairs = []
    for k, v in fields.items():
        try:
            field_pairs.append("{}={}".format(escape_key(k), format_field_value(v)))
        except Exception:
            # If a field can't be formatted, coerce to string
            field_pairs.append("{}=\"{}\"".format(escape_key(k), str(v)))

    if not field_pairs:
        print("No fields after formatting; skipping publish for:", measurement)
        return

    # Timestamp: seconds precision (per INFLUX_PRECISION)
    ts = int(timestamp if timestamp is not None else time.time())

    line = "{} {} {}".format(escape_key(measurement), ','.join(field_pairs), ts)
    payload = "\n".join([line])

    if args.verbose:
        print("Publishing to InfluxDB HTTP: {}".format(payload))

    try:
        params = {"db": INFLUX_DB, "precision": INFLUX_PRECISION}
        r = session.post(INFLUX_URL, params=params, data=payload.encode("utf-8"), timeout=10)
        r.raise_for_status()
        if args.verbose:
            try:
                print("Influx POST OK: status={} len={} resp={!r}".format(r.status_code, len(getattr(r, "text", "") or ""), (getattr(r, "text", "") or "")[:120]))
            except Exception:
                pass
    except Exception as e:
        status = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
        body = ""
        try:
            body = e.response.text[:200] if hasattr(e, "response") and hasattr(e.response, "text") else ""
        except Exception:
            body = ""
        print("Failed to POST to InfluxDB: {} status={} url={} db={}".format(e, status, INFLUX_URL, INFLUX_DB))
        if body:
            print("  Response:", body)
        print("  Payload was:", payload)


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

    except Exception:
        print("Invalid JSON received")


def on_error(ws, error):
    print("WebSocket Error:", error)


def on_close(ws, close_status_code, close_msg):
    print("WebSocket closed:", close_status_code, close_msg)


def on_open(ws):
    print("WebSocket connection opened (verbose={})".format(bool(args.verbose)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ws_url", default=DEFAULT_WS_URL, help="WebSocket URL of the PVS6")
    parser.add_argument("--raw", action="store_true", help="Print raw data to stdout")
    parser.add_argument("--influxdb", action="store_true", help="Publish to InfluxDB")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    print("pvs6_ws_logger starting; influxdb={}, db='{}', url='{}'".format(bool(args.influxdb), INFLUX_DB, INFLUX_URL))

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
