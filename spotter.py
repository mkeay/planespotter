#!/usr/bin/env python3
"""Planespotter: watches a local readsb/tar1090 feed for interesting aircraft
and alerts an IRC channel. Optionally also polls an online aggregator so
aircraft below the local radio horizon (terrain-masked) are reported too,
tagged "(online data)". Stdlib only."""

import argparse
import configparser
import fnmatch
import json
import logging
import re
import socket
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from math import radians, degrees, sin, cos, asin, sqrt, atan2
from pathlib import Path

import alertlog
import terrain
import astro
import squawks

log = logging.getLogger("spotter")

NM_TO_MILES = 1.15078
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
__version__ = "1.9"
USER_AGENT = f"planespotter/{__version__} (https://github.com/mkeay/planespotter)"
# Hijack / radio failure / general emergency: always worth saying, however high.
EMERGENCY_SQUAWKS = {"7500", "7600", "7700"}
# Conspicuity codes common enough elsewhere that an unrestricted watchlist
# match fires on traffic nowhere near us -- only worth reporting within a
# limited radius. See squawk_within_distance_limit().
SQUAWK_DISTANCE_LIMITS_MI = {"0020": 6, "0036": 3}
# All the line an IRC server will relay once it has added our prefix to
# it. Alerts are trimmed to fit rather than truncated mid-URL.
IRC_LINE_LIMIT = 430
# ":nick!user@host JOIN :#channel"
JOIN_RE = re.compile(r":([^!\s]+)![^\s]*\s+JOIN\s+:?(\S+)", re.I)
GREET_COOLDOWN = 60  # seconds, so a join/part flood can't turn us into a bot war


# === Configuration ===

