#!/bin/bash

# Script to enable web socket telemetry on PVS6
# Usage: ./enable_telemetry.sh <IP_ADDRESS> <SERIAL_NUMBER>

if [ $# -ne 2 ]; then
    echo "Usage: $0 <IP_ADDRESS> <SERIAL_NUMBER>"
    echo "Example: $0 192.168.1.8 1234567890"
    exit 1
fi

IP_ADDRESS=$1
SERIAL_NUMBER=$2

# Extract last 5 digits of serial number as password
PWD=$(echo $SERIAL_NUMBER | tail -c 6)

echo "IP Address: $IP_ADDRESS"
echo "Serial Number: $SERIAL_NUMBER"
echo "Password (last 5 digits): $PWD"

# Create authorization header
AUTH=$(echo -n "ssm_owner:$PWD" | base64)

echo "Logging in..."

# Login to PVS6
LOGIN_RESPONSE=$(curl -s -k \
    -b cookies.txt \
    -c cookies.txt \
    -H "Authorization: basic $AUTH" \
    https://$IP_ADDRESS/auth?login)

echo "Login response: $LOGIN_RESPONSE"

echo "Enabling web socket telemetry..."

# Enable web socket telemetry
TELEMETRY_RESPONSE=$(curl -s -k \
    -b cookies.txt \
    -c cookies.txt \
    https://$IP_ADDRESS/vars?match=telemetryws)

echo "Current Telemetry response: $TELEMETRY_RESPONSE"

echo "Enabling web socket telemetry..."

# Enable web socket telemetry
SET_TELEMETRY_RESPONSE=$(curl -s -k \
    -b cookies.txt \
    -c cookies.txt \
    https://$IP_ADDRESS/vars?set=/sys/telemetryws/enable=1)

echo "Set telemetry response: $SET_TELEMETRY_RESPONSE"

echo "Done!"
