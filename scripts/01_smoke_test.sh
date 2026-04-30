#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# 01_smoke_test.sh — Verify both modules run and write output before committing
# to a full sweep. Runs one UDP experiment and one TCP experiment at a small
# message count so the whole thing finishes in under 10 seconds.
#
# Usage:
#   bash scripts/01_smoke_test.sh
#
# Expected output:
#   UDP | baseline | payload=1024B | throughput=... | loss=0.00% | ...
#   TCP | baseline | payload=1024B | throughput=... | avg_rtt=...
#   [PASS] results/udp_results.csv exists
#   [PASS] results/tcp_results.csv exists
# -----------------------------------------------------------------------------

set -e  # exit immediately if any command returns a non-zero status

PAYLOAD=1024
BUFFER=65536
MESSAGES=100   # small count — just enough to confirm the pipeline works
RATE=200       # pps — 100 messages at 200 pps takes 0.5s + 2s receiver timeout

echo "=== Smoke Test ==="
echo "Running one UDP experiment..."

uv run python src/udp_module.py \
    --payload  $PAYLOAD  \
    --buffer   $BUFFER   \
    --messages $MESSAGES \
    --rate     $RATE     \
    --label    baseline  \
    --run      1

echo ""
echo "Running one TCP experiment..."

uv run python src/tcp_module.py \
    --payload  $PAYLOAD  \
    --buffer   $BUFFER   \
    --messages $MESSAGES \
    --label    baseline  \
    --run      1

# ── Verify CSV files were created ──────────────────────────────────────────
echo ""
echo "=== Checking output ==="

if [ -f "results/udp_results.csv" ]; then
    echo "[PASS] results/udp_results.csv exists"
    echo "       $(wc -l < results/udp_results.csv) line(s) including header"
else
    echo "[FAIL] results/udp_results.csv was not created"
    exit 1
fi

if [ -f "results/tcp_results.csv" ]; then
    echo "[PASS] results/tcp_results.csv exists"
    echo "       $(wc -l < results/tcp_results.csv) line(s) including header"
else
    echo "[FAIL] results/tcp_results.csv was not created"
    exit 1
fi

echo ""
echo "Smoke test passed. Run 02_baseline_sweep.sh to collect full baseline data."
