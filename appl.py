import sys
import types
import os
import json
import requests
import pandas as pd
import streamlit as st
import ee
import folium
from folium import plugins
from streamlit_folium import st_folium

# -----------------------------
# PATCHES & DUMMY MODULES
# -----------------------------
dummy_blessings = types.ModuleType('blessings')
class DummyTerminal:
    def __init__(self, *args, **kwargs):
        self.number_of_colors, self.height, self.width = 0, 24, 80
    def __getattr__(self, name): return lambda *args, **kwargs: ''
    def location(self, *args, **kwargs): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass

dummy_blessings.Terminal = DummyTerminal
sys.modules['blessings'] = dummy_blessings

# -----------------------------
# PAGE SETTINGS
# -----------------------------
st.set_page_config(page_title="LULC Classifier", layout="wide")
st.title("🌍 Land Use And Land Cover Classification")

# -----------------------------
# INITIALIZE EARTH ENGINE
# -----------------------------
service_account = "gee-streamlit@lulc-dash.iam.gserviceaccount.com"
key_file = "lulc-dash-fc10dde0b2ed.json"

if not os.path.exists(key_file):
    st.error(f"❌ Key file '{key_file}' not found. Please ensure it is in the same directory as this script.")
    st.stop()

try:
    credentials = ee.ServiceAccountCredentials(service_account, key_file)
    ee.Initialize(credentials)
    st.sidebar.success("✅ Earth Engine Connected")
except Exception as e:
    st.error(f"❌ Earth Engine failed: {e}")
    st.stop()

# -----------------------------
# UI CONTROLS
# -----------------------------
with st.sidebar:
    st.header("Settings")
    start_date_val = st.date_input("Start Date", value=pd.to_datetime("2023-01-01"))
    start_date = start_date_val.strftime('%Y-%m-%d')
    end_date = st.date_input("End Date", value=pd.to_datetime("2023-11-30")).strftime('%Y-%m-%d')
    st.info("Draw a rectangle or polygon on the map to start analysis.")

# -----------------------------
# SESSION STATE INIT
# -----------------------------
if "lulc_result" not in st.session_state:
    st.session_state.lulc_result = None
if "last_drawing" not in st.session_state:
    st.session_state.last_drawing = None

# -----------------------------
# MAIN MAP (AOI SELECTION)
# -----------------------------
st.subheader("1. Define Area of Interest")
Map = folium.Map(location=[17.4, 78.4], zoom_start=10)
folium.TileLayer(
    tiles="https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
    attr="Google Satellite",
    name="Google Satellite"
).add_to(Map)

draw = plugins.Draw(export=False, draw_options={
    "polyline": False, "circle": False, "marker": False,
    "circlemarker": False, "rectangle": True, "polygon": True
})
draw.add_to(Map)
folium.LayerControl().add_to(Map)
map_data = st_folium(Map, height=450, width=1000, key="aoi_map")

# -----------------------------
# PROCESSING LOGIC
# -----------------------------
if map_data and map_data.get("last_active_drawing"):
    drawing = map_data["last_active_drawing"]

    # Only reprocess if the drawing has changed
    if drawing != st.session_state.last_drawing:
        st.session_state.last_drawing = drawing
        st.session_state.lulc_result = None  # Reset previous result

    geometry = drawing["geometry"]

    try:
        aoi = ee.Geometry(geometry)

        # Compute map center from AOI bounds
        bounds = aoi.bounds().getInfo()['coordinates'][0]
        center_lat = (bounds[0][1] + bounds[2][1]) / 2
        center_lon = (bounds[0][0] + bounds[2][0]) / 2

        with st.spinner("🛰️ Fetching Data..."):
            worldcover = ee.ImageCollection("ESA/WorldCover/v200").first().clip(aoi)

            remapped = worldcover.select('Map').remap(
                [10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100],
                [3,  3,  3,  2,  1,  0,  0,  4,  4,  4,  3]
            ).rename('classification')

            smoothed = remapped.reduceNeighborhood(
                reducer=ee.Reducer.mode(),
                kernel=ee.Kernel.square(1, 'pixels')
            )

            # Rename correctly after reduceNeighborhood (band gets a suffix)
            band_name = smoothed.bandNames().getInfo()[0]
            smoothed = smoothed.select(band_name).rename('classification')

        # -----------------------------
        # DISPLAY LULC MAP
        # -----------------------------
        st.subheader("2. LULC Classification Result")
        Map2 = folium.Map(location=[center_lat, center_lon], zoom_start=11)

        sentinel = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(aoi)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 10))
            .median()
            .clip(aoi)
        )

        s2_id = sentinel.getMapId({'bands': ['B4', 'B3', 'B2'], 'min': 0, 'max': 3000})
        folium.TileLayer(
            tiles=s2_id['tile_fetcher'].url_format,
            attr="Sentinel-2",
            name="Sentinel-2"
        ).add_to(Map2)

        viz = {'min': 0, 'max': 4, 'palette': ['#D2B48C', '#FF0000', '#FFFF00', '#008000', '#0000FF']}
        lulc_id = smoothed.getMapId(viz)
        folium.TileLayer(
            tiles=lulc_id['tile_fetcher'].url_format,
            attr="ESA WorldCover",
            name="LULC",
            overlay=True
        ).add_to(Map2)

        folium.LayerControl().add_to(Map2)
        st_folium(Map2, height=500, width=1000, key="lulc_map")

        # -----------------------------
        # STATISTICS
        # -----------------------------
        st.subheader("3. Landscape Statistics")
        stats = smoothed.reduceRegion(
            reducer=ee.Reducer.frequencyHistogram(),
            geometry=aoi,
            scale=10,
            maxPixels=1e9
        ).get('classification').getInfo()

        if stats:
            class_names = ['Bare Soil', 'Built-up', 'Cropland', 'Vegetation', 'Water']
            colors = ['#D2B48C', '#FF0000', '#FFFF00', '#008000', '#0000FF']

            total_pixels = sum(stats.values())
            area_sqkm = (total_pixels * 100) / 1_000_000

            cols = st.columns(len(class_names))
            lulc_summary = {}

            for i, name in enumerate(class_names):
                count = stats.get(str(i), 0)
                perc = (count / total_pixels) * 100
                lulc_summary[name] = round(perc, 2)
                with cols[i]:
                    st.markdown(
                        f"""<div style=' border-top:5px solid {colors[i]};
                        padding:10px;
                        background:#f0f2f6;
                        color:black;   /* 👈 THIS FIXES IT */
                        border-radius:8px;
                    '>
                        <b>{name}</b><br>{perc:.1f}%
                    </div>
                    """,
                    unsafe_allow_html=True
                    )

            st.metric("Estimated Total Area", f"{area_sqkm:.2f} km²")

            # Store result in session state so widgets below don't trigger rerun
            st.session_state.lulc_result = {
                "lulc_summary": lulc_summary,
                "area_sqkm": area_sqkm
            }

    except Exception as e:
        st.error(f"Processing Error: {e}")

