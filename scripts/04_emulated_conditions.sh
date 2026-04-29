#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# 04_emulated_conditions.sh — Collect data under dummynet network conditions.
#
# Requires sudo — dummynet commands need root to modify the packet filter.
# Each condition is applied to lo0, experiments run, then dummynet is torn
# down before the next condition starts.
#
# IMPORTANT: Always run the teardown function if the script is interrupted.
# Leaving dummynet active will slow all loopback traffic on your machine,
# including the test suite. If the script crashes, run manually:
#
#   sudo dnctl -q flush && sudo pfctl -f /etc/pf.conf && sudo pfctl -d
#
# Conditions applied:
#   high_latency  — 50ms delay, 100Mbit/s, small queue, no loss
#   bufferbloat   — 0ms delay, 1Mbit/s, 1000-slot queue, no loss
#   lossy         — 0ms delay, 100Mbit/s, small queue, 5% random loss
#
# What it runs:
#   3 conditions × 5 payload sizes × 2 protocols × 3 runs = 90 rows total
#
# Runtime: ~30-45 minutes (bufferbloat condition is slow due to 1Mbit/s cap)
#
# Usage:
#   bash scripts/04_emulated_conditions.sh
# -----------------------------------------------------------------------------

set -e

PAYLOAD_SIZES=(64 256 1024 4096 16384)
BUFFER=65536
MESSAGES=500
RATE=200        # lower rate for emulated conditions — pacing matters more under slow links
MESSAGES_TCP=200  # fewer messages for TCP under slow conditions to keep runtime reasonable

# ── dummynet helpers ──────────────────────────────────────────────────────────

apply_condition() {
    local label=$1 delay=$2 bw=$3 queue=$4 plr=$5
    echo "  Applying dummynet: delay=${delay}ms bw=${bw}Mbit/s queue=${queue} loss=${plr}"
    sudo dnctl pipe 1 config delay $delay bw ${bw}Mbit/s queue $queue plr $plr
    echo "dummynet out quick on lo0 all pipe 1" | sudo pfctl -f -
    sudo pfctl -e 2>/dev/null || true  # -e enables pf; ignore error if already enabled
}

teardown() {
    echo "  Tearing down dummynet..."
    sudo dnctl -q flush
    sudo pfctl -f /etc/pf.conf
    sudo pfctl -d 2>/dev/null || true  # -d disables pf; ignore error if already disabled
    echo "  Teardown complete."
}

# Always tear down on exit, even if the script fails mid-run
trap teardown EXIT

# ── Condition definitions ─────────────────────────────────────────────────────
# Format: "label delay_ms bw_mbit queue_slots loss_rate"
CONDITIONS=(
    "high_latency  50  100   32   0.0"
    "bufferbloat    0    1  1000  0.0"
    "lossy          0  100   32   0.05"
)

# ── Main sweep ────────────────────────────────────────────────────────────────

echo "=== Emulated Conditions Sweep ==="
echo "Conditions: high_latency, bufferbloat, lossy"
echo "Payload sizes: ${PAYLOAD_SIZES[*]}"
echo "Note: this script requires sudo for dummynet commands."
echo ""

for condition_str in "${CONDITIONS[@]}"; do
    # Split the condition string into its fields
    read -r label delay bw queue plr <<< "$condition_str"

    echo "──────────────────────────────────────"
    echo "Condition: $label"
    echo "──────────────────────────────────────"

    apply_condition "$label" "$delay" "$bw" "$queue" "$plr"
    echo ""

    cell=0
    total=$((${#PAYLOAD_SIZES[@]} * 3 * 2))

    for payload in "${PAYLOAD_SIZES[@]}"; do
        for run in 1 2 3; do
            cell=$((cell + 1))
            echo "  [$cell/$total] TCP | $label | payload=${payload}B | run=${run}"
            uv run python src/tcp_module.py \
                --payload  $payload      \
                --buffer   $BUFFER       \
                --messages $MESSAGES_TCP \
                --label    $label        \
                --run      $run

            cell=$((cell + 1))
            echo "  [$cell/$total] UDP | $label | payload=${payload}B | run=${run}"
            uv run python src/udp_module.py \
                --payload  $payload  \
                --buffer   $BUFFER   \
                --messages $MESSAGES \
                --rate     $RATE     \
                --label    $label    \
                --run      $run

            echo ""
        done
    done

    teardown
    echo ""
done

# Disable the trap now that we've torn down cleanly
trap - EXIT

echo "=== Emulated conditions sweep complete ==="
echo "TCP rows written: $(( $(wc -l < results/tcp_results.csv) - 1 ))"
echo "UDP rows written: $(( $(wc -l < results/udp_results.csv) - 1 ))"
echo ""
echo "All data collected. Run src/analyze.py to generate plots."
