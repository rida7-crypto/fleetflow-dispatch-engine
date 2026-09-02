"""
FleetFlow Engine
----------------
A tiny ride-dispatch backend that demonstrates:
  1. Redis Geospatial indexing for live driver locations (GEOADD / GEOSEARCH)
  2. A pure, immutable functional-programming state machine for ride status
  3. A route/ETA cache (grid-rounded key + Redis TTL) that stands in for
     cutting third-party Map API calls
  4. Atomic driver locking (SET ... NX) so two riders can never be matched
     to the same driver at the same instant

Run with:
    uvicorn main:app --reload
"""

import math
import os
from dataclasses import dataclass
from enum import Enum

import redis
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="FleetFlow Engine")

# Allow the local index.html (served from a different origin/file://) to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Connect to Redis. Defaults to a local instance (Docker/WSL/Memurai), but
# reads from environment variables so it can point at a free cloud Redis
# database instead — see README for how to get these values from Redis Cloud.
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD")  # None for local, set for cloud
# Some Redis Cloud databases require TLS. Set REDIS_TLS=true in your shell if
# the plain connection below hangs or refuses — see README.
REDIS_TLS = os.environ.get("REDIS_TLS", "false").lower() == "true"

r = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    db=0,
    decode_responses=True,
    ssl=REDIS_TLS,
    socket_connect_timeout=5,  # fail fast instead of hanging forever
    socket_timeout=5,
)


# -------------------------------------------------------------------------
# 1. FUNCTIONAL PROGRAMMING STATE ENGINE (immutable, no in-place mutation)
# -------------------------------------------------------------------------
class RideStatus(str, Enum):
    REQUESTED = "REQUESTED"
    ASSIGNED = "ASSIGNED"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class RideState:
    ride_id: str
    rider_id: str
    driver_id: str | None
    status: RideStatus


def transition_ride(current: RideState, event: str, driver_id: str | None = None) -> RideState:
    """Pure function: (state, event) -> new state. Never mutates `current`."""
    if current.status == RideStatus.REQUESTED and event == "ASSIGN_DRIVER":
        if not driver_id:
            raise ValueError("driver_id is required to assign a ride")
        return RideState(
            ride_id=current.ride_id,
            rider_id=current.rider_id,
            driver_id=driver_id,
            status=RideStatus.ASSIGNED,
        )
    raise ValueError(f"Invalid transition from {current.status} on event '{event}'")


# -------------------------------------------------------------------------
# 2. REQUEST / RESPONSE SCHEMAS
# -------------------------------------------------------------------------
class LocationUpdate(BaseModel):
    driver_id: str
    lat: float
    lon: float


class RideRequest(BaseModel):
    ride_id: str
    rider_id: str
    pickup_lat: float
    pickup_lon: float
    drop_lat: float
    drop_lon: float


# -------------------------------------------------------------------------
# 3. ENDPOINTS
# -------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        r.ping()
        return {"status": "ok", "redis": "connected"}
    except redis.exceptions.ConnectionError:
        return {
            "status": "degraded",
            "redis": f"unreachable at {REDIS_HOST}:{REDIS_PORT} — check REDIS_HOST/PORT/PASSWORD env vars",
        }
    except redis.exceptions.AuthenticationError:
        return {"status": "degraded", "redis": "auth failed — check REDIS_PASSWORD"}


@app.post("/drivers/location")
def update_driver_location(loc: LocationUpdate):
    """Writes/updates a driver's live GPS position into the Redis geo index."""
    try:
        r.geoadd("active_drivers", (loc.lon, loc.lat, loc.driver_id))
    except redis.exceptions.RedisError as e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")
    return {"status": "success", "driver_id": loc.driver_id}


@app.get("/drivers")
def list_drivers():
    """Returns every driver currently in the geo index (for the map to redraw)."""
    members = r.zrange("active_drivers", 0, -1)
    out = []
    for member in members:
        pos = r.geopos("active_drivers", member)[0]
        if pos:
            out.append({"driver_id": member, "lon": pos[0], "lat": pos[1]})
    return out


@app.post("/rides/estimate-and-dispatch")
def estimate_and_dispatch(req: RideRequest):
    # --- Step A: Route cache lookup (this is what removes 3rd-party Map API cost) ---
    route_key = (
        f"route:{round(req.pickup_lat, 2)},{round(req.pickup_lon, 2)}"
        f"->{round(req.drop_lat, 2)},{round(req.drop_lon, 2)}"
    )
    try:
        cached_distance = r.get(route_key)
    except redis.exceptions.RedisError as e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")

    if cached_distance:
        distance_km = float(cached_distance)
        cache_hit = True
    else:
        dlat = math.radians(req.drop_lat - req.pickup_lat)
        dlon = math.radians(req.drop_lon - req.pickup_lon)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(math.radians(req.pickup_lat))
            * math.cos(math.radians(req.drop_lat))
            * math.sin(dlon / 2) ** 2
        )
        distance_km = round(6371 * 2 * math.asin(math.sqrt(a)), 2)
        r.setex(route_key, 600, str(distance_km))  # cache for 10 minutes
        cache_hit = False

    # --- Step B: Find nearby drivers via Redis geospatial search ---
    try:
        nearby_drivers = r.geosearch(
            "active_drivers",
            longitude=req.pickup_lon,
            latitude=req.pickup_lat,
            radius=5,
            unit="km",
            withdist=True,
            sort="ASC",
        )
    except redis.exceptions.RedisError as e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")

    if not nearby_drivers:
        raise HTTPException(status_code=404, detail="No active drivers nearby")

    # --- Step C: Atomic lock so two riders can never grab the same driver ---
    assigned_driver, driver_distance = None, None
    try:
        for driver_id, dist in nearby_drivers:
            if r.set(f"lock:driver:{driver_id}", "busy", nx=True, ex=15):
                assigned_driver, driver_distance = driver_id, dist
                break
    except redis.exceptions.RedisError as e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")
    if not assigned_driver:
        raise HTTPException(status_code=409, detail="All nearby drivers are currently being matched")

    # --- Step D: Pure functional state transition ---
    initial_state = RideState(
        ride_id=req.ride_id, rider_id=req.rider_id, driver_id=None, status=RideStatus.REQUESTED
    )
    final_state = transition_ride(initial_state, "ASSIGN_DRIVER", driver_id=assigned_driver)

    return {
        "ride_id": final_state.ride_id,
        "status": final_state.status.value,
        "assigned_driver": final_state.driver_id,
        "driver_distance_km": round(driver_distance, 2),
        "trip_distance_km": distance_km,
        "estimated_fare": max(30, round(distance_km * 15)),
        "cache_hit": cache_hit,
    }