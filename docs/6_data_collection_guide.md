# Data Collection Guide

This document explains what each script in `scripts/` does, why each step exists,
and gives explicit start-to-finish instructions for running a full data collection
session safely.

---

## Before You Start — Pre-flight Checklist

Complete every item here before running any script. Skipping this causes port
conflicts, stale dummynet pipes, or contaminated data that requires re-running
entire sweeps.

**1. Confirm you are in the project root**
```bash
pwd
# Should print: .../5470_Final_Project
```

**2. Confirm the virtual environment is active**
```bash
uv run python --version
# Should print: Python 3.14.x
```

**3. Check that no measurement ports are in use**

The modules use four ports. If any are held by a previous crashed run, the
experiment will fail immediately with "Address already in use."

```bash
lsof -i :5201 -i :5202 -i :5301 -i :5400
```

If any port shows a process, kill it:
```bash
kill <PID>
```

Then re-check until the command returns nothing.

**4. Confirm dummynet is not already active**
```bash
sudo dnctl show
```

If this prints any pipes, tear them down before continuing:
```bash
sudo dnctl -q flush && sudo pfctl -f /etc/pf.conf && sudo pfctl -d
```

**5. Run the test suite to confirm the codebase is healthy**
```bash
uv run pytest tests/ -v
```

All 28 tests must pass. If any fail, fix the underlying issue before collecting
data. A failing test means the module is broken and the data it produces will be
wrong.

**6. Close anything that might compete for CPU**

Close other applications, browsers with many tabs, etc. The experiments are
sensitive to OS scheduling noise — background CPU usage inflates latency readings.

---

## Step 1 — Smoke Test

**Script:** `scripts/01_smoke_test.sh`

**What it does:** Runs one UDP experiment and one TCP experiment at 100 messages
each. Verifies both CSV files are created. Finishes in under 10 seconds.

**Why we do this first:** Before spending 30–45 minutes on a full sweep, you want
to confirm the modules run, the CSVs are written to the right place, and the
output numbers look sane (throughput > 0, loss ≈ 0%). If the smoke test fails,
you catch the problem in 10 seconds instead of after the sweep half-finishes.

**Run it:**
```bash
bash scripts/01_smoke_test.sh
```

**What good output looks like:**
```
UDP | baseline | payload=1024B | throughput=... Mbps | loss=0.00% | ...
TCP | baseline | payload=1024B | throughput=... Mbps | avg_rtt=...ms | ...
[PASS] results/udp_results.csv exists
[PASS] results/tcp_results.csv exists
Smoke test passed.
```

**If it fails:** Check for port conflicts (`lsof -i :5201 -i :5301`), then re-run
the test suite (`uv run pytest tests/ -v`) to identify the broken function.

**After it finishes:** Verify the CSV rows look correct:
```bash
cat results/tcp_results.csv
cat results/udp_results.csv
```

---

## Step 2 — Baseline Sweep

**Script:** `scripts/02_baseline_sweep.sh`

**What it does:** Sweeps payload sizes across both protocols, three runs each.
No dummynet. Produces 33 rows total.

- **TCP**: 6 sizes (64B → 65536B) — TCP streams, so 65536B is valid.
- **UDP**: 5 sizes (64B → 16384B) — UDP datagrams are capped at 65507 bytes by the
  protocol (65535 − 20 IP header − 8 UDP header). 65536B exceeds that limit and the
  kernel rejects the send with `EMSGSIZE`. 16384B is the meaningful upper bound for
  UDP because it sits at the loopback MTU, which is where IP fragmentation begins.

**Why we do this:** This is your **control group**. Every result from Scripts 3
and 4 is compared against these numbers. Running it without any network emulation
measures the raw performance of both protocols on your machine with no artificial
interference.

**Why we vary payload size:** Payload size is the primary independent variable.
Varying it shows fragmentation effects, header overhead at small sizes, and
throughput scaling at large sizes.

**Why three runs per cell:** A single run can be affected by OS scheduling noise.
Three runs let `analyze.py` compute mean ± standard deviation and smooth out
one-off anomalies.

**Run it:**
```bash
bash scripts/02_baseline_sweep.sh
```

**Runtime:** ~5–8 minutes.

**After it finishes:**

Confirm the row counts look right:
```bash
# TCP: 1 header + 1 smoke row + 18 baseline rows = 20 lines
# UDP: 1 header + 1 smoke row + 15 baseline rows = 17 lines
wc -l results/tcp_results.csv
wc -l results/udp_results.csv
```

Check that all payload sizes appear (6 for TCP, 5 for UDP):
```bash
awk -F',' 'NR>1 {print $1, $2, $8}' results/tcp_results.csv | sort | uniq -c
awk -F',' 'NR>1 {print $1, $2, $8}' results/udp_results.csv | sort | uniq -c
```

Release any lingering sockets before continuing:
```bash
lsof -i :5201 -i :5202 -i :5301 -i :5400
# Kill any PIDs that appear
```

---

## Step 3 — Congested Sweep

**Script:** `scripts/03_congested_sweep.sh`

**What it does:** Runs the same payload sweep but with `background_flood.py`
active during TCP experiments. UDP experiments run without the flood. Produces
30 rows total. No dummynet required.

**Why the flood is needed:** dummynet's bandwidth cap alone does not trigger TCP's
AIMD congestion control. TCP only backs off when it detects packet loss caused by
a queue overflowing from **competing traffic**. The flood creates that competition:
both flows share the loopback queue, the queue overflows, drops fire, and TCP cuts
its congestion window in half. Without the flood, you would apply a bandwidth cap
but never observe actual congestion control behavior.

