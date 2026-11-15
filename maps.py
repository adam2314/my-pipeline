#!/usr/bin/env python3
"""
MapAround's offline maps (github.com/adam2314/my-pipeline): OpenStreetMap extracts from
Geofabrik, written as mapsforge .map files with the data the MapAround app's styles draw that the
mapsforge.org maps leave out: marked hiking trails (from the route relations), cliffs, aretes and
ridges, path and track grades, surfaces, 4x4-only tracks, fords, huts and more (see
tag-mapping.xml). Run monthly on GitHub Actions (.github/workflows/maps.yml); the maps go to
GitHub releases, listed in catalog.json for the app. See README.md.

  maps.py setup   --tools DIR                    Osmosis and the mapsforge map writer
  maps.py land    --land DIR                     the coastline (land polygons) from osmdata.openstreetmap.de
  maps.py plan    --out plan.json [--only "europe/malta ..."] [--max-mb 1500]
  maps.py build   --plan plan.json --batch N --tools DIR --land DIR --work DIR --out DIR [--release TAG]
  maps.py region  europe/malta --tools DIR --land DIR --work DIR --out DIR     (one region, locally)
  maps.py catalog --plan plan.json --results DIR [--previous old.json] --out catalog.json

Needs Python 3.10+ with `pip install osmium shapely pyshp`, Java 17+, and for uploads the GitHub
CLI (gh).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GEOFABRIK = "https://download.geofabrik.de/"
LAND_URL = "https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip"
OSMOSIS_URL = "https://github.com/openstreetmap/osmosis/releases/download/0.49.2/osmosis-0.49.2.zip"
WRITER_URL = (
    "https://repo1.maven.org/maven2/org/mapsforge/mapsforge-map-writer/0.25.0/"
    "mapsforge-map-writer-0.25.0-jar-with-dependencies.jar"
)
USER_AGENT = "MapAround-maps/1 (OpenStreetMap map builder)"

# The app recognises these maps by this in the map file's comment (MapAroundMaps.MARKER in
# github.com/adam2314/maparound, shared/.../data/OfflineData.kt): keep the two the same.
MARKER = "MapAround maps"
COMMENT = f"Map data (c) OpenStreetMap contributors, ODbL. {MARKER} 1: marked trails, cliffs, path and track grades."
LANGUAGES = "en,nl,fr,de,it,es"  # as the mapsforge.org maps

# Objects MapAround adds (land and sea, trail lines) get ids far above OpenStreetMap's.
NEW_ID = 9_000_000_000_000

MB = 1 << 20


# ---- small helpers -------------------------------------------------------------------------------


def log(*a):
    print(*a, flush=True)


def fetch(url: str, dest: Path, tries: int = 4) -> Path:
    """Downloads [url] to [dest] (via a .part file), retrying network errors."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            part.replace(dest)
            return dest
        except Exception as e:  # noqa: BLE001 - any network error: try again
            if attempt == tries - 1:
                raise
            log(f"  retry {url}: {e}")
            time.sleep(15 * (attempt + 1))
    return dest


def head_size(url: str, tries: int = 4) -> int | None:
    """Content-Length of [url], or None if it doesn't exist."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r:
                return int(r.headers["Content-Length"])
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == tries - 1:
                raise
        except Exception:  # noqa: BLE001
            if attempt == tries - 1:
                raise
        time.sleep(5 * (attempt + 1))
    return None


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def read_regions(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def file_name(area_path: str) -> str:
    """The app's flat file name (its MapNames.localFileName): europe/germany/bayern -> europe__germany__bayern.map"""
    return "__".join(p for p in area_path.split("/") if p) + ".map"


# ---- the region's area -----------------------------------------------------------------------------


