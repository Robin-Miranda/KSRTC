"""
Backend logic for the Driver Live Dispatch module (no Streamlit code here).

* SQLite store (data/dispatch.db) shared by every driver using the app, so one
  driver's charging plan is visible to all other drivers on the same route.
* Charging-time model based on station kW / voltage / amps, bus battery size,
  bus max charge acceptance, charging efficiency and a taper above 80 % SoC.
* FIFO queue model per station (number of ports) -> expected waiting time and
  congestion level.
"""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "dispatch.db"

# ---- model assumptions (edit here) -----------------------------------------
AVG_SPEED_KMPH = 50.0        # average highway speed of the bus
CHARGE_EFFICIENCY = 0.92     # grid -> battery efficiency
TAPER_FROM_SOC = 80.0        # above this SoC the charge power tapers
TAPER_POWER_FACTOR = 0.35    # average power fraction between 80 % and 100 %
MAX_PORTS = 12               # cap for very large hubs
BUS_MIN_KW = 60.0            # minimum power to count a station as "bus-capable"
ASSUMED_DC_VOLTAGE = 500.0   # used when the station has no voltage on record
ASSUMED_AC_VOLTAGE = 230.0
FREE_WAIT_MIN = 5            # wait < 5 min  -> Free
BUSY_WAIT_MIN = 20           # wait < 20 min -> Busy, otherwise Congested
STALE_PLAN_MIN = 90          # planned stop older than this is ignored

LEVEL_ICON = {"free": "🟢 Free", "busy": "🟡 Busy", "congested": "🔴 Congested"}
LEVEL_COLOR = {"free": [0, 200, 50, 230], "busy": [255, 195, 0, 230], "congested": [255, 40, 40, 230]}


def now():
    return datetime.now().replace(microsecond=0)


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else None


# ---------------------------------------------------------------- database --
def _exec(sql, params=()):
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    try:
        cur = con.execute(sql, params)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _query(sql, params=()):
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS trips (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              driver_name TEXT, driver_id TEXT, phone TEXT,
              bus_name TEXT, bus_no TEXT,
              route TEXT, corridor_id TEXT, start_location TEXT, destination TEXT,
              battery_kwh REAL, bus_max_kw REAL,
              start_soc REAL, current_soc REAL, position_km REAL DEFAULT 0,
              status TEXT DEFAULT 'active',
              started_at TEXT, updated_at TEXT, is_demo INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              trip_id INTEGER, corridor_id TEXT,
              station_id INTEGER, station_name TEXT,
              arrival_ts TEXT, start_ts TEXT, end_ts TEXT,
              soc_from REAL, soc_to REAL, power_kw REAL, duration_min REAL,
              status TEXT, created_at TEXT, is_demo INTEGER DEFAULT 0);
            """
        )
        con.commit()
    finally:
        con.close()


# ------------------------------------------------------------------- trips --
def create_trip(driver_name, driver_id, phone, bus_name, bus_no, route, corridor_id,
                start_location, destination, battery_kwh, bus_max_kw, start_soc, is_demo=0):
    t = _ts(now())
    return _exec(
        """INSERT INTO trips (driver_name, driver_id, phone, bus_name, bus_no, route, corridor_id,
           start_location, destination, battery_kwh, bus_max_kw, start_soc, current_soc,
           position_km, status, started_at, updated_at, is_demo)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,'active',?,?,?)""",
        (driver_name, driver_id, phone, bus_name, bus_no, route, corridor_id, start_location,
         destination, battery_kwh, bus_max_kw, start_soc, start_soc, t, t, is_demo),
    )


def get_trip(trip_id):
    rows = _query("SELECT * FROM trips WHERE id=?", (trip_id,))
    return rows[0] if rows else None


def find_active_trip(driver_id, bus_no):
    rows = _query(
        """SELECT * FROM trips WHERE lower(driver_id)=lower(?) AND lower(bus_no)=lower(?)
           AND status='active' AND is_demo=0 ORDER BY id DESC LIMIT 1""",
        (driver_id.strip(), bus_no.strip()),
    )
    return rows[0] if rows else None


def update_trip(trip_id, **fields):
    fields["updated_at"] = _ts(now())
    cols = ", ".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE trips SET {cols} WHERE id=?", (*fields.values(), trip_id))


def end_trip(trip_id):
    t = _ts(now())
    _exec("UPDATE sessions SET status='cancelled' WHERE trip_id=? AND status='planned'", (trip_id,))
    _exec("UPDATE sessions SET status='done', end_ts=? WHERE trip_id=? AND status='charging'", (t, trip_id))
    update_trip(trip_id, status="ended")


def active_trips(corridor_id):
    return pd.DataFrame(_query(
        "SELECT * FROM trips WHERE corridor_id=? AND status='active' ORDER BY id", (corridor_id,)))


# ---------------------------------------------------------------- sessions --
def add_session(trip_id, corridor_id, station_id, station_name, arrival, duration_min,
                soc_from, soc_to, power_kw, status="planned", start=None, is_demo=0):
    return _exec(
        """INSERT INTO sessions (trip_id, corridor_id, station_id, station_name, arrival_ts, start_ts,
           soc_from, soc_to, power_kw, duration_min, status, created_at, is_demo)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (trip_id, corridor_id, int(station_id), station_name, _ts(arrival), _ts(start),
         soc_from, soc_to, power_kw, duration_min, status, _ts(now()), is_demo),
    )


