"""GeoNexus campus routing API: FastAPI + PostGIS + pgRouting.

Run:   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
Docs:  http://localhost:8000/docs
"""
import json
import os
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------
DB_CONN = os.getenv(
    "GEONEXUS_DB",
    "dbname=geonexus_hatfield user=postgres host=localhost port=5432",
)

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]

BUILDINGS = "public.digitized_buildings"    # needs columns: name, geom
PATHS = "public.noded_pathways"             # needs columns: pid, source, target, geom
PATH_ID = "pid"
DATA_SRID = 32735                           # WGS 84 / UTM zone 35S
WALK_METRES_PER_MINUTE = 80                 # roughly 4.8 km/h

app = FastAPI(title="GeoNexus Campus Router", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ----------------------------------------------------------------------
# SQL Statements
# ----------------------------------------------------------------------
NAMES_SQL = f"""
    SELECT DISTINCT name
    FROM {BUILDINGS}
    WHERE name IS NOT NULL AND trim(name) <> ''
    ORDER BY name;
"""

# Finds the routing node closest to the centre of the named building.
NEAREST_NODE_SQL = f"""
    WITH building_point AS (
        SELECT ST_Transform(ST_PointOnSurface(geom), {DATA_SRID}) AS geom
        FROM {BUILDINGS}
        WHERE lower(trim(name)) = lower(trim(%s))
        LIMIT 1
    ),
    nearest_path AS (
        SELECT p.source, p.target, p.geom,
               building_point.geom AS building_geom
        FROM building_point
        CROSS JOIN LATERAL (
            SELECT source, target, geom
            FROM {PATHS}
            WHERE source IS NOT NULL AND target IS NOT NULL
            ORDER BY geom <-> building_point.geom
            LIMIT 1
        ) p
    )
    SELECT CASE
             WHEN ST_Distance(ST_StartPoint(geom), building_geom)
                  <= ST_Distance(ST_EndPoint(geom), building_geom)
             THEN source ELSE target
           END
    FROM nearest_path;
"""

# Shortest walking route between two nodes returned as GeoJSON line in WGS84 (EPSG:4326)
ROUTE_SQL = f"""
    SELECT ST_AsGeoJSON(
               ST_Transform(
                   ST_LineMerge(ST_Collect(p.geom ORDER BY route.path_seq)),
                   4326)),
           sum(ST_Length(p.geom))
    FROM pgr_dijkstra(
        'SELECT {PATH_ID} AS id, source, target, ST_Length(geom) AS cost FROM {PATHS}',
        %s::bigint, %s::bigint, false
    ) AS route
    JOIN {PATHS} p ON route.edge = p.{PATH_ID}
    WHERE route.edge <> -1;
"""


def nearest_node(cur, name: str) -> int:
    cur.execute(NEAREST_NODE_SQL, (name,))
    row = cur.fetchone()
    if row is None or row[0] is None:
        raise HTTPException(status_code=404, detail=f"Could not find '{name}' on the map.")
    return row[0]


# ----------------------------------------------------------------------
# API Endpoints
# ----------------------------------------------------------------------
@app.get("/api/health")
def health_check():
    try:
        with psycopg.connect(DB_CONN) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1;")
                cursor.fetchone()
            return {"status": "ok"}
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="Database is not reachable.") from exc


@app.get("/api/search-buildings")
def get_named_buildings():
    """Names of all buildings, used to fill search dropdowns."""
    try:
        with psycopg.connect(DB_CONN) as conn, conn.cursor() as cur:
            cur.execute(NAMES_SQL)
            return [row[0] for row in cur.fetchall()]
    except psycopg.Error:
        raise HTTPException(status_code=503, detail="Database query failed or database unreachable.")


@app.get("/api/route")
def get_geojson_route(start_name: str, dest_name: str):
    """Shortest walking route between two named buildings, returned as GeoJSON."""
    if start_name.strip().lower() == dest_name.strip().lower():
        raise HTTPException(status_code=400, detail="Start and destination are the same place.")

    try:
        with psycopg.connect(DB_CONN) as conn, conn.cursor() as cur:
            start_node = nearest_node(cur, start_name.strip())
            end_node = nearest_node(cur, dest_name.strip())

            if start_node == end_node:
                raise HTTPException(status_code=400, detail="These two places share the same pathway junction.")

            cur.execute(ROUTE_SQL, (start_node, end_node))
            row = cur.fetchone()
    except psycopg.Error as err:
        raise HTTPException(status_code=500, detail=f"Database error during routing calculation: {str(err)}")

    if not row or row[0] is None:
        raise HTTPException(status_code=404, detail="No pathway connection found between these places.")

    distance_m = round(row[1])
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": json.loads(row[0]),
            "properties": {
                "start": start_name,
                "destination": dest_name,
                "distance_m": distance_m,
                "walk_minutes": max(1, round(distance_m / WALK_METRES_PER_MINUTE)),
            },
        }],
    }


# ----------------------------------------------------------------------
# Serve Static Frontend Files
# ----------------------------------------------------------------------
STATIC_DIR = Path(__file__).resolve().parent / "static"
if not STATIC_DIR.exists():
    raise RuntimeError(f"Static directory does not exist: {STATIC_DIR}")

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")