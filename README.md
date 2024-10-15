Planespotter - Real-Time Aircraft Tracking and Alert System
Project Description

Planespotter is a Python-based tool designed for real-time aircraft tracking, notification, and alerting. Running on a Raspberry Pi, this project connects to an ADS-B data source (e.g., ADSBexchange or Tar1090) and monitors local air traffic. The system sends alerts to an IRC channel with information on aircraft that meet specified watchlist criteria, including altitude, squawk codes, and speed. Each alert message includes flight details, distance, directional bearing, estimated time of arrival (ETA), and speed metrics. The project can also send alerts to a specified web API endpoint, making it suitable for aviation enthusiasts, planespotters, and those interested in monitoring airspace activity.
Features

    Real-Time Aircraft Monitoring: Connects to ADS-B data to track aircraft in real-time.
    Customizable Watchlist Criteria: Set criteria based on squawk codes, altitude, aircraft categories, and emergency status.
    IRC Alerts with Enhanced Formatting: Sends formatted alerts to an IRC channel with color codes for easy readability.
    Distance, Direction, and ETA Calculations: Displays distance, directional bearing, and ETA based on ground speed.
    Speed Information: Shows ground speed, indicated airspeed, and true airspeed if available.
    Web API Integration: Optionally send alerts to a web API endpoint via HTTP POST.
    Extensibility: Easy to modify for additional notification channels or criteria adjustments.

Getting Started
Prerequisites

    A Raspberry Pi or compatible Linux system
    Python 3.x
    Internet connection
    Access to an ADS-B data source (e.g., ADSBexchange or Tar1090)
    An IRC account and access to an IRC server
    Optionally, a web API endpoint for additional alert notifications

Installation

    Clone the Repository

    bash

git clone https://github.com/mkeay/planespotter.git
cd planespotter

Install Dependencies Install the necessary Python packages:

bash

pip install requests

Configure the Script Open the Python script and configure the following sections:

    IRC Server Details: Set your IRC server, channel, and bot nickname.
    Reference Location: Enter your latitude and longitude for accurate distance and direction calculations.
    ADSBexchange/Tar1090 URL: Set the URL for your ADS-B data source.
    Web API Endpoint: Specify the URL for receiving alert messages as HTTP POST requests (optional).

Run the Script

bash

    python planespotter.py

The bot will start connecting to the ADS-B data source, monitoring aircraft, and sending alerts to your IRC channel and, if configured, to your web API endpoint.
Configuration Options
Watchlist Criteria

Edit the following variables in the script to adjust the aircraft monitoring criteria:

    Squawk Codes: watchlist_squawks — List specific codes or ranges to watch for.
    Aircraft ICAO Codes: watchlist_aircraft — Add specific ICAO codes to the list.
    Altitude Threshold: altitude_threshold — Set the maximum altitude for monitoring.
    Aircraft Categories: watchlist_categories — Specify aircraft categories to monitor (e.g., commercial, private, etc.).

Alert Customization

    Verbose Mode: Set verbose = True to receive all alerts, regardless of criteria matching.
    Alert Interval: Adjust alert_interval to set how often alerts are repeated for the same aircraft.
    Notification Settings: The script currently sends alerts to an IRC channel and optionally to a web API endpoint but can be extended to other platforms (e.g., email, SMS, WhatsApp).

Example Alert

An example alert message in the IRC channel:

yaml

Alert! Aircraft EZY428M (407d22) with squawk 6351 at altitude 30625 ft, category A3, emergency status: none. 
Location: x, -y | Distance: 10.2 miles NNE (22.5°) | Ground Speed: 450 knots, IAS: 430 knots, TAS: 470 knots | ETA: 82 seconds | Track here: https://globe.adsbexchange.com/?icao=407d22

Formatting

    Bold & Color Codes: Flight code, altitude, and squawk are displayed in bold with white, yellow, and green, respectively.
    Distance & Direction: Calculated based on the reference location and displayed in miles.
    ETA: Calculated based on the aircraft’s ground speed.

Contributing

Contributions are welcome! Please fork the repository and submit a pull request with any enhancements or bug fixes. For major changes, open an issue first to discuss your ideas.
License

This project is licensed under the MIT License. See the LICENSE file for details.
Acknowledgments

Special thanks to ADSBexchange and Tar1090 for providing open-source ADS-B data that powers this project.
