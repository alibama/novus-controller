"""
Analytics page — reads the CSV logs the monitor writes. No BLE access, so
it's safe to use anytime, even mid-firing.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

import app_core as core

st.set_page_config(page_title="Kiln analytics", page_icon="📈", layout="wide")

if not core.require_password():
    st.stop()

LOG_DIR = core.LOG_DIR


def list_logged() -> list[str]:
    if not LOG_DIR.exists():
        return []
    return sorted(d.name for d in LOG_DIR.iterdir() if d.is_dir())


def load_series(name: str, since: date, until: date) -> pd.DataFrame:
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
    for c in ("pv", "sp", "output_pct"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def compute_ramp_rate(df: pd.DataFrame, smooth_window: int = 12) -> pd.DataFrame:
    if df.empty or df["pv"].isna().all():
        return df
    df = df.copy()
    df["pv_smooth"] = df["pv"].rolling(smooth_window, min_periods=2, center=True).mean()
    dt_min = df["timestamp_utc"].diff().dt.total_seconds() / 60.0
    df["ramp_rate_per_min"] = df["pv_smooth"].diff() / dt_min
    return df


st.title("📈 Kiln analytics")
st.caption("Reads logged history. Does not touch Bluetooth.")

logged = list_logged()
if not logged:
    st.warning(f"No logs yet in `{LOG_DIR}`. Let the monitor run a while first.")
    st.stop()

c1, c2, c3 = st.columns(3)
with c1:
    which = st.selectbox("Controller", logged)
with c2:
    days_back = st.number_input("Days of history", min_value=1, max_value=90, value=7)
with c3:
    smooth = st.slider("Smoothing (samples)", 2, 60, 12,
                       help="Higher = smoother ramp-rate curve")

until = date.today()
since = until - timedelta(days=days_back - 1)
df = load_series(which, since, until)
if df.empty:
    st.info(f"No data for {which} between {since} and {until}.")
    st.stop()

st.caption(f"**{len(df):,} samples** · {df['timestamp_utc'].min()} → {df['timestamp_utc'].max()}")
df = compute_ramp_rate(df, smooth_window=smooth)

tab1, tab2, tab3 = st.tabs(["Time series", "Ramp rate vs temp", "Firing summary"])

with tab1:
    st.subheader("PV and SP over time")
    st.line_chart(df.set_index("timestamp_utc")[["pv", "sp"]], height=300)
    st.subheader("Output power")
    st.line_chart(df.set_index("timestamp_utc")["output_pct"], height=180)
    st.subheader("Ramp rate (°/min)")
    st.line_chart(df.set_index("timestamp_utc")["ramp_rate_per_min"], height=180)

with tab2:
    st.subheader("How fast can it move at each temperature?")
    st.caption("Heating periods only (positive ramp, output > 50%).")
    heating = df[(df["ramp_rate_per_min"] > 0) & (df["output_pct"] > 50)].dropna(
        subset=["pv", "ramp_rate_per_min"])
    if heating.empty:
        st.info("Not enough heating data yet.")
    else:
        heating = heating.copy()
        heating["pv_bin"] = (heating["pv"] // 50 * 50).astype(int)
        summary = heating.groupby("pv_bin")["ramp_rate_per_min"].agg(
            ["count", "mean", "max"]).reset_index()
        summary.columns = ["PV (°, lower bin)", "samples", "mean °/min", "max °/min"]
        st.dataframe(summary, use_container_width=True, hide_index=True)
        st.scatter_chart(heating, x="pv", y="ramp_rate_per_min", size=12, height=400)

with tab3:
    st.subheader("Programs run in this window")
    runs = df[df["active_program"] > 0].copy()
    if runs.empty:
        st.info("No program runs recorded.")
    else:
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
