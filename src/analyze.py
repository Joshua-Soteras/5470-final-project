"""
Data Pipeline and Visualization
--------------------------------
Loads results from both measurement CSVs, aggregates across repeated runs,
and generates five plots comparing TCP and UDP performance across conditions.

Run after all four data collection scripts have completed:
    uv run python src/analyze.py

Output: five PNG files saved to plots/
"""

import os
import sys
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# ─── Paths ───────────────��───────────────────────────────────���────────────────

# os.path.dirname(__file__) = the directory this script lives in (src/).
# ".." steps up to the project root. All paths are relative to the project root.
ROOT         = os.path.join(os.path.dirname(__file__), "..")
TCP_CSV      = os.path.join(ROOT, "results", "tcp_results.csv")
UDP_CSV      = os.path.join(ROOT, "results", "udp_results.csv")
PLOTS_DIR    = os.path.join(ROOT, "plots")

# ─── Plot styling ──────────────────────────────────────────────��──────────────

# Consistent colors used across all plots so TCP is always blue and UDP is
# always orange — makes cross-plot comparison easier for the reader.
TCP_COLOR = "#1f77b4"   # matplotlib default blue
UDP_COLOR = "#ff7f0e"   # matplotlib default orange

# One color per network condition — used in multi-line plots.
# Using a colorblind-friendly qualitative palette.
CONDITION_COLORS = {
    "baseline":     "#2ca02c",   # green
    "congested":    "#d62728",   # red
    "high_latency": "#9467bd",   # purple
    "bufferbloat":  "#8c564b",   # brown
    "lossy":        "#e377c2",   # pink
}

# Display-friendly names for condition labels (used in legend entries).
CONDITION_LABELS = {
    "baseline":     "Baseline",
    "congested":    "Congested (flood)",
    "high_latency": "High Latency (50ms)",
    "bufferbloat":  "Bufferbloat (1Mbit queue)",
    "lossy":        "Lossy (5% loss)",
}

# Payload sizes used in the payload sweep — used to set x-axis tick positions.
PAYLOAD_SIZES = [64, 256, 1024, 4096, 16384, 65536]

# Buffer sizes used in the buffer sweep (scripts/05_buffer_sweep.sh).
BUFFER_SIZES = [4096, 16384, 65536, 131072, 262144]

# ─── Data loading ────────────────────���────────────────────────────────────────

