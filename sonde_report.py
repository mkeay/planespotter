#!/usr/bin/env python3
"""Weather balloon flight report: the "next time we see a weather balloon"
runbook. Takes a completed radiosonde_auto_rx capture log, summarises the
flight (burst altitude, ascent/descent rates, temperature range, last known
position), builds a timeline graph, altitude-profile plots and a
temperature-coloured KML ground track, fetches the last 5 days of weather
plus the next 24h outlook for context, and posts the result to IRC via the
announce_dir bridge (norris-say) -- the same route dumpvdl2/raspberry-noaa
hooks use, so it needs nothing from spotter.py's own process.

This is deliberately independent of spotter.py's SondeHub-radius-limited
sonde alerting (sonde_radius_km in config.txt): that setting bounds how far
out spotter.py asks the SondeHub *aggregator* for other stations' sondes,
which is a different question from "did our own receiver decode this",
which is always interesting regardless of range. --watch mode scans
auto_rx's own log directory directly, so it triggers on anything this
station actually caught, no matter how far out.

Usage:
    sonde_report.py --serial Y1422577        # one-shot, by sonde serial
    sonde_report.py --log-file <path>         # one-shot, by log path
    sonde_report.py --watch                   # scan for newly-stale, unreported flights (run from cron)
"""
import argparse
import csv
import json
import logging
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.cm as cm
import matplotlib.colors as mcolors

sys.path.insert(0, str(Path(__file__).parent))
import astro
import spotter

AUTO_RX_LOG_DIR = Path("/home/pi/radiosonde_auto_rx/auto_rx/log")
PLOT_DIR = Path("/var/www/html/sonde-plots")
PLOT_BASE_URL = "http://192.168.255.19/sonde-plots"
NORRIS_SAY = Path(__file__).parent / "norris-say"
CONFIG_PATH = "/home/pi/config.txt"
STALE_MINUTES = 45  # no new telemetry for this long => flight over (landed or out of range)

# vr.z.je (MOFFAT community push interface, see https://vr.z.je/cue.php).
# Same key as the ADS-B feed -- one key per station, for everything it hears
# -- but that copy is root-only (mode 600, read by the DynamicUser ADS-B
# service), and this script runs as the pi user via cron, so it gets its own
# copy here rather than either widening the root-owned file's permissions or
# embedding the key in this script (which, unlike the ADS-B feeder, lives
# somewhere world-readable).
VRJE_KEY_FILE = Path("/home/pi/.vrje-key")
VRJE_INGEST_URL = "https://vr.z.je/pi_ingest.php"

# CSV field names understood by pi_ingest.php's telemetry rows, unchanged
# from the auto_rx log header -- send sentinels (-273.0 temp, -1.0
# humidity/pressure) exactly as auto_rx writes them; the server converts
# them to NULL itself.
_TELEMETRY_FLOAT_FIELDS = ("lat", "lon", "alt", "vel_v", "vel_h", "heading",
                           "temp", "humidity", "pressure", "freq_mhz", "snr", "batt_v")
_TELEMETRY_INT_FIELDS = ("frame", "f_error_hz", "sats")

# Fixed range so flights are visually comparable to each other, not just
# self-normalised -- covers everything from a warm surface launch to a cold
# stratospheric burst.
TEMP_COLOR_MIN, TEMP_COLOR_MAX = -70.0, 30.0

log = logging.getLogger("sonde_report")


def read_raw_rows(path):
    """Raw CSV rows as dicts, field names/values exactly as auto_rx wrote
    them -- what vr.z.je's pi_ingest.php telemetry rows want, as opposed to
    parse_log()'s reduced/typed set below for our own plotting."""
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _coerce_row_for_vrje(row):
    out = dict(row)
    for field in _TELEMETRY_FLOAT_FIELDS:
        if out.get(field) not in (None, ""):
            try:
                out[field] = float(out[field])
            except ValueError:
                pass
    for field in _TELEMETRY_INT_FIELDS:
        if out.get(field) not in (None, ""):
            try:
                out[field] = int(float(out[field]))
            except ValueError:
                pass
    return out


def _vrje_key():
    try:
        return VRJE_KEY_FILE.read_text().strip() or None
    except OSError:
        return None


