"""Tests for maps.py's parts that don't need Osmosis or the network: python -m unittest"""

import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import maps

HAS_OSMIUM = all(importlib.util.find_spec(m) for m in ("osmium", "shapely", "shapefile"))


class Area(unittest.TestCase):
    def test_one_box_a_little_bigger(self):
        poly = "liechtenstein\n1\n   9.47  47.05\n   9.63  47.27\n   9.52  47.20\nEND\nEND\n"
        (s, w, n, e), = maps.poly_boxes(poly)
        self.assertAlmostEqual(s, 46.95)
        self.assertAlmostEqual(w, 9.37)
        self.assertAlmostEqual(n, 47.37)
        self.assertAlmostEqual(e, 9.73)

    def test_holes_belong_to_their_part(self):
        poly = "x\npolygon_a\n   9.4  47.0\n   9.7  47.3\nEND\n!2\n   9.5  47.1\n   9.6  47.2\nEND\nEND\n"
        self.assertEqual(1, len(maps.poly_boxes(poly)))

    def test_across_the_antimeridian_two_boxes(self):
        poly = "fiji\n1\n   179.0  -17.0\n   179.9  -16.0\nEND\n2\n   -179.9  -17.0\n   -178.5  -16.0\nEND\nEND\n"
        boxes = maps.poly_boxes(poly)
        self.assertEqual(2, len(boxes))
        self.assertEqual(180.0, boxes[0][3])
        self.assertEqual(-180.0, boxes[1][1])

    def test_parts_on_one_side_one_box(self):
        poly = "x\n1\n   10  50\n   11  51\nEND\n2\n   12  52\n   13  53\nEND\nEND\n"
        (s, w, n, e), = maps.poly_boxes(poly)
        self.assertAlmostEqual(w, 9.9)
        self.assertAlmostEqual(n, 53.1)

    def test_file_names_as_the_app_stores_them(self):
        self.assertEqual("europe__germany__bayern.map", maps.file_name("europe/germany/bayern"))
        self.assertEqual("russia__far-eastern-fed-district-1.map", maps.file_name("russia/far-eastern-fed-district-1"))


class Trails(unittest.TestCase):
    def test_colours(self):
        self.assertEqual("red", maps.trail_colour("red:white:red_bar", None))
        self.assertEqual("blue", maps.trail_colour("darkblue:white:blue_stripe", "red"))
        self.assertEqual("red", maps.trail_colour(None, "Red"))
        self.assertEqual("blue", maps.trail_colour(None, "#1133cc"))
        self.assertEqual("other", maps.trail_colour("teal:white", "teal"))
        self.assertEqual("other", maps.trail_colour(None, None))

    def test_lines_one_per_colour_side_by_side(self):
        lines = maps.trail_lines([
            ("Local loop", "", 3, "blue"),
            ("Via Alpina", "R", 0, "red"),
            ("Regional", "02", 2, "red"),
        ])
        self.assertEqual(["red", "blue"], [t["trail_colour"] for t in lines])
        self.assertEqual(["l05", "r05"], [t["trail_offset"] for t in lines])
        # The most important route's name and ref, on the first line only.
        self.assertEqual({"trail": "hiking", "trail_colour": "red", "trail_offset": "l05", "trail_n": "2", "name": "Via Alpina", "ref": "R"}, lines[0])
        self.assertNotIn("name", lines[1])

    def test_dirt_roads_as_the_style_draws_them(self):
        def track(**t):
            return maps.trail_track({"highway": "track", **t})
        self.assertEqual("minor", track())
        self.assertEqual("any", track(tracktype="grade2"))
        self.assertEqual("high", track(smoothness="bad", tracktype="grade1"))  # smoothness first
        self.assertEqual("4x4", track(**{"4wd_only": "yes", "smoothness": "good"}))  # 4wd_only before both
        self.assertEqual("4x4", track(tracktype="grade5"))
        self.assertIsNone(track(smoothness="impassable"))  # a dotted line, no edges
        self.assertIsNone(track(tunnel="yes"))
        self.assertIsNone(maps.trail_track({"highway": "path"}))

    def test_lines_on_a_dirt_road_know_it(self):
        lines = maps.trail_lines([("A", "", 3, "red"), ("B", "", 3, "blue")], "4x4")
        self.assertEqual(["4x4", "4x4"], [t["trail_track"] for t in lines])
        self.assertNotIn("trail_track", maps.trail_lines([("A", "", 3, "red")])[0])

    def test_at_most_four_colours(self):
        colours = ["red", "blue", "green", "yellow", "black"]
        lines = maps.trail_lines([("", "", 4, c) for c in colours])
        self.assertEqual(4, len(lines))
        self.assertEqual(["l15", "l05", "r05", "r15"], [t["trail_offset"] for t in lines])
        self.assertEqual("4", lines[0]["trail_n"])
        self.assertEqual([], maps.trail_lines([]))


