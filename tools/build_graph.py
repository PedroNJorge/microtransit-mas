#!/usr/bin/env python3
"""
tools/build_graph.py
OFFLINE build step: OpenStreetMap -> compact config/graph.json for the simulation.

    pip install osmnx
    python tools/build_graph.py --out config/graph.json                 # the 8 default hubs
    python tools/build_graph.py --stations "Trindade,Bolhão,Casa da Música"  # custom subset
    python tools/build_graph.py --auto-hubs --max-hubs 5                # greedy, centre-biased

The simulation never touches OSM or osmnx at runtime; it only loads the JSON.
Commit config/graph.json: every run and every figure in a report then uses the
same network. The output is deterministic for a given OSM download (no
randomness anywhere); `meta` records when the data was fetched.

Pipeline
  1. Locate the hub stations, then download the drivable network inside their convex
     hull + --buffer (or load --graphml). Senhora da Hora (Matosinhos) and Campanhã
     are ~6 km apart, so the area is derived from the hubs, not a fixed circle.
  2. Drop service roads, keep the largest strongly-connected component.
  3. Per-edge travel time = length / (speed * --speed-factor), seconds.
  4. Project to metres, merge intersection clusters (--tolerance), collapse to a
     DiGraph (parallel edges -> fastest; one-way streets preserved).
  5. Fetch Metro do Porto stations, snap the chosen hubs to the nearest node.
  6. Tag nodes: hub / residential / junction, plus a zone (parish) for equity metrics.
  7. Write JSON and print a sanity summary.

Output schema (all times in seconds, x/y in projected metres):
{
  "meta":  {...},
  "nodes": [{"id","x","y","lat","lon","type","zone"}],
  "edges": [{"from","to","travel_time","length"}],          # directed
  "hubs":  [{"id","name","role","coverage_radius","capacity","snap_m"}]  # id == a node id
}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import networkx as nx
import numpy as np

log = logging.getLogger("build_graph")

# Urban defaults (km/h) used where OSM has no maxspeed tag.
URBAN_SPEEDS = {
    "motorway": 80, "motorway_link": 50, "trunk": 60, "trunk_link": 40,
    "primary": 50, "primary_link": 35, "secondary": 45, "secondary_link": 30,
    "tertiary": 40, "tertiary_link": 30, "unclassified": 30,
    "residential": 30, "living_street": 20, "service": 20,
}
RESIDENTIAL_HIGHWAYS = {"residential", "living_street"}


@dataclass
class HubSpec:
    name: str                           # matched against OSM station names (accent-insensitive)
    role: str = ""
    coverage_radius: float = 600.0      # TRAVEL SECONDS (not metres)
    capacity: int = 6


# The 8 Porto hubs. coverage_radius / capacity are tunable PLACEHOLDERS, not derived from data:
# central hubs are close together (small, overlapping catchments); peripheral hubs are larger.
DEFAULT_HUBS: List[HubSpec] = [
    HubSpec("Trindade", "primary anchor / central interchange", 420, 10),
    HubSpec("Campanhã", "rail interface (east)", 600, 8),
    HubSpec("Casa da Música", "bus/metro interface (west)", 600, 6),
    HubSpec("São Bento", "historic centre entry", 420, 6),
    HubSpec("Bolhão", "retail/commercial centre", 420, 6),
    HubSpec("Aliados", "central civic node", 420, 6),
    HubSpec("Campo 24 de Agosto", "eastern buffer", 480, 6),
    HubSpec("Senhora da Hora", "western park-and-ride", 720, 8),
]


@dataclass
class Params:
    hubs: List[HubSpec] = field(default_factory=lambda: [replace(h) for h in DEFAULT_HUBS])
    buffer_m: float = 1200.0            # padding around the hubs' convex hull
    station_search_center: Tuple[float, float] = (41.155, -8.62)   # lat, lon (Porto, mid-span)
    station_search_dist_m: float = 9000.0   # bbox half-width for the station query only
    auto_hubs: bool = False             # greedy centre-biased choice instead of `hubs`
    max_hubs: int = 5                   # auto mode only
    min_hub_sep_m: float = 800.0        # auto mode only (explicit hubs may be close together)
    max_snap_m: float = 300.0           # station -> nearest road node limit
    speed_factor: float = 0.85          # urban delay factor applied to free-flow speed
    tolerance_m: float = 15.0           # intersection consolidation (0 = off)
    drop_highways: List[str] = field(default_factory=lambda: ["service"])
    parish_level: str = "8"             # OSM admin_level of Portuguese freguesias
    zone_min_nodes: int = 30            # zones with fewer nodes are merged into "other"
    max_nodes_warn: int = 8000


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def slug(s: str) -> str:
    return norm(s).replace(" ", "_") or "station"


def _hw_set(v) -> set:
    if v is None:
        return set()
    return {str(x) for x in v} if isinstance(v, (list, tuple, set)) else {str(v)}


# --------------------------------------------------------------------------
# network I/O (the only functions that touch the internet)
# --------------------------------------------------------------------------

def fetch_network(polygon):
    import osmnx as ox
    ox.settings.use_cache = True
    log.info("Downloading drivable network (%.1f km^2 area)", _area_km2(polygon))
    return ox.graph_from_polygon(polygon, network_type="drive")


def _area_km2(polygon) -> float:
    import geopandas as gpd
    g = gpd.GeoSeries([polygon], crs="EPSG:4326")
    return float(g.to_crs(g.estimate_utm_crs()).area.iloc[0] / 1e6)


def filter_metro(gdf):
    """Keep Metro do Porto stations. Prefers operator/network match; falls back to
    station=subway|light_rail. Returns [(name, lat, lon)]."""
    import pandas as pd
    if gdf is None or len(gdf) == 0 or "name" not in gdf:
        return []
    gdf = gdf[gdf["name"].notna()]

    def has(col, text):
        if col not in gdf:
            return pd.Series(False, index=gdf.index)
        return gdf[col].fillna("").astype(str).str.lower().str.contains(text, regex=False)

    sel = gdf[has("operator", "metro do porto") | has("network", "metro do porto")]
    if len(sel) == 0 and "station" in gdf:
        sel = gdf[gdf["station"].isin(["subway", "light_rail"])]
    return [(str(r["name"]), float(r.geometry.centroid.y), float(r.geometry.centroid.x))
            for _, r in sel.iterrows()]


def fetch_stations(p: Params):
    import osmnx as ox
    log.info("Downloading station candidates")
    tags = {"railway": ["station", "halt", "stop"], "station": ["subway", "light_rail"]}
    gdf = ox.features_from_point(p.station_search_center, tags=tags, dist=p.station_search_dist_m)
    st = filter_metro(gdf)
    if not st:
        raise RuntimeError(
            "No Metro do Porto stations found. Check OSM tags, or pass "
            "--stations-csv name,lat,lon as a fallback.")
    return st


def fetch_parishes(polygon, p: Params):
    import osmnx as ox
    log.info("Downloading parish boundaries (admin_level=%s)", p.parish_level)
    gdf = ox.features_from_polygon(
        polygon, tags={"boundary": "administrative", "admin_level": p.parish_level})
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])]
    if not len(gdf):
        return None
    return clean_parishes(gdf[["name", "geometry"]].dropna(subset=["name"]).copy())


# --------------------------------------------------------------------------
# pure processing (no network): unit-testable
# --------------------------------------------------------------------------

def collapse_to_digraph(Gc) -> nx.DiGraph:
    """MultiDiGraph -> DiGraph. Parallel edges keep the fastest; self-loops dropped;
    each node records the union of highway tags of all incident edges."""
    D = nx.DiGraph()
    hw: Dict = {}
    for n, d in Gc.nodes(data=True):
        D.add_node(n, x=float(d["x"]), y=float(d["y"]))
        hw[n] = set()
    for u, v, d in Gc.edges(data=True):
        if u == v:
            continue
        tags = _hw_set(d.get("highway"))
        hw[u] |= tags
        hw[v] |= tags
        tt = max(float(d["travel_time"]), 1.0)
        if D.has_edge(u, v) and D[u][v]["travel_time"] <= tt:
            continue
        D.add_edge(u, v, travel_time=tt, length=float(d.get("length", 0.0)))
    nx.set_node_attributes(D, hw, "highways")
    return D


def largest_scc(D: nx.DiGraph) -> nx.DiGraph:
    comp = max(nx.strongly_connected_components(D), key=len)
    dropped = D.number_of_nodes() - len(comp)
    if dropped:
        log.info("Dropped %d nodes outside the largest strongly-connected component", dropped)
    return D.subgraph(comp).copy()


def merge_stations(stations: Sequence[Tuple[str, float, float]]) -> Dict[str, list]:
    """Group platform/entrance rows that share a (normalised) name."""
    merged: Dict[str, list] = {}
    for name, a, b in stations:
        merged.setdefault(norm(name), []).append((name, a, b))
    return merged


def find_key(keys: Sequence[str], wanted: str) -> str:
    """Resolve a hub name to ONE merged-station key: exact match first, else substring.
    Raises KeyError if absent and ValueError if ambiguous."""
    w = norm(wanted)
    if w in keys:
        return w
    hits = sorted(k for k in keys if w in k)
    if not hits:
        raise KeyError(f"Station {wanted!r} not found. Available: {sorted(keys)}")
    if len(hits) > 1:
        raise ValueError(f"Station {wanted!r} is ambiguous: {hits}")
    return hits[0]


def hub_area_polygon(stations: Sequence[Tuple[str, float, float]], specs: Sequence[HubSpec],
                     buffer_m: float):
    """Lat/lon polygon: convex hull of the hub stations buffered by buffer_m metres."""
    import geopandas as gpd
    from shapely.geometry import MultiPoint
    merged = merge_stations(stations)
    pts = []
    for spec in specs:
        rows = merged[find_key(list(merged), spec.name)]
        pts.append((float(np.mean([r[2] for r in rows])), float(np.mean([r[1] for r in rows]))))
    g = gpd.GeoSeries([MultiPoint(pts).convex_hull], crs="EPSG:4326")
    return g.to_crs(g.estimate_utm_crs()).buffer(buffer_m).to_crs("EPSG:4326").iloc[0]


def circle_polygon(center: Tuple[float, float], dist_m: float):
    import geopandas as gpd
    from shapely.geometry import Point
    g = gpd.GeoSeries([Point(center[1], center[0])], crs="EPSG:4326")
    return g.to_crs(g.estimate_utm_crs()).buffer(dist_m).to_crs("EPSG:4326").iloc[0]


def select_hubs(node_ids: Sequence, xy: np.ndarray, stations_xy: Sequence[Tuple[str, float, float]],
                center_xy: Tuple[float, float], p: Params) -> List[dict]:
    """Snap stations to graph nodes and pick hubs. Deterministic.
    Explicit mode (default): every hub in p.hubs must be found, within max_snap_m of a road
    node, on a distinct node, else an error (never silently fewer hubs).
    Auto mode: greedy centre-biased choice honouring min_hub_sep_m / max_hubs."""
    merged = merge_stations(stations_xy)

    def snap(key):
        rows = merged[key]
        x = float(np.mean([r[1] for r in rows]))
        y = float(np.mean([r[2] for r in rows]))
        d = np.hypot(xy[:, 0] - x, xy[:, 1] - y)
        i = int(d.argmin())
        return {"name": rows[0][0], "node": node_ids[i], "snap_m": float(d[i]),
                "dist_center": math.hypot(x - center_xy[0], y - center_xy[1]),
                "pos": (float(xy[i, 0]), float(xy[i, 1]))}

    out: List[dict] = []
    if not p.auto_hubs:
        used: Dict = {}
        for spec in p.hubs:
            c = snap(find_key(list(merged), spec.name))
            if c["snap_m"] > p.max_snap_m:
                raise ValueError(f"Hub {spec.name!r} is {c['snap_m']:.0f} m from the nearest road "
                                 f"node (> {p.max_snap_m:.0f}); raise --max-snap or fix the data")
            if c["node"] in used:
                raise ValueError(f"Hubs {used[c['node']]!r} and {spec.name!r} snap to the same "
                                 f"road node; lower --tolerance")
            used[c["node"]] = spec.name
            out.append({**c, "name": spec.name, "role": spec.role,
                        "coverage_radius": spec.coverage_radius, "capacity": spec.capacity})
        return out

    cands = []
    for key in sorted(merged):
        c = snap(key)
        if c["snap_m"] > p.max_snap_m:
            log.warning("Station %r is %.0f m from the nearest road node (> %.0f): skipped",
                        c["name"], c["snap_m"], p.max_snap_m)
            continue
        cands.append(c)
    chosen: List[dict] = []
    for c in sorted(cands, key=lambda c: (c["dist_center"], c["name"])):
        if len(chosen) >= p.max_hubs:
            break
        if c["node"] in {o["node"] for o in chosen}:
            continue
        if all(math.dist(c["pos"], o["pos"]) >= p.min_hub_sep_m for o in chosen):
            chosen.append(c)
    if not chosen:
        raise RuntimeError("No hubs selected")
    d = HubSpec("")
    return [{**c, "role": "", "coverage_radius": d.coverage_radius, "capacity": d.capacity}
            for c in chosen]


def clean_parishes(gdf, contain_ratio: float = 0.9, cover_ratio: float = 0.5):
    """Drop container polygons (municipalities / super-parishes that leak in at the same
    admin_level). Polygon B is 'inside' A when >= contain_ratio of B's area lies in A; A is
    dropped when the polygons inside it cover >= cover_ratio of A's own area. (A parish that
    merely holds a small enclave is kept.) Logs a table (name, area) so bad OSM data is
    visible. Returns a GeoDataFrame with an `area_km2` column, or None if nothing is left."""
    if gdf is None or len(gdf) == 0:
        return None
    gdf = gdf.drop_duplicates("name").reset_index(drop=True)
    proj = gdf.to_crs(gdf.estimate_utm_crs())
    geoms, areas = list(proj.geometry.buffer(0)), list(proj.geometry.area)
    drop = set()
    for i, a in enumerate(geoms):
        inside = [j for j, b in enumerate(geoms)
                  if j != i and 0 < areas[j] < areas[i]
                  and a.intersection(b).area / areas[j] >= contain_ratio]
        if inside and sum(areas[j] for j in inside) >= cover_ratio * areas[i]:
            drop.add(i)
    log.info("Parish polygons (%d returned):", len(gdf))
    for i in sorted(range(len(gdf)), key=lambda k: areas[k]):
        log.info("  %-60s %8.2f km2 %s", gdf.loc[i, "name"], areas[i] / 1e6,
                 "DROPPED (contains other polygons)" if i in drop else "")
    keep = gdf[[i not in drop for i in range(len(gdf))]].copy()
    keep["area_km2"] = [areas[i] / 1e6 for i in range(len(gdf)) if i not in drop]
    return keep.reset_index(drop=True) if len(keep) else None


def tag_zones(D: nx.DiGraph, crs, parishes) -> Dict:
    """Zone (parish) name per node. A node inside several polygons gets the SMALLEST one;
    boundary nodes count as inside; 'unknown' where there is no polygon."""
    zones = {n: "unknown" for n in D.nodes}
    if parishes is None or len(parishes) == 0:
        return zones
    import geopandas as gpd
    from shapely.geometry import Point
    parishes = parishes.reset_index(drop=True)
    if "area_km2" not in parishes:
        parishes = parishes.copy()
        parishes["area_km2"] = parishes.to_crs(parishes.estimate_utm_crs()).geometry.area / 1e6
    ids = list(D.nodes)
    pts = gpd.GeoDataFrame(
        {"nid": ids}, geometry=[Point(D.nodes[n]["x"], D.nodes[n]["y"]) for n in ids], crs=crs)
    pts = pts.to_crs(parishes.crs)
    joined = gpd.sjoin(pts, parishes[["name", "area_km2", "geometry"]], how="left",
                       predicate="intersects")
    joined = joined.sort_values(["nid", "area_km2", "name"], na_position="last").drop_duplicates("nid")
    for nid, name in zip(joined["nid"], joined["name"]):
        if isinstance(name, str):
            zones[nid] = name
    return zones


def merge_small_zones(zones: Dict, min_nodes: int) -> Tuple[Dict, List[str]]:
    """Zones with < min_nodes nodes (fringe artefacts) become 'other'. 'unknown' is kept."""
    counts: Dict[str, int] = {}
    for z in zones.values():
        counts[z] = counts.get(z, 0) + 1
    small = sorted(z for z, c in counts.items() if c < min_nodes and z != "unknown")
    return {n: ("other" if z in small else z) for n, z in zones.items()}, small


def build_dataset(G_latlon, stations: Sequence[Tuple[str, float, float]], parishes, p: Params) -> dict:
    """stations: [(name, lat, lon)]; parishes: GeoDataFrame(name, geometry) or None."""
    import osmnx as ox
    from pyproj import Transformer

    G = G_latlon.copy()
    drop = set(p.drop_highways)
    if drop:                                              # e.g. service roads
        rm = [(u, v, k) for u, v, k, d in G.edges(keys=True, data=True)
              if _hw_set(d.get("highway")) and _hw_set(d.get("highway")) <= drop]
        G.remove_edges_from(rm)
        G.remove_nodes_from(list(nx.isolates(G)))
    G = ox.truncate.largest_component(G, strongly=True)
    G = ox.add_edge_speeds(G, hwy_speeds=URBAN_SPEEDS, fallback=30)
    for _, _, d in G.edges(data=True):
        d["travel_time"] = float(d["length"]) / (float(d["speed_kph"]) * p.speed_factor / 3.6)
    Gp = ox.project_graph(G)
    crs = Gp.graph["crs"]
    if p.tolerance_m > 0:
        Gp = ox.consolidate_intersections(Gp, tolerance=p.tolerance_m, rebuild_graph=True,
                                          dead_ends=False)
    D = largest_scc(collapse_to_digraph(Gp))

    # --- hubs ---
    to_proj = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    st_xy = []
    for name, lat, lon in stations:
        x, y = to_proj.transform(lon, lat)
        st_xy.append((name, x, y))
    cx, cy = to_proj.transform(p.station_search_center[1], p.station_search_center[0])
    ids = sorted(D.nodes, key=lambda n: (round(D.nodes[n]["x"], 1), round(D.nodes[n]["y"], 1), str(n)))
    xy = np.array([[D.nodes[n]["x"], D.nodes[n]["y"]] for n in ids])
    hubs = select_hubs(ids, xy, st_xy, (cx, cy), p)

    # --- renumber: n00001..., hubs become hub_<slug> ---
    new_id = {old: f"n{i:05d}" for i, old in enumerate(ids, 1)}
    used = set()
    for h in hubs:
        hid, k = f"hub_{slug(h['name'])}", 2
        while hid in used:
            hid, k = f"hub_{slug(h['name'])}_{k}", k + 1
        used.add(hid)
        new_id[h["node"]] = hid
        h["id"] = hid

    # --- types and zones ---
    hub_nodes = {h["id"] for h in hubs}
    zones = tag_zones(D, crs, parishes)
    zones, merged_zones = merge_small_zones(zones, p.zone_min_nodes)
    if merged_zones:
        log.info("Merged %d small zones (< %d nodes) into 'other': %s",
                 len(merged_zones), p.zone_min_nodes, merged_zones)
    to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    nodes = []
    for old in ids:
        nid = new_id[old]
        if nid in hub_nodes:
            typ = "hub"
        elif D.nodes[old]["highways"] & RESIDENTIAL_HIGHWAYS:
            typ = "residential"
        else:
            typ = "junction"
        x, y = D.nodes[old]["x"], D.nodes[old]["y"]
        lon, lat = to_ll.transform(x, y)
        nodes.append({"id": nid, "x": round(x, 1), "y": round(y, 1), "lat": round(lat, 6),
                      "lon": round(lon, 6), "type": typ, "zone": zones[old]})
    nodes.sort(key=lambda n: n["id"])
    edges = sorted(
        ({"from": new_id[u], "to": new_id[v], "travel_time": round(d["travel_time"], 1),
          "length": round(d["length"], 1)} for u, v, d in D.edges(data=True)),
        key=lambda e: (e["from"], e["to"]))
    hub_list = [{"id": h["id"], "name": h["name"], "role": h["role"],
                 "coverage_radius": h["coverage_radius"], "capacity": h["capacity"],
                 "snap_m": round(h["snap_m"], 1)}
                for h in sorted(hubs, key=lambda h: h["id"])]

    body = {"nodes": nodes, "edges": edges, "hubs": hub_list}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    try:
        import osmnx
        ox_version = osmnx.__version__
    except Exception:           # pragma: no cover
        ox_version = "unknown"
    meta = {
        "source": "OpenStreetMap contributors (ODbL) via OSMnx",
        "osmnx_version": ox_version,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "crs": str(crs), "units": {"time": "s", "distance": "m"},
        "params": json.loads(json.dumps(asdict(p))),
        "counts": {"nodes": len(nodes), "edges": len(edges), "hubs": len(hub_list),
                   "residential": sum(n["type"] == "residential" for n in nodes)},
        "zones": {"min_nodes": p.zone_min_nodes, "merged_into_other": merged_zones},
        "content_sha256": digest,
    }
    return {"meta": meta, **body}


def summarize(data: dict, max_nodes_warn: int = 8000) -> str:
    D = nx.DiGraph()
    for e in data["edges"]:
        D.add_edge(e["from"], e["to"], travel_time=e["travel_time"])
    res = {n["id"] for n in data["nodes"] if n["type"] == "residential"}
    zone = {n["id"]: n["zone"] for n in data["nodes"]}
    hub_ids = [h["id"] for h in data["hubs"]]
    lines = [f"nodes={D.number_of_nodes()} edges={D.number_of_edges()} "
             f"residential={len(res)} strongly_connected={nx.is_strongly_connected(D)}"]
    if D.number_of_nodes() > max_nodes_warn:
        lines.append(f"  WARNING: {D.number_of_nodes()} nodes. Per-query Dijkstra will be slow; "
                     f"raise --tolerance, or cache hub->node times in TransitNetwork.")
    covered, hub_times = set(), {}
    for h in data["hubs"]:
        full = nx.single_source_dijkstra_path_length(D, h["id"], weight="travel_time")
        hub_times[h["id"]] = full
        inside = res & {n for n, t in full.items() if t <= h["coverage_radius"]}
        covered |= inside
        med = float(np.median([full[n] for n in inside])) if inside else float("nan")
        near = min((full[o], o) for o in hub_ids if o != h["id"]) if len(hub_ids) > 1 else (math.nan, "-")
        lines.append(f"  {h['id']:<28} snap={h['snap_m']:>4.0f}m  within {h['coverage_radius']:.0f}s: "
                     f"{len(inside):>5} residential (median {med:>4.0f}s)  "
                     f"nearest hub {near[1]} {near[0]:.0f}s")
    lines.append(f"  residential nodes covered by >=1 hub: {len(covered)}/{len(res)}")
    zc = {}
    for n in res:
        zc.setdefault(zone[n], [0, 0])[0] += 1
        zc[zone[n]][1] += n in covered
    for z, (tot, cov) in sorted(zc.items()):
        lines.append(f"  zone {z:<24} residential={tot:>5} covered={cov:>5}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _hubs_from_names(names: Sequence[str]) -> List[HubSpec]:
    """Names -> HubSpecs, reusing a default profile when the name matches one."""
    default = {norm(h.name): h for h in DEFAULT_HUBS}
    return [replace(default.get(norm(n), HubSpec(n))) for n in names]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="config/graph.json")
    ap.add_argument("--stations", default="", help="comma-separated hub names (default: the 8 Porto hubs)")
    ap.add_argument("--auto-hubs", action="store_true", help="greedy centre-biased hubs instead")
    ap.add_argument("--max-hubs", type=int, default=5, help="auto mode only")
    ap.add_argument("--buffer", type=float, default=1200.0,
                    help="padding around the hubs' hull (m). 1200 leaves the SW (Aldoar, Foz) "
                         "nearly empty; use ~2000-2500 to include them (bigger graph)")
    ap.add_argument("--search-center", default="41.155,-8.62", help="lat,lon for the station query")
    ap.add_argument("--search-dist", type=float, default=9000.0)
    ap.add_argument("--stations-csv", help="fallback file: name,lat,lon (no OSM station query)")
    ap.add_argument("--graphml", help="load this saved OSMnx graph instead of downloading")
    ap.add_argument("--save-graphml", help="save the downloaded raw graph (reuse with --graphml)")
    ap.add_argument("--no-parishes", action="store_true")
    ap.add_argument("--parish-level", default="8")
    ap.add_argument("--zone-min-nodes", type=int, default=30,
                    help="zones with fewer nodes are merged into 'other' (fringe artefacts)")
    ap.add_argument("--tolerance", type=float, default=15.0)
    ap.add_argument("--speed-factor", type=float, default=0.85)
    ap.add_argument("--min-hub-sep", type=float, default=800.0, help="auto mode only")
    ap.add_argument("--max-snap", type=float, default=300.0)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(message)s")

    lat, lon = (float(v) for v in a.search_center.split(","))
    names = [s.strip() for s in a.stations.split(",") if s.strip()]
    p = Params(buffer_m=a.buffer, station_search_center=(lat, lon), station_search_dist_m=a.search_dist,
               auto_hubs=a.auto_hubs, max_hubs=a.max_hubs, min_hub_sep_m=a.min_hub_sep,
               max_snap_m=a.max_snap, speed_factor=a.speed_factor, tolerance_m=a.tolerance,
               parish_level=a.parish_level, zone_min_nodes=a.zone_min_nodes)
    if names:
        p.hubs = _hubs_from_names(names)

    import osmnx as ox
    if a.stations_csv:
        import csv
        with open(a.stations_csv, encoding="utf-8") as f:
            stations = [(r["name"], float(r["lat"]), float(r["lon"])) for r in csv.DictReader(f)]
    else:
        stations = fetch_stations(p)

    polygon = (circle_polygon(p.station_search_center, p.station_search_dist_m / 2) if p.auto_hubs
               else hub_area_polygon(stations, p.hubs, p.buffer_m))
    if a.graphml:
        G = ox.load_graphml(a.graphml)
    else:
        G = fetch_network(polygon)
        if a.save_graphml:
            ox.save_graphml(G, a.save_graphml)

    parishes = None
    if not a.no_parishes:
        try:
            parishes = fetch_parishes(polygon, p)
            if parishes is None:
                log.warning("No parish polygons found; zones will be 'unknown'")
        except Exception as e:                       # network/tag problems must not kill the build
            log.warning("Parish download failed (%s); zones will be 'unknown'", e)

    data = build_dataset(G, stations, parishes, p)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %s (%.0f KB)", out, out.stat().st_size / 1024)
    print(summarize(data, p.max_nodes_warn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