def _vrje_post(payload, key):
    req = urllib.request.Request(
        VRJE_INGEST_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"X-Feed-Key": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except (ValueError, OSError):
            log.warning("vr.z.je push failed: HTTP %s", e.code)
            return None
    except (OSError, ValueError) as e:
        log.warning("vr.z.je push failed: %s", e)
        return None


def push_telemetry_to_vrje(raw_rows, key, chunk_size=20000):
    """POST raw per-second telemetry rows, chunked to the documented 20000
    row limit. Returns the per-serial {"serial": url} dict merged across
    chunks, or {} if nothing succeeded."""
    urls = {}
    for i in range(0, len(raw_rows), chunk_size):
        chunk = [_coerce_row_for_vrje(r) for r in raw_rows[i:i + chunk_size]]
        resp = _vrje_post({"kind": "telemetry", "rows": chunk}, key)
        if not resp or not resp.get("ok"):
            log.warning("vr.z.je telemetry push failed: %s", resp)
            continue
        log.info("vr.z.je: wrote %d/%d telemetry rows", resp.get("written", 0), len(chunk))
        urls.update(resp.get("urls") or {})
    return urls


def _json_safe(value):
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def push_summary_to_vrje(serial, summary, key):
    resp = _vrje_post({"kind": "summary", "serial": serial, "summary": _json_safe(summary)}, key)
    if not resp or not resp.get("ok"):
        log.warning("vr.z.je summary push failed: %s", resp)
        return None
    return resp.get("url")


def parse_log(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "time": datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")),
                    "lat": float(r["lat"]), "lon": float(r["lon"]), "alt": float(r["alt"]),
                    "vel_v": float(r["vel_v"]), "vel_h": float(r["vel_h"]),
                    "temp": float(r["temp"]), "humidity": float(r["humidity"]),
                    "pressure": float(r["pressure"]),
                })
            except (KeyError, ValueError):
                continue
    rows.sort(key=lambda r: r["time"])
    return rows


def summarize(rows, cfg):
    first, last = rows[0], rows[-1]
    burst = max(rows, key=lambda r: r["alt"])
    ascent_secs = max((burst["time"] - first["time"]).total_seconds(), 1)
    descent_secs = (last["time"] - burst["time"]).total_seconds()
    temps = [r["temp"] for r in rows if r["temp"] > -270]
    humidities = [r["humidity"] for r in rows if r["humidity"] >= 0]
    dist_nm = spotter.haversine_nm(cfg.reference_lat, cfg.reference_lon, last["lat"], last["lon"])
    brg = spotter.bearing_deg(cfg.reference_lat, cfg.reference_lon, last["lat"], last["lon"])
    # Crude tropopause estimate: the altitude above which temperature stops
    # falling and starts climbing again on the ascent leg (stratospheric
    # inversion) -- the lowest point of the ascent-leg temperature curve.
    ascent_rows = [r for r in rows if r["time"] <= burst["time"] and r["temp"] > -270]
    tropopause = min(ascent_rows, key=lambda r: r["temp"]) if ascent_rows else None
    return {
        "first": first, "last": last, "burst": burst, "tropopause": tropopause,
        "ascent_rate": (burst["alt"] - first["alt"]) / ascent_secs,
        "descent_rate": (last["alt"] - burst["alt"]) / descent_secs if descent_secs > 60 else None,
        "min_temp": min(temps) if temps else None,
        "max_temp": max(temps) if temps else None,
        "max_humidity": max(humidities) if humidities else None,
        "dist_mi": dist_nm * spotter.NM_TO_MILES,
        "direction": spotter.COMPASS[round(brg / 22.5) % 16],
        "brg": brg,
        "still_descending": last["vel_v"] < -1 and last["alt"] > 500,
        "duration_min": (last["time"] - first["time"]).total_seconds() / 60,
    }


