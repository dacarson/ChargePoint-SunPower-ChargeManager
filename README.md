![Github License](https://img.shields.io/github/license/dacarson/ChargePoint-SunPower-ChargeManager) ![Github Release](https://img.shields.io/github/v/release/dacarson/ChargePoint-SunPower-ChargeManager?display_name=tag)
# Solar Smart Charge Controller for ChargePoint Home Flex

This project provides an intelligent solar-aware charge controller for the ChargePoint Home Flex EV charger.  
It dynamically adjusts the vehicle charging speed based on excess solar power available, ensuring you maximize solar usage while minimizing grid draw.

The controller queries historical solar production and consumption data from a Prometheus database populated by a SunPower PVS exporter.  
Charging amperage is updated every few minutes based on the most recent smoothed data.

---

## Features

- **Prometheus-powered:** Pulls 5 mins (default - user controlled) average solar production and consumption for stable decision making.
- **ChargePoint Home Flex control:** Dynamically sets charger amperage up or down based on available solar energy.
- **Fast Night Charging:** Automatically switches to full-speed charging overnight when solar drops off.
- **Smart Amperage Selection:** Rounds up to the nearest allowed amperage setting to maximize charging speed.
- **Minimal Stress on Devices:** No direct high-frequency polling of the SunPower PVS6.
- **Simple Logging:** Logs to file, with optional quiet mode (no console output).
- **CLI Configurable:** Easy control via command-line parameters.

---

##  Architecture

1. Prometheus scrapes solar production and consumption metrics from SunPower PVS6.
2. This controller queries Prometheus for the user defined control-interval average data.
3. Calculates available excess solar based on **grid export** (negative consumption).
4. Dynamically adjusts ChargePoint Home Flex charging amperage accordingly.
5. Falls back to maximum amperage for fast overnight charging.

---

## Dependencies

You will need:

- [python-chargepoint](https://github.com/mbillow/python-chargepoint)  
  To interface with the ChargePoint Home Flex charger via API.
  
- [sunpower-pvs-exporter](https://github.com/ginoledesma/sunpower-pvs-exporter)  
  A small Prometheus exporter that exposes SunPower PVS6 production and consumption metrics.

Install required libraries:

```bash
pip install python-chargepoint requests
```

## Configuration

Make sure your Prometheus server is scraping your SunPower PVS6 via the [`sunpower-pvs-exporter`](https://github.com/ginoledesma/sunpower-pvs-exporter), and is reachable.

### Example Command to Run Controller

```bash
python solar_charge_controller.py \
  --email "your@email.com" \
  --password "yourpassword" \
  --prometheus-url "http://localhost:9090" \
  --control-interval 5 \
  --log-file "solar_charge_controller.log" \
  --quiet
```
### Arguments

| Argument&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | Required? | Description |
|:---|:---|:---|
| `--email` | ✅ | ChargePoint account email address |
| `--password` | ✅ | ChargePoint account password |
| `--prometheus-url` | ✅ | Base URL of your Prometheus server (e.g., `http://localhost:9090`) |
| `--control-interval` | ❌ | (default: 5) Time in minutes between checking solar production and adjusting charging. This should not be too frequent because if the car is charging, it must stop it to change the amperage and restart it. |
| `--log-file` | ❌ | (default: `solar_charge_controller.log`) File to write logs to |
| `--quiet` | ❌ | Suppress console output, log only to file |

## License

This project is licensed under the [MIT License](LICENSE).

You are free to use, modify, and distribute this software with minimal restrictions.  
See the [LICENSE](LICENSE) file for full license text.

## Credits

- [python-chargepoint](https://github.com/mbillow/python-chargepoint) by [mbillow](https://github.com/mbillow)  
  Library used to interact with the ChargePoint Home Flex charger via API.

- [sunpower-pvs-exporter](https://github.com/ginoledesma/sunpower-pvs-exporter) by [ginoledesma](https://github.com/ginoledesma)  
  Exporter used to provide SunPower PVS6 metrics into Prometheus.

