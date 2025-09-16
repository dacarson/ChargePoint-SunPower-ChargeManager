import sys
import time
import logging
import argparse
from python_chargepoint import ChargePoint
from python_chargepoint.exceptions import ChargePointCommunicationException
from influxdb import InfluxDBClient

# --- GLOBAL VARIABLES ---

current_charging_session = None
# Cache the last known good charging power (in watts) to use on transient API errors
last_known_charging_watts = 0.0

# --- FUNCTIONS ---

def parse_args():
    parser = argparse.ArgumentParser(description="Solar Smart Charge Controller for ChargePoint Home Flex (via InFluxDB).")

    # ChargePoint credentials
    parser.add_argument("--username", required=True, help="ChargePoint account username or email")
    parser.add_argument("--password", required=True, help="ChargePoint account password")

    # InfluxDB 1.x parameters
    parser.add_argument("--influxdb-host", default="localhost", help="InfluxDB host (default: localhost)")
    parser.add_argument("--influxdb-port", type=int, default=8086, help="InfluxDB port (default: 8086)")
    parser.add_argument("--influxdb-user", required=True, help="InfluxDB username")
    parser.add_argument("--influxdb-pass", required=True, help="InfluxDB password")
    parser.add_argument("--influxdb-db", default="pvs6", help="InfluxDB database name (default: pvs6)")

    # Control loop options
    parser.add_argument("--control-interval", type=int, default=5, help="Control interval in minutes (default: 5)")
    parser.add_argument("--slope-window", type=int, default=30, help="Time window in minutes for slope calculation (default: 30)")
    parser.add_argument("--log-file", default="solar_charge_controller.log", help="Log file path")
    parser.add_argument("--quiet", action="store_true", help="Suppress console logging")

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

def log_control_metrics_to_influx(influx_client, solar_slope, predicted_excess, current_charging_watts, target_amps, current_amperage):
    json_body = [{
        "measurement": "solar_charge_control",
        "fields": {
            "solar_slope_w_per_s": float(solar_slope),
            "excess_solar_watts": float(predicted_excess),
            "charging_power_watts": float(current_charging_watts),
            "target_amperage": int(target_amps),
            "current_amperage": int(current_amperage),
        }
    }]
    try:
        influx_client.write_points(json_body)
    except Exception as e:
        logging.warning(f"Failed to write control metrics to InfluxDB: {e}")

def log_charging_status_debug(client, charging_status, charger_status=None, session_snapshot=None):
    """Log detailed charging status information for debugging."""
    if not charging_status:
        logging.info("No charging status available")
        return
    
    try:
        session = session_snapshot if session_snapshot is not None else client.get_charging_session(charging_status.session_id)
        if charger_status is None:
            charger_status = client.get_home_charger_status_v2(charging_status.stations[0].id)
        logging.info(f"=== CHARGING STATUS DEBUG ===")
        logging.info(f"UserChargingStatus.state: {charging_status.state}")
        logging.info(f"HomeChargerStatusV2.charging_status: {charger_status.charging_status}")
        logging.info(f"Session.charging_state: {session.charging_state}")
        logging.info(f"Session.power_kw: {session.power_kw}")
        logging.info(f"Session.energy_kwh: {session.energy_kwh}")
        logging.info(f"Session.charging_time: {session.charging_time}")
        logging.info(f"Session.last_update_data_timestamp: {session.last_update_data_timestamp}")
        if session.update_data:
            latest_update = session.update_data[-1]
            logging.info(f"Latest update - energy_kwh: {latest_update.energy_kwh}, power_kw: {latest_update.power_kw}")
        logging.info(f"===============================")
    except Exception as e:
        logging.warning(f"Failed to log charging status debug info: {e}")