def poly_boxes(text: str) -> list[tuple[float, float, float, float]]:
    """
    The bounding boxes (south, west, north, east) of a Geofabrik .poly file, 0.1 deg larger, as
    mapsforge's poly2bb.pl: one box, or one per part when the region crosses the antimeridian
    (parts both entirely east and entirely west of it).
    """
    parts: list[list[float]] = []
    lines = [s.strip() for s in text.splitlines() if s.strip()]
    for s in lines[1:]:  # the first line is the file's name
        if s == "END":
            continue
        fields = s.split()
        if len(fields) >= 2:
            try:
                x, y = float(fields[0]), float(fields[1])
            except ValueError:
                continue
            if not parts:
                parts.append([360.0, 360.0, -360.0, -360.0])
            b = parts[-1]
            b[0] = min(b[0], y); b[1] = min(b[1], x); b[2] = max(b[2], y); b[3] = max(b[3], x)
        elif not s.startswith("!"):
            # A section header starts a part; a hole ("!n") belongs to the part before it, as in
            # poly2bb.pl.
            parts.append([360.0, 360.0, -360.0, -360.0])
    parts = [p for p in parts if p[0] <= p[2]]
    if not parts:
        raise ValueError("empty .poly file")
    west = any(p[1] > 0 and p[3] > 0 for p in parts)
    east = any(p[1] < 0 and p[3] < 0 for p in parts)
    if len(parts) > 1 and west and east:
        boxes = parts
    else:
        boxes = [[min(p[0] for p in parts), min(p[1] for p in parts), max(p[2] for p in parts), max(p[3] for p in parts)]]
    buf = 0.1
    return [
        (max(-90.0, s - buf), max(-180.0, w - buf), min(90.0, n + buf), min(180.0, e + buf))
        for s, w, n, e in boxes
    ]


