# Data Collection Guide

This document explains what each script in `scripts/` does, why each step exists,
and what the collected data will be used for.

---

## Why four separate scripts?

Each script builds on the previous one. Starting with a smoke test ensures the
environment works before you invest 30+ minutes in a full sweep. Starting with
baseline (no emulation) gives you a clean control group before introducing
variables. Running congestion before emulated conditions separates two different
types of interference — software-level (competing traffic) vs. network-level
(dummynet delay/loss).

Running everything as one big script would make it harder to debug if something
goes wrong halfway through, and harder to re-run just one condition if you need to.

---

## Script 1 — `scripts/01_smoke_test.sh`

### What it does

Runs one UDP experiment and one TCP experiment with a small message count (100
messages) and checks that both CSV files were created. The whole thing finishes
in under 10 seconds.

### Why we do this first

Before committing 30–45 minutes to a full sweep, you want to know:
- The modules actually run without errors
- The CSV files are being written to the right place
- The output numbers look roughly sane (throughput > 0, loss ≈ 0%)

If the smoke test fails, something is wrong with your environment — a port
conflict, a broken import, a missing dependency — and you catch it in 10 seconds
instead of after the sweep half-finishes.

### What data it produces

One row in `results/udp_results.csv` and one row in `results/tcp_results.csv`,
both labeled `baseline`, `run=1`, `payload=1024B`.

### When to re-run it

Any time you change the source code and want to confirm nothing is broken before
running the full sweep.

---

## Script 2 — `scripts/02_baseline_sweep.sh`

### What it does

Sweeps across six payload sizes (64B, 256B, 1024B, 4096B, 16384B, 65536B) at a
fixed buffer size (65536 bytes), running both TCP and UDP, three times each.

**Total rows produced:** 6 payload sizes × 2 protocols × 3 runs = **36 rows**

### Why we vary payload size

Payload size is the primary independent variable in this project. Varying it
shows:

| Payload range | What you observe |
|---------------|-----------------|
| 64–256B | High per-message header overhead. TCP's 20-byte header is a significant fraction of a 64-byte payload. UDP's 8-byte header matters less. |
| 1024B | Typical application message size — a reasonable midpoint. |
| 4096–16384B | Throughput generally increases as per-message overhead becomes proportionally smaller. |
| 65536B | Above the loopback MTU (16384 bytes on macOS). The OS must fragment the datagram into multiple IP packets and reassemble on the other end. You should see a throughput drop and latency increase relative to 16384B. |

### Why three runs per cell

A single run can be affected by OS scheduling noise — a background process
temporarily competing for CPU, the garbage collector running mid-experiment, etc.
Running three times and averaging reduces this noise. `analyze.py` will compute
mean ± standard deviation across the three runs for each cell.

### Why no dummynet

This is your **control group**. Dummynet introduces artificial conditions; this
sweep measures the raw performance of both protocols on your machine with no
interference. Every result from Scripts 3 and 4 is interpreted relative to these
baseline numbers.

### What data it produces

36 rows spread across both CSVs, all labeled `condition=baseline`.

---

## Script 3 — `scripts/03_congested_sweep.sh`

### What it does

Runs the same payload sweep as Script 2, but with `background_flood.py` active
during all TCP experiments. UDP experiments run without the flood since UDP has
no congestion control.

**Total rows produced:** 5 payload sizes × 2 protocols × 3 runs = **30 rows**

(65536B is excluded — the combination of a large payload and a background flood
makes the experiment very slow on a slow loopback.)

### Why this is separate from Script 4

This condition creates congestion **without dummynet** — no sudo required, no
delay or bandwidth cap applied. The only thing that changes is a competing TCP
flow consuming bandwidth on the same loopback queue. This isolates the effect of
**competing traffic** from the effect of **network conditions**.

### What the flood actually does

When the flood is active, both the flood and the experiment share the same
loopback queue. The queue overflows, dropping packets from both flows. TCP detects
those drops and triggers AIMD (Additive Increase, Multiplicative Decrease):

- **Additive Increase** — slowly ramp up the congestion window when no loss is
  detected
- **Multiplicative Decrease** — cut the congestion window in half when a drop is
  detected

