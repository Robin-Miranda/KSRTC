"""Streamlit UI for page 5 (Network Overview) and page 6 (Driver Live Dispatch)."""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pydeck as pdk
import streamlit as st

import dispatch as dp


# ------------------------------------------------------------------ helpers --
def _plot(fig):
    try:
        st.plotly_chart(fig, width="stretch")
    except TypeError:
        st.plotly_chart(fig, use_container_width=True)


def _fmt_t(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


def _route_layer(routes, corridor_id, opacity=0.9):
    feats = [f for f in routes.get("features", []) if f.get("properties", {}).get("corridor_id") == corridor_id]
    if not feats:
        return None
    return pdk.Layer("GeoJsonLayer", {"type": "FeatureCollection", "features": feats},
                     opacity=opacity, stroked=True, filled=False, get_line_color=[0, 100, 255],
                     get_line_width=25, line_width_min_pixels=3, line_width_max_pixels=10,
                     line_joint_rounded=True, line_cap_rounded=True, pickable=False)


def _dots(df, color, radius_px=(5, 12)):
    return pdk.Layer("ScatterplotLayer", df, get_position="[lon, lat]", get_fill_color=color,
                     get_radius=50, radius_min_pixels=radius_px[0], radius_max_pixels=radius_px[1],
                     pickable=True, auto_highlight=True)


def _deck(layers, view, pitch=0):
    lat, lon, zoom = view
    return pdk.Deck(layers=layers, map_provider="carto", map_style="road",
                    initial_view_state=pdk.ViewState(latitude=lat, longitude=lon, zoom=zoom, pitch=pitch),
                    tooltip={"html": "<b>{tooltip}</b><br/><span style='color:#ccc;'>{details}</span>",
                             "style": {"color": "white", "backgroundColor": "#222"}})


# =========================================================== PAGE 5: NETWORK ==
def render_network(data, corridor_map, route_distances, default_route):
    st.title("Network Overview")
    st.markdown("Pick a route to see **only the charging stations on that route** (green dots).")

    routes = list(corridor_map)
    c1, c2 = st.columns([2, 1])
    sel = c1.selectbox("Select route", routes, index=routes.index(default_route), key="net_route")
    only_bus = c2.checkbox("Only bus-capable (DC ≥ 60 kW)", value=False, key="net_busonly")
    cid = corridor_map[sel]
    route_km = route_distances.get(sel, 0)

    ex = data["existing"]
    if ex.empty or "corridor_id" not in ex.columns:
        st.warning("Charging-station data is not available.")
        return
    stn = ex[ex["corridor_id"] == cid].drop_duplicates("ocm_id").copy()
    stn["max_power_kw"] = stn["max_power_kw"].fillna(0)
    if only_bus:
        stn = stn[stn["max_power_kw"] >= dp.BUS_MIN_KW]
    stn = stn.sort_values("chainage_km")

    # ---- summary metrics
    ch = [0.0] + stn["chainage_km"].tolist() + [float(route_km)]
    max_gap = max(np.diff(sorted(ch))) if len(ch) > 1 else route_km
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Route length", f"{route_km} km")
    m2.metric("Charging stations on route", len(stn))
    m3.metric("Fast (≥ 60 kW)", int((stn["max_power_kw"] >= dp.BUS_MIN_KW).sum()))
    m4.metric("Longest gap between chargers", f"{max_gap:.0f} km")

    st.markdown("**Legend:** 🟢 Charging station on this route   |   🔵 Selected route")

    pts = dp.route_points(data["routes"], cid)
    layers = []
    rl = _route_layer(data["routes"], cid, 0.7)
    if rl:
        layers.append(rl)
    if not stn.empty:
        df = pd.DataFrame({
            "lat": stn["lat"].astype(float), "lon": stn["lon"].astype(float),
            "tooltip": stn["title"].fillna("Charging station").astype(str),
            "details": [f"{o} · {int(p)} kW · {c:.0f} km from Bengaluru"
                        for o, p, c in zip(stn["operator"].fillna("Unknown operator"),
                                           stn["max_power_kw"], stn["chainage_km"])]})
        layers.append(_dots(df, [0, 200, 50, 230]))
    else:
        st.info("No charging stations match the filter for this route.")
    st.pydeck_chart(_deck(layers, dp.view_for(pts)))

    if not stn.empty:
        st.markdown(f"### Charging stations on {sel} (in driving order)")
        table = pd.DataFrame({
            "Km from Bengaluru": stn["chainage_km"].round(1), "Station": stn["title"],
            "Town": stn["town"], "Operator": stn["operator"], "Power (kW)": stn["max_power_kw"].astype(int),
            "Bus-capable": np.where(stn["max_power_kw"] >= dp.BUS_MIN_KW, "Yes", "No")})
        st.dataframe(table, hide_index=True, width="stretch") if _has_width() else \
            st.dataframe(table, hide_index=True, use_container_width=True)


def _has_width():
    import inspect
    return "width" in inspect.signature(st.dataframe).parameters


# ========================================================= PAGE 6: DISPATCH ==
def render_dispatch(data, route, corridor_map, route_distances, battery_capacity,
                    initial_charge, consumption_rate, reserve_pct):
    dp.init_db()
    ss = st.session_state
    st.title("🚌 Driver Live Dispatch")
    st.caption("Log in, share your charging plan, and see live station availability and congestion "
               "from every bus on the route — so nobody queues unnecessarily.")

    trip = dp.get_trip(ss["trip_id"]) if ss.get("trip_id") else None
    if trip and trip["status"] != "active":
        ss.pop("trip_id", None)
        trip = None

    tab1, tab2, tab3 = st.tabs(["🔐 Driver Login & Trip Start", "🔋 Charging Update", "🚦 Stations & Congestion"])

    ctx_route = trip["route"] if trip else route
    cid = corridor_map[ctx_route]
    route_km = float(route_distances.get(ctx_route, 200))
    dest = ctx_route.split(" → ")[-1]
    stations = dp.build_stations(cid, data["existing"], data["ocm"], data.get("connections"))
    sessions = dp.open_sessions(cid)
    status = dp.all_station_status(stations, sessions, exclude_trip=None)

    # ------------------------------------------------------------- TAB 1 ----
    with tab1:
        if trip:
            st.success(f"Logged in: **{trip['driver_name']}** · Bus **{trip['bus_name']} ({trip['bus_no']})**")
            a, b, c, d = st.columns(4)
            a.metric("Route", trip["route"])
            b.metric("Started from", trip["start_location"])
            c.metric("Started at", _fmt_t(dp._dt(trip["started_at"])))
            d.metric("Battery now", f"{trip['current_soc']:.0f}%")
            if st.button("🚪 End trip / Log out"):
                dp.end_trip(trip["id"])
                ss.pop("trip_id", None)
                st.rerun()
        else:
            mode = st.radio("Driver access", ["Start a new trip", "Resume my active trip"], horizontal=True)
            if mode == "Start a new trip":
                with st.form("login_form"):
                    c1, c2 = st.columns(2)
                    name = c1.text_input("Driver name *")
                    drv_id = c2.text_input("Driver / Employee ID *")
                    phone = c1.text_input("Mobile number")
                    bus_name = c2.text_input("Bus name / service (e.g. Airavat EV)")
                    bus_no = c1.text_input("Bus registration no. * (e.g. KA-57-F-0012)")
                    routes = list(corridor_map)
                    r_sel = c2.selectbox("Route", routes, index=routes.index(route))
                    start_loc = c1.text_input("Start location", value="Bengaluru (Kempegowda Bus Station)")
                    cap = c2.number_input("Battery capacity (kWh)", 100, 600, int(battery_capacity), step=10)
                    max_kw = c1.number_input("Bus max charging power (kW)", 30, 400, 150, step=10,
                                             help="Highest power the bus can accept from a DC charger")
                    soc0 = c2.slider("Battery % at departure", 10, 100, int(initial_charge), step=1)
                    go_btn = st.form_submit_button("▶ Start Trip")
                if go_btn:
                    if not (name.strip() and drv_id.strip() and bus_no.strip()):
                        st.error("Driver name, Driver ID and Bus registration no. are required.")
                    elif dp.find_active_trip(drv_id, bus_no):
                        st.warning("An active trip already exists for this driver & bus — use **Resume my active trip**.")
                    else:
                        tid = dp.create_trip(name.strip(), drv_id.strip(), phone.strip(), bus_name.strip() or "EV Bus",
                                             bus_no.strip().upper(), r_sel, corridor_map[r_sel], start_loc.strip(),
                                             r_sel.split(" → ")[-1], float(cap), float(max_kw), float(soc0))
                        ss["trip_id"] = tid
                        st.rerun()
            else:
                with st.form("resume_form"):
                    r1, r2 = st.columns(2)
                    rid = r1.text_input("Driver / Employee ID")
                    rbus = r2.text_input("Bus registration no.")
                    rb = st.form_submit_button("Resume")
                if rb:
                    found = dp.find_active_trip(rid, rbus)
                    if found:
                        ss["trip_id"] = found["id"]
                        st.rerun()
                    else:
                        st.error("No active trip found for that Driver ID and Bus no.")

    # ------------------------------------------------------------- TAB 2 ----
    with tab2:
        if not trip:
            st.info("Please log in and start a trip in the first tab.")
        elif stations.empty:
            st.warning("No charging-station data for this route.")
        else:
            _tab_charging(trip, stations, sessions, status, route_km, consumption_rate, reserve_pct)

    # ------------------------------------------------------------- TAB 3 ----
    with tab3:
        _tab_congestion(trip, data, cid, ctx_route, dest, stations, sessions, route_km,
                        consumption_rate, reserve_pct)


# ------------------------------------------------------- charging update tab --
def _tab_charging(trip, stations, sessions, status, route_km, cons, reserve):
    ss = st.session_state
    ver = ss.get("ver", 0)
    tid, cap = trip["id"], trip["battery_kwh"]
    st.markdown(f"**{trip['bus_name']} · {trip['bus_no']}** — {trip['route']} · battery {cap:.0f} kWh · "
                f"max charge {trip['bus_max_kw']:.0f} kW")

    c1, c2 = st.columns(2)
    pos = c1.number_input("Distance covered from start (km)", 0.0, route_km, float(min(trip["position_km"], route_km)),
                          step=1.0, key=f"pos_{tid}_{ver}")
    soc = c2.slider("Battery % available now", 0, 100, int(round(trip["current_soc"])), key=f"soc_{tid}_{ver}")

    only_bus = st.checkbox("Show bus-capable chargers only (DC ≥ 60 kW)", value=True, key=f"ob_{tid}")
    ahead = stations[(stations["chainage_km"] >= pos - 0.5) & (stations["bus_capable"] | (not only_bus))]
    if ahead.empty:
        st.warning("No charging stations ahead of your position on this route.")
        return

    def label(sid):
        r = ahead[ahead["station_id"] == sid].iloc[0]
        return (f"{dp.LEVEL_ICON[status[sid]['level']].split()[0]} {r['name']} ({r['town']}) · "
                f"{max(r['chainage_km'] - pos, 0):.0f} km ahead · {r['power_kw']:.0f} kW · {int(r['ports'])} port(s)")

    sid = st.selectbox("Charging location (station ahead)", ahead["station_id"].tolist(), format_func=label,
                       key=f"stn_{tid}_{ver}")
    s = ahead[ahead["station_id"] == sid].iloc[0]
    dist = max(s["chainage_km"] - pos, 0.0)
    soc_arr = soc - dist * cons / cap * 100

    if soc_arr < 0 or soc_arr < reserve:
        st.error(f"⚠️ At the current battery level you would arrive with **{soc_arr:.1f}%** — below the "
                 f"{reserve}% reserve. Pick a nearer station.")
        near = dp.recommend(stations, sessions, {**trip, "position_km": pos, "current_soc": soc}, 100, cons, reserve, route_km, only_bus, strict=False)
        near = near[near["reachable"]] if not near.empty else near
        if not near.empty:
            st.info(f"Nearest reachable option: **{near.sort_values('km_ahead').iloc[0]['Station']}** "
                    f"({near.sort_values('km_ahead').iloc[0]['km_ahead']} km ahead).")
        return

    if soc_arr >= 95:
        st.info(f"🔋 You would reach this station with ~{soc_arr:.0f}% battery — no charging needed here. "
                "Choose a station further ahead, or update your battery % after driving.")
        _my_session(trip, stations)
        return
    lo = int(max(soc_arr, 0)) + 1
    target = st.slider("Charge up to (%)", lo, 100, max(lo, 90), key=f"tgt_{tid}_{ver}_{sid}")
    prof = dp.charging_profile(cap, soc_arr, target, s["power_kw"], trip["bus_max_kw"])
    ref = dp.now()
    travel = dist / dp.AVG_SPEED_KMPH * 60
    arrival = ref + pd.Timedelta(minutes=travel)
    here = [x for x in sessions if x["station_id"] == int(sid)]
    q = dp.quote(here, int(s["ports"]), arrival, prof["minutes"], ref, tid)
    amps = prof["power_kw"] * 1000 / s["voltage_v"]

    st.markdown("#### Simulated charging plan")
    a, b, c, d = st.columns(4)
    a.metric("Arrival battery", f"{soc_arr:.0f}%", help="Estimated from distance and consumption rate")
    b.metric("Energy to add", f"{prof['battery_kwh']:.0f} kWh", delta=f"{soc_arr:.0f}% → {target}%", delta_color="off")
    c.metric("Charging time", f"{prof['minutes']:.0f} min")
    d.metric("Expected wait", f"{q['wait_min']:.0f} min")
    e, f, g, h = st.columns(4)
    e.metric("Charging power", f"{prof['power_kw']:.0f} kW",
             help=f"min(station {s['power_kw']:.0f} kW, bus {trip['bus_max_kw']:.0f} kW); "
                  f"{dp.TAPER_FROM_SOC:.0f}–100% tapers")
    f.metric("Voltage / current", f"{s['voltage_v']:.0f} V · {amps:.0f} A" + (" *" if s["v_assumed"] else ""))
    g.metric("Arrive at station", _fmt_t(arrival))
    h.metric("Leave station", _fmt_t(q["end"]), delta=f"{travel + q['wait_min'] + prof['minutes']:.0f} min from now",
             delta_color="off")
    if s["v_assumed"]:
        st.caption("* Voltage not on record for this station — assumed typical value for its charger type.")

    lvl = status[int(sid)]["level"]
    if q["wait_min"] >= dp.BUSY_WAIT_MIN or lvl == "congested":
        st.error(f"🔴 **Congestion expected** at {s['name']}: ~{q['wait_min']:.0f} min waiting when you arrive.")
        rec = dp.recommend(stations, sessions, {**trip, "position_km": pos, "current_soc": soc}, target, cons, reserve, route_km, only_bus)
        rec = rec[rec["station_id"] != int(sid)] if not rec.empty else rec
        if not rec.empty:
            b0 = rec.iloc[0]
            mine = travel + q["wait_min"] + prof["minutes"]
            if b0["total_min"] < mine:
                st.success(f"💡 Better option: **{b0['Station']}** ({b0['km_ahead']} km ahead) — done in "
                           f"~{b0['total_min']} min vs ~{mine:.0f} min here (saves ~{mine - b0['total_min']:.0f} min).")
    elif q["wait_min"] >= dp.FREE_WAIT_MIN:
        st.warning(f"🟡 Station is busy — expect ~{q['wait_min']:.0f} min wait.")
    else:
        st.success("🟢 Charger expected to be free when you arrive.")

    st.markdown("---")
    b1, b2 = st.columns(2)
    if b1.button("💾 Save position & battery %"):
        dp.update_trip(tid, position_km=float(pos), current_soc=float(soc))
        st.success("Updated.")
        st.rerun()
    if b2.button("📍 Plan this charging stop", type="primary"):
        dp.update_trip(tid, position_km=float(pos), current_soc=float(soc))
        dp.cancel_trip_plans(tid)
        dp.add_session(tid, trip["corridor_id"], int(sid), s["name"], arrival, prof["minutes"],
                       float(soc_arr), float(target), prof["power_kw"])
        st.rerun()

    _my_session(trip, stations)


def _my_session(trip, stations):
    ss = st.session_state
    sess = dp.trip_open_session(trip["id"])
    st.markdown("#### Your current charging stop")
    if not sess:
        st.info("No charging stop planned yet.")
    else:
        ref = dp.now()
        if sess["status"] == "planned":
            st.info(f"📍 **Planned:** {sess['station_name']} · arrive ~{_fmt_t(sess['arrival'])} · "
                    f"{sess['soc_from']:.0f}% → {sess['soc_to']:.0f}% · ~{sess['duration_min']:.0f} min")
            x, y = st.columns(2)
            if x.button("🔌 I've arrived — start charging"):
                dp.set_session(sess["id"], status="charging", start_ts=dp._ts(ref), arrival_ts=dp._ts(ref))
                st.rerun()
            if y.button("❌ Cancel plan"):
                dp.set_session(sess["id"], status="cancelled")
                st.rerun()
        else:
            elapsed = (ref - sess["start"]).total_seconds() / 60
            frac = min(1.0, elapsed / max(sess["duration_min"], 1))
            cur = sess["soc_from"] + (sess["soc_to"] - sess["soc_from"]) * frac
            st.success(f"🔌 **Charging** at {sess['station_name']} · started {_fmt_t(sess['start'])} · "
                       f"est. finish {_fmt_t(sess['start'] + pd.Timedelta(minutes=sess['duration_min']))}")
            st.progress(frac, text=f"≈ {cur:.0f}% battery ({frac*100:.0f}% of session)")
            if st.button("✅ Finish charging & continue trip"):
                stn_row = stations[stations["station_id"] == sess["station_id"]]
                upd = dict(current_soc=float(round(cur if frac < 1 else sess["soc_to"])))
                if not stn_row.empty:
                    upd["position_km"] = float(stn_row.iloc[0]["chainage_km"])
                dp.update_trip(trip["id"], **upd)
                dp.set_session(sess["id"], status="done", end_ts=dp._ts(ref))
                ss["ver"] = ss.get("ver", 0) + 1
                st.rerun()

    hist = dp.trip_history(trip["id"])
    if not hist.empty:
        with st.expander("Trip charging history"):
            h = hist[["station_name", "status", "soc_from", "soc_to", "duration_min", "arrival_ts"]].copy()
            h.columns = ["Station", "Status", "From %", "To %", "Planned min", "Arrival"]
            st.dataframe(h.round(0), hide_index=True)


# ----------------------------------------------------- congestion overview tab --
def _tab_congestion(trip, data, cid, route_name, dest, stations, sessions, route_km, cons, reserve):
    tid = trip["id"] if trip else None
    st.markdown(f"### {route_name} — live station availability")
    if stations.empty:
        st.warning("No charging-station data for this route.")
        return

    c1, c2, c3 = st.columns([1, 1, 1])
    only_bus = c1.checkbox("Bus-capable only (DC ≥ 60 kW)", value=True, key="cg_bus")
    target = c2.slider("Planning target %", 60, 100, 90, key="cg_target")
    c3.button("🔄 Refresh")

    status = dp.all_station_status(stations, sessions)
    view = stations[stations["bus_capable"]] if only_bus else stations

    # ---- recommendation for the logged-in driver
    if trip:
        rec = dp.recommend(stations, sessions, trip, target, cons, reserve, route_km, only_bus)
        st.markdown("#### Best charging option for you")
        dest_soc = dp.soc_at_destination(trip, route_km, cons)
        if dest_soc >= reserve:
            st.info(f"🔋 You can reach {dest} with ~{dest_soc:.0f}% left without charging. "
                    "Any stop below is optional.")
        else:
            st.warning(f"🔋 Without charging you would reach {dest} with only ~{dest_soc:.0f}% "
                       f"(reserve {reserve}%). A charging stop is needed.")
        ok = rec
        if ok.empty:
            st.info("No station ahead gives a meaningful charge that is reachable and gets you to the destination "
                    "with this target — try a higher planning target %.")
        else:
            b = ok.iloc[0]
            st.success(f"✅ **{b['Station']}** ({b['Town']}) — {b['km_ahead']} km ahead · arrive with {b['soc_arrival']}% · "
                       f"wait {b['wait_min']} min · charge {b['charge_min']} min · back on road at **{_fmt_t(b['leave_at'])}** "
                       f"(total ~{b['total_min']} min)")
            show = ok.head(5)[["Station", "km_ahead", "soc_arrival", "power_kw", "ports", "travel_min",
                               "wait_min", "charge_min", "total_min"]].copy()
            show.columns = ["Station", "Km ahead", "Arrival %", "kW", "Ports", "Drive (min)", "Wait (min)",
                            "Charge (min)", "Total (min)"]
            st.dataframe(show, hide_index=True)
    else:
        st.info("Log in (first tab) to get a personalised recommendation based on your bus and position.")

    # ---- map
    pts = dp.route_points(data["routes"], cid)
    layers = []
    rl = _route_layer(data["routes"], cid, 0.7)
    if rl:
        layers.append(rl)
    srows = []
    for r in view.itertuples():
        s = status[int(r.station_id)]
        srows.append(dict(lat=float(r.lat), lon=float(r.lon), tooltip=str(r.name),
                          details=f"{dp.LEVEL_ICON[s['level']]} · wait ~{s['wait_min']} min · {s['charging']} charging, "
                                  f"{s['arriving']} arriving ≤30 min · {r.power_kw:.0f} kW · {int(r.ports)} port(s)",
                          color=dp.LEVEL_COLOR[s["level"]]))
    if srows:
        layers.append(pdk.Layer("ScatterplotLayer", pd.DataFrame(srows), get_position="[lon, lat]",
                                get_fill_color="color", get_radius=50, radius_min_pixels=7, radius_max_pixels=14,
                                pickable=True, auto_highlight=True))
    trips_df = dp.active_trips(cid)
    brows = []
    for t in trips_df.itertuples():
        p = dp.point_at_km(pts, float(t.position_km), route_km)
        if p:
            me = tid is not None and int(t.id) == tid
            brows.append(dict(lat=p[0], lon=p[1], tooltip=f"{'YOU · ' if me else ''}{t.bus_name} ({t.bus_no})",
                              details=f"Driver {t.driver_name} · battery {t.current_soc:.0f}% · km {t.position_km:.0f}",
                              color=[0, 240, 255, 255] if me else [30, 110, 255, 255], r=11 if me else 8))
    if brows:
        layers.append(pdk.Layer("ScatterplotLayer", pd.DataFrame(brows), get_position="[lon, lat]",
                                get_fill_color="color", get_radius=50, radius_min_pixels=8, radius_max_pixels=16,
                                get_line_color=[255, 255, 255], stroked=True, line_width_min_pixels=2, pickable=True))
    st.markdown("**Legend:** 🟢 Free  🟡 Busy  🔴 Congested  |  🔵 Buses on route (cyan = you)")
    st.pydeck_chart(_deck(layers, dp.view_for(pts)))

    # ---- congestion along the route
    ch = pd.DataFrame({"km": view["chainage_km"], "name": view["name"],
                       "wait": [status[int(i)]["wait_min"] for i in view["station_id"]],
                       "level": [status[int(i)]["level"] for i in view["station_id"]]})
    fig = go.Figure(go.Bar(x=ch["km"], y=ch["wait"], marker_color=["rgb(%d,%d,%d)" % tuple(dp.LEVEL_COLOR[l][:3]) for l in ch["level"]],
                           text=ch["name"], hovertemplate="%{text}<br>km %{x}<br>wait ~%{y} min<extra></extra>"))
    if trip:
        fig.add_vline(x=trip["position_km"], line_dash="dash", line_color="#00F0FF", annotation_text="You")
    fig.update_layout(template="plotly_dark", height=300, margin=dict(l=40, r=20, t=30, b=40),
                      title="Expected waiting time by position on route", xaxis_title="Km from start",
                      yaxis_title="Wait (min)", xaxis=dict(range=[0, route_km]))
    _plot(fig)

    hot = ch[ch["level"] == "congested"]
    if not hot.empty:
        st.error("🔴 Congestion hotspots: " + ", ".join(f"{n} (km {k:.0f}, ~{w} min)" for n, k, w in
                                                         zip(hot["name"], hot["km"], hot["wait"])))

    # ---- station table
    tbl = pd.DataFrame({
        "Km": view["chainage_km"].round(1), "Station": view["name"], "Town": view["town"],
        "kW": view["power_kw"].astype(int), "Voltage (V)": view["voltage_v"].astype(int),
        "Ports": view["ports"],
        "Charging now": [status[int(i)]["charging"] for i in view["station_id"]],
        "Arriving ≤30 min": [status[int(i)]["arriving"] for i in view["station_id"]],
        "Est. wait (min)": [status[int(i)]["wait_min"] for i in view["station_id"]],
        "Status": [dp.LEVEL_ICON[status[int(i)]["level"]] for i in view["station_id"]]})
    st.dataframe(tbl, hide_index=True)

    if not trips_df.empty:
        with st.expander(f"Buses currently on this route ({len(trips_df)})"):
            bt = trips_df[["bus_name", "bus_no", "driver_name", "position_km", "current_soc"]].copy()
            bt.columns = ["Bus", "Bus no.", "Driver", "Km covered", "Battery %"]
            st.dataframe(bt.round(0), hide_index=True)

    with st.expander("🧪 Demo / test tools"):
        st.caption("Adds simulated buses charging and queuing on this route so you can see congestion working.")
        d1, d2 = st.columns(2)
        if d1.button("Load demo buses & queues"):
            n = dp.seed_demo(cid, route_name, dest, stations, route_km)
            st.success(f"Added {n} demo buses.")
            st.rerun()
        if d2.button("Clear demo data"):
            dp.clear_demo(cid)
            st.rerun()