def set_session(session_id, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), session_id))


def _parse_session(r):
    r["arrival"] = _dt(r["arrival_ts"])
    r["start"] = _dt(r["start_ts"])
    r["end"] = _dt(r["end_ts"])
    return r


def open_sessions(corridor_id):
    """Planned + charging sessions on a corridor that still matter for queueing."""
    ref = now()
    out = []
    for r in _query("SELECT * FROM sessions WHERE corridor_id=? AND status IN ('planned','charging')",
                    (corridor_id,)):
        r = _parse_session(r)
        if r["status"] == "planned" and r["arrival"] < ref - timedelta(minutes=STALE_PLAN_MIN):
            continue
        if r["status"] == "charging" and r["start"] and \
                r["start"] + timedelta(minutes=r["duration_min"]) <= ref:
            continue
        out.append(r)
    return out


def trip_open_session(trip_id):
    rows = _query("SELECT * FROM sessions WHERE trip_id=? AND status IN ('planned','charging') "
                  "ORDER BY id DESC LIMIT 1", (trip_id,))
    return _parse_session(rows[0]) if rows else None


def trip_history(trip_id):
    return pd.DataFrame(_query("SELECT * FROM sessions WHERE trip_id=? ORDER BY id DESC", (trip_id,)))


def cancel_trip_plans(trip_id):
    _exec("UPDATE sessions SET status='cancelled' WHERE trip_id=? AND status='planned'", (trip_id,))


