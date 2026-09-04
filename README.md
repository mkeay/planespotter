# ✈️ Planespotter

**Planespotter** is a Python-based real-time aircraft tracking and alerting system designed to run on a Raspberry Pi. It connects to ADS-B data sources like [ADSBexchange](https://www.adsbexchange.com/) or [Tar1090](https://github.com/wiedehopf/tar1090) to monitor local air traffic. The system sends alerts to an IRC channel and/or a specified web API endpoint when aircraft meet defined watchlist criteria.

---

## 🔧 Features

- **Real-Time Monitoring**: Reads the local readsb/tar1090 `aircraft.json` directly from disk — no HTTP round-trip.
- **Customizable Alerts**: Triggers on squawk codes (incl. ranges), ICAO hex watchlist, emitter category, low altitude, or emergency status.
- **Squawk Decoding**: Every alert says what the code is *for*, or who controls it — `0020 (air ambulance / HEMS medivac)`, `0036 (helicopter pipeline/powerline inspection)`, `0043 (Metropolitan Police ASU)`, `5271 (transit code, Channel Islands)`, `0421 (Farnborough Radar / RAF Leeming / Ireland Domestic and others)` — covering the whole UK SSR code assignment plan, all 4096 codes. Codes are reused between units too far apart to clash, so where one has several owners the most specific are listed first.
- **Online Horizon Extension**: Optionally polls a free aggregator API (adsb.lol / adsb.fi / airplanes.live) and alerts on watchlist aircraft nearby that your receiver can't see (e.g. terrain-masked low-fliers), tagged `(online data)`.
- **IRC Notifications**: Structured, colour-coded alerts with distance, bearing, climb/descent rate, manoeuvring indicator, and an ETA shown only when the aircraft is actually inbound.
- **Military & MLAT Detection**: Flags military airframes via the aircraft database (`dbFlags`, needs readsb `--db-file`) and alerts on MLAT-only targets — aircraft transmitting no ADS-B position, located by the MLAT network.
- **Photo Links**: Appends a planespotters.net photo link for the alerted airframe.
- **Daily Digest**: Posts a once-a-day receiver stats summary (aircraft tracked, positions, messages, max range, signal levels) to IRC.
- **Astronomy Almanac**: A nightly report an hour before sunset — sunset/sunrise times, moon phase with full-moon countdown, darkness times and a best-observing-window from [Clear Outside](https://clearoutside.com) (primary source, Open-Meteo as fallback), astronomical seeing and transparency from [7Timer!](https://www.7timer.info), a 0–100 "ideal astronomy weather" score (100 = perfect clear skies), overnight frost and snow warnings (only when expected), and the day's saints' days plus UK national and international observances (Easter-linked movable feasts computed too).
- **Tonight's Sky**: The nightly report adds the naked-eye planets that actually get up (how high, which way, and when they're best), and — only when seeing *and* transparency are average or better — three deep-sky targets well placed at the darkest hour, chosen with the moon's brightness in mind so a gibbous moon doesn't send you after a faint galaxy. Positions come from `ephemeris.py`: orbital elements and perturbation terms computed locally, no network and no ephemeris files.
- **Eclipse Alerts**: A lunar eclipse is announced on the nights there is one, with the type, how deep, when it's greatest and the window it's actually above the horizon *here*; a solar eclipse appears in the morning briefing with the percentage of the sun covered from your coordinates (worked topocentrically — it's a different eclipse a hundred miles away). Nothing is said on the overwhelming majority of nights when there's no eclipse.
- **Aurora Alerts**: Watches [AuroraWatch UK](https://aurorawatch.lancs.ac.uk) (Lancaster University magnetometers — a nowcast) and [NOAA SWPC](https://www.swpc.noaa.gov)'s Kp forecast through the dark hours, alerting when the aurora becomes a real prospect at *your* latitude (the Kp threshold is derived from it). Once per escalation, not once per poll — and silent when the sky is clouded over, because there's no sense alerting on an aurora nobody could see.
- **Radiosonde Alerts**: Polls [SondeHub](https://sondehub.org) for weather balloons near you — one alert when sighted, another when descent begins (the chase-worthy moment), with a tracking link. No sonde receiver required; a local [radiosonde_auto_rx](https://github.com/projecthorus/radiosonde_auto_rx) station simply adds your data to the same network.
- **Announce Spool**: A drop-directory bridge to IRC for the rest of your SDR stack — `norris-say "NOAA-19 pass captured"` from any script, cron job, or decoder hook (dumpvdl2, auto_rx, raspberry-noaa-v2) relays the message to the channel, queued safely while IRC reconnects.
- **Robust**: Automatic IRC reconnect with backoff, proper line-buffered PING handling, and no unhandled crashes on ground traffic.
- **API Integration**: Optionally posts alert data to a specified webhook.
- **Follow-up Updates**: If an alert fires before position/speed data arrives, a single `UPDATE` message follows once it does.
- **Zero dependencies**: Python 3 standard library only.

---

## 🛠️ Installation

1. **Clone the Repository**
   ```bash
   git clone https://github.com/mkeay/planespotter.git
   cd planespotter
   ```

2. **Configure Settings**
   - Copy `config.txt.example` to `config.txt`
   - Edit the configuration to match your IRC server, location, and watchlist preferences

3. **Run the Spotter**
   ```bash
   python3 spotter.py --config /path/to/config.txt
   ```

4. **Optional: run as a systemd service**
   ```bash
   sudo cp planespotter.service /etc/systemd/system/
   # edit paths in the unit file if yours differ, then:
   sudo systemctl daemon-reload
   sudo systemctl enable --now planespotter
   journalctl -u planespotter -f
   ```

---

## ⚙️ Configuration (`config.txt`)

Planespotter uses a single `[DEFAULT]` section in its `config.txt` for all settings — see [`config.txt.example`](config.txt.example) for a complete annotated example.

### Key settings

| Setting | Meaning |
|---|---|
| `server`, `port`, `nickname`, `channel` | IRC connection details |
| `reference_lat`, `reference_lon` | Your location, for distance/bearing/ETA |
| `local_json` | Path to readsb/tar1090 `aircraft.json` (default `/run/readsb/aircraft.json`) |
| `altitude_threshold` | Alert on anything below this altitude (feet) |
| `watchlist_squawks` | Squawk codes, single or ranges (`7500,7600-7700`) |
| `watchlist_categories` | ADS-B emitter categories (`A6,A7,...`) |
| `watchlist_aircraft` | ICAO hex codes to always alert on |
| `alert_interval_minutes` | Minimum time between repeat alerts per aircraft |
| `online_enabled`, `online_radius_nm` | Enable the aggregator check and its radius |
| `astro_enabled`, `astro_lead_minutes` | Nightly sky report, and how long before sunset it posts |
| `aurora_enabled`, `aurora_min_status` | Aurora watch, and the lowest AuroraWatch level worth an alert (`yellow`/`amber`/`red`) |
| `webapi` | Optional webhook URL to POST alerts to |

---

## 📡 Data Sources

- **Computed locally**: sun, moon, planets, eclipse circumstances and deep-sky visibility (`astro.py`, `ephemeris.py`) — no ephemeris files, no API, works offline.
- **Local**: `aircraft.json` written by [readsb](https://github.com/wiedehopf/readsb)/[tar1090](https://github.com/wiedehopf/tar1090), read directly from `/run` (tmpfs).
- **Online** (optional): a readsb-style `/v2/point/{lat}/{lon}/{radius}` API — [adsb.lol](https://api.adsb.lol/docs), [adsb.fi](https://github.com/adsbfi/opendata), and [airplanes.live](https://airplanes.live/api-guide/) all work; ADS-B Exchange itself has no free API tier.

---

## 🔄 Alert Flow

- Each aircraft is checked against the configured criteria every `poll_seconds`
- Repeat alerts per aircraft are suppressed for `alert_interval_minutes`
- Alerts missing position/speed get one follow-up `UPDATE` message when the data arrives
- Watchlist aircraft within `online_radius_nm` on the aggregator but absent from the local feed are alerted with an `(online data)` tag — useful when terrain masks low-flying aircraft from your antenna

---

## 🤝 Contributing

Pull requests, suggestions, and improvements are welcome!

---

## 📄 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---

*Happy spotting from your Pi!*
