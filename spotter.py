import socket
import requests
import time
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
# URL to send web alerts if specified
# webapi
#webapi = "http://your-api-endpoint.com/alert"  # Replace with your URL or set to None if unused

# Reference location coordinates
reference_lat = XX.XXXXX  # Replace with your latitude
reference_lon = -Y.XXXXX  # Replace with your longitude

# Initialize and connect to the IRC server
irc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
print("Connecting to IRC server...")
irc.connect((server, port))
print("Connected to server. Sending NICK...")
irc.send(f"NICK {nickname}\r\n".encode('utf-8'))

# Wait a second before sending USER
time.sleep(1)
print("Sending USER command...")
irc.send(f"USER {nickname} 0 * :{realname}\r\n".encode('utf-8'))

# Wait for server PING after sending USER
while True:
    irc_data = irc.recv(2048).decode('utf-8')
    print(f"Received from server: {irc_data}")
    
    if irc_data.startswith("PING"):
        # Respond to the PING
        ping_id = irc_data.split(":", 1)[1].strip()
        irc.send(f"PONG :{ping_id}\r\n".encode('utf-8'))
        print(f"Responded to PING with: {ping_id}")
        break

# Wait a moment to ensure connection stability before joining channel
time.sleep(5)
print(f"Joining channel {channel}...")
irc.send(f"JOIN {channel}\r\n".encode('utf-8'))
print(f"Joined {channel}")

# Track the last alert time for each aircraft by ICAO hex code
last_alert_time = {}

def send_message(message):
    """
    Send a message to the specified IRC channel.
    """
    irc.send(f"PRIVMSG {channel} :{message}\r\n".encode('utf-8'))
    print(f"Sent to IRC: {message}")

def send_web_alert(message):
    """
    Send an alert message as a POST request to the specified webapi URL.
    """
    if webapi:
        try:
            response = requests.post(webapi, data=message)
            print(f"Sent web alert: {message} | Response: {response.status_code}")
        except requests.RequestException as e:
            print(f"Failed to send web alert: {e}")

def fetch_aircraft_data():
    """
    Fetch aircraft data from the ADSBexchange or Tar1090 JSON feed.
    """
    try:
        response = requests.get(adsb_url)
        if response.ok:  # Check if response is successful
            return response.json()
        else:
            print(f"Error: Received response status {response.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Error accessing ADSBexchange/Tar1090: {e}")
        return None
    except ValueError as e:
        print("Error: Received non-JSON response")
        return None

def is_squawk_in_watchlist(squawk):
    """
    Check if a given squawk matches any in the watchlist, supporting both single values and ranges.
    """
    if not squawk:
        return False  # Return False if squawk is None or empty
    
    for item in watchlist_squawks:
        if "-" in item:
            # Range detected, split and check if within range
            start, end = item.split("-")
            if int(start) <= int(squawk) <= int(end):
                return True
        elif item == squawk:
            # Exact match
            return True
    return False
def haversine(lat1, lon1, lat2, lon2):
    """
    Calculate the distance between two latitude and longitude points in miles.
    """
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2)**2 + cos(lat1) * cos(lat2) * sin(dlon / 2)**2
    c = 2 * asin(sqrt(a))
    r = 3956  # Radius of Earth in miles
    return c * r

def calculate_bearing(lat1, lon1, lat2, lon2):
    """
    Calculate the bearing between two latitude/longitude points and convert it to compass direction.
    """
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = sin(dlon) * cos(lat2)
    y = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    initial_bearing = atan2(x, y)
    initial_bearing = degrees(initial_bearing)
    compass_bearing = (initial_bearing + 360) % 360

    # Determine compass direction based on bearing
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", 
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    idx = round(compass_bearing / 22.5) % 16
    return directions[idx], compass_bearing

