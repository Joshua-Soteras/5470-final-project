# CS 5470 — Network Performance Analyzer

Measures how **MTU / payload size** and **socket buffer settings** affect TCP and UDP
performance on the loopback interface (`127.0.0.1`). Network conditions (latency,
bandwidth caps, packet loss, bufferbloat) are emulated with macOS's built-in
`dummynet` traffic shaper.

| Module | Status | What it does |
|--------|--------|--------------|
| `src/udp_module.py` | Complete | UDP sender + receiver — throughput, loss, jitter, one-way delay |
| `src/tcp_module.py` | Complete | TCP sender + receiver — throughput, RTT latency |
| `src/background_flood.py` | Complete | Background TCP flood for congestion comparison |
| `src/analyze.py` | Not yet built | Aggregates CSVs, generates 5 plots |

Supporting scripts (`src/emulate.sh`, `run_experiments.sh`) are still to be built —
see [Files still to be created](#files-still-to-be-created).

---

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| macOS | Any modern version | `dummynet` is macOS/BSD only — Linux is not supported |
| Python | 3.14+ | Enforced by `.python-version` |
| uv | Latest | Python package + venv manager — replaces pip/venv |

### Install uv

If you don't have `uv` yet, install it with one command:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then restart your terminal so the `uv` command is on your `PATH`.

---

## Setup

All steps below are run once, from the project root directory.

### 1. Clone the repo (if you haven't already)

```bash
git clone <repo-url>
cd 5470_Final_Project
```

### 2. Create the virtual environment and install dependencies

`uv` reads `pyproject.toml` and handles everything automatically:

```bash
uv sync
```

This creates a `.venv/` folder inside the project and installs:
- `matplotlib` — plotting
- `numpy` — numerical helpers
- `pandas` — CSV aggregation
- `pytest` — test runner (dev dependency)

You do **not** need to run `pip install` or `python -m venv` manually.

### 3. Verify the setup

```bash
uv run python --version        # should print Python 3.14.x
uv run pytest tests/ -v        # 28 tests should pass
```

---

## Running a Module

Always prefix commands with `uv run` so they execute inside the project's virtual
environment. Alternatively, activate the venv first with `source .venv/bin/activate`
and then run `python` directly.

### UDP module

Sends datagrams at a fixed rate and measures what arrived at the receiver.

```bash
uv run python src/udp_module.py \
    --payload  1024  \   # datagram size in bytes (must be >= 16)
    --buffer  65536  \   # socket buffer size in bytes (SO_SNDBUF / SO_RCVBUF)
    --messages 1000  \   # number of datagrams to send
    --rate     500   \   # send rate in packets per second
    --label  baseline\   # network condition label written to the CSV
    --run      1         # run index 1-3 (for repeated trials)
```

**Example output:**

```
UDP | baseline | payload=1024B | throughput=4.09 Mbps | loss=0.00% | jitter=0.12ms | owd=0.031ms
```

Results are appended to `results/udp_results.csv` automatically.

---

### TCP module

Runs two measurements back-to-back: a bulk-transfer throughput test, then a
ping-pong RTT latency test. Both results are saved in a single CSV row.

```bash
uv run python src/tcp_module.py \
    --payload  1024  \   # message size in bytes
    --buffer  65536  \   # socket buffer size in bytes (SO_SNDBUF / SO_RCVBUF)
    --messages 1000  \   # number of messages (used for both throughput and latency)
    --label  baseline\   # network condition label written to the CSV
    --run      1         # run index 1-3 (for repeated trials)
```

**Example output:**

```
TCP | baseline | payload=1024B | throughput=3241.57 Mbps | avg_rtt=0.018ms | rtt_stdev=0.004ms
```

Results are appended to `results/tcp_results.csv` automatically.

---

### Congestion comparison — TCP with and without background flood

The `--flood` flag starts a background TCP flood on port 5400 that competes for
loopback bandwidth during the experiment. This triggers TCP's AIMD congestion
control (the sender backs off when it detects loss from queue overflow). Run the
same configuration with and without `--flood` to see the difference directly:

```bash
# Step 1 — baseline: no competing traffic
uv run python src/tcp_module.py \
    --payload 1024 --buffer 65536 --messages 1000 --label baseline --run 1

# Step 2 — congested: background flood active during the experiment
uv run python src/tcp_module.py \
    --payload 1024 --buffer 65536 --messages 1000 --label congested --run 1 --flood
```

With the flood active you should see **lower throughput** and **higher RTT** as
TCP's congestion window shrinks in response to competing traffic. UDP does not
have this mechanism — you can run the same comparison on `udp_module.py` (without
`--flood`, since it has no congestion control built in) to see it hold its send
rate regardless.

> **Why `--flood` instead of just dummynet?**  
> dummynet caps bandwidth but does not create a rival TCP flow. AIMD only triggers
> when a competing flow fills the queue and causes loss events — the flood creates
> that contention. See `docs/MEASUREMENT_GAPS.md` for a full explanation.

---

## How Measurements Work

### What one run does

Each module invocation produces **one row** in its CSV. The table below shows
what each module measures and how:

| Module | Metric | How it is measured |
|--------|--------|--------------------|
| TCP | **Throughput** | Server-side: total bytes received ÷ elapsed time (first byte → connection close) |
| TCP | **Avg RTT** | Ping-pong echo: time from sending `prefix + payload` to receiving the full echo back |
| TCP | **RTT stdev** | Standard deviation across all `--messages` ping-pong round trips |
| UDP | **Throughput** | Receiver-side: total bytes received × 8 ÷ elapsed time (first → last datagram) |
| UDP | **Loss rate** | Sequence number gaps — `(sent − received) / sent × 100` |
| UDP | **Jitter** | Mean absolute deviation of consecutive inter-arrival gaps (RFC 3550) |
| UDP | **One-way delay** | `arrival_ns − send_ns` per datagram — valid on loopback where clocks are shared |

Both modules run the sender and receiver as threads on the same machine over
loopback (`127.0.0.1`).

### The `--run` flag — repetitions for averaging

A single run can be noisy due to OS scheduling. The `--run` flag (1, 2, or 3) is
just a label so results can be averaged later. Run the **same configuration three
times** and `analyze.py` will compute mean ± stdev across them:

```bash
# Example: three runs of the same UDP configuration
uv run python src/udp_module.py --payload 1024 --buffer 65536 --messages 1000 --rate 500 --label baseline --run 1
uv run python src/udp_module.py --payload 1024 --buffer 65536 --messages 1000 --rate 500 --label baseline --run 2
uv run python src/udp_module.py --payload 1024 --buffer 65536 --messages 1000 --rate 500 --label baseline --run 3
```

Each call appends a new row — the CSV is never overwritten.

### Getting different measurements — vary the parameters

| Parameter | What varying it shows |
|-----------|-----------------------|
| `--payload` | How message size affects throughput, loss, jitter, and latency |
| `--buffer` | How socket buffer size affects queuing and throughput |
| `--label` | Different network conditions — use `emulate.sh` to apply dummynet first |
| `--flood` (TCP only) | Whether competing background traffic causes AIMD backoff |

Example: sweeping payload size at a fixed buffer:

```bash
for payload in 64 256 1024 4096 16384; do
    uv run python src/tcp_module.py --payload $payload --buffer 65536 --messages 500 --label baseline --run 1
    uv run python src/udp_module.py --payload $payload --buffer 65536 --messages 500 --rate 500 --label baseline --run 1
done
```

The full automated sweep across all combinations is what `run_experiments.sh` will
handle once it is built.

---

## Running the Tests

```bash
# Run all 28 tests with detailed output
uv run pytest tests/ -v

# Run just the TCP tests
uv run pytest tests/test_tcp_module.py -v

# Run just the UDP tests
uv run pytest tests/test_udp_module.py -v

# Run a single test by name
uv run pytest tests/test_tcp_module.py::test_run_tcp_experiment_with_flood -v
```

### What the tests cover

**TCP module — 14 tests (`tests/test_tcp_module.py`)**

| Test group | Tests | What is tested |
|------------|-------|----------------|
| `test_tcp_save_result_*` | 4 | File creation, header written once, row appending, column names |
| `test_recv_exact_*` | 3 | `_recv_exact()` — full message, fragmented chunks, raises on disconnect |
| `test_measure_throughput_*` | 2 | Positive throughput at 512B and 4096B payloads |
| `test_measure_latency_*` | 3 | Positive RTT, non-negative stdev, single-ping edge case |
| `test_run_tcp_experiment_basic` | 1 | End-to-end: both measurements, one CSV row, all fields present |
| `test_run_tcp_experiment_with_flood` | 1 | Experiment still returns valid metrics when background flood is active |

**UDP module — 14 tests (`tests/test_udp_module.py`)**

| Test group | Tests | What is tested |
|------------|-------|----------------|
| `test_jitter_*` | 6 | Edge cases (empty, 1, 2 packets), uniform streams, hand-calculated values, return type |
| `test_save_result_*` | 4 | File creation, header written once, row appending, column names |
| `test_sender_*` | 3 | `run_udp_sender()` — rejects payload < 16 bytes, rejects zero, accepts minimum |
| `test_experiment_basic_loopback` | 1 | End-to-end: throughput > 0, loss < 1%, OWD sane, CSV written |

---

## Project Structure

```
5470_Final_Project/
├── src/
│   ├── tcp_module.py        # TCP measurement — throughput + RTT latency (complete)
│   ├── udp_module.py        # UDP measurement — throughput, loss, jitter, OWD (complete)
│   ├── background_flood.py  # Background TCP flood for congestion testing (complete)
│   ├── emulate.sh           # dummynet setup/teardown (not yet built)
│   └── analyze.py           # data pipeline + plots (not yet built)
├── tests/
│   ├── __init__.py
│   ├── test_tcp_module.py   # 14 pytest tests for tcp_module.py
│   └── test_udp_module.py   # 14 pytest tests for udp_module.py
├── results/
│   ├── tcp_results.csv      # written by tcp_module.py
│   └── udp_results.csv      # written by udp_module.py
├── plots/                   # generated by analyze.py (empty until then)
├── docs/
│   ├── PROJECT_BREAKDOWN.md    # detailed implementation guide
│   ├── MEASUREMENT_GAPS.md     # gap analysis and implementation notes
│   ├── intermediate_report.md  # one-page progress report
│   └── Proposal.docx           # original project proposal
├── pyproject.toml           # Python project config and dependencies
└── run_experiments.sh       # full sweep orchestrator (not yet built)
```

### CSV schemas

**`results/tcp_results.csv`**
```
protocol, payload_bytes, buffer_bytes, condition, throughput_mbps, avg_latency_ms, latency_stdev_ms, run_index
```

**`results/udp_results.csv`**
```
protocol, payload_bytes, buffer_bytes, condition, throughput_mbps, loss_rate_pct, jitter_ms, avg_owd_ms, run_index
```

---

## Network Emulation

Real network conditions are emulated using **dummynet** (`dnctl` + `pfctl`), which is
built into macOS. All conditions are applied to the loopback interface (`lo0`).

### Why dummynet instead of tc/netem?

The original project proposal referenced `tc`/`netem` for network emulation, which is
the standard tool on Linux. This project is developed on **macOS**, where `tc` is not
available — macOS is BSD-based, not Linux. `dummynet` is the macOS/BSD equivalent and
ships with the OS, requiring no additional installs. It supports the same capabilities
needed here: propagation delay, bandwidth limits, queue depth (for bufferbloat), and
packet loss rate — all configurable per interface. The results are directly comparable
to what `tc`/`netem` would produce in a Linux environment.

> **Important:** `dummynet` commands require `sudo`.

### Apply a condition manually

```bash
# Syntax: delay <ms>, bandwidth <Mbit/s>, queue <slots>, packet loss rate
sudo dnctl pipe 1 config delay 20 bw 1Mbit/s queue 1000 plr 0.01
echo "dummynet out quick on lo0 all pipe 1" | sudo pfctl -f -
sudo pfctl -e
```

### Tear down (always run this when done)

```bash
sudo dnctl -q flush
sudo pfctl -f /etc/pf.conf
sudo pfctl -d
```

### Planned conditions

| Label | Delay | Bandwidth | Queue | Loss | Purpose |
|-------|-------|-----------|-------|------|---------|
| `baseline` | 0 ms | 100 Mbit/s | 32 slots | 0% | No emulation |
| `high_latency` | 50 ms | 100 Mbit/s | 32 slots | 0% | Propagation delay |
| `bufferbloat` | 0 ms | 1 Mbit/s | 1000 slots | 0% | Large queue + slow link |
| `lossy` | 0 ms | 100 Mbit/s | 32 slots | 5% | Random packet loss |
| `congested` | 20 ms | 2 Mbit/s | 500 slots | 1% | Combined stress |

These will be automated via `src/emulate.sh` once it is built.

---

## Files Still to Be Created

| File | Purpose |
|------|---------|
| `src/emulate.sh` | Shell script — applies and tears down dummynet conditions by name |
| `src/analyze.py` | Loads CSVs, aggregates 3 runs, generates 5 plots into `plots/` |
| `run_experiments.sh` | Loops over all payload × buffer × condition × run combinations |

See `docs/PROJECT_BREAKDOWN.md` for the detailed implementation spec for each.

---

## Ports Used

| Module | Mode | Port |
|--------|------|------|
| `tcp_module.py` | Throughput | 5201 |
| `tcp_module.py` | Latency (ping-pong) | 5202 |
| `udp_module.py` | Receiver | 5301 |
| `background_flood.py` | Flood sender + receiver | 5400 |

If a port is already in use (e.g. from a previous run that crashed), you can find
and kill the process holding it:

```bash
lsof -i :<port>          # find the PID using the port
kill <PID>
```

---

## Dependency Management

This project uses `uv`. Common commands:

```bash
uv sync                  # install all dependencies from pyproject.toml
uv add <package>         # add a runtime dependency
uv add --dev <package>   # add a dev-only dependency (e.g. a test library)
uv run <command>         # run any command inside the project venv
```
