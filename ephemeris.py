#!/usr/bin/env python3
"""Positions of the moon and planets, eclipse circumstances and a
showpiece deep-sky catalogue -- everything computed locally, stdlib only,
no network and no ephemeris files.

The orbital elements and perturbation terms are Paul Schlyter's low
precision set ("How to compute planetary positions"): about 1-2 arcmin on
the planets and the moon, which is far finer than "is it up, how high,
and roughly how bright". Eclipse circumstances come out of the same
positions, so times are good to a few minutes -- enough to say "there's a
partial lunar eclipse tonight and the moon is up for it", not enough to
time contacts to the second."""

from datetime import timedelta
from math import (sin, cos, tan, asin, acos, atan2, sqrt, radians, degrees,
                  hypot, log10)

# Schlyter's day number: days from 2000 Jan 0.0 UT (= 1999-12-31 00:00 UT).
_SCHLYTER_EPOCH_JD = 2451543.5
_J2000_JD = 2451545.0
_UNIX_EPOCH_JD = 2440587.5
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


# === Frames and angles ===

def _jd(when):
    return when.timestamp() / 86400.0 + _UNIX_EPOCH_JD


def _day(when):
    """Schlyter day number, the argument of every element below."""
    return _jd(when) - _SCHLYTER_EPOCH_JD


def _rev(deg):
    return deg % 360.0


def _kepler(mean_anomaly, e):
    """Eccentric anomaly in degrees from the mean anomaly in degrees.
    Newton's method; three passes is plenty at planetary eccentricities."""
    m = radians(_rev(mean_anomaly))
    ecc = m + e * sin(m) * (1.0 + e * cos(m))
    for _ in range(3):
        ecc -= (ecc - e * sin(ecc) - m) / (1.0 - e * cos(ecc))
    return degrees(ecc)


def _obliquity(d):
    return 23.4393 - 3.563e-7 * d


def ecl_to_equatorial(lon, lat, d):
    """Ecliptic longitude/latitude in degrees to right ascension and
    declination in degrees."""
    eps = radians(_obliquity(d))
    lon, lat = radians(lon), radians(lat)
    x = cos(lat) * cos(lon)
    y = cos(lat) * sin(lon) * cos(eps) - sin(lat) * sin(eps)
    z = cos(lat) * sin(lon) * sin(eps) + sin(lat) * cos(eps)
    return _rev(degrees(atan2(y, x))), degrees(asin(z))


def _lst_deg(when, lon):
    """Local apparent sidereal time in degrees."""
    d2000 = _jd(when) - _J2000_JD
    return _rev(280.46061837 + 360.98564736629 * d2000 + lon)


def alt_az(ra, dec, when, lat, lon):
    """Altitude and azimuth in degrees for an equatorial position."""
    hour_angle = radians(_lst_deg(when, lon) - ra)
    dec, phi = radians(dec), radians(lat)
    alt = asin(sin(phi) * sin(dec) + cos(phi) * cos(dec) * cos(hour_angle))
    az = atan2(sin(hour_angle),
               cos(hour_angle) * sin(phi) - tan(dec) * cos(phi))
    return degrees(alt), _rev(degrees(az) + 180.0)


def compass(az):
    return COMPASS[int(round(az / 22.5)) % 16]


def _observer_vector(when, lat, lon):
    """The observer's position relative to the centre of the Earth, in
    equatorial rectangular coordinates and Earth radii, flattening
    included. Subtracting it turns a geocentric position into the
    topocentric one an observer actually sees -- worth an entire degree
    for the moon, and the difference between a solar eclipse being
    partial here and total here."""
    phi = radians(lat)
    # Geocentric latitude and radius on the IAU 1976 spheroid.
    u = atan2(0.99664719 * sin(phi), cos(phi))
    rho_sin = 0.99664719 * sin(u)
    rho_cos = cos(u)
    theta = radians(_lst_deg(when, lon))
    return rho_cos * cos(theta), rho_cos * sin(theta), rho_sin


def _topocentric(ra, dec, dist_er, when, lat, lon):
    """(ra, dec, distance) shifted from the Earth's centre to the
    observer. Distances are in Earth radii."""
    ra_r, dec_r = radians(ra), radians(dec)
    x = dist_er * cos(dec_r) * cos(ra_r)
    y = dist_er * cos(dec_r) * sin(ra_r)
    z = dist_er * sin(dec_r)
    ox, oy, oz = _observer_vector(when, lat, lon)
    x, y, z = x - ox, y - oy, z - oz
    dist = sqrt(x * x + y * y + z * z)
    return _rev(degrees(atan2(y, x))), degrees(asin(z / dist)), dist