**Why UDP does not use --flood:** UDP has no congestion control — it does not back
off when it detects loss. The interesting finding is that UDP **maintains its
throughput** while TCP backs off under the same condition. Running them side-by-side
in the same `congested` condition rows shows this contrast directly.

**Run it:**
```bash
bash scripts/03_congested_sweep.sh
```

**Runtime:** ~6–10 minutes.

**After it finishes:**

Confirm the flood is no longer running — it is managed automatically by the
`--flood` flag but verify no stale process remains:
```bash
lsof -i :5400
# Should return nothing
```

Release all measurement ports:
```bash
lsof -i :5201 -i :5202 -i :5301 -i :5400
# Kill any PIDs that appear
```

Spot-check that `congested` rows were written:
```bash
grep "congested" results/tcp_results.csv | wc -l
# Should be 15 (5 payloads × 3 runs)
grep "congested" results/udp_results.csv | wc -l
# Should be 15
```

---

## Step 4 — Emulated Conditions

**Script:** `scripts/04_emulated_conditions.sh`

**What it does:** Applies three dummynet conditions to the loopback interface in
sequence — `high_latency`, `bufferbloat`, `lossy` — running the payload sweep
under each one. Tears down dummynet completely between conditions. Produces 90
rows total.

**Why this requires sudo:** dummynet is part of the macOS packet filter (`pfctl`).
Modifying the packet filter requires root privileges.

**The three conditions:**

| Condition | Settings | What it measures |
|-----------|----------|-----------------|
| `high_latency` | 50ms delay, 100Mbit/s, small queue | Propagation delay — OWD rises to ~50ms, TCP RTT to ~100ms |
| `bufferbloat` | 0ms delay, 1Mbit/s, 1000-slot queue | Large queue + slow link — latency spikes while loss stays near 0% |
| `lossy` | 0ms delay, 100Mbit/s, 5% random loss | UDP shows 5% loss; TCP hides it via retransmit but throughput drops |

**Before running — extra safety check:**

Confirm dummynet is clean:
```bash
sudo dnctl show
# Must return nothing before you start
```

**Run it:**
```bash
bash scripts/04_emulated_conditions.sh
```

**Runtime:** ~30–45 minutes. The `bufferbloat` condition is the slowest — the
1Mbit/s cap means each experiment transfers data at a fraction of normal speed.

**If the script is interrupted (Ctrl+C or crash):**

The `trap teardown EXIT` in the script will attempt to clean up automatically.
But always verify manually after an interruption:

```bash
# 1. Confirm dummynet is clear
sudo dnctl show
# Should return nothing

# 2. If it still shows pipes, tear down manually
sudo dnctl -q flush && sudo pfctl -f /etc/pf.conf && sudo pfctl -d

# 3. Release all ports
lsof -i :5201 -i :5202 -i :5301 -i :5400
# Kill any PIDs that appear

# 4. Run the test suite to confirm the environment is clean
uv run pytest tests/ -v
# All 28 must pass before re-running the script
```

**After it finishes:**

Verify dummynet is fully torn down:
```bash
sudo dnctl show
# Must return nothing
```

Release all ports:
```bash
lsof -i :5201 -i :5202 -i :5301 -i :5400
```

Spot-check row counts per condition:
```bash
for condition in high_latency bufferbloat lossy; do
    count=$(grep "$condition" results/tcp_results.csv | wc -l | tr -d ' ')
    echo "TCP $condition: $count rows (expected 15)"
done
for condition in high_latency bufferbloat lossy; do
    count=$(grep "$condition" results/udp_results.csv | wc -l | tr -d ' ')
    echo "UDP $condition: $count rows (expected 15)"
done
```

---

## After All Scripts Complete — Final Verification

Run these checks before handing the data to `analyze.py`.

**1. Confirm total row counts**
```bash
echo "TCP rows: $(( $(wc -l < results/tcp_results.csv) - 1 ))"
echo "UDP rows: $(( $(wc -l < results/udp_results.csv) - 1 ))"
# TCP: expect ~157 (1 smoke + 36 baseline + 15 congested + 45 emulated + 15 flood TCP + ...)
# UDP: expect ~157
```

**2. Confirm all five conditions are present**
```bash
awk -F',' 'NR>1 {print $4}' results/tcp_results.csv | sort | uniq -c
awk -F',' 'NR>1 {print $4}' results/udp_results.csv | sort | uniq -c
```

You should see `baseline`, `congested`, `high_latency`, `bufferbloat`, and `lossy`
all represented.

**3. Confirm dummynet is off**
```bash
sudo dnctl show   # must return nothing
```

**4. Confirm all ports are free**
```bash
lsof -i :5201 -i :5202 -i :5301 -i :5400   # must return nothing
```

**5. Run the test suite one final time**
```bash
uv run pytest tests/ -v
# All 28 must pass — confirms the environment is clean and nothing was left open
```

---

## Summary Table

| Step | Script | Runtime | Requires sudo | Rows added |
|------|--------|---------|---------------|-----------|
| 1 | `01_smoke_test.sh` | < 10 sec | No | 2 |
| 2 | `02_baseline_sweep.sh` | 5–8 min | No | 33 |
| 3 | `03_congested_sweep.sh` | 6–10 min | No | 30 |
| 4 | `04_emulated_conditions.sh` | 30–45 min | Yes | 90 |
| **Total** | | **~50–65 min** | | **155** |

---

## Why four separate scripts?

Each script builds on the previous one. Starting with a smoke test catches
environment problems early. Starting with baseline gives you a clean control
group. Running congestion before emulated conditions separates two distinct types
of interference — software-level (competing traffic) vs. network-level (dummynet
delay/loss). Running them as one monolithic script would make it harder to debug
a mid-run failure and harder to re-run a single condition without repeating
everything.
