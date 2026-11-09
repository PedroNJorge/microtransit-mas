"""
Data-contract tests for the COMMITTED config/porto_graph.json (output of tools/build_graph.py).
No internet, no osmnx. Skipped if the file is missing. If you rebuild the graph with different
hubs or settings, update EXPECTED_HUBS / thresholds deliberately: these guard the contract that
the agents rely on.
"""
import hashlib
import json
from pathlib import Path

import networkx as nx
import pytest

PATH = Path(__file__).resolve().parents[1] / "config" / "porto_graph.json"
pytestmark = pytest.mark.skipif(not PATH.exists(), reason="config/porto_graph.json not built yet")

EXPECTED_HUBS = {"hub_trindade", "hub_campanha", "hub_casa_da_musica", "hub_sao_bento",
                 "hub_bolhao", "hub_aliados", "hub_campo_24_de_agosto", "hub_senhora_da_hora"}


@pytest.fixture(scope="module")
def data():
    return json.loads(PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def G(data):
    g = nx.DiGraph()
    g.add_nodes_from(n["id"] for n in data["nodes"])
    for e in data["edges"]:
        g.add_edge(e["from"], e["to"], travel_time=e["travel_time"])
    return g


def test_top_level_schema(data):
    assert {"meta", "nodes", "edges", "hubs"} <= set(data)
    assert data["meta"]["units"] == {"time": "s", "distance": "m"}
    counts = data["meta"]["counts"]
    assert counts["nodes"] == len(data["nodes"]) and counts["edges"] == len(data["edges"])
    assert counts["hubs"] == len(data["hubs"])


def test_content_hash_matches(data):
    body = {k: data[k] for k in ("nodes", "edges", "hubs")}
    assert hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() \
        == data["meta"]["content_sha256"], "file was edited after build (or hashing changed)"


def test_node_and_edge_integrity(data):
    ids = [n["id"] for n in data["nodes"]]
    assert len(ids) == len(set(ids))
    assert {n["type"] for n in data["nodes"]} <= {"hub", "residential", "junction"}
    idset = set(ids)
    assert all(e["from"] in idset and e["to"] in idset and e["from"] != e["to"] for e in data["edges"])
    assert all(e["travel_time"] >= 1.0 and e["length"] >= 0 for e in data["edges"])
    pairs = [(e["from"], e["to"]) for e in data["edges"]]
    assert len(pairs) == len(set(pairs))


def test_strongly_connected(G):
    assert nx.is_strongly_connected(G)


def test_hubs_are_the_expected_eight_and_are_hub_nodes(data):
    assert {h["id"] for h in data["hubs"]} == EXPECTED_HUBS
    node = {n["id"]: n for n in data["nodes"]}
    for h in data["hubs"]:
        assert node[h["id"]]["type"] == "hub"
        assert h["snap_m"] <= 300 and h["coverage_radius"] > 0 and h["capacity"] > 0
    assert sum(n["type"] == "hub" for n in data["nodes"]) == 8


def test_every_hub_reaches_nearly_everything_both_ways(data, G):
    n = G.number_of_nodes()
    Gr = G.reverse(copy=False)
    for h in data["hubs"]:
        out = nx.single_source_dijkstra_path_length(G, h["id"], weight="travel_time")
        back = nx.single_source_dijkstra_path_length(Gr, h["id"], weight="travel_time")
        assert len(out) == n and len(back) == n


def test_zone_tags_are_parishes_not_municipalities(data):
    zones = {n["zone"] for n in data["nodes"]}
    assert not zones & {"Porto", "Matosinhos"}            # leaked municipality polygons
    assert "Ramalde" in zones and "Paranhos" in zones      # the regression we fixed
    counts = {}
    for n in data["nodes"]:
        counts[n["zone"]] = counts.get(n["zone"], 0) + 1
    assert counts["Ramalde"] > 100


def test_unknown_zone_share_is_bounded(data):
    unknown = sum(n["zone"] == "unknown" for n in data["nodes"])
    assert unknown / len(data["nodes"]) < 0.05            # currently ~3%; investigate if it grows


def test_residential_nodes_exist_in_every_big_zone(data):
    res = {}
    for n in data["nodes"]:
        if n["type"] == "residential":
            res[n["zone"]] = res.get(n["zone"], 0) + 1
    assert sum(res.values()) > 2000
    assert res.get("Ramalde", 0) > 100 and res.get("Campanhã", 0) > 100
