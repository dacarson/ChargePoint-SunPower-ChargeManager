import sys
import time
import logging
import argparse
import requests
import math
from python_chargepoint import ChargePoint

# --- FUNCTIONS ---

def parse_args():
    parser = argparse.ArgumentParser(description="Solar Smart Charge Controller for ChargePoint Home Flex (via Prometheus).")
    parser.add_argument("--email", required=True, help="ChargePoint account email")
    parser.add_argument("--password", required=True, help="ChargePoint account password")
    parser.add_argument("--prometheus-url", required=True, help="Prometheus base URL (e.g., http://localhost:9090)")
    parser.add_argument("--log-file", default="solar_charge_controller.log", help="Log file path")
    parser.add_argument("--control-interval", type=int, default=5, help="Control interval in minutes (default: 5)")
    parser.add_argument("--quiet", action="store_true", help="Suppress console logging (only log to file)")
    return parser.parse_args()

def setup_logging(log_file, quiet):
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    if not quiet:
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        console.setFormatter(formatter)
        logging.getLogger().addHandler(console)

def query_prometheus(prometheus_url, promql_query):
    try:
        response = requests.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": promql_query},
            timeout=30
        )
        response.raise_for_status()
        result = response.json()
        return float(result["data"]["result"][0]["value"][1])
    except Exception as e:
        logging.error(f"Error querying Prometheus ({promql_query}): {e}")
        return None

def get_solar_power_status(prometheus_url, control_interval):
    promql_window = f"[{control_interval}m]"

    production_query = f'avg_over_time(sunpower_pvs_power_meter_average_real_power_watts{{mode="production"}}{promql_window})'
    consumption_query = f'avg_over_time(sunpower_pvs_power_meter_average_real_power_watts{{mode="consumption"}}{promql_window})'

    production_watts = query_prometheus(prometheus_url, production_query)
    consumption_watts = query_prometheus(prometheus_url, consumption_query)

    if production_watts is None or consumption_watts is None:
        return None

    # Excess = grid export (negative consumption)
    if consumption_watts < 0:
        excess_solar_watts = abs(consumption_watts)
    else:
        excess_solar_watts = 0

    return {
        "production_watts": production_watts,
        "consumption_watts": consumption_watts,
        "excess_solar_watts": excess_solar_watts
    }

def determine_target_amperage(avg_excess_solar_watts, allowed_amps, voltage=240):
    if avg_excess_solar_watts <= 0:
        return 0

    ideal_amps = avg_excess_solar_watts / voltage

    # Round UP to nearest allowed amps
    possible_amps = [amp for amp in allowed_amps if amp >= ideal_amps]
    if not possible_amps:
        return max(allowed_amps)
    return min(possible_amps)

def get_current_charging_watts(client):
    """Fetch current car charging load if charging."""
    try:
        charging_status = client.get_user_charging_status()
        if charging_status and charging_status.charging_status == "CHARGING":
            session = client.get_charging_session(charging_status.session_id)
            current_amps = session.amperage
            charging_watts = current_amps * 240
            return charging_watts
        else:
            return 0
    except Exception as e:
        logging.error(f"Failed to fetch current charging watts: {e}")
        return 0

# --- MAIN ---

def main():
    args = parse_args()

    setup_logging(args.log_file, args.quiet)

    email = args.email
    password = args.password
    prometheus_url = args.prometheus_url
    control_interval = args.control_interval  # minutes

    logging.info("Connecting to ChargePoint...")
    client = ChargePoint(email, password)
    chargers = client.get_home_chargers()

    if not chargers:
        logging.error("No home chargers found.")
        sys.exit(1)

    charger_id = chargers[0]
    logging.info(f"Found charger {charger_id}")

    charger_status = client.get_home_charger_status(charger_id)
    allowed_amps = charger_status.possible_amperage_limits
    min_amperage = min(allowed_amps)
    minimum_watts_required = min_amperage * 240

    logging.info(f"Minimum amperage is {min_amperage}A, requiring at least {minimum_watts_required}W of solar excess to start charging.")

    while True:
        try:
            solar = get_solar_power_status(prometheus_url, control_interval)
            if not solar:
                logging.warning("No solar data. Skipping...")
                time.sleep(control_interval * 60)
                continue

            production = solar["production_watts"]
            consumption = solar["consumption_watts"]
            grid_excess = solar["excess_solar_watts"]

            current_charging_watts = get_current_charging_watts(client)
            excess = grid_excess + current_charging_watts

            logging.info(f"{control_interval}-min averages - Production: {production:.1f}W, Grid Excess: {grid_excess:.1f}W, Current Charging Load: {current_charging_watts:.1f}W, True excess: {excess:.1f}W")

            charger_status = client.get_home_charger_status(charger_id)
            allowed_amps = charger_status.possible_amperage_limits
            current_amperage = charger_status.amperage_limit
            charging_status = charger_status.charging_status

            if production < 500:
                target_amps = max(allowed_amps)
                logging.info(f"Low production ({production:.1f}W). Setting to max amperage {target_amps}A for fast charging.")
            elif excess >= minimum_watts_required:
                target_amps = determine_target_amperage(excess, allowed_amps)
                logging.info(f"Excess solar ({excess:.1f}W). Setting amperage to {target_amps}A.")
            else:
                target_amps = 0
                logging.info(f"Not enough excess solar ({excess:.1f}W) < minimum ({minimum_watts_required:.1f}W). Stopping charging.")

            # --- Apply the charging decision ---

            if target_amps == 0:
                if charging_status == "CHARGING":
                    logging.info("Stopping charging...")
                    session = client.get_charging_session(client.get_user_charging_status().session_id)
                    session.stop()
                else:
                    logging.info("Already not charging.")
            else:
                if current_amperage != target_amps:
                    logging.info(f"Changing amperage from {current_amperage}A to {target_amps}A...")

                    was_charging = (charging_status == "CHARGING")
                    if was_charging:
                        session = client.get_charging_session(client.get_user_charging_status().session_id)
                        device_id = session.device_id
                        session.stop()
                    else:
                        device_id = charger_status.mac_address

                    client.set_amperage_limit(charger_id, target_amps)
                    logging.info(f"Amperage set command sent for {target_amps}A.")

                    time.sleep(5)

                    updated_status = client.get_home_charger_status(charger_id)
                    confirmed_amperage = updated_status.amperage_limit

                    if confirmed_amperage == target_amps:
                        logging.info(f"Confirmed amperage: {confirmed_amperage}A.")
                    else:
                        logging.warning(f"Amperage mismatch! Set {target_amps}A but charger reports {confirmed_amperage}A.")

                    if was_charging:
                        logging.info("Restarting charging session...")
                        client.start_charging_session(device_id)
                else:
                    logging.info(f"Amperage already set correctly ({current_amperage}A). No change needed.")
                    
                # --- Start the charger now if we are not already charging and we plugged in (aka AVAILABLE)
                if charging_status == "AVAILABLE":
                    logging.info("Starting charging session...")
                    session = client.start_charging_session(charger_id)

            time.sleep(control_interval * 60)

        except Exception as e:
            logging.error(f"Error in main loop: {e}")
            time.sleep(control_interval * 60)

if __name__ == "__main__":
    main()