def ping_listener():
    """
    Dedicated function to listen for and respond to server PINGs and CTCP requests.
    """
    while True:
        try:
            irc_data = irc.recv(2048).decode('utf-8')
            print(f"Received from server: {irc_data}")

            if irc_data.startswith("PING"):
                ping_id = irc_data.split(":", 1)[1].strip()
                irc.send(f"PONG :{ping_id}\r\n".encode('utf-8'))
                print(f"Responded to PING with: {ping_id}")
            
            elif "PRIVMSG" in irc_data and "\x01VERSION\x01" in irc_data:
                sender_nick = irc_data.split('!')[0][1:]  # Extract sender's nickname
                response = f"NOTICE {sender_nick} :\x01VERSION PlaneSpotter/0.3\x01"
                irc.send(f"{response}\r\n".encode('utf-8'))
                print(f"Responded to CTCP VERSION request from {sender_nick} with PlaneSpotter/0.2")
                
        except socket.timeout:
            continue
        except Exception as e:
            print(f"Error in PING listener: {e}")
            break

ping_thread = threading.Thread(target=ping_listener, daemon=True)
ping_thread.start()

while True:
    data = fetch_aircraft_data()

    if data and "aircraft" in data:
        current_time = datetime.now()

        for aircraft in data["aircraft"]:
            squawk = aircraft.get("squawk")
            icao = aircraft.get("hex")
            altitude_raw = aircraft.get("alt_baro", "0")
            altitude = int("".join(filter(str.isdigit, str(altitude_raw))))
            category = aircraft.get("category")
            emergency = aircraft.get("emergency")
            latitude = aircraft.get("lat")
            longitude = aircraft.get("lon")
            
            # Retrieve speeds (ground, indicated airspeed, true airspeed)
            ground_speed = aircraft.get("gs", "N/A")  # Ground speed, in knots
            indicated_air_speed = aircraft.get("ias", "N/A")  # Indicated airspeed, in knots
            true_air_speed = aircraft.get("tas", "N/A")  # True airspeed, in knots
            
            # Initialize additional details
            distance_str = ""
            direction_str = ""
            eta_str = ""
            speed_str = ""
            
            # Compile available speeds
            if ground_speed != "N/A":
                speed_str += f"Ground Speed: {ground_speed} knots"
            if indicated_air_speed != "N/A":
                speed_str += f", IAS: {indicated_air_speed} knots"
            if true_air_speed != "N/A":
                speed_str += f", TAS: {true_air_speed} knots"

            # Calculate distance, direction, and ETA if latitude, longitude, and ground speed are available
            if latitude is not None and longitude is not None:
                distance = haversine(reference_lat, reference_lon, latitude, longitude)
                direction, bearing = calculate_bearing(reference_lat, reference_lon, latitude, longitude)
                distance_str = f" | Distance: {distance:.2f} miles {direction} ({bearing:.1f}Â°)"
                
                # Calculate ETA based on ground speed only
                if ground_speed != "N/A" and ground_speed > 0:
                    eta_seconds = (distance * 3600) / ground_speed
                    eta_str = f" | ETA: {eta_seconds:.0f} seconds"

            # Check if the alert meets any criteria to send a message
            squawk_match = is_squawk_in_watchlist(squawk)
            icao_match = icao in watchlist_aircraft
            altitude_match = (altitude > 0 and altitude < altitude_threshold)
            category_match = category in watchlist_categories
            emergency_match = emergency and emergency.lower() != "none"
            
            meets_criteria = (squawk_match or icao_match or altitude_match or category_match or emergency_match)

            if (meets_criteria or verbose) and (current_time - last_alert_time.get(icao, datetime.min) >= alert_interval):
                last_alert_time[icao] = current_time

                # Format the alert message with color and bold
                flight_code = f"\x02\x0300{aircraft.get('flight', 'Unknown')}\x03\x02"
                altitude_str = f"\x02\x0308{altitude} ft\x03\x02"  # Bold Yellow
                squawk_str = f"\x02\x0303{squawk}\x03\x02"         # Bold Green

                message = (f"Alert! Aircraft {flight_code} ({icao}) with squawk {squawk_str} "
                           f"at altitude {altitude_str}, category {category}, emergency status: {emergency}. "
                           f"Location: {latitude}, {longitude}{distance_str} | {speed_str}{eta_str}")
                
                if icao:
                    message += f" | Track here: https://globe.adsbexchange.com/?icao={icao}"

                # Send alert to IRC
                send_message(message)
                
                # Send alert to webapi if specified
                send_web_alert(message)
                
                time.sleep(bot_message_delay)
    
    # Poll aircraft data every 10 seconds
    time.sleep(10)

