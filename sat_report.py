#!/usr/bin/env python3
"""NOAA/Meteor satellite pass report: watches raspberry-noaa-v2's own
panel.db for newly-decoded passes, pushes a couple of image enhancements to
vr.z.je (same push interface as sonde_report.py's radiosonde flights, see
https://vr.z.je/cue.php), and posts a summary + the returned map URL to IRC
via the announce_dir bridge (norris-say).

Deliberately independent of raspberry-noaa-v2's own push integrations
(Discord/Slack/Twitter/etc, all disabled in settings.yml) -- this is its own
watcher over the pass database, the same shape as sonde_report.py's
--watch mode, so it needs nothing from raspberry-noaa-v2's bash scripts.

Usage:
    sat_report.py --pass-id 42     # one-shot, by decoded_passes.id
    sat_report.py --watch          # scan for newly-decoded, unposted passes (run from cron)
"""
import argparse
import base64
import json
import logging
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DB_FILE = Path("/home/pi/raspberry-noaa-v2/db/panel.db")
IMAGE_OUTPUT = Path("/srv/images")
NORRIS_SAY = Path(__file__).parent / "norris-say"
STATE_FILE = Path("/home/pi/.sat_report_state.json")

# Same key as the radiosonde and ADS-B feeds -- one key per station, for
# everything it hears -- but that copy is root-only (mode 600, read by the
# DynamicUser ADS-B service), and this script runs as the pi user via cron,
# so it uses the pi-owned copy sonde_report.py already reads from.
VRJE_KEY_FILE = Path("/home/pi/.vrje-key")
VRJE_INGEST_URL = "https://vr.z.je/pi_ingest.php"

# Enhancement variants to push, matching vr.z.je/cue.php's own example
# (MCIR + therm) -- both NOAA and Meteor enhancement lists in settings.yml
# include these, and they're the two most visually distinct (colour
# composite + thermal IR), so a pass usually has both without pushing every
# esoteric WXtoImg palette variant.
IMAGE_VARIANTS = ["MCIR", "therm"]

# Enhancements to judge pass quality on. Deliberately excludes MCIR and ZA:
# MCIR's colour palette and ZA's mostly-black night rendering each impose smooth
# structure of their own, and on the archived noise passes they scored 0.72-0.78
# where therm/NO/histeq on the *same* pass scored 0.02-0.45. These three render
# the decoder output most directly and so discriminate honestly.
QUALITY_VARIANTS = ["therm", "NO", "histeq"]

# Lag-1 spatial autocorrelation below which a pass is considered noise rather
# than imagery. Real satellite pictures are smooth (adjacent pixels strongly
# related, >0.9); receiver noise is not. Calibrated 2026-08-05 against all 15
# archived NOAA passes -- every one of them noise, since NOAA 15/18/19 were
# decommissioned in 2025 -- where the worst case across QUALITY_VARIANTS scored
# 0.45. Set with margin on both sides: well above the noise, well below imagery.
NOISE_CORR_THRESHOLD = 0.60

log = logging.getLogger("sat_report")


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
        with urllib.request.urlopen(req, timeout=60) as resp:
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


def push_satpass_to_vrje(sat_name, mode, pass_start, pass_end, file_path_base, key):
    """POST whichever of IMAGE_VARIANTS actually exist for this pass.
    Returns the map URL, or None if nothing usable was pushed."""
    images = {}
    for variant in IMAGE_VARIANTS:
        img_path = IMAGE_OUTPUT / f"{file_path_base}-{variant}.jpg"
        try:
            images[variant] = base64.b64encode(img_path.read_bytes()).decode("ascii")
        except OSError:
            continue
    if not images:
        log.warning("No usable image variants for %s (%s) among %s",
                    sat_name, file_path_base, IMAGE_VARIANTS)
        return None

    payload = {
        "kind": "satpass",
        "satellite": sat_name,
        "mode": mode,
        "pass_start": pass_start,
        "pass_end": pass_end,
        "images": images,
    }
    resp = _vrje_post(payload, key)
    if not resp or not resp.get("ok"):
        log.warning("vr.z.je satpass push failed: %s", resp)
        return None
    return resp.get("url")