def _split(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_greetings(value):
    """Nicks to greet as they join: "nick:message" pairs separated by ";".
    The nick is prefixed to the message when it goes out. A nick containing
    * or ? is a wildcard pattern ("*tig*" catches tig, tig|, and bigtigger);
    exact entries win over patterns."""
    greetings = {}
    for part in value.split(";"):
        nick, _, message = part.strip().partition(":")
        nick, message = nick.strip(), message.strip()
        if nick and message:
            greetings[nick.lower()] = message
    return greetings


def _parse_zones(value):
    """Exclusion zones: "lat,lon,radius_nm,below_alt_ft[,name]" separated by
    semicolons, e.g. an airport's approach/circuit airspace. The optional
    name lets alerts say where an arrival is probably headed."""
    zones = []
    for part in value.split(";"):
        part = part.strip()
        if not part:
            continue
        try:
            lat, lon, radius, below, *rest = part.split(",")
            name = rest[0].strip() if rest else None
            zones.append((float(lat), float(lon), float(radius), int(below),
                          name or None))
        except ValueError:
            raise SystemExit(f"Bad exclude_zones entry: {part!r}")
    return zones


class Config:
    def __init__(self, path):
        cp = configparser.ConfigParser(interpolation=None)
        if not cp.read(path):
            raise SystemExit(f"Config file not found: {path}")
        d = cp["DEFAULT"]

        self.server = d.get("server")
        self.port = d.getint("port", 6667)
        self.nickname = d.get("nickname")
        self.realname = d.get("realname", self.nickname)
        self.channel = d.get("channel")
        self.bot_message_delay = d.getfloat("bot_message_delay", 1)
        self.greet_nicks = _parse_greetings(d.get("greet_nicks", ""))

        self.reference_lat = d.getfloat("reference_lat")
        self.reference_lon = d.getfloat("reference_lon")

        # Plain civilian traffic still only counts when it's low and local.
        self.altitude_threshold = d.getint("altitude_threshold", 3000)
        # The see/hear ceiling. Above it nothing alerts except hand-picked
        # tails and emergencies: if you can't see or hear it, it's a digest
        # statistic, not an interruption.
        self.alert_ceiling_ft = d.getint("alert_ceiling_ft", 8000)
        # How close an aircraft's projected path must pass to count as
        # "actually coming here" rather than merely pointing this way.
        self.inbound_cpa_nm = d.getint("inbound_cpa_nm", 25)
        self.alert_interval = timedelta(minutes=d.getint("alert_interval_minutes", 15))
        self.verbose = d.getboolean("verbose", False)

        self.watchlist_squawks = _split(d.get("watchlist_squawks", ""))
        self.watchlist_categories = [c.upper() for c in _split(d.get("watchlist_categories", ""))]
        self.watchlist_aircraft = {a.lower() for a in _split(d.get("watchlist_aircraft", ""))}
        # Callsign prefixes worth an alert whatever they're doing -- REDARROW
        # flies whichever Hawk is serviceable, so a hex watchlist can't hold it.
        self.watchlist_callsigns = [c.upper() for c in _split(d.get("watchlist_callsigns", ""))]
        # Type codes so common and so identifiable by ear that the line is
        # useful but the excitement isn't (looking at you, Robinson).
        self.snooze_types = {t.upper() for t in _split(d.get("snooze_types", ""))}

        self.alert_military = d.getboolean("alert_military", True)
        self.alert_mlat = d.getboolean("alert_mlat", True)
        # Behavioural detectors: going round in circles (local data, 10 s
        # cadence), military formations and survey patterns (online sweep).
        self.loiter_alert_enabled = d.getboolean("loiter_alert_enabled", True)
        self.loiter_radius_nm = d.getfloat("loiter_radius_nm", 4.0)
        self.formation_enabled = d.getboolean("formation_enabled", True)
        self.formation_max_sep_nm = d.getfloat("formation_max_sep_nm", 2.0)
        self.formation_min_sweeps = d.getint("formation_min_sweeps", 3)
        self.survey_enabled = d.getboolean("survey_enabled", True)
        # Prebuilt elevation grid (tools/build_terrain.py) for AGL in alerts.
        self.terrain_grid = d.get("terrain_grid", "")
        self.exclude_zones = _parse_zones(d.get("exclude_zones", ""))
        # Airports whose arrivals are somebody else's business: traffic lined
        # up to land at one of these is transiting to it, not visiting us.
        self.airport_zones = _parse_zones(d.get("airport_zones", ""))
        self.photo_links = d.getboolean("photo_links", True)

        # "Went low and vanished heading our way" -- the terrain-masked
        # arrival signature, see check_lost_contact().
        self.vanish_alert_enabled = d.getboolean("vanish_alert_enabled", True)
        self.vanish_max_dist_nm = d.getint("vanish_max_dist_nm", 40)

        self.stats_json = d.get("stats_json", "/run/readsb/stats.json")
        self.digest_enabled = d.getboolean("digest_enabled", True)
        self.digest_time = d.get("digest_time", "08:00")

        self.local_json = d.get("local_json", "/run/readsb/aircraft.json")
        self.poll_seconds = d.getint("poll_seconds", 10)
        self.recheck_seconds = d.getint("recheck_seconds", 30)
        self.state_file = d.get("state_file", "last_alert_time.json")
        self.alert_db = d.get("alert_db", "")
        # Who to tell privately when the local receiver stops answering.
        self.alert_nick = d.get("alert_nick", "").strip()
        self.receiver_alert_repeat_hours = d.getfloat("receiver_alert_repeat_hours", 6.0)
        self.webapi = d.get("webapi", "") or None

        self.online_enabled = d.getboolean("online_enabled", False)
        self.online_url = d.get("online_url", "https://api.adsb.lol/v2/point/{lat}/{lon}/{radius}")
        self.online_radius_nm = d.getint("online_radius_nm", 40)
        self.online_poll_seconds = d.getint("online_poll_seconds", 60)

        self.announce_dir = d.get("announce_dir", "")

        self.astro_enabled = d.getboolean("astro_enabled", False)
        self.astro_lead_minutes = d.getint("astro_lead_minutes", 60)

        # The aurora doesn't wait for the nightly report, so it gets its
        # own poll through the dark hours. AuroraWatch UK ask for no more
        # than one request every three minutes; the default is far
        # slacker than that.
        self.aurora_enabled = d.getboolean("aurora_enabled", False)
        self.aurora_min_status = d.get("aurora_min_status", "amber").lower()
        self.aurora_poll_minutes = max(3, d.getint("aurora_poll_minutes", 15))
        self.aurora_repeat_hours = d.getfloat("aurora_repeat_hours", 3)

        self.morning_enabled = d.getboolean("morning_enabled", False)
        self.morning_time = d.get("morning_time", "06:00")
        self.bins_ical_url = d.get("bins_ical_url", "")
        self.iss_min_elevation = d.getint("iss_min_elevation", 10)

        self.sonde_enabled = d.getboolean("sonde_enabled", False)
        self.sonde_radius_km = d.getint("sonde_radius_km", 75)
        self.sonde_poll_seconds = d.getint("sonde_poll_seconds", 300)
        self.sonde_max_age_minutes = d.getint("sonde_max_age_minutes", 30)

        # Wide sweep, counting only. It used to alert on any aircraft holding
        # a converging course, but at 150 nm that is overwhelmingly airway
        # traffic at FL300+ -- impossible to see or hear, and mostly bound
        # somewhere else entirely. It now feeds the digest tally instead.
        # See process_long_range().
        self.long_range_enabled = d.getboolean("long_range_enabled", False)
        self.long_range_radius_nm = d.getint("long_range_radius_nm", 150)
        self.long_range_poll_seconds = d.getint("long_range_poll_seconds", 180)


# === Aircraft helpers ===

def parse_altitude(raw):
    """alt_baro is a number, or the string "ground", or absent (None)."""
    if isinstance(raw, (int, float)):
        return int(raw)
    if raw == "ground":
        return 0
    return None


def haversine_nm(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * asin(sqrt(a)) * 3440.065


def bearing_deg(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = sin(dlon) * cos(lat2)
    y = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    return (degrees(atan2(x, y)) + 360) % 360


def bearing_diff(a, b):
    """Smallest angular difference between two bearings, in [0, 180]."""
    return abs((a - b + 180) % 360 - 180)


def closest_approach(ref_lat, ref_lon, lat, lon, course, range_nm=None):
    """How near an aircraft holding `course` will pass a point, as
    (miss_nm, along_nm): the cross-track distance at closest approach, and how
    far ahead that point still is. A negative along_nm means the aircraft is
    already past it and opening.

    This is the test the old convergence watch was missing. "Steady bearing,
    closing range" is necessary but nowhere near sufficient -- a straight-line
    overflight that will miss by 80 miles shows exactly that shape for the
    whole approaching half of its pass. Miss distance is what separates
    something genuinely coming here from an airway transit."""
    if course is None:
        return None, None
    if range_nm is None:
        range_nm = haversine_nm(ref_lat, ref_lon, lat, lon)
    angle_off = radians(bearing_diff(bearing_deg(lat, lon, ref_lat, ref_lon), course))
    return range_nm * sin(angle_off), range_nm * cos(angle_off)


def aircraft_course(ac, previous=None):
    """Where the aircraft is actually pointing: its broadcast track, the
    server-derived calc_track (all an MLAT-only contact ever gets -- a lot of
    the interesting military traffic), or failing that the bearing from a
    previous fix to this one."""
    for key in ("track", "calc_track"):
        track = ac.get(key)
        if track is not None:
            return track
    lat, lon = ac.get("lat"), ac.get("lon")
    if previous is None or lat is None or lon is None:
        return None
    prev_lat, prev_lon = previous
    if haversine_nm(prev_lat, prev_lon, lat, lon) < 0.5:
        return None  # hasn't moved far enough for the bearing to mean anything
    return bearing_deg(prev_lat, prev_lon, lat, lon)


def squawk_in_watchlist(squawk, watchlist):
    if not squawk:
        return False
    for item in watchlist:
        if "-" in item:
            try:
                start, end = item.split("-", 1)
                if int(start) <= int(squawk) <= int(end):
                    return True
            except ValueError:
                continue
        elif item == squawk:
            return True
    return False


def is_emergency(ac):
    return ((ac.get("emergency") or "none").lower() != "none"
            or (ac.get("squawk") or "") in EMERGENCY_SQUAWKS)


def is_military(ac):
    # dbFlags bit 0 = military, per wiedehopf's aircraft database
    # (needs readsb --db-file locally; comes free from the aggregator).
    return bool((ac.get("dbFlags") or 0) & 1)


def is_snoozer(ac, cfg):
    """Types announced with a sleepy (Zzz) instead of Alert! -- identifiable
    by ear before the line is even read, so it carries no suspense."""
    return (ac.get("t") or "").strip().upper() in cfg.snooze_types


# "No info"/reserved emitter categories -- Category Set D (D0-D7) is entirely
# unassigned, and A0/B0/B5/C0/C6/C7 are each that set's own "no information"
# or reserved slot. A DF17 message reporting one of these passed CRC (so the
# bits are real), but it carries no more information than no category at all.
RESERVED_CATEGORIES = {"A0", "B0", "B5", "C0", "C6", "C7",
                        "D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7"}


def has_identity(ac):
    """Whether there is any evidence this is a real aircraft beyond a bare
    altitude reply: a position, a callsign, a squawk, or a database match.

    Mode-S short replies (DF4/5/20/21) don't carry the ICAO address in the
    clear -- it's XORed into the parity field and *derived* by the decoder --
    so one bit error in a weak frame invents a plausible-looking address
    reporting a perfectly clean altitude and nothing else. Position frames
    (DF17) carry a real CRC and simply fail instead, which is why these
    phantoms never have one."""
    category = ac.get("category")
    return bool(ac.get("lat") is not None
                or (ac.get("flight") or "").strip()
                or ac.get("squawk")
                or (category and category not in RESERVED_CATEGORIES)
                or ac.get("t") or ac.get("desc") or ac.get("r"))


def always_alert(ac, cfg):
    """Worth an alert at any altitude: a hand-picked airframe or callsign, or
    an emergency. Everything else has to be low enough to see or hear. An
    emergency needs has_identity too: the DF emergency/priority field arrives
    in the same corroboration-free Mode-S replies that invent phantom
    addresses, and one bit-flipped frame once alerted 'unlawful interference'
    from an aircraft that never existed (alerts.db id 40)."""
    flight = (ac.get("flight") or "").strip().upper()
    return ((ac.get("hex") or "").lower() in cfg.watchlist_aircraft
            or any(flight.startswith(c) for c in cfg.watchlist_callsigns)
            or (is_emergency(ac) and has_identity(ac)))


def squawk_within_distance_limit(ac, cfg):
    """Some watchlist squawks (SQUAWK_DISTANCE_LIMITS_MI) are only worth
    reporting within a limited radius. No position means the limit can't be
    confirmed, so it doesn't pass. Squawks with no configured limit always
    pass."""
    limit_mi = SQUAWK_DISTANCE_LIMITS_MI.get(ac.get("squawk"))
    if limit_mi is None:
        return True
    lat, lon = ac.get("lat"), ac.get("lon")
    if lat is None or lon is None:
        return False
    dist_mi = haversine_nm(cfg.reference_lat, cfg.reference_lon, lat, lon) * NM_TO_MILES
    return dist_mi <= limit_mi


def see_hear_worthy(ac, cfg, local=True):
    """Types worth an alert *when they're low enough to notice*. Category and
    MLAT triggers stay local-only: inside the online ring they'd match every
    civilian rotorcraft and MLAT'd Cessna on the periphery."""
    return ((squawk_in_watchlist(ac.get("squawk"), cfg.watchlist_squawks)
             and squawk_within_distance_limit(ac, cfg))
            or (cfg.alert_military and is_military(ac))
            or (local and ac.get("category") in cfg.watchlist_categories)
            or (local and cfg.alert_mlat and ac.get("type") == "mlat"))


def in_excluded_zone(ac, cfg):
    """True when the aircraft sits inside a configured exclusion or airport
    zone and below its altitude cutoff — e.g. Prestwick's circuit and
    approaches. No position means no exclusion; missing altitude counts as
    below the cutoff (an airport zone's dataless traffic is usually on the
    ground)."""
    lat, lon = ac.get("lat"), ac.get("lon")
    if lat is None or lon is None:
        return False
    altitude = parse_altitude(ac.get("alt_baro"))
    return any(
        (altitude is None or altitude < below_ft)
        and haversine_nm(zlat, zlon, lat, lon) <= radius_nm
        for zlat, zlon, radius_nm, below_ft, _ in cfg.exclude_zones + cfg.airport_zones
    )


def bound_for_airport(ac, cfg, course):
    """The airport this aircraft is landing at, best guess -- an arrival
    somewhere else, not a visitor. Same closest-approach test used to decide
    whether something is coming to us, measured against each airport instead.
    Returns the zone's name (or True if it has none) so callers can either
    suppress the alert or annotate it; None when it isn't airport-bound.

    Descending and lined up is the classic signature, but approaches also
    hold level through step-downs and vectoring (a jet lined up dead-on
    Carlisle at 1800 ft was alerting because it wasn't descending that
    second), so close in, lined up and below the zone's cutoff counts even
    without a descent rate -- as long as it isn't climbing away."""
    lat, lon = ac.get("lat"), ac.get("lon")
    if lat is None or lon is None or course is None:
        return None
    climb = ac.get("baro_rate", ac.get("geom_rate"))
    altitude = parse_altitude(ac.get("alt_baro"))
    for zlat, zlon, radius_nm, below_ft, name in cfg.airport_zones:
        miss, along = closest_approach(zlat, zlon, lat, lon, course)
        if miss is None or along <= 0 or miss > radius_nm:
            continue
        # Descending and lined up: the classic arrival, at any range.
        if climb is not None and climb <= -200:
            return name or True
        if altitude is None or altitude >= below_ft:
            continue
        # Lined up below the zone's cutoff with no rate at all (MLAT rarely
        # carries one -- a missing rate is no evidence of level flight), or
        # holding level close in (step-downs, vectoring): still an arrival.
        # Climbing traffic never is.
        if climb is None or (climb < 200 and along <= 2.5 * radius_nm):
            return name or True
    return None


def activity_guess(ac, cfg, dest):
    """A line of context for traffic that alerts regardless of where it's
    going (watchlist tails and callsigns): where it's probably landing, or
    what it just climbed out of. Guesses, labelled as such."""
    if isinstance(dest, str):
        return f"probably landing at {dest}"
    lat, lon = ac.get("lat"), ac.get("lon")
    climb = ac.get("baro_rate", ac.get("geom_rate"))
    if lat is None or lon is None or climb is None or climb < 200:
        return None
    for zlat, zlon, radius_nm, _, name in cfg.airport_zones:
        if name and haversine_nm(zlat, zlon, lat, lon) <= 1.5 * radius_nm:
            return f"probably out of {name}"
    return None


# === Behavioural detectors ===

def update_orbit(orbits, icao, ac, cfg):
    """Accumulate signed course change while the aircraft stays near where
    it started; two full turns' worth is someone going round in circles --
    police, SAR, survey, calibration. Returns minutes on station once that
    threshold is crossed, else None. Signed sum so a straight transit (~0)
    and jinking (+/- cancelling) don't accumulate; figure-eights cancel too
    and are accepted as the price of that. The wander limit is 2x the
    configured radius because the anchor is a point ON the orbit, not its
    centre -- the far side of an r-radius circle is 2r away."""
    lat, lon = ac.get("lat"), ac.get("lon")
    course = aircraft_course(ac)
    now = time.time()
    if lat is None or lon is None or course is None:
        return None
    st = orbits.get(icao)
    if st is None or now - st["last"] > 120:
        orbits[icao] = {"anchor": (lat, lon), "prev": course, "cum": 0.0,
                        "start": now, "last": now}
        return None
    st["last"] = now
    gs = ac.get("gs")
    if gs is not None and gs < 25:
        # Hovering or ground-running: track jitter can random-walk the
        # accumulator to 720 with the aircraft essentially stationary.
        st["prev"] = course
        return None
    delta = (course - st["prev"] + 180) % 360 - 180
    st["prev"] = course
    if haversine_nm(st["anchor"][0], st["anchor"][1], lat, lon) > 2 * cfg.loiter_radius_nm:
        # Wandered off: whatever it was doing, it isn't loitering here.
        st.update(anchor=(lat, lon), cum=0.0, start=now)
        return None
    st["cum"] += delta
    if abs(st["cum"]) >= 720:
        return (now - st["start"]) / 60
    return None


def in_company(a, b, cfg):
    """Two aircraft flying as one: close, co-level, matched speed and
    heading. MLAT jitter is why missing fields pass rather than fail --
    separation is the one test that always has data."""
    if haversine_nm(a["lat"], a["lon"], b["lat"], b["lon"]) > cfg.formation_max_sep_nm:
        return False
    alt_a, alt_b = parse_altitude(a.get("alt_baro")), parse_altitude(b.get("alt_baro"))
    if alt_a is not None and alt_b is not None and abs(alt_a - alt_b) > 1000:
        return False
    gs_a, gs_b = a.get("gs"), b.get("gs")
    if gs_a is not None and gs_b is not None and abs(gs_a - gs_b) > 30:
        return False
    crs_a, crs_b = aircraft_course(a), aircraft_course(b)
    if crs_a is not None and crs_b is not None and bearing_diff(crs_a, crs_b) > 15:
        return False
    return True


def detect_formations(aircraft, cfg, irc, state, formations, log_db=None):
    """Two or more military aircraft holding close company across several
    consecutive sweeps -- tanker trails, pairs in transit, display teams.
    Rare enough to be worth an alert at any altitude: a trail at FL240 is
    still a line of contrails."""
    if not cfg.formation_enabled:
        return
    now = time.time()
    mil = {}
    for ac in aircraft:
        icao = (ac.get("hex") or "").lower()
        # Positive evidence of being airborne is required -- aircraft on
        # the ground often report a numeric altitude (no weight-on-wheels
        # wiring), and a ramp full of parked tankers at Prestwick is inside
        # the online ring. A pair genuinely in company shows a groundspeed.
        if (icao and not icao.startswith("~") and is_military(ac)
                and ac.get("lat") is not None and ac.get("lon") is not None
                and ac.get("alt_baro") not in (None, "ground")
                and ac.get("gs") is not None and ac.get("gs") >= 50):
            mil[icao] = ac

    live = set()
    icaos = sorted(mil)
    for i, a_id in enumerate(icaos):
        for b_id in icaos[i + 1:]:
            if in_company(mil[a_id], mil[b_id], cfg):
                key = a_id + "+" + b_id
                entry = formations.setdefault(key, {"count": 0, "last": 0.0})
                entry["count"] += 1
                entry["last"] = now
                live.add(key)
    for key, entry in list(formations.items()):
        if key not in live:
            # The streak is consecutive sweeps: one miss resets it. The
            # entry itself lingers a little longer purely to bound churn.
            entry["count"] = 0
            if now - entry["last"] > 3 * cfg.online_poll_seconds:
                del formations[key]

    # Persistent pairs merge into flights: A+B and B+C is a three-ship.
    links = [k.split("+") for k, e in formations.items()
             if k in live and e["count"] >= cfg.formation_min_sweeps]
    groups = []
    for a_id, b_id in links:
        touching = [g for g in groups if a_id in g or b_id in g]
        merged = {a_id, b_id}.union(*touching) if touching else {a_id, b_id}
        groups = [g for g in groups if g not in touching] + [merged]

    for group in groups:
        # One announcement per flight; a re-alert needs a new face in it
        # (or six hours -- long enough that the same pair transiting back
        # through in the evening is news again).
        if not any(state.should_alert(m + ":formation", timedelta(hours=6))
                   for m in group):
            continue
        def label(m):
            name = (mil[m].get("flight") or "").strip() or m
            actype = (mil[m].get("t") or "").strip()
            return f"{name} ({actype})" if actype else name
        lead = max(group, key=lambda m: bool((mil[m].get("flight") or "").strip()))
        others = ", ".join(label(m) for m in sorted(group) if m != lead)
        extra = f"{len(group)}-ship, in company with {others}"
        dispatch(irc, cfg, mil[lead], kind="Formation", tag="online data",
                 extra=extra, source="online", log_db=log_db)
        for m in group:
            state.mark(m + ":formation")


def update_path(paths, icao, ac):
    """Roll a ~45-minute course/position history per hex (online sweep
    cadence) for the pattern detectors. A coverage gap breaks the history:
    stitching fixes minutes apart would let two unrelated passes read as
    one pattern."""
    lat, lon = ac.get("lat"), ac.get("lon")
    course = aircraft_course(ac)
    if lat is None or lon is None or course is None:
        return None
    history = paths.setdefault(icao, deque(maxlen=45))
    if history and time.time() - history[-1][0] > 180:
        history.clear()
    history.append((time.time(), lat, lon, course))
    return history


def survey_pattern(history):
    """The mapping signature: 20+ minutes of legs along one axis with ~180
    degree reversals, the legs *marching* steadily across the area -- each
    new line offset the same way from the last. The march is what separates
    a survey grid from a racetrack hold, whose two legs swap back and forth
    over the same ground (a signed-turn test can't do it: at sweep cadence a
    reversal completes between samples and its direction is unknowable).
    Axis discipline is measured with axial statistics -- courses folded mod
    180 via the doubled-angle resultant. Returns (minutes, legs) or None."""
    if len(history) < 15:
        return None
    window = list(history)[-30:]
    span_min = (window[-1][0] - window[0][0]) / 60
    if span_min < 20:
        return None
    lats = [w[1] for w in window]
    lons = [w[2] for w in window]
    if haversine_nm(min(lats), min(lons), max(lats), max(lons)) > 40:
        return None
    courses = [w[3] for w in window]
    # Doubled-angle resultant: 1.0 = every sample on one axis, 0 = no axis.
    sin2 = sum(sin(2 * radians(c)) for c in courses) / len(courses)
    cos2 = sum(cos(2 * radians(c)) for c in courses) / len(courses)
    if sqrt(sin2 * sin2 + cos2 * cos2) < 0.6:
        return None
    axis = degrees(atan2(sin2, cos2)) / 2
    # Split into legs at each reversal, then watch the leg midpoints march
    # across the axis.
    legs, start = [], 0
    for i in range(1, len(window)):
        if bearing_diff(courses[i], courses[i - 1]) > 150:
            legs.append(window[start:i])
            start = i
    legs.append(window[start:])
    if len(legs) < 4:  # three reversals minimum
        return None
    ref_lat, ref_lon = window[0][1], window[0][2]
    perp = radians(axis + 90)
    offsets = []
    for leg in legs:
        mid_lat = sum(p[1] for p in leg) / len(leg)
        mid_lon = sum(p[2] for p in leg) / len(leg)
        north = (mid_lat - ref_lat) * 60
        east = (mid_lon - ref_lon) * 60 * cos(radians(ref_lat))
        offsets.append(north * cos(perp) + east * sin(perp))
    if max(offsets) - min(offsets) < 2:  # nm of lateral progress
        return None
    steps = [b - a for a, b in zip(offsets, offsets[1:])]
    signs = [1 if s > 0 else -1 for s in steps if abs(s) > 0.1]
    # The march must involve most of the leg transitions, and most of them
    # must go the same way. Without the quorum, a hold that relocated once
    # (an ATC re-clear) qualifies on the strength of that single step.
    if len(signs) < max(2, (len(legs) - 1) // 2):
        return None
    if abs(sum(signs)) < 0.7 * len(signs):
        return None  # oscillating over the same line: a hold, not a grid
    return span_min, len(legs)


def meets_criteria(ac, cfg, local=True):
    """Three tiers, in order: hand-picked tails and emergencies alert at any
    altitude; military and other interesting types alert only below the
    see/hear ceiling; plain civilian traffic only when it's low and local.

    Anything failing all three is too high to see or hear, and is counted for
    the daily digest instead of interrupting anyone (see note_high_transit)."""
    if always_alert(ac, cfg):
        return True
    altitude = parse_altitude(ac.get("alt_baro"))
    if altitude is None:
        return False  # can't judge the ceiling yet; re-checked as data arrives
    # is_military() is a bare hex-range lookup (e.g. 0xAE0000-0xAFFFFF for US
    # military) with nothing else backing it up, so a phantom Mode-S address
    # landing in one of those blocks reads as a military contact out of thin
    # air -- that block alone is ~1/128 of the address space. has_identity()
    # is a no-op for the squawk/category matches in see_hear_worthy (a match
    # there already implies one of those fields is set) but it closes that
    # hole.
    if see_hear_worthy(ac, cfg, local) and altitude <= cfg.alert_ceiling_ft and has_identity(ac):
        return True
    # Plain civilian traffic needs corroboration as well as a low altitude,
    # or a phantom Mode-S address reporting one becomes an alert.
    return (local and 0 < altitude < cfg.altitude_threshold and has_identity(ac))


def format_alert(ac, cfg, kind="Alert", tag=None, photo=None, extra=None):
    icao = ac.get("hex", "")
    flight = (ac.get("flight") or "").strip() or "Unknown"
    altitude = parse_altitude(ac.get("alt_baro"))
    squawk = ac.get("squawk") or "n/a"
    lat, lon = ac.get("lat"), ac.get("lon")
    gs, ias, tas = ac.get("gs"), ac.get("ias"), ac.get("tas")

    # IRC formatting: bold + white/yellow/green
    flight_str = f"\x02\x0300{flight}\x03\x02"
    squawk_str = f"\x02\x0303{squawk}\x03\x02"
    # Most codes mean something specific in the UK plan (0020 air ambulance,
    # 0036 pipeline inspection...). Ordinary ATC-assigned codes return None
    # and stay unadorned.
    reason = squawks.purpose(squawk)
    if reason:
        squawk_str += f" ({reason})"

    if ac.get("alt_baro") == "ground":
        alt_part = "\x02\x0308on the ground\x03\x02"
    elif altitude is None:
        alt_part = "at \x02\x0308unknown altitude\x03\x02"
    else:
        alt_part = f"at altitude \x02\x0308{altitude} ft\x03\x02"
        # Around here barometric altitude flatters everything: 3000 ft over
        # an 2500 ft ridge is rooftop height. Say what the ground thinks,
        # rounded to 100 ft -- the inputs don't honestly give better.
        agl = terrain.agl_ft(ac, altitude) if altitude <= 10000 else None
        if agl is not None:
            alt_part += f" (≈{max(0, round(agl / 100) * 100)} ft AGL)"
        climb = ac.get("baro_rate", ac.get("geom_rate"))
        if climb is not None and abs(climb) >= 100:
            arrow = "↑" if climb > 0 else "↓"
            alt_part += f" {arrow}{abs(climb)} fpm"

    head = "(Zzz)" if kind == "Zzz" else f"{kind}!"
    if tag:
        head += f" ({tag})"
    message = f"{head} Aircraft {flight_str} ({icao}) with squawk {squawk_str} {alt_part}"

    actype, reg = ac.get("desc") or ac.get("t"), ac.get("r")
    if actype or reg:
        message += f" | {' '.join(filter(None, [actype, reg]))}"
    if ac.get("ownOp"):
        message += f" ({ac['ownOp']})"
    if is_military(ac):
        message += " | \x02\x0304MILITARY\x03\x02"
    if ac.get("type") == "mlat":
        message += " | MLAT position (no ADS-B)"
    if ac.get("category"):
        message += f" | Category: {ac['category']}"
    if is_emergency(ac):
        # The emergency field only comes from ADS-B version 2 targets. A
        # bare 7500/7600/7700 squawk -- the case that matters most --
        # carries no such field, so fall back to the code itself rather
        # than raising KeyError out of the alert path.
        message += f" | EMERGENCY: {ac.get('emergency') or 'squawking ' + squawk}"
    roll = ac.get("roll")
    if roll is not None and abs(roll) > 25:
        message += f" | Manoeuvring (roll {abs(roll):.0f}°)"

    if lat is not None and lon is not None:
        dist_nm = haversine_nm(cfg.reference_lat, cfg.reference_lon, lat, lon)
        brg = bearing_deg(cfg.reference_lat, cfg.reference_lon, lat, lon)
        direction = COMPASS[round(brg / 22.5) % 16]
        message += f" | Distance: {dist_nm * NM_TO_MILES:.1f} miles {direction} ({brg:.0f}°)"
        if gs:
            # Only show an ETA when the aircraft is actually heading our way.
            track = ac.get("track")
            inbound = track is None or abs((track - brg) % 360 - 180) <= 90
            if inbound:
                message += f" | ETA: {dist_nm * 3600 / gs:.0f} seconds"

    speeds = [f"{label}: {value:.0f} knots"
              for label, value in (("Ground Speed", gs), ("IAS", ias), ("TAS", tas))
              if value is not None]
    if speeds:
        message += " | " + ", ".join(speeds)

    if extra:
        message += f" | {extra}"

    if icao:
        message += f" | Track here: https://globe.adsbexchange.com/?icao={icao}"
    if reason and len(message) > IRC_LINE_LIMIT:
        # The line is truncated from the right, which would take the
        # tracking link with it. Of everything on it, what the squawk is
        # for is the cheapest thing to give up.
        message = message.replace(f" ({reason})", "", 1)
    if photo and len(message) + len(photo) + 10 <= IRC_LINE_LIMIT:  # never truncate mid-URL
        message += f" | Photo: {photo}"
    return message


def format_squawk_update(ac):
    """One-liner for a squawk that decoded after the alert went out. Mode A
    arrives by interrogation reply, typically half a minute behind the first
    position, so a fresh contact alerts as (n/a) and the code — often the
    most telling thing on the line — deserves a follow-up when it lands."""
    icao = ac.get("hex", "")
    flight = (ac.get("flight") or "").strip() or "Unknown"
    squawk = ac["squawk"]
    squawk_str = f"\x02\x0303{squawk}\x03\x02"
    reason = squawks.purpose(squawk)
    if reason:
        squawk_str += f" ({reason})"
    message = f"Squawk update: \x02\x0300{flight}\x03\x02 ({icao}) now squawking {squawk_str}"
    if icao:
        message += f" | Track here: https://globe.adsbexchange.com/?icao={icao}"
    return message


# === Alert bookkeeping ===

class AlertState:
    """Remembers when each aircraft was last alerted, persisted to disk."""

    def __init__(self, cfg):
        self.path = Path(cfg.state_file)
        self.interval = cfg.alert_interval
        self.times = {}
        self.dirty = False
        try:
            raw = json.loads(self.path.read_text())
            self.times = {k: datetime.fromisoformat(v) for k, v in raw.items()}
        except (FileNotFoundError, ValueError):
            pass

    def should_alert(self, key, interval=None):
        return datetime.now() - self.times.get(key, datetime.min) >= (interval or self.interval)

    def mark(self, key):
        self.times[key] = datetime.now()
        self.dirty = True

    def save_if_dirty(self):
        if not self.dirty:
            return
        cutoff = datetime.now() - max(2 * self.interval, timedelta(hours=24))
        self.times = {k: v for k, v in self.times.items() if v > cutoff}
        self.path.write_text(json.dumps({k: v.isoformat() for k, v in self.times.items()}))
        self.dirty = False


# === IRC ===

IRC_IDLE_TIMEOUT = 300  # seconds with no data at all (incl. server PINGs) before we assume the link is dead


class IrcClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sock = None
        self.buffer = b""
        self.connected = False
        self.next_attempt = 0.0
        self.backoff = 5
        self.nick = cfg.nickname
        self.greeted = {}
        self.last_recv = 0.0

    def ensure_connected(self):
        if self.connected or time.time() < self.next_attempt:
            return
        try:
            self._connect()
            self.connected = True
            self.backoff = 5
            log.info("Joined %s", self.cfg.channel)
        except OSError as e:
            self._teardown()
            log.warning("IRC connect failed: %s (retrying in %ds)", e, self.backoff)
            self.next_attempt = time.time() + self.backoff
            self.backoff = min(self.backoff * 2, 300)

    def _connect(self):
        log.info("Connecting to %s:%d ...", self.cfg.server, self.cfg.port)
        self.sock = socket.create_connection((self.cfg.server, self.cfg.port), timeout=30)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
        except (AttributeError, OSError):
            pass  # keepalive tuning isn't available on every platform
        self.buffer = b""
        self.last_recv = time.time()
        nick = self.nick = self.cfg.nickname
        self._send_line(f"NICK {nick}")
        self._send_line(f"USER {nick} 0 * :{self.cfg.realname}")

        deadline = time.time() + 90
        while time.time() < deadline:
            for line in self._read_lines(timeout=5):
                parts = line.split()
                code = parts[1] if len(parts) > 1 else ""
                if code == "001":  # registered
                    self._send_line(f"JOIN {self.cfg.channel}")
                    return
                if code in ("433", "437"):  # nick unavailable
                    nick = self.nick = nick + "_"
                    log.info("Nick in use, trying %s", nick)
                    self._send_line(f"NICK {nick}")
                if line.startswith("ERROR"):
                    raise OSError(f"server error: {line}")
        raise OSError("IRC registration timed out")

    def _read_lines(self, timeout):
        """Read whatever is available, answer PINGs, return other lines."""
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(4096)
        except (TimeoutError, BlockingIOError):
            return []
        if not data:
            raise OSError("connection closed by server")
        self.last_recv = time.time()
        self.buffer += data
        lines = []
        while b"\r\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\r\n", 1)
            line = raw.decode("utf-8", errors="replace")
            if line.startswith("PING"):
                self._send_line("PONG" + line[4:])
            else:
                lines.append(line)
        return lines

    def _send_line(self, line):
        self.sock.sendall(f"{line}\r\n".encode("utf-8"))

    def _teardown(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.connected = False

    def poll(self):
        """Call regularly: answers server PINGs, greets any configured nick as
        it joins, and notices dropped or silently stale connections."""
        if not self.connected:
            return
        if time.time() - self.last_recv > IRC_IDLE_TIMEOUT:
            log.warning("IRC connection stale (no data in over %ds), reconnecting", IRC_IDLE_TIMEOUT)
            self._teardown()
            self.next_attempt = time.time() + self.backoff
            return
        try:
            lines = self._read_lines(timeout=0)
        except OSError as e:
            log.warning("IRC connection lost: %s", e)
            self._teardown()
            self.next_attempt = time.time() + self.backoff
            return
        for line in lines:
            self._maybe_greet(line)

    def _maybe_greet(self, line):
        """Say hello when one of the greet_nicks joins our channel."""
        m = JOIN_RE.match(line)
        if not m:
            return
        nick, channel = m.group(1), m.group(2)
        if nick == self.nick or channel.lower() != self.cfg.channel.lower():
            return
        message = self.cfg.greet_nicks.get(nick.lower())
        if not message:
            for pattern, greeting in self.cfg.greet_nicks.items():
                if (("*" in pattern or "?" in pattern)
                        and fnmatch.fnmatchcase(nick.lower(), pattern)):
                    message = greeting
                    break
        if not message:
            return
        if time.time() - self.greeted.get(nick.lower(), 0) < GREET_COOLDOWN:
            return
        self.greeted[nick.lower()] = time.time()
        self.send_message(f"{nick}: {message}")

    def send_message(self, text):
        if not self.connected:
            log.warning("IRC not connected, alert not delivered: %s", text)
            return False
        try:
            self._send_line(f"PRIVMSG {self.cfg.channel} :{text[:IRC_LINE_LIMIT]}")
            log.info("Sent to IRC: %s", text)
            return True
        except OSError as e:
            log.warning("IRC send failed: %s", e)
            self._teardown()
            self.next_attempt = time.time() + self.backoff
            return False

    def send_notice(self, nick, text):
        """A private line to one person, for things the channel doesn't need.
        Returns False when the link is down so the caller can hold the message
        and try again rather than dropping it on the floor."""
        if not nick:
            return True  # nobody configured, so nothing is owed
        if not self.connected:
            return False
        try:
            self._send_line(f"PRIVMSG {nick} :{text[:IRC_LINE_LIMIT]}")
            log.info("Sent to %s: %s", nick, text)
            return True
        except OSError as e:
            log.warning("IRC notice failed: %s", e)
            self._teardown()
            self.next_attempt = time.time() + self.backoff
            return False


# === Data sources ===

def fetch_local(cfg):
    """Returns (aircraft, error). aircraft is None when the feed can't be
    read; the reason comes back alongside rather than going straight to the
    log, because at one poll a second that warning is what buries the very
    history you'd want in order to explain the outage. ReceiverHealth decides
    what is worth saying and how often."""
    try:
        data = json.loads(Path(cfg.local_json).read_text())
        return data.get("aircraft", []), None
    except (OSError, ValueError) as e:
        return None, str(e)


RECEIVER_DOWN_MESSAGE = "UNABLE TO READ AIRCRAFT, USB ISSUE"


class ReceiverHealth:
    """Whether the local feed is readable, reported once per transition.

    A dead dongle is otherwise silent: the channel simply goes quiet while
    the online ring carries on as though nothing had happened, so the first
    sign of trouble is noticing days later that nothing local has been seen.
    This says so directly, to one person, the moment the feed stops
    answering.

    It also logs the fault once instead of once per poll. The old per-poll
    warning wrote about a line a second while readsb crash-looped, which on a
    RAM-only journal evicted the entire alert history inside two hours -- the
    complaint destroying the evidence.

    The notice is queued rather than sent outright, because the receiver can
    fail while IRC is disconnected. It goes out on the first poll after the
    link returns, and repeats every receiver_alert_repeat_hours while the
    fault stands, so a failure that starts overnight isn't one missed line."""

    def __init__(self, cfg, log_db):
        self.cfg = cfg
        self.db = log_db
        self.up = None           # None until the first poll settles it
        self.since = time.time()
        self.pending = ""        # notice queued for delivery
        self.last_notice = 0.0

    def update(self, ok, detail, irc):
        """Call once per local poll with whether the feed read cleanly."""
        now = time.time()
        if ok != self.up:
            was, down_since = self.up, self.since
            self.up, self.since = ok, now
            if ok:
                if was is not None:   # not worth announcing a clean start
                    log.info("Local feed readable again (down for %s)",
                             format_duration(now - down_since))
                    self.pending = "Aircraft feed readable again."
                    self.db.record_receiver("up")
            else:
                log.warning("Local feed unreadable: %s -- %s",
                            self.cfg.local_json, detail)
                self.pending = RECEIVER_DOWN_MESSAGE
                self.db.record_receiver("down", detail)
        elif (not ok and not self.pending
                and self.cfg.receiver_alert_repeat_hours > 0):
            if now - self.last_notice >= self.cfg.receiver_alert_repeat_hours * 3600:
                self.pending = (f"{RECEIVER_DOWN_MESSAGE} "
                                f"(still down after {format_duration(now - self.since)})")
        self._deliver(irc, now)

    def _deliver(self, irc, now):
        if not self.pending:
            return
        if irc.send_notice(self.cfg.alert_nick, self.pending):
            self.pending = ""
            self.last_notice = now


def format_duration(seconds):
    """Rough human span -- minutes below an hour, then hours, then days."""
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 60 * 48:
        return f"{minutes // 60}h {minutes % 60:02d}m"
    return f"{minutes // 1440}d {(minutes % 1440) // 60:02d}h"


def fetch_online(cfg, radius_nm=None):
    url = cfg.online_url.format(lat=cfg.reference_lat, lon=cfg.reference_lon,
                                radius=radius_nm or cfg.online_radius_nm)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("ac", [])
    except (OSError, ValueError) as e:
        log.warning("Online API fetch failed: %s", e)
        return None


_photo_cache = {}


def fetch_photo_link(icao):
    """Look up a planespotters.net photo page for this airframe (cached).
    Only called when an alert actually fires, so request volume is tiny."""
    if icao in _photo_cache:
        return _photo_cache[icao]
    url = f"https://api.planespotters.net/pub/photos/hex/{icao}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            photos = json.loads(resp.read()).get("photos") or []
        link = photos[0]["link"].split("?")[0] if photos else None
        _photo_cache[icao] = link  # cache "no photo" too; transient errors aren't cached
        return link
    except (OSError, ValueError, KeyError) as e:
        log.warning("Photo lookup failed for %s: %s", icao, e)
        return None


def fetch_sondes(cfg):
    """Radiosondes near us, from the SondeHub aggregator (community
    receivers, like adsb.lol for ADS-B). Keyed by sonde serial."""
    url = (f"https://api.v2.sondehub.org/sondes?lat={cfg.reference_lat}"
           f"&lon={cfg.reference_lon}&distance={cfg.sonde_radius_km * 1000}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (OSError, ValueError) as e:
        log.warning("SondeHub fetch failed: %s", e)
        return None


def send_web_alert(cfg, message):
    if not cfg.webapi:
        return
    req = urllib.request.Request(cfg.webapi, data=message.encode("utf-8"),
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log.info("Web alert sent (%d)", resp.status)
    except OSError as e:
        log.warning("Web alert failed: %s", e)


# === Alert dispatch ===

def dispatch(irc, cfg, ac, kind="Alert", tag=None, extra=None, source=None,
             log_db=None, message=None):
    if message is None:
        photo = None
        if cfg.photo_links and ac.get("hex"):
            photo = fetch_photo_link(ac["hex"].lower())
        message = format_alert(ac, cfg, kind=kind, tag=tag, photo=photo, extra=extra)
    irc.send_message(message)
    send_web_alert(cfg, message)
    if log_db is not None:
        lat, lon = ac.get("lat"), ac.get("lon")
        dist_nm = brg = None
        if lat is not None and lon is not None:
            dist_nm = haversine_nm(cfg.reference_lat, cfg.reference_lon, lat, lon)
            brg = bearing_deg(cfg.reference_lat, cfg.reference_lon, lat, lon)
        log_db.record_alert(ac, message, kind=kind, tag=tag, source=source,
                            dist_nm=dist_nm, bearing_deg=brg,
                            altitude=parse_altitude(ac.get("alt_baro")),
                            military=is_military(ac),
                            mlat=ac.get("type") == "mlat",
                            emergency=is_emergency(ac))
    time.sleep(cfg.bot_message_delay)


def process_local(aircraft, cfg, irc, state, rechecks, pending, squawk_watch,
                  orbits, log_db=None):
    """Alert on matching aircraft; return the set of hexes currently seen."""
    seen_hexes = set()
    by_hex = {}
    for ac in aircraft:
        icao = (ac.get("hex") or "").lower()
        if not icao or icao.startswith("~"):
            continue
        by_hex[icao] = ac
        # Only a position decode counts as "locally seen": a bare mode-S hex
        # (heard, no position) must not suppress the online sweep, or a
        # terrain-masked approacher alerts only when it's nearly overhead.
        if ac.get("seen", 0) < 60 and ac.get("lat") is not None:
            seen_hexes.add(icao)

        # Loitering is judged for everything with a position, cooldown or
        # not -- the interesting part usually starts well after the arrival
        # alert has been and gone. Only close-in circling is announced (it
        # means "something is working YOUR area", not "a glider is
        # thermalling somewhere"), gliders/ultralights don't count, and the
        # follow-up cadence is hourly with the accumulator reset so a
        # re-announcement needs two fresh turns as well.
        minutes = update_orbit(orbits, icao, ac, cfg)
        if (minutes is not None and cfg.loiter_alert_enabled
                and ac.get("category") not in ("B1", "B4")
                and state.should_alert(icao + ":loiter", timedelta(hours=1))):
            altitude = parse_altitude(ac.get("alt_baro"))
            dist_nm = haversine_nm(cfg.reference_lat, cfg.reference_lon,
                                   ac["lat"], ac["lon"])
            if (altitude is not None and 0 < altitude <= cfg.alert_ceiling_ft
                    and dist_nm <= 15 and has_identity(ac)
                    and not in_excluded_zone(ac, cfg)):
                state.mark(icao + ":loiter")
                orbits[icao]["cum"] = 0.0
                dispatch(irc, cfg, ac, kind="Loitering",
                         extra=f"circling on station for {minutes:.0f} min",
                         source="local", log_db=log_db)

        # A squawk change TO an emergency code mid-cooldown must still get
        # through: the routine alert minutes earlier already marked the hex,
        # and 7700 is exactly the update worth interrupting for again.
        emergency_now = is_emergency(ac) and has_identity(ac)
        if not ((meets_criteria(ac, cfg) or cfg.verbose) and state.should_alert(icao)):
            if not (emergency_now and state.should_alert(icao + ":emergency")):
                continue
        course = aircraft_course(ac)
        dest = bound_for_airport(ac, cfg, course)
        privileged = always_alert(ac, cfg)
        if not privileged:
            if in_excluded_zone(ac, cfg):
                continue
            # An arrival into somebody's airport, seen locally: the online
            # sweep has always run this test; the local path never did, so
            # a Prestwick-bound arrival letting down the valleys still paged.
            if dest:
                continue
        # Interesting hexes often appear in aircraft.json well before
        # anything decodes; hold the alert until there's a position or an
        # altitude — however long that takes. A timeout here would race
        # the decode and mute late-arriving data for the whole interval.
        has_data = ac.get("lat") is not None or ac.get("alt_baro") is not None
        # A dataless emergency may skip the hold, but not on the strength of
        # the suspect squawk alone -- a bit-flipped Mode-A reply reads as
        # 7700 from an aircraft that doesn't exist. Ask for one independent
        # field; a real emergency decodes an altitude within seconds anyway.
        corroborated_emergency = (emergency_now
                                  and ((ac.get("flight") or "").strip()
                                       or ac.get("t") or ac.get("r")))
        if not has_data and not corroborated_emergency:
            pending.setdefault(icao, time.time())
            continue
        pending.pop(icao, None)
        state.mark(icao)
        if emergency_now:
            state.mark(icao + ":emergency")
        # Watchlist traffic alerts even when airport-bound; say where it's
        # probably going instead of staying quiet about it.
        extra = activity_guess(ac, cfg, dest) if privileged else None
        kind = "Zzz" if is_snoozer(ac, cfg) and not is_emergency(ac) else "Alert"
        dispatch(irc, cfg, ac, kind=kind, extra=extra, source="local", log_db=log_db)
        # Data often trickles in; if position or speed was missing,
        # schedule one follow-up look.
        if ac.get("lat") is None or ac.get("gs") is None:
            rechecks.setdefault(icao, time.time() + cfg.recheck_seconds)
        if ac.get("squawk") is None:
            squawk_watch.setdefault(icao, time.time() + 4 * cfg.recheck_seconds)

    # Forget pending holds for aircraft no longer in the feed.
    for icao in list(pending):
        if icao not in by_hex:
            log.info("Dropped dataless alert for %s (held %.0fs)",
                     icao, time.time() - pending[icao])
            del pending[icao]
    # Age orbit state out on time, not presence -- one poll's absence from
    # aircraft.json must not erase an accumulating orbit.
    for icao in list(orbits):
        if time.time() - orbits[icao]["last"] > 300:
            del orbits[icao]

    # Follow-ups: send an UPDATE once the missing data has arrived.
    for icao in [k for k, due in rechecks.items() if time.time() >= due]:
        ac = by_hex.get(icao)
        if ac is not None and ac.get("lat") is not None and ac.get("gs") is not None:
            del rechecks[icao]
            # The data that turned up can disqualify what it described: one
            # contact's first position put it above the ceiling, another's
            # inside an airport zone, and the old unconditional UPDATE
            # announced them anyway.
            suppressed = (not always_alert(ac, cfg)
                          and (in_excluded_zone(ac, cfg)
                               or bound_for_airport(ac, cfg, aircraft_course(ac))))
            if (meets_criteria(ac, cfg) or cfg.verbose) and not suppressed:
                dispatch(irc, cfg, ac, kind="UPDATE", source="local", log_db=log_db)
                if is_emergency(ac) and has_identity(ac):
                    state.mark(icao + ":emergency")
                if ac.get("squawk"):
                    squawk_watch.pop(icao, None)  # the UPDATE line carried it
            else:
                # The follow-up was disqualified; its squawk one-liner is
                # equally unwanted.
                squawk_watch.pop(icao, None)
        elif time.time() >= rechecks[icao] + 4 * cfg.recheck_seconds:
            del rechecks[icao]  # give up, aircraft gone or data never arrived

    # A squawk that decodes after its alert is still news -- post a
    # one-liner the moment it lands. A late EMERGENCY code needs nothing
    # here: the ":emergency" cooldown bypass in the loop above has already
    # fired a full alert for it this same poll (dispatching from here too
    # sent the identical alert twice, ~10 s apart).
    for icao in list(squawk_watch):
        ac = by_hex.get(icao)
        if ac is None or time.time() >= squawk_watch[icao]:
            del squawk_watch[icao]
            continue
        squawk = ac.get("squawk")
        if not squawk:
            continue
        del squawk_watch[icao]
        if squawk not in EMERGENCY_SQUAWKS:
            dispatch(irc, cfg, ac, kind="Squawk update", source="local",
                     log_db=log_db, message=format_squawk_update(ac))

    return seen_hexes


def process_online(aircraft, cfg, irc, state, local_hexes, tracks, transits,
                   formations, paths, log_db=None):
    """Alert on aircraft nearby per the aggregator but invisible to the local
    receiver (e.g. terrain-masked low-fliers). Uses its own dedup timer
    (icao + ":online") but also checks the *local* timer read-only: a hex
    confirmed locally within the last alert_interval is skipped here, since a
    distant/departing online ping adds nothing once we've actually seen it
    close up. This is one-directional -- process_local never checks this key,
    so the normal shape of an encounter (spotted online while inbound, then
    confirmed by the local receiver minutes later) still posts both messages.

    Beyond alerting, this keeps a one-fix-deep track per aircraft so
    check_lost_contact() can spot the arrival signature: something low, headed
    our way, that drops off the feed short of the local receiver."""
    now = time.time()
    seen = set()
    detect_formations(aircraft, cfg, irc, state, formations, log_db)
    for ac in aircraft:
        icao = (ac.get("hex") or "").lower()
        if not icao or icao.startswith("~"):
            continue
        altitude = parse_altitude(ac.get("alt_baro"))
        lat, lon = ac.get("lat"), ac.get("lon")

        # Course history feeds the pattern detectors, and surveys are worth
        # calling at any altitude -- a mapping run photographs the ground,
        # so the ground may as well look back.
        history = update_path(paths, icao, ac)
        if history is not None and cfg.survey_enabled and has_identity(ac):
            pattern = survey_pattern(history)
            if pattern is not None and state.should_alert(icao + ":survey",
                                                          timedelta(hours=6)):
                state.mark(icao + ":survey")
                span_min, legs = pattern
                extra = (f"flying survey lines ({legs} legs in "
                         f"{span_min:.0f} min) — go outside and make a shape")
                dispatch(irc, cfg, ac, kind="Survey", tag="online data",
                         extra=extra, source="online", log_db=log_db)

        # Too high to see or hear: counted for the digest, never announced --
        # but keep the fix. The first sweep back below the ceiling is the one
        # that alerts, and for an MLAT-only contact it needs a previous
        # position to derive a course from; popping the track here meant
        # every descending arrival reached the ceiling course-less and took
        # the unknown-course benefit of the doubt.
        if (altitude is not None and altitude > cfg.alert_ceiling_ft
                and not always_alert(ac, cfg)):
            if is_military(ac):
                note_high_transit(transits, ac)
            if lat is None or lon is None:
                tracks.pop(icao, None)
            else:
                seen.add(icao)
                tracks[icao] = {"time": now, "lat": lat, "lon": lon,
                                "alt": altitude,
                                "dist": haversine_nm(cfg.reference_lat,
                                                     cfg.reference_lon, lat, lon),
                                "inbound": False, "elsewhere": False, "ac": ac}
            continue
        if lat is None or lon is None:
            continue

        seen.add(icao)
        previous = tracks.get(icao)
        course = aircraft_course(ac, previous and (previous["lat"], previous["lon"]))
        dist_nm = haversine_nm(cfg.reference_lat, cfg.reference_lon, lat, lon)
        miss, along = closest_approach(cfg.reference_lat, cfg.reference_lon,
                                       lat, lon, course, dist_nm)
        inbound = miss is not None and along > 0 and miss <= cfg.inbound_cpa_nm
        dest = bound_for_airport(ac, cfg, course)
        elsewhere = bool(dest) or in_excluded_zone(ac, cfg)
        tracks[icao] = {"time": now, "lat": lat, "lon": lon, "alt": altitude,
                        "dist": dist_nm, "inbound": inbound, "elsewhere": elsewhere,
                        "ac": ac}

        if icao in local_hexes:
            continue
        # As on the local path: a squawk change TO an emergency code must
        # get past the cooldowns the routine alert already started.
        emergency_now = is_emergency(ac) and has_identity(ac)
        routine = (meets_criteria(ac, cfg, local=False)
                   and state.should_alert(icao + ":online")
                   # confirmed locally recently? nothing new to say.
                   and state.should_alert(icao))
        if not routine and not (emergency_now and state.should_alert(icao + ":emergency")):
            continue
        # Watchlist traffic and emergencies alert wherever they're going;
        # everything else that's landing elsewhere or not coming here is
        # somebody else's traffic.
        if not always_alert(ac, cfg):
            if elsewhere:
                continue
            # An unknown course gets the benefit of the doubt -- the next
            # sweep gives us a second fix to derive one from.
            if course is not None and not inbound:
                continue
        state.mark(icao + ":online")
        if emergency_now:
            state.mark(icao + ":emergency")
        dispatch(irc, cfg, ac, kind="Alert", tag="online data",
                 extra=activity_guess(ac, cfg, dest), source="online",
                 log_db=log_db)

    for icao in list(paths):
        if now - paths[icao][-1][0] > 600:
            del paths[icao]

    check_lost_contact(cfg, irc, state, tracks, seen, local_hexes, log_db)


def check_lost_contact(cfg, irc, state, tracks, seen, local_hexes, log_db=None):
    """The arrival signature worth interrupting for: something low, tracking
    our way, that vanishes off the feed rather than landing somewhere or
    carrying on past. That's what terrain masking looks like from here -- an
    aircraft descending into the valleys drops below every receiver's horizon
    well before it reaches us."""
    for icao in [h for h in tracks if h not in seen]:
        last = tracks.pop(icao)
        if not cfg.vanish_alert_enabled or icao in local_hexes:
            continue
        if not last["inbound"] or last["elsewhere"]:
            continue
        if last["alt"] is None or last["alt"] > cfg.alert_ceiling_ft:
            continue
        if last["dist"] > cfg.vanish_max_dist_nm:
            continue
        ac = last["ac"]
        if not (meets_criteria(ac, cfg, local=False) and state.should_alert(icao + ":lost")):
            continue
        state.mark(icao + ":lost")
        extra = (f"lost contact {last['dist'] * NM_TO_MILES:.0f} mi out, low and "
                 f"tracking our way — may be coming down the valleys")
        dispatch(irc, cfg, ac, kind="Lost contact", extra=extra,
                 source="online", log_db=log_db)


def note_high_transit(transits, ac):
    """Record a distinct high-altitude military contact for the daily digest.
    Keyed by hex, so one aircraft crossing several sweeps still counts once."""
    icao = (ac.get("hex") or "").lower()
    if not icao:
        return
    label = ((ac.get("t") or "").strip() or (ac.get("desc") or "").strip()
             or "unknown type")
    transits.setdefault(icao, label)


def process_long_range(aircraft, cfg, transits):
    """The wide sweep, which no longer alerts on anything.

    It used to post a "Heads up" whenever an aircraft held a converging course
    -- steady bearing, closing range -- for a few minutes. That shape turns out
    to be nearly worthless at this radius: a straight-line overflight destined
    to miss by 80 miles shows it for the entire approaching half of its pass.
    So the sweep fired constantly on airway traffic at FL300+ that was never
    coming here and couldn't have been seen or heard if it were. Replaying
    2026-07-21..23 found almost every one was a transatlantic C-17 or tanker
    crossing at 30-35,000 ft.

    What survives is the part that was genuinely interesting: how *many* of
    them there are. Distinct high-altitude military contacts are tallied here
    and reported once a day by the digest."""
    for ac in aircraft:
        icao = (ac.get("hex") or "").lower()
        if not icao or icao.startswith("~") or not is_military(ac):
            continue
        altitude = parse_altitude(ac.get("alt_baro"))
        if altitude is not None and altitude > cfg.alert_ceiling_ft:
            note_high_transit(transits, ac)


def process_sondes(sondes, cfg, irc, state):
    """One alert per flight phase per sonde: once when first sighted
    (usually ascending), once more when it flips to descending — the
    chase-worthy moment. Stale last-known positions are skipped."""
    max_age = timedelta(minutes=cfg.sonde_max_age_minutes)
    for serial, tel in sondes.items():
        if not isinstance(tel, dict):
            continue
        lat, lon, alt = tel.get("lat"), tel.get("lon"), tel.get("alt")
        if lat is None or lon is None:
            continue
        try:
            seen = datetime.fromisoformat(tel["datetime"].replace("Z", "+00:00"))
            age = datetime.now(seen.tzinfo) - seen
        except (KeyError, ValueError):
            continue
        if age > max_age:
            continue

        vel_v = tel.get("vel_v") or 0
        phase = "descending" if vel_v < -1 else "ascending"
        key = f"sonde:{serial}:{phase}"
        if not state.should_alert(key, interval=timedelta(hours=12)):
            continue
        state.mark(key)

        dist_km = haversine_nm(cfg.reference_lat, cfg.reference_lon, lat, lon) * 1.852
        brg = bearing_deg(cfg.reference_lat, cfg.reference_lon, lat, lon)
        direction = COMPASS[round(brg / 22.5) % 16]
        sonde_type = tel.get("subtype") or tel.get("type") or "Radiosonde"
        message = (f"Radiosonde! {sonde_type} \x02\x0300{serial}\x03\x02 {phase} "
                   f"at \x02\x0308{alt:.0f} m\x03\x02 ({vel_v:+.1f} m/s)"
                   f" | {dist_km:.0f} km {direction} ({brg:.0f}°)")
        if tel.get("frequency"):
            message += f" | {tel['frequency']:.3f} MHz"
        message += f" | Track here: https://sondehub.org/{serial}"
        irc.send_message(message)
        send_web_alert(cfg, message)
        time.sleep(cfg.bot_message_delay)


def process_announcements(cfg, irc):
    """The platform glue: any other service (radiosonde_auto_rx, dumpvdl2,
    raspberry-noaa post-capture hooks, cron jobs...) drops a text file into
    announce_dir — e.g. via the norris-say helper — and each line is posted
    to the channel. Files are only removed once actually delivered."""
    spool = Path(cfg.announce_dir)
    try:
        files = sorted(spool.iterdir(), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for f in files[:10]:
        try:
            lines = [l.strip() for l in f.read_text(errors="replace").splitlines()]
        except OSError:
            continue
        if all(irc.send_message(line) for line in lines if line):
            f.unlink(missing_ok=True)
            time.sleep(cfg.bot_message_delay)
        else:
            break  # IRC is down; leave the spool for when it's back


def maybe_send_digest(cfg, irc, transits, receiver_up=True):
    """Once a day after digest_time, post a receiver stats summary to IRC.
    Counter deltas come from a snapshot of readsb's since-start totals, and
    the high-altitude military tally covers everything since the last digest
    -- the traffic that's deliberately never alerted on."""
    now = datetime.now()
    today = now.date().isoformat()
    if (now.strftime("%H:%M") < cfg.digest_time or not irc.connected
            or getattr(maybe_send_digest, "sent_date", None) == today):
        return
    # No receiver, no stats to summarise. Returning here rather than reading
    # and failing is what stops a dead dongle writing a warning a second for
    # the rest of the day -- this runs on every pass of the main loop.
    if not receiver_up:
        return
    snap_path = Path(cfg.state_file).with_suffix(".digest.json")
    try:
        snap = json.loads(snap_path.read_text())
    except (FileNotFoundError, ValueError):
        snap = {}
    if snap.get("date") == today:
        maybe_send_digest.sent_date = today
        return

    try:
        stats = json.loads(Path(cfg.stats_json).read_text())
    except (OSError, ValueError) as e:
        log.warning("Could not read %s: %s", cfg.stats_json, e)
        return
    total = stats.get("total", {})
    cur = {
        "tracks": (total.get("tracks") or {}).get("all", 0),
        "positions": total.get("position_count_total", 0),
        "messages": total.get("messages", 0),
    }
    prev = snap.get("counters", {})
    delta = {k: cur[k] - prev.get(k, 0) for k in cur}
    since = "in the last day"
    if any(v < 0 for v in delta.values()) or not prev:
        delta, since = cur, "since readsb restart"  # counters reset on restart

    local = total.get("local", {})
    parts = [
        f"Daily digest ({since}): {delta['tracks']} aircraft tracked",
        f"{delta['positions']:,} positions",
        f"{delta['messages']:,} messages",
        f"max range {total.get('max_distance', 0) / 1852:.0f} nm",
        f"signal {local.get('signal', 0):.1f} dBFS (noise {local.get('noise', 0):.1f}, peak {local.get('peak_signal', 0):.1f})",
        f"gain {stats.get('gain_db', 0):.1f} dB",
    ]
    if transits:
        counts = {}
        for label in transits.values():
            counts[label] = counts.get(label, 0) + 1
        top = ", ".join(f"{n}x {label}" for label, n in
                        sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:4])
        parts.append(f"{len(transits)} military transits too high to see or hear ({top})")
    irc.send_message(" | ".join(parts))
    transits.clear()
    snap_path.write_text(json.dumps({"date": today, "counters": cur}))
    maybe_send_digest.sent_date = today


def maybe_send_astro(cfg, irc):
    """Once a day, from astro_lead_minutes before local sunset, post
    tonight's astronomy outlook, likely-visible ISS passes and
    eve-of-collection bin reminder (built in astro.py). Same snapshot
    pattern as the digest so restarts don't repeat it."""
    now = datetime.now(astro.UK) if astro.UK else datetime.now().astimezone()
    today = now.date().isoformat()
    if getattr(maybe_send_astro, "sent_date", None) == today or not irc.connected:
        return
    _, sunset = astro.sun_times(now.date(), cfg.reference_lat, cfg.reference_lon)
    if sunset is None or now < sunset - timedelta(minutes=cfg.astro_lead_minutes):
        return
    snap_path = Path(cfg.state_file).with_suffix(".astro.json")
    try:
        if json.loads(snap_path.read_text()).get("date") == today:
            maybe_send_astro.sent_date = today
            return
    except (FileNotFoundError, ValueError):
        pass

    for line in astro.build_report(cfg):
        irc.send_message(line)
        send_web_alert(cfg, line)
        time.sleep(cfg.bot_message_delay)
    snap_path.write_text(json.dumps({"date": today}))
    maybe_send_astro.sent_date = today


def maybe_send_aurora(cfg, irc):
    """Through the dark hours, watch AuroraWatch UK and NOAA and say
    something the moment the aurora becomes a real prospect here.

    Unlike the nightly astro report this is an interrupt, because the
    aurora doesn't keep to a schedule and by morning it is a story about
    something everyone missed. It fires once per escalation rather than
    once per poll, stays quiet when the sky is covered (astro.py makes
    that call), and keeps its state in a snapshot beside the others so a
    restart loop can't turn one substorm into a flood of messages."""
    if not irc.connected:
        return
    now = time.time()
    if now < getattr(maybe_send_aurora, "next_check", 0.0):
        return
    maybe_send_aurora.next_check = now + cfg.aurora_poll_minutes * 60

    when = datetime.now(timezone.utc)
    if astro.sun_altitude(when, cfg.reference_lat, cfg.reference_lon) > -6.0:
        return  # daylight or twilight: nothing to see even if it's raging

    snap_path = Path(cfg.state_file).with_suffix(".aurora.json")
    try:
        snap = json.loads(snap_path.read_text())
    except (FileNotFoundError, ValueError):
        snap = {}

    status, line = astro.aurora_report(cfg.reference_lat, cfg.reference_lon,
                                       when, when + timedelta(hours=3),
                                       min_status=cfg.aurora_min_status)
    if not line:
        # Only touch the file when the level has actually moved: this
        # runs every quarter of an hour all night, on an SD card.
        if status != snap.get("status"):
            snap_path.write_text(json.dumps({"status": status,
                                             "sent_at": snap.get("sent_at", 0.0)}))
        return

    escalated = astro.aurora_rank(status) > astro.aurora_rank(snap.get("status"))
    if not escalated and now - snap.get("sent_at", 0.0) < cfg.aurora_repeat_hours * 3600:
        return
    irc.send_message(line)
    send_web_alert(cfg, line)
    snap_path.write_text(json.dumps({"status": status, "sent_at": now}))


def maybe_send_morning(cfg, irc):
    """Once a day after morning_time (local), post the day briefing —
    calendar, bin collection, weather (built in astro.py). Same
    snapshot pattern as the digest so restarts don't repeat it."""
    now = datetime.now(astro.UK) if astro.UK else datetime.now().astimezone()
    today = now.date().isoformat()
    if (now.strftime("%H:%M") < cfg.morning_time or not irc.connected
            or getattr(maybe_send_morning, "sent_date", None) == today):
        return
    snap_path = Path(cfg.state_file).with_suffix(".morning.json")
    try:
        if json.loads(snap_path.read_text()).get("date") == today:
            maybe_send_morning.sent_date = today
            return
    except (FileNotFoundError, ValueError):
        pass

    for line in astro.build_morning_report(cfg):
        irc.send_message(line)
        send_web_alert(cfg, line)
        time.sleep(cfg.bot_message_delay)
    snap_path.write_text(json.dumps({"date": today}))
    maybe_send_morning.sent_date = today


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.txt", help="path to config file")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config(args.config)
    if cfg.terrain_grid:
        terrain.load(cfg.terrain_grid)
    state = AlertState(cfg)
    irc = IrcClient(cfg)
    log_db = alertlog.AlertLog(cfg.alert_db)
    receiver = ReceiverHealth(cfg, log_db)
    rechecks = {}
    pending = {}
    squawk_watch = {}  # alerted as (n/a); post a one-liner when the code lands
    tracks = {}      # last online fix per hex, for the lost-contact check
    transits = {}    # high-altitude military seen since the last digest
    orbits = {}      # per-hex turn accumulators for the loiter check
    formations = {}  # military pair persistence counts
    paths = {}       # per-hex course history for the survey check
    local_hexes = set()
    next_local = next_online = next_sonde = next_long_range = 0.0

    if cfg.announce_dir:
        Path(cfg.announce_dir).mkdir(parents=True, exist_ok=True)

    log.info("planespotter %s watching %s for %d watchlist aircraft, alerts to %s on %s",
             __version__, cfg.local_json, len(cfg.watchlist_aircraft), cfg.channel, cfg.server)

    while True:
        irc.ensure_connected()
        irc.poll()

        now = time.time()
        if now >= next_local:
            next_local = now + cfg.poll_seconds
            aircraft, err = fetch_local(cfg)
            receiver.update(aircraft is not None, err, irc)
            if aircraft is not None:
                local_hexes = process_local(aircraft, cfg, irc, state, rechecks,
                                            pending, squawk_watch, orbits, log_db)
            else:
                # Whatever was overhead at the last good poll is long gone.
                # Keeping the set would go on suppressing online alerts for
                # those hexes for as long as the receiver stayed down.
                local_hexes = set()

        if cfg.online_enabled and now >= next_online:
            next_online = now + cfg.online_poll_seconds
            online = fetch_online(cfg)
            if online is not None:
                process_online(online, cfg, irc, state, local_hexes, tracks,
                               transits, formations, paths, log_db)

        if cfg.long_range_enabled and now >= next_long_range:
            next_long_range = now + cfg.long_range_poll_seconds
            far = fetch_online(cfg, radius_nm=cfg.long_range_radius_nm)
            if far is not None:
                process_long_range(far, cfg, transits)

        if cfg.sonde_enabled and now >= next_sonde:
            next_sonde = now + cfg.sonde_poll_seconds
            sondes = fetch_sondes(cfg)
            if sondes is not None:
                process_sondes(sondes, cfg, irc, state)

        if cfg.announce_dir:
            process_announcements(cfg, irc)

        if cfg.digest_enabled:
            maybe_send_digest(cfg, irc, transits, receiver_up=bool(receiver.up))

        if cfg.astro_enabled:
            maybe_send_astro(cfg, irc)

        if cfg.morning_enabled:
            maybe_send_morning(cfg, irc)

        if cfg.aurora_enabled:
            maybe_send_aurora(cfg, irc)

        state.save_if_dirty()
        time.sleep(1)


if __name__ == "__main__":
    main()
