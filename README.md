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
| `src/analyze.py` | Complete | Aggregates CSVs across 3 runs, generates 7 plots into `plots/` |

Data collection scripts live in `scripts/` — see [Data Collection Scripts](#data-collection-scripts).

---

## Architecture

### System pipeline

How the scripts connect from start to finish:

```mermaid
flowchart TD
    A["run_experiments.sh"] -->|1 - apply condition| B["emulate.sh<br/>dummynet / pfctl"]
    A -->|2 - run measurements| C["tcp_module.py"]
    A -->|2 - run measurements| D["udp_module.py"]
    A -->|3 - congested condition only| E["background_flood.py"]
    E -->|competing TCP traffic| C
    C -->|appends one row| F["results/tcp_results.csv"]
    D -->|appends one row| G["results/udp_results.csv"]
    F --> H["analyze.py"]
    G --> H
    H -->|generates| I["plots/"]
    B -->|tear down after each condition| A
```

### TCP module — what happens inside one run

Both throughput and latency modes run sequentially per invocation. Each produces metrics that are saved together in one CSV row.

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant S as Server Thread
    participant C as Client (main thread)

    Note over O,C: ── Throughput mode ──
    O->>S: start daemon thread
    S->>S: bind(:5201), listen()
    O->>O: sleep 0.05s
    O->>C: run_throughput_client()
    C->>S: connect()
    loop n_messages
        C->>S: sendall(payload bytes)
    end
    C->>S: close() sends FIN
    S->>S: recv() returns b"" — record elapsed_s + total_bytes
    O->>S: join(timeout=10s)
    O->>O: throughput = total_bytes × 8 / elapsed_s / 1e6

    Note over O,C: ── Latency mode (ping-pong) ──
    O->>S: start echo server thread on :5202
    O->>C: run_latency_client()
    loop n_pings
        C->>C: t0 = perf_counter()
        C->>S: [4-byte length prefix][payload]
        S->>C: echo body back
        C->>C: RTT = (perf_counter() - t0) × 1000 ms
    end
    O->>O: mean + stdev of RTT list → save CSV row
```

### UDP module — what happens inside one run

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant R as Receiver Thread
    participant S as Sender (main thread)

    O->>R: start daemon thread
    R->>R: bind(:5301), settimeout(2s)
    O->>O: sleep 0.1s
    O->>S: run_udp_sender()
    loop n_messages — paced at send_rate_pps
        S->>R: [seq_num 8B][send_ns 8B][zero padding]
        R->>R: record arrival_ns, unpack seq_num + send_ns
    end
    Note over R: 2 s silence → timeout → transfer done
    R->>R: compute loss_rate, jitter, avg_owd
    O->>R: join(timeout)
    O->>O: compute throughput → save CSV row
```

### Why background_flood.py exists

`dummynet`'s bandwidth cap alone is **not enough** to trigger TCP's congestion control (AIMD). Here is why, and what the flood fixes:

```mermaid
flowchart TD
    subgraph no_flood["Without flood"]
        D1[dummynet bandwidth cap] -->|single flow — queue never fills| T1[TCP experiment]
        T1 --> N1["AIMD never triggers\n(no loss events, no congestion window reduction)"]
    end

    subgraph with_flood["With --flood"]
        F["background_flood.py\nport 5400 — no rate limit"] -->|saturates queue| Q[shared loopback queue]
        T2["TCP experiment\nport 5201"] --> Q
        Q -->|overflow → packet drops| L[loss events fire on both flows]
        L -->|AIMD detects loss| R["congestion window cut in half\nthroughput drops, RTT rises\n← this is what we want to measure"]
    end
```

In short: the flood creates the **competition** that forces the queue to overflow, which produces the **loss events** that activate AIMD. Without it, TCP on loopback under a bandwidth cap just slows down gracefully with no observable congestion control behavior. UDP is unaffected by the flood (it has no congestion control), so the contrast between TCP backing off and UDP holding steady becomes clearly visible in the results.

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

Then restart your terminal so the `uv` command is on your `PATH`. You can verify
it worked by running:

```bash
uv --version
```

---

## Setup

All steps below are run **once**, from the project root directory.

### Step 1 — Clone the repo

```bash
git clone <repo-url>
cd 5470_Final_Project
```

### Step 2 — Create the virtual environment and install dependencies

`uv` reads `pyproject.toml` and handles everything automatically:

```bash
uv sync
```

This creates a `.venv/` folder inside the project and installs:
- `matplotlib` — plotting
- `numpy` — numerical helpers
- `pandas` — CSV aggregation
- `pytest` — test runner (dev dependency)

You do **not** need to run `pip install` or `python -m venv` manually. `uv sync`
does both in one step.

### Step 3 — Verify the setup

Run these two commands to confirm everything installed correctly:

```bash
uv run python --version
```

You should see `Python 3.14.x`. If you see an older version, make sure you are in
the project directory — `uv` uses the `.python-version` file to select the right
interpreter.

```bash
uv run pytest tests/ -v
```

You should see 28 tests collected and all passing. If any fail, check the
[Troubleshooting](#troubleshooting) section at the bottom.

---

## Quickstart — Your First Experiment

This walks through running one complete measurement from scratch to results. It
takes about 30 seconds.

### Step 1 — Run a UDP experiment

```bash
uv run python src/udp_module.py \
    --payload 1024 \
    --buffer 65536 \
    --messages 100 \
    --rate 200 \
    --label baseline \
    --run 1
```

You will see a line printed like:

```
UDP | baseline | payload=1024B | throughput=1.63 Mbps | loss=0.00% | jitter=0.21ms | owd=0.038ms
```

### Step 2 — Check what was saved

```bash
cat results/udp_results.csv
```

You should see a header row followed by one data row:

```
protocol,payload_bytes,buffer_bytes,condition,throughput_mbps,loss_rate_pct,jitter_ms,avg_owd_ms,run_index
UDP,1024,65536,baseline,1.6312,0.0,0.2134,0.0381,1
```

### Step 3 — Run a TCP experiment

```bash
uv run python src/tcp_module.py \
    --payload 1024 \
    --buffer 65536 \
    --messages 100 \
    --label baseline \
    --run 1
```

You will see:

```
TCP | baseline | payload=1024B | throughput=3241.57 Mbps | avg_rtt=0.018ms | rtt_stdev=0.004ms
```

### Step 4 — Check what was saved

```bash
cat results/tcp_results.csv
```

```
protocol,payload_bytes,buffer_bytes,condition,throughput_mbps,avg_latency_ms,latency_stdev_ms,run_index
TCP,1024,65536,baseline,3241.5734,0.018,0.004,1
```

You now have one row in each CSV. The full experiment sweep repeats this across
many payload sizes, buffer sizes, and conditions — that is what `run_experiments.sh`
will automate once it is built.

---

## Step-by-Step: UDP Module

### What it does

The UDP module sends a stream of datagrams at a controlled rate and measures what
arrived at the receiver. Because UDP has no delivery guarantee, every datagram is
stamped with a sequence number and a send timestamp so the receiver can detect
lost packets and measure timing.

### Running it

**Step 1 — Choose your parameters.**

| Flag | What to set | Example |
|------|-------------|---------|
| `--payload` | Size of each datagram in bytes. Must be at least 16 (header size). Try 64, 512, 1024, 4096. | `--payload 1024` |
| `--buffer` | Socket buffer size in bytes. Controls how much the OS can queue. Try 4096, 65536, 262144. | `--buffer 65536` |
| `--messages` | How many datagrams to send. 1000 is a good default. | `--messages 1000` |
| `--rate` | How many datagrams per second to send. 500 means one every 2 ms. | `--rate 500` |
| `--label` | A name for this condition. Written to the CSV so you can filter results later. | `--label baseline` |
| `--run` | Which repetition this is. Use 1, 2, or 3. Run three times to average out noise. | `--run 1` |

**Step 2 — Run the command.**

```bash
uv run python src/udp_module.py \
    --payload 1024 \
    --buffer 65536 \
    --messages 1000 \
    --rate 500 \
    --label baseline \
    --run 1
```

The experiment takes about `messages / rate` seconds to send (here: 1000 / 500 = 2 s)
plus a 2-second receiver timeout at the end. Total: ~4 seconds.

**Step 3 — Read the output.**

```
UDP | baseline | payload=1024B | throughput=4.09 Mbps | loss=0.00% | jitter=0.12ms | owd=0.031ms
```

| Field | What it means |
|-------|--------------|
| `throughput=4.09 Mbps` | The receiver absorbed 4.09 megabits per second. At 1024 bytes × 500 pps = 512,000 bytes/s = 4.096 Mbps, this is close to the theoretical max — good. |
| `loss=0.00%` | No datagrams were dropped. On loopback with rate pacing this should always be near zero. A non-zero value here would indicate the receive buffer was too small for the send rate. |
| `jitter=0.12ms` | The inter-arrival gaps varied by about 0.12 ms on average. Very low on loopback. This number grows when a slow link or large queue causes irregular delivery. |
| `owd=0.031ms` | Each datagram took about 0.031 ms to travel from the sender to the receiver. On loopback this is essentially OS scheduling overhead. Under dummynet delay, this will rise to match the configured delay. |

**Step 4 — Repeat for three runs.**

```bash
uv run python src/udp_module.py --payload 1024 --buffer 65536 --messages 1000 --rate 500 --label baseline --run 2
uv run python src/udp_module.py --payload 1024 --buffer 65536 --messages 1000 --rate 500 --label baseline --run 3
```

Each run appends one more row. `analyze.py` will average across the three runs
when it is built.

**Step 5 — Inspect the CSV.**

```bash
cat results/udp_results.csv
```

---

## Step-by-Step: TCP Module

### What it does

The TCP module runs two measurements in sequence for every invocation:

1. **Throughput mode** — sends `--messages` × `--payload` bytes as fast as possible
   and measures how many megabits per second arrived at the server.
2. **Latency mode** — sends one message, waits for the echo, records the RTT, and
   repeats `--messages` times. Returns the mean and standard deviation of all RTTs.

Both results are saved in **one CSV row**.

### Running it

**Step 1 — Choose your parameters.**

| Flag | What to set | Example |
|------|-------------|---------|
| `--payload` | Message size in bytes. No minimum (unlike UDP). Try 64, 512, 1024, 4096. | `--payload 1024` |
| `--buffer` | Socket buffer size in bytes. | `--buffer 65536` |
| `--messages` | Number of messages. Used for both throughput (bulk count) and latency (ping count). | `--messages 1000` |
| `--label` | Condition name written to the CSV. | `--label baseline` |
| `--run` | Repetition index 1–3. | `--run 1` |
| `--flood` | Optional flag. Starts competing background traffic before the experiment. Omit for a clean baseline; include to observe congestion. | `--flood` |

**Step 2 — Run a clean baseline.**

```bash
uv run python src/tcp_module.py \
    --payload 1024 \
    --buffer 65536 \
    --messages 1000 \
    --label baseline \
    --run 1
```

The throughput phase sends 1000 × 1024 bytes = ~1 MB as fast as TCP will go.
The latency phase sends 1000 ping-pong messages. Both run in under a second on
loopback. Total experiment time: 1–3 seconds.

**Step 3 — Read the output.**

```
TCP | baseline | payload=1024B | throughput=3241.57 Mbps | avg_rtt=0.018ms | rtt_stdev=0.004ms
```

| Field | What it means |
|-------|--------------|
| `throughput=3241.57 Mbps` | TCP on loopback with no emulation is extremely fast — the OS never actually puts bytes on a NIC. This number drops significantly under dummynet bandwidth caps or with the flood active. |
| `avg_rtt=0.018ms` | The average round-trip time for a ping-pong message. On loopback this is essentially OS thread scheduling time. Under dummynet delay of 20 ms, expect this to rise to ~40 ms (delay is one-way, RTT doubles it). |
| `rtt_stdev=0.004ms` | How much the RTT varied between pings. Low stdev means consistent delivery. Under bufferbloat, stdev rises because some pings hit a full queue and wait longer. |

**Step 4 — Inspect the CSV.**

```bash
cat results/tcp_results.csv
```

---

## Step-by-Step: Congestion Comparison with `--flood`

This walkthrough shows how to directly observe TCP's AIMD congestion control by
comparing the same experiment with and without competing background traffic.

### What is happening

When `--flood` is passed, `background_flood.py` starts a TCP flood on port 5400
that hammers the loopback interface with continuous traffic. The experiment's TCP
flow now has to share the same queue. When the queue overflows, both flows lose
packets. TCP detects the loss and cuts its congestion window in half (the
"multiplicative decrease" in AIMD), reducing throughput and increasing latency.

### Step 1 — Run the baseline (no flood)

```bash
uv run python src/tcp_module.py \
    --payload 1024 \
    --buffer 65536 \
    --messages 1000 \
    --label baseline \
    --run 1
```

Note the throughput and avg_rtt values from the output.

### Step 2 — Run the congested version (with flood)

```bash
uv run python src/tcp_module.py \
    --payload 1024 \
    --buffer 65536 \
    --messages 1000 \
    --label congested \
    --run 1 \
    --flood
```

You will see an extra line printed before the results:

```
Background flood started on port 5400 — competing for loopback bandwidth
TCP | congested [flood active] | payload=1024B | throughput=... Mbps | avg_rtt=...ms | rtt_stdev=...ms
```

### Step 3 — Compare the two rows in the CSV

```bash
cat results/tcp_results.csv
```

You should see two rows — one labeled `baseline`, one labeled `congested`. The
congested row should have lower `throughput_mbps` and higher `avg_latency_ms` than
the baseline row. The magnitude depends on your machine's load, but the direction
should be consistent.

### Step 4 — Compare TCP vs UDP under congestion

UDP has no congestion control — it does not back off when it detects loss. Run
the same payload on the UDP module (no `--flood` flag, since UDP doesn't have one)
and observe that its throughput stays near the rate-limited maximum regardless of
what TCP is doing:

```bash
uv run python src/udp_module.py \
    --payload 1024 \
    --buffer 65536 \
    --messages 1000 \
    --rate 500 \
    --label baseline \
    --run 1
```

This contrast — TCP backs off, UDP does not — is one of the core findings the
project is designed to show.

---

## Step-by-Step: Background Flood Standalone

`background_flood.py` can also be used as a standalone process directly from the
terminal, independent of the TCP module. This is useful if you want to run the
flood in one terminal window while manually running experiments in another.

### Step 1 — Start the flood in a background terminal

Open a terminal window and run:

```bash
uv run python src/background_flood.py
```

You will see:

```
Starting background TCP flood on 127.0.0.1:5400 — press Ctrl+C to stop
```

The flood is now running. Leave this terminal open.

### Step 2 — Run experiments in a second terminal

In a new terminal window, run any experiment normally (without `--flood`):

```bash
uv run python src/tcp_module.py \
    --payload 1024 \
    --buffer 65536 \
    --messages 1000 \
    --label congested_manual \
    --run 1
```

The flood is active in the background and competing for loopback bandwidth.

### Step 3 — Stop the flood

Go back to the first terminal and press `Ctrl+C`:

```
^CStopping flood...
```

### Using it from a shell script

If you want to automate this in a bash script:

```bash
# Start flood and capture its PID
uv run python src/background_flood.py &
FLOOD_PID=$!

# Wait a moment for the flood to reach full speed
sleep 1

# Run your experiment
uv run python src/tcp_module.py --payload 1024 --buffer 65536 --label congested --run 1

# Stop the flood
kill $FLOOD_PID
```

---

## Step-by-Step: Running the Tests

The test suite verifies that every function in the codebase works correctly before
you run real experiments. Always run the tests after pulling new changes or making
edits to the source files.

### Step 1 — Run all tests

```bash
uv run pytest tests/ -v
```

The `-v` flag (verbose) prints each test name and its result instead of just dots.
You should see 28 tests all marked `PASSED`:

```
tests/test_tcp_module.py::test_tcp_save_result_creates_file PASSED
tests/test_tcp_module.py::test_recv_exact_fragmented_message PASSED
...
tests/test_udp_module.py::test_experiment_basic_loopback PASSED

28 passed in 3.65s
```

### Step 2 — Understand what each group tests

**TCP tests (`tests/test_tcp_module.py`)**

| Group | What it is checking |
|-------|---------------------|
| `test_tcp_save_result_*` | The CSV writer works correctly — file gets created, header appears once, rows append in order, all columns are present |
| `test_recv_exact_*` | The `_recv_exact()` helper correctly reassembles TCP data that arrives in fragments. One test sends 1000 bytes in 10-byte chunks and checks that all 1000 bytes are reassembled before returning. Another checks that it raises `ConnectionError` if the connection closes early. |
| `test_measure_throughput_*` | A real throughput experiment runs on loopback and returns a positive number |
| `test_measure_latency_*` | A real ping-pong experiment runs on loopback and returns a positive RTT. Also checks that `stdev` is 0.0 when only one ping is sent (you can't compute standard deviation from one sample). |
| `test_run_tcp_experiment_basic` | Runs the full experiment end-to-end and checks that one CSV row was written with the correct values |
| `test_run_tcp_experiment_with_flood` | Runs the full experiment while the background flood is active and verifies it still returns valid results |

**UDP tests (`tests/test_udp_module.py`)**

| Group | What it is checking |
|-------|---------------------|
| `test_jitter_*` | The `compute_jitter()` function handles edge cases correctly: empty list → 0.0, one packet → 0.0, two packets → 0.0, perfectly uniform arrivals → 0.0, alternating gaps → hand-calculated expected value |
| `test_save_result_*` | Same CSV checks as TCP |
| `test_sender_*` | The sender rejects bad input before opening any socket. Payloads smaller than 16 bytes (the header size) should raise `ValueError` immediately. |
| `test_experiment_basic_loopback` | Full end-to-end experiment on loopback — throughput > 0, loss < 1%, OWD between 0 and 100 ms, CSV row written |

### Step 3 — Run a specific test file

If you only changed UDP code, you do not need to run the TCP tests:

```bash
uv run pytest tests/test_udp_module.py -v
```

### Step 4 — Run a single test

If you want to check just one specific thing:

```bash
uv run pytest tests/test_tcp_module.py::test_recv_exact_fragmented_message -v
```

### Step 5 — Interpreting a failure

If a test fails, pytest prints the exact assertion that failed and the values
on both sides. For example:

```
FAILED tests/test_udp_module.py::test_experiment_basic_loopback
AssertionError: Expected < 1% loss on loopback, got 3.00%
```

This tells you: the integration test sent real datagrams and 3% were lost. Likely
cause — port 5301 was already in use from a previous crashed run. Fix:

```bash
lsof -i :5301        # find the PID holding the port
kill <PID>           # release it
uv run pytest tests/test_udp_module.py::test_experiment_basic_loopback -v
```

---

## Reading Your Results

After running experiments, results are stored in two CSV files. Here is how to
read them and what the values mean.

### UDP results — `results/udp_results.csv`

```
protocol,payload_bytes,buffer_bytes,condition,throughput_mbps,loss_rate_pct,jitter_ms,avg_owd_ms,run_index
```

| Column | What it means | Typical baseline value | What causes it to change |
|--------|--------------|----------------------|--------------------------|
| `protocol` | Always `UDP` | `UDP` | — |
| `payload_bytes` | Datagram size set by `--payload` | varies | Larger payloads → higher throughput up to the MTU limit (~16 KB on loopback) |
| `buffer_bytes` | Socket buffer size set by `--buffer` | varies | Larger buffers → lower loss under burst traffic |
| `condition` | Label set by `--label` | `baseline` | Changes with each dummynet condition |
| `throughput_mbps` | Megabits per second delivered to the receiver | ~4 Mbps at 500 pps × 1024 B | Drops under lossy or congested conditions |
| `loss_rate_pct` | Percentage of datagrams that never arrived | `0.00` on loopback | Rises under `lossy` (5%) and `congested` (1%) conditions; rises if buffer is too small |
| `jitter_ms` | How irregular the inter-arrival gaps were | < 1 ms on loopback | Rises under `bufferbloat` (queue delays cause irregular delivery) and `high_latency` |
| `avg_owd_ms` | Average one-way delay from sender to receiver | < 0.1 ms on loopback | Rises to match dummynet delay setting; rises sharply under `bufferbloat` |
| `run_index` | Which repetition (1, 2, or 3) | 1, 2, or 3 | — |

### TCP results — `results/tcp_results.csv`

```
protocol,payload_bytes,buffer_bytes,condition,throughput_mbps,avg_latency_ms,latency_stdev_ms,run_index
```

| Column | What it means | Typical baseline value | What causes it to change |
|--------|--------------|----------------------|--------------------------|
| `protocol` | Always `TCP` | `TCP` | — |
| `payload_bytes` | Message size set by `--payload` | varies | Larger payloads → fewer messages to achieve the same bytes → lower per-message overhead |
| `buffer_bytes` | Socket buffer size set by `--buffer` | varies | Larger buffers → TCP can keep more data in flight → higher throughput |
| `condition` | Label set by `--label` | `baseline` | — |
| `throughput_mbps` | Megabits per second received by the server | ~3000+ Mbps on loopback | Drops under dummynet bandwidth cap, drops sharply with `--flood` active |
| `avg_latency_ms` | Mean RTT across all ping-pong messages | < 0.1 ms on loopback | Rises with dummynet delay (RTT ≈ 2 × one-way delay); rises under `bufferbloat` as queue grows |
| `latency_stdev_ms` | Standard deviation of RTT samples | < 0.01 ms on loopback | Rises when delivery is inconsistent — `bufferbloat` is the main driver |
| `run_index` | Which repetition (1, 2, or 3) | 1, 2, or 3 | — |

### What good vs. concerning values look like

| Situation | What you see | What it means |
|-----------|-------------|---------------|
| Clean loopback baseline | Loss = 0%, OWD < 0.1 ms, RTT < 0.1 ms | Normal — no emulation applied |
| `high_latency` condition active | OWD ≈ 50 ms, RTT ≈ 100 ms, throughput similar to baseline | dummynet delay is working |
| `bufferbloat` condition active | OWD rising, RTT rising, loss near 0% | Queue is filling up — exactly the bufferbloat signature |
| `lossy` condition active | Loss ≈ 5%, throughput lower than baseline | dummynet packet loss is working |
| `--flood` active (TCP) | Throughput much lower, RTT higher | AIMD congestion control is backing off |
| Unexpected high loss on loopback | Loss > 1% without any emulation | Port conflict or previous run left a socket open |

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
│   ├── intermediate_report.md  # progress report
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

Real network conditions are emulated using **dummynet** (`dnctl` + `pfctl`), built
into macOS. All conditions are applied to the loopback interface (`lo0`).

### Why dummynet instead of tc/netem?

The original project proposal referenced `tc`/`netem` for network emulation — the
standard tool on Linux. This project runs on **macOS**, where `tc` is not available.
`dummynet` is the macOS/BSD equivalent and ships with the OS. It supports the same
capabilities: propagation delay, bandwidth limits, queue depth (for bufferbloat),
and packet loss rate.

> **Important:** `dummynet` commands require `sudo`.

### Step-by-step: apply a condition manually

**Step 1 — Configure the pipe.**

A "pipe" in dummynet is a virtual link with configurable properties. This command
creates pipe 1 with 20 ms delay, 1 Mbit/s bandwidth, a 100-slot queue, and 1% loss:

```bash
sudo dnctl pipe 1 config delay 20 bw 1Mbit/s queue 100 plr 0.01
```

**Step 2 — Route loopback traffic through the pipe.**

```bash
echo "dummynet out quick on lo0 all pipe 1" | sudo pfctl -f -
sudo pfctl -e
```

**Step 3 — Run your experiment.**

```bash
uv run python src/udp_module.py --payload 1024 --buffer 65536 --messages 1000 --rate 500 --label congested --run 1
```

You should now see OWD ≈ 20 ms in the output.

**Step 4 — Always tear down when done.**

Leaving dummynet active will slow down all loopback traffic on your machine,
including the test suite:

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
| `bufferbloat` | 0 ms | 1 Mbit/s | 100 slots | 0% | Large queue + slow link |
| `lossy` | 0 ms | 100 Mbit/s | 32 slots | 5% | Random packet loss |
| `congested` | 20 ms | 2 Mbit/s | 500 slots | 1% | Combined stress |

These will be automated via `src/emulate.sh` once it is built.

---

## Data Collection Scripts

All scripts live in `scripts/` and must be run in order. Each one appends rows
to the existing CSVs — none overwrite previous results. See
`docs/data_collection_guide.md` for a full explanation of why each step exists.

### `scripts/01_smoke_test.sh` — verify the environment works

Runs one UDP experiment and one TCP experiment at 100 messages each. Checks that
both CSV files are created and prints a pass/fail result. Finishes in under 10
seconds. **Always run this first** — if it fails, something is wrong with your
environment and you will catch it before wasting 30+ minutes on a full sweep.

```bash
bash scripts/01_smoke_test.sh
```

### `scripts/02_baseline_sweep.sh` — control group data, no emulation

Sweeps payload sizes across both protocols, three runs each. TCP covers six sizes
(64B → 65536B); UDP covers five sizes (64B → 16384B) — 65536B is excluded because
it exceeds the UDP datagram limit of 65507 bytes. No dummynet required. This is
your **control group** — every result from Scripts 3 and 4 is compared against
these numbers. Produces 33 rows (18 TCP + 15 UDP).

```bash
bash scripts/02_baseline_sweep.sh
```

### `scripts/03_congested_sweep.sh` — TCP congestion control under competing traffic

Same payload sweep as Script 2 but with `background_flood.py` active during TCP
experiments. The flood saturates the loopback queue, causing packet drops that
trigger TCP's AIMD congestion control — you see throughput drop and latency rise.
UDP runs without the flood (it has no congestion control) so the two protocols
can be compared side-by-side in the same condition. No dummynet required.
Produces 30 rows.

```bash
bash scripts/03_congested_sweep.sh
```

### `scripts/04_emulated_conditions.sh` — dummynet network conditions

Applies three dummynet conditions to loopback in sequence, running the payload
sweep under each. Tears down dummynet between conditions. **Requires sudo.**

| Condition | What it simulates | Key signal |
|-----------|------------------|------------|
| `high_latency` | 50ms propagation delay | OWD and RTT rise to ~50ms / ~100ms |
| `bufferbloat` | 1Mbit/s link + 100-slot queue | Latency spikes while loss stays near 0% |
| `lossy` | 5% random packet loss | UDP shows 5% loss; TCP hides it via retransmit but throughput drops |

Produces 90 rows.

```bash
bash scripts/04_emulated_conditions.sh
```

> If the script crashes mid-run, reset dummynet manually:
> ```bash
> sudo dnctl -q flush && sudo pfctl -f /etc/pf.conf && sudo pfctl -d
> ```

### `scripts/05_buffer_sweep.sh` — socket buffer size sweep

**Why this script was added:** Scripts 02–04 always held `buffer_bytes` fixed at
65536. The project's stated research question is *"how do MTU/payload size and
**socket buffer settings** affect performance?"* — but buffer settings were never
varied. This script answers that half of the question.

It holds payload fixed at 1024B and varies `SO_SNDBUF` / `SO_RCVBUF` across five
sizes: **4KB, 16KB, 64KB, 128KB, 256KB**. Two conditions are run: baseline (no
dummynet) and bufferbloat (1Mbit/s, 100-slot queue via dummynet). Both protocols,
3 runs each. Produces 60 rows.

**What you should see:**

- Small buffers (4KB): TCP throughput drops because the kernel can only queue a
  few bytes at a time before stalling the sender. UDP may show loss if the receive
  buffer fills before the application can drain it.
- Large buffers (128KB+): throughput plateaus — once the buffer is large enough to
  keep the pipeline full, adding more makes no difference.
- Under bufferbloat: UDP one-way delay (OWD) rises with buffer size even though no
  loss occurs — the socket buffer acts as an extra queue before the 1Mbit/s
  bottleneck. This is **socket-layer bufferbloat**: separate from the dummynet
  queue effect, and only visible when you vary SO_RCVBUF.

**Requires sudo** (bufferbloat phase uses dummynet).

```bash
bash scripts/05_buffer_sweep.sh
```

**Runtime:** ~20 minutes.

> **Bug fixed (2026-05-03):** The original version of this script used a single
> `RATE=500` pps for UDP in both conditions. Under bufferbloat (1Mbit/s cap),
> 500 pps × 1024B = 4.1 Mbps send rate — 4× the cap — producing ~50% UDP loss
> instead of the intended ~8%. Script 04 (`04_emulated_conditions.sh`) uses
> 200 pps for bufferbloat, which sits just above the cap and produces the
> intended bufferbloat signature. The script now uses `RATE_BASELINE=500` and
> `RATE_BUFFERBLOAT=200` so both datasets are directly comparable. The 15
> contaminating rows (buffer-sweep bufferbloat UDP, loss > 20%) were removed
> from `results/udp_results.csv` before regenerating plots.

### Total data produced

| Script | Rows added | Requires sudo |
|--------|-----------|---------------|
| `01_smoke_test.sh` | 2 | No |
| `02_baseline_sweep.sh` | 33 | No |
| `03_congested_sweep.sh` | 30 | No |
| `04_emulated_conditions.sh` | 90 | Yes |
| `05_buffer_sweep.sh` | 60 | Yes (bufferbloat phase) |
| **Total** | **215** | |

---

## Step-by-Step: Buffer Size Sweep

### What it measures

This sweep answers a different question than the payload sweep. Instead of
varying message size, it varies how much kernel memory is reserved for the
send and receive socket queues (`SO_SNDBUF` and `SO_RCVBUF`).

| Buffer size | What happens |
|-------------|-------------|
| 4096 (4KB) | Very small — kernel can queue only ~4 packets at a time; sender stalls frequently |
| 16384 (16KB) | Small — noticeable throughput reduction vs default |
| 65536 (64KB) | **Default used in all other scripts** — the baseline reference point |
| 131072 (128KB) | Large — throughput typically plateaus here |
| 262144 (256KB) | Very large — beyond the plateau; OWD increases under bufferbloat |

### How to run

1. Confirm the first four scripts have already been run (the buffer sweep is additive — it appends rows).
2. Confirm dummynet is not already active:
   ```bash
   sudo dnctl show   # must return nothing
   ```
3. Run the script:
   ```bash
   bash scripts/05_buffer_sweep.sh
   ```
4. After it finishes, confirm the buffer sizes appear in the CSV:
   ```bash
   awk -F',' 'NR>1 && $4=="baseline" {print $3}' results/tcp_results.csv | sort -n | uniq -c
   # Should show all five buffer sizes
   ```
5. Regenerate all plots (analyze.py now generates Plots 6 and 7 from this data):
   ```bash
   uv run python src/analyze.py
   ```

### New plots generated

**Plot 6 — Throughput vs Buffer Size** (`plots/06_throughput_vs_buffer.png`)

Both TCP and UDP throughput at baseline, plotted against socket buffer size.
Shows the floor (small buffers cause throughput collapse) and the plateau
(large buffers stop helping).

**Plot 7 — UDP One-Way Delay vs Buffer Size** (`plots/07_owd_vs_buffer.png`)

UDP OWD under both baseline and bufferbloat conditions, plotted against
SO_RCVBUF. Shows socket-layer bufferbloat: under the 1Mbit/s bufferbloat
condition, each additional KB of receive buffer adds directly to queuing
delay — even before the dummynet queue itself fills up.

---

## Step-by-Step: Analyzing the Data with `analyze.py`

### What it does

`src/analyze.py` is the data pipeline and visualization module. It loads both CSV
files, aggregates the three runs per condition into a mean ± standard deviation,
and generates seven plots into `plots/`. Plots 1–5 cover the payload size sweep
across all five network conditions. Plots 6–7 cover the socket buffer size sweep.
Each plot isolates one aspect of the comparison between TCP and UDP behavior under
different network conditions.

### What we are analyzing

The core research question is: **how does payload size affect TCP and UDP
performance, and how does each protocol respond to network stress?**

To answer this, the data was collected under five conditions:

| Condition | What it isolates |
|-----------|-----------------|
| `baseline` | Raw loopback performance — no emulation, no competing traffic |
| `congested` | TCP's AIMD congestion control — competing flood traffic forces queue overflow and loss events |
| `high_latency` | Propagation delay — 50ms one-way delay added via dummynet |
| `bufferbloat` | Queuing delay — 1 Mbit/s cap + 100-slot queue fills under load, latency spikes while loss stays near zero |
| `lossy` | Packet loss — 5% random drop rate applied by dummynet |

### The seven plots

**Plot 1 — Throughput vs Payload Size (baseline)**

Shows how raw throughput scales with payload size for both protocols on a clean
loopback. TCP throughput rises steeply — from ~82 Mbps at 64B to ~9,300 Mbps at
64KB — because larger payloads amortize per-message overhead and TCP's streaming
model keeps the pipe continuously filled. UDP throughput rises linearly with
payload because it is rate-limited at 500 pps (throughput = rate × payload_size).
A vertical dashed line marks the loopback MTU at 16,384 bytes — the fragmentation
threshold where IP must split datagrams into multiple packets.

**Plot 2 — TCP Latency vs Payload Size (all conditions)**

Shows mean RTT across all five conditions on a single chart. Baseline and congested
cluster near zero (<0.1ms). High latency shows a stable ~103ms (exactly 2 × 50ms
one-way delay — confirms dummynet accuracy). Bufferbloat is the critical result:
RTT stays low at small payloads but explodes to ~1,060ms at 16KB with ±700ms
standard deviation — classic queuing-induced latency. Lossy shows ~11–15ms mean
RTT with very large error bars (17–64ms stdev) driven by unpredictable retransmit
timing.

**Plot 3 — UDP Packet Loss vs Payload Size (lossy + congested)**

Shows UDP loss rate under the two conditions where it is non-trivial. Lossy holds
near 5% across all payload sizes (dummynet applies loss per-packet, independent of
datagram size). Congested shows 0% UDP loss — the background TCP flood does not
overwhelm the loopback queue at 200 pps. TCP is absent from this chart because its
reliability layer retransmits lost segments invisibly — loss appears in RTT
inflation (Plot 2) rather than as counted drops.

**Plot 4 — UDP Jitter vs Payload Size (all conditions)**

Shows inter-arrival delay variability. Baseline and congested are near zero (~0.1ms).
High latency produces ~1.1ms flat jitter — the constant 50ms shift preserves sender
pacing. Bufferbloat produces an unexpected *downward* trend: high jitter (~1.2ms) at
small payloads, dropping to near zero at 16KB — at full link saturation the queue
itself becomes the pacer, regularizing arrivals. Lossy jitter rises with payload
because each dropped datagram leaves a gap proportional to its size.

**Plot 5 — TCP vs UDP Throughput Under Congestion**

A grouped bar chart comparing throughput under the congested condition at each
payload size. TCP fills the bar: ~100 Mbps at 64B rising to ~11,600 Mbps at 16KB.
UDP is barely visible at the base. On loopback, AIMD backoff events complete in
microseconds (RTT ~0.04ms), so TCP recovers almost immediately and averages near
full speed. The structural point remains valid — TCP continuously probes and fills
available bandwidth while UDP only sends at the application-configured rate.

**Plot 6 — Throughput vs Buffer Size (baseline, payload=1024B)**

Shows TCP and UDP throughput as SO_SNDBUF/SO_RCVBUF varies from 4KB to 256KB,
with payload fixed at 1024B on clean loopback. TCP shows a relatively flat profile
(1,063–1,335 Mbps) with wide error bars. The expected dramatic collapse at 4KB does
not appear because macOS silently doubles socket buffer requests and zero-RTT
loopback means even a small buffer keeps the pipeline full. UDP is perfectly flat
at ~3.2 Mbps — the rate-pacing loop (500 pps) controls throughput, not the socket
buffer.

**Plot 7 — UDP One-Way Delay vs Buffer Size (payload=1024B)**

Shows UDP OWD under baseline and bufferbloat conditions as SO_RCVBUF varies.
Baseline is flat at ~0.09ms. Bufferbloat is flat at ~516ms. The hypothesized
"OWD grows with buffer size" effect does not appear because the 1Mbit/s dummynet
pipe is the bottleneck — packets queue in the dummynet scheduler, not the socket
buffer. Only when SO_RCVBUF is small enough to hold less data than the link's
in-flight window would socket-layer bufferbloat become visible, and macOS's
buffer-doubling prevents that threshold from being reached here.

### How to run it

```bash
uv run python src/analyze.py
```

The script prints a summary of the loaded data before generating plots:

```
TCP rows: 170  conditions: baseline, bufferbloat, congested, high_latency, lossy
UDP rows: 156  conditions: baseline, bufferbloat, congested, high_latency, lossy
Generating plots...
  [1/5] plots/1_throughput_vs_payload.png
  [2/5] plots/2_tcp_latency_vs_payload.png
  [3/5] plots/3_udp_loss_vs_payload.png
  [4/5] plots/4_udp_jitter_vs_payload.png
  [5/5] plots/5_congestion_comparison.png
Done.
```

Plots are saved to `plots/` as PNG files. Open them in any image viewer. Each
plot uses error bars (±1 standard deviation) across the three runs per condition
to show measurement variability.

### If a plot is skipped

`analyze.py` skips any plot where the required data is missing and prints a
warning instead of crashing. If you see `[SKIP]` for a plot, check that the
relevant condition exists in the CSV with:

```bash
awk -F',' 'NR>1 {print $4}' results/tcp_results.csv | sort | uniq -c
awk -F',' 'NR>1 {print $4}' results/udp_results.csv | sort | uniq -c
```

All five conditions must be present for all five plots to generate.

---

## Results and Analysis

The plots below were generated from the full data collection sweep across all five
conditions. Each section explains what the graph shows, why the result looks the
way it does at a mechanism level, and what it means in practice.

---

### Plot 1 — Throughput vs Payload Size (Baseline)

![Throughput vs Payload Size](plots/01_throughput_vs_payload.png)

**What it shows:** Raw throughput for both protocols on clean loopback with no
emulation and no competing traffic, as payload size grows from 64B to 64KB.

**Results:**
- TCP: ~85 Mbps at 64B → ~9,300 Mbps at 64KB
- UDP: ~0.2 Mbps at 64B → ~51 Mbps at 16KB (flat, linear)
- TCP achieves roughly **180× higher throughput** than UDP at 16KB

**Why TCP scales so sharply with payload size:**

Every TCP message requires the OS to go through the same sequence of work
regardless of how many bytes are in it: a `send()` system call, a kernel context
switch, ACK processing, a `recv()` system call on the other side. At 64B, this
overhead consumes the vast majority of the time — you are paying full cost for
64 bytes of data. As the payload grows, the same fixed overhead is divided across
more and more bytes, making each byte progressively cheaper. This is called
**amortization of per-message overhead**. At 64KB, the overhead is almost
invisible relative to the data transferred, and TCP's streaming model keeps the
loopback pipe continuously filled.

TCP also benefits from its own flow and congestion control — it probes for
available bandwidth and dynamically increases how much data it keeps in flight
(the congestion window). On loopback with essentially unlimited bandwidth, this
means TCP rapidly ramps up to fill the pipe completely.

**Why UDP stays flat:**

UDP was rate-limited to 500 packets per second in the baseline sweep. This is an
application-level cap — the sender explicitly sleeps between sends. The result is
that UDP throughput is simply `rate × payload_size × 8 bits`. At 500 pps and
16KB, that is 500 × 16,384 × 8 = 65.5 Mbps theoretical maximum. The measured
~51 Mbps is below theoretical because `time.sleep()` on macOS is not
sub-millisecond accurate — the actual inter-send interval is slightly longer than
the target. UDP has no mechanism to self-adjust its send rate based on available
bandwidth the way TCP does.

**Why the error bars widen at 16KB:**

The vertical dashed line marks the macOS loopback MTU of 16,384 bytes. At or
above this threshold, the IP layer must **fragment** the datagram — split it into
multiple smaller IP packets, send them independently, and reassemble them at the
destination. This adds non-deterministic OS scheduling work to each transfer,
increasing run-to-run variability. The wider error bars at 16KB reflect this: some
runs complete reassembly quickly, others get delayed by the OS scheduler.

**Conclusion:** TCP is the right choice when raw throughput is the priority. UDP's
flat line is not a weakness — it reflects a deliberate design choice to put rate
control in the application's hands. The 180× gap exists because TCP is
continuously probing and filling available bandwidth, while UDP only sends as fast
as the application tells it to.

---

### Plot 2 — TCP Latency (RTT) vs Payload Size — All Conditions

![TCP Latency vs Payload Size](plots/02_tcp_latency_vs_payload.png)

**What it shows:** Mean round-trip time for TCP ping-pong messages (one message
sent, echo received = one RTT sample) across all five conditions.

**Results:**
- Baseline: ~0.07ms, flat across all payload sizes
- Congested: ~0.04ms, flat — paradoxically *lower* than baseline (see below)
- High latency: ~103ms, flat — exactly 2 × 50ms one-way delay, payload-independent
- Bufferbloat: low until 4KB, then spikes to ~1,060ms at 16KB with ±740ms stdev
- Lossy: ~11–15ms mean RTT, flat across payload sizes, but stdev of 17–64ms from retransmit timing

**Why baseline RTT is near-zero:**

On loopback, data never leaves the machine. The OS copies bytes from the sender's
buffer to the receiver's buffer entirely in memory, without involving a NIC,
network cable, or switch. The only latency is OS scheduling delay (thread context
switches). Sub-millisecond RTT on loopback is expected and correct.

**Why congested RTT is lower than baseline — a notable anomaly:**

The congested condition shows ~0.04ms RTT, which is *lower* than baseline's
~0.07ms. AIMD theory predicts congestion should increase RTT, not decrease it.
The most likely explanation is a **kernel warm-path effect**: the background flood
fills the OS send queue and keeps the TCP fast path active. On an otherwise idle
loopback, each ping-pong experiment must wake the kernel from a cold scheduling
state. With the flood running, the kernel's TCP path is already warm — context
switches are faster and memory caches are hot. This effect is loopback-specific;
on a real network with non-trivial RTT, AIMD backoff would dominate and congested
RTT would be clearly higher than baseline.

**Why high latency is flat at 103ms regardless of payload size:**

dummynet adds a fixed **50ms delay per packet** on the outgoing side of the
loopback interface. The key word is *per packet*, not per byte. Whether the packet
carries 64B or 16KB, it sits in the delay queue for exactly 50ms before being
released. The RTT is therefore 2 × 50ms = 100ms, plus the sub-millisecond
loopback overhead, giving ~103ms at all payload sizes. This confirms the delay
emulation is working correctly and is payload-independent.

**Why bufferbloat causes such dramatic RTT inflation at large payloads:**

This is the most important result in the experiment. The bufferbloat condition
sets the link to 1 Mbit/s with a 100-slot queue. At small payloads (64B–1KB),
the bandwidth required to carry each TCP ping-pong message is tiny — 64B × 8 /
1,000,000 = 0.5ms transmission time. The queue never fills, so RTT stays near
the baseline.

At 16KB payloads, each ping-pong message is 16,384 bytes = 131,072 bits. At
1 Mbit/s, transmitting one message takes **131ms**. Even a few messages queued
ahead of yours means hundreds of milliseconds of wait time before your packet
reaches the front of the line. The queue fills, packets pile up, and RTT explodes
to over 1,000ms.

This is the textbook definition of **bufferbloat**: a large queue at a slow link
causes packets to queue for so long that interactive latency becomes unusable —
even though no packets are being dropped. The ±740ms error bar at 16KB is itself
a finding: one run measured 20ms RTT, another measured 1,604ms. The queue was
oscillating between full and draining between runs — sometimes the test caught the
queue mid-fill, sometimes just after it drained — producing wildly different
latencies. This nondeterminism is exactly what makes bufferbloat so damaging in
practice: users experience good and terrible latency unpredictably on the same
connection.

**Why lossy RTT shows high variance rather than a trend:**

Under 5% random loss, TCP retransmits lost segments. The mean RTT stays in the
11–15ms range across all payload sizes, but the standard deviation is enormous —
17ms at 64B, up to 64ms at 16KB. The mean is misleading: it reflects the majority
of packets that arrive without retransmission. The high stdev reflects the
bimodal distribution underneath — most packets arrive fast (sub-millisecond),
while the minority that trigger retransmissions take tens to hundreds of
milliseconds depending on when the timeout fires. Payload size does not shift
the mean significantly but does widen the tail of the distribution, because larger
segments carry more data that must wait longer if they are retransmitted.

**Conclusion:** Queuing delay (bufferbloat) is a fundamentally different and more
damaging form of latency than propagation delay. High latency gives you a
consistent, predictable 103ms — applications can adapt to that with buffering.
Bufferbloat gives you anywhere from 20ms to 1,600ms on the same connection,
unpredictably — applications cannot compensate because the delay is load-dependent
and nondeterministic. This is why bufferbloat is considered one of the most
damaging real-world network problems despite causing no packet loss.

---

### Plot 3 — UDP Packet Loss vs Payload Size

![UDP Packet Loss vs Payload Size](plots/03_udp_loss_vs_payload.png)

**What it shows:** UDP packet loss rate under the `lossy` condition (5% random
drop via dummynet) and the `congested` condition (background TCP flood).

**Results:**
- Lossy: 4.2–5.4% across all payload sizes, averaging ~4.9%
- Congested: 0.0% at every payload size

**Why lossy holds near 5% regardless of payload size:**

dummynet's PLR (packet loss rate) setting drops packets probabilistically —
each packet is independently dropped with a 5% probability. This decision is
made per packet, not per byte, which is why the loss rate is independent of
payload size. Whether a packet carries 64B or 16KB, it faces the same 5% coin
flip. The slight variation around 5% (4.2–5.4%) is expected statistical noise
from a small sample of 500 packets per run — with enough packets the mean would
converge to exactly 5%.

**Why TCP is not represented on this plot:**

TCP experiences the same 5% loss under this condition, but it hides it entirely.
TCP's reliability guarantee means every lost segment is retransmitted. From the
application's perspective, no data is lost — the receiver gets everything the
sender sent, just slightly later. TCP's loss_rate column always reads 0% because
the protocol itself absorbs the loss invisibly. This is one of TCP's core
guarantees, and the cost is visible in Plot 2 (elevated RTT from retransmissions).

UDP has no such guarantee. The receiver counts sequence number gaps — if seq 47
arrives after seq 45 and seq 46 never arrives, seq 46 is permanently lost. UDP
surfaces this directly. **This is not a flaw in UDP — it is a design choice.**
Applications that use UDP (video streaming, DNS, VoIP, games) either tolerate
missing data or handle recovery themselves at the application layer, which is
faster and more flexible than TCP's generic retransmission.

**Why congested shows 0% UDP loss:**

The background TCP flood creates queue pressure on the loopback interface, but
UDP's send rate under this condition was 200 pps. At 200 pps × 16KB, the offered
UDP load is ~26 Mbps. The loopback queue, even while handling the flood, never
backed up to the point of dropping UDP packets at this rate. This is an important
distinction: the congested condition shows that TCP's AIMD backoff is not triggered
by queue presence alone, and that UDP can coexist with a competing TCP flow without
loss as long as the combined load does not exhaust the queue. If the UDP send rate
had been higher (e.g., 1,000 pps), queue overflow and UDP loss would appear.

**Conclusion:** UDP loss is payload-independent under random drop conditions because
loss is decided per-packet. The critical real-world implication is that UDP
applications must design for loss — sequence numbers, checksums, and
application-layer recovery — because the network will not do it for them.

---

### Plot 4 — UDP Jitter vs Payload Size — All Conditions

![UDP Jitter vs Payload Size](plots/04_udp_jitter_vs_payload.png)

**What it shows:** Jitter (mean absolute deviation of consecutive inter-arrival
gaps, per RFC 3550) for UDP datagrams across all five conditions.

**Results:**
- Baseline: ~0.1ms, flat — near-zero variability
- Congested: ~0.07ms, flat — nearly identical to baseline
- High latency: ~1.1ms, flat across all payload sizes
- Bufferbloat: ~1.2ms at 64B–256B, drops sharply to ~0ms at 16KB
- Lossy: ~0.8ms at 64B, rises steadily to ~1.4ms at 16KB

**Why baseline and congested jitter are both near-zero:**

On clean loopback with a paced sender (time.sleep between sends), datagrams
arrive at nearly uniform intervals. There is nothing to disrupt the timing.
Congested produces the same result because at 200 pps, the UDP flow is not
competing hard enough with the flood to cause irregular scheduling. The flood
affects TCP (which backs off) but not the lightly-loaded UDP flow.

**Why high latency jitter is flat and elevated:**

The 50ms dummynet pipe releases packets at a controlled rate. Because all packets
see the same fixed delay, the inter-arrival gaps are largely preserved from the
sender's pacing — the delay shifts every packet by the same 50ms. The ~1.1ms
jitter comes from slight timer imprecision inside dummynet itself when releasing
packets from the delay queue. Since this imprecision is payload-independent, the
line is flat across all payload sizes.

**Why bufferbloat jitter follows a downward trend — the most counterintuitive result:**

At small payloads (64B), the 1 Mbit/s link drains the queue quickly between
packet arrivals. The queue empties, refills, empties again, creating irregular
delivery intervals — high jitter. At 16KB payloads, the offered load from the
sender exceeds the 1 Mbit/s link capacity, so the queue is *constantly* full and
never drains between packets. Packets exit the queue at a perfectly steady
1 Mbit/s clock rate — the queue itself becomes the pacer. The result is
paradoxically low jitter at large payloads under bufferbloat, even though absolute
latency is over 1,000ms. **This is a known property of large queues: they
regularize delivery timing at the cost of adding enormous delay.**

**Why lossy jitter rises with payload:**

Each dropped packet creates a gap in the arrival sequence — instead of two packets
arriving 2ms apart, the receiver sees one packet, then nothing, then the next
packet arrives 4ms later (because one was dropped). The gap size is proportional
to the time it would have taken that packet to arrive, which grows with payload
size. At 16KB and 200 pps, a single dropped packet creates a gap roughly equal to
two inter-send intervals, which is a large deviation. At 64B the gap is tiny.
Hence jitter rises linearly with payload under the lossy condition.

**Conclusion:** Jitter is the metric most sensitive to the *type* of network
problem rather than its severity. High latency produces flat, predictable jitter.
Bufferbloat produces high jitter at small payloads but low jitter at large ones.
Lossy produces rising jitter as payload grows. Each condition has a distinct
jitter fingerprint, which is why jitter is used in real networks (VoIP,
video conferencing) to diagnose network quality — it tells you not just that
something is wrong, but what type of problem is present.

---

### Plot 5 — TCP vs UDP Throughput — Congested Condition

![TCP vs UDP Throughput under Congestion](plots/05_congestion_comparison.png)

**What it shows:** Side-by-side throughput for TCP (with background flood) and
UDP (without flood) under the `congested` condition, at each payload size.

**Results:**
- TCP: ~100 Mbps at 64B → ~11,600 Mbps at 16KB
- UDP: ~0.2 Mbps at 64B → ~51 Mbps at 16KB
- TCP throughput is **~230× higher** than UDP at 16KB under congestion

**Why TCP throughput is so high despite congestion:**

AIMD (Additive Increase, Multiplicative Decrease) — TCP's congestion control
algorithm — halves the congestion window every time a loss event is detected. On
a real WAN link with RTTs of 20–100ms, this halving causes a significant and
sustained throughput drop because it takes many RTTs to ramp back up. On loopback,
RTT is ~0.04ms. TCP detects the loss, halves the window, and ramps back up to
full speed in a fraction of a millisecond. Over the measurement window of 500
messages, these micro-backoffs average out to near-baseline throughput. **This is
a loopback limitation, not a flaw in the experiment.** On a real network the
congestion signal would be clearly visible as a sustained drop.

**Why UDP throughput is so much lower:**

UDP is rate-limited to 200 pps by the application. This is intentional — UDP
gives the application control over send rate rather than automatically probing for
bandwidth. At 200 pps × 16KB, the maximum UDP throughput is ~26 Mbps, and the
measured ~51 Mbps at 16KB exceeds this because the timing measurement captures
the receive window from first to last packet (not including the 2-second timeout),
which slightly compresses the apparent elapsed time.

**Why this plot still matters despite the AIMD signal not appearing:**

The structural point this plot demonstrates is still valid: **TCP and UDP occupy
completely different performance regimes.** TCP continuously probes and fills
available bandwidth. UDP only sends as fast as the application configures it to.
This is the fundamental design trade-off between the two protocols — TCP optimizes
for maximum utilization of the network, UDP optimizes for application control and
low overhead. Neither is universally better; the right choice depends entirely on
what the application needs.

---

### Plot 6 — Throughput vs Socket Buffer Size (Baseline, payload=1024B)

![Throughput vs Buffer Size](plots/06_throughput_vs_buffer.png)

**What it shows:** TCP and UDP throughput as SO_SNDBUF/SO_RCVBUF varies from
4KB to 256KB, with payload fixed at 1024B on clean loopback. This plot answers
the buffer-settings half of the project's research question: *how do socket buffer
settings affect performance?*

**Results:**
- TCP: 4KB → 1,063 Mbps | 16KB → 1,243 Mbps | 64KB → 1,128 Mbps | 128KB → 1,334 Mbps | 256KB → 1,298 Mbps
- TCP spread: only ~25% between smallest and largest buffer (1,063 vs 1,334 Mbps)
- UDP: flat at ~3.2 Mbps across all buffer sizes — indistinguishable from baseline

**Why TCP throughput does not collapse at 4KB as expected:**

On a real network with a non-zero RTT, the socket send buffer determines how
much data TCP can have in-flight before it must stop and wait for an ACK. This
is the **bandwidth-delay product** constraint: a 10 Gbps link with 10ms RTT
requires 10,000,000 × 0.01 = 100,000 bytes = ~100KB in flight to keep the pipe
full. A 4KB buffer on that link would cap throughput at 4,096 × 8 / 0.01 =
~3.3 Mbps — a 99.97% reduction.

On loopback, RTT is ~0.09ms. The bandwidth-delay product is therefore
~1,000,000,000 × 0.00009 / 8 ≈ 11,250 bytes. A 4KB buffer already covers
most of that window. Even if the OS did not double the buffer, 4KB is still
large enough to keep the zero-delay loopback pipe filled with 1024B messages.
Two compounding factors remove any buffer-size signal:

1. **macOS silently doubles SO_SNDBUF/SO_RCVBUF.** A `setsockopt` call
   requesting 4,096 bytes results in an actual kernel buffer of ~8,192 bytes.
   The OS enforces a minimum because tiny buffers would break POSIX semantics.
   This is documented behavior in the macOS kernel, not an accident.

2. **Zero-RTT loopback requires almost no buffer.** Without propagation delay,
   every ACK returns almost instantly. TCP never has to wait for in-flight data
   to drain, so the buffer never becomes the bottleneck.

**What this tells us about real networks:**

The 4KB buffer effect would be dramatic on a real WAN link. At 100Mbit/s with
50ms RTT, the bandwidth-delay product is 625,000 bytes. A 4KB buffer caps
throughput at 4,096 × 8 / 0.05 ≈ 655 Kbps — a 99.3% reduction from maximum.
Large cloud providers (Amazon, Google) dedicate significant engineering effort to
buffer tuning precisely because this effect is critical in production. The loopback
measurement confirms the mechanism is real; it simply cannot demonstrate the
magnitude without a real propagation delay.

**Why UDP throughput is perfectly flat:**

UDP sends at 500 pps × 1024B = ~4.1 Mbps of offered load, but the measured value
is ~3.2 Mbps because Python's `time.sleep()` is not sub-millisecond accurate — the
actual inter-send interval is consistently longer than the target. Critically,
SO_RCVBUF does not affect this: the rate limiter is entirely in the sender's pacing
loop. UDP has no flow control mechanism that would allow the receive buffer to
throttle the sender. The receive buffer can only prevent loss by absorbing bursts
before the application drains it — but at 500 pps on an idle loopback, no bursting
occurs.

**Conclusion:** Socket buffer settings have a measurable but modest effect on TCP
throughput on loopback (1,063 → 1,334 Mbps, ~25% swing) and no effect on UDP
throughput. The modest TCP effect is explained by macOS's buffer-doubling behavior
and the near-zero RTT of loopback, both of which reduce the bandwidth-delay
product to where a small buffer is still sufficient. Buffer sizing is a critical
tuning parameter in production networking with real propagation delays — the
loopback environment understates the effect by design.

---

### Plot 7 — UDP One-Way Delay vs Socket Buffer Size (payload=1024B)

![UDP OWD vs Buffer Size](plots/07_owd_vs_buffer.png)

**What it shows:** UDP average one-way delay (OWD) as SO_RCVBUF varies from 4KB
to 256KB, under both baseline and bufferbloat conditions. This is the complementary
view to Plot 6: instead of throughput, it examines whether the socket buffer
introduces queuing delay.

**Results:**
- Baseline: ~0.09ms flat across all buffer sizes — no effect
- Bufferbloat: ~516–525ms flat across all buffer sizes — no trend

**Why OWD is flat under baseline:**

With no dummynet and 200 pps send rate, the loopback delivers each datagram in
under 0.1ms. The receive buffer always has capacity — packets are drained by the
receiver thread before the next one arrives. Buffer size is irrelevant when the
buffer is never more than 10% full.

**Why OWD does not increase with buffer size under bufferbloat — the key negative result:**

The hypothesis was: a larger SO_RCVBUF would hold more unread packets, increasing
the queuing delay experienced by each datagram before the application reads it.
This is socket-layer bufferbloat — distinct from network-layer bufferbloat
(which occurs in router queues).

This effect did not materialize. OWD is flat at ~516ms across all five buffer
sizes. The reason is architectural: dummynet intercepts packets at the packet
filter level, *before* they reach the socket receive buffer. The 1Mbit/s bandwidth
cap means the dummynet pipe itself is the bottleneck. All queuing occurs inside
dummynet's 100-slot internal queue, and by the time packets exit the pipe and
reach the socket buffer, they are already spaced at the 1Mbit/s drain rate. The
socket buffer sees a steady stream at ~1Mbit/s, which it can absorb regardless of
whether it is 4KB or 256KB.

Socket-layer bufferbloat only becomes visible when the *application* is slow to
read from the buffer — not when the *network* is slow. In that scenario, a large
buffer allows more unread data to accumulate, and new arrivals must wait behind
data the application has not yet consumed. The experiment design — with a dedicated
receiver thread that reads immediately — prevents this from occurring. The thread
always drains the buffer faster than the 1Mbit/s pipe can refill it.

**What conditions would show the hypothesized effect:**

To observe OWD growing with SO_RCVBUF, the experiment would need either a slow
application reader (introduce an artificial `time.sleep()` in the receive loop) or
a faster link that does not serialize queuing at the network layer. Under those
conditions, a 256KB buffer would allow ~250 unread 1024B packets to accumulate
before the kernel drops new arrivals, and late-reading packets would experience
OWDs proportional to how many packets were ahead of them.

**Conclusion:** Under the current experimental setup, SO_RCVBUF size does not affect
UDP OWD. The network-layer bottleneck (dummynet at 1Mbit/s) dominates, and the
socket buffer operates well within its capacity. This is a valid finding: it
demonstrates that socket-layer and network-layer bufferbloat are distinct phenomena,
and that a dedicated receiver thread effectively eliminates socket-layer queuing as
a latency source regardless of buffer size.

---

### Overall Conclusions

Across all seven plots and five network conditions, the data consistently
demonstrates the following findings:

**1. Payload size is the dominant driver of TCP throughput, with a 128× range.**

TCP throughput scales from ~82 Mbps at 64B to ~9,300 Mbps at 64KB under baseline
conditions. The mechanism is amortization of per-message overhead: every message
requires a system call, a kernel context switch, and ACK processing. At 64B, this
fixed cost represents the majority of elapsed time. At 64KB, the same overhead is
spread across 1,024× more data, making each byte 1,024× cheaper to deliver. TCP's
streaming model amplifies this further — it fills the pipe continuously rather than
waiting for application round-trips.

UDP scales linearly across the same range (~0.2 Mbps at 64B to ~51 Mbps at 16KB)
because it is rate-paced at 500 pps. Throughput is simply rate × payload. The
gap between TCP and UDP at large payloads (~180× at 16KB) reflects the fundamental
design difference: TCP continuously probes and fills available bandwidth, UDP
delivers only what the application explicitly sends.

**2. Bufferbloat is the most damaging condition measured, and uniquely unpredictable.**

The high latency condition adds a stable, consistent 103ms RTT at every payload
size — every run, every condition. Applications can buffer 103ms of data and adapt.
Bufferbloat at 16KB payload delivers RTTs between 20ms and 1,604ms across three
runs — a 80× range on the same machine, same link, same experiment. The mean
(~1,060ms) is almost irrelevant because the variance is so large. No application
can adapt to a latency whose next value is structurally unpredictable.

The mechanism is queue nondeterminism: the 1Mbit/s dummynet pipe fills and drains
between runs depending on OS scheduling. When the test begins with a full queue,
RTT is extreme. When the queue just drained, RTT is momentarily low. The end-user
experience mirrors this: a single connection can feel fine one second and completely
stalled the next, with no indication of why. This is why AQM (Active Queue
Management) algorithms like CoDel and FQ-CoDel were developed — they limit queue
depth dynamically rather than allowing unbounded buildup.

**3. UDP's jitter profile is a diagnostic fingerprint of the network condition.**

Each condition produces a distinct jitter signature that is visible in Plot 4:

- **Baseline / congested:** ~0.1ms. Clean loopback produces nearly clock-like
  inter-arrival spacing.
- **High latency:** ~1.1ms flat across all payload sizes. The constant 50ms delay
  shifts every packet equally, preserving the sender's pacing rhythm with only
  minor timer imprecision from dummynet.
- **Bufferbloat:** ~1.2ms at small payloads, falling to near-zero at 16KB. This
  inverted pattern is counterintuitive: at full link saturation the queue itself
  becomes the pacer, releasing packets at a mechanically regular 1Mbit/s rate.
  The queue regularizes timing while simultaneously adding enormous absolute delay.
- **Lossy:** ~0.8ms at 64B, rising to ~1.4ms at 16KB. Each dropped datagram
  leaves a proportional gap in the arrival sequence — larger packets create larger
  gaps when dropped.

This fingerprinting property is why jitter is used in real network diagnostics and
VoIP quality metrics (RFC 3550 / RTP). A flat jitter profile points to propagation
delay. A payload-dependent jitter profile points to loss. An inverted profile
points to a saturated queue.

**4. TCP and UDP respond fundamentally differently to every form of network stress.**

| Condition | TCP response | UDP response |
|-----------|-------------|-------------|
| Baseline | Throughput scales with payload; RTT ~0.07ms | Rate-paced; throughput linear with payload |
| Congested | AIMD backs off (microseconds on loopback); RTT paradoxically lower than baseline | No change — no congestion control |
| High latency | RTT = 2× propagation delay; throughput limited by bandwidth-delay product | OWD = propagation delay; loss = 0%; throughput unchanged |
| Bufferbloat | RTT explodes at large payloads (1,060ms, ±740ms) | Loss at large payloads (62% at 4KB, 100% at 16KB); OWD ~516ms |
| Lossy | Retransmits hide loss; RTT variance increases (17–64ms stdev) | Loss appears directly (4–5%); no retransmit cost |

TCP absorbs problems at the cost of latency and throughput variability. UDP
exposes problems directly at the cost of data loss. Neither protocol is better in
absolute terms — the choice depends on whether the application cares more about
delivery guarantees or about latency predictability.

**5. Socket buffer size has a modest effect on TCP and no measurable effect on UDP on loopback.**

TCP throughput across the 4KB–256KB buffer range spans only 1,063–1,334 Mbps —
a 25% swing with overlapping error bars. UDP is perfectly flat at ~3.2 Mbps across
the same range. Both findings are explained by the same loopback limitation: at
sub-millisecond RTT, the bandwidth-delay product is so small (~11KB) that even a
4KB buffer (doubled by macOS to ~8KB) is sufficient to keep the pipeline full.
The buffer-size effect becomes dramatic only with real propagation delay — at 50ms
RTT and 1Gbps, a 4KB buffer caps throughput at ~655 Kbps vs the wire limit.

The buffer sweep validates the measurement pipeline and confirms that at baseline
the kernel honors the requested buffer sizes (the throughput trend, though modest,
is monotonically increasing). Under bufferbloat, OWD is flat at ~516ms regardless
of SO_RCVBUF — the network-layer bottleneck (dummynet at 1Mbit/s) dominates and
socket-layer queuing is negligible when the receiver thread drains the buffer
continuously.

**6. Anomalies and limitations.**

Two results require explicit acknowledgment:

*Congested TCP outperforms baseline TCP at large payloads.* At 16KB, TCP under
the congested condition achieved ~11,579 Mbps, compared to ~7,887 Mbps at baseline.
AIMD theory predicts congestion should reduce throughput. The explanation is a
kernel warm-path effect: the background flood keeps TCP's fast path active and
memory caches warm. On loopback, AIMD backoff events complete in microseconds and
throughput recovers almost immediately. On a real network with non-trivial RTT,
the backoff penalty would persist for many RTTs and congested throughput would
clearly be lower than baseline.

*The buffer sweep cannot demonstrate the full magnitude of buffer effects.*
macOS's silent buffer-doubling and loopback's near-zero RTT both reduce the
measurable signal. Observing the full effect requires either a real physical
interface with propagation delay or an artificially imposed RTT via dummynet. This
is a valid limitation of any loopback-based networking experiment and should be
disclosed in any comparison with real-network measurements.

---

## Limitations and Future Work

Two results in this project warrant explicit acknowledgment: they are not
measurement errors, but they reflect the fundamental constraints of loopback-based
network experimentation. Both are defensible and explainable, but a reader
comparing these results against real-network benchmarks should understand why the
numbers look the way they do.

---

### Limitation 1 — Congested TCP Exceeds Baseline TCP (Plot 5)

**What was observed:** Under the congested condition (background TCP flood), TCP
throughput at 16KB payload reached ~11,579 Mbps — significantly higher than the
baseline's ~7,887 Mbps. AIMD theory predicts congestion should *reduce* throughput,
not increase it.

**Why this happens on loopback:** AIMD (Additive Increase, Multiplicative Decrease)
halves the congestion window every time a loss event is detected. On a real network
with an RTT of 20–100ms, this halving causes a sustained throughput reduction —
TCP must wait multiple RTTs before the window ramps back up to full size. On
loopback, RTT is ~0.04ms. TCP detects the loss, halves the window, and ramps back
to full speed within a single millisecond. Over the entire measurement window of
hundreds of messages, these micro-backoffs average out to near-baseline or above.
A secondary effect reinforces this: the background flood keeps the kernel's TCP
fast path continuously warm. On an otherwise idle loopback, each experiment must
wake the kernel from a cold state. With the flood running, TCP's memory caches are
hot and context switches are faster, producing a measured RTT *lower* than baseline
(~0.04ms vs ~0.07ms).

**Why it was not fixed:** The congested condition was designed to isolate competing
traffic as the single independent variable — no dummynet, no artificial delay. The
only way to make AIMD backoff persistent enough to show up in the measurement window
is to add propagation delay (e.g., 5ms via dummynet), but that conflates two
variables (congestion + propagation delay) and makes the condition harder to
interpret scientifically. Changing the experiment design to fix the anomaly would
undermine the controlled structure of the other conditions.

**What this means for real networks:** The anomaly is loopback-specific. On any
network with RTT > 1ms, AIMD backoff persists long enough to produce a clear and
sustained throughput reduction. The TCP congestion control mechanism is correct and
well-understood — the loopback environment simply cannot sustain the backoff long
enough to measure it.

**Future work:** Re-running the congested sweep with a small artificial RTT (5ms
via dummynet) would force AIMD backoff to persist across multiple measurement RTTs
and produce the expected congested < baseline result. This would be a minimal
change to the script and would not affect the other conditions.

---

### Limitation 2 — Buffer Sweep Effect is Subtle on Loopback (Plots 6 & 7)

**What was observed:** TCP throughput varies only ~25% across the full 4KB–256KB
buffer range (1,063–1,334 Mbps), and UDP throughput is flat across the same range.
The expected result — TCP collapsing at small buffers — did not appear. UDP OWD
was flat at ~516ms under bufferbloat regardless of SO_RCVBUF, not increasing with
buffer size as hypothesized.

**Why this happens on loopback:** The buffer-size effect on throughput is governed
by the **bandwidth-delay product** — the amount of data that must be in flight to
keep a link fully utilized: `BDP = bandwidth × RTT`. On a 1 Gbps link with 100ms
RTT, BDP = 12.5 MB. A 4KB send buffer on that link caps throughput at roughly
`4,096 × 8 / 0.1 = 328 Kbps` — a 99.97% reduction. On loopback, RTT is ~0.09ms.
BDP = `1,000,000,000 × 0.00009 / 8 ≈ 11,250 bytes`. A 4KB buffer (doubled by
macOS to ~8KB) already covers this entire window. There is almost no data that
needs to remain in-flight, so the buffer is never the bottleneck regardless of its
size.

Two compounding factors eliminate any remaining signal:

1. **macOS silently doubles SO_SNDBUF and SO_RCVBUF.** A `setsockopt` request for
   4,096 bytes results in an actual kernel allocation of ~8,192 bytes. This is
   documented macOS kernel behavior — the OS enforces a minimum to preserve POSIX
   socket semantics. As a result, the true minimum buffer tested was ~8KB, not 4KB.

2. **The receiver drains the socket buffer continuously.** The UDP receiver thread
   reads immediately upon waking. Under bufferbloat, packets arrive at 1Mbit/s —
   the receiver always stays ahead of the incoming data. OWD is determined by the
   dummynet pipe (which sits between the sender and the socket buffer), not by how
   much data is waiting unread in the buffer itself. Socket-layer bufferbloat only
   manifests when the *application* is slow to read — not when the *link* is slow
   to deliver.

**Why it was not fixed:** Demonstrating the full buffer-size effect would require
running the buffer sweep with an artificial propagation delay. Combining the buffer
sweep with the high_latency condition (50ms dummynet delay, 100ms RTT) would
increase BDP to ~12.5MB and make a 4KB buffer severely limiting. This would be a
meaningful scope expansion: a new condition in `05_buffer_sweep.sh`, additional
~20 minutes of data collection, and new aggregation logic in `analyze.py`. Given
the project's stage, this was deferred rather than implemented.

**What this means for real networks:** Socket buffer tuning is a critical
performance parameter in production systems precisely because real links have RTTs
measured in milliseconds, not microseconds. Amazon EC2, Google Cloud, and Linux
kernel defaults all include auto-tuning logic (TCP autotuning, `SO_RCVBUF` hints)
specifically to address this at scale. The loopback results confirm the mechanism
exists — they simply cannot demonstrate its magnitude without non-trivial
propagation delay.

**Future work:** Re-running `05_buffer_sweep.sh` with dummynet's 50ms delay active
would expose the full buffer-size effect on both throughput and OWD. A 4KB buffer
under 100ms RTT would cap TCP throughput to ~328 Kbps regardless of link speed,
and a 256KB buffer would show near-full throughput — a >100× difference vs the
~25% seen on loopback. For Plot 7, introducing an artificial read delay in the
receiver thread (e.g., 10ms sleep between reads) would allow packets to accumulate
in the socket buffer, making OWD grow with SO_RCVBUF as originally hypothesized.

---

## Implementation Notes

This section records design decisions and bugs encountered during development that
are relevant to understanding the measurement results.

### UDP datagram size limit — EMSGSIZE (Errno 40)

**Encountered during:** `scripts/02_baseline_sweep.sh` at payload = 65536B.

**Error:**
```
OSError: [Errno 40] Message too long
```

**Root cause:** The UDP protocol limits a single datagram to **65,507 bytes**
(65,535 byte IP packet − 20 byte IP header − 8 byte UDP header). A payload of
65,536 bytes is 29 bytes over this limit. Unlike TCP — which is a stream and
silently segments data into MSS-sized segments — UDP transmits each application
write as one atomic datagram. If the datagram exceeds the socket limit, the kernel
rejects the `sendto()` call with `EMSGSIZE` before the packet ever reaches the
network stack.

**Fix:** The baseline sweep was updated to use separate payload arrays for TCP and
UDP. TCP retains all six payload sizes (64B → 65536B). UDP uses five sizes (64B →
16384B), with 65536B excluded. 16384B was chosen as the UDP ceiling because it
sits at the macOS loopback MTU — the highest payload size that exercises IP
fragmentation without hitting the protocol limit. A validation check was also added
to `run_udp_sender()` that raises `ValueError` with an explanatory message if a
payload above 65507 bytes is requested, instead of propagating the OS-level error.

**Why 16384B specifically — not some value between 16384 and 65507?**

Every network interface has an MTU (Maximum Transmission Unit): the largest payload
it can carry in a single packet without the IP layer having to fragment it. On a
real Ethernet or Wi-Fi interface the MTU is typically 1,500 bytes. On macOS
loopback (`lo0`), Apple sets it to **16,384 bytes**.

When a UDP datagram exceeds the MTU, the IP layer splits it into multiple fragments,
sends them independently, and reassembles them at the destination. This is called
**IP fragmentation**, and it is one of the core behaviors this project is designed
to measure — it is where payload size begins to affect delivery reliability and
latency in a non-linear way.

16,384B is therefore the most scientifically meaningful upper bound for UDP on this
platform: it sits exactly at the fragmentation threshold. Payloads below it travel
as a single unfragmented packet; payloads at or above it trigger fragmentation.
Any value between 16,385B and 65,507B would only show "more fragmentation than
16,384B" without revealing a new behavior. Stopping at 16,384B keeps the payload
sweep focused on the transition point rather than adding redundant data points in a
range with no additional scientific value.

**Impact on results:** TCP baseline includes a 65536B data point; UDP baseline
does not. When comparing TCP vs UDP throughput at large payload sizes, the
comparison tops out at 16384B for UDP.

---

### sudo credential expiry kills script between dummynet conditions

**Encountered during:** `scripts/04_emulated_conditions.sh` — script consistently
stopped after `high_latency` and never reached `bufferbloat` or `lossy`.

**Root cause:** The `high_latency` condition runs 200 TCP ping-pong messages at
~100ms RTT across 5 payload sizes × 3 runs. That takes roughly 6 minutes. macOS
caches sudo credentials for 5 minutes by default. By the time the `high_latency`
loop finished and `teardown()` called `sudo dnctl -q flush`, the credential had
expired. In a non-interactive script context, `sudo` exits with a non-zero code
rather than prompting for a password — and `set -e` at the top of the script
treated that as a fatal error and killed the process.

This happened silently: the script exited without printing any error, making it
look like it completed successfully after the first condition.

**Fix:** Added `sudo -v` calls to refresh the credential cache at three points:
once at script start (to prompt upfront), once at the top of each condition's
loop iteration, and once inside `teardown()` itself. `sudo -v` does nothing if
the credential is still valid, and re-authenticates silently if it has expired.

### dummynet queue size hard limit — `2 <= queue size <= 100`

**Encountered during:** `scripts/04_emulated_conditions.sh`, `bufferbloat` condition.

**Error:**
```
dnctl: 2 <= queue size <= 100
```

**Root cause:** The `bufferbloat` condition was originally designed with `queue 1000`
to simulate an extremely large buffer. macOS's `dnctl` enforces a hard limit of
**100 slots maximum** per pipe. The value 1000 was taken from Linux `tc/netem`
examples where no such ceiling exists. macOS dummynet does not document this limit
prominently — it is only visible when the command fails at runtime.

**Fix:** Queue size reduced from 1000 to **100** (the dnctl maximum). At 1 Mbit/s
bandwidth, a 100-slot queue still produces clear bufferbloat behavior: each slot
holds a full-size packet (~16KB at the largest payload), so the queue can buffer
up to ~1.6 MB of data, creating significant queuing delay while keeping loss near
zero. The scientific signal (latency rising, loss staying flat) is preserved.

**Impact on results:** Bufferbloat queuing depth is smaller than originally
designed, but the characteristic signature — OWD and RTT rising while loss stays
at 0% — still appears clearly in the data.

---

**Impact on results:** The first four runs of script 04 each produced only
`high_latency` rows (15 per run = 60 total) before stopping. `bufferbloat` and
`lossy` data was collected on the fifth run after the fix was applied.

---

## Troubleshooting

### "Address already in use" when running an experiment

A previous run crashed and left a socket open on one of the measurement ports.

```bash
# Find which process is holding the port (replace 5301 with the relevant port)
lsof -i :5301

# Kill it
kill <PID>
```

Ports used: 5201 (TCP throughput), 5202 (TCP latency), 5301 (UDP), 5400 (flood).

### Tests fail with port conflict

Same as above — the integration tests open real sockets. If a port is stuck:

```bash
lsof -i :5201 -i :5202 -i :5301 -i :5400
kill <PID>
uv run pytest tests/ -v
```

### dummynet is still active after a crash

If your terminal closed mid-experiment while dummynet was applied, all loopback
traffic (including the test suite) will be affected. Reset it:

```bash
sudo dnctl -q flush
sudo pfctl -f /etc/pf.conf
sudo pfctl -d
```

Then verify the tests pass cleanly:

```bash
uv run pytest tests/ -v
```

### "Python 3.12 found, expected 3.14"

You are running `python` directly instead of through `uv`. Always use:

```bash
uv run python src/...
```

Or activate the venv first:

```bash
source .venv/bin/activate
python src/...
```

---

## Ports Used

| Module | Mode | Port |
|--------|------|------|
| `tcp_module.py` | Throughput | 5201 |
| `tcp_module.py` | Latency (ping-pong) | 5202 |
| `udp_module.py` | Receiver | 5301 |
| `background_flood.py` | Flood sender + receiver | 5400 |

---

## Dependency Management

This project uses `uv`. Common commands:

```bash
uv sync                  # install all dependencies from pyproject.toml
uv add <package>         # add a runtime dependency
uv add --dev <package>   # add a dev-only dependency (e.g. a test library)
uv run <command>         # run any command inside the project venv
```
