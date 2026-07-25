#!/usr/bin/env python3
"""Planespotter alert review: parses a day's "Sent to IRC" aircraft alerts
from the systemd journal, groups them into per-aircraft encounters, and
flags the patterns worth a human looking at:

  - noisy encounters (several messages for one pass -- possible duplicate
    alerting rather than genuinely new information)
  - distant contacts that were alerted on but never confirmed by the local
    receiver (peripheral traffic that never came close)
  - short-notice low-level "pop-ups": first spotted locally, low, with no
    prior online/long-range notice at all

For flagged pop-ups it also tries a ground-truth check: fetches the
aircraft's public trace from the adsb.lol globe-history archive and
replays it through the rule spotter.py now applies -- low enough to see or
hear, on a course that actually passes close, not descending into a nearby
airport -- to see when it would first have qualified, or why it never did.
Best-effort -- this depends on a third-party archive that may not have every
aircraft, and network hiccups are treated as "unknown", not an error.

Stdlib only. Run standalone: ./review.py [--date YYYY-MM-DD] [--config PATH]
"""

import argparse
import gzip
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta
from math import asin, atan2, cos, degrees, radians, sin
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spotter import (Config, haversine_nm, bearing_deg, closest_approach,
                     bound_for_airport, COMPASS, NM_TO_MILES, USER_AGENT)

LOCAL_TZ = ZoneInfo("Europe/London")
M_TO_FT = 3.28084
AGL_RELEVANT_FT = 5000  # above this, terrain is noise next to the altitude -- don't bother

IRC_FORMAT_RE = re.compile(r"\x03(\d{1,2}(,\d{1,2})?)?|[\x02\x0f\x1d\x1f\x16]")
LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO Sent to IRC: (?P<msg>.*)$"
)
ALERT_RE = re.compile(
    r"^(?P<kind>[A-Za-z][\w ]*)!(?: \((?P<tag>[^)]*)\))? "
    r"Aircraft \S+ \((?P<hex>[0-9a-fA-F]{6})\)"
)
DIST_RE = re.compile(r"Distance: ([\d.]+) miles (\w+) \((\d+)\xb0\)")
ETA_RE = re.compile(r"ETA: (\d+) seconds")
ALT_RE = re.compile(r"at altitude (\d+) ft")

ENCOUNTER_GAP = timedelta(minutes=25)
POPUP_ALT_FT = 3000
NOISY_THRESHOLD = 3


def strip_irc(text):
    return IRC_FORMAT_RE.sub("", text)