# ---------------------------------------------------------------- stations --
def build_stations(corridor_id, existing, ocm, conns):
    """Station table for one corridor with power / voltage / amps / ports."""
    cols = ["station_id", "name", "town", "operator", "lat", "lon", "chainage_km",
            "power_kw", "voltage_v", "v_assumed", "amps", "ports", "dc", "bus_capable"]
    if existing is None or existing.empty or "corridor_id" not in existing.columns:
        return pd.DataFrame(columns=cols)
    df = existing[existing["corridor_id"] == corridor_id].drop_duplicates("ocm_id").copy()
    if df.empty:
        return pd.DataFrame(columns=cols)

    ports = pd.Series(dtype=float)
    if ocm is not None and not ocm.empty:
        ports = ocm.drop_duplicates("ocm_id").set_index("ocm_id")["number_of_points"]
    df["ports"] = df["ocm_id"].map(ports).fillna(1).clip(1, MAX_PORTS).astype(int)

    df["conn_kw"], df["voltage_v"], df["amps"], df["dc"] = np.nan, np.nan, np.nan, False
    if conns is not None and not conns.empty:
        best = conns.sort_values("power_kw", ascending=False).drop_duplicates("ocm_id").set_index("ocm_id")
        df["conn_kw"] = df["ocm_id"].map(best["power_kw"])
        df["voltage_v"] = df["ocm_id"].map(best["voltage"])
        df["amps"] = df["ocm_id"].map(best["amps"])
        df["dc"] = df["ocm_id"].map(best["current_type"].astype(str).str.contains("DC", na=False)).fillna(False)

    df["power_kw"] = df["max_power_kw"].fillna(df["conn_kw"]).fillna(22.0)
    df["dc"] = df["dc"].astype(bool) | (df["power_kw"] >= BUS_MIN_KW)
    df["v_assumed"] = df["voltage_v"].isna()
    assumed_v = np.where(df["dc"], ASSUMED_DC_VOLTAGE, ASSUMED_AC_VOLTAGE)
    df["voltage_v"] = df["voltage_v"].fillna(pd.Series(assumed_v, index=df.index))
    df["amps"] = df["amps"].fillna(df["power_kw"] * 1000.0 / df["voltage_v"])
    df["bus_capable"] = df["power_kw"] >= BUS_MIN_KW

    out = df.rename(columns={"ocm_id": "station_id", "title": "name"})
    out["town"] = out["town"].fillna("")
    out["operator"] = out["operator"].fillna("Unknown operator")
    out["name"] = out["name"].fillna("Charging station")
    out["station_id"] = out["station_id"].astype(int)
    return out[cols].sort_values("chainage_km").reset_index(drop=True)


# ----------------------------------------------------------- charging maths --
def charging_profile(battery_kwh, soc_from, soc_to, station_kw, bus_max_kw):
    """Simulated charging time. Power = min(station, bus acceptance); taper above 80 % SoC."""
    p = max(1.0, min(float(station_kw), float(bus_max_kw)))
    eff_p = p * CHARGE_EFFICIENCY                       # kW that really reaches the battery
    soc_from, soc_to = max(0.0, float(soc_from)), min(100.0, float(soc_to))
    if soc_to <= soc_from:
        return dict(minutes=0.0, battery_kwh=0.0, grid_kwh=0.0, power_kw=p, eff_kw=eff_p)
    fast = max(0.0, min(soc_to, TAPER_FROM_SOC) - soc_from)
    slow = max(0.0, soc_to - max(soc_from, TAPER_FROM_SOC))
    e_fast, e_slow = fast / 100 * battery_kwh, slow / 100 * battery_kwh
    hours = e_fast / eff_p + e_slow / (eff_p * TAPER_POWER_FACTOR)
    e_batt = e_fast + e_slow
    return dict(minutes=hours * 60, battery_kwh=e_batt, grid_kwh=e_batt / CHARGE_EFFICIENCY,
                power_kw=p, eff_kw=eff_p)


# --------------------------------------------------------------- queue model --
def _free_ports(sessions, ports, ref):
    free = [ref] * int(ports)
    for s in sessions:
        if s["status"] == "charging" and s["start"]:
            end = s["start"] + timedelta(minutes=s["duration_min"])
            if end > ref:
                i = int(np.argmin(free))
                free[i] = max(free[i], end)
    planned = sorted((s for s in sessions if s["status"] == "planned"), key=lambda s: s["arrival"])
    return free, planned


def quote(sessions, ports, arrival, duration_min, ref=None, exclude_trip=None):
    """FIFO wait for a bus arriving at `arrival` and needing `duration_min` of charging."""
    ref = ref or now()
    sessions = [s for s in sessions if s["trip_id"] != exclude_trip]
    free, planned = _free_ports(sessions, ports, ref)
    for s in planned:
        arr = max(s["arrival"], ref)
        if arr > arrival:
            break
        i = int(np.argmin(free))
        free[i] = max(arr, free[i]) + timedelta(minutes=s["duration_min"])
    i = int(np.argmin(free))
    start = max(arrival, free[i])
    return dict(wait_min=(start - arrival).total_seconds() / 60.0, start=start,
                end=start + timedelta(minutes=duration_min))