def get_solar_power_status(influx_client, control_interval_minutes, slope_window_minutes):
    try:
        now = int(time.time())
        control_start_time = now - (control_interval_minutes * 60)
        slope_start_time = now - (slope_window_minutes * 60)
        
        # Time clauses for different queries
        control_time_clause = f"time >= {control_start_time}s and time <= {now}s"
        slope_time_clause = f"time >= {slope_start_time}s and time <= {now}s"

        # Average pv_p (production) over the control window
        production_query = f'SELECT MEAN("pv_p") FROM "sunpower_power" WHERE {control_time_clause}'
        # Average net_p (grid import/export) over the control window
        net_query = f'SELECT MEAN("net_p") FROM "sunpower_power" WHERE {control_time_clause}'
        # Estimate slope via linear regression of pv_p over the longer slope window
        slope_query = (
            f'SELECT DERIVATIVE(MEAN("pv_p"), 1s) FROM "sunpower_power" '
            f'WHERE {slope_time_clause} GROUP BY time(1m) fill(null)'
        )

        prod_result = influx_client.query(production_query)
        net_result = influx_client.query(net_query)
        slope_result = influx_client.query(slope_query)

        prod_point = list(prod_result.get_points())
        net_point = list(net_result.get_points())
        slope_points = list(slope_result.get_points())
        valid_slopes = [pt['derivative'] for pt in slope_points if pt['derivative'] is not None]

        if not prod_point or not net_point or not valid_slopes:
            logging.warning("Influx query returned no data.")
            return None

        # Convert kW → W
        production_watts = prod_point[0]['mean'] * 1000
        net_watts = net_point[0]['mean'] * 1000
        solar_slope_watts_per_s = (sum(valid_slopes) / len(valid_slopes)) * 1000  # kW/s → W/s

        return {
            "production_watts": production_watts,
            "consumption_watts": net_watts,  # note: 'net' = grid power
            "solar_slope_watts_per_second": solar_slope_watts_per_s
        }

    except Exception as e:
        logging.error(f"Failed to query InfluxDB: {e}")
        return None

def determine_target_amperage(avg_excess_solar_watts, allowed_amps, voltage=240):
    if avg_excess_solar_watts <= 0:
        return 0

    # Ensure allowed_amps is a list before iterating
    if not isinstance(allowed_amps, (list, tuple)):
        logging.error(f"allowed_amps is not a list: {type(allowed_amps)} = {allowed_amps}")
        # Log additional debug info only when there's a problem
        logging.error(f"DEBUG: determine_target_amperage called with avg_excess_solar_watts={avg_excess_solar_watts}, allowed_amps type={type(allowed_amps)}, allowed_amps value={allowed_amps}, voltage={voltage}")
        return 0

    # Adjust ideal_amps logic to handle float rounding just below min allowed
    ideal_amps = max(avg_excess_solar_watts / voltage, min(allowed_amps) - 0.5)

    # Round UP to nearest allowed amps
    possible_amps = [amp for amp in allowed_amps if amp >= ideal_amps]
    
    if not possible_amps:
        result = max(allowed_amps)
        return result
    
    result = min(possible_amps)
    return result

def initialize_charging_session_if_active(client, charger_id, charger_status=None, user_charging_status=None, session_snapshot=None):
    """
    Determine whether an active charging session exists and, if so, initialize
    the global current_charging_session.

    Returns:
        True  - determination is reliable; you may trust current_charging_session
                (it will be set to a valid session when active, or None when not active).
        False - determination is not reliable due to API errors; do not trust
                current_charging_session.
    """
    global current_charging_session

    # 1) Get user charging status (primary signal)
    try:
        status = user_charging_status if user_charging_status is not None else client.get_user_charging_status()
    except Exception as e:
        logging.warning(f"Failed to fetch user charging status: {e}")
        return False

    # No status -> confidently not active
    if status is None:
        current_charging_session = None
        logging.info("No user charging status; treating as not active.")
        return True

    state = getattr(status, "state", None)

    # 2) Try to fetch device status (secondary signal). If this fails, we continue
    # where possible, but must return False if we cannot confidently determine activity.
    try:
        device_status = charger_status if charger_status is not None else client.get_home_charger_status_v2(charger_id)
    except Exception as e:
        device_status = None
        logging.warning(f"Failed to fetch home charger status: {e}")

    # 3) Fully charged: consider active only if still drawing meaningful power
    if state == "fully_charged":
        try:
            sess = session_snapshot if session_snapshot is not None else client.get_charging_session(status.session_id)
            power_kw = getattr(sess, "power_kw", 0.0) if sess else 0.0
            if sess and power_kw >= 0.1:
                current_charging_session = sess
                logging.info(f"Initializing session despite fully_charged; drawing {power_kw:.3f}kW")
                return True
            current_charging_session = None
            logging.info("Fully charged with low/zero draw; no active session.")
            return True
        except Exception as e:
            logging.warning(f"Failed to verify power for 'fully_charged': {e}")
            return False

    # 4) Active states -> ensure we can fetch/initialize the session
    if state in ("waiting", "in_use") or (device_status and getattr(device_status, "charging_status", None) == "CHARGING"):
        try:
            current_charging_session = session_snapshot if session_snapshot is not None else client.get_charging_session(status.session_id)
            logging.info(f"Found and initialized global charging session: {current_charging_session.session_id}")
            return True
        except Exception as e:
            logging.warning(f"Failed to get charging session: {e}")
            return False

    # 5) Anything else -> not active (reliable)
    current_charging_session = None
    logging.info("No active charging session found.")
    return True