# === Sun and moon ===

AU_IN_EARTH_RADII = 23454.8
SUN_SEMIDIAMETER_AU = 0.2666      # degrees at 1 AU
MOON_RADIUS_ER = 0.2725           # moon radius in Earth radii


def sun_ecliptic(d):
    """(ecliptic longitude, distance in AU, mean anomaly, argument of
    perihelion) -- all degrees except the distance."""
    w = 282.9404 + 4.70935e-5 * d
    e = 0.016709 - 1.151e-9 * d
    m = _rev(356.0470 + 0.9856002585 * d)
    ecc = radians(_kepler(m, e))
    x, y = cos(ecc) - e, sin(ecc) * sqrt(1.0 - e * e)
    return _rev(degrees(atan2(y, x)) + w), hypot(x, y), m, w


def sun_position(when):
    """Geocentric right ascension, declination and distance (AU)."""
    d = _day(when)
    lon, r, _, _ = sun_ecliptic(d)
    ra, dec = ecl_to_equatorial(lon, 0.0, d)
    return ra, dec, r


def moon_ecliptic(d):
    """Geocentric ecliptic longitude and latitude in degrees and the
    distance in Earth radii, with Schlyter's twelve longitude and five
    latitude perturbation terms -- the evection and the variation matter
    at the degree level, so none of them are optional."""
    n = 125.1228 - 0.0529538083 * d
    i = 5.1454
    w = 318.0634 + 0.1643573223 * d
    a, e = 60.2666, 0.054900
    m = _rev(115.3654 + 13.0649929509 * d)

    ecc = radians(_kepler(m, e))
    xv = a * (cos(ecc) - e)
    yv = a * sqrt(1.0 - e * e) * sin(ecc)
    v = degrees(atan2(yv, xv))
    r = hypot(xv, yv)

    nr, ir, vw = radians(n), radians(i), radians(v + w)
    xe = r * (cos(nr) * cos(vw) - sin(nr) * sin(vw) * cos(ir))
    ye = r * (sin(nr) * cos(vw) + cos(nr) * sin(vw) * cos(ir))
    ze = r * sin(vw) * sin(ir)
    lon = degrees(atan2(ye, xe))
    lat = degrees(atan2(ze, hypot(xe, ye)))

    # Perturbation arguments: the sun's and moon's mean anomalies, the
    # moon's mean elongation from the sun and its argument of latitude.
    _, _, ms, ws = sun_ecliptic(d)
    ls = _rev(ms + ws)
    lm = _rev(n + w + m)
    dl = radians(_rev(lm - ls))
    f = radians(_rev(lm - n))
    ms_r, mm_r = radians(ms), radians(m)

    lon += (-1.274 * sin(mm_r - 2 * dl)        # evection
            + 0.658 * sin(2 * dl)              # variation
            - 0.186 * sin(ms_r)                # yearly equation
            - 0.059 * sin(2 * mm_r - 2 * dl)
            - 0.057 * sin(mm_r - 2 * dl + ms_r)
            + 0.053 * sin(mm_r + 2 * dl)
            + 0.046 * sin(2 * dl - ms_r)
            + 0.041 * sin(mm_r - ms_r)
            - 0.035 * sin(dl)                  # parallactic equation
            - 0.031 * sin(mm_r + ms_r)
            - 0.015 * sin(2 * f - 2 * dl)
            + 0.011 * sin(mm_r - 4 * dl))
    lat += (-0.173 * sin(f - 2 * dl)
            - 0.055 * sin(mm_r - f - 2 * dl)
            - 0.046 * sin(mm_r + f - 2 * dl)
            + 0.033 * sin(f + 2 * dl)
            + 0.017 * sin(2 * mm_r + f))
    r += -0.58 * cos(mm_r - 2 * dl) - 0.46 * cos(2 * dl)
    return _rev(lon), lat, r


def moon_position(when):
    """Geocentric right ascension, declination and distance in Earth
    radii."""
    d = _day(when)
    lon, lat, r = moon_ecliptic(d)
    ra, dec = ecl_to_equatorial(lon, lat, d)
    return ra, dec, r