def station_status(sessions_here, ports, ref=None, exclude_trip=None):
    ref = ref or now()
    ss = [s for s in sessions_here if s["trip_id"] != exclude_trip]
    charging = sum(1 for s in ss if s["status"] == "charging")
    arriving = sum(1 for s in ss if s["status"] == "planned" and
                   max(s["arrival"], ref) <= ref + timedelta(minutes=30))
    wait = max(quote(ss, ports, ref, 30, ref)["wait_min"],
               quote(ss, ports, ref + timedelta(minutes=20), 30, ref)["wait_min"])
    level = "free" if wait < FREE_WAIT_MIN else ("busy" if wait < BUSY_WAIT_MIN else "congested")
    return dict(charging=charging, arriving=arriving, wait_min=round(wait), level=level)


def all_station_status(stations, sessions, exclude_trip=None):
    ref = now()
    by_station = {}
    for s in sessions:
        by_station.setdefault(s["station_id"], []).append(s)
    return {int(r.station_id): station_status(by_station.get(int(r.station_id), []), int(r.ports), ref, exclude_trip)
            for r in stations.itertuples()}


def recommend(stations, sessions, trip, target_soc, consumption, reserve_pct, route_km, only_bus=True, strict=True):
    """Rank stations ahead of the bus by total time (drive + wait + charge).

    strict=True keeps only stops that matter: reachable with the reserve intact, a meaningful
    charge (>= 5 %), and a target high enough to finish the route.
    """
    ref = now()
    cap, pos, soc = trip["battery_kwh"], trip["position_km"], trip["current_soc"]
    rows = []
    for r in stations.itertuples():
        if only_bus and not r.bus_capable:
            continue
        dist = r.chainage_km - pos
        if dist < -0.5:
            continue
        dist = max(dist, 0.0)
        soc_arr = soc - dist * consumption / cap * 100
        if target_soc - soc_arr < 5:
            continue
        reachable = soc_arr >= reserve_pct
        rest_km = max(route_km - r.chainage_km, 0.0)
        needed = reserve_pct + 5 + rest_km * consumption / cap * 100
        finish_ok = target_soc >= needed
        if strict and not (reachable and finish_ok):
            continue
        prof = charging_profile(cap, max(soc_arr, 0), target_soc, r.power_kw, trip["bus_max_kw"])
        travel = dist / AVG_SPEED_KMPH * 60
        here = [s for s in sessions if s["station_id"] == r.station_id]
        q = quote(here, int(r.ports), ref + timedelta(minutes=travel), prof["minutes"], ref, trip["id"])
        rows.append(dict(station_id=int(r.station_id), Station=r.name, Town=r.town,
                         km_ahead=round(dist, 1), soc_arrival=round(soc_arr, 1), power_kw=prof["power_kw"],
                         ports=int(r.ports), travel_min=round(travel), wait_min=round(q["wait_min"]),
                         charge_min=round(prof["minutes"]), total_min=round(travel + q["wait_min"] + prof["minutes"]),
                         leave_at=q["end"], reachable=bool(reachable), finish_ok=bool(finish_ok)))
    df = pd.DataFrame(rows)
    return df.sort_values("total_min").reset_index(drop=True) if not df.empty else df


def soc_at_destination(trip, route_km, consumption):
    """Battery % on arrival at the destination if the bus did not charge any more."""
    return trip["current_soc"] - max(route_km - trip["position_km"], 0) * consumption / trip["battery_kwh"] * 100


# ------------------------------------------------------------ route geometry --
def route_points(routes_geojson, corridor_id):
    for f in routes_geojson.get("features", []):
        if f.get("properties", {}).get("corridor_id") == corridor_id:
            g = f["geometry"]
            coords = g["coordinates"]
            if g["type"] == "MultiLineString":
                coords = [p for line in coords for p in line]
            return [(float(p[0]), float(p[1])) for p in coords]
    return []