def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Reads both CSVs into DataFrames and validates that required columns exist.

    Returns
    -------
    tuple[pd.DataFrame, pd.DataFrame]
        (tcp_df, udp_df) — raw data, one row per experiment run.

    Raises
    ------
    SystemExit
        If a CSV file is missing or does not contain the required columns.
        We exit rather than raise so the error message is clean for the user.
    """
    # Define the exact columns each CSV must have. If the CSV was written with
    # an old schema (e.g. missing avg_owd_ms), we catch it here before any
    # plotting code tries to access a non-existent column.
    tcp_required = {
        "protocol", "payload_bytes", "buffer_bytes", "condition",
        "throughput_mbps", "avg_latency_ms", "latency_stdev_ms", "run_index",
    }
    udp_required = {
        "protocol", "payload_bytes", "buffer_bytes", "condition",
        "throughput_mbps", "loss_rate_pct", "jitter_ms", "avg_owd_ms", "run_index",
    }

    for path, label in [(TCP_CSV, "TCP"), (UDP_CSV, "UDP")]:
        if not os.path.exists(path):
            print(f"ERROR: {path} not found.")
            print("Run the data collection scripts first:")
            print("  bash scripts/01_smoke_test.sh")
            print("  bash scripts/02_baseline_sweep.sh")
            print("  bash scripts/03_congested_sweep.sh")
            print("  bash scripts/04_emulated_conditions.sh")
            sys.exit(1)

    tcp_df = pd.read_csv(TCP_CSV)
    udp_df = pd.read_csv(UDP_CSV)

    # Check for missing columns in each DataFrame.
    for df, required, label in [(tcp_df, tcp_required, "TCP"), (udp_df, udp_required, "UDP")]:
        missing = required - set(df.columns)
        if missing:
            print(f"ERROR: {label} CSV is missing columns: {missing}")
            print("The CSV may have been written with an old schema.")
            print("Reset it by re-running the data collection scripts.")
            sys.exit(1)

    # Warn if either CSV has fewer than 10 data rows — likely only smoke test
    # data, which won't produce meaningful plots.
    for df, label in [(tcp_df, "TCP"), (udp_df, "UDP")]:
        if len(df) < 10:
            print(f"WARNING: {label} CSV only has {len(df)} rows.")
            print("Plots may be sparse. Run the full baseline sweep first.")

    return tcp_df, udp_df


# ─── Aggregation ──────────────────────────────────────────────────────────────

def aggregate(df: pd.DataFrame, group_cols: list[str],
              metric_cols: list[str]) -> pd.DataFrame:
    """
    Groups rows by group_cols and computes mean and standard deviation for
    each metric column across repeated runs.

    For example, grouping by ["payload_bytes", "condition"] and aggregating
    "throughput_mbps" produces one row per (payload, condition) pair, with
    columns "throughput_mbps_mean" and "throughput_mbps_std" computed across
    the three run_index repetitions.

    Parameters
    ----------
    df : pd.DataFrame
        Raw data — one row per experiment run.
    group_cols : list[str]
        Columns to group by, e.g. ["payload_bytes", "condition"].
    metric_cols : list[str]
        Columns to aggregate, e.g. ["throughput_mbps", "avg_latency_ms"].

    Returns
    -------
    pd.DataFrame
        One row per unique combination of group_cols. For each metric M,
        adds columns M_mean and M_std. The index is reset so group_cols
        are regular columns, not index levels.
    """
    # Select only the columns we need before groupby to keep the result clean.
    cols = group_cols + metric_cols
    grouped = df[cols].groupby(group_cols)

    # .agg(["mean", "std"]) returns a MultiIndex column like
    # (throughput_mbps, mean) and (throughput_mbps, std).
    # We flatten it to throughput_mbps_mean and throughput_mbps_std.
    agg = grouped.agg(["mean", "std"])
    agg.columns = [f"{col}_{stat}" for col, stat in agg.columns]

    # reset_index() converts the group_cols from the index back into regular
    # columns so the DataFrame is easy to filter and plot.
    return agg.reset_index()


# ─── Plot helpers ───────────────��────────────────────────���────────────────────

def _save(fig: plt.Figure, filename: str) -> None:
    """Saves a figure to plots/ at 150 DPI and closes it to free memory."""
    os.makedirs(PLOTS_DIR, exist_ok=True)
    path = os.path.join(PLOTS_DIR, filename)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path}")


def _payload_x_axis(ax: plt.Axes) -> None:
    """
    Sets x-axis ticks to the exact payload sizes used in the sweep and
    formats them as human-readable strings (64B, 1KB, 16KB, 64KB).

    Using explicit ticks avoids matplotlib placing ticks at odd values like
    30000 or 50000 between our actual measurement points.
    """
    labels = []
    for p in PAYLOAD_SIZES:
        if p >= 1024:
            # Integer division — 1024 → "1KB", 65536 → "64KB"
            labels.append(f"{p // 1024}KB")
        else:
            labels.append(f"{p}B")

    ax.set_xticks(PAYLOAD_SIZES)
    ax.set_xticklabels(labels, rotation=30, ha="right")

    # Log scale on x-axis because payload sizes span three orders of magnitude
    # (64 → 65536). Linear scale would compress the small-payload region where
    # the most interesting overhead effects occur.
    ax.set_xscale("log")
    ax.xaxis.set_major_formatter(ticker.NullFormatter())
    ax.xaxis.set_minor_formatter(ticker.NullFormatter())
    ax.set_xticks(PAYLOAD_SIZES)
    ax.set_xticklabels(labels, rotation=30, ha="right")


# ─── Plot 1 — Throughput vs Payload Size (TCP vs UDP, baseline) ───────────────

def plot_1_throughput_vs_payload(tcp_agg: pd.DataFrame,
                                  udp_agg: pd.DataFrame) -> None:
    """
    Line chart: throughput (Mbps) on Y, payload size on X.
    Two lines: TCP (blue) and UDP (orange), baseline condition only.

    What to look for:
    - At small payloads (64–256B): UDP often outperforms TCP because TCP has
      more per-message overhead (connection state, Nagle, ACK waiting).
    - At large payloads (>16KB): TCP catches up as its bulk-transfer
      optimizations (congestion window, send buffer) kick in.
    - At 65536B: a drop in throughput is expected — this payload exceeds the
      loopback MTU and requires IP fragmentation.
    """
    # Filter to baseline condition only for a clean control comparison.
    tcp_b = tcp_agg[tcp_agg["condition"] == "baseline"].copy()
    udp_b = udp_agg[udp_agg["condition"] == "baseline"].copy()

    if tcp_b.empty and udp_b.empty:
        print("  Skipping Plot 1 — no baseline data found.")
        return

    fig, ax = plt.subplots(figsize=(8, 5))

    # Plot TCP line with error band.
    # errorbar() draws the line and the ± error bars at each data point.
    # yerr= takes the standard deviation column; capsize= adds horizontal caps.
    if not tcp_b.empty:
        ax.errorbar(
            tcp_b["payload_bytes"],
            tcp_b["throughput_mbps_mean"],
            yerr=tcp_b["throughput_mbps_std"].fillna(0),
            label="TCP",
            color=TCP_COLOR,
            marker="o",
            capsize=4,
            linewidth=2,
        )

    if not udp_b.empty:
        ax.errorbar(
            udp_b["payload_bytes"],
            udp_b["throughput_mbps_mean"],
            yerr=udp_b["throughput_mbps_std"].fillna(0),
            label="UDP",
            color=UDP_COLOR,
            marker="s",
            capsize=4,
            linewidth=2,
        )

    # Draw a vertical dashed line at the loopback MTU boundary (16384 bytes).
    # Payloads above this require IP fragmentation — it helps the reader see
    # whether there is a throughput inflection at that point.
    ax.axvline(x=16384, color="gray", linestyle="--", linewidth=1,
               label="Loopback MTU (16KB)")

    ax.set_title("Throughput vs Payload Size — Baseline Condition")
    ax.set_xlabel("Payload Size")
    ax.set_ylabel("Throughput (Mbps)")
    ax.legend()
    ax.grid(True, which="both", linestyle="--", alpha=0.4)
    _payload_x_axis(ax)

    fig.tight_layout()
    _save(fig, "01_throughput_vs_payload.png")


# ─── Plot 2 — TCP Latency vs Payload Size (all conditions) ���──────────────────

def plot_2_tcp_latency_vs_payload(tcp_agg: pd.DataFrame) -> None:
    """
    Line chart: TCP avg RTT (ms) on Y, payload size on X.
    One line per network condition.

    What to look for:
    - baseline: very low, near-flat — loopback RTT is dominated by OS
      scheduling overhead, not payload size.
    - high_latency: RTT ≈ 2 × 50ms = ~100ms — RTT doubles the one-way delay.
    - bufferbloat: RTT rises steeply with payload size as larger payloads fill
      the 1000-slot queue faster. This is the bufferbloat signature: high
      latency without high loss.
    - congested: moderate RTT increase from AIMD backoff.
    """
    conditions = [c for c in CONDITION_LABELS if c in tcp_agg["condition"].unique()]

    if not conditions:
        print("  Skipping Plot 2 — no TCP data found.")
        return

    fig, ax = plt.subplots(figsize=(8, 5))

    for condition in conditions:
        subset = tcp_agg[tcp_agg["condition"] == condition]
        if subset.empty:
            continue

        ax.errorbar(
            subset["payload_bytes"],
            subset["avg_latency_ms_mean"],
            yerr=subset["avg_latency_ms_std"].fillna(0),
            label=CONDITION_LABELS.get(condition, condition),
            color=CONDITION_COLORS.get(condition, "gray"),
            marker="o",
            capsize=4,
            linewidth=2,
        )

    ax.set_title("TCP Latency (RTT) vs Payload Size — All Conditions")
    ax.set_xlabel("Payload Size")
    ax.set_ylabel("Avg Round-Trip Time (ms)")
    ax.legend()
    ax.grid(True, which="both", linestyle="--", alpha=0.4)
    _payload_x_axis(ax)

    fig.tight_layout()
    _save(fig, "02_tcp_latency_vs_payload.png")


# ─── Plot 3 — UDP Packet Loss vs Payload Size ─────────��──────────────────────

def plot_3_udp_loss_vs_payload(udp_agg: pd.DataFrame) -> None:
    """
    Line chart: UDP loss rate (%) on Y, payload size on X.
    Shows lossy and congested conditions only — baseline loss should be ~0%
    and including it would flatten the scale and obscure the signal.

    What to look for:
    - lossy: loss should be near 5% across all payload sizes (dummynet applies
      loss per-packet, independent of size).
    - congested: loss rises with payload size — larger datagrams take longer
      to send, giving the queue more time to fill and overflow.
    """
    # Only show conditions where loss is non-trivial.
    target_conditions = ["lossy", "congested"]
    available = [c for c in target_conditions if c in udp_agg["condition"].unique()]

    if not available:
        print("  Skipping Plot 3 — no lossy/congested UDP data found.")
        return

    fig, ax = plt.subplots(figsize=(8, 5))

    for condition in available:
        subset = udp_agg[udp_agg["condition"] == condition]
        ax.errorbar(
            subset["payload_bytes"],
            subset["loss_rate_pct_mean"],
            yerr=subset["loss_rate_pct_std"].fillna(0),
            label=CONDITION_LABELS.get(condition, condition),
            color=CONDITION_COLORS.get(condition, "gray"),
            marker="o",
            capsize=4,
            linewidth=2,
        )

    ax.set_title("UDP Packet Loss vs Payload Size")
    ax.set_xlabel("Payload Size")
    ax.set_ylabel("Loss Rate (%)")

    # y-axis starts at 0 so the 0% baseline is always visible — prevents the
    # chart from implying large loss when values are small.
    ax.set_ylim(bottom=0)
    ax.legend()
    ax.grid(True, which="both", linestyle="--", alpha=0.4)
    _payload_x_axis(ax)

    fig.tight_layout()
    _save(fig, "03_udp_loss_vs_payload.png")


# ─── Plot 4 — UDP Jitter vs Payload Size (all conditions) ────────────────────

def plot_4_udp_jitter_vs_payload(udp_agg: pd.DataFrame) -> None:
    """
    Line chart: UDP jitter (ms) on Y, payload size on X.
    One line per condition.

    Jitter measures how irregular the inter-arrival gaps are. A perfectly
    paced stream on an idle loopback has near-zero jitter. Conditions that
    introduce queuing cause gaps to vary, producing higher jitter.

    What to look for:
    - bufferbloat: highest jitter — the large queue causes some packets to
      wait many ms while others sail through, producing wildly inconsistent
      inter-arrival gaps.
    - high_latency: jitter should stay low — the delay is constant (dummynet
      adds the same 50ms to every packet), so gaps are still uniform.
    - lossy: moderate jitter — random drops remove some arrivals, making
      the remaining inter-arrival gaps appear larger.
    """
    conditions = [c for c in CONDITION_LABELS if c in udp_agg["condition"].unique()]

    if not conditions:
        print("  Skipping Plot 4 — no UDP data found.")
        return

    fig, ax = plt.subplots(figsize=(8, 5))

    for condition in conditions:
        subset = udp_agg[udp_agg["condition"] == condition]
        if subset.empty:
            continue

        ax.errorbar(
            subset["payload_bytes"],
            subset["jitter_ms_mean"],
            yerr=subset["jitter_ms_std"].fillna(0),
            label=CONDITION_LABELS.get(condition, condition),
            color=CONDITION_COLORS.get(condition, "gray"),
            marker="o",
            capsize=4,
            linewidth=2,
        )

    ax.set_title("UDP Jitter vs Payload Size — All Conditions")
    ax.set_xlabel("Payload Size")
    ax.set_ylabel("Jitter (ms)")
    ax.set_ylim(bottom=0)
    ax.legend()
    ax.grid(True, which="both", linestyle="--", alpha=0.4)
    _payload_x_axis(ax)

    fig.tight_layout()
    _save(fig, "04_udp_jitter_vs_payload.png")


# ─── Plot 5 — TCP vs UDP Throughput Under Congestion ────────────────────────

def plot_5_congestion_comparison(tcp_agg: pd.DataFrame,
                                  udp_agg: pd.DataFrame) -> None:
    """
    Grouped bar chart: throughput (Mbps) on Y, payload size bucket on X.
    For each payload size, two bars: TCP (blue) and UDP (orange).
    Congested condition only.

    This is the core finding of the project: TCP's AIMD congestion control
    causes it to back off when it detects loss, reducing throughput. UDP has
    no congestion control and maintains its rate, but at the cost of packet
    loss (visible in Plot 3).

    What to look for:
    - UDP bars should be consistently taller than TCP bars — UDP does not back
      off while TCP does.
    - The gap between TCP and UDP should widen at larger payloads where the
      queue fills more aggressively.
    """
    tcp_c = tcp_agg[tcp_agg["condition"] == "congested"].copy()
    udp_c = udp_agg[udp_agg["condition"] == "congested"].copy()

    if tcp_c.empty and udp_c.empty:
        print("  Skipping Plot 5 — no congested data found.")
        return

    # Use only payload sizes that appear in both DataFrames so the bars are
    # always paired. If a payload appears in one but not the other, we skip it.
    tcp_payloads = set(tcp_c["payload_bytes"].unique())
    udp_payloads = set(udp_c["payload_bytes"].unique())
    common = sorted(tcp_payloads & udp_payloads)

    if not common:
        print("  Skipping Plot 5 — no common payload sizes between TCP and UDP congested data.")
        return

    tcp_c = tcp_c[tcp_c["payload_bytes"].isin(common)].sort_values("payload_bytes")
    udp_c = udp_c[udp_c["payload_bytes"].isin(common)].sort_values("payload_bytes")

    # Bar chart setup: two groups of bars side-by-side.
    # x = integer positions for each payload bucket.
    # bar_width = how wide each bar is; offset shifts one group left and one right.
    x = np.arange(len(common))
    bar_width = 0.35
    offset = bar_width / 2

    fig, ax = plt.subplots(figsize=(10, 5))

    # TCP bars shifted left of center; UDP bars shifted right.
    ax.bar(x - offset, tcp_c["throughput_mbps_mean"], bar_width,
           yerr=tcp_c["throughput_mbps_std"].fillna(0),
           label="TCP", color=TCP_COLOR, capsize=4)

    ax.bar(x + offset, udp_c["throughput_mbps_mean"], bar_width,
           yerr=udp_c["throughput_mbps_std"].fillna(0),
           label="UDP", color=UDP_COLOR, capsize=4)

    # X-axis tick labels: convert raw bytes to human-readable strings.
    x_labels = [f"{p // 1024}KB" if p >= 1024 else f"{p}B" for p in common]
    ax.set_xticks(x)
    ax.set_xticklabels(x_labels)

    ax.set_title("TCP vs UDP Throughput — Congested Condition")
    ax.set_xlabel("Payload Size")
    ax.set_ylabel("Throughput (Mbps)")
    ax.set_ylim(bottom=0)
    ax.legend()
    ax.grid(True, axis="y", linestyle="--", alpha=0.4)

    fig.tight_layout()
    _save(fig, "05_congestion_comparison.png")


# ─── Plot 6 — TCP vs UDP Throughput vs Buffer Size (baseline) ────────────────

def _buffer_x_axis(ax: plt.Axes) -> None:
    """
    Sets x-axis ticks to the exact buffer sizes used in the sweep and
    formats them as human-readable strings (4KB, 16KB, 64KB, 128KB, 256KB).
    Uses log scale because buffer sizes span two orders of magnitude.
    """
    labels = [f"{b // 1024}KB" for b in BUFFER_SIZES]
    ax.set_xscale("log")
    ax.set_xticks(BUFFER_SIZES)
    ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.xaxis.set_major_formatter(ticker.NullFormatter())
    ax.xaxis.set_minor_formatter(ticker.NullFormatter())
    ax.set_xticks(BUFFER_SIZES)
    ax.set_xticklabels(labels, rotation=30, ha="right")


def plot_6_throughput_vs_buffer(tcp_agg: pd.DataFrame,
                                 udp_agg: pd.DataFrame) -> None:
    """
    Line chart: throughput (Mbps) on Y, buffer size on X.
    Two lines: TCP (blue) and UDP (orange), baseline condition only.

    This directly answers the buffer-settings half of the research question.

    What to look for:
    - At very small buffers (4KB): throughput drops because the OS can only
      queue a few packets at a time before blocking the sender. SO_SNDBUF
      creates back-pressure; SO_RCVBUF causes the receiver to signal a small
      TCP window, slowing the sender.
    - At large buffers (128KB–256KB): throughput plateaus — once the buffer
      is large enough to keep the pipeline full, adding more doesn't help.
    - UDP may be less sensitive than TCP because UDP has no flow control;
      SO_SNDBUF affects UDP only when the send rate exceeds what the kernel
      can drain.
    """
    tcp_b = tcp_agg[tcp_agg["condition"] == "baseline"].copy()
    udp_b = udp_agg[udp_agg["condition"] == "baseline"].copy()

    if tcp_b.empty and udp_b.empty:
        print("  Skipping Plot 6 — no baseline buffer-sweep data found.")
        print("  Run scripts/05_buffer_sweep.sh first.")
        return

    fig, ax = plt.subplots(figsize=(8, 5))

    if not tcp_b.empty:
        ax.errorbar(
            tcp_b["buffer_bytes"],
            tcp_b["throughput_mbps_mean"],
            yerr=tcp_b["throughput_mbps_std"].fillna(0),
            label="TCP",
            color=TCP_COLOR,
            marker="o",
            capsize=4,
            linewidth=2,
        )

    if not udp_b.empty:
        ax.errorbar(
            udp_b["buffer_bytes"],
            udp_b["throughput_mbps_mean"],
            yerr=udp_b["throughput_mbps_std"].fillna(0),
            label="UDP",
            color=UDP_COLOR,
            marker="s",
            capsize=4,
            linewidth=2,
        )

    ax.set_title("Throughput vs Socket Buffer Size — Baseline (payload=1024B)")
    ax.set_xlabel("Socket Buffer Size (SO_SNDBUF / SO_RCVBUF)")
    ax.set_ylabel("Throughput (Mbps)")
    ax.legend()
    ax.grid(True, which="both", linestyle="--", alpha=0.4)
    _buffer_x_axis(ax)

    fig.tight_layout()
    _save(fig, "06_throughput_vs_buffer.png")


# ─── Plot 7 — UDP OWD vs Buffer Size (baseline + bufferbloat) ────────────────

def plot_7_owd_vs_buffer(udp_agg: pd.DataFrame) -> None:
    """
    Line chart: UDP average one-way delay (ms) on Y, buffer size on X.
    Two lines: baseline and bufferbloat conditions.

    This plot shows socket-layer bufferbloat: even at baseline, a very large
    SO_RCVBUF causes packets to queue in the kernel receive buffer before the
    application reads them, increasing OWD. Under bufferbloat (1Mbit/s link),
    a large socket buffer amplifies this effect dramatically.

    What to look for:
    - baseline: OWD rises gently with buffer size — larger buffers mean more
      queuing in the socket layer before delivery to the application.
    - bufferbloat: OWD rises steeply — the slow link fills the socket buffer
      quickly, and each additional KB of buffer adds directly to queuing delay.
    - The gap between baseline and bufferbloat lines shows the combined effect
      of network-layer queuing (dummynet) and socket-layer queuing (SO_RCVBUF).
    """
    target = ["baseline", "bufferbloat"]
    available = [c for c in target if c in udp_agg["condition"].unique()]

    if not available:
        print("  Skipping Plot 7 — no buffer-sweep UDP data found.")
        print("  Run scripts/05_buffer_sweep.sh first.")
        return

    fig, ax = plt.subplots(figsize=(8, 5))

    for condition in available:
        subset = udp_agg[udp_agg["condition"] == condition]
        if subset.empty:
            continue

        ax.errorbar(
            subset["buffer_bytes"],
            subset["avg_owd_ms_mean"],
            yerr=subset["avg_owd_ms_std"].fillna(0),
            label=CONDITION_LABELS.get(condition, condition),
            color=CONDITION_COLORS.get(condition, "gray"),
            marker="o",
            capsize=4,
            linewidth=2,
        )

    ax.set_title("UDP One-Way Delay vs Socket Buffer Size (payload=1024B)")
    ax.set_xlabel("Socket Buffer Size (SO_RCVBUF)")
    ax.set_ylabel("Avg One-Way Delay (ms)")
    ax.set_ylim(bottom=0)
    ax.legend()
    ax.grid(True, which="both", linestyle="--", alpha=0.4)
    _buffer_x_axis(ax)

    fig.tight_layout()
    _save(fig, "07_owd_vs_buffer.png")


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main() -> None:
    """
    Loads both CSVs, aggregates across runs, and generates all seven plots.

    Two separate aggregation paths prevent the payload sweep and buffer sweep
    from contaminating each other:

    - Payload sweep (plots 1–5): rows where buffer_bytes == 65536 (the fixed
      default used in scripts 02–04). Grouped by payload_bytes + condition.

    - Buffer sweep (plots 6–7): rows where payload_bytes == 1024 (the fixed
      payload used in script 05). Grouped by buffer_bytes + condition.

    Plots 6–7 are skipped with a clear message if script 05 has not been run yet.
    """
    print("=== analyze.py ===")
    print()

    # ── Load ──────────────────────────────────────────────────────────────────
    print("Loading data...")
    tcp_df, udp_df = load_data()

    print(f"  TCP: {len(tcp_df)} rows | conditions: {sorted(tcp_df['condition'].unique())}")
    print(f"  UDP: {len(udp_df)} rows | conditions: {sorted(udp_df['condition'].unique())}")
    print()

    # ── Aggregate: payload sweep ───────────────────────────────────────────────
    # Filter to buffer_bytes == 65536 so that buffer sweep rows (which also use
    # some of the same conditions) do not skew the payload-sweep means.
    print("Aggregating payload sweep (buffer_bytes == 65536)...")

    tcp_payload_df = tcp_df[tcp_df["buffer_bytes"] == 65536].copy()
    udp_payload_df = udp_df[udp_df["buffer_bytes"] == 65536].copy()

    tcp_payload_agg = aggregate(
        tcp_payload_df,
        group_cols  = ["payload_bytes", "condition"],
        metric_cols = ["throughput_mbps", "avg_latency_ms", "latency_stdev_ms"],
    )

    udp_payload_agg = aggregate(
        udp_payload_df,
        group_cols  = ["payload_bytes", "condition"],
        metric_cols = ["throughput_mbps", "loss_rate_pct", "jitter_ms", "avg_owd_ms"],
    )

    print(f"  TCP payload cells: {len(tcp_payload_agg)}")
    print(f"  UDP payload cells: {len(udp_payload_agg)}")
    print()

    # ── Aggregate: buffer sweep ────────────────────────────────────────────────
    # Filter to payload_bytes == 1024 (the fixed payload used in script 05).
    # Multiple buffer sizes are present in this slice.
    print("Aggregating buffer sweep (payload_bytes == 1024)...")

    tcp_buffer_df = tcp_df[tcp_df["payload_bytes"] == 1024].copy()
    udp_buffer_df = udp_df[udp_df["payload_bytes"] == 1024].copy()

    tcp_buffer_agg = aggregate(
        tcp_buffer_df,
        group_cols  = ["buffer_bytes", "condition"],
        metric_cols = ["throughput_mbps", "avg_latency_ms", "latency_stdev_ms"],
    )

    udp_buffer_agg = aggregate(
        udp_buffer_df,
        group_cols  = ["buffer_bytes", "condition"],
        metric_cols = ["throughput_mbps", "loss_rate_pct", "jitter_ms", "avg_owd_ms"],
    )

    print(f"  TCP buffer cells: {len(tcp_buffer_agg)}")
    print(f"  UDP buffer cells: {len(udp_buffer_agg)}")
    print()

    # ── Plot ──────────────────────────────────────────────────────────────────
    print("Generating plots...")

    # Plots 1–5: payload sweep
    plot_1_throughput_vs_payload(tcp_payload_agg, udp_payload_agg)
    plot_2_tcp_latency_vs_payload(tcp_payload_agg)
    plot_3_udp_loss_vs_payload(udp_payload_agg)
    plot_4_udp_jitter_vs_payload(udp_payload_agg)
    plot_5_congestion_comparison(tcp_payload_agg, udp_payload_agg)

    # Plots 6–7: buffer sweep (skipped gracefully if script 05 hasn't run yet)
    plot_6_throughput_vs_buffer(tcp_buffer_agg, udp_buffer_agg)
    plot_7_owd_vs_buffer(udp_buffer_agg)

    print()
    print(f"Done. All plots saved to {os.path.abspath(PLOTS_DIR)}/")


if __name__ == "__main__":
    main()
