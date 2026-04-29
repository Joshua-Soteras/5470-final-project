#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# 02_baseline_sweep.sh — Collect baseline data across all payload sizes.
#
# No dummynet required — runs on raw loopback with no artificial conditions.
# This is your control group. Every other condition is compared against these
# numbers when you analyze results.
#
# What it runs:
#   6 payload sizes × 2 protocols × 3 runs = 36 rows total
#   Appended to results/tcp_results.csv and results/udp_results.csv
#
# Runtime: ~5-8 minutes (UDP is paced at 500 pps so each run takes ~2s + timeout)
#
# Usage:
#   bash scripts/02_baseline_sweep.sh
# -----------------------------------------------------------------------------

set -e

# Payload sizes in bytes. Spans small (64B) to large (64KB) to show:
#   - 64-256B:   high per-message overhead relative to payload
#   - 1024B:     typical application message size
#   - 4096B:     medium transfer
#   - 16384B:    near the loopback MTU (16KB) — fragmentation threshold
#   - 65536B:    above MTU — triggers IP fragmentation on loopback
PAYLOAD_SIZES=(64 256 1024 4096 16384 65536)

BUFFER=65536    # fixed buffer size for the baseline sweep
MESSAGES=500    # enough messages to get a stable average without taking too long
RATE=500        # UDP send rate in packets per second
LABEL="baseline"

echo "=== Baseline Sweep ==="
echo "Payload sizes: ${PAYLOAD_SIZES[*]}"
echo "Buffer: ${BUFFER}B | Messages: ${MESSAGES} | UDP rate: ${RATE} pps"
echo "Runs per cell: 3"
echo ""

total=$((${#PAYLOAD_SIZES[@]} * 3))
completed=0

for payload in "${PAYLOAD_SIZES[@]}"; do
    for run in 1 2 3; do
        completed=$((completed + 1))
        echo "[$completed/$total] TCP | payload=${payload}B | run=${run}"
        uv run python src/tcp_module.py \
            --payload  $payload  \
            --buffer   $BUFFER   \
            --messages $MESSAGES \
            --label    $LABEL    \
            --run      $run

        completed=$((completed + 1))
        echo "[$completed/$total] UDP | payload=${payload}B | run=${run}"
        uv run python src/udp_module.py \
            --payload  $payload  \
            --buffer   $BUFFER   \
            --messages $MESSAGES \
            --rate     $RATE     \
            --label    $LABEL    \
            --run      $run

        echo ""
    done
done

echo "=== Baseline sweep complete ==="
echo "TCP rows written: $(( $(wc -l < results/tcp_results.csv) - 1 ))"
echo "UDP rows written: $(( $(wc -l < results/udp_results.csv) - 1 ))"
echo ""
echo "Next step: run 03_congested_sweep.sh"