def moon_altitude(when, lat, lon):
    """The moon's topocentric altitude in degrees -- the parallax
    correction is up to a degree, which matters when the question is
    "is it above the horizon"."""
    ra, dec, r = moon_position(when)
    t_ra, t_dec, _ = _topocentric(ra, dec, r, when, lat, lon)
    return alt_az(t_ra, t_dec, when, lat, lon)[0]


# === Planets ===

# name: (N, i, w, a, e, M) as (constant, per-day rate) pairs. a is in AU
# and constant for the inner planets.
_ELEMENTS = {
    "Mercury": ((48.3313, 3.24587e-5), (7.0047, 5.00e-8),
                (29.1241, 1.01444e-5), (0.387098, 0.0),
                (0.205635, 5.59e-10), (168.6562, 4.0923344368)),
    "Venus": ((76.6799, 2.46590e-5), (3.3946, 2.75e-8),
              (54.8910, 1.38374e-5), (0.723330, 0.0),
              (0.006773, -1.302e-9), (48.0052, 1.6021302244)),
    "Mars": ((49.5574, 2.11081e-5), (1.8497, -1.78e-8),
             (286.5016, 2.92961e-5), (1.523688, 0.0),
             (0.093405, 2.516e-9), (18.6021, 0.5240207766)),
    "Jupiter": ((100.4542, 2.76854e-5), (1.3030, -1.557e-7),
                (273.8777, 1.64505e-5), (5.20256, 0.0),
                (0.048498, 4.469e-9), (19.8950, 0.0830853001)),
    "Saturn": ((113.6634, 2.38980e-5), (2.4886, -1.081e-7),
               (339.3939, 2.97661e-5), (9.55475, 0.0),
               (0.055546, -9.499e-9), (316.9670, 0.0334442282)),
    "Uranus": ((74.0005, 1.3978e-5), (0.7733, 1.9e-8),
               (96.6612, 3.0565e-5), (19.18171, -1.55e-8),
               (0.047318, 7.45e-9), (142.5905, 0.011725806)),
    "Neptune": ((131.7806, 3.0173e-5), (1.7700, -2.55e-7),
                (272.8461, -6.027e-6), (30.05826, 3.313e-8),
                (0.008606, 2.15e-9), (260.2471, 0.005995147)),
}
# The five anyone can see without help. Uranus and Neptune stay in the
# element table for the eclipse-free pleasure of completeness, but a
# report that says "Neptune is up" is not telling anyone anything useful.
NAKED_EYE = ("Mercury", "Venus", "Mars", "Jupiter", "Saturn")


def _helio(name, d):
    """Heliocentric ecliptic longitude, latitude and distance (AU)."""
    (n0, nr), (i0, ir), (w0, wr), (a0, ar), (e0, er), (m0, mr) = _ELEMENTS[name]
    n, i, w = n0 + nr * d, i0 + ir * d, w0 + wr * d
    a, e, m = a0 + ar * d, e0 + er * d, _rev(m0 + mr * d)

    ecc = radians(_kepler(m, e))
    xv = a * (cos(ecc) - e)
    yv = a * sqrt(1.0 - e * e) * sin(ecc)
    v = degrees(atan2(yv, xv))
    r = hypot(xv, yv)

    nr_, ir_, vw = radians(_rev(n)), radians(i), radians(v + w)
    x = r * (cos(nr_) * cos(vw) - sin(nr_) * sin(vw) * cos(ir_))
    y = r * (sin(nr_) * cos(vw) + cos(nr_) * sin(vw) * cos(ir_))
    z = r * sin(vw) * sin(ir_)
    lon = _rev(degrees(atan2(y, x)))
    lat = degrees(atan2(z, hypot(x, y)))
    return _apply_perturbations(name, d, lon, lat, r)


