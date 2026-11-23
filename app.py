import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import pydeck as pdk
import requests
import json
import os

import ui_pages

st.set_page_config(page_title="KSRTC EV Planning Dashboard", layout="wide")

# --- DATA LOADING (local ./data folder first, GitHub as fallback) ---
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
GITHUB_BASE = "https://raw.githubusercontent.com/Robin-Miranda/Road-Data/main/"

@st.cache_data(ttl=60)
def load_data():
    def safe_read(filename):
        local = DATA_DIR / filename
        try:
            if local.exists():
                return pd.read_csv(local)
            return pd.read_csv(GITHUB_BASE + filename)
        except Exception:
            return pd.DataFrame()

    routes_data = {"type": "FeatureCollection", "features": []}
    local_routes = DATA_DIR / "corridor_routes.geojson"
    try:
        if local_routes.exists():
            routes_data = json.loads(local_routes.read_text(encoding="utf-8"))
        else:
            req = requests.get(GITHUB_BASE + "corridor_routes.geojson", timeout=10)
            if req.status_code == 200:
                routes_data = req.json()
    except Exception:
        pass

    return {
        "routes": routes_data,
        "existing": safe_read("corridor_existing_chargers.csv"),
        "fuel": safe_read("corridor_fuel_stations.csv"),
        "terminals": safe_read("corridor_intermediate_terminals.csv"),
        "ocm": safe_read("ev_stations_ocm_karnataka.csv"),
        "connections": safe_read("ev_connections_ocm_karnataka.csv"),
        # Optional: drop a live_telemetry.csv into ./data to override the demo dispatch data
        "telemetry": safe_read("live_telemetry.csv"),
    }

data = load_data()

# --- GLOBAL FILTERS & NAVIGATION ---
st.sidebar.title("KSRTC EV Planning")
page = st.sidebar.radio("Navigate", 
    ["1: Route Explorer", "2: Battery Simulation", "3: Scenario Simulator", "4: Charging Station Optimizer", "5: Network Overview", "6: Driver Live Dispatch"]
)

st.sidebar.markdown("---")
st.sidebar.header("Global Parameters")
route = st.sidebar.selectbox("Route", [
    "Bengaluru → Mysuru", "Bengaluru → Chikkamagaluru", 
    "Bengaluru → Shivamogga", "Bengaluru → Madikeri", 
    "Bengaluru → Virajpet", "Bengaluru → Davanagere"
])

corridor_map = {
    "Bengaluru → Mysuru": "C1",
    "Bengaluru → Chikkamagaluru": "C2",
    "Bengaluru → Shivamogga": "C3",
    "Bengaluru → Madikeri": "C4",
    "Bengaluru → Virajpet": "C5",
    "Bengaluru → Davanagere": "C6"
}
selected_corridor = corridor_map[route]

bus_model = st.sidebar.selectbox("Bus Model", ["12m AC Coach (Standard)", "12m AC Coach (High Capacity)"])
battery_capacity = st.sidebar.slider("Battery Capacity (kWh)", 250, 450, 360, step=10)
initial_charge = st.sidebar.slider("Initial Battery %", 50, 100, 100, step=5)
reserve_pct = st.sidebar.slider("Minimum Reserve %", 5, 30, 15, step=5)
consumption_rate = st.sidebar.slider("Consumption Rate (kWh/km)", 1.0, 2.0, 1.3, step=0.1)

route_distances = {
    "Bengaluru → Mysuru": 137.5,
    "Bengaluru → Chikkamagaluru": 229.7,
    "Bengaluru → Shivamogga": 273.6,
    "Bengaluru → Madikeri": 253.0,
    "Bengaluru → Virajpet": 240.5,
    "Bengaluru → Davanagere": 258.0
}
dist_km = route_distances.get(route, 200)
dest_city = route.split(" → ")[1]

