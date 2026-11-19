"""Weather for MapAround's web map: NOAA's GFS forecast as small pictures, every 6 hours.

For each forecast hour (3 to 48, every 3), two PNG files of the whole world at 0.5° (720 × 361,
west to east from 180°W, north to south from 90°N), each colour channel one field scaled to 0-255:

  wt_fNNN.png  R: wind east (u) at 10 m, G: wind north (v), B: temperature at 2 m
  cp_fNNN.png  R: cloud cover (whole sky), G: rain and snow (square root of the rate, so light
               rain shows), B: pressure at sea level

and manifest.json: the model run, the steps' times and files, and each field's range. The web
map draws them on the graphics card (smooth colours, wind particles); see MapAround's
docs/data-view.md.

GFS: NOAA's Global Forecast System, public domain (a work of the US government). Read from NOAA's
NOMADS server, through its "grib filter" (only the fields and the 0.5° grid needed).

    python weather.py --out DIR [--run 2026111806] [--hours 48]
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

import numpy as np
from PIL import Image

NOMADS = "https://nomads.ncep.noaa.gov"
USER_AGENT = "MapAround-weather (github.com/adam2314/my-pipeline)"

# Each field's range in the pictures (values outside are clipped).
RANGES = {
    "u": (-40.0, 40.0),  # m/s
    "v": (-40.0, 40.0),  # m/s
    "t": (-50.0, 50.0),  # °C
    "cloud": (0.0, 100.0),  # %
    "precip": (0.0, 20.0),  # mm/h, as its square root
    "pressure": (950.0, 1050.0),  # hPa
}


def get(url, tries=4):
    """The bytes at [url], trying again a few times (NOMADS gets busy)."""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError) as e:
            if i == tries - 1:
                raise
            print(f"  retry after {e}", file=sys.stderr)
            time.sleep(10 * (i + 1))


def exists(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status == 200
    except urllib.error.URLError:
        return False


def latest_run(hours):
    """The newest model run whose last step is out (runs are at 00, 06, 12 and 18 UTC)."""
    now = dt.datetime.now(dt.timezone.utc)
    t = now.replace(minute=0, second=0, microsecond=0, hour=now.hour // 6 * 6)
    for _ in range(6):
        d, h = t.strftime("%Y%m%d"), t.strftime("%H")
        if exists(f"{NOMADS}/pub/data/nccf/com/gfs/prod/gfs.{d}/{h}/atmos/gfs.t{h}z.pgrb2full.0p50.f{hours:03d}.idx"):
            return t
        t -= dt.timedelta(hours=6)
    raise SystemExit("No complete GFS run found on NOMADS")


def fetch_step(run, hour):
    """The GRIB2 bytes of one forecast hour: only the fields used."""
    d, h = run.strftime("%Y%m%d"), run.strftime("%H")
    query = "&".join(
        [
            f"dir=%2Fgfs.{d}%2F{h}%2Fatmos",
            f"file=gfs.t{h}z.pgrb2full.0p50.f{hour:03d}",
            "var_UGRD=on", "var_VGRD=on", "var_TMP=on", "var_TCDC=on", "var_PRATE=on", "var_PRMSL=on",
            "lev_10_m_above_ground=on", "lev_2_m_above_ground=on", "lev_entire_atmosphere=on",
            "lev_surface=on", "lev_mean_sea_level=on",
        ]
    )
    return get(f"{NOMADS}/cgi-bin/filter_gfs_0p50.pl?{query}")


def decode(grib_bytes):
    """The fields in a GRIB2 file, as 361 × 720 arrays from 90°N and 180°W."""
    import eccodes

    fields = {}
    for message in messages(grib_bytes):
        gid = eccodes.codes_new_from_message(message)
        try:
            name = eccodes.codes_get(gid, "shortName")
            step_type = eccodes.codes_get(gid, "stepType")
            ni, nj = eccodes.codes_get(gid, "Ni"), eccodes.codes_get(gid, "Nj")
            values = eccodes.codes_get_values(gid).reshape(nj, ni)
            if eccodes.codes_get(gid, "jScansPositively") == 1:
                values = values[::-1]  # north first
            # From 0°E eastwards to from 180°W.
            values = np.roll(values, ni // 2, axis=1)
            fields[f"{name}:{step_type}"] = values
        finally:
            eccodes.codes_release(gid)
    return fields


def messages(data):
    """The GRIB2 messages in [data], each as its bytes ("GRIB", then its total length at 8-15)."""
    i = 0
    while True:
        i = data.find(b"GRIB", i)
        if i < 0 or i + 16 > len(data):
            return
        length = int.from_bytes(data[i + 8:i + 16], "big")
        yield data[i:i + length]
        i += length


def scaled(values, lo, hi):
    return np.clip(np.rint((values - lo) / (hi - lo) * 255), 0, 255).astype(np.uint8)


def pictures(fields):
    def field(*keys):
        for k in keys:
            if k in fields:
                return fields[k]
        raise KeyError(f"none of {keys} in {sorted(fields)}")

    u = field("10u:instant")
    v = field("10v:instant")
    t = field("2t:instant") - 273.15
    cloud = field("tcc:instant", "tcc:avg")
    rate = field("prate:avg", "prate:instant") * 3600  # kg/m²/s → mm/h
    pressure = field("prmsl:instant") / 100
    wt = np.dstack([scaled(u, *RANGES["u"]), scaled(v, *RANGES["v"]), scaled(t, *RANGES["t"])])
    lo, hi = RANGES["precip"]
    cp = np.dstack(
        [
            scaled(cloud, *RANGES["cloud"]),
            scaled(np.sqrt(np.clip(rate, 0, hi)), 0, np.sqrt(hi)),
            scaled(pressure, *RANGES["pressure"]),
        ]
    )
    return Image.fromarray(wt, "RGB"), Image.fromarray(cp, "RGB")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True)
    p.add_argument("--run", help="the model run, YYYYMMDDHH (default: the newest complete one)")
    p.add_argument("--hours", type=int, default=48)
    p.add_argument("--every", type=int, default=3)
    a = p.parse_args()

    os.makedirs(a.out, exist_ok=True)
    run = (
        dt.datetime.strptime(a.run, "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
        if a.run
        else latest_run(a.hours)
    )
    print(f"GFS run {run:%Y-%m-%d %H}Z", file=sys.stderr)
    steps = []
    width = height = None
    for hour in range(a.every, a.hours + 1, a.every):
        fields = decode(fetch_step(run, hour))
        wt, cp = pictures(fields)
        width, height = wt.size
        names = (f"wt_f{hour:03d}.png", f"cp_f{hour:03d}.png")
        wt.save(os.path.join(a.out, names[0]), optimize=True)
        cp.save(os.path.join(a.out, names[1]), optimize=True)
        valid = run + dt.timedelta(hours=hour)
        steps.append({"hour": hour, "time": valid.strftime("%Y-%m-%dT%H:%MZ"), "wt": names[0], "cp": names[1]})
        print(f"  +{hour:02d} h  {valid:%d %H}Z", file=sys.stderr)
    manifest = {
        "v": 1,
        "run": run.strftime("%Y-%m-%dT%H:%MZ"),
        "made": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "source": "NOAA GFS 0.5°",
        "licence": "public domain (NOAA)",
        "grid": {"width": width, "height": height, "west": -180, "north": 90, "step": 0.5},
        "channels": {"wt": ["u", "v", "t"], "cp": ["cloud", "precip", "pressure"]},
        "ranges": {k: list(v) for k, v in RANGES.items()},
        "precipScale": "sqrt",
        "steps": steps,
    }
    with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)


if __name__ == "__main__":
    main()