class Borders(unittest.TestCase):
    def test_lines_of_a_country_border_say_so(self):
        self.assertEqual({"boundary": "administrative", "admin_level": "2"},
                         maps.border_lines({"boundary": "administrative", "name": "Lebanon - Israel"}, False))
        # Disputed: by the line's own tags, or in disputed borders only.
        for tags, only in [({"boundary": "administrative", "disputed": "yes"}, False),
                           ({"boundary": "disputed"}, False), ({"boundary": "administrative", "admin_level": "4"}, True)]:
            self.assertEqual("yes", maps.border_lines(tags, only)["disputed"])
        # Already saying so, at sea, the coast: no copy.
        self.assertIsNone(maps.border_lines({"boundary": "administrative", "admin_level": "2"}, False))
        self.assertIsNone(maps.border_lines({"boundary": "administrative", "maritime": "yes"}, False))
        self.assertIsNone(maps.border_lines({"natural": "coastline"}, False))

    def test_the_map_keeps_disputed(self):
        mapping = (maps.HERE / "tag-mapping.xml").read_text(encoding="utf-8")
        self.assertRegex(mapping, 'key="disputed"[^>]*value="yes"')

    @unittest.skipUnless(HAS_OSMIUM, "needs pip install osmium shapely pyshp")
    def test_from_the_countries_relations(self):
        import osmium
        from osmium.osm import Location
        from osmium.osm.mutable import Node, Relation, Way

        path = Path(tempfile.mkdtemp()) / "borders.osm.pbf"
        w = osmium.SimpleWriter(str(path))
        adm = {"boundary": "administrative"}
        try:
            for i in range(1, 9):
                w.add_node(Node(id=i, location=Location(35.0 + i / 10, 32.0), version=1))
            w.add_way(Way(id=10, nodes=[1, 2], tags={**adm, "name": "Lebanon - Israel"}, version=1))
            w.add_way(Way(id=11, nodes=[2, 3], tags={**adm, "admin_level": "2"}, version=1))  # says so
            w.add_way(Way(id=12, nodes=[3, 4], tags={**adm, "maritime": "yes"}, version=1))  # at sea
            w.add_way(Way(id=13, nodes=[4, 5], tags=adm, version=1))  # a country's, and a disputed territory's
            w.add_way(Way(id=14, nodes=[5, 6], tags={**adm, "admin_level": "4"}, version=1))  # a line of control only
            w.add_way(Way(id=15, nodes=[6, 7], tags=adm, version=1))  # a district's
            w.add_way(Way(id=16, nodes=[7, 8], tags={**adm, "disputed": "yes"}, version=1))  # a country's, disputed
            country = {"type": "boundary", **adm, "admin_level": "2"}
            w.add_relation(Relation(id=20, members=[("w", i, "outer") for i in (10, 11, 12, 13, 16)], tags=country, version=1))
            w.add_relation(Relation(id=21, members=[("w", 15, "outer")], tags={**country, "admin_level": "6"}, version=1))
            w.add_relation(Relation(id=22, members=[("w", 13, "outer"), ("w", 14, "")],
                                    tags={"type": "boundary", "boundary": "disputed", "admin_level": "2"}, version=1))
        finally:
            w.close()
        line = {**adm, "admin_level": "2"}
        dashed = {**line, "disputed": "yes"}
        self.assertEqual([([1, 2], line), ([4, 5], line), ([5, 6], dashed), ([7, 8], dashed)], maps.border_ways(path))


class Plan(unittest.TestCase):
    def test_jobs_filled_biggest_first(self):
        mb = maps.MB
        sizes = {"a": 900 * mb, "b": 500 * mb, "c": 300 * mb, "d": 200 * mb, "e": 100 * mb}
        batches = maps.pack(sizes, 800 * mb)
        self.assertEqual([["a"], ["b", "c"], ["d", "e"]], batches)
        self.assertEqual(sorted(sizes), sorted(r for b in batches for r in b))

    def test_outdoor_tags_with_their_osm_values(self):
        # mapsforge-creator's transform doesn't rename these (its rules are off): OSM's own values.
        mapping = (maps.HERE / "tag-mapping.xml").read_text(encoding="utf-8")
        for k, v in [("sac_scale", "mountain_hiking"), ("mtb:scale", "3"), ("trail_visibility", "bad"),
                     ("smoothness", "very_bad"), ("4wd_only", "yes"), ("natural", "cliff"), ("trail", "hiking")]:
            self.assertIn(f'key="{k}"', mapping)
            self.assertRegex(mapping, f'key="{k}"[^>]*value="{v}"')

    def test_regions_file(self):
        regions = maps.read_regions(maps.HERE / "regions.txt")
        self.assertEqual(len(regions), len(set(regions)))
        self.assertIn("asia/israel-and-palestine", regions)
        self.assertIn("europe/poland/malopolskie", regions)
        self.assertTrue(all(r == r.strip() and not r.endswith("/") for r in regions))


