#!/usr/bin/env python3
"""Build a compact WGS84 elevation grid from OS Terrain 50 for norris.

Reads the Ordnance Survey Terrain 50 GB zip (open data, ASCII grid flavour:
terr50_gagg_gb.zip -- a zip of per-10km-tile zips each holding an .asc) and
resamples it onto a plain lat/lon grid, doing the WGS84 -> OSGB36 datum
shift and transverse Mercator projection here, once, so the bot's runtime
lookup is a dependency-free array index.

Output format (terrain.bin), little-endian:
  magic  4s   b"TER1"
  lat0   f8   southern edge, degrees
  lon0   f8   western edge, degrees
  dlat   f8   row step, degrees
  dlon   f8   column step, degrees
  nlat   i4   rows (south to north)
  nlon   i4   columns (west to east)
  data   nlat*nlon int16 metres, row-major from the southwest corner;
              -32768 = no data (outside GB coverage; treat as sea level)

Usage: build_terrain.py <terr50_gagg_gb.zip> <out.bin>
"""
import io
import re
import struct
import sys
import zipfile
from array import array
from math import atan2, cos, radians, sin, sqrt, tan

# Grid bounds: generous cover of the 55 nm online sweep around the receiver.
LAT0, LAT1 = 54.0, 56.6
LON0, LON1 = -5.6, -1.2
DLAT = 0.002          # ~222 m
DLON = 0.0035         # ~222 m at 55.3N
NODATA = -32768

# --- WGS84 lat/lon -> OSGB36 easting/northing (standard OS algorithm) ---

def latlon_to_osgb(lat_deg, lon_deg):
    # 1. Geodetic -> cartesian on WGS84 (h = 0).
    a, f = 6378137.0, 1 / 298.257223563
    e2 = f * (2 - f)
    lat, lon = radians(lat_deg), radians(lon_deg)
    nu = a / sqrt(1 - e2 * sin(lat) ** 2)
    x = nu * cos(lat) * cos(lon)
    y = nu * cos(lat) * sin(lon)
    z = nu * (1 - e2) * sin(lat)

    # 2. Helmert transform WGS84 -> OSGB36 (Airy 1830).
    tx, ty, tz = -446.448, 125.157, -542.060
    rx, ry, rz = (r / 3600 * 3.141592653589793 / 180
                  for r in (-0.1502, -0.2470, -0.8421))
    s = 20.4894e-6
    x2 = tx + (1 + s) * x - rz * y + ry * z
    y2 = ty + rz * x + (1 + s) * y - rx * z
    z2 = tz - ry * x + rx * y + (1 + s) * z

    # 3. Cartesian -> geodetic on Airy 1830.
    a, b = 6377563.396, 6356256.909
    e2 = 1 - (b * b) / (a * a)
    p = sqrt(x2 * x2 + y2 * y2)
    lat = atan2(z2, p * (1 - e2))
    for _ in range(8):
        nu = a / sqrt(1 - e2 * sin(lat) ** 2)
        lat = atan2(z2 + e2 * nu * sin(lat), p)
    lon = atan2(y2, x2)

    # 4. Transverse Mercator projection (National Grid).
    F0 = 0.9996012717
    lat0, lon0 = radians(49.0), radians(-2.0)
    E0, N0 = 400000.0, -100000.0
    n = (a - b) / (a + b)
    slat, clat, tlat = sin(lat), cos(lat), tan(lat)
    nu = a * F0 / sqrt(1 - e2 * slat * slat)
    rho = a * F0 * (1 - e2) / (1 - e2 * slat * slat) ** 1.5
    eta2 = nu / rho - 1
    dlat, plat = lat - lat0, lat + lat0
    M = b * F0 * (
        (1 + n + 1.25 * n * n + 1.25 * n ** 3) * dlat
        - (3 * n + 3 * n * n + 2.625 * n ** 3) * sin(dlat) * cos(plat)
        + (1.875 * n * n + 1.875 * n ** 3) * sin(2 * dlat) * cos(2 * plat)
        - (35 / 24) * n ** 3 * sin(3 * dlat) * cos(3 * plat))
    I = M + N0
    II = nu / 2 * slat * clat
    III = nu / 24 * slat * clat ** 3 * (5 - tlat ** 2 + 9 * eta2)
    IIIA = nu / 720 * slat * clat ** 5 * (61 - 58 * tlat ** 2 + tlat ** 4)
    IV = nu * clat
    V = nu / 6 * clat ** 3 * (nu / rho - tlat ** 2)
    VI = (nu / 120 * clat ** 5
          * (5 - 18 * tlat ** 2 + tlat ** 4 + 14 * eta2 - 58 * tlat ** 2 * eta2))
    dl = lon - lon0
    northing = I + II * dl ** 2 + III * dl ** 4 + IIIA * dl ** 6
    easting = E0 + IV * dl + V * dl ** 3 + VI * dl ** 5
    return easting, northing


