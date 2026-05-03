#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# 05_buffer_sweep.sh — Collect data across socket buffer sizes.
#
# This is the buffer-settings half of the project's stated research question:
# "how do MTU/payload size and socket buffer settings affect performance?"
# Scripts 02–04 held buffer_bytes fixed at 65536. This script varies it.
#
# What it runs:
#   Fixed payload: 1024B (mid-range — shows both small-payload and large-buffer
#   effects without hitting protocol limits)
#   Buffer sizes: 4096, 16384, 65536, 131072, 262144 bytes
#   Conditions: baseline (no dummynet) + bufferbloat (dummynet, requires sudo)
#   Both protocols, 3 runs each
#   Total: 5 buffer sizes × 2 protocols × 3 runs × 2 conditions = 60 rows
#
# Why two conditions?
#   baseline    — shows how SO_SNDBUF/SO_RCVBUF alone affects throughput;
#                 on a fast loopback the effect is subtle at large buffers
#   bufferbloat — shows socket-layer bufferbloat: a large receive buffer queues
#                 packets that the application hasn't read yet, increasing OWD
#                 and jitter even before network-layer queuing kicks in
#
# Runtime: ~5 min (baseline) + ~15 min (bufferbloat at 1Mbit/s) = ~20 min total
#
# Usage:
#   bash scripts/05_buffer_sweep.sh
#
# If interrupted mid-run, always verify dummynet is clean before re-running:
#   sudo dnctl show          # must return nothing
#   sudo dnctl -q flush && sudo pfctl -f /etc/pf.conf && sudo pfctl -d
# -----------------------------------------------------------------------------

set -e

PAYLOAD=1024
BUFFER_SIZES=(4096 16384 65536 131072 262144)

# TCP uses fewer messages under bufferbloat because 1Mbit/s means each run is
# slow; 200 messages keeps per-run time under ~2 minutes.
MESSAGES_TCP=200
MESSAGES_UDP=500

# UDP send rates — MUST match the rates used by the corresponding condition in
# scripts 02 and 04 so that buffer-sweep data and payload-sweep data are
# directly comparable when analyze.py aggregates rows from both scripts.
#
# baseline:    500 pps — matches 02_baseline_sweep.sh (RATE=500)
# bufferbloat: 200 pps — matches 04_emulated_conditions.sh (RATE=200)
#
# Bug fixed: the original version of this script used RATE=500 for both
# conditions. At 500 pps with a 1Mbit/s cap, UDP loses ~50% of datagrams
# (send rate 4× the link capacity). Script 04 uses 200 pps, which sits just
# above the cap and produces ~8% loss — the intended bufferbloat signature.
# Using 500 pps here produced results that were inconsistent with script 04
# and contaminated the payload-sweep bufferbloat cells when both datasets
# were aggregated together in analyze.py.
RATE_BASELINE=500
RATE_BUFFERBLOAT=200

# ── dummynet helpers ──────────────────────────────────────────────────────────

apply_bufferbloat() {
    echo "  Applying dummynet: 1Mbit/s, 100-slot queue, 0ms delay, 0% loss"
    sudo dnctl pipe 1 config delay 0 bw 1Mbit/s queue 100 plr 0.0
    echo "dummynet out quick on lo0 all pipe 1" | sudo pfctl -f - 2>/dev/null || true
    sudo pfctl -e 2>/dev/null || true
}

teardown() {
    echo "  Tearing down dummynet..."
    sudo -v 2>/dev/null || true
    sudo dnctl -q flush
    sudo pfctl -f /etc/pf.conf 2>/dev/null || true
    sudo pfctl -d 2>/dev/null || true
    echo "  Teardown complete."
}

# Tear down dummynet on any exit (normal, error, or Ctrl+C).
trap teardown EXIT

# ── Summary ───────────────────────────────────────────────────────────────────

echo "=== Buffer Size Sweep ==="
echo "Fixed payload: ${PAYLOAD}B"
echo "Buffer sizes: ${BUFFER_SIZES[*]}"
echo "Conditions: baseline + bufferbloat"
echo "TCP messages: ${MESSAGES_TCP} | UDP messages: ${MESSAGES_UDP} | UDP rate baseline: ${RATE_BASELINE} pps / bufferbloat: ${RATE_BUFFERBLOAT} pps"
echo "Runs per cell: 3"
echo ""

total=$(( ${#BUFFER_SIZES[@]} * 3 * 2 * 2 ))
completed=0

# ── Condition 1: baseline (no dummynet) ───────────────────────────────────────

echo "──────────────────────────────────────"
echo "Condition: baseline"
echo "──────────────────────────────────────"
echo ""

for buf in "${BUFFER_SIZES[@]}"; do
    for run in 1 2 3; do
        completed=$((completed + 1))
        echo "[$completed/$total] TCP | baseline | buffer=${buf}B | run=${run}"
        uv run python src/tcp_module.py \
            --payload  $PAYLOAD      \
            --buffer   $buf          \
            --messages $MESSAGES_TCP \
            --label    baseline      \
            --run      $run
        echo ""

        completed=$((completed + 1))
        echo "[$completed/$total] UDP | baseline | buffer=${buf}B | run=${run}"
        uv run python src/udp_module.py \
            --payload  $PAYLOAD         \
            --buffer   $buf             \
            --messages $MESSAGES_UDP    \
            --rate     $RATE_BASELINE   \
            --label    baseline         \
            --run      $run
        echo ""
    done
done

echo "Baseline portion complete."
echo ""

# ── Condition 2: bufferbloat (dummynet, requires sudo) ────────────────────────

echo "──────────────────────────────────────"
echo "Condition: bufferbloat"
echo "This requires sudo for dummynet commands."
echo "──────────────────────────────────────"
echo ""

# Cache sudo credential upfront. The bufferbloat sweep takes ~15 minutes;
# sudo -v is called again before each buffer size to prevent expiry.
sudo -v
apply_bufferbloat
echo ""

for buf in "${BUFFER_SIZES[@]}"; do
    # Refresh sudo credential before each buffer size — the default sudo timeout
    # (5 minutes) can expire during a multi-buffer sweep.
    sudo -v

    for run in 1 2 3; do
        completed=$((completed + 1))
        echo "[$completed/$total] TCP | bufferbloat | buffer=${buf}B | run=${run}"
        uv run python src/tcp_module.py \
            --payload  $PAYLOAD        \
            --buffer   $buf            \
            --messages $MESSAGES_TCP   \
            --label    bufferbloat     \
            --run      $run
        echo ""

        completed=$((completed + 1))
        echo "[$completed/$total] UDP | bufferbloat | buffer=${buf}B | run=${run}"
        uv run python src/udp_module.py \
            --payload  $PAYLOAD           \
            --buffer   $buf               \
            --messages $MESSAGES_UDP      \
            --rate     $RATE_BUFFERBLOAT  \
            --label    bufferbloat        \
            --run      $run
        echo ""
    done
done

# Disable the trap now that we've torn down cleanly in teardown()
trap - EXIT
teardown
echo ""

# ── Final row counts ──────────────────────────────────────────────────────────

echo "=== Buffer sweep complete ==="
echo "TCP rows written: $(( $(wc -l < results/tcp_results.csv) - 1 ))"
echo "UDP rows written: $(( $(wc -l < results/udp_results.csv) - 1 ))"
echo ""
echo "Verify the new rows are present:"
echo "  grep 'baseline' results/tcp_results.csv | awk -F',' '{print \$3}' | sort -u"
echo "  # Should list: 4096, 16384, 65536, 131072, 262144"
echo ""
echo "Next step: run src/analyze.py to regenerate all plots including the buffer size plots."
