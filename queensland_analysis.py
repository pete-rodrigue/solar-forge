"""
qld_generation_april.py
=======================
Fetches hourly electricity generation data for Queensland (QLD1) from the
Open Electricity API (formerly OpenNEM) for April 2024, April 2025, and
April 2026, broken down by fuel technology (solar, gas, battery, coal, etc.).

Results are saved to:
  - qld_april_generation.csv        (long-format tidy data)
  - qld_april_tod.csv               (average time-of-day MW by hour and fuel type)
  - qld_april_tod_by_year.png       (stacked area charts, one panel per April year)

Requirements:
  pip install requests pandas seaborn matplotlib

API key:
  Register for free at https://platform.openelectricity.org.au
  Then set the environment variable:
    export OPENELECTRICITY_API_KEY="your_key_here"
  Or paste your key directly into API_KEY below (not recommended for shared code).

Notes on data limits (Community/free plan):
  - Hourly interval: max 32-day range per request (April = 30 days, fine)
  - Historical window: last 2 years from today
    April 2024 is ~26 months ago as of June 2026, so it may be just outside
    the community window. If you get a 400 error for April 2024, you'll need
    an Academic plan (free for researchers) — apply at platform.openelectricity.org.au
"""

import os
import sys
import time
import requests
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import seaborn as sns
from datetime import datetime
import api_key

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("OPENELECTRICITY_API_KEY", api_key.NEM_API_KEY)
BASE_URL = "https://api.openelectricity.org.au/v4"


# Queensland's network region code within the NEM
NETWORK_CODE = "NEM"
NETWORK_REGION = "QLD1"
 
# Fuel types we care about most for the solar-vs-gas story.
# Leave as None to fetch ALL fuel types (recommended for exploration).
# To filter, use codes from the fueltech table, e.g.:
#   ["solar_utility", "solar_rooftop", "gas_ccgt", "gas_ocgt",
#    "battery_discharging", "battery_charging", "coal_black", "wind"]
FUELTECH_FILTER = None
 
# April date ranges for each year (inclusive start, inclusive end)
APRIL_WINDOWS = [
    ("2024-08-01T00:00:00", "2024-08-30T23:59:59"),
    ("2025-04-01T00:00:00", "2025-04-30T23:59:59"),
    ("2026-04-01T00:00:00", "2026-04-30T23:59:59"),
]
 
OUTPUT_RAW  = "qld_april_generation.csv"
OUTPUT_TOD  = "qld_april_tod.csv"
OUTPUT_PLOT = "qld_april_tod_by_year.png"
 
# ---------------------------------------------------------------------------
# API helper
# ---------------------------------------------------------------------------
 
def fetch_generation(date_start: str, date_end: str) -> list[dict]:
    """
    Fetch hourly power generation (MW) for QLD1, grouped by fueltech,
    for a single date window.
 
    Returns a list of raw result dicts from the API 'data' array.
    Raises on HTTP errors.
    """
    if API_KEY == "YOUR_API_KEY_HERE":
        sys.exit(
            "ERROR: Please set your API key. Either:\n"
            "  export OPENELECTRICITY_API_KEY='your_key'\n"
            "or edit the API_KEY variable at the top of this script."
        )
 
    url = f"{BASE_URL}/data/network/{NETWORK_CODE}"
 
    params = {
        "metrics": "power",          # MW (instantaneous power, hourly avg)
        "interval": "1h",            # hourly buckets
        "date_start": date_start,
        "date_end": date_end,
        "network_region": NETWORK_REGION,
        "primary_grouping": "network_region",
        "secondary_grouping": "fueltech",
    }
 
    # Optionally filter to specific fuel types
    if FUELTECH_FILTER:
        params["fueltech"] = FUELTECH_FILTER
 
    headers = {"Authorization": f"Bearer {API_KEY}"}
 
    print(f"  Requesting {date_start[:7]} ... ", end="", flush=True)
    resp = requests.get(url, params=params, headers=headers, timeout=60)
 
    if resp.status_code == 400:
        # Common cause: date range outside community plan's 2-year window
        print("FAILED")
        print(f"  400 error: {resp.text}")
        print(
            "  Tip: April 2024 may be outside the Community plan's 2-year window.\n"
            "  Apply for a free Academic plan at https://platform.openelectricity.org.au"
        )
        return []
 
    resp.raise_for_status()
    body = resp.json()
 
    if not body.get("success"):
        print("FAILED")
        print(f"  API error: {body.get('error')}")
        return []
 
    records = body.get("data", [])
    # Show every fueltech series name so we know exactly what came back
    if records:
        names = [s.get("name", "?") for s in records[0].get("results", [])]
        print(f"OK ({len(records)} series returned, fueltechs: {names})")
    else:
        print(f"OK (0 records)")
    return records
 
 
# ---------------------------------------------------------------------------
# Parse API response into a tidy DataFrame
# ---------------------------------------------------------------------------
 