def fetch_journal(since, until):
    out = subprocess.run(
        ["journalctl", "-u", "planespotter", "--since", since, "--until", until,
         "--no-pager", "-o", "cat"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.splitlines()


def parse_events(lines):
    events = []
    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            continue
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ)
        msg = strip_irc(m.group("msg"))
        am = ALERT_RE.match(msg)
        if not am:
            continue  # train cancellations / other announce-spool lines
        ev = {
            "time": ts, "kind": am.group("kind"), "tag": am.group("tag"),
            "hex": am.group("hex").lower(), "raw": msg,
            "military": "MILITARY" in msg,
        }
        dm = DIST_RE.search(msg)
        if dm:
            ev["dist_mi"], ev["brg"] = float(dm.group(1)), float(dm.group(3))
        em = ETA_RE.search(msg)
        if em:
            ev["eta_s"] = int(em.group(1))
        altm = ALT_RE.search(msg)
        if altm:
            ev["alt_ft"] = int(altm.group(1))
        elif "on the ground" in msg:
            ev["alt_ft"] = 0
        events.append(ev)
    return events


NON_TYPE_PREFIXES = ("Distance:", "ETA:", "Category:", "\x02\x0304MILITARY", "MILITARY",
                     "Ground Speed:", "IAS:", "TAS:", "Track here:", "Photo:", "EMERGENCY:",
                     "Manoeuvring", "MLAT position")


def extract_type_reg(raw):
    """Pulls the "{type} {registration}" segment out of a formatted alert,
    e.g. "AIRBUS A-400M ZM421" -- it's whichever "|"-separated segment isn't
    one of the other known fields."""
    parts = [p.strip() for p in raw.split("|")]
    for p in parts[1:]:
        if p and not p.startswith("(") and not any(p.startswith(pre) for pre in NON_TYPE_PREFIXES):
            return p
    return None


def source_of(ev):
    if ev["tag"] == "online data":
        return "online"
    kind = ev["kind"].strip().lower()
    if kind == "heads up":
        return "long-range"   # retired 2026-07-23; still present in old journals
    if kind == "lost contact":
        return "vanished"
    return "local"


def group_encounters(events):
    by_hex = {}
    for ev in sorted(events, key=lambda e: e["time"]):
        by_hex.setdefault(ev["hex"], []).append(ev)

    encounters = []
    for hexcode, evs in by_hex.items():
        current = [evs[0]]
        for ev in evs[1:]:
            if ev["time"] - current[-1]["time"] <= ENCOUNTER_GAP:
                current.append(ev)
            else:
                encounters.append((hexcode, current))
                current = [ev]
        encounters.append((hexcode, current))
    encounters.sort(key=lambda e: e[1][0]["time"])
    return encounters


def summarize(hexcode, evs):
    first, last = evs[0], evs[-1]
    sources = [source_of(e) for e in evs]
    breakdown = {}
    for e, src in zip(evs, sources):
        key = f'{e["kind"]}({src})'
        breakdown[key] = breakdown.get(key, 0) + 1

    first_local = next((e["time"] for e in evs if source_of(e) == "local"), None)
    first_remote = next((e["time"] for e in evs if source_of(e) in ("online", "long-range")), None)
    lead_minutes = None
    if first_remote and first_local and first_remote < first_local:
        lead_minutes = (first_local - first_remote).total_seconds() / 60

    popup = (sources[0] == "local" and first.get("alt_ft") is not None
             and first["alt_ft"] < POPUP_ALT_FT and first["military"])

    alts = [e["alt_ft"] for e in evs if "alt_ft" in e]
    dists = [e["dist_mi"] for e in evs if "dist_mi" in e]
    type_reg = next((extract_type_reg(e["raw"]) for e in evs if extract_type_reg(e["raw"])), None)

    return {
        "hex": hexcode, "events": evs, "breakdown": breakdown,
        "first_source": sources[0], "lead_minutes": lead_minutes,
        "popup": popup, "noisy": len(evs) >= NOISY_THRESHOLD,
        "materialized_locally": first_local is not None,
        "start": first["time"], "end": last["time"], "military": first["military"],
        "min_alt_ft": min(alts) if alts else None,
        "min_dist_mi": min(dists) if dists else None,
        "type_reg": type_reg,
    }


def trace_link(hexcode, day):
    return f"https://globe.adsb.lol/?icao={hexcode}&showTrace={day.isoformat()}"


def destination_point(lat, lon, bearing, dist_nm):
    """Where you end up starting at (lat, lon), heading `bearing`, for
    `dist_nm` nautical miles -- used to reconstruct an aircraft's
    approximate position from the distance/bearing pair we log, since we
    don't record its raw lat/lon anywhere."""
    R = 3440.065
    lat1, lon1, brg = radians(lat), radians(lon), radians(bearing)
    d = dist_nm / R
    lat2 = asin(sin(lat1) * cos(d) + cos(lat1) * sin(d) * cos(brg))
    lon2 = lon1 + atan2(sin(brg) * sin(d) * cos(lat1), cos(d) - sin(lat1) * sin(lat2))
    return degrees(lat2), degrees(lon2)


def lowest_geolocatable_event(evs):
    """The lowest-altitude event that also has a distance/bearing fix, so
    we can estimate the terrain underneath it. Not necessarily the same
    event as the encounter's bare min_alt_ft (that one might have no
    position yet), but the best cross-referenced number available."""
    candidates = [e for e in evs if "alt_ft" in e and "dist_mi" in e and "brg" in e]
    return min(candidates, key=lambda e: e["alt_ft"]) if candidates else None


def fetch_elevations_m(coords):
    """Batched Open-Meteo elevation lookup (one request for every position
    the report needs, not one per aircraft). Returns None per-entry on
    failure rather than raising -- terrain data is a nice-to-have, not
    worth losing the whole report over."""
    if not coords:
        return []
    lats = ",".join(f"{lat:.5f}" for lat, _ in coords)
    lons = ",".join(f"{lon:.5f}" for _, lon in coords)
    url = f"https://api.open-meteo.com/v1/elevation?latitude={lats}&longitude={lons}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        elevations = data.get("elevation")
        if elevations and len(elevations) == len(coords):
            return elevations
    except (OSError, ValueError):
        pass
    return [None] * len(coords)


def merge_by_hex(encounters):
    """One aircraft can show up as several separate encounters in a day
    (e.g. an outbound pass and a return leg hours apart) -- the report is
    per-aircraft-per-day, not per-encounter, since the trace link already
    covers the whole day regardless. Groups are kept (as pass_times) so a
    repeat visitor is still called out as such."""
    by_hex = {}
    for hexcode, evs in encounters:
        by_hex.setdefault(hexcode, []).append(evs)
    merged = []
    for hexcode, groups in by_hex.items():
        all_evs = [e for g in groups for e in g]
        merged.append((hexcode, groups, all_evs))
    merged.sort(key=lambda m: m[2][0]["time"])
    return merged


def format_report_line(s, day, pass_times, agl_ft=None):
    label = s["type_reg"] or s["hex"]
    bits = [f'{"MILITARY " if s["military"] else ""}{label} ({s["hex"]})']
    if len(pass_times) > 1:
        bits.append(f'seen {len(pass_times)}x: {", ".join(pass_times)} ({s["first_source"]} first)')
    else:
        bits.append(f'first seen {s["start"].strftime("%H:%M")} ({s["first_source"]})')
    if s["min_alt_ft"] is not None:
        if agl_ft is not None:
            bits.append(f'lowest {s["min_alt_ft"]:.0f} ft baro (~{agl_ft:.0f} ft above the '
                        f'terrain there)')
        else:
            bits.append(f'lowest {s["min_alt_ft"]:.0f} ft (baro/ASL, not terrain-corrected)')
    if s["min_dist_mi"] is not None:
        bits.append(f'closest {s["min_dist_mi"]:.1f} mi')
    bits.append(f"full track: {trace_link(s['hex'], day)}")
    return "\U0001f6e9 " + " | ".join(bits)


def build_report(encounters, day, cfg):
    if not encounters:
        return [f"Evening report ({day.isoformat()}): quiet one, nothing worth a second look."]
    merged = merge_by_hex(encounters)
    summaries = [(hexcode, groups, summarize(hexcode, all_evs)) for hexcode, groups, all_evs in merged]

    # One batched terrain lookup for every low-flying aircraft in the report,
    # rather than a request per aircraft.
    low_points = {}
    for hexcode, groups, s in summaries:
        if s["min_alt_ft"] is not None and s["min_alt_ft"] < AGL_RELEVANT_FT:
            ev = lowest_geolocatable_event([e for g in groups for e in g])
            if ev is not None:
                low_points[hexcode] = destination_point(
                    cfg.reference_lat, cfg.reference_lon, ev["brg"], ev["dist_mi"] / NM_TO_MILES)

    hexes = list(low_points)
    elevations_m = fetch_elevations_m([low_points[h] for h in hexes])
    terrain_ft = {h: (e * M_TO_FT if e is not None else None) for h, e in zip(hexes, elevations_m)}

    lines = [f"Evening report ({day.isoformat()}): {len(merged)} aircraft worth a look "
             f"({len(encounters)} pass(es) total) —"]
    for hexcode, groups, s in summaries:
        pass_times = [g[0]["time"].strftime("%H:%M") for g in groups]
        agl_ft = None
        if terrain_ft.get(hexcode) is not None:
            agl_ft = s["min_alt_ft"] - terrain_ft[hexcode]
        lines.append(format_report_line(s, day, pass_times, agl_ft))
    return lines


def fetch_trace(hexcode):
    shard = hexcode[-2:]
    url = f"https://globe.adsb.lol/data/traces/{shard}/trace_full_{hexcode}.json"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    return json.loads(raw)


def check_ground_truth(hexcode, alert_time, cfg, window_hours=4):
    """Best-effort replay of the public trace through the rule spotter.py now
    applies: low enough to see or hear, on a course whose closest approach
    actually passes near us, and not simply descending into a nearby airport.

    Reports when that first became true -- or, when it never did, the closest
    the aircraft came to qualifying, which is the useful bit when asking why
    something was (or wasn't) worth alerting on."""
    try:
        trace = fetch_trace(hexcode)
    except Exception as e:
        return f"    ground truth: lookup failed ({e})"

    base_ts = trace["timestamp"]
    alert_epoch = alert_time.timestamp()
    pts = []
    for pt in trace["trace"]:
        ts = base_ts + pt[0]
        if not (alert_epoch - window_hours * 3600 <= ts <= alert_epoch + 120):
            continue
        lat, lon = pt[1], pt[2]
        if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
            continue
        # trace point: [dt, lat, lon, alt, gs, track, ...]
        alt = pt[3] if len(pt) > 3 and isinstance(pt[3], (int, float)) else None
        track = pt[5] if len(pt) > 5 and isinstance(pt[5], (int, float)) else None
        rate = pt[7] if len(pt) > 7 and isinstance(pt[7], (int, float)) else None
        pts.append((ts, lat, lon, alt, track, rate))

    if not pts:
        return "    ground truth: no position data found in the archive for this window"

    earliest_hit, best_miss, min_alt, gap_start, prev_ts = None, None, None, None, None
    for ts, lat, lon, alt, track, rate in pts:
        if prev_ts is not None and ts - prev_ts > 600:
            gap_start = prev_ts
        prev_ts = ts

        if alt is not None:
            min_alt = alt if min_alt is None else min(min_alt, alt)
        dist_nm = haversine_nm(cfg.reference_lat, cfg.reference_lon, lat, lon)
        brg = bearing_deg(cfg.reference_lat, cfg.reference_lon, lat, lon)
        miss, along = closest_approach(cfg.reference_lat, cfg.reference_lon,
                                       lat, lon, track, dist_nm)
        if miss is not None and along > 0 and (best_miss is None or miss < best_miss):
            best_miss = miss
        if earliest_hit is not None or alt is None or track is None:
            continue
        if alt > cfg.alert_ceiling_ft:
            continue
        if not (along > 0 and miss <= cfg.inbound_cpa_nm):
            continue
        ac = {"lat": lat, "lon": lon, "alt_baro": alt, "track": track, "baro_rate": rate}
        if bound_for_airport(ac, cfg, track):
            continue
        earliest_hit = (ts, dist_nm, brg, miss, alt)

    if earliest_hit is None:
        bits = []
        if min_alt is not None:
            bits.append(f"never got below {min_alt:.0f} ft (ceiling {cfg.alert_ceiling_ft})"
                        if min_alt > cfg.alert_ceiling_ft else f"got down to {min_alt:.0f} ft")
        if best_miss is not None:
            bits.append(f"closest projected approach {best_miss * NM_TO_MILES:.0f} mi "
                        f"(needs {cfg.inbound_cpa_nm * NM_TO_MILES:.0f})")
        return ("    ground truth: would not alert under the current rule"
                + (" -- " + "; ".join(bits) if bits else ""))

    hit_ts, hit_dist_nm, hit_brg, miss, alt = earliest_hit
    lead_minutes = (alert_epoch - hit_ts) / 60
    direction = COMPASS[round(hit_brg / 22.5) % 16]
    hit_local = datetime.fromtimestamp(hit_ts, LOCAL_TZ).strftime("%H:%M:%S")
    lines = [f"    ground truth: would alert from {hit_local} "
             f"({hit_dist_nm * NM_TO_MILES:.0f} mi {direction} at {alt:.0f} ft, "
             f"projected to pass {miss * NM_TO_MILES:.0f} mi away)",
             f"    -> roughly {lead_minutes:.0f} minute(s) of notice relative to the logged alert"]
    if gap_start:
        gap_local = datetime.fromtimestamp(gap_start, LOCAL_TZ).strftime("%H:%M:%S")
        lines.append(f"    (coverage gap after {gap_local} -- no feeder anywhere had a fix "
                      f"through closest approach)")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--config", default="/home/pi/config.txt")
    parser.add_argument("--no-deep", action="store_true",
                         help="skip ground-truth trace lookups for flagged pop-ups")
    parser.add_argument("--report", action="store_true",
                         help="print a short IRC-ready evening digest instead of the full "
                              "analyst view (one line per sighting, with a full-track link) "
                              "-- pipe into norris-say")
    args = parser.parse_args()

    cfg = Config(args.config)

    if args.date:
        day = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        day = datetime.now(LOCAL_TZ).date()
    since = day.isoformat()
    until = (day + timedelta(days=1)).isoformat()

    lines = fetch_journal(since, until)
    events = parse_events(lines)

    if args.report:
        encounters = group_encounters(events) if events else []
        for line in build_report(encounters, day, cfg):
            print(line)
        return

    if not events:
        print(f"No aircraft alerts found for {since}.")
        return
    encounters = group_encounters(events)

    print(f"=== Planespotter alert review: {since} ===")
    print(f"{len(events)} aircraft message(s) across {len(encounters)} encounter(s)\n")

    noisy, missed_notice, distant_misses = [], [], []

    for hexcode, evs in encounters:
        s = summarize(hexcode, evs)
        span = f"{s['start'].strftime('%H:%M:%S')}-{s['end'].strftime('%H:%M:%S')}"
        breakdown_str = ", ".join(f"{k} x{v}" for k, v in s["breakdown"].items())
        flags = []
        if s["noisy"]:
            flags.append("NOISY")
            noisy.append(s)
        if s["popup"]:
            flags.append("SHORT-NOTICE POP-UP")
            missed_notice.append(s)
        if not s["materialized_locally"] and s["first_source"] in ("online", "long-range"):
            flags.append("never confirmed locally")
            distant_misses.append(s)
        flag_str = f"  [{', '.join(flags)}]" if flags else ""

        print(f"[{span}] {hexcode}{flag_str}")
        print(f"  {len(evs)} message(s): {breakdown_str}")
        if s["lead_minutes"] is not None:
            print(f"  first noticed {s['first_source']}, confirmed locally "
                  f"{s['lead_minutes']:.1f} min later")
        if s["popup"] and not args.no_deep:
            print(check_ground_truth(hexcode, s["start"], cfg))
        print()

    if noisy:
        print(f"--- Noisy encounters ({len(noisy)}) ---")
        for s in noisy:
            print(f"  {s['hex']}: {len(s['events'])} messages "
                  f"({s['start'].strftime('%H:%M')}-{s['end'].strftime('%H:%M')})")
        print()

    if distant_misses:
        print(f"--- Distant contacts never confirmed locally ({len(distant_misses)}) ---")
        for s in distant_misses:
            first = s["events"][0]
            eta = f", ETA {first['eta_s']/60:.0f} min" if "eta_s" in first else ""
            dist = f"{first['dist_mi']:.1f} mi" if "dist_mi" in first else "?"
            print(f"  {s['hex']} @ {s['start'].strftime('%H:%M')}: {dist}{eta}")
        print()

    if not (noisy or missed_notice or distant_misses):
        print("No noise or notice-quality issues flagged today.")


if __name__ == "__main__":
    main()