def make_timeline_plot(rows, serial, out_path):
    """Multiple time series sharing one time axis - altitude, vertical
    speed, temperature, humidity and horizontal drift speed - so how each
    quantity evolved through the flight can be read off together."""
    t0 = rows[0]["time"]
    minutes = [(r["time"] - t0).total_seconds() / 60 for r in rows]
    alts_km = [r["alt"] / 1000 for r in rows]
    vel_v = [r["vel_v"] for r in rows]
    temps = [r["temp"] if r["temp"] > -270 else None for r in rows]
    hums = [r["humidity"] if r["humidity"] >= 0 else None for r in rows]
    wind_kt = [r["vel_h"] * 1.94384 for r in rows]

    fig, axes = plt.subplots(5, 1, figsize=(12, 12), sharex=True)

    axes[0].plot(minutes, alts_km, color="#1f77b4")
    axes[0].set_ylabel("Altitude\n(km)")
    axes[0].grid(alpha=0.3)
    axes[0].set_title(f"{serial} - flight timeline")

    axes[1].plot(minutes, vel_v, color="#9467bd")
    axes[1].axhline(0, color="gray", linewidth=0.7)
    axes[1].set_ylabel("Vertical\nspeed (m/s)")
    axes[1].grid(alpha=0.3)

    def _valid(vals):
        return [(m, v) for m, v in zip(minutes, vals) if v is not None]

    vt = _valid(temps)
    if vt:
        axes[2].plot(*zip(*vt), color="#d62728")
    axes[2].set_ylabel("Temp\n(deg C)")
    axes[2].grid(alpha=0.3)

    vh = _valid(hums)
    if vh:
        axes[3].plot(*zip(*vh), color="#17becf")
    axes[3].set_ylabel("Humidity\n(%)")
    axes[3].grid(alpha=0.3)

    axes[4].plot(minutes, wind_kt, color="#2ca02c")
    axes[4].set_ylabel("Drift speed\n(kt)")
    axes[4].set_xlabel("Minutes since first fix")
    axes[4].grid(alpha=0.3)

    fig.tight_layout()
    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def make_profile_plot(rows, serial, out_path):
    """Temperature, humidity and drift-speed (wind proxy) vs altitude."""
    alts_km = [r["alt"] / 1000 for r in rows]
    temps = [r["temp"] if r["temp"] > -270 else None for r in rows]
    hums = [r["humidity"] if r["humidity"] >= 0 else None for r in rows]
    wind_kt = [r["vel_h"] * 1.94384 for r in rows]

    fig, axes = plt.subplots(1, 3, figsize=(15, 5))

    valid = [(t, a) for t, a in zip(temps, alts_km) if t is not None]
    if valid:
        axes[0].plot(*zip(*valid), color="#d62728")
    axes[0].set_xlabel("Temperature (deg C)")
    axes[0].set_ylabel("Altitude (km)")
    axes[0].set_title(f"{serial} - temperature vs altitude")
    axes[0].grid(alpha=0.3)

    validh = [(h, a) for h, a in zip(hums, alts_km) if h is not None]
    if validh:
        axes[1].plot(*zip(*validh), color="#17becf")
    axes[1].set_xlabel("Relative humidity (%)")
    axes[1].set_ylabel("Altitude (km)")
    axes[1].set_title("Humidity vs altitude")
    axes[1].grid(alpha=0.3)

    axes[2].plot(wind_kt, alts_km, color="#2ca02c")
    axes[2].set_xlabel("Horizontal speed (kt)")
    axes[2].set_ylabel("Altitude (km)")
    axes[2].set_title("Drift speed vs altitude\n(proxy for wind aloft)")
    axes[2].grid(alpha=0.3)

    fig.tight_layout()
    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def _kml_color(temp_c):
    """KML colour string (aabbggrr hex) for a temperature, blue=cold/red=warm
    over a fixed -70..30 deg C range so flights are comparable to each other."""
    norm = mcolors.Normalize(vmin=TEMP_COLOR_MIN, vmax=TEMP_COLOR_MAX)
    r, g, b, _ = cm.get_cmap("RdBu_r")(norm(temp_c))
    return "ff%02x%02x%02x" % (int(b * 255), int(g * 255), int(r * 255))


