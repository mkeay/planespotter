#!/usr/bin/env python3
"""Astronomy almanac for the norris platform: sunrise/sunset and moon
phase computed locally, tonight's observing conditions from 7Timer!'s
ASTRO product and Open-Meteo (the same model data behind sites like
clearoutside.com), a calendar of saints' days and UK national /
international observances, and a morning briefing: today's calendar,
bin collection (council iCal feed), day weather and likely-visible ISS
passes. Stdlib only."""

import json
import logging
import re
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from math import sin, cos, acos, asin, atan2, radians, degrees, pi

try:
    from zoneinfo import ZoneInfo
    UK = ZoneInfo("Europe/London")  # the Pi's clock runs UTC; report in local time
except Exception:
    UK = None

log = logging.getLogger("astro")

USER_AGENT = "planespotter/1.0 (https://github.com/mkeay/planespotter)"


# === Sun and moon (computed, no network) ===

def _from_julian(jd):
    return datetime.fromtimestamp((jd - 2440587.5) * 86400.0, tz=timezone.utc)


def sun_times(day, lat, lon):
    """Sunrise and sunset (aware UTC datetimes) for the given date at
    lat/lon in degrees, longitude east-positive. Wikipedia's sunrise
    equation — good to a minute or two, plenty for scheduling. Returns
    (None, None) during polar day/night."""
    n = day.toordinal() - date(2000, 1, 1).toordinal()  # days since J2000
    j_star = n + 0.0008 - lon / 360.0                   # mean solar noon
    m = radians((357.5291 + 0.98560028 * j_star) % 360.0)
    c = 1.9148 * sin(m) + 0.0200 * sin(2 * m) + 0.0003 * sin(3 * m)
    lam = radians((degrees(m) + c + 180.0 + 102.9372) % 360.0)
    j_transit = 2451545.0 + j_star + 0.0053 * sin(m) - 0.0069 * sin(2 * lam)
    decl = asin(sin(lam) * sin(radians(23.4397)))
    cos_h = ((sin(radians(-0.833)) - sin(radians(lat)) * sin(decl))
             / (cos(radians(lat)) * cos(decl)))
    if not -1.0 <= cos_h <= 1.0:
        return None, None
    h = degrees(acos(cos_h)) / 360.0
    return _from_julian(j_transit - h), _from_julian(j_transit + h)


def sun_altitude(when, lat, lon):
    """Sun's altitude in degrees at an aware datetime — low-precision
    solar position (Meeus-style, good to ~0.01°), used to judge whether
    a satellite pass happens in a dark-enough sky."""
    n = ((when - datetime(2000, 1, 1, 12, tzinfo=timezone.utc)).total_seconds()
         / 86400.0)
    mean_lon = radians((280.460 + 0.9856474 * n) % 360.0)
    m = radians((357.528 + 0.9856003 * n) % 360.0)
    lam = mean_lon + radians(1.915) * sin(m) + radians(0.020) * sin(2 * m)
    eps = radians(23.439 - 4e-7 * n)
    ra = atan2(cos(eps) * sin(lam), cos(lam))
    dec = asin(sin(eps) * sin(lam))
    gmst = radians((280.46061837 + 360.98564736629 * n) % 360.0)
    hour_angle = gmst + radians(lon) - ra
    return degrees(asin(sin(radians(lat)) * sin(dec)
                        + cos(radians(lat)) * cos(dec) * cos(hour_angle)))


SYNODIC_DAYS = 29.530588853
_NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)


def moon_phase(when):
    """(phase name, % illuminated, days until next full moon), from the
    mean synodic cycle — accurate to a few hours, fine for a nightly
    report."""
    age = ((when - _NEW_MOON).total_seconds() / 86400.0) % SYNODIC_DAYS
    illum = (1.0 - cos(2.0 * pi * age / SYNODIC_DAYS)) / 2.0 * 100.0
    to_full = (SYNODIC_DAYS / 2.0 - age) % SYNODIC_DAYS
    names = ["New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
             "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent"]
    slot = int(((age + SYNODIC_DAYS / 16.0) % SYNODIC_DAYS) / (SYNODIC_DAYS / 8.0))
    return names[slot], illum, to_full


