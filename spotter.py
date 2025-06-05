import socket
import requests
import time
import json
from datetime import datetime, timedelta
import threading
from math import radians, degrees, cos, sin, asin, sqrt, atan2

# IRC server and channel configuration
server = "irc.network"
port = 6667
nickname = "ircnick"
realname = "irc real name"
channel = "#channel"
bot_message_delay = 1  # Delay between messages to comply with IRC rate limits
ping_wait_timeout = 10  # Time to wait for PING response before joining channel

# Aircraft watchlist criteria with ranges
watchlist_squawks = ['0001', '0020', '7500', '7700']
watchlist_aircraft = ['a0001', 'a12345']
altitude_threshold = 5000
watchlist_categories = ['A6', 'A7', 'B2', 'B3', 'B4', 'B6', 'B7']
alert_interval = timedelta(minutes=15)  # 15-minute interval for repeated alerts

# Verbose mode for general sighting notifications (set to False)
verbose = False

# URL for local ADSBexchange or Tar1090 JSON data
adsb_url = "http://adsbexchange.local/tar1090/data/aircraft.json"
webapi = None  # Define webapi globally to avoid NameError; set to None or the actual URL

# Reference location coordinates
reference_lat = XX.XXXXX  # Replace with your latitude
reference_lon = -Y.XXXXX  # Replace with your longitude

# File to store last alert times
alert_time_file = "last_alert_time.json"

# Track pending updates for incomplete aircraft data
pending_updates = {}
pending_updates_lock = threading.Lock()