def debug_response_shape(records: list[dict]) -> None:
    """
    Print the top-level keys and nested structure of the first record so we
    can see exactly what the API actually returned. Handy when the docs and
    reality diverge.
    """
    import json
    print("\n  [DEBUG] Response shape — first record:")
    if not records:
        print("    (empty list)")
        return
    r = records[0]
    print(f"    Top-level keys: {list(r.keys())}")
    # Print a truncated version so we don't flood the terminal
    preview = json.dumps(r, indent=2, default=str)
    lines = preview.splitlines()
    for line in lines[:60]:
        print(f"    {line}")
    if len(lines) > 60:
        print(f"    ... ({len(lines) - 60} more lines)")
    print()
 
 
def parse_records(records: list[dict], year: int, debug: bool = False) -> pd.DataFrame:
    """
    Parses the actual v4 API response shape (confirmed from live output):
 
      The outer list has ONE wrapper object with these keys:
        network_code, metric, unit, interval, date_start, date_end,
        groupings, results, network_timezone_offset
 
      'results' is a list of per-fueltech-group series, each shaped as:
        {
          "name": "power_QLD1|battery",
          "date_start": "2025-04-01T00:00:00+10:00",
          "date_end":   "2025-04-30T23:00:00+10:00",
          "columns": {"region": "QLD1", "fueltech": "battery"},
          "data": [
            ["2025-04-01T00:00:00+10:00", -36.22],
            ["2025-04-01T01:00:00+10:00", -42.16],
            ...
          ]
        }
 
    Each data row is [iso_timestamp_string, float_mw].
    Positive = generation/discharge, negative = charging/consumption.
    """
    if debug:
        debug_response_shape(records)
 
    rows = []
 
    for wrapper in records:
        results = wrapper.get("results", [])
        if not results:
            print("  [WARN] wrapper has no 'results' key")
            continue
 
        for series in results:
            # fueltech label lives in columns dict
            columns_meta = series.get("columns", {})
            fueltech = columns_meta.get("fueltech") or series.get("name", "unknown")
 
            data_pairs = series.get("data", [])
            if not data_pairs:
                continue
 
            for pair in data_pairs:
                if len(pair) != 2:
                    continue
                ts_str, mw_val = pair
                rows.append({
                    "interval_start": ts_str,
                    "fueltech": fueltech,
                    "fueltech_label": fueltech,   # can be enriched below
                    "power_mw": mw_val,
                    "year": year,
                })
 
    if not rows:
        print("  [WARN] parse_records: no rows extracted.")
        return pd.DataFrame()
 
    df = pd.DataFrame(rows)
    df["interval_start"] = pd.to_datetime(df["interval_start"], utc=True).dt.tz_convert(
        "Australia/Brisbane"
    ).dt.tz_localize(None)   # strip tz for easier groupby/plotting
    df["power_mw"] = pd.to_numeric(df["power_mw"], errors="coerce")
    df = df.dropna(subset=["power_mw"])
 
    # Map fueltech group codes to friendlier labels
    label_map = {
        "battery":    "Battery",
        "gas":        "Gas",
        "coal":       "Coal",
        "solar":      "Solar",
        "wind":       "Wind",
        "hydro":      "Hydro",
        "distillate": "Distillate",
        "bioenergy":  "Bioenergy",
    }
    df["fueltech_label"] = df["fueltech"].map(label_map).fillna(df["fueltech"])
 
    print(f"    Parsed {len(df):,} rows, fueltechs: {sorted(df['fueltech'].unique())}")
    return df
 
 
# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------
 