# --- OS Terrain 50 tile store ---

class TileStore:
    """Lazy reader of the nested Terrain 50 zip; 10 km tiles keyed by their
    southwest corner (easting // 10000, northing // 10000)."""

    LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"

    def __init__(self, path):
        self.outer = zipfile.ZipFile(path)
        self.index = {}
        for name in self.outer.namelist():
            m = re.search(r"([A-Za-z]{2})(\d)(\d)_OST50GRID", name)
            if not m:
                continue
            sq, e1, n1 = m.group(1).upper(), int(m.group(2)), int(m.group(3))
            l1, l2 = self.LETTERS.index(sq[0]), self.LETTERS.index(sq[1])
            e100 = ((l1 - 2) % 5) * 5 + (l2 % 5)
            n100 = (19 - 5 * (l1 // 5)) - (l2 // 5)
            self.index[(e100 * 10 + e1, n100 * 10 + n1)] = name
        self.cache = {}

    def elevation(self, easting, northing):
        key = (int(easting // 10000), int(northing // 10000))
        if key not in self.index:
            return None
        tile = self.cache.get(key)
        if tile is None:
            tile = self._load(key)
            self.cache[key] = tile
        cells, xll, yll = tile
        col = int((easting - xll) // 50)
        row = int((northing - yll) // 50)
        if not (0 <= col < 200 and 0 <= row < 200):
            return None
        return cells[row * 200 + col]

    def _load(self, key):
        inner = self.outer.read(self.index[key])
        with zipfile.ZipFile(io.BytesIO(inner)) as zf:
            asc_name = next(n for n in zf.namelist() if n.endswith(".asc"))
            text = zf.read(asc_name).decode("ascii")
        header, values = {}, []
        for line in text.splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0].isalpha() or parts[0][0].isalpha():
                header[parts[0].lower()] = parts[1]
            else:
                values.extend(float(v) for v in parts)
        xll = float(header["xllcorner"])
        yll = float(header["yllcorner"])
        # .asc rows run north -> south; flip so row 0 is the southern edge.
        rows = [values[i * 200:(i + 1) * 200] for i in range(200)]
        flat = []
        for r in reversed(rows):
            flat.extend(int(round(v)) for v in r)
        cells = array("h", flat)
        return cells, xll, yll


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    store = TileStore(sys.argv[1])
    nlat = int(round((LAT1 - LAT0) / DLAT))
    nlon = int(round((LON1 - LON0) / DLON))
    out = array("h")
    print(f"{nlat} x {nlon} cells, {len(store.index)} tiles indexed")
    for i in range(nlat):
        lat = LAT0 + (i + 0.5) * DLAT
        row = array("h", [NODATA] * nlon)
        for j in range(nlon):
            lon = LON0 + (j + 0.5) * DLON
            e, n = latlon_to_osgb(lat, lon)
            elev = store.elevation(e, n)
            if elev is not None:
                row[j] = max(NODATA + 1, min(32767, int(elev)))
        out.extend(row)
        if i % 100 == 0:
            print(f"row {i}/{nlat}")
    with open(sys.argv[2], "wb") as f:
        f.write(struct.pack("<4s4d2i", b"TER1", LAT0, LON0, DLAT, DLON, nlat, nlon))
        out.tofile(f)
    print(f"wrote {sys.argv[2]}: {4 + 32 + 8 + out.itemsize * len(out)} bytes")


if __name__ == "__main__":
    main()