else:
    st.info("💡 Use the draw tools on the map to select an area for classification.")

# -----------------------------
# AI ANALYSIS — outside the drawing block to avoid rerun loss
# -----------------------------
if st.session_state.lulc_result:
    lulc_summary = st.session_state.lulc_result["lulc_summary"]
    area_sqkm = st.session_state.lulc_result["area_sqkm"]

    st.markdown("---")
    st.subheader("4. 🤖 AI Environmental Report")

    analysis_type = st.selectbox(
        "Select Report Focus",
        ["Urban Expansion", "Water Scarcity", "Agricultural Health", "General Ecosystem"],
        key="analysis_type"
    )

    if st.button("Generate AI Insights", type="primary"):
        with st.spinner("Consulting Gemini AI..."):

            api_key = "AIzaSyChFrdpGQcrQES8DjJuglLMJ8Sk6qOCqt0"
            models_to_try = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.0-flash-lite"]

            prompt_text = f"""
You are an expert environmental scientist analyzing Land Use Land Cover (LULC) satellite data. But when generating the report dont mention this.
Area Size: {area_sqkm:.2f} km²
Land Cover Distribution:
{json.dumps(lulc_summary, indent=2)}
Analysis Focus: {analysis_type}

Provide a detailed, data-driven environmental assessment with the following sections:

**Key Observations**
Write 3-4 sentences describing the most significant findings from the land cover data. Reference specific percentages.

**Detailed Analysis**
Write a full paragraph (5-6 sentences) analyzing the implications of this land cover distribution for the {analysis_type} focus area. Be specific and scientific.

**Critical Recommendations**
• Provide 5 specific, actionable recommendations based on the data
• Each recommendation should reference the actual land cover percentages
• Include both short-term and long-term strategies

**Environmental Risks**
• List 4 specific risks based on the current land cover distribution
• Quantify risks where possible using the provided percentages

**Conclusion**
Write 2-3 sentences summarizing the overall environmental health of this area and priority actions.
"""

            payload = {
                "contents": [{"parts": [{"text": prompt_text}]}],
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 2048,
                    "topP": 0.9,
                    "topK": 40
                }
            }

            ai_text = None
            error_messages = []

            for model in models_to_try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
                try:
                    response = requests.post(
                        url, json=payload,
                        headers={"Content-Type": "application/json"},
                        timeout=60
                    )
                    if response.status_code == 200:
                        res_data = response.json()
                        ai_text = res_data['candidates'][0]['content']['parts'][0]['text']
                        break
                    else:
                        error_messages.append(f"{model}: HTTP {response.status_code} — {response.text[:200]}")
                except Exception as e:
                    error_messages.append(f"{model}: {str(e)[:80]}")

            if ai_text:
                # Render Gemini markdown properly in Streamlit
                st.markdown(ai_text)

                st.download_button(
                    label="📥 Download Report",
                    data=ai_text,
                    file_name="lulc_environmental_report.txt",
                    mime="text/plain"
                )
            else:
                st.error("❌ Gemini API failed. Please check your API key or network and try again.")
                with st.expander("🔍 Technical Details"):
                    for msg in error_messages:
                        st.write(msg)