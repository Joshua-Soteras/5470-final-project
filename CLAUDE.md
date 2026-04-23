# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

CS 5470 (Advanced Computer Networks) final project. Measures how MTU/payload size and socket buffer settings affect TCP and UDP performance on loopback. Solo implementation covering all three modules from the original group proposal: TCP measurement, UDP measurement, and data pipeline + visualization.

## Environment

Python 3.14, managed by **uv**. All scripts must be run via `uv run` or from within the activated venv.

```bash
# Run a module
uv run python src/tcp_module.py --payload 1024 --buffer 65536 --messages 1000 --label baseline --run 1
uv run python src/udp_module.py --payload 1024 --buffer 65536 --messages 1000 --rate 500 --label baseline --run 1

# Add a dependency
uv add <package>

# Activate venv directly (alternative)
source .venv/bin/activate
```

## Network Emulation

This project runs on **macOS**. Do not use `tc`/`netem` — those are Linux-only. The macOS equivalent is `dummynet` via `pfctl`:

```bash
# Apply a condition (delay ms, bandwidth Mbit/s, queue slots, packet loss rate)
sudo dnctl pipe 1 config delay 20 bw 1Mbit/s queue 1000 plr 0.01
echo "dummynet out quick on lo0 all pipe 1" | sudo pfctl -f -
sudo pfctl -e

# Tear down
sudo dnctl -q flush
sudo pfctl -f /etc/pf.conf
sudo pfctl -d
```

All network conditions are applied to the **loopback interface (lo0)**. The loopback MTU on macOS is 16384 bytes — IP fragmentation only occurs for application payloads above ~16KB.

## Architecture

Each measurement module is self-contained and follows the same pattern:
- A **server/receiver** runs in a background daemon thread
- A **client/sender** runs in the main thread and blocks until complete
- Results are appended to a shared CSV in `results/` via `save_result()`
- The orchestrator function (`measure_throughput`, `measure_latency`, `run_udp_experiment`) handles thread lifecycle and calls `save_result()`

**TCP framing**: the latency (ping-pong) mode uses a 4-byte big-endian length prefix (`struct.pack('>I', payload_size)`) before each message so the echo server knows how many bytes to wait for. Throughput mode has no framing — the server reads until `recv()` returns `b''`.

**UDP datagram format**: every datagram has a 16-byte header (`HEADER_FMT = ">QQ"` — seq_num + send timestamp in nanoseconds) followed by zero-padding to reach `payload_size`. Minimum payload is 16 bytes (`HEADER_SIZE`). The receiver uses `time.perf_counter_ns()` for arrival timestamps.

## Ports

| Module | Use | Port |
|--------|-----|------|
| tcp_module | throughput | 5201 |
| tcp_module | latency | 5202 |
| udp_module | receiver | 5301 |

## Files Still To Be Created

- `src/emulate.sh` — dummynet setup/teardown for named conditions (baseline, high_latency, bufferbloat, lossy, congested)
- `src/analyze.py` — loads both CSVs, aggregates across 3 runs, generates 5 plots into `plots/`
- `run_experiments.sh` — loops over all payload sizes, buffer sizes, conditions, and run indices; calls both modules and emulate.sh

## CSV Schemas

**`results/tcp_results.csv`**
```
protocol, payload_bytes, buffer_bytes, condition, throughput_mbps, avg_latency_ms, latency_stdev_ms, run_index
```

**`results/udp_results.csv`**
```
protocol, payload_bytes, buffer_bytes, condition, throughput_mbps, loss_rate_pct, jitter_ms, run_index
```

## Known Issue

`src/udp_module.py` has syntax errors — the `raise NotImplementedError(...)` stubs on lines 70, 105, 129, and 150 are missing their closing `")`. Fix these before running or importing the module.