# The fueltech GROUP codes actually returned by the API (confirmed from debug output).
# The API uses group-level codes: "battery", "gas", "solar", etc.
PLOT_FUELTECHS = {
    "gas_ocgt":     ("Gas",               "#F48024"),  # orange
    "battery": ("Battery (net)",     "#BF5FFF"),  # purple
    # Note: battery values can be negative (charging) or positive (discharging).
    # We plot the raw net value so you can see both behaviours.
}
 
 
def plot_tod(tod: pd.DataFrame, years: list[int]) -> None:
    """
    Produce a 1-row × N-column figure with one line-chart panel per year,
    showing average MW by hour of day for Gas (OCGT) and Battery (Discharge).
 
    Panels share a y-axis so year-on-year magnitude changes are immediately
    visible. A dashed vertical guide marks 13:00 (typical solar peak).
 
    Saves to OUTPUT_PLOT.
    """
    sns.set_theme(style="whitegrid", font_scale=1.05)
 
    n_years = len(years)
    fig, axes = plt.subplots(
        1, n_years,
        figsize=(5 * n_years, 5),
        sharey=True,
        constrained_layout=True,
    )
    if n_years == 1:
        axes = [axes]
 
    hours = list(range(24))
 
    # Pre-build pivot tables and find the global y range
    panels = {}
    global_ymax = 0
    global_ymin = 0
 
    for year in years:
        year_df = tod[tod["year"] == year]
        pivot = (
            year_df
            .pivot_table(index="hour", columns="fueltech", values="avg_power_mw", aggfunc="mean")
            .reindex(index=hours)
            .fillna(0)
        )
        panels[year] = pivot
        for ft in PLOT_FUELTECHS:
            if ft in pivot.columns:
                global_ymax = max(global_ymax, pivot[ft].max())
                global_ymin = min(global_ymin, pivot[ft].min())
 
    for ax, year in zip(axes, years):
        pivot = panels[year]
 
        for ft, (label, color) in PLOT_FUELTECHS.items():
            if ft not in pivot.columns:
                # Fuel type absent for this year — draw a flat zero with a note
                ax.plot(hours, [0] * 24, color=color, linewidth=1.5,
                        linestyle=":", label=f"{label} (no data)", alpha=0.5)
                continue
 
            values = pivot[ft].values
 
            # Filled line for visual weight
            ax.fill_between(hours, values, alpha=0.18, color=color)
            ax.plot(hours, values, color=color, linewidth=2.2, label=label)
 
        # Vertical guide at solar peak
        # ax.axvline(13, color="#CCAA00", linewidth=1.0, linestyle="--",
        #            alpha=0.7, label="Solar peak (~13:00)")
        if year > 2024:
            ax.set_title(f"April {year}", fontsize=13, fontweight="bold", pad=8)
        else:
            ax.set_title(f"July {year}", fontsize=13, fontweight="bold", pad=8)
        ax.set_xlabel("Hour of day", fontsize=10)
        ax.set_xlim(0, 23)
        ax.set_xticks([0, 6, 12, 18, 23])
        ax.set_xticklabels(["00:00", "06:00", "12:00", "18:00", "23:00"], fontsize=8)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
        ax.axhline(0, color="black", linewidth=0.5)
 
    # Y-axis label on leftmost panel only
    axes[0].set_ylabel("Average power (MW)", fontsize=10)
 
    # Consistent y range across all panels
    padding = (global_ymax - global_ymin) * 0.08
    for ax in axes:
        ax.set_ylim(global_ymin - padding, global_ymax + padding)
 
    # Single shared legend — pull from first panel
    handles, legend_labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles, legend_labels,
        loc="lower center",
        ncol=len(handles),
        bbox_to_anchor=(0.5, -0.10),
        frameon=True,
        fontsize=9,
    )
 
    # fig.suptitle(
    #     "In Queensland AUS, gas peaker plants rapidly displaced by renewables + batteries",
    #     fontsize=13,
    #     fontweight="bold",
    #     y=1.02,
    # )
 
    fig.savefig(OUTPUT_PLOT, dpi=150, bbox_inches="tight")
    print(f"Plot saved to: {OUTPUT_PLOT}")
    plt.close(fig)
 
 
# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
 
def main():
    all_dfs = []
 
    for i, (date_start, date_end) in enumerate(APRIL_WINDOWS):
        year = int(date_start[:4])
        records = fetch_generation(date_start, date_end)
 
        if records:
            # Print response shape on first successful call to aid debugging
            df = parse_records(records, year, debug=(i == 0))
            if not df.empty:
                all_dfs.append(df)
            else:
                print(f"  [WARN] {year}: parse produced empty DataFrame despite {len(records)} records")
 
        # Be polite to the API
        time.sleep(1)
 
    if not all_dfs:
        print("\nNo data retrieved. Check your API key and plan limits.")
        sys.exit(1)
 
    # Combine all years
    combined = pd.concat(all_dfs, ignore_index=True)
    combined["hour"] = combined["interval_start"].dt.hour
    combined["month"] = combined["interval_start"].dt.month
    combined["day"] = combined["interval_start"].dt.day
 
    # Save raw long-format data
    combined.to_csv(OUTPUT_RAW, index=False)
    print(f"\nRaw data saved to: {OUTPUT_RAW}")
    print(f"  Shape: {combined.shape}")
    print(f"  Fuel types found: {sorted(combined['fueltech'].unique())}")
    print(f"  Years: {sorted(combined['year'].unique())}")
 
    # Build time-of-day summary: average MW by hour, fueltech, and year
    tod = (
        combined
        .groupby(["year", "hour", "fueltech", "fueltech_label"])["power_mw"]
        .mean()
        .reset_index()
        .rename(columns={"power_mw": "avg_power_mw"})
        .sort_values(["fueltech", "year", "hour"])
    )
 
    tod.to_csv(OUTPUT_TOD, index=False)
    print(f"Time-of-day summary saved to: {OUTPUT_TOD}")
    print(f"  Shape: {tod.shape}")
 
    # Quick console summary for gas and battery
    print("\n--- April average MW by hour (gas + battery) ---")
    summary_fuels = ["gas", "battery"]
    summary = tod[tod["fueltech"].isin(summary_fuels)].copy()
    if summary.empty:
        print("  (None of the expected fuel types found — check fueltech codes in raw CSV)")
    else:
        pivot = summary.pivot_table(
            index=["fueltech", "hour"], columns="year", values="avg_power_mw"
        )
        print(pivot.to_string())
 
    # Generate the side-by-side plot
    years_present = sorted(combined["year"].unique())
    print(f"\nGenerating plot for years: {years_present}")
    plot_tod(tod, years_present)
 
 
if __name__ == "__main__":
    main()
 