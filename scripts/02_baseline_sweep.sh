#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# 02_baseline_sweep.sh — Collect baseline data across all payload sizes.
#
# No dummynet required — runs on raw loopback with no artificial conditions.
# This is your control group. Every other condition is compared against these
# numbers when you analyze results.
#
# What it runs:
#   TCP: 6 payload sizes × 3 runs = 18 rows
#   UDP: 5 payload sizes × 3 runs = 15 rows  (65536 excluded — exceeds UDP 65507B limit)
#   Total: 33 rows appended to results/tcp_results.csv and results/udp_results.csv
#
# Runtime: ~5-8 minutes (UDP is paced at 500 pps so each run takes ~2s + timeout)
#
# Usage:
#   bash scripts/02_baseline_sweep.sh
# -----------------------------------------------------------------------------

set -e

# TCP payload sizes: 64B → 65536B (6 sizes).
# TCP streams so 65536B is valid — the OS segments it transparently.
TCP_PAYLOAD_SIZES=(64 256 1024 4096 16384 65536)

# UDP payload sizes: 64B → 16384B (5 sizes, no 65536).
# A UDP datagram is limited to 65507 bytes (65535 - 20 IP - 8 UDP header).
# 65536 exceeds that limit and the kernel rejects the sendto() call with EMSGSIZE.
# 16384 is already the interesting upper bound — it sits at the loopback MTU
# (16KB on macOS) so it tests IP fragmentation without hitting the protocol limit.
UDP_PAYLOAD_SIZES=(64 256 1024 4096 16384)

BUFFER=65536    # fixed buffer size for the baseline sweep
MESSAGES=500    # enough messages to get a stable average without taking too long
RATE=500        # UDP send rate in packets per second
LABEL="baseline"

echo "=== Baseline Sweep ==="
echo "TCP payload sizes: ${TCP_PAYLOAD_SIZES[*]}"
echo "UDP payload sizes: ${UDP_PAYLOAD_SIZES[*]}"
echo "Buffer: ${BUFFER}B | Messages: ${MESSAGES} | UDP rate: ${RATE} pps"
echo "Runs per cell: 3"
echo ""

# Total = (TCP sizes + UDP sizes) × 3 runs
total=$(( (${#TCP_PAYLOAD_SIZES[@]} + ${#UDP_PAYLOAD_SIZES[@]}) * 3 ))
completed=0

for payload in "${TCP_PAYLOAD_SIZES[@]}"; do
    for run in 1 2 3; do
        completed=$((completed + 1))
        echo "[$completed/$total] TCP | payload=${payload}B | run=${run}"
        uv run python src/tcp_module.py \
            --payload  $payload  \
            --buffer   $BUFFER   \
            --messages $MESSAGES \
            --label    $LABEL    \
            --run      $run
        echo ""
    done
done

for payload in "${UDP_PAYLOAD_SIZES[@]}"; do
    for run in 1 2 3; do
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