def make_kml(rows, serial, out_path, max_points=300):
    """Geographically-accurate 3D ground track, altitude included, coloured
    per-segment by temperature so it renders directly in Google Earth /
    any KML-capable GIS viewer."""
    step = max(1, len(rows) // max_points)
    pts = rows[::step]
    if pts[-1] is not rows[-1]:
        pts.append(rows[-1])

    placemarks = []
    for a, b in zip(pts, pts[1:]):
        color = _kml_color((a["temp"] + b["temp"]) / 2 if a["temp"] > -270 and b["temp"] > -270 else -70.0)
        placemarks.append(f"""
    <Placemark>
      <Style><LineStyle><color>{color}</color><width>4</width></LineStyle></Style>
      <LineString>
        <altitudeMode>absolute</altitudeMode>
        <coordinates>{a['lon']:.6f},{a['lat']:.6f},{a['alt']:.1f} {b['lon']:.6f},{b['lat']:.6f},{b['alt']:.1f}</coordinates>
      </LineString>
    </Placemark>""")

    burst = max(rows, key=lambda r: r["alt"])
    marker = f"""
    <Placemark>
      <name>Burst ({burst['alt']:.0f} m)</name>
      <Point>
        <altitudeMode>absolute</altitudeMode>
        <coordinates>{burst['lon']:.6f},{burst['lat']:.6f},{burst['alt']:.1f}</coordinates>
      </Point>
    </Placemark>"""

    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{serial} flight track</name>
    <description>Colour = temperature ({TEMP_COLOR_MIN:.0f} to {TEMP_COLOR_MAX:.0f} deg C, blue=cold/red=warm)</description>
    {marker}
    {''.join(placemarks)}
  </Document>
</kml>"""
    out_path.write_text(kml)


def fetch_weather_trend(lat, lon):
    """Last 5 days of actuals plus the next 24h outlook, via Open-Meteo's
    past_days/forecast_days params on the same endpoint astro.py already
    uses. Returns None if unreachable."""
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max"
           "&hourly=temperature_2m,precipitation,wind_speed_10m,relative_humidity_2m"
           "&wind_speed_unit=mph&timezone=Europe%2FLondon&past_days=5&forecast_days=2")
    try:
        d = astro._get_json(url)
        daily = d["daily"]
        past5 = {
            "tmax": daily["temperature_2m_max"][:5],
            "tmin": daily["temperature_2m_min"][:5],
            "precip_total": round(sum(daily["precipitation_sum"][:5]), 1),
            "wind_max": max(daily["wind_speed_10m_max"][:5]),
        }
        now = datetime.now(timezone.utc)
        hourly_times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in d["hourly"]["time"]]
        idx = [i for i, t in enumerate(hourly_times) if t >= now][:24]
        next24 = {
            "tmax": max(d["hourly"]["temperature_2m"][i] for i in idx),
            "tmin": min(d["hourly"]["temperature_2m"][i] for i in idx),
            "precip_total": round(sum(d["hourly"]["precipitation"][i] for i in idx), 1),
            "wind_max": max(d["hourly"]["wind_speed_10m"][i] for i in idx),
            "humidity_max": max(d["hourly"]["relative_humidity_2m"][i] for i in idx),
        }
        return {"past5": past5, "next24": next24}
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        log.warning("Weather trend fetch failed: %s", e)
        return None


def build_commentary(summary, trend):
    """Grounded, hedged remarks tying the flight's own measurements to the
    5-day trend and next-24h outlook -- observations, not a forecast."""
    if not trend:
        return []
    lines = []
    p5, n24 = trend["past5"], trend["next24"]
    warming = p5["tmax"][-1] > p5["tmax"][0]
    lines.append(
        f"Last 5 days: {min(p5['tmin']):.0f} to {max(p5['tmax']):.0f} deg C "
        f"({'warming' if warming else 'cooling'} trend), {p5['precip_total']:.1f}mm total rain, "
        f"winds to {p5['wind_max']:.0f} mph"
    )
    lines.append(
        f"Next 24h: {n24['tmin']:.0f} to {n24['tmax']:.0f} deg C, "
        f"{n24['precip_total']:.1f}mm expected, winds to {n24['wind_max']:.0f} mph, "
        f"humidity up to {n24['humidity_max']:.0f}%"
    )

    remarks = []
    if summary["max_humidity"] is not None and summary["max_humidity"] > 80 and n24["precip_total"] > 0.5:
        remarks.append("high humidity in the sonde's lower profile lines up with rain in the next 24h")
    if n24["wind_max"] > p5["wind_max"] * 1.3:
        remarks.append("surface wind picking up vs the last 5 days - consistent with the wind shear the sonde saw aloft")
    if not warming and n24["tmax"] < p5["tmax"][-1]:
        remarks.append("cooling trend continuing into tomorrow")
    if remarks:
        lines.append("Assessment: " + "; ".join(remarks) + ".")
    else:
        lines.append("Assessment: no strong signal tying the profile to a shift in the next 24h - looks like a continuation of recent conditions.")
    return lines


def build_irc_lines(serial, summary, cfg, trend, vrje_url=None):
    s = summary
    lines = [
        f"Weather balloon report: \x02\x0300{serial}\x03\x02 | "
        f"burst at \x02\x0308{s['burst']['alt']/1000:.1f} km\x03\x02, "
        f"ascent {s['ascent_rate']:.1f} m/s"
        + (f", descent {abs(s['descent_rate']):.1f} m/s" if s['descent_rate'] else "")
        + f" | flight so far: {s['duration_min']:.0f} min",
    ]
    if s["min_temp"] is not None:
        lines.append(f"Temperature range seen: {s['min_temp']:.1f} to {s['max_temp']:.1f} deg C")
    if s["tropopause"] is not None:
        lines.append(f"Tropopause (coldest point on ascent): {s['tropopause']['temp']:.1f} deg C at {s['tropopause']['alt']/1000:.1f} km")
    pos_note = "still descending" if s["still_descending"] else "last known position"
    lines.append(
        f"{pos_note.capitalize()}: {s['dist_mi']:.0f} miles {s['direction']} of station "
        f"({s['brg']:.0f} deg) at {s['last']['alt']/1000:.1f} km"
    )

    lines.extend(build_commentary(s, trend))

    lines.append(f"Track: https://sondehub.org/{serial}")
    if vrje_url:
        lines.append(f"Live map: {vrje_url}")
    return lines


def send_to_irc(lines):
    text = "\n".join(lines) + "\n"
    subprocess.run([str(NORRIS_SAY)], input=text, text=True, check=True)


def process_log(log_path, post=True):
    serial = log_path.stem.split("_")[1] if "_" in log_path.stem else log_path.stem
    rows = parse_log(log_path)
    if len(rows) < 2:
        log.warning("Not enough telemetry rows in %s to report", log_path)
        return
    cfg = spotter.Config(CONFIG_PATH)
    summary = summarize(rows, cfg)
    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    make_timeline_plot(rows, serial, PLOT_DIR / f"{serial}_timeline.png")
    make_profile_plot(rows, serial, PLOT_DIR / f"{serial}_profile.png")
    make_kml(rows, serial, PLOT_DIR / f"{serial}.kml")
    trend = fetch_weather_trend(cfg.reference_lat, cfg.reference_lon)

    vrje_url = None
    key = _vrje_key()
    if key:
        raw_rows = read_raw_rows(log_path)
        urls = push_telemetry_to_vrje(raw_rows, key)
        vrje_url = urls.get(serial)
        summary_url = push_summary_to_vrje(serial, summary, key)
        vrje_url = vrje_url or summary_url
    else:
        log.warning("No vr.z.je key at %s, skipping push", VRJE_KEY_FILE)

    lines = build_irc_lines(serial, summary, cfg, trend, vrje_url)
    for line in lines:
        print(line)
    if post:
        send_to_irc(lines)
    return lines


def watch_once():
    """Scan auto_rx's own log directory for flights that have gone quiet and
    haven't been reported yet -- the automatic trigger for future catches,
    independent of spotter.py's SondeHub-radius sonde alerting."""
    import time
    now = time.time()
    for log_path in AUTO_RX_LOG_DIR.glob("*_sonde.log"):
        marker = log_path.with_suffix(".reported")
        if marker.exists():
            continue
        age_min = (now - log_path.stat().st_mtime) / 60
        if age_min < STALE_MINUTES:
            continue
        log.info("Flight in %s looks complete (idle %.0f min) - generating report", log_path.name, age_min)
        try:
            process_log(log_path)
        except Exception as e:
            log.error("Failed to process %s: %s", log_path, e)
            continue
        marker.touch()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--serial", help="sonde serial, e.g. Y1422577 (looks up the log in auto_rx's log dir)")
    group.add_argument("--log-file", type=Path, help="path to a specific *_sonde.log file")
    group.add_argument("--watch", action="store_true", help="scan for newly-stale, unreported flights")
    parser.add_argument("--no-post", action="store_true", help="print the report without sending to IRC")
    args = parser.parse_args()

    if args.watch:
        watch_once()
        return

    if args.serial:
        matches = sorted(AUTO_RX_LOG_DIR.glob(f"*_{args.serial}_*_sonde.log"))
        if not matches:
            raise SystemExit(f"No log file found for serial {args.serial} in {AUTO_RX_LOG_DIR}")
        log_path = matches[-1]
    else:
        log_path = args.log_file

    process_log(log_path, post=not args.no_post)


if __name__ == "__main__":
    main()