This is TCP's built-in congestion control responding to real competition. You will
see `throughput_mbps` drop and `avg_latency_ms` rise compared to the baseline.

### Why UDP does not use --flood

UDP does not have congestion control. It does not back off when it detects loss —
it just keeps sending at the same rate. Running UDP alongside an active TCP flood
would show loss increasing, but that is the same signal as the `lossy` dummynet
condition in Script 4. The more interesting observation is that UDP **maintains
its throughput** while TCP backs off — which you see by comparing the two protocols
side-by-side in the same `congested` condition rows.

### What data it produces

30 rows, all labeled `condition=congested`.

---

## Script 4 — `scripts/04_emulated_conditions.sh`

### What it does

Applies three dummynet conditions to the loopback interface in sequence, running
the payload sweep under each one. Tears down dummynet completely between conditions.

**Total rows produced:** 3 conditions × 5 payload sizes × 2 protocols × 3 runs = **90 rows**

### Why this requires sudo

dummynet is part of the macOS packet filter (`pfctl`). Modifying the packet
filter requires root privileges — the same reason you need sudo to configure
network interfaces.

### The three conditions and what they measure

#### `high_latency` — 50ms delay, 100Mbit/s, small queue

Simulates a high-latency link (e.g. a satellite connection or a geographically
distant server). There is plenty of bandwidth, but every packet takes 50ms to
traverse the link.

**What you expect to see:**
- UDP one-way delay (OWD) rises to ~50ms
- TCP RTT rises to ~100ms (RTT = 2 × one-way delay)
- Throughput stays near baseline — the pipe is wide, just slow
- This isolates **propagation delay** from all other variables

#### `bufferbloat` — 0ms delay, 1Mbit/s, 1000-slot queue

This is the most important condition for this project. A slow link (1Mbit/s) with
a very large queue (1000 slots). The slow link causes the queue to fill up; the
large queue holds thousands of packets before dropping any, so loss stays near
zero — but latency spikes because packets wait in the queue before they are sent.

**What you expect to see:**
- Packet loss stays low (near 0%) — the queue absorbs the backlog instead of
  dropping
- OWD and RTT rise dramatically as queue depth grows
- Throughput is capped at ~1Mbit/s regardless of payload size
- This is the **bufferbloat signature**: high latency despite low loss

This condition demonstrates why large buffers are not always better — they trade
latency for loss avoidance, which is the wrong tradeoff for latency-sensitive
applications.

#### `lossy` — 0ms delay, 100Mbit/s, small queue, 5% random loss

Simulates a lossy wireless link. Every packet has a 5% chance of being dropped at
random, independent of queue state.

**What you expect to see:**
- UDP loss rate rises to ~5% — UDP delivers whatever gets through with no retry
- TCP loss rate in the CSV stays 0% — TCP automatically retransmits lost segments
  so the application layer never sees missing data, but throughput drops because
  retransmissions consume bandwidth
- TCP RTT rises slightly due to retransmission delays
- This contrast between TCP and UDP under loss is a key data point for the report

### Why dummynet is torn down between conditions

If condition A is still active when condition B starts, measurements for B are
contaminated by A's settings. The `trap teardown EXIT` line in the script ensures
dummynet is torn down even if the script crashes or is interrupted with Ctrl+C.

### What to do if the script crashes mid-run

Dummynet will still be active. Reset it manually:

```bash
sudo dnctl -q flush && sudo pfctl -f /etc/pf.conf && sudo pfctl -d
```

Then verify the test suite passes cleanly before re-running:

```bash
uv run pytest tests/ -v
```

---

## Running order and total data

Run the scripts in order. Each one appends rows to the existing CSVs — none of
them overwrite previous results.

| Script | Condition | Rows added | Requires sudo |
|--------|-----------|-----------|---------------|
| `01_smoke_test.sh` | baseline (1 row each) | 2 | No |
| `02_baseline_sweep.sh` | baseline | 36 | No |
| `03_congested_sweep.sh` | congested | 30 | No |
| `04_emulated_conditions.sh` | high_latency, bufferbloat, lossy | 90 | Yes |
| **Total** | | **158 rows** | |

After all four scripts complete, both CSVs will have enough data for `analyze.py`
to generate all five plots across four distinct conditions plus baseline.
