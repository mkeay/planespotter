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
watchlist_aircraft = [
    # Elon Musk
    'a835af',  # Gulfstream G650ER (N628TS) - Elon Musk's primary jet
    'a2c671',  # Gulfstream G550 (N272BG) - Elon Musk's previous jet
    'a7c747',  # Gulfstream G650 (N628TS) - Elon Musk (historical)

    # U.S. Presidential Fleet
    'ae4ae8',  # Boeing VC-25A (92-9000) - Air Force One
    'ae4ae9',  # Boeing VC-25A (92-8000) - Air Force One
    'ae01ce',  # Boeing C-32A (99-0003) - Air Force Two
    'ae01cf',  # Boeing C-32A (99-0004) - Air Force Two
    'ae4aed',  # C-32B (02-4452)
    'ae4aee',  # C-32B (00-9001)
    'ae0402',  # E-4B Nightwatch (73-1676)
    'ae0410',  # E-6B Mercury (164385)
    'ae0411',  # E-6B Mercury (164386)
    'ae0412',  # E-6B Mercury (164387)
    'ae11f9',  # Boeing E-4B (75-0125) - Doomsday plane
    'ae11fa',  # Boeing E-4B (74-0787) - Doomsday plane

    # Trump Organization
    'a72b76',  # Boeing 757 (N757AF) - Trump's personal 757
    'a9b8f1',  # Cessna Citation X (N725DT) - Trump's Citation
    'aa3c1f',  # Sikorsky S-76B (N76DT) - Trump's helicopter
    'a69f59',  # Gulfstream G650ER (N272BG) - Trump backup

    # British Royal Family
    '43c6f9',  # Airbus A330 Voyager (ZZ336) - RAF VIP transport
    '43c4ec',  # BAe 146 (ZE700) - Royal Flight
    '43c4ed',  # BAe 146 (ZE701) - Royal Flight

    # Other Billionaires/Celebrities
    'a4ff61',  # Boeing 737 (N887WM) - Warren Buffett's NetJets
    'a9e51e',  # Gulfstream G650 (N721DG) - Mark Cuban
    'a3f3f6',  # Gulfstream G650ER (N71GE) - Jeff Bezos (rumored)
    'a5c37c',  # Gulfstream G650 (N464TF) - Taylor Swift (formerly)
    'a54ac6',  # Global Express (N1F) - Oprah Winfrey
    'a326ca',  # Gulfstream G650 (N2N) - Laurene Powell Jobs
    'ac5c30',  # Boeing 767 (N894JB) - Jerry Bruckheimer
    'a0fc23',  # Gulfstream G650 (N1F) - Nike/Phil Knight
    'a98682',  # Bombardier Global Express (N660KK) - Kirk Kerkorian estate
    'aa5ed6',  # Gulfstream G650ER (N825MG) - Michael Jordan
    'a4f8e0',  # Gulfstream G550 (N887WT) - Oprah Winfrey
    'a6a378',  # Bombardier Global 6000 (N624AG) - Bill Gates
    'a0a5c1',  # Gulfstream G650ER (N194WM) - Walmart heirs
    'a2894f',  # Gulfstream G550 (N271DV) - Google executives
    'a66aa8',  # Boeing 767 (N606TD) - Tyler Perry
    'ac7d60',  # Gulfstream G650 (N899JH) - Jay-Z/Beyoncé
    'a835b0',  # Gulfstream G550 (N628VM) - Jim Carrey
    'a4b8a2',  # Bombardier Global Express (N884TA) - Tom Cruise

    # Russian Government/Oligarchs
    '4691c7',  # Airbus A340 (VP-BMS) - Russian government VIP
    '14f11f',  # Il-96-300PU (RA-96022)
    '14f100',  # Il-96-300PU (RA-96021)
    '424070',  # Ilyushin Il-96 (RA-96016) - Putin's primary aircraft
    '424071',  # Ilyushin Il-96 (RA-96017) - Russian government
    '43eb2e',  # Airbus A319 (M-KATE) - Alisher Usmanov
    '4ca83b',  # Boeing 787 (P4-BDL) - Roman Abramovich

    # Other World Leaders
    '3c6644',  # Airbus A340 (16+01) - German Air Force One
    '3c6645',  # Airbus A340 (16+02) - German government
    '3b7ac8',  # Airbus A330 (F-RARF) - French Air Force One
    '33ffd9',  # Airbus A330 (MM62293) - Italian government
    '471f49',  # Boeing 737 (LN-KKR) - Norwegian government
    '440417',  # Airbus A319 (OE-LUX) - Austrian government
    '4b1a02',  # Airbus A340 (TC-TRK) - Turkish government
    '71bc08',  # Boeing 747 (HL7643) - South Korean Air Force One
    '7c4774',  # Boeing 737 (A36-001) - Australian government
    'e48f76',  # Boeing 777 (FAB2900) - Brazilian Air Force One

    # Special/Unique Aircraft
    '424242',  # An-225 Mriya (UR-82060) - Destroyed
    'a0db06',  # Boeing 747 (N748JB) - Virgin Orbit Cosmic Girl
    'aa3410',  # Boeing 737 (N859WP) - Amazon Prime Air
    'a4c7e4',  # Boeing 737 (N737AT) - JANET (Area 51)
    'a4c7e5',  # Boeing 737 (N738AT) - JANET
    'a6f3a7',  # Boeing 737 (N628TS) - Janet Airlines (Area 51)
    'a6f3a8',  # Boeing 737 (N365SR) - Janet Airlines
    'a8df5a',  # Boeing 737 (N869HH) - SpaceX charter

    # NASA Aircraft
    'ac82ec',  # Boeing 747SP (N747NA) - NASA SOFIA
    'acd5d4',  # Gulfstream G-III (N992NA) - NASA research
    'abd8d7',  # WB-57 (N927NA)
    'acd6cc',  # DC-8 (N817NA)
    'a547c3',  # Lockheed U-2S (NASA)

    # Antonov An-124s (Antonov Airlines)
    '508000',  # UR-82007
    '508101',  # UR-82008
    '508102',  # UR-82009
    '508103',  # UR-82029
    '508104',  # UR-82073
    '508105',  # UR-82027
    '508035',  # Antonov An-124 (UR-82072) - Antonov Airlines
    '508036',  # Antonov An-124 (UR-82073) - Antonov Airlines
    '50801c',  # Antonov An-124 (UR-82007) - Antonov Airlines
    '508037',  # Antonov An-124 (UR-82008) - Antonov Airlines
    '50803a',  # Antonov An-124 (UR-82009) - Antonov Airlines

    # Antonov An-124s (Volga-Dnepr)
    '15407d',  # RA-82045
    '15406c',  # RA-82044
    '15406b',  # RA-82043

    # Antonov An-22
    '154093',  # RA-09341

   # Ilyushin Il-76
    '5081a2',  # UR-76744
    '5081a3',  # UR-78786
    '145960',  # RA-78830
    '145961',  # RA-78831

    # C-5M Super Galaxy (USAF)
    'ae07aa',  # 86-0013
    'ae07ac',  # 86-0025
    'ae07ae',  # 87-0036

    # C-17 Globemaster III
    'ae11f1',  # 01-0187
    'ae11f2',  # 01-0188
    '43c26f',  # ZZ176 (RAF)
    '43c270',  # ZZ177 (RAF)

    # Middle Eastern Royal/Government
    '710258',  # Boeing 747 (HZ-WBT7) - Saudi Royal Flight
    '710259',  # Boeing 747 (HZ-HM1B) - Saudi government
    '896048',  # Boeing 747 (A6-HRM) - Dubai Royal
    '896049',  # Boeing 747 (A6-PFA) - UAE government
    '06a1e3',  # Boeing 747 (A9C-HAK) - Bahrain Royal Flight
    '06a1e4',  # Boeing 747 (A9C-HMK) - Bahrain government

    # Corporate Jets
    'a494a9',  # Gulfstream G650 (N650GD) - Google executives
    'a77bd6',  # Boeing 737 (N737ER) - Oracle (Larry Ellison)
    'a5c657',  # Gulfstream G650 (N502SX) - Starbucks
    'a154c5',  # Boeing 737 (N227WA) - Walmart corporate
    'a6dead',  # Gulfstream G650 (N624DG) - Dell corporate
]

altitude_threshold = 3000
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
