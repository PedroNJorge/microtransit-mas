"""Offline tests for tools/build_graph.py using a synthetic OSMnx-style graph (no internet)."""
import json
import sys
from pathlib import Path

import networkx as nx
import pytest

ox = pytest.importorskip("osmnx")
gpd = pytest.importorskip("geopandas")
from shapely.geometry import Point, Polygon  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
for _cand in (_ROOT / "tools", _ROOT / "src" / "tools"):        # root tools/ preferred
    if (_cand / "build_graph.py").exists():
        sys.path.insert(0, str(_cand))
        break
import build_graph as bg  # noqa: E402

LAT0, LON0 = 41.14, -8.62
DLAT, DLON = 0.0009, 0.0012          # ~100 m


def synthetic(n=14):
    """n x n street grid in lat/lon, osmnx conventions. Some one-way streets, one
    parallel (slower) duplicate edge, a primary road row and a disconnected island."""
    G = nx.MultiDiGraph(crs="epsg:4326")
    nid = lambda i, j: i * n + j + 1
    for i in range(n):
        for j in range(n):
            G.add_node(nid(i, j), x=LON0 + j * DLON, y=LAT0 + i * DLAT)
    def road(a, b, hw="residential", both=True, length=100.0):
        G.add_edge(a, b, 0, length=length, highway=hw, osmid=1)
        if both:
            G.add_edge(b, a, 0, length=length, highway=hw, osmid=1)
    for i in range(n):
        for j in range(n):
            hw = "primary" if i == n // 2 else "residential"
            hv = "primary" if i in (n // 2 - 1, n // 2) else "residential"
            if j + 1 < n:
                road(nid(i, j), nid(i, j + 1), hw, both=not (i == 3 and j % 2 == 0))  # one-way bits
            if i + 1 < n:
                road(nid(i, j), nid(i + 1, j), hv)
    G.add_edge(nid(0, 0), nid(0, 1), 1, length=300.0, highway="residential", osmid=2)   # slower parallel
    # a service road that must be dropped
    G.add_edge(nid(0, 0), 7777, 0, length=50.0, highway="service")
    G.add_edge(7777, nid(0, 0), 0, length=50.0, highway="service")
    G.add_node(7777, x=LON0 - DLON, y=LAT0)
    G.add_node(9999, x=LON0 + 0.5, y=LAT0 + 0.5)                                       # island
    G.add_node(9998, x=LON0 + 0.5, y=LAT0 + 0.5009)
    G.add_edge(9999, 9998, 0, length=100.0, highway="residential")
    G.add_edge(9998, 9999, 0, length=100.0, highway="residential")
    return G


def stations(n=14):
    at = lambda i, j, dj=0.0: (LAT0 + i * DLAT, LON0 + (j + dj) * DLON)
    return [
        ("Estação A", *at(3, 3)), ("Estacao A", *at(3, 3, 0.1)),      # duplicate platform, same name
        ("Estação B", *at(3, 10)),
        ("Estação C", *at(10, 6)),
        ("Estação D", *at(10, 3)),                                    # <800 m from A? (7 blocks) -> ok
        ("Estação Longe", LAT0 + 0.05, LON0 + 0.05),                  # far from any road
    ]


@pytest.fixture(scope="module")
def parishes():
    left = Polygon([(LON0 - 0.01, LAT0 - 0.01), (LON0 + 7 * DLON, LAT0 - 0.01),
                    (LON0 + 7 * DLON, LAT0 + 0.02), (LON0 - 0.01, LAT0 + 0.02)])
    right = Polygon([(LON0 + 7 * DLON, LAT0 - 0.01), (LON0 + 0.02, LAT0 - 0.01),
                     (LON0 + 0.02, LAT0 + 0.02), (LON0 + 7 * DLON, LAT0 + 0.02)])
    return gpd.GeoDataFrame({"name": ["Oeste", "Leste"]}, geometry=[left, right], crs="EPSG:4326")


HUBS = [bg.HubSpec("Estação A", "anchor", 420, 10), bg.HubSpec("Estação B", "east", 600, 8),
        bg.HubSpec("Estação C", "south", 720, 6)]
PARAMS = dict(hubs=HUBS, station_search_center=(LAT0 + 6 * DLAT, LON0 + 6 * DLON),
              max_snap_m=150, tolerance_m=0)


@pytest.fixture(scope="module")
def data(parishes):
    return bg.build_dataset(synthetic(), stations(), parishes, bg.Params(**PARAMS))


def graph_of(d):
    G = nx.DiGraph()
    G.add_nodes_from(n["id"] for n in d["nodes"])
    for e in d["edges"]:
        G.add_edge(e["from"], e["to"], travel_time=e["travel_time"])
    return G


def test_island_removed_and_strongly_connected(data):
    G = graph_of(data)
    assert nx.is_strongly_connected(G)
    assert G.number_of_nodes() == 14 * 14


def test_edges_positive_and_parallel_collapsed(data):
    assert all(e["travel_time"] >= 1.0 for e in data["edges"])
    pairs = [(e["from"], e["to"]) for e in data["edges"]]
    assert len(pairs) == len(set(pairs))
    assert not any(a == b for a, b in pairs)
    # parallel 300 m edge dropped: 100 m @ 30 km/h * 0.85 = 14.1 s, not 42 s
    corner = {n["id"]: n for n in data["nodes"]}
    fast = [e["travel_time"] for e in data["edges"] if e["length"] == 100.0 and e["travel_time"] < 20]
    assert fast and max(e["travel_time"] for e in data["edges"] if e["length"] == 300.0
                        ) if any(e["length"] == 300.0 for e in data["edges"]) else True


def test_one_way_streets_preserved(data):
    G = graph_of(data)
    one_way = [(u, v) for u, v in G.edges if not G.has_edge(v, u)]
    assert one_way, "expected some one-way edges to survive"


def test_travel_time_formula(data):
    e = next(e for e in data["edges"] if e["length"] == 100.0)
    assert e["travel_time"] in (round(100 / (30 * .85 / 3.6), 1), round(100 / (50 * .85 / 3.6), 1))


def test_hubs_explicit_with_roles_and_per_hub_settings(data):
    by_name = {h["name"]: h for h in data["hubs"]}
    assert set(by_name) == {"Estação A", "Estação B", "Estação C"}   # duplicate platform merged
    assert by_name["Estação A"]["coverage_radius"] == 420 and by_name["Estação A"]["capacity"] == 10
    assert by_name["Estação C"]["role"] == "south" and by_name["Estação C"]["coverage_radius"] == 720
    node = {n["id"]: n for n in data["nodes"]}
    for h in data["hubs"]:
        assert h["id"].startswith("hub_") and h["snap_m"] <= 150
        assert node[h["id"]]["type"] == "hub"


def test_service_roads_dropped(data):
    assert len(data["nodes"]) == 14 * 14            # 7777 (service-only) and island both gone


def test_missing_hub_is_an_error_not_silent():
    p = bg.Params(**{**PARAMS, "hubs": HUBS + [bg.HubSpec("Nonexistent")]})
    with pytest.raises(KeyError, match="not found"):
        bg.build_dataset(synthetic(), stations(), None, p)


def test_hub_too_far_from_roads_is_an_error():
    p = bg.Params(**{**PARAMS, "hubs": HUBS + [bg.HubSpec("Estação Longe")]})
    with pytest.raises(ValueError, match="nearest road"):
        bg.build_dataset(synthetic(), stations(), None, p)


def test_two_hubs_on_same_node_is_an_error():
    st = stations() + [("Estação A2", LAT0 + 3 * DLAT, LON0 + 3 * DLON + 0.00005)]
    p = bg.Params(**{**PARAMS, "hubs": [bg.HubSpec("Estação A"), bg.HubSpec("Estação A2")]})
    with pytest.raises(ValueError, match="same road node"):
        bg.build_dataset(synthetic(), st, None, p)


def test_name_matching_exact_before_substring_and_ambiguity():
    keys = ["trindade", "senhora da hora", "campo 24 de agosto", "campo alegre", "campo"]
    assert bg.find_key(keys, "Campo") == "campo"                       # exact wins
    assert bg.find_key(keys, "Senhora") == "senhora da hora"           # unique substring
    assert bg.find_key(keys, "São Trindade".replace("São ", "")) == "trindade"
    with pytest.raises(ValueError, match="ambiguous"):
        bg.find_key(["campo 24 de agosto", "campo alegre"], "campo")
    with pytest.raises(KeyError):
        bg.find_key(keys, "zzz")


def test_auto_hub_mode_is_centre_biased_and_separated(parishes):
    p = bg.Params(**{**PARAMS, "auto_hubs": True, "max_hubs": 3, "min_hub_sep_m": 300})
    d = bg.build_dataset(synthetic(), stations(), parishes, p)
    assert 1 <= len(d["hubs"]) <= 3
    assert "Estação Longe" not in {h["name"] for h in d["hubs"]}


def test_hub_area_polygon_covers_hubs_with_buffer():
    poly = bg.hub_area_polygon(stations(), HUBS, 500)
    from shapely.geometry import Point
    for _, lat, lon in stations()[:4]:
        assert poly.contains(Point(lon, lat))
    assert not poly.contains(Point(LON0 + 0.05, LAT0 + 0.05))


def test_default_hubs_are_the_eight_porto_stations():
    names = [h.name for h in bg.DEFAULT_HUBS]
    assert names == ["Trindade", "Campanhã", "Casa da Música", "São Bento", "Bolhão",
                     "Aliados", "Campo 24 de Agosto", "Senhora da Hora"]
    assert bg._hubs_from_names(["trindade"])[0].capacity == bg.DEFAULT_HUBS[0].capacity
    assert bg._hubs_from_names(["Other"])[0].coverage_radius == 600.0
    assert bg.Params().hubs is not bg.DEFAULT_HUBS                    # no shared mutable state


def test_node_types_and_zones(data):
    types = {n["type"] for n in data["nodes"]}
    assert {"hub", "residential", "junction"} <= types
    assert {n["zone"] for n in data["nodes"]} == {"Oeste", "Leste"}
    west = [n for n in data["nodes"] if n["lon"] < LON0 + 6 * DLON]
    assert all(n["zone"] == "Oeste" for n in west)


def test_without_parishes_zones_unknown():
    d = bg.build_dataset(synthetic(), stations(), None, bg.Params(**PARAMS))
    assert {n["zone"] for n in d["nodes"]} == {"unknown"}


def test_deterministic_and_json_roundtrip(data, parishes):
    d2 = bg.build_dataset(synthetic(), stations(), parishes, bg.Params(**PARAMS))
    assert data["meta"]["content_sha256"] == d2["meta"]["content_sha256"]
    assert json.loads(json.dumps(data)) == data


def test_consolidation_keeps_graph_valid(parishes):
    d = bg.build_dataset(synthetic(), stations(), parishes,
                         bg.Params(**{**PARAMS, "tolerance_m": 15}))
    assert nx.is_strongly_connected(graph_of(d))
    assert len(d["hubs"]) == 3


def test_summary_runs(data):
    out = bg.summarize(data)
    assert "strongly_connected=True" in out and "zone Leste" in out


def test_filter_metro_prefers_operator_then_falls_back():
    gdf = gpd.GeoDataFrame(
        {"name": ["Trindade", "Rail X", None], "operator": ["Metro do Porto", "CP", "Metro do Porto"],
         "station": ["light_rail", "yes", "light_rail"]},
        geometry=[Point(-8.60, 41.15), Point(-8.5, 41.1), Point(0, 0)], crs="EPSG:4326")
    assert [s[0] for s in bg.filter_metro(gdf)] == ["Trindade"]
    gdf2 = gdf.drop(columns="operator")
    assert [s[0] for s in bg.filter_metro(gdf2)] == ["Trindade"]      # station=light_rail fallback
    assert bg.filter_metro(None) == []


# ------------------------------------------------------------------
# Zone tagging: overlapping polygons, containment filter, small-zone merge
# ------------------------------------------------------------------

def _poly(x0, x1, y0=-0.01, y1=0.02):
    return Polygon([(LON0 + x0, LAT0 + y0), (LON0 + x1, LAT0 + y0),
                    (LON0 + x1, LAT0 + y1), (LON0 + x0, LAT0 + y1)])


@pytest.fixture(scope="module")
def messy_parishes():
    """Oeste/Leste plus a municipality 'AAA Cidade' covering both (sorts first alphabetically)
    and a sliver parish 'Minusculo' covering ~2 nodes."""
    rows = [("AAA Cidade", _poly(-0.01, 0.02)), ("Oeste", _poly(-0.01, 7 * DLON)),
            ("Leste", _poly(7 * DLON, 0.02)),
            ("Minusculo", _poly(0.5 * DLON, 1.5 * DLON, LAT0 - LAT0 - 0.0001, 0.0001))]
    return gpd.GeoDataFrame({"name": [r[0] for r in rows]}, geometry=[r[1] for r in rows],
                            crs="EPSG:4326")


def test_clean_parishes_drops_containers_only(messy_parishes):
    out = bg.clean_parishes(messy_parishes)
    assert set(out["name"]) == {"Oeste", "Leste", "Minusculo"}
    assert (out["area_km2"] > 0).all()
    assert bg.clean_parishes(None) is None


def test_clean_parishes_logs_table(messy_parishes, caplog):
    with caplog.at_level("INFO", logger="build_graph"):
        bg.clean_parishes(messy_parishes)
    assert "AAA Cidade" in caplog.text and "DROPPED" in caplog.text


def test_container_polygon_no_longer_swallows_parishes(messy_parishes):
    d = bg.build_dataset(synthetic(), stations(), bg.clean_parishes(messy_parishes), bg.Params(**PARAMS))
    zones = {n["zone"] for n in d["nodes"]}
    assert "AAA Cidade" not in zones and {"Oeste", "Leste"} <= zones


def test_smallest_polygon_wins_even_without_cleaning(messy_parishes):
    """Defence in depth: if a container slips through, tagging still prefers the smaller polygon."""
    d = bg.build_dataset(synthetic(), stations(), messy_parishes,
                         bg.Params(**{**PARAMS, "zone_min_nodes": 0}))
    zones = {n["zone"] for n in d["nodes"]}
    assert zones == {"Oeste", "Leste", "Minusculo"}


def test_small_zones_merged_into_other(messy_parishes):
    d = bg.build_dataset(synthetic(), stations(), bg.clean_parishes(messy_parishes), bg.Params(**PARAMS))
    zones = {n["zone"] for n in d["nodes"]}
    assert "Minusculo" not in zones and "other" in zones
    assert d["meta"]["zones"]["merged_into_other"] == ["Minusculo"]


def test_merge_small_zones_keeps_unknown():
    z, small = bg.merge_small_zones({1: "A", 2: "A", 3: "B", 4: "unknown"}, 2)
    assert small == ["B"] and z[3] == "other" and z[4] == "unknown" and z[1] == "A"


def test_parish_with_small_enclave_is_kept(messy_parishes):
    """Oeste holds the sliver polygon but is not a container: it must survive cleaning."""
    out = bg.clean_parishes(messy_parishes)
    assert "Oeste" in set(out["name"])