def get_current_charging_watts(client, charger_id, charger_status, user_charging_status, session_snapshot=None):
    """Return present charging load (W). Clears session and returns 0W if not charging/unplugged."""
    global current_charging_session
    global last_known_charging_watts
    POWER_THRESHOLD_KW = 0.1  # ~100W => effectively not charging
    VOLTAGE = 240

    def _estimate_from_amperage(charger_status_obj):
        """Estimate watts from current amperage limit (no extra network call)."""
        try:
            estimated = int(charger_status_obj.amperage_limit * VOLTAGE)
            logging.info(f"Estimating charging power: {estimated}W (based on {charger_status_obj.amperage_limit}A @ {VOLTAGE}V)")
            return estimated
        except Exception as e:
            logging.warning(f"Failed to estimate from amperage: {e}")
            return last_known_charging_watts

    # Ensure we can rely on the current_charging_session state
    init_reliable = True
    if current_charging_session is None:
        init_reliable = initialize_charging_session_if_active(client, charger_id, charger_status, user_charging_status, session_snapshot)

    if not init_reliable:
        logging.info("Session initialization not reliable; returning last known charging watts.")
        return last_known_charging_watts

    if current_charging_session is None:
        # Determination is reliable and indicates not charging
        last_known_charging_watts = 0
        return 0

    try:
        # logging.info(f"get_current_charging_watts current_charging_session: {current_charging_session}")

        status = user_charging_status
        # logging.info(f"Checking charging status for active charging session: {status}")

        # We are charging as we have a valid current_charging_session, but the user_charging_status is None -> last_known_charging_watts
        if status is None:
            # If the car is unplugged or charger is clearly stopped, clear the session
            try:
                unplugged = hasattr(charger_status, "plugged_in") and (not charger_status.plugged_in)
            except Exception:
                unplugged = False

            try:
                stopped = hasattr(charger_status, "charging_status") and str(charger_status.charging_status) in ("CHARGING_STOPPED", "AVAILABLE", "IDLE")
            except Exception:
                stopped = False

            # Consider the cached session stale if its last update is older than 2x its reported update_period (default ~8s -> 16s)
            def _session_is_stale(sess) -> bool:
                try:
                    # Prefer the latest update timestamp if present
                    if sess and getattr(sess, "update_data", None):
                        ts = sess.update_data[-1].timestamp
                    else:
                        ts = getattr(sess, "last_update_data_timestamp", None)
                    if ts is None:
                        return False
                    now_ts = time.time()
                    sess_ts = ts.timestamp() if hasattr(ts, "timestamp") else float(ts)
                    # Fallback to 120s if update_period is missing
                    upd_period = getattr(sess, "update_period", 8000) / 1000.0
                    max_age = max(120.0, 2.0 * upd_period)
                    return (now_ts - sess_ts) > max_age
                except Exception:
                    return False

            stale = _session_is_stale(session_snapshot if session_snapshot is not None else current_charging_session)

            if unplugged or stopped or stale:
                logging.info(
                    f"No user_charging_status; unplugged={unplugged}, stopped={stopped}, stale_session={stale}. Clearing session and returning 0W."
                )
                current_charging_session = None
                last_known_charging_watts = 0
                return 0

            # Otherwise treat as transient API hiccup and fall back to the last known watts
            logging.info("We have an active session via current_charging_session, but no user_charging_status. Returning last_known_charging_watts.")
            return last_known_charging_watts

        # Fully charged -> only keep if still drawing meaningful power
        if status.state == "fully_charged":
            try:
                session = session_snapshot if session_snapshot is not None else client.get_charging_session(status.session_id)
                power_kw = getattr(session, "power_kw", None)
                if power_kw is not None and power_kw >= POWER_THRESHOLD_KW:
                    watts = int(power_kw * 1000)
                    logging.info(f"State 'fully_charged' but still drawing {watts}W; reporting actual.")
                    last_known_charging_watts = watts
                    return watts
                logging.info(f"Fully charged and low power ({0.0 if power_kw is None else power_kw:.3f}kW). Clearing session.")
                current_charging_session = None
                last_known_charging_watts = 0
                return 0
            except Exception as e:
                logging.warning(f"Failed to verify power for 'fully_charged': {e}. Clearing session.")
                current_charging_session = None
                last_known_charging_watts = 0
                return 0

        # Waiting -> estimate from amperage
        if status.state == "waiting":
            watts = _estimate_from_amperage(charger_status)
            last_known_charging_watts = watts
            return watts

        # Actively charging -> prefer actual session measurement
        if status.state == "in_use":
            try:
                sess = session_snapshot if session_snapshot is not None else current_charging_session
                if session_snapshot is None:
                    sess.refresh()
                actual_kw = sess.update_data[-1].power_kw
                watts = int(actual_kw * 1000)
                logging.info(f"Car is in use, using actual measurement: {watts}W")
                last_known_charging_watts = watts
                return watts
            except Exception as e:
                logging.warning(f"Failed to get actual power measurement: {e}. Using amperage estimate.")
                watts = _estimate_from_amperage(charger_status)
                last_known_charging_watts = watts
                return watts

        # Any other state -> treat as not charging
        logging.info(f"Unknown charging state: {status.state}. Assuming not charging.")
        last_known_charging_watts = 0
        return 0

    except Exception as e:
        logging.warning(
    f"Failed to refresh cached charging session: {e}. Charger plugged_in={getattr(charger_status, 'plugged_in', 'unknown')} status={getattr(charger_status, 'charging_status', 'unknown')}. Returning last known charging watts ({last_known_charging_watts}W)."
)
        # Do not clear the session here; transient API errors are common.
        return last_known_charging_watts