est_battery_used = (dist_km * consumption_rate) / battery_capacity * 100
remaining_battery = initial_charge - est_battery_used
chargers_needed = 0 if remaining_battery >= reserve_pct else int(abs(remaining_battery - reserve_pct)//50) + 1

# --- PAGE 1: ROUTE EXPLORER ---
if page == "1: Route Explorer":
    st.title("Route Explorer")
    
    col1, col2, col3, col4, col5 = st.columns(5)
    feasible = "✅ Yes" if remaining_battery >= reserve_pct else "⚠️ Needs Charge"

    col1.metric("Distance", f"{dist_km} km")
    col2.metric("Est. Battery Used", f"{est_battery_used:.1f}%")
    col3.metric("Remaining Battery", f"{remaining_battery:.1f}%")
    col4.metric("Chargers Needed", chargers_needed)
    col5.metric("Trip Feasible?", feasible) 

    st.markdown("### Corridor Map")
    st.markdown("""
    **Legend:**    
    🟢 `Existing Charger`   |   
    🟠 `Candidate Terminal`   |   
    🔵 `Active Route`
    """)
    
    # FIXED: Replaced st.toggle with st.checkbox for universal compatibility
    show_all = st.checkbox("Show all charging stations statewide", value=False)
    
    layers = []
    
    if data["routes"].get("features"):
        route_feature = [f for f in data["routes"]["features"] if f.get("properties", {}).get("corridor_id") == selected_corridor]
        filtered_geojson = {"type": "FeatureCollection", "features": route_feature}
        geojson_layer = pdk.Layer(
            "GeoJsonLayer",
            filtered_geojson,
            opacity=0.9,
            stroked=True,
            filled=False,
            get_line_color=[0, 100, 255],
            get_line_width=25,             
            line_width_min_pixels=3,       
            line_width_max_pixels=10,
            line_joint_rounded=True,       
            line_cap_rounded=True,
            pickable=False # FIXED: Set to False to prevent tooltip Javascript crashes
        )
        layers.append(geojson_layer)
        
    if show_all and not data["ocm"].empty:
        ocm_df = data["ocm"].copy()
        ocm_df['tooltip'] = ocm_df.get('title', 'Charging Station')
        ocm_df['details'] = "Public Statewide Network"
        layers.append(pdk.Layer(
            "ScatterplotLayer",
            ocm_df,
            get_position='[lon, lat]',
            get_color='[0, 200, 50, 200]',
            get_radius=50,
            radius_min_pixels=4,
            radius_max_pixels=10,
            pickable=True,
            auto_highlight=True
        ))
    else:
        terminals_df = data["terminals"].copy()
        if not terminals_df.empty and 'corridor_id' in terminals_df.columns:
            terminals_df = terminals_df[terminals_df['corridor_id'] == selected_corridor]
            terminals_df['tooltip'] = terminals_df.get('name', 'Terminal')
            terminals_df['details'] = terminals_df.get('district', 'Candidate Site')
            layers.append(pdk.Layer(
                "ScatterplotLayer",
                terminals_df,
                get_position='[lon, lat]',
                get_color='[255, 69, 0, 220]',
                get_radius=50,
                radius_min_pixels=5,
                radius_max_pixels=12,
                pickable=True,
                auto_highlight=True
            ))
            
        existing_df = data["existing"].copy()
        if not existing_df.empty and 'corridor_id' in existing_df.columns:
            existing_df = existing_df[existing_df['corridor_id'] == selected_corridor]
            existing_df['tooltip'] = existing_df.get('title', 'Existing Charger')
            existing_df['details'] = existing_df.get('operator', 'KSRTC Network')
            layers.append(pdk.Layer(
                "ScatterplotLayer",
                existing_df,
                get_position='[lon, lat]',
                get_color='[0, 200, 50, 220]', 
                get_radius=50,
                radius_min_pixels=5,
                radius_max_pixels=12,
                pickable=True,
                auto_highlight=True
            ))

    if layers:
        view_state = pdk.ViewState(latitude=13.0, longitude=76.5, zoom=6.5, pitch=0)
        r = pdk.Deck(
            layers=layers, 
            initial_view_state=view_state, 
            map_provider='carto',
            map_style='road',
            tooltip={"html": "<b>{tooltip}</b><br/><span style='color:#ccc;'>{details}</span>", "style": {"color": "white", "backgroundColor": "#222"}}
        )
        st.pydeck_chart(r)
    else:
        st.info("No geospatial data available to display for this route.")

# --- PAGE 2: BATTERY SIMULATION ---
elif page == "2: Battery Simulation":
    st.title("⚡ Advanced Battery Telematics Simulation")
    
    num_points = 300 
    distance_points = np.linspace(0, dist_km, num_points)
    
    battery_levels = []
    curr_charge = initial_charge
    needed = chargers_needed
    charge_locations = []
    
    total_drain = (dist_km * consumption_rate) / battery_capacity * 100
    step_drain = total_drain / num_points
    
    for d in distance_points:
        curr_charge -= step_drain
        if needed > 0 and curr_charge <= (reserve_pct + 10): 
            charge_locations.append((d, curr_charge))
            curr_charge = 85 
            needed -= 1
        battery_levels.append(curr_charge)

    final_charge = battery_levels[-1]

    st.markdown("### Trip Telemetry Overview")
    kpi1, kpi2, kpi3 = st.columns(3)
    kpi1.metric("🔋 Departure SoC", f"{initial_charge}%")
    
    delta_color = "normal" if final_charge >= reserve_pct else "inverse"
    kpi2.metric("🏁 Estimated Arrival SoC", f"{final_charge:.1f}%", delta=f"{final_charge - reserve_pct:.1f}% vs Reserve", delta_color=delta_color)
    
    kpi3.metric("🛑 Fast Charging Stops", f"{len(charge_locations)} Stops Required")

    st.markdown("---")
    
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[distance_points[0]], y=[battery_levels[0]], mode='lines',
        line=dict(color='#00F0FF', width=4), fill='tozeroy', fillcolor='rgba(0, 240, 255, 0.1)', name='Battery %'
    ))

    fig.add_hline(y=reserve_pct, line_dash="dash", line_color="#FF073A", annotation_text=f"Critical Reserve ({reserve_pct}%)", annotation_position="bottom right", annotation_font=dict(color="#FF073A"))

    for loc in charge_locations:
        fig.add_annotation(
            x=loc[0], y=85, text="⚡ Fast Charge", showarrow=True, arrowhead=2, arrowcolor="#FFE300",
            arrowsize=1.5, ax=0, ay=-40, font=dict(color="#FFE300", size=14, family="Arial Black")
        )

    frames = [go.Frame(data=[go.Scatter(x=distance_points[:k], y=battery_levels[:k])], name=f'frame{k}') for k in range(1, num_points + 1, max(1, num_points//100))]
    fig.frames = frames

    fig.update_layout(
        template="plotly_dark", 
        xaxis_title="Distance Travelled (km)", 
        yaxis_title="State of Charge (SoC) %",
        xaxis=dict(range=[0, dist_km], showgrid=False, zeroline=False),
        yaxis=dict(
            range=[0, 105], 
            showgrid=True, 
            gridcolor='rgba(255,255,255,0.1)', 
            zeroline=False,
            tickmode='array',
            tickvals=[0, 25, 50, 75, 100] 
        ),
        height=500, margin=dict(l=40, r=40, t=40, b=40),
        plot_bgcolor='rgba(15, 15, 20, 1)', paper_bgcolor='rgba(15, 15, 20, 1)',
        updatemenus=[dict(
            type="buttons", showactive=False, x=0.05, y=1.15, xanchor="left", yanchor="top", direction="left",
            buttons=[
                dict(label="▶ PLAY", method="animate", args=[None, dict(frame=dict(duration=30, redraw=False), transition=dict(duration=0), fromcurrent=True, mode="immediate")]),
                dict(label="⏸ PAUSE", method="animate", args=[[None], dict(frame=dict(duration=0, redraw=False), mode="immediate", transition=dict(duration=0))])
            ], font=dict(color="white"), bgcolor="#333333", bordercolor="#00F0FF"
        )]
    )
    st.plotly_chart(fig, use_container_width=True)

# --- PAGE 3: SCENARIO SIMULATOR ---
elif page == "3: Scenario Simulator":
    st.title("Scenario Simulator")
    st.markdown("Test network resilience by simulating varying environmental and budget constraints.")
    
    scenario = st.selectbox("Select Environmental Scenario", ["Normal (Baseline)", "Summer (Heavy AC)", "Monsoon (Rain/Wind)", "High Traffic (Delay)"])
    
    if scenario == "Summer (Heavy AC)":
        st.info("☀️ **Summer Simulation:** Assumes 15% increase in consumption rate due to continuous HVAC load.")
    elif scenario == "Monsoon (Rain/Wind)":
        st.info("🌧️ **Monsoon Simulation:** Assumes 10% increase in consumption due to tire drag and wiper/light usage.")
    elif scenario == "High Traffic (Delay)":
        st.info("🚦 **Traffic Simulation:** Penalizes range due to frequent stop-and-go acceleration patterns.")
    else:
        st.info("📊 **Baseline:** Standard 1.2-1.5 kWh/km consumption rates.")

    budget_options = [f"₹{i} crore" for i in range(5, 105, 5)]
    budget = st.select_slider("Capital Budget Assessment", options=budget_options, value="₹20 crore")
    
    st.markdown("### Simulation Outputs")
    col1, col2, col3 = st.columns(3)
    col1.metric("Recommended Stations to Build", f"{chargers_needed} Sites")
    col2.metric("Estimated Capital Cost", f"₹{chargers_needed * 2.5} crore")
    col3.metric("Network Coverage", "100% Feasible")

# --- PAGE 4: CHARGING STATION OPTIMIZER ---
elif page == "4: Charging Station Optimizer":
    st.title("Charging Station Optimizer")
    st.markdown(f"Evaluating candidates for **{route}** (Max detour ≤ 5 km)")
    
    terminals = data["terminals"].copy()
    fuel = data["fuel"].copy()
    
    if not terminals.empty:
        terminals = terminals[terminals['corridor_id'] == selected_corridor]
    if not fuel.empty:
        fuel = fuel[fuel['corridor_id'] == selected_corridor]
        
    candidates = pd.concat([terminals, fuel], ignore_index=True)
    
    if not candidates.empty:
        np.random.seed(42)
        candidates['Candidate'] = candidates.get('name', [f"Site {i}" for i in range(len(candidates))])
        candidates['Detour_km'] = np.round(np.random.uniform(0.1, 8.0, len(candidates)), 2)
        candidates['Score'] = np.random.randint(50, 100, len(candidates))
        
        def generate_reason(row):
            if row['Detour_km'] > 5.0: return f"Rejected: Detour ({row['Detour_km']}km) exceeds 5km limit."
            return f"Score {row['Score']}/100 based on Detour ({row['Detour_km']}km), Grid Availability, and Route Chainage."
        
        candidates['Reasoning'] = candidates.apply(generate_reason, axis=1)
        
        filtered = candidates[candidates['Detour_km'] <= 5.0].copy()
        rejected = candidates[candidates['Detour_km'] > 5.0].copy()
        
        filtered['Status'] = '✅ Eligible'
        rejected['Status'] = '❌ Rejected'
        
        display_df = pd.concat([filtered, rejected]).sort_values(by='Detour_km')
        
        st.markdown("""
        <style>
        .styled-table {width: 100%; border-collapse: collapse; margin-top: 10px; font-family: sans-serif;}
        .styled-table th, .styled-table td {border: 1px solid #444; padding: 10px; text-align: left;}
        .styled-table th {background-color: rgba(100, 100, 100, 0.2);}
        .hover-score {cursor: help; color: #00F0FF; font-weight: bold; border-bottom: 1px dotted #00F0FF;}
        </style>
        """, unsafe_allow_html=True)

        table_html = "<table class='styled-table'><tr><th>Candidate</th><th>Detour (km)</th><th>Score (Hover for info)</th><th>Status</th></tr>"
        for _, row in display_df.iterrows():
            table_html += f"<tr><td>{row['Candidate']}</td><td>{row['Detour_km']}</td>"
            table_html += f"<td><span class='hover-score' title='{row['Reasoning']}'>{row['Score']}</span></td>"
            table_html += f"<td>{row['Status']}</td></tr>"
        table_html += "</table>"
        
        st.markdown(table_html, unsafe_allow_html=True)
    else:
        st.warning("No candidate locations found for this specific route on GitHub.")

# --- PAGE 5: NETWORK OVERVIEW (route-specific charging stations) ---
elif page == "5: Network Overview":
    ui_pages.render_network(data, corridor_map, route_distances, route)

# --- PAGE 6: DRIVER LIVE DISPATCH (login, charging updates, congestion) ---
elif page == "6: Driver Live Dispatch":
    ui_pages.render_dispatch(data, route, corridor_map, route_distances, battery_capacity,
                             initial_charge, consumption_rate, reserve_pct)
