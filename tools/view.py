import json
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, LineString

data = json.load(open("config/porto_graph.json"))

# --- nodes ---
nodes = pd.DataFrame(data["nodes"])
gdf_nodes = gpd.GeoDataFrame(
    nodes,
    geometry=[Point(xy) for xy in zip(nodes["lon"], nodes["lat"])],
    crs="EPSG:4326",
)

# --- edges (approximate: straight lines between node coordinates) ---
id_to_xy = {n["id"]: (n["lon"], n["lat"]) for n in data["nodes"]}
edges = pd.DataFrame(data["edges"])
gdf_edges = gpd.GeoDataFrame(
    edges,
    geometry=[
        LineString([id_to_xy[e["from"]], id_to_xy[e["to"]]])
        for _, e in edges.iterrows()
    ],
    crs="EPSG:4326",
)

# --- interactive map ---
# m = gdf_edges.explore(color="#888888", weight=1, tiles="CartoDB positron")
m = gdf_edges.explore(color="#888888", weight=1, tiles="Esri.WorldGrayCanvas")
gdf_nodes[gdf_nodes["type"] == "hub"].explore(
    m=m, color="red", marker_kwds={"radius": 8}, tooltip=["id", "zone"]
)
gdf_nodes[gdf_nodes["type"] == "residential"].explore(
    m=m, color="green", marker_kwds={"radius": 2}, tooltip=["id", "zone"]
)
m.save("porto_graph_map.html")