def apply_charging_decision(client, charger_id, charger_status, target_amps, min_amperage, user_charging_status=None, session_snapshot=None):
    global current_charging_session
    current_amperage = charger_status.amperage_limit

    if target_amps == 0:
        if get_current_charging_watts(client, charger_id, charger_status, user_charging_status, session_snapshot) > 0:
            if current_charging_session is not None:
                current_charging_session.stop()
                current_charging_session = None  # Clear global session when stopping
                logging.info("Stopped charging and cleared global session.")
            else:
                logging.warning("Wanted to stop charging but session object was None; skipping stop.")
        else:
            logging.info("Already not charging.")
            if current_amperage != min_amperage:
                client.set_amperage_limit(charger_id, min_amperage)
                logging.info(f"Amperage set command set for minimum for {min_amperage}A.")
            current_charging_session = None  # Ensure global session is cleared
        confirmed_amperage = 0
    else:
        if current_amperage != target_amps:
            logging.info(f"Changing amperage from {current_amperage}A to {target_amps}A...")

            was_charging = get_current_charging_watts(client, charger_id, charger_status, user_charging_status, session_snapshot) > 0
            
            if was_charging and current_charging_session is not None:
                # Use the new API to change amperage during active charging
                try:
                    logging.info(f"Using new API to change amperage during active charging session: {current_charging_session.session_id}")
                    response = current_charging_session.set_charge_amperage_limit(target_amps)
                    logging.info(f"Amperage limit change response: {response.status} (desired: {response.desired_value}A)")
                    
                    if response.status == "APPLYING":
                        logging.info("Amperage limit change is being applied to the charger")
                        confirmed_amperage = target_amps
                    else:
                        logging.warning(f"Unexpected amperage limit response status: {response.status}")
                        # Fall back to old method if new API doesn't work as expected
                        logging.info("Falling back to stop/start method for amperage change")
                        current_charging_session.stop()
                        current_charging_session = None
                        client.set_amperage_limit(charger_id, target_amps)
                        current_charging_session = client.start_charging_session(charger_id)
                        if current_charging_session:
                            logging.info(f"Restarted charging session: {current_charging_session.session_id}")
                        confirmed_amperage = target_amps
                        
                except Exception as e:
                    logging.warning(f"Failed to use new amperage limit API: {e}")
                    logging.info("Falling back to stop/start method for amperage change")
                    current_charging_session.stop()
                    current_charging_session = None
                    client.set_amperage_limit(charger_id, target_amps)
                    current_charging_session = client.start_charging_session(charger_id)
                    if current_charging_session:
                        logging.info(f"Restarted charging session: {current_charging_session.session_id}")
                    confirmed_amperage = target_amps
            else:
                # Not charging, use the standard method
                if was_charging and current_charging_session is None:
                    logging.warning("Expected active session to stop, but session object was None.")

                client.set_amperage_limit(charger_id, target_amps)
                logging.info(f"Amperage set command sent for {target_amps}A.")
                # set_amperage_limit throws an exception if it fails to set the amperage
                #so we can assume at this point that the amperage was set correctly
                confirmed_amperage = target_amps

                if was_charging:
                    current_charging_session = client.start_charging_session(charger_id)
                    if current_charging_session:
                        logging.info(f"Restarted charging session: {current_charging_session.session_id}")
                    else:
                        logging.warning("Failed to restart charging session - got None")
        else:
            logging.info(f"Amperage already set correctly ({current_amperage}A). No change needed.")
            confirmed_amperage = current_amperage

        # Check if we should attempt to start charging
        should_start_charging = (
            get_current_charging_watts(client, charger_id, charger_status, user_charging_status, session_snapshot) == 0
            and charger_status.plugged_in
        )
        
        # Additional checks to prevent starting charging when vehicle is fully charged or not charging
        if should_start_charging and user_charging_status is not None:
            # Check if vehicle is fully charged
            if getattr(user_charging_status, "state", None) == "fully_charged":
                logging.info("Vehicle is fully charged; skipping charging session start")
                should_start_charging = False
        
        if should_start_charging:
            current_charging_session = client.start_charging_session(charger_id)
            if current_charging_session:
                logging.info(f"Started charging session: {current_charging_session.session_id}")
            else:
                logging.warning("Started charging session but received no session object")
        elif target_amps > 0 and not should_start_charging:
            logging.info("Not starting charging session due to vehicle state (fully charged)")

    return confirmed_amperage if target_amps != 0 else 0