# Track the last alert time for each aircraft by ICAO hex code
def load_last_alert_time():
    try:
        with open(alert_time_file, "r") as file:
            return {k: datetime.fromisoformat(v) for k, v in json.load(file).items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_last_alert_time():
    with open(alert_time_file, "w") as file:
        json.dump({k: v.isoformat() for k, v in last_alert_time.items()}, file)

last_alert_time = load_last_alert_time()

# Initialize and connect to the IRC server
def connect_to_irc():
    irc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    print("Connecting to IRC server...")
    irc.connect((server, port))
    print("Connected to server. Sending NICK...")
    irc.send(f"NICK {nickname}\r\n".encode('utf-8'))
    time.sleep(1)
    print("Sending USER command...")
    irc.send(f"USER {nickname} 0 * :{realname}\r\n".encode('utf-8'))

    start_time = time.time()

    while True:
        if time.time() - start_time > ping_wait_timeout:
            print("PING wait timeout reached, joining channel anyway...")
            break

        irc_data = irc.recv(2048).decode('utf-8')
        print(f"Received from server: {irc_data}")

        if irc_data.startswith("PING"):
            # Respond to the PING and proceed to join the channel
            ping_id = irc_data.split(":", 1)[1].strip()
            irc.send(f"PONG :{ping_id}\r\n".encode('utf-8'))
            print(f"Responded to PING with: {ping_id}")
            break

    # Join the channel regardless of PING response
    time.sleep(2)  # Short delay to ensure connection stability
    print(f"Joining channel {channel}...")
    irc.send(f"JOIN {channel}\r\n".encode('utf-8'))
    print(f"Joined {channel}")

    return irc

def send_message(irc, message):
    irc.send(f"PRIVMSG {channel} :{message}\r\n".encode('utf-8'))
    print(f"Sent to IRC: {message}")

def send_web_alert(message):
    if webapi:
        try:
            response = requests.post(webapi, data=message)
            print(f"Sent web alert: {message} | Response: {response.status_code}")
        except requests.RequestException as e:
            print(f"Failed to send web alert: {e}")

def fetch_aircraft_data():
    try:
        response = requests.get(adsb_url)
        if response.ok:
            return response.json()
        else:
            print(f"Error: Received response status {response.status_code}")
            return None
    except requests.RequestException as e:
        print(f"Error accessing ADSBexchange/Tar1090: {e}")
        return None

def is_squawk_in_watchlist(squawk):
    if not squawk:
        return False

    for item in watchlist_squawks:
        if "-" in item:
            start, end = item.split("-")
            if int(start) <= int(squawk) <= int(end):
                return True
        elif item == squawk:
            return True
    return False

def haversine(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2)**2 + cos(lat1) * cos(lat2) * sin(dlon / 2)**2
    c = 2 * asin(sqrt(a))
    r = 3956
    return c * r

def calculate_bearing(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = sin(dlon) * cos(lat2)
    y = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    initial_bearing = atan2(x, y)
    initial_bearing = degrees(initial_bearing)
    compass_bearing = (initial_bearing + 360) % 360
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", 
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    idx = round(compass_bearing / 22.5) % 16
    return directions[idx], compass_bearing

def ping_listener(irc):
    while True:
        try:
            irc_data = irc.recv(2048).decode('utf-8')
            print(f"Received from server: {irc_data}")
            if irc_data.startswith("PING"):
                ping_id = irc_data.split(":", 1)[1].strip()
                irc.send(f"PONG :{ping_id}\r\n".encode('utf-8'))
        except socket.timeout:
            continue
        except Exception as e:
            print(f"Error in PING listener: {e}")
            break

def check_for_update(irc, icao, original_data):
    """Check for updated aircraft data after 30 seconds"""
    time.sleep(30)

    data = fetch_aircraft_data()
    if data and "aircraft" in data:
        for aircraft in data["aircraft"]:
            if aircraft.get("hex") == icao:
                # Skip aircraft with ICAO starting with tilde
                if icao and icao.startswith("~"):
                    break

                # Check if we now have location or speed data that was missing
                latitude = aircraft.get("lat")
                longitude = aircraft.get("lon")
                ground_speed = aircraft.get("gs", "N/A")

                # Only send update if we have new data that was previously missing
                if ((latitude and longitude and not (original_data.get("lat") and original_data.get("lon"))) or
                    (ground_speed != "N/A" and original_data.get("gs", "N/A") == "N/A")):

                    # Build the update message
                    raw_alt = str(aircraft.get("alt_baro", "0"))
                    digits = "".join(filter(str.isdigit, raw_alt))
                    if not digits and raw_alt.lower() not in ("0", "none"):
                        print(f"[WARN] alt_baro value '{raw_alt}' produced no digits — defaulting to 0")
                    altitude = int(digits) if digits else 0

                    category = aircraft.get("category")
                    emergency = aircraft.get("emergency")
                    squawk = aircraft.get("squawk")
                    indicated_air_speed = aircraft.get("ias", "N/A")
                    true_air_speed = aircraft.get("tas", "N/A")
                    distance_str, eta_str, speed_str = "", "", ""

                    if ground_speed != "N/A":
                        speed_str += f"Ground Speed: {ground_speed} knots"
                    if indicated_air_speed != "N/A":
                        speed_str += f", IAS: {indicated_air_speed} knots"
                    if true_air_speed != "N/A":
                        speed_str += f", TAS: {true_air_speed} knots"
                    if latitude and longitude:
                        distance = haversine(reference_lat, reference_lon, latitude, longitude)
                        direction, bearing = calculate_bearing(reference_lat, reference_lon, latitude, longitude)
                        distance_str = f" | Distance: {distance:.2f} miles {direction} ({bearing:.1f}°)"
                        if ground_speed != "N/A" and ground_speed > 0:
                            eta_seconds = (distance * 3600) / ground_speed
                            eta_str = f" | ETA: {eta_seconds:.0f} seconds"

                    flight_code = f"\x02\x0300{aircraft.get('flight', 'Unknown')}\x03\x02"
                    altitude_str = f"\x02\x0308{altitude} ft\x03\x02"
                    squawk_str = f"\x02\x0303{squawk}\x03\x02"
                    message = (f"UPDATE! Aircraft {flight_code} ({icao}) with squawk {squawk_str} "
                               f"at altitude {altitude_str}, category {category}, emergency status: {emergency}. "
                               f"Location: {latitude}, {longitude}{distance_str} | {speed_str}{eta_str}")
                    if icao:
                        message += f" | Track here: https://globe.adsbexchange.com/?icao={icao}"
                    send_message(irc, message)
                    send_web_alert(message)
                break

    # Remove from pending updates
    with pending_updates_lock:
        if icao in pending_updates:
            del pending_updates[icao]

# Connect to IRC and start PING listener
irc = connect_to_irc()
ping_thread = threading.Thread(target=ping_listener, args=(irc,), daemon=True)
ping_thread.start()

# Main loop to fetch aircraft data and send alerts
while True:
    data = fetch_aircraft_data()
    if data and "aircraft" in data:
        current_time = datetime.now()
        for aircraft in data["aircraft"]:
            icao = aircraft.get("hex")

            # Skip aircraft with ICAO starting with tilde
            if icao and icao.startswith("~"):
                continue

            squawk = aircraft.get("squawk")
            altitude = int("".join(filter(str.isdigit, str(aircraft.get("alt_baro", "0")))))
            category = aircraft.get("category")
            emergency = aircraft.get("emergency")
            latitude = aircraft.get("lat")
            longitude = aircraft.get("lon")
            ground_speed = aircraft.get("gs", "N/A")
            indicated_air_speed = aircraft.get("ias", "N/A")
            true_air_speed = aircraft.get("tas", "N/A")

            # Check if aircraft has meaningful data
            has_valid_data = (
                squawk and 
                altitude > 0 and 
                category and 
                latitude is not None and 
                longitude is not None
            )

            # Skip aircraft with no meaningful data, but don't update last_alert_time
            if not has_valid_data:
                continue

            distance_str, eta_str, speed_str = "", "", ""

            if ground_speed != "N/A":
                speed_str += f"Ground Speed: {ground_speed} knots"
            if indicated_air_speed != "N/A":
                speed_str += f", IAS: {indicated_air_speed} knots"
            if true_air_speed != "N/A":
                speed_str += f", TAS: {true_air_speed} knots"
            if latitude and longitude:
                distance = haversine(reference_lat, reference_lon, latitude, longitude)
                direction, bearing = calculate_bearing(reference_lat, reference_lon, latitude, longitude)
                distance_str = f" | Distance: {distance:.2f} miles {direction} ({bearing:.1f}°)"
                if ground_speed != "N/A" and ground_speed > 0:
                    eta_seconds = (distance * 3600) / ground_speed
                    eta_str = f" | ETA: {eta_seconds:.0f} seconds"

            meets_criteria = (is_squawk_in_watchlist(squawk) or icao in watchlist_aircraft 
                              or (altitude > 0 and altitude < altitude_threshold) 
                              or category in watchlist_categories 
                              or (emergency and emergency.lower() != "none"))

            if (meets_criteria or verbose) and (current_time - last_alert_time.get(icao, datetime.min) >= alert_interval):
                last_alert_time[icao] = current_time
                flight_code = f"\x02\x0300{aircraft.get('flight', 'Unknown')}\x03\x02"
                altitude_str = f"\x02\x0308{altitude} ft\x03\x02"
                squawk_str = f"\x02\x0303{squawk}\x03\x02"
                message = (f"Alert! Aircraft {flight_code} ({icao}) with squawk {squawk_str} "
                           f"at altitude {altitude_str}, category {category}, emergency status: {emergency}. "
                           f"Location: {latitude}, {longitude}{distance_str} | {speed_str}{eta_str}")
                if icao:
                    message += f" | Track here: https://globe.adsbexchange.com/?icao={icao}"
                send_message(irc, message)
                send_web_alert(message)
                time.sleep(bot_message_delay)

                # Check if we need to schedule an update check
                needs_update = (not latitude or not longitude or ground_speed == "N/A")
                if needs_update and icao not in pending_updates:
                    with pending_updates_lock:
                        pending_updates[icao] = True
                    # Start a thread to check for updates in 30 seconds
                    update_thread = threading.Thread(
                        target=check_for_update, 
                        args=(irc, icao, aircraft.copy()),
                        daemon=True
                    )
                    update_thread.start()

    save_last_alert_time()
    time.sleep(10)