def data_date(pbf: Path) -> str:
    """When the extract's data is from (its replication timestamp), ISO 8601 UTC."""
    import osmium

    r = osmium.io.Reader(str(pbf), osmium.osm.osm_entity_bits.NOTHING)
    try:
        stamp = r.header().get("osmosis_replication_timestamp", "")
    finally:
        r.close()
    if stamp:
        return stamp
    return dt.datetime.fromtimestamp(pbf.stat().st_mtime, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- marked trails --------------------------------------------------------------------------------
# As the app makes them from its own trail download (github.com/adam2314/maparound: TrailCellIO in
# shared/.../trails/Trails.kt, TrailTags in app/.../trails/TrailIndex.kt), so its styles draw both
# the same way: keep them in step.

PALETTE = {
    "red": 0xD32F2F, "blue": 0x1E63D6, "green": 0x2E9E3E, "yellow": 0xF2C200, "black": 0x222222,
    "white": 0xFFFFFF, "orange": 0xF57C00, "purple": 0x8E24AA, "brown": 0x8D5524,
}
NAMED = {
    "red": "red", "darkred": "red",
    "blue": "blue", "darkblue": "blue", "lightblue": "blue", "aqua": "blue", "cyan": "blue",
    "green": "green", "darkgreen": "green", "lightgreen": "green", "lime": "green",
    "yellow": "yellow", "gold": "yellow",
    "black": "black", "white": "white", "orange": "orange",
    "purple": "purple", "violet": "purple", "magenta": "purple", "pink": "purple",
    "brown": "brown",
}
NETWORK = {"iwn": 0, "nwn": 1, "rwn": 2, "lwn": 3}
OFFSETS = [["0"], ["l05", "r05"], ["l1", "0", "r1"], ["l15", "l05", "r05", "r15"]]


def trail_colour(osmc: str | None, colour: str | None) -> str:
    """The waymark colour: `osmc:symbol`'s first field, else `colour` (a name or the nearest to a hex)."""
    if osmc:
        c = NAMED.get(osmc.split(":", 1)[0].strip().lower())
        if c:
            return c
    if colour:
        c = NAMED.get(colour.strip().lower())
        if c:
            return c
        h = colour.strip().removeprefix("#")
        if len(h) == 6:
            try:
                v = int(h, 16)
            except ValueError:
                v = None
            if v is not None:
                def dist(rgb: int) -> int:
                    return sum(abs((rgb >> s & 255) - (v >> s & 255)) for s in (16, 8, 0))
                return min(PALETTE, key=lambda k: dist(PALETTE[k]))
    return "other"


def trail_track(tags) -> str | None:
    """The dirt road a trail way is, as the Topo style draws it (TrailTrack.of): a double line whose
    edges then take the trail's colours. any|high|4x4|minor, or None (a path, a road, a broken track)."""
    if tags.get("highway") != "track":
        return None
    if tags.get("tunnel") not in (None, "no", "false") or tags.get("area") not in (None, "no", "false"):
        return None
    four = tags.get("4wd_only")
    if four == "yes":
        return "4x4"
    if four not in (None, "no"):
        return None
    smoothness = tags.get("smoothness")
    if smoothness is not None:
        return {"excellent": "any", "good": "any", "intermediate": "any", "bad": "high",
                "very_bad": "4x4", "horrible": "4x4"}.get(smoothness)
    tracktype = tags.get("tracktype")
    if tracktype is None:
        return "minor"
    return {"grade1": "any", "grade2": "any", "grade3": "high", "grade4": "4x4", "grade5": "4x4"}.get(tracktype)


def trail_lines(routes: list[tuple[str, str, int, str]], track: str | None = None) -> list[dict[str, str]]:
    """Tag sets for a way on [routes] (name, ref, network, colour), a dirt road when [track]: one per
    waymark colour (TrailTags.lines)."""
    ordered = sorted(routes, key=lambda r: r[2])
    colours: list[str] = []
    for r in ordered:
        if r[3] not in colours:
            colours.append(r[3])
    colours = colours[:4]
    if not colours:
        return []
    offsets = OFFSETS[len(colours) - 1]
    out = []
    for k, colour in enumerate(colours):
        tags = {"trail": "hiking", "trail_colour": colour, "trail_offset": offsets[k]}
        if track:
            tags["trail_track"] = track
        if k == 0:
            tags["trail_n"] = str(len(colours))
            name, ref = ordered[0][0], ordered[0][1]
            if name:
                tags["name"] = name
            if ref:
                tags["ref"] = ref
        out.append(tags)
    return out


def trail_ways(pbf: Path) -> list[tuple[list[int], dict[str, str]]]:
    """The marked trail lines of an extract: ways of hiking and foot route relations, one copy per colour."""
    import osmium

    routes: list[tuple[str, str, int, str]] = []
    way_routes: dict[int, list[int]] = {}
    rels = osmium.FileProcessor(str(pbf), osmium.osm.RELATION).with_filter(
        osmium.filter.TagFilter(("route", "hiking"), ("route", "foot")))
    for rel in rels:
        t = rel.tags
        index = len(routes)
        routes.append((
            t.get("name") or t.get("ref") or "",
            t.get("ref") or "",
            NETWORK.get(t.get("network", ""), 4),
            trail_colour(t.get("osmc:symbol"), t.get("colour")),
        ))
        for m in rel.members:
            if m.type == "w":
                lst = way_routes.setdefault(m.ref, [])
                if index not in lst:
                    lst.append(index)
    out: list[tuple[list[int], dict[str, str]]] = []
    if not way_routes:
        return out
    ways = osmium.FileProcessor(str(pbf), osmium.osm.WAY).with_filter(osmium.filter.IdFilter(way_routes.keys()))
    for w in ways:
        r = way_routes.get(w.id)
        if r is None or len(w.nodes) < 2:
            continue
        nodes = [n.ref for n in w.nodes]
        track = trail_track({t.k: t.v for t in w.tags})
        for tags in trail_lines([routes[i] for i in r], track):
            out.append((nodes, tags))
    return out


# ---- country borders -----------------------------------------------------------------------------
# OpenStreetMap puts a border's level on its relation (boundary=administrative + admin_level=2), and
# more and more lines carry only boundary=administrative (all of Israel's and Palestine's do now);
# the map writer reads the lines' own tags. The app's styles draw country borders from admin_level=2
# lines (and show the world's coarse ones where a map has none: WorldTile.kt in
# github.com/adam2314/maparound), so each line of a country's border that doesn't say so gets a copy
# that does. Lines only in disputed ones (boundary=disputed, admin_level=2: lines of control, the
# extent of a claim) are disputed borders, which the styles dash.


def border_lines(way_tags: dict[str, str], disputed_only: bool) -> dict[str, str] | None:
    """
    The tags for a copy of a country border's line, or None when it needs none (it says so itself)
    or isn't drawn (at sea, the coast). [disputed_only]: it's in disputed borders only, no country's.
    """
    if way_tags.get("admin_level") == "2":
        return None
    if way_tags.get("maritime") == "yes" or way_tags.get("boundary") == "maritime" or way_tags.get("natural") == "coastline":
        return None
    tags = {"boundary": "administrative", "admin_level": "2"}
    if disputed_only or way_tags.get("disputed") == "yes" or way_tags.get("boundary") == "disputed":
        tags["disputed"] = "yes"
    return tags


def border_ways(pbf: Path) -> list[tuple[list[int], dict[str, str]]]:
    """Copies of the extract's country border lines (and disputed ones) that don't carry admin_level=2 themselves."""
    import osmium

    countries: set[int] = set()
    disputed: set[int] = set()
    rels = osmium.FileProcessor(str(pbf), osmium.osm.RELATION).with_filter(
        osmium.filter.TagFilter(("boundary", "administrative"), ("boundary", "disputed")))
    for rel in rels:
        t = rel.tags
        if t.get("admin_level") != "2":
            continue
        into = disputed if t.get("boundary") == "disputed" else countries
        for m in rel.members:
            if m.type == "w" and m.role in ("outer", "inner", ""):
                into.add(m.ref)
    out: list[tuple[list[int], dict[str, str]]] = []
    lines = countries | disputed
    if not lines:
        return out
    ways = osmium.FileProcessor(str(pbf), osmium.osm.WAY).with_filter(osmium.filter.IdFilter(lines))
    for w in ways:
        if len(w.nodes) < 2:
            continue
        tags = border_lines(dict(w.tags), w.id not in countries)
        if tags:
            out.append(([n.ref for n in w.nodes], tags))
    return out


# ---- land and sea (as mapsforge-creator) ---------------------------------------------------------


def land_polygons(land_dir: Path, box: tuple[float, float, float, float]):
    """The land in [box] (south, west, north, east): shapely polygons, clipped to it."""
    import shapefile
    import shapely
    from shapely.geometry import shape

    s, w, n, e = box
    shp = land_dir / "land-polygons-split-4326" / "land_polygons.shp"
    with shapefile.Reader(str(shp)) as sf:
        for item in sf.iterShapes(bbox=[w, s, e, n]):
            g = shape(item.__geo_interface__)
            if not g.is_valid:
                g = shapely.make_valid(g)
            c = shapely.clip_by_rect(g, w, s, e, n)
            for p in shapely.get_parts(c):
                if p.geom_type == "Polygon" and not p.is_empty and p.area > 0:
                    yield p
                elif p.geom_type in ("MultiPolygon", "GeometryCollection"):
                    for q in shapely.get_parts(p):
                        if q.geom_type == "Polygon" and not q.is_empty and q.area > 0:
                            yield q


class Extra:
    """The objects MapAround adds to an extract, written sorted (nodes, ways, relations) to a .pbf."""

    def __init__(self):
        self.nodes: list[tuple[int, float, float]] = []
        self.ways: list[tuple[int, list[int], dict[str, str]]] = []
        self.relations: list[tuple[int, list[tuple[str, int, str]], dict[str, str]]] = []
        self._next = {"n": NEW_ID, "w": NEW_ID, "r": NEW_ID}

    def _id(self, kind: str) -> int:
        i = self._next[kind]
        self._next[kind] = i + 1
        return i

    def ring(self, coords) -> list[int]:
        pts = [(round(x, 7), round(y, 7)) for x, y in coords]
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts.pop()
        ids: list[int] = []
        last = None
        for x, y in pts:
            if (x, y) == last:
                continue
            last = (x, y)
            i = self._id("n")
            self.nodes.append((i, x, y))
            ids.append(i)
        if len(ids) < 3:
            return []
        return ids + [ids[0]]

    def way(self, nodes: list[int], tags: dict[str, str]) -> int:
        i = self._id("w")
        self.ways.append((i, nodes, tags))
        return i

    def area(self, polygon, tags: dict[str, str]):
        outer = self.ring(polygon.exterior.coords)
        if not outer:
            return
        holes = [r for r in (self.ring(h.coords) for h in polygon.interiors) if r]
        if not holes:
            self.way(outer, tags)
            return
        members = [("w", self.way(outer, {}), "outer")] + [("w", self.way(h, {}), "inner") for h in holes]
        self.relations.append((self._id("r"), members, {"type": "multipolygon", **tags}))

    def write(self, path: Path):
        import osmium
        from osmium.osm import Location
        from osmium.osm.mutable import Node, Relation, Way

        if path.exists():
            path.unlink()
        w = osmium.SimpleWriter(str(path))
        try:
            for i, x, y in self.nodes:
                w.add_node(Node(id=i, location=Location(x, y), version=1))
            for i, nodes, tags in self.ways:
                w.add_way(Way(id=i, nodes=nodes, tags=tags, version=1))
            for i, members, tags in self.relations:
                w.add_relation(Relation(id=i, members=members, tags=tags, version=1))
        finally:
            w.close()


def extra_for(box, land_dir: Path, trails, borders=()) -> Extra:
    """The sea over the whole box, the land on it (natural=sea / nosea, as mapsforge's maps), the trail lines and the country border lines."""
    s, w, n, e = box
    x = Extra()
    sea = x.ring([(w, s), (w, n), (e, n), (e, s)])
    x.way(sea, {"area": "yes", "layer": "-5", "natural": "sea"})
    count = 0
    for p in land_polygons(land_dir, box):
        x.area(p, {"natural": "nosea", "layer": "-5"})
        count += 1
    for nodes, tags in trails:
        x.way(nodes, tags)
    for nodes, tags in borders:
        x.way(nodes, tags)
    log(f"  land: {count} polygons; trail lines: {len(trails)}; country border lines: {len(borders)}")
    return x


# ---- the map writer ------------------------------------------------------------------------------


def java_heap() -> str:
    """Most of the machine's memory for Java, leaving some for the system."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total_mb = int(line.split()[1]) // 1024
                    return f"{max(1024, total_mb - 3072)}m"
    except OSError:
        pass
    return "8g"


def write_map(tools: Path, pbf: Path, extra: Path, out: Path, box, kind: str, heap: str, threads: int):
    """Osmosis with the mapsforge writer on the classpath (it registers itself as a plugin)."""
    s, w, n, e = box
    sep = ";" if os.name == "nt" else ":"
    osmosis_lib = next((tools / "osmosis").glob("osmosis-*")) / "lib"
    classpath = sep.join([str(osmosis_lib / "*"), str(tools / "mapsforge-map-writer.jar")])
    java = os.path.join(os.environ["JAVA_HOME"], "bin", "java") if os.environ.get("JAVA_HOME") else "java"
    tmpdir = out.parent / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        java, f"-Xmx{heap}", f"-Djava.io.tmpdir={tmpdir}", "-cp", classpath, "org.openstreetmap.osmosis.core.Osmosis", "-q",
        "--rb", f"file={pbf}", "--rb", f"file={extra}", "--s", "--merge",
        "--tt", f"file={HERE / 'tag-transform.xml'}",
        "--mw", f"file={out}",
        f"bbox={s:.6f},{w:.6f},{n:.6f},{e:.6f}",
        f"map-start-position={(s + n) / 2:.6f},{(w + e) / 2:.6f}", "map-start-zoom=8",
        "tag-values=true", f"tag-conf-file={HERE / 'tag-mapping.xml'}",
        f"preferred-languages={LANGUAGES}", f"type={kind}", f"threads={threads}",
        f"comment={COMMENT}", "progress-logs=false",
    ]
    log("  " + " ".join(cmd[:2] + ["…"] + cmd[cmd.index("--mw"):]))
    subprocess.run(cmd, check=True)


# ---- one region ----------------------------------------------------------------------------------


def build_region(path: str, a, size: int | None = None) -> list[dict]:
    """Downloads and builds one region; returns its maps (one, or one per part across the antimeridian)."""
    t0 = time.time()
    name = path.rsplit("/", 1)[-1]
    work = Path(a.work) / path.replace("/", "__")
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        pbf = fetch(GEOFABRIK + path + "-latest.osm.pbf", work / f"{name}-latest.osm.pbf")
        md5 = fetch(GEOFABRIK + path + "-latest.osm.pbf.md5", work / f"{name}.md5").read_text().split()[0]
        if md5_of(pbf) != md5:
            raise RuntimeError("checksum mismatch")
        poly = fetch(GEOFABRIK + path + ".poly", work / f"{name}.poly")
        size = pbf.stat().st_size
        boxes = poly_boxes(poly.read_text(encoding="utf-8", errors="replace"))
        date = data_date(pbf)
        log(f"  {size / MB:.0f} MB of OSM data from {date}, {len(boxes)} area(s)")
        trails = trail_ways(pbf)
        borders = border_ways(pbf)
        kind = "ram" if size <= a.ram_mb * MB else "hd"
        results = []
        for i, box in enumerate(boxes):
            area = path if len(boxes) == 1 else f"{path}-{i + 1}"
            extra = work / f"extra-{i}.osm.pbf"
            extra_for(box, Path(a.land), trails, borders).write(extra)
            out = out_dir / file_name(area)
            tmp = out.with_name(out.name + ".tmp")
            try:
                write_map(Path(a.tools), pbf, extra, tmp, box, kind, a.heap or java_heap(), a.threads)
            except subprocess.CalledProcessError:
                if kind != "ram":
                    raise
                # Out of memory (dense regions: the Alps' trails): the same again, via disk.
                log("  in memory failed: again via disk")
                tmp.unlink(missing_ok=True)
                write_map(Path(a.tools), pbf, extra, tmp, box, "hd", a.heap or java_heap(), a.threads)
            tmp.replace(out)
            results.append({
                "region": path,
                "path": area,
                "file": out.name,
                "size": out.stat().st_size,
                "sha256": sha256_of(out),
                "date": date,
                "bbox": [round(v, 6) for v in box],
                "osmBytes": size,
                "minutes": round((time.time() - t0) / 60, 1),
            })
            log(f"  wrote {out.name}: {out.stat().st_size / MB:.1f} MB")
        return results
    finally:
        shutil.rmtree(work, ignore_errors=True)


def upload(tag: str, files: list[Path]):
    """Adds the maps to the GitHub release [tag] (gh CLI, GH_TOKEN), replacing files of the same name."""
    for f in files:
        for attempt in range(4):
            r = subprocess.run(["gh", "release", "upload", tag, str(f), "--clobber"])
            if r.returncode == 0:
                break
            if attempt == 3:
                raise RuntimeError(f"upload of {f.name} failed")
            time.sleep(30 * (attempt + 1))


def cmd_build(a):
    plan = json.loads(Path(a.plan).read_text())
    regions = plan["batches"][a.batch]
    results_dir = Path(a.results)
    results_dir.mkdir(parents=True, exist_ok=True)
    for path in regions:
        log(f"== {path}")
        record: dict = {"region": path}
        try:
            maps = build_region(path, a)
            files = [Path(a.out) / m["file"] for m in maps]
            if a.release:
                upload(a.release, files)
                for m in maps:
                    m["tag"] = a.release
                for f in files:
                    f.unlink()
            record["maps"] = maps
        except Exception as e:  # noqa: BLE001 - one region failing doesn't stop the others
            log(f"  FAILED: {e}")
            record["error"] = str(e)[:500]
        (results_dir / (path.replace("/", "__") + ".json")).write_text(json.dumps(record, indent=1))
        shutil.rmtree(Path(a.out) / "tmp", ignore_errors=True)


def cmd_region(a):
    for m in build_region(a.path, a):
        log(json.dumps(m))


# ---- tools and data ----------------------------------------------------------------------------


def cmd_setup(a):
    tools = Path(a.tools)
    tools.mkdir(parents=True, exist_ok=True)
    if not any((tools / "osmosis").glob("osmosis-*/lib")):
        z = fetch(OSMOSIS_URL, tools / "osmosis.zip")
        with zipfile.ZipFile(z) as f:
            f.extractall(tools / "osmosis")
        z.unlink()
    jar = tools / "mapsforge-map-writer.jar"
    if not jar.exists():
        fetch(WRITER_URL, jar)
    log(f"tools in {tools}")


def cmd_land(a):
    land = Path(a.land)
    if (land / "land-polygons-split-4326" / "land_polygons.shp").exists():
        log("land polygons already there")
        return
    z = Path(a.zip) if a.zip else fetch(LAND_URL, land / "land-polygons-split-4326.zip")
    with zipfile.ZipFile(z) as f:
        f.extractall(land)
    if not a.zip:
        z.unlink()
    log(f"land polygons in {land}")


# ---- planning: what to build, in which job ----------------------------------------------------


def pack(sizes: dict[str, int], job_bytes: int) -> list[list[str]]:
    """Regions shared out over jobs: first fit, biggest first, up to [job_bytes] of OSM data a job (a bigger region alone)."""
    batches: list[list[str]] = []
    loads: list[int] = []
    for r in sorted(sizes, key=lambda r: (-sizes[r], r)):
        s = sizes[r]
        for i in range(len(batches)):
            if loads[i] + s <= job_bytes:
                batches[i].append(r)
                loads[i] += s
                break
        else:
            batches.append([r])
            loads.append(s)
    return batches


def cmd_plan(a):
    regions = read_regions(Path(a.regions))
    only = set(a.only.split()) if a.only else None
    if only:
        unknown = only - set(regions)
        if unknown:
            raise SystemExit(f"not in {a.regions}: {' '.join(sorted(unknown))}")
        regions = [r for r in regions if r in only]
    sizes: dict[str, int | None] = {}
    for r in regions:
        sizes[r] = head_size(GEOFABRIK + r + "-latest.osm.pbf")
        time.sleep(0.2)
    build = [r for r in regions if sizes[r] is not None and sizes[r] <= a.max_mb * MB]
    too_big = [r for r in regions if sizes[r] is not None and sizes[r] > a.max_mb * MB]
    missing = [r for r in regions if sizes[r] is None]
    batches = pack({r: sizes[r] for r in build}, a.job_mb * MB)
    loads = [sum(sizes[r] for r in b) for b in batches]
    if len(batches) > 250:
        raise SystemExit(f"{len(batches)} jobs: more than a workflow can run, raise --job-mb")
    month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    plan = {
        "created": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tag": f"maps-{month}",
        "maxBytes": a.max_mb * MB,
        "regions": regions,
        "sizes": sizes,
        "batches": batches,
        "tooBig": too_big,
        "missing": missing,
    }
    Path(a.out).write_text(json.dumps(plan, indent=1))
    log(f"{len(build)} regions to build in {len(batches)} jobs ({sum(loads) / (1 << 30):.1f} GB of OSM data); "
        f"{len(too_big)} too big: {' '.join(too_big)}; {len(missing)} missing: {' '.join(missing)}")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"batches={json.dumps(list(range(len(batches))))}\n")
            f.write(f"tag={plan['tag']}\n")


# ---- the catalog the app reads ---------------------------------------------------------------


def cmd_catalog(a):
    """
    catalog.json: every region of the plan as built now, or as before if it failed this time;
    regions too big for the free build machines as needing funding. Regions not in this plan (a
    partial run) stay as they were; regions of regions.txt never built are listed as unavailable,
    and regions no longer in it are dropped.
    """
    plan = json.loads(Path(a.plan).read_text())
    repo = a.repo or os.environ.get("GITHUB_REPOSITORY", "")
    prev = json.loads(Path(a.previous).read_text()) if a.previous and Path(a.previous).exists() else {"maps": []}
    by_region: dict[str, list[dict]] = {}
    order: list[str] = []
    for m in prev.get("maps", []):
        r = m.get("region", m["path"])
        if r not in by_region:
            by_region[r] = []
            order.append(r)
        by_region[r].append(m)
    results: dict[str, dict] = {}
    for f in Path(a.results).rglob("*.json"):
        rec = json.loads(f.read_text())
        results[rec["region"]] = rec
    failed = []
    for r in plan["regions"]:
        if r not in by_region:
            order.append(r)
        rec = results.get(r)
        if rec and "maps" in rec:
            by_region[r] = [{
                "region": r,
                "path": m["path"],
                "url": f"https://github.com/{repo}/releases/download/{m['tag']}/{m['file']}" if m.get("tag") else m["file"],
                "size": m["size"],
                "sha256": m["sha256"],
                "date": m["date"],
                "bbox": m["bbox"],
            } for m in rec["maps"]]
        elif r in plan["tooBig"]:
            by_region[r] = [{"region": r, "path": r, "status": "needs-funding", "osmBytes": plan["sizes"][r]}]
        else:
            failed.append(r)
            old = [m for m in by_region.get(r, []) if "url" in m]
            by_region[r] = old or [{"region": r, "path": r, "status": "unavailable"}]
    listed = read_regions(Path(a.regions))
    for r in listed:
        if r not in by_region:
            by_region[r] = [{"region": r, "path": r, "status": "unavailable"}]
            order.append(r)
    keep = set(listed)
    maps = [m for r in sorted(order) if r in keep for m in by_region[r]]
    catalog = {
        "version": 1,
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attribution": "Map data (c) OpenStreetMap contributors, ODbL (openstreetmap.org/copyright)",
        "maps": maps,
    }
    Path(a.out).write_text(json.dumps(catalog, indent=1, ensure_ascii=False))
    ok = sum(1 for m in maps if "url" in m)
    log(f"catalog: {ok} maps, {sum(1 for m in maps if m.get('status') == 'needs-funding')} need funding, "
        f"{len(failed)} failed this time" + (f": {' '.join(failed[:30])}{' …' if len(failed) > 30 else ''}" if failed else ""))
    if a.prune:
        prune(prev, catalog)


def prune(prev: dict, catalog: dict):
    """Deletes map releases neither this catalog nor the previous one points to (downloads in progress finish)."""
    used = set()
    for c in (prev, catalog):
        for m in c.get("maps", []):
            url = m.get("url", "")
            if "/releases/download/" in url:
                used.add(url.split("/releases/download/")[1].split("/")[0])
    out = subprocess.run(["gh", "release", "list", "--limit", "200", "--json", "tagName"], capture_output=True, text=True, check=True)
    for rel in json.loads(out.stdout):
        tag = rel["tagName"]
        if tag.startswith("maps-20") and tag not in used:
            log(f"deleting release {tag}")
            subprocess.run(["gh", "release", "delete", tag, "--cleanup-tag", "--yes"], check=True)


# ---- command line --------------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup")
    s.add_argument("--tools", required=True)
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("land")
    s.add_argument("--land", required=True)
    s.add_argument("--zip", help="an already downloaded land-polygons-split-4326.zip")
    s.set_defaults(func=cmd_land)

    s = sub.add_parser("plan")
    s.add_argument("--regions", default=str(HERE / "regions.txt"))
    s.add_argument("--only", help="space-separated region paths (default: all)")
    s.add_argument("--max-mb", type=int, default=1500, help="biggest OSM extract the free build machines take")
    s.add_argument("--job-mb", type=int, default=800, help="OSM data per job")
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_plan)

    def build_args(s):
        s.add_argument("--tools", required=True)
        s.add_argument("--land", required=True)
        s.add_argument("--work", required=True)
        s.add_argument("--out", required=True)
        s.add_argument("--ram-mb", type=int, default=600, help="extracts up to this size are written in memory, bigger ones via disk")
        s.add_argument("--heap", help="Java heap, e.g. 12g (default: the machine's memory minus 3 GB)")
        s.add_argument("--threads", type=int, default=2)

    s = sub.add_parser("build")
    build_args(s)
    s.add_argument("--plan", required=True)
    s.add_argument("--batch", type=int, required=True)
    s.add_argument("--results", required=True)
    s.add_argument("--release", help="GitHub release tag to upload to (then the local files are deleted)")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("region")
    build_args(s)
    s.add_argument("path")
    s.set_defaults(func=cmd_region)

    s = sub.add_parser("catalog")
    s.add_argument("--regions", default=str(HERE / "regions.txt"))
    s.add_argument("--plan", required=True)
    s.add_argument("--results", required=True)
    s.add_argument("--previous")
    s.add_argument("--repo", help="owner/name (default: $GITHUB_REPOSITORY)")
    s.add_argument("--prune", action="store_true", help="delete map releases no catalog points to")
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_catalog)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
