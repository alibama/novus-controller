"""
kiln_analysis.py
================
Companion Streamlit page that *reads* the CSV logs the dashboard
writes and answers the question "how fast can my kiln actually go
at different temperatures?"

Run separately:
    streamlit run kiln_analysis.py --server.port 8502

Or add as a second page in a multi-page Streamlit app.

This page does NOT touch BLE — safe to run while the dashboard is
also running and connected to the kilns.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

LOG_DIR = Path(__file__).parent / "logs"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def list_kilns() -> list[str]:
    if not LOG_DIR.exists():
        return []
    return sorted(d.name for d in LOG_DIR.iterdir() if d.is_dir())


def load_kiln(name: str, since: date, until: date) -> pd.DataFrame:
    """Load and concatenate all daily CSVs for one kiln in a date range."""
    frames = []
    d = since
    while d <= until:
        path = LOG_DIR / name / f"{d.isoformat()}.csv"
        if path.exists():
            try:
                frames.append(pd.read_csv(path))
            except Exception as e:
                st.warning(f"Could not read {path}: {e}")
        d += timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"])
    df = df.sort_values("timestamp_utc").reset_index(drop=True)
    df["pv"] = pd.to_numeric(df["pv"], errors="coerce")
    df["sp"] = pd.to_numeric(df["sp"], errors="coerce")
    df["output_pct"] = pd.to_numeric(df["output_pct"], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------
def compute_ramp_rate(df: pd.DataFrame, smooth_window: int = 12) -> pd.DataFrame:
    """
    Compute degrees-per-minute as a smoothed derivative of PV.

    smooth_window = 12 samples ≈ 1 minute at 5s polling.
    """
    if df.empty or df["pv"].isna().all():
        return df
    df = df.copy()
    # Forward-fill brief dropouts, but only short ones
    df["pv_smooth"] = df["pv"].rolling(smooth_window, min_periods=2, center=True).mean()
    # dt in minutes for each sample-to-sample interval
    dt_min = df["timestamp_utc"].diff().dt.total_seconds() / 60.0
    dpv = df["pv_smooth"].diff()
    df["ramp_rate_per_min"] = dpv / dt_min
    return df


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Kiln analysis", page_icon="📊", layout="wide")
st.title("📊 Kiln performance analysis")

kilns = list_kilns()
if not kilns:
    st.warning(f"No log files found in `{LOG_DIR}`. Run the dashboard "
               "for a while first to accumulate data.")
    st.stop()

col1, col2, col3 = st.columns([1, 1, 1])
with col1:
    kiln = st.selectbox("Kiln", kilns)
with col2:
    days_back = st.number_input("Days of history", min_value=1, max_value=90, value=7)
with col3:
    smooth = st.slider("Smoothing (samples)", 2, 60, 12,
                       help="Higher = smoother ramp-rate curve, less noisy")

until = date.today()
since = until - timedelta(days=days_back - 1)
df = load_kiln(kiln, since, until)

if df.empty:
    st.info(f"No data for {kiln} between {since} and {until}.")
    st.stop()

st.caption(f"**{len(df):,} samples** from {df['timestamp_utc'].min()} "
           f"to {df['timestamp_utc'].max()}")

df = compute_ramp_rate(df, smooth_window=smooth)

# Tab 1: time series
tab1, tab2, tab3 = st.tabs(["Time series", "Ramp rate vs temperature",
                            "Firing summary"])

with tab1:
    st.subheader("PV and SP over time")
    st.line_chart(df.set_index("timestamp_utc")[["pv", "sp"]], height=300)
    st.subheader("Output power")
    st.line_chart(df.set_index("timestamp_utc")["output_pct"], height=180)
    st.subheader("Ramp rate (°/min)")
    st.line_chart(df.set_index("timestamp_utc")["ramp_rate_per_min"], height=180)

with tab2:
    st.subheader("How fast can the kiln move at each temperature?")
    st.caption("Each point is one observation. Heating periods only "
               "(positive ramp rate, output > 50%).")
    heating = df[(df["ramp_rate_per_min"] > 0)
                 & (df["output_pct"] > 50)].dropna(subset=["pv", "ramp_rate_per_min"])
    if heating.empty:
        st.info("Not enough heating data yet. Run a program at high output to populate.")
    else:
        # bin by 50° buckets and show mean / max ramp rate per bucket
        heating["pv_bin"] = (heating["pv"] // 50 * 50).astype(int)
        summary = heating.groupby("pv_bin")["ramp_rate_per_min"].agg(
            ["count", "mean", "max"]).reset_index()
        summary.columns = ["PV (°, lower bin)", "samples", "mean °/min", "max °/min"]

        st.dataframe(summary, use_container_width=True, hide_index=True)
        st.scatter_chart(heating, x="pv", y="ramp_rate_per_min",
                         size=12, height=400)

with tab3:
    st.subheader("Programs run in this window")
    runs = df[df["active_program"] > 0].copy()
    if runs.empty:
        st.info("No program runs recorded in this window.")
    else:
        # Identify distinct runs as contiguous groups by active_program
        runs["run_id"] = (runs["active_program"].diff().fillna(1) != 0).cumsum()
        firings = runs.groupby("run_id").agg(
            program=("active_program", "first"),
            started=("timestamp_utc", "min"),
            ended=("timestamp_utc", "max"),
            min_pv=("pv", "min"),
            max_pv=("pv", "max"),
            samples=("pv", "count"),
        ).reset_index(drop=True)
        firings["duration"] = firings["ended"] - firings["started"]
        st.dataframe(firings, use_container_width=True, hide_index=True)