def _hav(a, b):
    from math import radians, sin, cos, asin, sqrt
    la1, lo1, la2, lo2 = map(radians, (a[1], a[0], b[1], b[0]))
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(h))


def point_at_km(points, km, total_km):
    if not points:
        return None
    cum = np.cumsum([0.0] + [_hav(points[i], points[i + 1]) for i in range(len(points) - 1)])
    cum = cum * (total_km / cum[-1]) if cum[-1] > 0 else cum
    lon = float(np.interp(km, cum, [p[0] for p in points]))
    lat = float(np.interp(km, cum, [p[1] for p in points]))
    return lat, lon


def view_for(points, fallback=(13.0, 76.5, 6.5)):
    if not points:
        return fallback
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    span = max(max(lats) - min(lats), (max(lons) - min(lons)) * 0.97, 0.05)
    zoom = float(np.clip(np.log2(900 / span) - 1.0, 5, 11))
    return (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2, zoom


# ------------------------------------------------------------------ demo data --
DEMO_DRIVERS = [("Ravi Kumar", "Airavat EV 101"), ("Suresh Gowda", "Airavat EV 102"),
                ("Manjunath S", "Airavat EV 103"), ("Lakshmi N", "Airavat EV 104"),
                ("Imran Pasha", "Airavat EV 105"), ("Deepa R", "Airavat EV 106")]


def clear_demo(corridor_id):
    _exec("DELETE FROM sessions WHERE is_demo=1 AND corridor_id=?", (corridor_id,))
    _exec("DELETE FROM trips WHERE is_demo=1 AND corridor_id=?", (corridor_id,))


def seed_demo(corridor_id, route, destination, stations, route_km):
    """Create a few simulated buses so congestion is visible for testing."""
    clear_demo(corridor_id)
    cand = stations[stations["bus_capable"]]
    fast = cand[(cand["ports"] <= 4) & (cand["power_kw"] >= 100)]
    small = cand[cand["ports"] <= 4]
    cand = fast if len(fast) >= 2 else (small if len(small) >= 2 else cand)
    if cand.empty:
        return 0
    rng = np.random.default_rng(7)
    picks = [cand.iloc[int(len(cand) * f)] for f in (0.35, 0.65)] if len(cand) > 1 else [cand.iloc[0]]
    ref = now()
    made, d = 0, 0

    def new_bus(pos, soc):
        nonlocal d, made
        name, bus = DEMO_DRIVERS[d % len(DEMO_DRIVERS)]
        tid = create_trip(name, f"DEMO{d+1:02d}", "-", bus, f"KA-57-F-{9000+d}", route, corridor_id,
                          "Bengaluru", destination, 360, 150, soc, is_demo=1)
        update_trip(tid, position_km=float(pos), current_soc=float(soc))
        d += 1
        made += 1
        return tid

    for k, st_row in enumerate(picks):
        n_charging = int(st_row["ports"]) if k == 0 else 1
        n_waiting = 2 if k == 0 else 0
        for _ in range(n_charging):
            prof = charging_profile(360, 30, 70, st_row["power_kw"], 150)
            tid = new_bus(st_row["chainage_km"], 45)
            started = ref - timedelta(minutes=float(rng.integers(5, 25)))
            add_session(tid, corridor_id, st_row["station_id"], st_row["name"], started, prof["minutes"],
                        45, 70, prof["power_kw"], "charging", started, 1)
        for _ in range(n_waiting):
            prof = charging_profile(360, 30, 70, st_row["power_kw"], 150)
            tid = new_bus(max(st_row["chainage_km"] - 8, 0), 50)
            add_session(tid, corridor_id, st_row["station_id"], st_row["name"],
                        ref + timedelta(minutes=float(rng.integers(5, 20))), prof["minutes"],
                        45, 70, prof["power_kw"], "planned", None, 1)
    new_bus(route_km * 0.15, 78)
    new_bus(route_km * 0.85, 55)
    return made