def _apply_perturbations(name, d, lon, lat, r):
    """The mutual pulls of the giant planets. Left out, Saturn strays
    over half a degree and Jupiter a third of one."""
    if name not in ("Jupiter", "Saturn", "Uranus"):
        return lon, lat, r
    mj = radians(_rev(19.8950 + 0.0830853001 * d))
    ms = radians(_rev(316.9670 + 0.0334442282 * d))
    mu = radians(_rev(142.5905 + 0.011725806 * d))
    if name == "Jupiter":
        lon += (-0.332 * sin(2 * mj - 5 * ms - radians(67.6))
                - 0.056 * sin(2 * mj - 2 * ms + radians(21))
                + 0.042 * sin(3 * mj - 5 * ms + radians(21))
                - 0.036 * sin(mj - 2 * ms)
                + 0.022 * cos(mj - ms)
                + 0.023 * sin(2 * mj - 3 * ms + radians(52))
                - 0.016 * sin(mj - 5 * ms - radians(69)))
    elif name == "Saturn":
        lon += (0.812 * sin(2 * mj - 5 * ms - radians(67.6))
                - 0.229 * cos(2 * mj - 4 * ms - radians(2))
                + 0.119 * sin(mj - 2 * ms - radians(3))
                + 0.046 * sin(2 * mj - 6 * ms - radians(69))
                + 0.014 * sin(mj - 3 * ms + radians(32)))
        lat += (-0.020 * cos(2 * mj - 4 * ms - radians(2))
                + 0.018 * sin(2 * mj - 6 * ms - radians(49)))
    else:
        lon += (0.040 * sin(ms - 2 * mu + radians(6))
                + 0.035 * sin(ms - 3 * mu + radians(33))
                - 0.015 * sin(mj - mu + radians(20)))
    return _rev(lon), lat, r


def _magnitude(name, d, r, dist, phase, lon, lat):
    """Apparent magnitude from Schlyter's fits: distance, then a phase
    term, plus the tilt of the rings for Saturn -- which swings it by
    more than a magnitude across its orbit."""
    base = 5.0 * log10(r * dist)
    if name == "Mercury":
        return -0.36 + base + 0.027 * phase + 2.2e-13 * phase ** 6
    if name == "Venus":
        return -4.34 + base + 0.013 * phase + 4.2e-7 * phase ** 3
    if name == "Mars":
        return -1.51 + base + 0.016 * phase
    if name == "Jupiter":
        return -9.25 + base + 0.014 * phase
    if name == "Saturn":
        # Ring opening angle, from the pole of the ring plane.
        ir_, nr_ = radians(28.06), radians(169.51 + 3.82e-5 * d)
        lat_r, lon_r = radians(lat), radians(lon)
        b = asin(sin(lat_r) * cos(ir_)
                 - cos(lat_r) * sin(ir_) * sin(lon_r - nr_))
        rings = -2.6 * abs(sin(b)) + 1.2 * sin(b) ** 2
        return -9.0 + base + 0.044 * phase + rings
    if name == "Uranus":
        return -7.15 + base + 0.001 * phase
    return -6.90 + base + 0.001 * phase


def planet_position(name, when):
    """Everything the report needs about one planet: right ascension,
    declination, apparent magnitude, elongation from the sun in degrees
    and geocentric distance in AU."""
    d = _day(when)
    lon_h, lat_h, r = _helio(name, d)
    sun_lon, sun_r, _, _ = sun_ecliptic(d)

    # Heliocentric to geocentric: add the sun's geocentric vector.
    lon_r_, lat_r_ = radians(lon_h), radians(lat_h)
    x = r * cos(lat_r_) * cos(lon_r_) + sun_r * cos(radians(sun_lon))
    y = r * cos(lat_r_) * sin(lon_r_) + sun_r * sin(radians(sun_lon))
    z = r * sin(lat_r_)
    dist = sqrt(x * x + y * y + z * z)
    lon = _rev(degrees(atan2(y, x)))
    lat = degrees(asin(z / dist))
    ra, dec = ecl_to_equatorial(lon, lat, d)

    elong = degrees(acos(max(-1.0, min(1.0,
        (sun_r * sun_r + dist * dist - r * r) / (2.0 * sun_r * dist)))))
    phase = degrees(acos(max(-1.0, min(1.0,
        (r * r + dist * dist - sun_r * sun_r) / (2.0 * r * dist)))))
    return {"name": name, "ra": ra, "dec": dec, "dist": dist,
            "elongation": elong,
            "mag": _magnitude(name, d, r, dist, phase, lon, lat)}


# How far the sun has to be below the horizon before a planet of a given
# brightness is really there to be seen: Venus and Jupiter hold their own
# in a twilight sky, the rest want the sun properly down.
BRIGHT_MAGNITUDE = -2.0


