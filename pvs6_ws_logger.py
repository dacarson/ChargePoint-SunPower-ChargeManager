#!/usr/bin/env python3
import argparse
import time
import json
import websocket
import ssl
from pprint import pprint
import requests
import base64
import os
import urllib3

# Disable SSL warnings since we're connecting to local PVS6 devices
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_PVS6_IP = "172.27.153.1"

# InfluxDB (HTTP Line Protocol) settings
INFLUX_URL = "http://127.0.0.1:8086/write"
INFLUX_DB = "pvs6"          # change if needed
INFLUX_PRECISION = "s"             # seconds
session = requests.Session()

# ############################################################################

def enable_telemetry(ip_address, serial_number):
    """
    Enable web socket telemetry on PVS6 by logging in and setting the telemetry flag.
    Based on the enable_telemetry.sh script logic.
    """
    try:
        # Extract last 5 digits of serial number as password
        password = serial_number[-5:]
        
        if args.verbose:
            print(f"Enabling telemetry for IP: {ip_address}, Serial: {serial_number}")
            print(f"Password (last 5 digits): {password}")
        
        # Create authorization header
        auth_string = f"ssm_owner:{password}"
        auth_header = base64.b64encode(auth_string.encode()).decode()
        
        # Create session for cookie management (like the shell script)
        session = requests.Session()
        session.verify = False  # Disable SSL verification like the shell script
        
        # Login to PVS6 (this will set cookies in the session)
        login_url = f"https://{ip_address}/auth?login"
        login_headers = {"Authorization": f"basic {auth_header}"}
        
        if args.verbose:
            print("Logging in...")
        
        login_response = session.get(login_url, headers=login_headers, timeout=10)
        
        if args.verbose:
            print(f"Login response: {login_response.status_code} - {login_response.text}")
        
        # Check if login was successful
        if login_response.status_code != 200:
            print(f"Login failed with status: {login_response.status_code}")
            return False
        
        # Enable web socket telemetry using the same session (cookies will be automatically included)
        telemetry_url = f"https://{ip_address}/vars?set=/sys/telemetryws/enable=1"
        
        if args.verbose:
            print("Enabling web socket telemetry...")
        
        telemetry_response = session.get(telemetry_url, timeout=10)
        
        if args.verbose:
            print(f"Telemetry response: {telemetry_response.status_code} - {telemetry_response.text}")
        
        # Check if telemetry was successfully enabled
        if telemetry_response.status_code == 200:
            print("Telemetry enabled successfully")
            return True
        else:
            print(f"Failed to enable telemetry: {telemetry_response.status_code}")
            return False
            
    except Exception as e:
        print(f"Error enabling telemetry: {e}")
        return False


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


def on_open_with_telemetry(ws):
    """WebSocket on_open handler that enables telemetry after connection is established"""
    print("WebSocket connection opened (verbose={})".format(bool(args.verbose)))
    print("Enabling telemetry...")
    enable_telemetry(args.ip, args.serial_number)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", default=DEFAULT_PVS6_IP, help="IP address of the PVS6")
    parser.add_argument("--serial_number", required=True, help="Serial number of the PVS6 (required for telemetry enablement)")
    parser.add_argument("--raw", action="store_true", help="Print raw data to stdout")
    parser.add_argument("--influxdb", action="store_true", help="Publish to InfluxDB")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    # Build WebSocket URL from IP address
    ws_url = f"ws://{args.ip}:9002"
    
    print("pvs6_ws_logger starting; influxdb={}, db='{}', url='{}'".format(bool(args.influxdb), INFLUX_DB, INFLUX_URL))
    print(f"Connecting to PVS6 at {args.ip}")

    while True:
        try:
            ws = websocket.WebSocketApp(ws_url,
                                        on_open=on_open_with_telemetry,
                                        on_message=on_message,
                                        on_error=on_error,
                                        on_close=on_close)

            ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        except Exception as e:
            print("Exception in WebSocket loop:", e)

        print("WebSocket connection lost or failed to start. Retrying in 5 minutes...")
        time.sleep(300)
