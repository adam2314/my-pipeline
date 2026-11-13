# MapAround maps

Offline maps for [MapAround](https://github.com/adam2314/maparound), the offline map app for
hiking and off-road trips. OpenStreetMap data written as mapsforge `.map` files, rebuilt every
month on GitHub Actions and published as GitHub releases. The app's **Download maps** list reads
them from here.

They're like the maps from download.mapsforge.org (same regions, same file names), plus the
data MapAround's Topo style draws for the outdoors that those maps leave out:

- **Marked hiking trails**, from the `route=hiking|foot` relations: each path once per waymark
  colour (`trail=hiking`, `trail_colour`, `trail_offset`, `trail_n`, the route's `name` and `ref`),
  the way the app makes them from its own "Marked trails" download, so the app doesn't need that
  download for these maps.
- **Cliffs, arêtes, ridges, earth banks**, bare rock, sand, shingle, glaciers, saddles, rocks,
  waterfalls.
- **Paths and tracks**: `sac_scale` (hiking … difficult_alpine_hiking), `trail_visibility`,
  `mtb:scale` and `mtb:scale:uphill`, `surface`, `smoothness`, `4wd_only`, `ford`, and more `access`,
  `motor_vehicle`, `foot` and `bicycle` values; via ferratas.
- **Outdoor places**: wilderness huts, water points, wells and taps, cairns, guideposts, isolated
  dwellings and farms, archaeological sites, wayside crosses and shrines, gates, stiles and
  cattle grids. Springs from zoom 14 (mapsforge: 17).

The full list is [tag-mapping.xml](tag-mapping.xml) (mapsforge's, with the additions at the end).
Tags pass through mapsforge-creator's tag transform first ([tag-transform.xml](tag-transform.xml)),
as for the mapsforge.org maps, except that `smoothness` is kept. Any app that reads mapsforge
maps can use them; the extra data shows with a style that draws it.

## How they're built

[maps.py](maps.py), for each region in [regions.txt](regions.txt):

1. Downloads the region's OpenStreetMap extract (`.osm.pbf`, checksum checked) and outline
   (`.poly`) from [Geofabrik](https://download.geofabrik.de).
2. Makes the trail lines from the route relations (pyosmium), and the land and sea for the map's
   area from the [land polygons](https://osmdata.openstreetmap.de/data/land-polygons.html)
   (`natural=nosea` / `natural=sea`, as mapsforge-creator does), into a small extra `.pbf`.
3. Runs [Osmosis](https://github.com/openstreetmap/osmosis) with the
   [mapsforge map writer](https://github.com/mapsforge/mapsforge/blob/master/docs/Getting-Started-Map-Writer.md):
   both files merged, the tag transform and the tag mapping, names in en, nl, fr, de, it, es (as the
   mapsforge.org maps), `tag-values=true` (map format v5). Extracts up to 600 MB are written in
   memory, bigger ones via disk.

Regions that cross the antimeridian become two maps (`…-1`, `…-2`), as on mapsforge.org.

The regions are mapsforge's list (from
[mapsforge-creator](https://github.com/mapsforge/mapsforge-creator/tree/master/regions)), plus
Poland's provinces and England's counties, which mapsforge offers only whole.

## Releases

[.github/workflows/maps.yml](.github/workflows/maps.yml) runs on the 16th of each month on
GitHub's free machines (4 CPUs, 16 GB of memory, up to 6 hours a job, 20 jobs at a time):

- **plan**: the size of each region's extract (Geofabrik), and the regions shared out over jobs of
  about 800 MB of OpenStreetMap data each.
- **build** (one job per batch): builds its regions and uploads each map to that month's release,
  **`maps-YYYY-MM`** (GitHub allows files up to 2 GiB each). A region that fails doesn't stop the
  others.
- **catalog**: writes **`catalog.json`** to the **`maps-latest`** release
  (`https://github.com/adam2314/my-pipeline/releases/download/maps-latest/catalog.json`) and to the
  **`catalog`** branch (`https://raw.githubusercontent.com/adam2314/my-pipeline/catalog/catalog.json`,
  which browsers may read from other sites, unlike release downloads: the web app). A region
  whose build failed keeps last month's map. Then it deletes map releases that neither this catalog
  nor the previous one points to, so downloads in progress can finish.

Run it by hand from the Actions tab (**Maps → Run workflow**): **Regions to build** takes paths
from regions.txt (e.g. `europe/malta asia/israel-and-palestine`), **Biggest extract** the size
limit. A partial run changes only those regions in the catalog; regions never built are listed as
unavailable.

### Too big for the free machines: needs funding

Extracts over 1,500 MB aren't built; the catalog lists them as `needs-funding`, and the app shows
"Not available: too big for the free build machines, needs funding". In November 2026 these were
the whole-country files of France, Germany, Italy, Great Britain, England, Poland, the Alps, Japan,
India, China, Indonesia, Brazil and the four US groupings. All of them can be downloaded as smaller
regions (states, provinces, counties), and meanwhile the app offers mapsforge.org's whole-country
file instead (see below).

Building them needs a machine with more memory and disk than GitHub's free ones (a self-hosted
runner, or a rented server, with `--max-mb` raised). The first full run will also show how long
the biggest regions take and whether they fit; if some fail, lower `--max-mb` (they're then listed
as needing funding) or `--ram-mb` (written via disk instead).

### catalog.json

```json
{
 "version": 1,
 "generated": "2026-11-16T09:12:00Z",
 "attribution": "Map data (c) OpenStreetMap contributors, ODbL (openstreetmap.org/copyright)",
 "maps": [
  {"region": "europe/austria", "path": "europe/austria",
   "url": "https://github.com/adam2314/my-pipeline/releases/download/maps-2026-11/europe__austria.map",
   "size": 754974720, "sha256": "…", "date": "2026-11-12T21:23:05Z", "bbox": [46.27, 9.43, 49.12, 17.26]},
  {"region": "europe/germany", "path": "europe/germany", "status": "needs-funding", "osmBytes": 4881422107},
  {"region": "europe/malta", "path": "europe/malta", "status": "unavailable"}
 ]
}
```

- `path`: where the map sits in the list (folders by `/`); the file name is the path with `__`
  for `/` (`europe__austria.map`), as the app stores downloaded maps.
- `date`: when its OpenStreetMap data is from. The app offers **Update** when it's newer than the
  downloaded file; maps from mapsforge.org have the same file names, so they're offered an update to
  these.
- `bbox`: south, west, north, east.
- No `url`: not available, with `status` `needs-funding` or `unavailable` (not built yet, or its
  build failed and there's no earlier one).

## What the app relies on

These are shared with [MapAround](https://github.com/adam2314/maparound); change them together:

- The catalog's address and format (the app's `MapAroundCatalog`).
- `MapAround maps` in each map file's comment (the app's `MapAroundMaps.MARKER`): with it, the
  app's ⋮ menu says "Marked trails: in this map", and the online trail overlay isn't drawn over it.
- The trail lines' tags, made like the app's `TrailTags` (colour from `osmc:symbol` or `colour`, up
  to 4 colours side by side, the most important route's name and ref).
- **Paths and file names as on mapsforge.org.** In the transition the app merges mapsforge.org's
  list into this one (`MapAroundCatalog.merge`): a region without a `url` here (`needs-funding` or
  `unavailable`) is offered with mapsforge.org's file of the same path, labelled as a basic map
  without marked trails, and regions only mapsforge.org has are listed the same way. A switch in
  the app shows the whole mapsforge.org list instead. An installed mapsforge.org map (no `MapAround
  maps` comment) is offered this one as an update.

## Building a region on your computer

Python 3.10+ with `pip install osmium shapely pyshp`, and Java 17+:

```bash
python maps.py setup --tools build/tools     # Osmosis and the map writer
python maps.py land --land build/land        # the land polygons (~1 GB)
python maps.py region europe/malta --tools build/tools --land build/land --work build/work --out build/maps
```

Then copy `build/maps/europe__malta.map` to the phone and import it in MapAround (Offline →
folder button).

Tests: `python -m unittest` (the parts that don't need Osmosis).

## Licences

- The maps: data © OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright); the
  maps are a derived database under the same licence, so credit OpenStreetMap wherever they're
  shown. The land polygons are from OpenStreetMap too (osmdata.openstreetmap.de, ODbL).
- [tag-mapping.xml](tag-mapping.xml) and [tag-transform.xml](tag-transform.xml) are based on
  mapsforge's and mapsforge-creator's (LGPL-3.0).
- The build runs Osmosis (public domain; its libraries have their own licences) and the mapsforge
  map writer (LGPL-3.0); they aren't part of this repository.
- The rest of this repository: GPL-3.0 ([LICENSE](LICENSE)), like MapAround.