class Catalog(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.regions = self.dir / "regions.txt"
        self.regions.write_text("# test\neurope/liechtenstein\neurope/malta\neurope/germany\neurope/andorra\n")
        self.plan = self.dir / "plan.json"
        self.plan.write_text(json.dumps({
            "regions": ["europe/liechtenstein", "europe/malta", "europe/germany"],
            "sizes": {"europe/liechtenstein": 3_000_000, "europe/malta": 9_000_000, "europe/germany": 4_900_000_000},
            "tooBig": ["europe/germany"],
        }))

    def built(self, run: str, region: str, tag: str | None = None, error: str | None = None):
        d = self.dir / run
        d.mkdir(exist_ok=True)
        rec = {"region": region}
        if error:
            rec["error"] = error
        else:
            rec["maps"] = [{"region": region, "path": region, "file": maps.file_name(region), "size": 123, "sha256": "aa",
                            "date": "2026-11-12T21:23:05Z", "bbox": [1, 2, 3, 4], "tag": tag}]
        (d / (region.replace("/", "__") + ".json")).write_text(json.dumps(rec))
        return d

    def catalog(self, results: Path, previous: Path | None, out: str) -> dict:
        a = argparse.Namespace(plan=str(self.plan), results=str(results), previous=str(previous) if previous else None,
                               repo="o/r", prune=False, out=str(self.dir / out), regions=str(self.regions))
        maps.cmd_catalog(a)
        return json.loads((self.dir / out).read_text(encoding="utf-8"))

    def test_a_month_then_the_next(self):
        self.built("r1", "europe/liechtenstein", tag="maps-2026-11")
        r1 = self.built("r1", "europe/malta", error="out of memory")
        c1 = self.catalog(r1, None, "c1.json")
        by = {m["path"]: m for m in c1["maps"]}
        self.assertEqual("https://github.com/o/r/releases/download/maps-2026-11/europe__liechtenstein.map", by["europe/liechtenstein"]["url"])
        self.assertEqual("unavailable", by["europe/malta"]["status"])
        self.assertEqual("needs-funding", by["europe/germany"]["status"])
        # In regions.txt but not in this run's plan: listed, not built yet.
        self.assertEqual("unavailable", by["europe/andorra"]["status"])

        # Next month Liechtenstein fails (it keeps its map), Malta is built.
        self.built("r2", "europe/liechtenstein", error="disk full")
        r2 = self.built("r2", "europe/malta", tag="maps-2026-12")
        c2 = self.catalog(r2, self.dir / "c1.json", "c2.json")
        by = {m["path"]: m for m in c2["maps"]}
        self.assertIn("maps-2026-11/", by["europe/liechtenstein"]["url"])
        self.assertIn("maps-2026-12/", by["europe/malta"]["url"])

    def test_regions_dropped_from_the_list_leave_the_catalog(self):
        r1 = self.built("r1", "europe/liechtenstein", tag="maps-2026-11")
        self.catalog(r1, None, "c1.json")
        self.regions.write_text("europe/malta\n")
        c2 = self.catalog(self.dir / "empty", self.dir / "c1.json", "c2.json")
        self.assertEqual(["europe/malta"], [m["path"] for m in c2["maps"]])


@unittest.skipUnless(HAS_OSMIUM, "needs pip install osmium shapely pyshp")
class ExtraFile(unittest.TestCase):
    def test_written_sorted_and_read_back(self):
        import osmium
        from shapely.geometry import Polygon

        x = maps.Extra()
        x.way(x.ring([(0, 0), (0, 1), (1, 1), (1, 0)]), {"natural": "sea", "area": "yes", "layer": "-5"})
        x.area(Polygon([(0.1, 0.1), (0.1, 0.9), (0.9, 0.9), (0.9, 0.1)], [[(0.4, 0.4), (0.4, 0.6), (0.6, 0.6), (0.6, 0.4)]]),
               {"natural": "nosea", "layer": "-5"})
        x.way([1, 2, 3], {"trail": "hiking", "trail_colour": "red"})
        path = Path(tempfile.mkdtemp()) / "extra.osm.pbf"
        x.write(path)
        seen = []
        for o in osmium.FileProcessor(str(path)):
            seen.append((o.type_str(), o.id, dict(o.tags)))
        kinds = [k for k, _, _ in seen]
        self.assertEqual(kinds, sorted(kinds, key="nwr".index))
        ids = {k: [i for kk, i, _ in seen if kk == k] for k in "nwr"}
        for k in "nwr":
            self.assertEqual(ids[k], sorted(ids[k]))
            self.assertTrue(all(i >= maps.NEW_ID for i in ids[k]))
        self.assertEqual(12, len(ids["n"]))
        # The land with a hole is a multipolygon relation.
        rel = [t for k, _, t in seen if k == "r"]
        self.assertEqual([{"type": "multipolygon", "natural": "nosea", "layer": "-5"}], rel)


if __name__ == "__main__":
    unittest.main()