def visible_planets(samples, lat, lon, min_altitude=10.0, min_elongation=12.0):
    """The naked-eye planets that get properly up tonight, brightest
    first.

    `samples` is a sequence of (time, sun altitude) pairs spanning the
    night -- the sun's altitude is needed because "visible" is a
    different bar for Venus at magnitude -4 than for Mars at +1.3. Each
    entry returned carries the moment that planet is highest in a sky
    dark enough for it, with the altitude and compass bearing then: a
    planet 40 degrees up in the south is worth a look, the same planet 5
    degrees up in the murk is not. Anything still buried in the sun's
    glare is dropped whatever its altitude."""
    found = []
    for name in NAKED_EYE:
        best = None
        for when, sun_alt in samples:
            pos = planet_position(name, when)
            if pos["elongation"] < min_elongation:
                continue
            needed = -0.833 if pos["mag"] <= BRIGHT_MAGNITUDE else -6.0
            if sun_alt > needed:
                continue
            alt, az = alt_az(pos["ra"], pos["dec"], when, lat, lon)
            if best is None or alt > best["alt"]:
                best = {"name": name, "when": when, "alt": alt, "az": az,
                        "mag": pos["mag"], "elongation": pos["elongation"]}
        if best and best["alt"] >= min_altitude:
            found.append(best)
    return sorted(found, key=lambda p: p["mag"])


# === Eclipses ===

def _shadow_radii(when):
    """Angular radii of the earth's umbra and penumbra at the moon's
    distance, plus the moon's own semidiameter and its separation from
    the shadow axis -- all degrees. The 1.02 enlargement is the
    conventional allowance for the earth's atmosphere."""
    d = _day(when)
    sun_lon, sun_r, _, _ = sun_ecliptic(d)
    moon_lon, moon_lat, moon_r = moon_ecliptic(d)

    moon_parallax = degrees(asin(1.0 / moon_r))
    sun_parallax = degrees(asin(1.0 / (sun_r * AU_IN_EARTH_RADII)))
    sun_sd = SUN_SEMIDIAMETER_AU / sun_r
    moon_sd = degrees(asin(MOON_RADIUS_ER / moon_r))

    umbra = 1.02 * (moon_parallax + sun_parallax - sun_sd)
    penumbra = 1.02 * (moon_parallax + sun_parallax + sun_sd)
    sep = _separation(moon_lon, moon_lat, sun_lon + 180.0, 0.0)
    return umbra, penumbra, moon_sd, sep


def _overlap_fraction(r_covered, r_cover, sep):
    """The fraction of a disc of radius `r_covered` hidden behind a disc
    of radius `r_cover` whose centre is `sep` away -- the area of the
    circular lens over the area of the disc. This is the "X% of the sun
    covered" figure everyone quotes, which is not the same as the
    eclipse magnitude (a ratio of diameters, and always the larger
    number)."""
    if sep >= r_covered + r_cover:
        return 0.0
    if sep <= abs(r_covered - r_cover):
        return min(1.0, (r_cover / r_covered) ** 2)
    a, b = r_covered, r_cover
    x = (sep * sep + a * a - b * b) / (2.0 * sep * a)
    y = (sep * sep + b * b - a * a) / (2.0 * sep * b)
    x, y = max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y))
    area = (a * a * (acos(x) - x * sqrt(max(0.0, 1.0 - x * x)))
            + b * b * (acos(y) - y * sqrt(max(0.0, 1.0 - y * y))))
    return area / (3.141592653589793 * a * a)


def _separation(lon1, lat1, lon2, lat2):
    """Angular separation in degrees between two spherical directions."""
    a1, b1, a2, b2 = radians(lon1), radians(lat1), radians(lon2), radians(lat2)
    cosine = sin(b1) * sin(b2) + cos(b1) * cos(b2) * cos(a1 - a2)
    return degrees(acos(max(-1.0, min(1.0, cosine))))


def _refine_peak(coarse, score, half_width, passes=2):
    """Walk in on the moment `score` is largest, starting from a coarse
    sample and halving the window each pass -- the scan step is minutes
    wide, and quoting greatest eclipse to the wrong four minutes is a
    silly way to be wrong."""
    best, width = coarse, half_width
    for _ in range(passes):
        step = width / 6.0
        for i in range(-6, 7):
            when = best + step * i
            if score(when) > score(best):
                best = when
        width = step
    return best