# === Forecast sources ===

def _get_json(url, tries=3):
    """GET with a couple of retries: these are once-a-day fetches, and a
    transient 503 shouldn't cost the whole day's forecast."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except OSError as e:
            if attempt == tries - 1:
                raise
            log.warning("Fetch failed (%s), retrying: %s", e, url)
            time.sleep(10 * (attempt + 1))


def fetch_clearoutside(lat, lon, night_start, night_end):
    """Clear Outside (clearoutside.com) 7-day forecast, the preferred
    source — no API, so scraped from the page (one hit a day). Returns
    the same overnight summary shape as fetch_night_weather plus their
    explicit frost flag, darkness times and a best observing window
    from their hourly Good/OK/Bad ratings. None if unreachable."""
    url = f"https://clearoutside.com/forecast/{lat:.2f}/{lon:.2f}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        return _parse_clearoutside(html, night_start, night_end)
    except (OSError, ValueError) as e:
        log.warning("Clear Outside fetch failed: %s", e)
        return None


def _co_cells(block, label):
    """The <li> cells of one labelled forecast row as (title, text)."""
    m = re.search(r'<span class="fc_detail_label"><span>' + re.escape(label)
                  + r'.*?<ul>(.*?)</ul>', block, re.S)
    if not m:
        return []
    return [(re.search(r'title="([^"]*)"', li).group(1) if 'title="' in li else "",
             re.sub(r"<[^>]+>", "", txt).strip())
            for li, txt in re.findall(r"<li([^>]*)>(.*?)</li>", m.group(1), re.S)]


def _parse_clearoutside(html, night_start, night_end):
    tz = UK or datetime.now().astimezone().tzinfo
    today = datetime.now(tz).date()
    blocks = re.split(r'<div class="fc_day" id="day_\d+">', html)[1:3]
    if not blocks:
        return None
    m = re.search(r"(\d+)", re.search(r'class="fc_day_date"[^>]*>(.*?)</div>',
                                      blocks[0], re.S).group(1).replace("<span>", " "))
    dom = int(m.group(1))
    day = next((today + timedelta(days=off) for off in (0, -1, 1)
                if (today + timedelta(days=off)).day == dom), today)

    hourly = {}  # aware UTC datetime -> per-hour dict
    for block in blocks:
        hours = [int(h) for h in
                 re.findall(r'glyphicon-time"></span>\s*(\d+)', block)]
        ratings = re.findall(r'<li class="fc_(good|ok|bad)"><span class="glyphicon',
                             block)
        rows = {key: _co_cells(block, label) for key, label in (
            ("cloud", "Total Clouds"), ("ptype", "Precipitation Type"),
            ("pprob", "Precipitation Probability"), ("pamount", "Precipitation Amount"),
            ("frost", "Chance of Frost"), ("temp", "Temperature"))}
        prev = None
        for i, hour in enumerate(hours):
            if prev is not None and hour < prev:
                day += timedelta(days=1)
            prev = hour
            when = datetime(day.year, day.month, day.day, hour, tzinfo=tz)
            entry = {"rating": ratings[i] if i < len(ratings) else ""}
            for key, cells in rows.items():
                if i < len(cells):
                    entry[key] = cells[i]
            hourly.setdefault(when.astimezone(timezone.utc), entry)
        day += timedelta(days=1)  # next block starts at 00:00 the following day

    def _num(cell):
        try:
            return float(cell[1])
        except (ValueError, TypeError, IndexError):
            return None

    clouds, temps, precip, ratings = [], [], [], []
    snow_mm, frost = 0.0, False
    for when in sorted(hourly):
        if not night_start <= when <= night_end:
            continue
        e = hourly[when]
        for target, key in ((clouds, "cloud"), (temps, "temp"), (precip, "pprob")):
            value = _num(e.get(key))
            if value is not None:
                target.append(value)
        title = (e.get("frost") or ("", ""))[0].lower()
        if title and "no frost" not in title:
            frost = True
        if "snow" in (e.get("ptype") or ("", ""))[0].lower():
            snow_mm += _num(e.get("pamount")) or 0.0
        ratings.append((when, e["rating"]))
    if not clouds or not temps:
        return None

    best = None  # longest contiguous run of their best rating tonight
    for want in ("good", "ok"):
        run, best_run = [], []
        for when, rating in ratings:
            run = run + [when] if rating == want else []
            if len(run) > len(best_run):
                best_run = list(run)
        if len(best_run) >= 2:
            best = (want, best_run[0],
                    min(best_run[-1] + timedelta(hours=1), night_end))
            break

    dark = None
    header = re.search(r"Civil Dark:.*?Astro Dark: [^\"<]*", blocks[0])
    if header:
        for kind in ("Astro", "Nautical", "Civil"):
            m = re.search(kind + r" Dark: (\d\d:\d\d) - (\d\d:\d\d)", header.group(0))
            if m:
                dark = (kind.lower(), m.group(1), m.group(2))
                break

    return {"cloud": sum(clouds) / len(clouds), "min_temp": min(temps),
            "snow_cm": snow_mm,  # ~1 mm liquid ≈ 1 cm of snow
            "precip": sum(precip) / len(precip) if precip else 0.0,
            "frost": frost, "dark": dark, "best": best, "source": "Clear Outside"}


def fetch_night_weather(lat, lon, night_start, night_end):
    """Open-Meteo hourly forecast reduced to tonight's window: mean cloud
    cover %, minimum temperature °C, total snowfall cm and mean
    precipitation probability %. None if unavailable."""
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&hourly=cloud_cover,temperature_2m,snowfall,precipitation_probability"
           "&forecast_days=2&timezone=UTC")
    try:
        hourly = _get_json(url)["hourly"]
        rows = zip(hourly["time"], hourly["cloud_cover"], hourly["temperature_2m"],
                   hourly["snowfall"], hourly["precipitation_probability"])
    except (OSError, ValueError, KeyError) as e:
        log.warning("Open-Meteo fetch failed: %s", e)
        return None
    clouds, temps, snow, precip = [], [], [], []
    for ts, cloud, temp, snowfall, prob in rows:
        when = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        if night_start <= when <= night_end:
            if cloud is not None:
                clouds.append(cloud)
            if temp is not None:
                temps.append(temp)
            if snowfall is not None:
                snow.append(snowfall)
            if prob is not None:
                precip.append(prob)
    if not temps or not clouds:
        return None
    return {"cloud": sum(clouds) / len(clouds),
            "min_temp": min(temps),
            "snow_cm": sum(snow),
            "precip": sum(precip) / len(precip) if precip else 0.0}


def fetch_seeing(lat, lon, night_start, night_end):
    """7Timer! ASTRO product: astronomical seeing and transparency on
    1 (best) to 8 (worst) scales in 3-hour blocks; averaged over the
    blocks that overlap tonight. None if unavailable."""
    url = ("https://www.7timer.info/bin/astro.php"
           f"?lon={lon}&lat={lat}&ac=0&unit=metric&output=json")
    try:
        data = _get_json(url)
        init = datetime.strptime(data["init"], "%Y%m%d%H").replace(tzinfo=timezone.utc)
        seeing, transp = [], []
        for block in data["dataseries"]:
            when = init + timedelta(hours=block["timepoint"])
            if night_start - timedelta(hours=3) <= when <= night_end:
                seeing.append(block["seeing"])
                transp.append(block["transparency"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("7Timer fetch failed: %s", e)
        return None
    if not seeing:
        return None
    return {"seeing": sum(seeing) / len(seeing),
            "transparency": sum(transp) / len(transp)}


def _scale_word(value):
    """Describe a 7Timer 1 (best) to 8 (worst) scale value."""
    for limit, word in ((1.5, "excellent"), (2.5, "good"), (4.5, "average"),
                        (6.5, "poor")):
        if value <= limit:
            return word
    return "terrible"


def sky_score(weather, seeing):
    """0-100 "ideal astronomy weather" score, 100 = clear, dry, steady
    skies. Cloud cover dominates (70%), seeing and transparency refine
    it (15% each), and precipitation risk knocks a little more off."""
    clear = 100.0 - weather["cloud"] if weather else None
    if seeing:
        s = (8.0 - seeing["seeing"]) / 7.0 * 100.0
        t = (8.0 - seeing["transparency"]) / 7.0 * 100.0
        score = 0.7 * clear + 0.15 * s + 0.15 * t if clear is not None else (s + t) / 2.0
    elif clear is not None:
        score = clear
    else:
        return None
    if weather:
        score -= 0.2 * weather["precip"]
    return max(0, min(100, round(score)))


# === Calendar ===

def easter(year):
    """Easter Sunday (Gregorian, anonymous computus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def observances(day):
    """Saints' days plus UK national and international days for a date:
    the fixed calendar below, Easter-linked movable feasts, and a couple
    of nth-Sunday observances."""
    found = list(_FIXED_DAYS.get(f"{day.month:02d}-{day.day:02d}", ()))
    movable = {-47: "Shrove Tuesday (Pancake Day)", -46: "Ash Wednesday",
               -21: "Mothering Sunday", -7: "Palm Sunday",
               -3: "Maundy Thursday", -2: "Good Friday", 0: "Easter Sunday",
               1: "Easter Monday", 39: "Ascension Day",
               49: "Pentecost (Whit Sunday)"}
    delta = (day - easter(day.year)).days
    if delta in movable:
        found.insert(0, movable[delta])
    if day.month == 6 and day.weekday() == 6 and 15 <= day.day <= 21:
        found.append("Father's Day")
    if day.month == 11 and day.weekday() == 6 and 8 <= day.day <= 14:
        found.append("Remembrance Sunday")
    return found


_FIXED_DAYS = {
    "01-01": ("New Year's Day",),
    "01-02": ("St Basil the Great & St Gregory Nazianzen",),
    "01-05": ("Twelfth Night",),
    "01-06": ("Epiphany",),
    "01-13": ("St Hilary of Poitiers",),
    "01-17": ("St Anthony of Egypt",),
    "01-20": ("St Fabian & St Sebastian",),
    "01-21": ("St Agnes",),
    "01-24": ("St Francis de Sales",),
    "01-25": ("Burns Night", "Conversion of St Paul"),
    "01-27": ("Holocaust Memorial Day",),
    "01-28": ("St Thomas Aquinas",),
    "01-31": ("St John Bosco",),
    "02-01": ("St Brigid of Kildare",),
    "02-02": ("Candlemas", "World Wetlands Day"),
    "02-03": ("St Blaise",),
    "02-04": ("World Cancer Day",),
    "02-05": ("St Agatha",),
    "02-10": ("St Scholastica",),
    "02-11": ("International Day of Women and Girls in Science",),
    "02-14": ("St Valentine's Day",),
    "02-21": ("International Mother Language Day",),
    "02-23": ("St Polycarp",),
    "03-01": ("St David's Day (Wales)",),
    "03-02": ("St Chad of Lichfield",),
    "03-03": ("World Wildlife Day",),
    "03-05": ("St Piran's Day (Cornwall)",),
    "03-07": ("St Perpetua & St Felicity",),
    "03-08": ("International Women's Day",),
    "03-14": ("Pi Day",),
    "03-17": ("St Patrick's Day (Ireland)",),
    "03-19": ("St Joseph",),
    "03-20": ("International Day of Happiness",),
    "03-21": ("World Poetry Day",),
    "03-22": ("World Water Day",),
    "03-23": ("World Meteorological Day",),
    "03-25": ("Lady Day (the Annunciation)",),
    "03-27": ("World Theatre Day",),
    "04-01": ("April Fools' Day",),
    "04-02": ("World Autism Awareness Day",),
    "04-07": ("World Health Day",),
    "04-11": ("St Stanislaus",),
    "04-12": ("International Day of Human Space Flight (Yuri's Night)",),
    "04-21": ("St Anselm of Canterbury",),
    "04-22": ("Earth Day",),
    "04-23": ("St George's Day (England)", "World Book Day"),
    "04-25": ("St Mark", "ANZAC Day"),
    "04-29": ("St Catherine of Siena",),
    "04-30": ("International Jazz Day",),
    "05-01": ("May Day", "St Philip & St James"),
    "05-02": ("St Athanasius",),
    "05-03": ("World Press Freedom Day",),
    "05-04": ("Star Wars Day",),
    "05-08": ("VE Day", "Julian of Norwich"),
    "05-12": ("International Nurses Day",),
    "05-14": ("St Matthias",),
    "05-15": ("International Day of Families",),
    "05-18": ("International Museum Day",),
    "05-20": ("World Bee Day",),
    "05-22": ("International Day for Biological Diversity",),
    "05-25": ("St Bede the Venerable", "Towel Day"),
    "05-26": ("St Augustine of Canterbury",),
    "05-30": ("St Joan of Arc",),
    "05-31": ("World No Tobacco Day",),
    "06-01": ("St Justin Martyr", "Global Day of Parents"),
    "06-05": ("St Boniface", "World Environment Day"),
    "06-08": ("World Oceans Day",),
    "06-09": ("St Columba of Iona",),
    "06-11": ("St Barnabas",),
    "06-13": ("St Anthony of Padua",),
    "06-14": ("World Blood Donor Day",),
    "06-20": ("World Refugee Day",),
    "06-21": ("International Day of Yoga",),
    "06-22": ("St Alban (first British martyr)",),
    "06-24": ("Midsummer Day (Nativity of St John the Baptist)",),
    "06-29": ("St Peter & St Paul",),
    "07-03": ("St Thomas the Apostle",),
    "07-11": ("St Benedict", "World Population Day"),
    "07-15": ("St Swithin's Day",),
    "07-18": ("Nelson Mandela International Day",),
    "07-20": ("St Margaret of Antioch", "International Chess Day",
              "Apollo 11 Moon landing anniversary"),
    "07-22": ("St Mary Magdalene",),
    "07-25": ("St James the Great", "St Christopher"),
    "07-26": ("St Joachim & St Anne",),
    "07-29": ("St Martha", "International Day of Friendship"),
    "07-31": ("St Ignatius of Loyola",),
    "08-01": ("Lammas", "Yorkshire Day"),
    "08-06": ("The Transfiguration",),
    "08-09": ("International Day of the World's Indigenous Peoples",),
    "08-10": ("St Lawrence (Perseid season)",),
    "08-11": ("St Clare of Assisi",),
    "08-12": ("International Youth Day", "the Glorious Twelfth"),
    "08-15": ("The Assumption",),
    "08-19": ("World Humanitarian Day", "World Photography Day"),
    "08-20": ("St Bernard of Clairvaux",),
    "08-24": ("St Bartholomew",),
    "08-27": ("St Monica",),
    "08-28": ("St Augustine of Hippo",),
    "08-31": ("St Aidan of Lindisfarne",),
    "09-01": ("St Giles",),
    "09-03": ("St Gregory the Great",),
    "09-05": ("International Day of Charity",),
    "09-08": ("International Literacy Day",),
    "09-13": ("St John Chrysostom",),
    "09-14": ("Holy Cross Day",),
    "09-15": ("International Day of Democracy",),
    "09-16": ("St Ninian of Whithorn", "International Ozone Layer Day"),
    "09-17": ("St Hildegard of Bingen",),
    "09-21": ("St Matthew", "International Day of Peace"),
    "09-26": ("European Day of Languages",),
    "09-27": ("St Vincent de Paul", "World Tourism Day"),
    "09-29": ("Michaelmas (St Michael & All Angels)",),
    "09-30": ("St Jerome",),
    "10-01": ("St Thérèse of Lisieux", "International Day of Older Persons"),
    "10-02": ("Guardian Angels", "International Day of Non-Violence"),
    "10-04": ("St Francis of Assisi", "World Animal Day", "World Space Week begins"),
    "10-05": ("World Teachers' Day",),
    "10-10": ("World Mental Health Day",),
    "10-13": ("St Edward the Confessor",),
    "10-15": ("St Teresa of Ávila",),
    "10-16": ("World Food Day",),
    "10-17": ("St Ignatius of Antioch",),
    "10-18": ("St Luke",),
    "10-24": ("United Nations Day",),
    "10-25": ("St Crispin's Day",),
    "10-28": ("St Simon & St Jude",),
    "10-31": ("Halloween (All Hallows' Eve)",),
    "11-01": ("All Saints' Day",),
    "11-02": ("All Souls' Day",),
    "11-05": ("Guy Fawkes Night",),
    "11-10": ("St Leo the Great",),
    "11-11": ("Armistice Day", "Martinmas (St Martin of Tours)"),
    "11-13": ("World Kindness Day",),
    "11-14": ("World Diabetes Day",),
    "11-16": ("St Margaret of Scotland", "International Day for Tolerance"),
    "11-17": ("St Hugh of Lincoln",),
    "11-19": ("International Men's Day",),
    "11-20": ("St Edmund", "World Children's Day"),
    "11-22": ("St Cecilia",),
    "11-23": ("St Clement",),
    "11-25": ("St Catherine of Alexandria",),
    "11-30": ("St Andrew's Day (Scotland)",),
    "12-01": ("World AIDS Day",),
    "12-03": ("International Day of Persons with Disabilities",),
    "12-04": ("St Barbara",),
    "12-06": ("St Nicholas",),
    "12-07": ("St Ambrose",),
    "12-08": ("Immaculate Conception",),
    "12-10": ("Human Rights Day",),
    "12-13": ("St Lucy",),
    "12-14": ("St John of the Cross",),
    "12-24": ("Christmas Eve",),
    "12-25": ("Christmas Day",),
    "12-26": ("St Stephen (Boxing Day)",),
    "12-27": ("St John the Evangelist",),
    "12-28": ("Holy Innocents",),
    "12-29": ("St Thomas Becket",),
    "12-31": ("Hogmanay (St Sylvester)",),
}


# === Morning briefing sources ===

_WMO_CODES = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "torrential showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail",
    99: "thunderstorms with heavy hail",
}


