# KSRTC EV Planning Dashboard (v2)

Streamlit dashboard with 6 pages. **What's new in v2**

* **Page 5 – Network Overview:** choose a route and see green dots for the charging stations
  on *that route only* (optional "bus-capable DC >= 60 kW" filter), with a station list in
  driving order and the longest gap between chargers.
* **Page 6 – Driver Live Dispatch:** driver login, charging updates, simulated charging time and
  live congestion across all buses on the route.

## Run in VS Code
Windows:
```
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
streamlit run app.py
```
Mac / Linux: use `python3 -m venv .venv` and `source .venv/bin/activate`, rest is the same.
(Or run `run_windows.bat` / `./run_mac_linux.sh`; F5 in VS Code also launches it.)

## Driver Live Dispatch – how it works
1. **Login & Trip Start:** name, driver ID, mobile, bus name, bus no., route, start location, battery
   kWh, max charging power the bus accepts, battery % at departure. "Resume my active trip" logs a
   driver back in with Driver ID + Bus no.
2. **Charging Update:** driver enters km covered, battery % now, picks the charging station ahead and
   the % to charge up to. The backend calculates:
   * arrival battery % at that station (distance x consumption rate)
   * charging power = min(station kW, bus max kW); current = kW / voltage
   * charging time from the battery size, with a taper above 80 % SoC and 92 % efficiency
   * expected waiting time from a FIFO queue over the station's ports, using every other bus's plan
   * the time the bus leaves the station, and a better station if the chosen one is congested
   Buttons: *Plan this charging stop* -> *I've arrived* -> *Finish charging*.
3. **Stations & Congestion:** best option for the logged-in bus (drive + wait + charge time), map with
   stations coloured Green / Amber / Red, buses on the route, a chart of waiting time along the route,
   and a station table (power, voltage, ports, charging now, arriving soon, wait).

All drivers share one SQLite file, `data/dispatch.db` (created automatically), so plans made by one
driver are visible to the others. Use *Demo / test tools* (tab 3) to load simulated buses.
Delete `data/dispatch.db` to reset everything.

## Assumptions (edit at the top of `dispatch.py`)
Average speed 50 km/h, efficiency 92 %, taper above 80 % SoC, wait < 5 min = Free, < 20 min = Busy,
else Congested. Station kW, ports and voltage come from `data/` (OpenChargeMap export); where voltage is
missing a typical value (500 V DC / 230 V AC) is assumed and marked with *.

## Files
`app.py` (pages 1-4 + navigation) · `ui_pages.py` (pages 5 & 6) · `dispatch.py` (database, charging and
queue model) · `data/` (route and charger data; read locally, GitHub used only as fallback).