def lunar_eclipse(start, end, lat, lon, step_minutes=4):
    """The lunar eclipse in the window `start`-`end`, or None. Only
    eclipses with the moon actually above the local horizon at some
    point count -- a total eclipse under our feet is somebody else's
    night.

    Returns the deepest kind reached ("total", "partial" or
    "penumbral"), the umbral magnitude at maximum (fraction of the
    moon's diameter inside the umbra; negative when it never reaches
    the umbra), the time of maximum, the first and last moments the
    eclipse is both under way and visible from here, and the moon's
    altitude at maximum."""
    step = timedelta(minutes=step_minutes)
    samples, when = [], start
    while when <= end:
        umbra, penumbra, moon_sd, sep = _shadow_radii(when)
        if sep < penumbra + moon_sd:
            samples.append((when, umbra, penumbra, moon_sd, sep))
        when += step
    if not samples:
        return None

    visible = [s for s in samples if moon_altitude(s[0], lat, lon) > 0.0]
    if not visible:
        return None

    def umbral_magnitude(sample):
        _, umbra, _, moon_sd, sep = sample
        return (umbra + moon_sd - sep) / (2.0 * moon_sd)

    peak = max(visible, key=umbral_magnitude)

    def depth(when):
        umbra, _, moon_sd, sep = _shadow_radii(when)
        return (umbra + moon_sd - sep) / (2.0 * moon_sd)

    when = _refine_peak(peak[0], depth, step)
    umbra, penumbra, moon_sd, sep = _shadow_radii(when)
    magnitude = depth(when)
    if sep < umbra - moon_sd:
        kind = "total"
    elif magnitude > 0.0:
        kind = "partial"
    else:
        kind = "penumbral"
    return {"kind": kind, "magnitude": magnitude, "peak": when,
            "start": visible[0][0], "end": visible[-1][0],
            "altitude": moon_altitude(when, lat, lon)}


def solar_eclipse(start, end, lat, lon, step_minutes=3):
    """The solar eclipse seen from this spot in the window `start`-`end`,
    or None. Worked topocentrically -- the whole point of a solar
    eclipse is that it is a different eclipse a hundred miles away --
    and reported as the eclipse magnitude, the fraction of the sun's
    diameter the moon covers at maximum. The sun has to be up."""
    step = timedelta(minutes=step_minutes)
    best, when = None, start
    while when <= end:
        d = _day(when)
        sun_ra, sun_dec, sun_r = sun_position(when)
        s_ra, s_dec, _ = _topocentric(sun_ra, sun_dec,
                                      sun_r * AU_IN_EARTH_RADII, when, lat, lon)
        alt = alt_az(s_ra, s_dec, when, lat, lon)[0]
        if alt > 0.0:
            m_ra, m_dec, m_r = moon_position(when)
            m_ra, m_dec, m_dist = _topocentric(m_ra, m_dec, m_r, when, lat, lon)
            sun_sd = SUN_SEMIDIAMETER_AU / sun_r
            moon_sd = degrees(asin(MOON_RADIUS_ER / m_dist))
            sep = _separation(s_ra, s_dec, m_ra, m_dec)
            magnitude = (sun_sd + moon_sd - sep) / (2.0 * sun_sd)
            if magnitude > 0.0 and (best is None or magnitude > best["magnitude"]):
                best = {"magnitude": magnitude, "peak": when, "altitude": alt,
                        "obscuration": _overlap_fraction(sun_sd, moon_sd, sep),
                        "kind": ("total" if sep < moon_sd - sun_sd else
                                 "annular" if sep < sun_sd - moon_sd else
                                 "partial")}
        when += step
    return best


def eclipse_possible(when, lunar=True, days=2.0):
    """Cheap gate before the minute-by-minute scan: eclipses only happen
    within a couple of days of full moon (lunar) or new moon (solar), so
    most nights can be dismissed on the moon's phase alone."""
    d = _day(when)
    sun_lon, _, _, _ = sun_ecliptic(d)
    moon_lon, _, _ = moon_ecliptic(d)
    elong = abs(((moon_lon - sun_lon + 180.0) % 360.0) - 180.0)
    target = 180.0 if lunar else 0.0
    # The moon gains about 12.2 degrees of elongation a day.
    return abs(elong - target) <= days * 12.2


# === Deep sky ===