def _best_band_correlation(a):
    """Max over horizontal bands of the weaker of the two lag-1
    autocorrelations. Banded because a pass often only acquires part-way
    through -- scoring the whole frame at once would average real imagery away
    against the noise either side of it. Taking min(horizontal, vertical)
    within a band means one axis alone can't carry it: JPEG blocking and the
    burnt-in map overlay both smear along a single axis, real cloud does not."""
    best = 0.0
    bands = 8
    h = a.shape[0]
    for i in range(bands):
        band = a[i * h // bands:(i + 1) * h // bands]
        if band.size < 1000:
            continue
        sd = band.std()
        if sd < 1e-6:
            continue
        x = band - band.mean()
        ch = float((x[:, :-1] * x[:, 1:]).mean() / (sd * sd))
        cv = float((x[:-1, :] * x[1:, :]).mean() / (sd * sd))
        best = max(best, min(ch, cv))
    return best


def image_correlation(path):
    """Spatial correlation score for one rendered enhancement, or None."""
    import numpy as np
    from PIL import Image

    a = np.asarray(Image.open(path).convert("L"), dtype=float)
    h, w = a.shape
    # Trim the annotation header and the grey sync/telemetry bars down each
    # side, both of which are present whether or not the pass carried signal.
    a = a[int(h * 0.12):int(h * 0.97), int(w * 0.20):int(w * 0.80)]
    if a.size < 1000:
        return None
    return _best_band_correlation(a)


def pass_looks_useless(file_path_base):
    """True when a pass carried no recoverable picture -- either nothing
    rendered at all (LRPT that never reached frame sync emits no images), or
    every enhancement is spatially uncorrelated (APT free-runs on noise and
    paints it as pixels regardless of whether a satellite was transmitting).

    Fails open: anything unexpected means we report the pass as before, since
    a spurious IRC line is a far cheaper mistake than swallowing a real one."""
    candidates = [IMAGE_OUTPUT / f"{file_path_base}-{v}.jpg" for v in QUALITY_VARIANTS]
    candidates = [p for p in candidates if p.exists()]
    if not candidates:
        # Unfamiliar enhancement set (Meteor composites are named differently
        # from the wxtoimg palettes) -- score whatever did render rather than
        # dropping a pass just because the preferred variants are absent.
        candidates = sorted(IMAGE_OUTPUT.glob(f"{file_path_base}-*.jpg"))

    scores = []
    for path in candidates:
        try:
            score = image_correlation(path)
        except Exception as e:
            log.warning("Quality check failed on %s (%s) -- reporting anyway",
                        path.name, e)
            return False
        if score is not None:
            scores.append((path.stem.replace(f"{file_path_base}-", ""), score))

    if not scores:
        log.info("%s: no imagery rendered -- suppressing", file_path_base)
        return True

    variant, best = max(scores, key=lambda kv: kv[1])
    useless = best < NOISE_CORR_THRESHOLD
    log.info("%s: spatial correlation %.3f (best of %d, %s) vs threshold %.2f -- %s",
             file_path_base, best, len(scores), variant, NOISE_CORR_THRESHOLD,
             "noise" if useless else "imagery")
    return useless


def fetch_new_passes(last_id):
    """decoded_passes rows past last_id, joined with predict_passes for the
    satellite name/elevation/direction that decoded_passes itself doesn't
    carry -- newest-decoded info lives in one table, pass geometry in the
    other, joined on pass_start same as the receive_*.sh scripts do."""
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT dp.id, dp.pass_start, dp.file_path, dp.sat_type,
                   pp.sat_name, pp.pass_end, pp.max_elev, pp.direction
            FROM decoded_passes dp
            LEFT JOIN predict_passes pp ON pp.pass_start = dp.pass_start
            WHERE dp.id > ?
            ORDER BY dp.id ASC
            """,
            (last_id,),
        ).fetchall()
    finally:
        conn.close()
    return rows


def build_irc_line(sat_name, pass_row, url):
    max_elev = pass_row["max_elev"]
    direction = pass_row["direction"] or "?"
    line = (
        f"Satellite pass: \x02\x0300{sat_name}\x03\x02"
        + (f" | max elev \x02\x0308{max_elev}\xb0\x03\x02 {direction}" if max_elev is not None else "")
    )
    if url:
        line += f" | {url}"
    return line


def send_to_irc(line):
    subprocess.run([str(NORRIS_SAY)], input=line + "\n", text=True, check=True)


def _load_last_id():
    try:
        return json.loads(STATE_FILE.read_text()).get("last_id", 0)
    except (FileNotFoundError, ValueError):
        return 0


def _save_last_id(last_id):
    STATE_FILE.write_text(json.dumps({"last_id": last_id}))


def process_pass(row, post=True, check_quality=True):
    sat_name = row["sat_name"] or f"sat_type={row['sat_type']}"
    mode = "APT" if row["sat_type"] == 1 else "LRPT"

    # Don't announce (or upload) a pass that carried no picture. NOAA 15/18/19
    # were decommissioned in 2025, so their scheduled passes now decode pure
    # noise into perfectly well-formed images -- without this they'd be posted
    # to IRC indistinguishably from a real one.
    if check_quality and pass_looks_useless(row["file_path"]):
        log.info("Suppressing %s (%s): no viable imagery", sat_name, row["file_path"])
        return None

    key = _vrje_key()
    url = None
    if key:
        url = push_satpass_to_vrje(sat_name, mode, row["pass_start"], row["pass_end"],
                                    row["file_path"], key)
    else:
        log.warning("No vr.z.je key at %s, skipping push", VRJE_KEY_FILE)

    line = build_irc_line(sat_name, row, url)
    print(line)
    if post:
        send_to_irc(line)
    return line


def watch_once():
    last_id = _load_last_id()
    rows = fetch_new_passes(last_id)
    for row in rows:
        log.info("New decoded pass id=%d (%s)", row["id"], row["sat_name"])
        try:
            process_pass(row)
        except Exception as e:
            log.error("Failed to process pass id=%d: %s", row["id"], e)
            continue
        _save_last_id(row["id"])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pass-id", type=int, help="decoded_passes.id to report on, one-shot")
    group.add_argument("--watch", action="store_true", help="scan for newly-decoded, unposted passes")
    parser.add_argument("--no-post", action="store_true", help="print the report without sending to IRC")
    parser.add_argument("--ignore-quality", action="store_true",
                        help="report even if the pass looks like noise (bypasses the viable-image check)")
    args = parser.parse_args()

    if args.watch:
        watch_once()
        return

    rows = fetch_new_passes(args.pass_id - 1)
    matches = [r for r in rows if r["id"] == args.pass_id]
    if not matches:
        raise SystemExit(f"No decoded_passes row with id {args.pass_id}")
    process_pass(matches[0], post=not args.no_post, check_quality=not args.ignore_quality)


if __name__ == "__main__":
    main()
