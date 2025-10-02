![Github License](https://img.shields.io/github/license/dacarson/ChargePoint-SunPower-ChargeManager) ![Github Release](https://img.shields.io/github/v/release/dacarson/ChargePoint-SunPower-ChargeManager?display_name=tag)
# Solar Smart Charge Controller for ChargePoint Home Flex

This project provides an intelligent solar-aware charge controller for the ChargePoint Home Flex EV charger.  
It dynamically adjusts the vehicle charging speed based on excess solar power available, ensuring you maximize solar usage while minimizing grid draw.

The system consists of two components:
1. A WebSocket logger that continuously collects data from the SunPower PVS6 and stores it in InfluxDB
2. A charge controller that uses this data to make intelligent charging decisions

Charging amperage is updated every few minutes based on the most recent smoothed data.

---

## Features

- **InfluxDB-powered:** Pulls 5 mins (default - user controlled) average solar production and consumption for stable decision making.
- **ChargePoint Home Flex control:** Dynamically sets charger amperage up or down based on available solar energy.
- **Fast Night Charging:** Automatically switches to full-speed charging overnight when solar drops off.
- **Smart Amperage Selection:** Rounds up to the nearest allowed amperage setting to maximize charging speed.
- **Minimal Stress on Devices:** No direct high-frequency polling of the SunPower PVS6.
- **Stable Solar Predictions:** Uses a longer time window (30 mins default) for slope calculations to smooth out short-term fluctuations.
- **Simple Logging:** Logs to file, with optional quiet mode (no console output).
- **CLI Configurable:** Easy control via command-line parameters.

---

##  Architecture

1. The PVS6 WebSocket Logger (`pvs6_ws_logger.py`) continuously connects to the SunPower PVS6's WebSocket interface and streams real-time power data.
2. This data is stored in InfluxDB for historical analysis.
3. The Solar Charge Controller (`solar_charge_controller.py`) queries InfluxDB for:
   - Recent average power data (over the control interval)
   - Solar power trend (slope) over a longer window to predict future production
4. The controller calculates available excess solar based on **grid export** (negative consumption).
5. The controller dynamically adjusts ChargePoint Home Flex charging amperage accordingly.
6. The system falls back to maximum amperage for fast overnight charging. Overnight is defined as when solar production is less than 500W.

---

## Dependencies

You will need:

- [python-chargepoint](https://github.com/mbillow/python-chargepoint)  
  To interface with the ChargePoint Home Flex charger via API.
  
- [influxdb-python](https://github.com/influxdata/influxdb-python)  
  Python client for InfluxDB 1.x.

- [websocket-client](https://github.com/websocket-client/websocket-client)  
  Python WebSocket client for connecting to the PVS6.

Install required libraries:

```bash
pip install python-chargepoint influxdb websocket-client requests
```

## Configuration

### PVS6 WebSocket Logger

The WebSocket logger needs to be running continuously to collect data from your SunPower PVS6. It will automatically reconnect if the connection is lost.
If no data seems to be appearing on the WebSocket interface, make sure it is enabled. Follow instructions at [SunPowerManagement varVars][https://github.com/SunStrong-Management/pypvs/blob/main/doc/LocalAPI.md#set-a-variable-by-name] to enable WebSocket telemetry.

```bash
python pvs6_ws_logger.py \
  --ws_url "ws://your-pvs6-ip:9002" \
  --influxdb \
  --influxdb-host "localhost" \
  --influxdb-port 8086 \
  --influxdb-user "your_influx_user" \
  --influxdb-pass "your_influx_password" \
  --influxdb-db "pvs6" \
  --verbose
```

#### Arguments

| Argument&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | Required? | Description |
|:---|:---|:---|
| `--ws_url` | ❌ | (default: ws://172.27.153.1:9002) WebSocket URL of the PVS6 |
| `--raw` | ❌ | Print raw data to stdout |
| `--influxdb` | ❌ | Enable publishing to InfluxDB |
| `--influxdb_host` | ❌ | (default: localhost) InfluxDB host |
| `--influxdb_port` | ❌ | (default: 8086) InfluxDB port |
| `--influxdb_user` | ✅ | InfluxDB username |
| `--influxdb_pass` | ✅ | InfluxDB password |
| `--influxdb_db` | ❌ | (default: pvs6) InfluxDB database name |
| `--verbose` | ❌ | Enable verbose output |

### Solar Charge Controller

Once the WebSocket logger is running and collecting data, you can start the solar charge controller:

```bash
python solar_charge_controller.py \
  --email "your@email.com" \
  --password "yourpassword" \
  --influxdb-host "localhost" \
  --influxdb-port 8086 \
  --influxdb-user "your_influx_user" \
  --influxdb-pass "your_influx_password" \
  --influxdb-db "pvs6" \
  --control-interval 5 \
  --slope-window 30 \
  --log-file "solar_charge_controller.log" \
  --quiet
```

#### Arguments

| Argument&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | Required? | Description |
|:---|:---|:---|
| `--email` | ✅ | ChargePoint account email address |
| `--password` | ✅ | ChargePoint account password |
| `--influxdb-host` | ❌ | (default: localhost) InfluxDB host |
| `--influxdb-port` | ❌ | (default: 8086) InfluxDB port |
| `--influxdb-user` | ✅ | InfluxDB username |
| `--influxdb-pass` | ✅ | InfluxDB password |
| `--influxdb-db` | ❌ | (default: pvs6) InfluxDB database name |
| `--control-interval` | ❌ | (default: 5) Time in minutes between checking solar production and adjusting charging. This should not be too frequent because if the car is charging, it must stop it to change the amperage and restart it. |
| `--slope-window` | ❌ | (default: 30) Time window in minutes for calculating solar power trends. A longer window provides more stable predictions by smoothing out short-term fluctuations. |
| `--log-file` | ❌ | (default: `solar_charge_controller.log`) File to write logs to |
| `--quiet` | ❌ | Suppress console output, log only to file |

## Systemd Service Installation

Both components can be installed as systemd services for automatic startup and management. The repository includes example service files in the `etc/systemd/system/` directory and corresponding configuration files in `etc/default/`.

### Installation Steps

1. Copy the service files to the systemd directory:
   ```bash
   sudo cp etc/systemd/system/pvs6_ws_logger.service /etc/systemd/system/
   sudo cp etc/systemd/system/solar_charge_controller.service /etc/systemd/system/
   ```

2. Copy the default configuration files:
   ```bash
   sudo cp etc/default/pvs6_ws_logger /etc/default/
   sudo cp etc/default/solar_charge_controller /etc/default/
   ```

3. Edit the configuration files to set your credentials:
   ```bash
   sudo nano /etc/default/pvs6_ws_logger
   sudo nano /etc/default/solar_charge_controller
   ```

4. Enable and start the services:
   ```bash
   sudo systemctl enable pvs6_ws_logger
   sudo systemctl enable solar_charge_controller
   sudo systemctl start pvs6_ws_logger
   sudo systemctl start solar_charge_controller
   ```

### Service Management

- Check service status:
  ```bash
  sudo systemctl status pvs6_ws_logger
  sudo systemctl status solar_charge_controller
  ```

- View service logs:
  ```bash
  sudo journalctl -u pvs6_ws_logger -f
  sudo journalctl -u solar_charge_controller -f
  ```

- Restart services:
  ```bash
  sudo systemctl restart pvs6_ws_logger
  sudo systemctl restart solar_charge_controller
  ```

### Configuration Files

The configuration files in `/etc/default/` allow you to set command-line arguments for each service:

- `/etc/default/pvs6_ws_logger`:
  ```bash
  CMDARGS="--influxdb --influxdb_user $INFLUX_USER --influxdb_pass $INFLUX_PASS"
  ```

- `/etc/default/solar_charge_controller`:
  ```bash
  CMDARGS="--influxdb-user $INFLUX_USER --influxdb-pass $INFLUX_PASS --email $CHARGEPOINT_EMAIL --password $CHARGEPOINT_PASS --log-file /home/pi/ChargePoint-SunPower-ChargeManager/solar_charge_controller.log --quiet"
  ```

You can add or modify arguments in these files to customize the behavior of each service. After making changes, restart the services for them to take effect.

## License

This project is licensed under the [MIT License](LICENSE).

You are free to use, modify, and distribute this software with minimal restrictions.  
See the [LICENSE](LICENSE) file for full license text.

## Credits

- [python-chargepoint](https://github.com/mbillow/python-chargepoint) by [mbillow](https://github.com/mbillow)  
  Library used to interact with the ChargePoint Home Flex charger via API.

