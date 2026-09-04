"""Terrain elevation lookups for norris, from a prebuilt grid.

The grid is produced by tools/build_terrain.py from OS Terrain 50 (open
data): a plain lat/lon int16 array in metres, datum work already done, so
this module is a bilinear interpolation and nothing else. No data file, or
a point off the grid, degrades to "no answer" rather than an error.

ADS-B geometric altitude (alt_geom) is height above the WGS84 ellipsoid;
OS Terrain 50 heights are above mean sea level (ODN). The geoid sits about
53 m below the ellipsoid over southern Scotland, so AGL from alt_geom
subtracts that constant. Barometric altitude is pressure altitude -- no
correction is *right*, so it's used as-is when alt_geom is absent (QNH
weather error, typically a couple of hundred feet, comes with it).
"""
import struct
from array import array

FT_PER_M = 3.28084
GEOID_OFFSET_FT = 175  # ellipsoid minus geoid, ~53 m hereabouts
NODATA = -32768

_grid = None


def load(path):
    """Load the grid; silently disables lookups if the file is unusable."""
    global _grid
    try:
        with open(path, "rb") as f:
            magic, lat0, lon0, dlat, dlon, nlat, nlon = struct.unpack(
                "<4s4d2i", f.read(4 + 32 + 8))
            if magic != b"TER1":
                raise ValueError("bad magic")
            cells = array("h")
            cells.fromfile(f, nlat * nlon)
        _grid = (lat0, lon0, dlat, dlon, nlat, nlon, cells)
        return True
    except (OSError, ValueError, EOFError) as e:
        import logging
        logging.getLogger("planespotter").warning(
            "Terrain grid %s not loaded: %s", path, e)
        _grid = None
        return False


def elevation_ft(lat, lon):
    """Ground elevation in feet AMSL, or None off-grid. Cells the builder
    couldn't cover (outside GB) read as sea level, which is right for the
    sea and wrong for Ireland -- both beyond caring about here."""
    if _grid is None:
        return None
    lat0, lon0, dlat, dlon, nlat, nlon, cells = _grid
    x = (lon - lon0) / dlon - 0.5
    y = (lat - lat0) / dlat - 0.5
    if not (0 <= y < nlat - 1 and 0 <= x < nlon - 1):
        return None
    i, j = int(y), int(x)
    fy, fx = y - i, x - j
    corners = [cells[i * nlon + j], cells[i * nlon + j + 1],
               cells[(i + 1) * nlon + j], cells[(i + 1) * nlon + j + 1]]
    corners = [0 if c == NODATA else c for c in corners]
    metres = (corners[0] * (1 - fx) * (1 - fy) + corners[1] * fx * (1 - fy)
              + corners[2] * (1 - fx) * fy + corners[3] * fx * fy)
    return metres * FT_PER_M


def agl_ft(ac, altitude_ft):
    """Height above ground, preferring geometric altitude (corrected from
    ellipsoid to sea level) over the barometric fallback. None when the
    position is missing, the grid is absent, or the aircraft reports
    'ground'."""
    lat, lon = ac.get("lat"), ac.get("lon")
    if lat is None or lon is None or altitude_ft is None:
        return None
    ground = elevation_ft(lat, lon)
    if ground is None:
        return None
    geom = ac.get("alt_geom")
    if isinstance(geom, (int, float)):
        return geom - GEOID_OFFSET_FT - ground
    return altitude_ft - ground
