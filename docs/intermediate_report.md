# CS 5470 — Intermediate Progress Report
**Project:** Network Performance Analyzer: MTU, Bufferbloat, TCP vs UDP  
**Student:** Joshua Soteras  
**Date:** April 24, 2026

---

## Project Overview

This project measures how application payload size (MTU sweep) and socket buffer settings affect network performance under TCP and UDP. All experiments run over the loopback interface (`127.0.0.1`) on macOS, with network conditions emulated via `dummynet` (`pfctl`). The three planned conditions of interest are bufferbloat, high latency, and lossy/congested links. Results are collected into CSV files and will be visualized with comparative plots.

---

## Work Completed

### UDP Measurement Module (`src/udp_module.py`) — Complete

The UDP module is fully implemented and tested. It runs a paced sender and a timeout-driven receiver as concurrent threads on loopback, collecting four metrics per experiment:

- **Throughput (Mbps):** total bytes received ÷ elapsed time (first to last datagram)
- **Packet loss (%):** detected via sequence number gaps — `(sent − received) / sent × 100`
- **Jitter (ms):** mean absolute deviation of consecutive inter-arrival gaps (RFC 3550 §A.8)
- **One-way delay / OWD (ms):** each datagram carries a nanosecond send timestamp; the receiver subtracts it from its own arrival timestamp to compute forward-path latency (valid on loopback where both sides share the same monotonic clock)

The module accepts `--payload`, `--buffer`, `--messages`, `--rate`, `--label`, and `--run` flags, appends results to `results/udp_results.csv`, and prints a one-line summary per run.

A key design decision was rate-pacing the sender (`--rate` in packets/sec with `time.sleep` between sends) rather than flooding. A fire-and-forget flood saturates the loopback queue before the receiver can drain it, producing artificial loss that would contaminate the buffer-size comparison. Pacing produces observable queuing behaviour instead.

### Background TCP Flood (`src/background_flood.py`) — Complete

The proposal specified a congestion scenario using competing background traffic, which dummynet's bandwidth cap alone cannot replicate. dummynet throttles the link but does not create a rival TCP flow — TCP's AIMD congestion control only triggers when queue overflow from competing traffic causes loss events.

`background_flood.py` spawns a TCP flood server and client as daemon threads on port 5400, saturating the loopback queue with continuous traffic. It exposes `start_flood()` / `stop_flood()` for use from `run_experiments.sh` and can also be run as a standalone background process (`python3 src/background_flood.py &`).

### Test Suite (`tests/test_udp_module.py`) — 14 Tests, All Passing

| Category | Tests | Coverage |
|----------|-------|----------|
| `compute_jitter()` unit tests | 6 | Edge cases (empty, 1, 2 packets), uniform streams, hand-calculated alternating gaps, return type |
| `save_result()` unit tests | 4 | File creation, header written once, row appending, column completeness |
| `run_udp_sender()` validation | 3 | Rejects payload < 16 bytes, rejects zero, accepts minimum valid size |
| Integration (full loopback) | 1 | End-to-end experiment: throughput > 0, loss < 1%, jitter ≥ 0, OWD sane, CSV written |

---

## Work Remaining

| Component | Description |
|-----------|-------------|
| `src/tcp_module.py` | TCP throughput mode (bulk transfer) and latency mode (ping-pong RTT); currently stubbed |
| `src/emulate.sh` | dummynet setup/teardown for five named conditions: `baseline`, `high_latency`, `bufferbloat`, `lossy`, `congested` |
| `run_experiments.sh` | Full sweep: 6 payload sizes × 4 buffer sizes × 5 conditions × 3 runs × 2 protocols |
| `src/analyze.py` | CSV aggregation (mean ± stdev across 3 runs) and 5 comparative plots |

---

## Design Decisions and Deviations from Proposal

**macOS vs. Linux:** The proposal referenced `tc`/`netem` for network emulation. This project runs on macOS, where those tools are unavailable. `dummynet` (via `pfctl`) is the macOS/BSD equivalent and supports the same capabilities: propagation delay, bandwidth limits, queue depth for bufferbloat, and packet loss rate.

**OWD added to UDP:** The proposal listed round-trip latency as a metric, covered by TCP's ping-pong mode. UDP one-way delay was added as an additional metric at no implementation cost — the send timestamp was already in every datagram header and only needed to be read rather than discarded. This enables a direct forward-path latency comparison between TCP and UDP across all conditions.

**Solo implementation:** This project was originally scoped across three team members. All three modules (TCP, UDP, and the data pipeline) are being implemented solo, which informed the decision to fully complete and test one module at a time rather than partially implementing all three in parallel.