# Showpieces only: things that are worth walking outside for from a
# British back garden, with the equipment they actually need. J2000
# positions, right ascension in hours.
DEEP_SKY = (
    ("M31 Andromeda Galaxy", 0.712, 41.27, 3.4, "galaxy", "binoculars"),
    ("M33 Triangulum Galaxy", 1.564, 30.66, 5.7, "galaxy", "dark skies"),
    ("NGC 869/884 Double Cluster", 2.33, 57.14, 4.3, "open cluster", "binoculars"),
    ("M34", 2.70, 42.78, 5.2, "open cluster", "binoculars"),
    ("NGC 457 Owl Cluster", 1.32, 58.28, 6.4, "open cluster", "small scope"),
    ("M103", 1.55, 60.66, 7.4, "open cluster", "small scope"),
    ("M45 Pleiades", 3.79, 24.12, 1.6, "open cluster", "naked eye"),
    ("M37", 5.87, 32.55, 5.6, "open cluster", "binoculars"),
    ("M42 Orion Nebula", 5.588, -5.39, 4.0, "nebula", "binoculars"),
    ("M35", 6.15, 24.33, 5.3, "open cluster", "binoculars"),
    ("M44 Beehive", 8.67, 19.67, 3.7, "open cluster", "naked eye"),
    ("M81/M82 Bode's & Cigar", 9.93, 69.07, 6.9, "galaxy", "small scope"),
    ("M97 Owl Nebula", 11.246, 55.02, 9.9, "planetary nebula", "telescope"),
    ("Mizar & Alcor", 13.40, 54.93, 2.2, "double star", "naked eye"),
    ("M51 Whirlpool Galaxy", 13.50, 47.20, 8.4, "galaxy", "telescope"),
    ("M101 Pinwheel Galaxy", 14.05, 54.35, 7.9, "galaxy", "dark skies"),
    ("M13 Hercules Cluster", 16.695, 36.46, 5.8, "globular cluster", "binoculars"),
    ("M92", 17.285, 43.14, 6.4, "globular cluster", "small scope"),
    ("M11 Wild Duck Cluster", 18.85, -6.27, 5.8, "open cluster", "binoculars"),
    ("M57 Ring Nebula", 18.893, 33.03, 8.8, "planetary nebula", "telescope"),
    ("Cr 399 Coathanger", 19.43, 20.19, 3.6, "asterism", "binoculars"),
    ("Albireo", 19.512, 27.96, 3.1, "double star", "small scope"),
    ("M27 Dumbbell Nebula", 19.99, 22.72, 7.4, "planetary nebula", "small scope"),
    ("NGC 6960/6992 Veil Nebula", 20.76, 30.70, 7.0, "nebula", "dark skies"),
    ("NGC 7000 North America Nebula", 20.98, 44.33, 4.0, "nebula", "dark skies"),
    ("M39", 21.53, 48.43, 4.6, "open cluster", "binoculars"),
    ("M15", 21.50, 12.17, 6.2, "globular cluster", "binoculars"),
    ("M2", 21.558, -0.82, 6.5, "globular cluster", "small scope"),
    ("M52", 23.40, 61.59, 6.9, "open cluster", "small scope"),
    ("NGC 7789 Caroline's Rose", 23.96, 56.72, 6.7, "open cluster", "small scope"),
)
# Faint, diffuse things a bright moon washes out entirely.
_MOON_SENSITIVE = {"galaxy", "nebula"}


def deep_sky_picks(when, lat, lon, moon_illum=0.0, count=3, min_altitude=30.0):
    """`count` targets that are well up at `when`, favouring bright ones
    and mixing the types so the list isn't three galaxies. A bright moon
    pushes the faint diffuse objects down the order in favour of
    clusters and double stars, which barely care."""
    scored = []
    for name, ra_h, dec, mag, kind, tool in DEEP_SKY:
        alt, az = alt_az(ra_h * 15.0, dec, when, lat, lon)
        if alt < min_altitude:
            continue
        score = alt - 4.0 * mag
        if kind in _MOON_SENSITIVE:
            # Moonlight costs a diffuse object far more than its
            # catalogue magnitude suggests -- those numbers are the light
            # of the whole object added up, spread over a patch of sky
            # the moon is busy lighting. Clusters and double stars barely
            # notice, so under a gibbous moon the list fills with those.
            score -= moon_illum / 100.0 * (12.0 + 2.0 * mag)
        scored.append({"name": name, "alt": alt, "az": az, "mag": mag,
                       "kind": kind, "tool": tool, "score": score})
    scored.sort(key=lambda o: -o["score"])

    picks, used = [], {}
    for candidate in scored:
        if len(picks) >= count:
            break
        if used.get(candidate["kind"], 0) >= 2:
            continue
        used[candidate["kind"]] = used.get(candidate["kind"], 0) + 1
        picks.append(candidate)
    return picks