def fetch_day_weather(lat, lon):
    """Open-Meteo daily summary for today (local time): conditions in
    words, min/max °C, peak precipitation probability, total fall and
    max wind. None if unavailable."""
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
           "precipitation_probability_max,precipitation_sum,wind_speed_10m_max"
           "&wind_speed_unit=mph&timezone=Europe%2FLondon&forecast_days=1")
    try:
        daily = _get_json(url)["daily"]
        summary = {"words": _WMO_CODES.get(daily["weather_code"][0], "unknown"),
                   "t_max": daily["temperature_2m_max"][0],
                   "t_min": daily["temperature_2m_min"][0],
                   "precip": daily["precipitation_probability_max"][0],
                   "fall_mm": daily["precipitation_sum"][0],
                   "wind_mph": daily["wind_speed_10m_max"][0]}
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        log.warning("Open-Meteo daily fetch failed: %s", e)
        return None
    if summary["t_max"] is None or summary["t_min"] is None:
        return None
    return summary


def fetch_bin_collection(url, day):
    """The given date's entry from a council iCal bin-collection feed
    (e.g. D&G's waste-collection-schedule download link). Returns the
    event summary ("Grey lidded bins for Non-recyclable waste"), "" if
    no collection that day, or None if the feed is unreachable."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except OSError as e:
        log.warning("Bin schedule fetch failed: %s", e)
        return None
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold folded iCal lines
    summary, wanted_day = "", False
    for line in text.splitlines():
        if line.startswith("BEGIN:VEVENT"):
            summary, wanted_day = "", False
        elif line.startswith("SUMMARY:"):
            summary = line[len("SUMMARY:"):].replace("\\,", ",").strip()
        elif line.startswith("DTSTART"):
            m = re.search(r":(\d{4})(\d{2})(\d{2})", line)
            wanted_day = bool(m) and date(*map(int, m.groups())) == day
        elif line.startswith("END:VEVENT") and wanted_day and summary:
            return summary
    return ""


# Depression of the sun below the observer's horizon beyond which the
# ISS (~420 km up) is inside the Earth's shadow, so no longer visible.
_ISS_SHADOW_DEG = degrees(acos(6371.0 / (6371.0 + 420.0)))

_COMPASS8 = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def fetch_iss_passes(lat, lon, min_elevation=10):
    """Likely-visible ISS passes in the next 24 h: pass predictions from
    api.g7vrd.co.uk (TLE-based), kept when the sky is dark (sun below
    -6°) but the station is still sunlit (sun above the shadow limit
    for its orbit height) at closest approach. None if the API is
    unreachable, [] if there are simply no visible passes."""
    url = (f"https://api.g7vrd.co.uk/v1/satellite-passes/25544/{lat}/{lon}.json"
           f"?hours=24&min_elevation={min_elevation}")
    try:
        passes = _get_json(url).get("passes", [])
    except (OSError, ValueError) as e:
        log.warning("ISS pass fetch failed: %s", e)
        return None
    visible = []
    for p in passes:
        try:
            tca = datetime.fromisoformat(p["tca"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if -_ISS_SHADOW_DEG <= sun_altitude(tca, lat, lon) <= -6.0:
            visible.append({"tca": tca,
                            "max_el": p.get("max_elevation", 0),
                            "dir": _COMPASS8[round(p.get("aos_azimuth", 0)
                                                   / 45.0) % 8]})
    return visible


# === Report ===

def _clock(dt):
    return dt.astimezone(UK).strftime("%H:%M") if UK else dt.astimezone().strftime("%H:%M")


def build_report(cfg):
    """Tonight's report as a list of IRC-ready lines: sun and moon
    computed locally, forecast parts degrading gracefully when a
    service is unreachable, and a bins-out reminder on the eve of a
    collection day."""
    lat, lon = cfg.reference_lat, cfg.reference_lon
    now = datetime.now(timezone.utc)
    today = now.astimezone(UK).date() if UK else now.astimezone().date()
    _, sunset = sun_times(today, lat, lon)
    sunrise, _ = sun_times(today + timedelta(days=1), lat, lon)
    night_start = sunset or now
    night_end = sunrise or now + timedelta(hours=12)

    parts = ["Astronomy tonight: sunset \x02\x0308"
             + (_clock(sunset) if sunset else "n/a")
             + "\x03\x02, sunrise \x02\x0308"
             + (_clock(sunrise) if sunrise else "n/a") + "\x03\x02"]

    name, illum, to_full = moon_phase(night_start)
    moon = f"Moon: {name}, {illum:.0f}% lit"
    if round(to_full) == 0 or name == "Full Moon":
        moon += " — \x02FULL MOON tonight\x02"
    else:
        days = round(to_full)
        moon += f", full in {days} day{'s' if days != 1 else ''}"
    parts.append(moon)

    weather = (fetch_clearoutside(lat, lon, night_start, night_end)
               or fetch_night_weather(lat, lon, night_start, night_end))
    seeing = fetch_seeing(lat, lon, night_start, night_end)
    if weather and weather.get("dark"):
        kind, start, end = weather["dark"]
        parts.append(f"{kind} dark {start}–{end}")
    if seeing:
        parts.append(f"Seeing: {_scale_word(seeing['seeing'])}, "
                     f"transparency: {_scale_word(seeing['transparency'])}")
    score = sky_score(weather, seeing)
    if score is not None:
        colour = "03" if score >= 70 else "08" if score >= 40 else "04"
        parts.append(f"Sky score: \x02\x03{colour}{score}%\x03\x02")
    else:
        parts.append("forecast unavailable")
    if weather and weather.get("best"):
        rating, start, end = weather["best"]
        parts.append(f"best window {_clock(start)}–{_clock(end)} ({rating})")
    if weather and (weather.get("frost") or weather["min_temp"] <= 0):
        parts.append(f"\x02\x0311FROST\x03\x02 overnight (min {weather['min_temp']:.0f}°C)")
    if weather and weather["snow_cm"] >= 0.1:
        parts.append(f"\x02\x0300SNOW\x03\x02 expected ({weather['snow_cm']:.1f} cm)")

    lines = [" | ".join(parts)]
    bins = bin_line(cfg.bins_ical_url, today + timedelta(days=1),
                    "Bins out tonight")
    if bins:
        lines.append(bins)
    return lines


_BIN_COLOURS = {"grey": "14", "blue": "12", "red": "04",
                "brown": "05", "green": "03", "purple": "06"}


def bin_line(url, day, prefix):
    """IRC line for the bin collection on `day`, or None when there is
    no collection that day — and, deliberately, also None when the feed
    is down (the fetch logs it): bin lines only ever appear when a
    collection is actually due."""
    if not url:
        return None
    collection = fetch_bin_collection(url, day)
    if not collection:
        return None
    m = re.match(r"(\w+)(?: lidded)? bins? for (.+)", collection, re.I)
    if not m:
        return f"{prefix}: {collection}"
    colour, what = m.groups()
    code = _BIN_COLOURS.get(colour.lower())
    shown = (f"\x02\x03{code}{colour} bin\x03\x02" if code
             else f"\x02{colour} bin\x02")
    return f"{prefix}: {shown} — {what}"


def build_morning_report(cfg):
    """The 6am day briefing as IRC-ready lines: today's calendar, bin
    collection (kind only — never the address), the day's weather and
    tonight's likely-visible ISS passes. Each network source degrades
    to an "unavailable" note on its own."""
    lat, lon = cfg.reference_lat, cfg.reference_lon
    tz = UK or datetime.now().astimezone().tzinfo
    today = datetime.now(tz).date()

    parts = [f"Good morning — {today:%A} {today.day} {today:%B}"]
    days = observances(today)
    if days:
        parts.append("Today: " + "; ".join(days))
    lines = [" | ".join(parts)]

    bins = bin_line(cfg.bins_ical_url, today, "Bin day")
    if bins:
        lines.append(bins)

    weather = fetch_day_weather(lat, lon)
    if weather:
        line = (f"Weather today: {weather['words']}, "
                f"{weather['t_min']:.0f}–{weather['t_max']:.0f}°C")
        if weather["precip"] is not None:
            line += f", precip {weather['precip']:.0f}%"
            if weather["fall_mm"]:
                line += f" ({weather['fall_mm']:.1f} mm)"
        if weather["wind_mph"] is not None:
            line += f", wind up to {weather['wind_mph']:.0f} mph"
        lines.append(line)
    else:
        lines.append("Weather forecast unavailable")

    passes = fetch_iss_passes(lat, lon, cfg.iss_min_elevation)
    if passes is None:
        lines.append("ISS pass forecast unavailable")
    elif not passes:
        lines.append("ISS: no visible passes in the next 24 h")
    else:
        times = ", ".join(f"{_clock(p['tca'])} (max {p['max_el']:.0f}°, "
                          f"from {p['dir']})" for p in passes)
        n = len(passes)
        lines.append(f"ISS: \x02{n}\x02 likely visible "
                     f"pass{'es' if n != 1 else ''} tonight: {times}")
    return lines
