#!/usr/bin/env python3
"""Planespotter: watches a local readsb/tar1090 feed for interesting aircraft
and alerts an IRC channel. Optionally also polls an online aggregator so
aircraft below the local radio horizon (terrain-masked) are reported too,
tagged "(online data)". Stdlib only."""

import argparse
import configparser
import json
import logging
import re
import socket
import time
import urllib.request
from datetime import datetime, timedelta
from math import radians, degrees, sin, cos, asin, sqrt, atan2
from pathlib import Path

import astro

log = logging.getLogger("spotter")

NM_TO_MILES = 1.15078
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
USER_AGENT = "planespotter/1.0 (https://github.com/mkeay/planespotter)"
# Hijack / radio failure / general emergency: always worth saying, however high.
EMERGENCY_SQUAWKS = {"7500", "7600", "7700"}
# ":nick!user@host JOIN :#channel"
JOIN_RE = re.compile(r":([^!\s]+)![^\s]*\s+JOIN\s+:?(\S+)", re.I)
GREET_COOLDOWN = 60  # seconds, so a join/part flood can't turn us into a bot war


# === Configuration ===

def _split(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_greetings(value):
    """Nicks to greet as they join: "nick:message" pairs separated by ";".
    The nick is prefixed to the message when it goes out."""
    greetings = {}
    for part in value.split(";"):
        nick, _, message = part.strip().partition(":")
        nick, message = nick.strip(), message.strip()
        if nick and message:
            greetings[nick.lower()] = message
    return greetings


def _parse_zones(value):
    """Exclusion zones: "lat,lon,radius_nm,below_alt_ft" separated by
    semicolons, e.g. an airport's approach/circuit airspace."""
    zones = []
    for part in value.split(";"):
        part = part.strip()
        if not part:
            continue
        try:
            lat, lon, radius, below = part.split(",")
            zones.append((float(lat), float(lon), float(radius), int(below)))
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

        self.alert_military = d.getboolean("alert_military", True)
        self.alert_mlat = d.getboolean("alert_mlat", True)
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
        self.webapi = d.get("webapi", "") or None

        self.online_enabled = d.getboolean("online_enabled", False)
        self.online_url = d.get("online_url", "https://api.adsb.lol/v2/point/{lat}/{lon}/{radius}")
        self.online_radius_nm = d.getint("online_radius_nm", 40)
        self.online_poll_seconds = d.getint("online_poll_seconds", 60)

        self.announce_dir = d.get("announce_dir", "")

        self.astro_enabled = d.getboolean("astro_enabled", False)
        self.astro_lead_minutes = d.getint("astro_lead_minutes", 60)

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
    """Where the aircraft is actually pointing: its broadcast track, or failing
    that the bearing from a previous fix to this one. MLAT-only contacts -- a
    lot of the interesting military traffic -- frequently have no track."""
    track = ac.get("track")
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
    """Worth an alert at any altitude: a hand-picked airframe, or an
    emergency. Everything else has to be low enough to see or hear."""
    return ((ac.get("hex") or "").lower() in cfg.watchlist_aircraft
            or is_emergency(ac))


def see_hear_worthy(ac, cfg, local=True):
    """Types worth an alert *when they're low enough to notice*. Category and
    MLAT triggers stay local-only: inside the online ring they'd match every
    civilian rotorcraft and MLAT'd Cessna on the periphery."""
    return (squawk_in_watchlist(ac.get("squawk"), cfg.watchlist_squawks)
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
        for zlat, zlon, radius_nm, below_ft in cfg.exclude_zones + cfg.airport_zones
    )


def bound_for_airport(ac, cfg, course):
    """True when the aircraft is descending and lined up on a known airport --
    an arrival somewhere else, not a visitor. Same closest-approach test used
    to decide whether something is coming to us, measured against each airport
    instead. Descent is required: plenty of traffic crosses an airport's
    overhead at cruise on the way past."""
    lat, lon = ac.get("lat"), ac.get("lon")
    if lat is None or lon is None or course is None:
        return False
    climb = ac.get("baro_rate", ac.get("geom_rate"))
    if climb is None or climb > -200:
        return False
    for zlat, zlon, radius_nm, _ in cfg.airport_zones:
        miss, along = closest_approach(zlat, zlon, lat, lon, course)
        if miss is not None and along > 0 and miss <= radius_nm:
            return True
    return False


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

    if ac.get("alt_baro") == "ground":
        alt_part = "\x02\x0308on the ground\x03\x02"
    elif altitude is None:
        alt_part = "at \x02\x0308unknown altitude\x03\x02"
    else:
        alt_part = f"at altitude \x02\x0308{altitude} ft\x03\x02"
        climb = ac.get("baro_rate", ac.get("geom_rate"))
        if climb is not None and abs(climb) >= 100:
            arrow = "↑" if climb > 0 else "↓"
            alt_part += f" {arrow}{abs(climb)} fpm"

    head = f"{kind}!"
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
        message += f" | EMERGENCY: {ac['emergency']}"
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
    if photo and len(message) + len(photo) + 10 <= 430:  # never truncate mid-URL
        message += f" | Photo: {photo}"
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
            self._send_line(f"PRIVMSG {self.cfg.channel} :{text[:430]}")
            log.info("Sent to IRC: %s", text)
            return True
        except OSError as e:
            log.warning("IRC send failed: %s", e)
            self._teardown()
            self.next_attempt = time.time() + self.backoff
            return False


# === Data sources ===

def fetch_local(cfg):
    try:
        data = json.loads(Path(cfg.local_json).read_text())
        return data.get("aircraft", [])
    except (OSError, ValueError) as e:
        log.warning("Could not read %s: %s", cfg.local_json, e)
        return None


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

def dispatch(irc, cfg, ac, kind="Alert", tag=None, extra=None):
    photo = None
    if cfg.photo_links and ac.get("hex"):
        photo = fetch_photo_link(ac["hex"].lower())
    message = format_alert(ac, cfg, kind=kind, tag=tag, photo=photo, extra=extra)
    irc.send_message(message)
    send_web_alert(cfg, message)
    time.sleep(cfg.bot_message_delay)


def process_local(aircraft, cfg, irc, state, rechecks, pending):
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

        if not ((meets_criteria(ac, cfg) or cfg.verbose) and state.should_alert(icao)):
            continue
        if in_excluded_zone(ac, cfg) and not is_emergency(ac):
            continue
        # Interesting hexes often appear in aircraft.json well before
        # anything decodes; hold the alert until there's a position or an
        # altitude — however long that takes. A timeout here would race
        # the decode and mute late-arriving data for the whole interval.
        has_data = ac.get("lat") is not None or ac.get("alt_baro") is not None
        if not has_data and not is_emergency(ac):
            pending.setdefault(icao, time.time())
            continue
        pending.pop(icao, None)
        state.mark(icao)
        dispatch(irc, cfg, ac, kind="Alert")
        # Data often trickles in; if position or speed was missing,
        # schedule one follow-up look.
        if ac.get("lat") is None or ac.get("gs") is None:
            rechecks.setdefault(icao, time.time() + cfg.recheck_seconds)

    # Forget pending holds for aircraft no longer in the feed.
    for icao in list(pending):
        if icao not in by_hex:
            log.info("Dropped dataless alert for %s (held %.0fs)",
                     icao, time.time() - pending[icao])
            del pending[icao]

    # Follow-ups: send an UPDATE once the missing data has arrived.
    for icao in [k for k, due in rechecks.items() if time.time() >= due]:
        ac = by_hex.get(icao)
        if ac is not None and ac.get("lat") is not None and ac.get("gs") is not None:
            dispatch(irc, cfg, ac, kind="UPDATE")
            del rechecks[icao]
        elif time.time() >= rechecks[icao] + 4 * cfg.recheck_seconds:
            del rechecks[icao]  # give up, aircraft gone or data never arrived

    return seen_hexes


def process_online(aircraft, cfg, irc, state, local_hexes, tracks, transits):
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
    for ac in aircraft:
        icao = (ac.get("hex") or "").lower()
        if not icao or icao.startswith("~"):
            continue
        altitude = parse_altitude(ac.get("alt_baro"))
        lat, lon = ac.get("lat"), ac.get("lon")

        # Too high to see or hear: counted for the digest, never announced.
        if (altitude is not None and altitude > cfg.alert_ceiling_ft
                and not always_alert(ac, cfg)):
            if is_military(ac):
                note_high_transit(transits, ac)
            tracks.pop(icao, None)
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
        elsewhere = bound_for_airport(ac, cfg, course) or in_excluded_zone(ac, cfg)
        tracks[icao] = {"time": now, "lat": lat, "lon": lon, "alt": altitude,
                        "dist": dist_nm, "inbound": inbound, "elsewhere": elsewhere,
                        "ac": ac}

        if icao in local_hexes:
            continue
        if not (meets_criteria(ac, cfg, local=False) and state.should_alert(icao + ":online")):
            continue
        if not state.should_alert(icao):  # confirmed locally recently? nothing new to say.
            continue
        if not is_emergency(ac):
            if elsewhere:
                continue
            # An unknown course gets the benefit of the doubt -- the next
            # sweep gives us a second fix to derive one from.
            if course is not None and not inbound:
                continue
        state.mark(icao + ":online")
        dispatch(irc, cfg, ac, kind="Alert", tag="online data")

    check_lost_contact(cfg, irc, state, tracks, seen, local_hexes)


def check_lost_contact(cfg, irc, state, tracks, seen, local_hexes):
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
        dispatch(irc, cfg, ac, kind="Lost contact", extra=extra)


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


def maybe_send_digest(cfg, irc, transits):
    """Once a day after digest_time, post a receiver stats summary to IRC.
    Counter deltas come from a snapshot of readsb's since-start totals, and
    the high-altitude military tally covers everything since the last digest
    -- the traffic that's deliberately never alerted on."""
    now = datetime.now()
    today = now.date().isoformat()
    if (now.strftime("%H:%M") < cfg.digest_time or not irc.connected
            or getattr(maybe_send_digest, "sent_date", None) == today):
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
    tonight's astronomy outlook and eve-of-collection bin reminder
    (built in astro.py). Same snapshot pattern as the digest so
    restarts don't repeat it."""
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


def maybe_send_morning(cfg, irc):
    """Once a day after morning_time (local), post the day briefing —
    calendar, bin collection, weather, ISS passes (built in astro.py).
    Same snapshot pattern as the digest so restarts don't repeat it."""
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
    state = AlertState(cfg)
    irc = IrcClient(cfg)
    rechecks = {}
    pending = {}
    tracks = {}      # last online fix per hex, for the lost-contact check
    transits = {}    # high-altitude military seen since the last digest
    local_hexes = set()
    next_local = next_online = next_sonde = next_long_range = 0.0

    if cfg.announce_dir:
        Path(cfg.announce_dir).mkdir(parents=True, exist_ok=True)

    log.info("Watching %s for %d watchlist aircraft, alerts to %s on %s",
             cfg.local_json, len(cfg.watchlist_aircraft), cfg.channel, cfg.server)

    while True:
        irc.ensure_connected()
        irc.poll()

        now = time.time()
        if now >= next_local:
            next_local = now + cfg.poll_seconds
            aircraft = fetch_local(cfg)
            if aircraft is not None:
                local_hexes = process_local(aircraft, cfg, irc, state, rechecks, pending)

        if cfg.online_enabled and now >= next_online:
            next_online = now + cfg.online_poll_seconds
            online = fetch_online(cfg)
            if online is not None:
                process_online(online, cfg, irc, state, local_hexes, tracks, transits)

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
            maybe_send_digest(cfg, irc, transits)

        if cfg.astro_enabled:
            maybe_send_astro(cfg, irc)

        if cfg.morning_enabled:
            maybe_send_morning(cfg, irc)

        state.save_if_dirty()
        time.sleep(1)


if __name__ == "__main__":
    main()