# --- MAIN ---

def main():
    global current_charging_session

    args = parse_args()

    influx_client = InfluxDBClient(
        host=args.influxdb_host,
        port=args.influxdb_port,
        username=args.influxdb_user,
        password=args.influxdb_pass,
        database=args.influxdb_db
    )

    setup_logging(args.log_file, args.quiet)

    username = args.username
    password = args.password
    control_interval = args.control_interval  # minutes
    slope_window = args.slope_window  # minutes

    logging.info(f"Using {control_interval}-min control interval and {slope_window}-min slope calculation window")

    logging.info("Connecting to ChargePoint...")
    client = ChargePoint(username, password)
    chargers = client.get_home_chargers()

    if not chargers:
        logging.error("No home chargers found.")
        sys.exit(1)

    charger_id = chargers[0]
    logging.info(f"Found charger {charger_id}")

    # Check for existing charging session and initialize global object if active
    initialize_charging_session_if_active(client, charger_id)

    charger_status = client.get_home_charger_status_v2(charger_id)
    allowed_amps = charger_status.possible_amperage_limits
    min_amperage = min(allowed_amps)
    minimum_watts_required = (min_amperage - 0.5) * 240

    logging.info(f"Minimum amperage is {min_amperage}A, requiring at least {minimum_watts_required}W of solar excess to start charging.")

    last_control_change = 0
    last_set_amperage = None

    while True:
        try:
            solar = get_solar_power_status(influx_client, control_interval, slope_window)
            if not solar:
                logging.warning("No solar data. Skipping...")
                time.sleep(control_interval * 60)
                continue

            production = solar["production_watts"]
            consumption = solar["consumption_watts"]
            solar_slope = solar["solar_slope_watts_per_second"]

            charger_status = client.get_home_charger_status_v2(charger_id)
            # Fast-path guard: if unplugged, clear any cached session to prevent stale watts
            try:
                if hasattr(charger_status, "plugged_in") and (not charger_status.plugged_in) and current_charging_session is not None:
                    logging.info("Charger reports unplugged; clearing cached charging session.")
                    current_charging_session = None
                    # Also ensure we don't carry forward a non-zero last-known value when unplugged
                    global last_known_charging_watts
                    last_known_charging_watts = 0
            except Exception:
                pass
                
            user_charging_status = client.get_user_charging_status()
            session_snapshot = None
            if user_charging_status is not None:
                try:
                    session_snapshot = client.get_charging_session(user_charging_status.session_id)
                except Exception as e:
                    logging.warning(f"Failed to fetch session snapshot: {e}")
            current_charging_watts = get_current_charging_watts(client, charger_id, charger_status, user_charging_status, session_snapshot)
            average_excess = -1 * (consumption - current_charging_watts)
            predicted_excess = average_excess + (solar_slope * control_interval * 60)

            # Log charging status debug info periodically
            if user_charging_status is not None:
                log_charging_status_debug(client, user_charging_status, charger_status, session_snapshot)

            logging.info(f"{control_interval}-min averages - Production: {production:.1f}W, Grid Consumption: {consumption:.1f}W, Current Charging Load: {current_charging_watts:.1f}W, Average Excess: {average_excess:.1f}W, Solar Slope: {solar_slope:.3f}W/s, Predicted Excess: {predicted_excess:.1f}W")

            allowed_amps = charger_status.possible_amperage_limits
            charging_status_val = charger_status.charging_status

            # Only log debug info if allowed_amps is not a list (indicating a problem)
            if not isinstance(allowed_amps, (list, tuple)):
                logging.error(f"allowed_amps is not a list: {type(allowed_amps)} = {allowed_amps}. Skipping charging decision.")
                # Log additional debug info only when there's a problem
                logging.error(f"DEBUG: charger_status type={type(charger_status)}")
                logging.error(f"DEBUG: charger_status attributes={dir(charger_status)}")
                if hasattr(charger_status, 'charging_status'):
                    logging.error(f"DEBUG: charger_status.charging_status={charger_status.charging_status}")
                if hasattr(charger_status, 'plugged_in'):
                    logging.error(f"DEBUG: charger_status.plugged_in={charger_status.plugged_in}")
                time.sleep(60)
                continue

            if production < 500:
                # After-hours heuristic: normally charge fast if plugged in,
                # but respect TOU schedule to avoid peak rates.
                is_off_peak = getattr(charger_status, "is_during_scheduled_time", None)
                if is_off_peak is True:
                    target_amps = max(allowed_amps)
                    logging.info(f"Low production ({production:.1f}W) and OFF-PEAK per charger schedule; setting to max amperage {target_amps}A for fast charging.")
                elif is_off_peak is False:
                    target_amps = 0
                    logging.info(f"Low production ({production:.1f}W) but PEAK per charger schedule; deferring charging until off-peak (target_amps=0).")
                else:
                    # Fallback if charger_status lacks the field: maintain previous behavior
                    target_amps = max(allowed_amps)
                    logging.info(f"Low production ({production:.1f}W) and schedule unknown; defaulting to max amperage {target_amps}A.")
            elif predicted_excess >= minimum_watts_required:
                target_amps = determine_target_amperage(predicted_excess, allowed_amps)
                logging.info(f"Predicted excess solar ({predicted_excess:.1f}W). Setting amperage to {target_amps}A.")
            else:
                target_amps = 0
                logging.info(f"Insufficient predicted excess solar ({predicted_excess:.1f}W) < minimum ({minimum_watts_required:.1f}W). Stopping charging.")
                
            if ((time.time() - last_control_change) >  (control_interval * 60)):
                current_amperage = charger_status.amperage_limit
                if charging_status_val == "CHARGING" and current_amperage == 40 and last_set_amperage != 40:
                    logging.info("User likely set charger to 40A manually. Skipping adjustment.")
                else:
                    session_for_wait_check = session_snapshot if session_snapshot is not None else current_charging_session
                    if session_for_wait_check and getattr(session_for_wait_check, "charging_state", None) == "waiting":
                        logging.info("Car is waiting to start charging. Skipping adjustment.")
                    else:
                        logging.info("=== Executing Adjustment ===")
                        try:
                            new_amperage = apply_charging_decision(client, charger_id, charger_status, target_amps, min_amperage, user_charging_status, session_snapshot)
                            last_set_amperage = new_amperage
                            last_control_change = time.time()
                        except ChargePointCommunicationException as e:
                            logging.error(f"Failed to apply charging decision, clearing global session: {e.message}")
                            current_charging_session = None
                        logging.info("=== === === === === === ===")

            # Remove redundant fetch; use charger_status already obtained above
            current_amperage = last_set_amperage if last_set_amperage is not None else charger_status.amperage_limit
            
            # Update and log the current_charging_watts
            log_control_metrics_to_influx(
                influx_client,
                solar_slope,
                predicted_excess,
                current_charging_watts,
                target_amps,
                current_amperage
            )
            
            time.sleep(60)

        except Exception as e:
            logging.error(f"Error in main loop: {e}")
            time.sleep(control_interval * 60)

if __name__ == "__main__":
    main()
