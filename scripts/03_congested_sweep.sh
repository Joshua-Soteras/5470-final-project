#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# 03_congested_sweep.sh — Collect data under competing background traffic.
#
# No dummynet required — congestion is created by background_flood.py, which
# saturates the loopback queue with a separate TCP flow. This triggers TCP's
# AIMD congestion control on the experiment flow.
#
# TCP gets --flood; UDP does not — UDP has no congestion control so it does
# not respond to the flood. That contrast (TCP backs off, UDP holds steady)
# is one of the core findings of the project.
#
# What it runs:
#   5 payload sizes × 2 protocols × 3 runs = 30 rows total
#   (65536B is excluded — flood + large payload makes the experiment very slow)
#
# Runtime: ~6-10 minutes
#
# Usage:
#   bash scripts/03_congested_sweep.sh
# -----------------------------------------------------------------------------

set -e

PAYLOAD_SIZES=(64 256 1024 4096 16384)
BUFFER=65536
MESSAGES=500
RATE=500
LABEL="congested"

echo "=== Congested Sweep ==="
echo "Payload sizes: ${PAYLOAD_SIZES[*]}"
echo "TCP runs with --flood (background competing traffic)"
echo "UDP runs without flood (no congestion control to observe)"
echo ""

total=$((${#PAYLOAD_SIZES[@]} * 3 * 2))
completed=0

for payload in "${PAYLOAD_SIZES[@]}"; do
    for run in 1 2 3; do
        completed=$((completed + 1))
        echo "[$completed/$total] TCP | congested | payload=${payload}B | run=${run} [flood active]"
        uv run python src/tcp_module.py \
            --payload  $payload  \
            --buffer   $BUFFER   \
            --messages $MESSAGES \
            --label    $LABEL    \
            --run      $run      \
            --flood

        completed=$((completed + 1))
        echo "[$completed/$total] UDP | congested | payload=${payload}B | run=${run}"
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

echo "=== Congested sweep complete ==="
echo "TCP rows written: $(( $(wc -l < results/tcp_results.csv) - 1 ))"
echo "UDP rows written: $(( $(wc -l < results/udp_results.csv) - 1 ))"
echo ""
echo "Next step: run 04_emulated_conditions.sh (requires sudo for dummynet)"
