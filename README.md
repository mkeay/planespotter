# ✈️ Planespotter

**Planespotter** is a Python-based real-time aircraft tracking and alerting system designed to run on a Raspberry Pi. It connects to ADS-B data sources like [ADSBexchange](https://www.adsbexchange.com/) or [Tar1090](https://github.com/wiedehopf/tar1090) to monitor local air traffic. The system sends alerts to an IRC channel and/or a specified web API endpoint when aircraft meet defined watchlist criteria.

---

## 🔧 Features

- **Real-Time Monitoring**: Continuously tracks aircraft using local ADS-B feeds.
- **Customizable Alerts**: Define triggers based on altitude, speed, squawk codes, ICAO hex, callsigns, and more.
- **IRC Notifications**: Sends structured alerts to your configured IRC server/channel.
- **API Integration**: Optionally posts alert data to a specified webhook.
- **Redundancy Handling**: Suppresses incomplete alerts and updates when more complete data is available.

---

## 🛠️ Installation

1. **Clone the Repository**
   ```bash
   git clone https://github.com/mkeay/planespotter.git
   cd planespotter
   ```

2. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Settings**
   - Rename `config.txt.example` to `config.txt`
   - Edit the configuration to match your IRC server, watchlist preferences, and webhook (if needed)

4. **Run the Spotter**
   ```bash
   python spotter.py
   ```

---

## ⚙️ Configuration (`config.txt`)

Planespotter uses a single `[default]` section in its `config.txt` for all settings.

### Example:
```ini
[default]
# IRC settings
irc_host = irc.example.net
irc_port = 6697
irc_channel = #planes
irc_nick = spotterbot
irc_tls = true

# JSON API settings
api_url = http://localhost:8080/data.json

# Watchlist criteria
altitude_below = 10000
speed_above = 300
squawks = 7500,7600,7700
callsigns = RRR,RRR123
icaos = 43C5AB,43C123

# Location for distance/direction calculation
lat = 55.9533
lon = -3.1883

# Optional: POST alert JSON to this URL
alert_webhook = https://your.webhook.url/receive
```

### Supported Watchlist Fields

- `altitude_below`: Trigger if altitude is below this value (in feet)
- `speed_above`: Trigger if speed exceeds this value (in knots)
- `squawks`: Comma-separated list of emergency/transponder squawk codes
- `callsigns`: Partial or full flight callsigns (case-insensitive)
- `icaos`: Specific aircraft hex codes

---

## 📡 Data Source

This script pulls data from a JSON feed such as:
- [`/data.json`](https://github.com/wiedehopf/tar1090#datajson) from a local Tar1090 or ADS-B Exchange instance

Make sure your Pi is feeding data and your receiver has this JSON endpoint enabled.

---

## 🔄 Alert Flow

- Each aircraft is checked against the configured criteria
- Alerts are sent once per aircraft (per event)
- Incomplete alerts (e.g. missing speed/location) are suppressed until data is complete
- Duplicate or repeat alerts are avoided to reduce IRC noise

---

## 🤝 Contributing

Pull requests, suggestions, and improvements are welcome!

---

## 📄 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---

*Happy spotting from your Pi!*
