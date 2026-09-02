# FleetFlow

**A Redis-powered ride-dispatch engine, built to demonstrate the ideas behind apps like Namma Yatri: in-memory geospatial matching, race-condition-free driver locking, and a cache layer that cuts third-party map costs.**

FastAPI backend · Redis (Geo + cache + atomic locks) · Leaflet.js dashboard · pure functional state machine

---

## What it does

Five autos, one map, one click. FleetFlow finds the nearest available driver, locks them atomically so no two riders can grab the same one, and caches route distances so repeat requests along the same corridor cost ~0ms instead of a full recalculation.

| Feature | How | Where |
|---|---|---|
| Live driver locations | `GEOADD` into a Redis sorted set | `POST /drivers/location` |
| Nearest-driver matching | `GEOSEARCH` sorted by distance | `POST /rides/estimate-and-dispatch` |
| No double-booking | `SET key val NX EX 15` atomic lock | same endpoint |
| Route/ETA caching | Grid-rounded coordinate key + `SETEX` TTL | same endpoint |
| Ride lifecycle | Pure, immutable state transitions | `transition_ride()` |

## Project structure

```
fleetflow/
├── main.py             FastAPI backend — endpoints, Redis calls, state machine
├── index.html           Leaflet dashboard — map, dispatch UI, event log
├── requirements.txt     fastapi, uvicorn, redis, pydantic
└── README.md
```

## Quickstart

### 1. Get Redis running

**Option A — free cloud Redis (no install):**
Create a free 30MB database at [Redis Cloud](https://redis.io/try-free/), then
grab its **Public endpoint** (`host:port`) and **password** from the database's
config page.

**Option B — Docker, if you have it:**
```bash
docker run -d -p 6379:6379 --name fleetflow-redis redis:alpine
```

### 2. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Point the app at Redis

Local Redis (Docker) needs no configuration — it defaults to `localhost:6379`.

Cloud Redis needs three environment variables set **in the same terminal**
you'll run `uvicorn` from:

```powershell
# PowerShell
$env:REDIS_HOST = "your-endpoint-host"
$env:REDIS_PORT = "your-port"
$env:REDIS_PASSWORD = "your-password"
```
```bash
# macOS/Linux
export REDIS_HOST="your-endpoint-host"
export REDIS_PORT="your-port"
export REDIS_PASSWORD="your-password"
```

If your Redis Cloud database requires TLS, also set `$env:REDIS_TLS = "true"`
(or `export REDIS_TLS=true`).

### 4. Run it

```bash
uvicorn main:app --reload
```

```bash
curl http://localhost:8000/health
# {"status":"ok","redis":"connected"}
```

Then open `index.html` in a browser — it talks to the API at
`http://localhost:8000`, no separate frontend server needed.

## Using the dashboard

1. **Seed 5 drivers** — writes five auto locations into Redis via `GEOADD`;
   yellow markers appear on the Koramangala map.
2. **Click anywhere on the map** — drops a rider pin and dispatches a ride.
   The sidebar walks `REQUESTED → ASSIGNED → COMPLETED`, metric cards fill
   in with the matched driver and fare, and the event log narrates each
   Redis operation.
3. **Click the same spot again** — `Route cache` flips from `MISS` to `HIT`
   and latency drops from ~1000ms+ to a few milliseconds, since the distance
   came straight out of Redis instead of being recalculated.

## Troubleshooting

**`/health` says `redis: unreachable` or the request hangs with no error:**
The client now fails after 5 seconds instead of hanging forever — if it's
still timing out, either the host/port is wrong, or your network is
blocking outbound traffic on that port. Corporate/school networks often
block non-standard ports.

**`/health` mentions TLS/SSL:**
Set `REDIS_TLS=true` in the same terminal and restart `uvicorn`.

**Browser console shows CORS errors:**
The backend already allows all origins — check the API is actually running
on port 8000 and that `index.html` is pointed at the right URL.

**`docker: command not found`:**
Docker Desktop isn't installed. Use Option A (cloud Redis) instead, or
install Docker Desktop from docker.com.

## Interview talking points

- **Why Redis Geo:** `GEOADD`/`GEOSEARCH` give sorted-by-distance driver
  lookups entirely in RAM — no round-trip to a primary database for
  something as high-frequency as GPS pings.
- **Why the atomic lock:** `SET key val NX EX 15` only succeeds if the key
  doesn't already exist, which is exactly what prevents two riders from
  being matched to the same driver in a race condition.
- **Why the route cache:** rounding coordinates to 2 decimal places groups
  nearby requests into the same cache key (~1km grid cell), so a popular
  corridor only hits a paid map API once every 10 minutes instead of on
  every single request.
- **Why immutable state:** `transition_ride()` never mutates a `RideState`
  in place — it returns a new one. That makes the ride lifecycle easy to
  reason about, test, and replay.

## Extending it

- Swap the Haversine estimate in `estimate_and_dispatch` for a real routing
  API (OSRM, Google Directions) behind the same cache.
- Add `POST /rides/complete` that calls `transition_ride(state, "COMPLETE")`
  and releases the driver lock (`DEL lock:driver:<id>`).
- Persist ride history to a real database asynchronously, so Redis stays
  the fast path and Postgres/Mongo becomes the system of record.

## License

MIT — do whatever you'd like with this.
