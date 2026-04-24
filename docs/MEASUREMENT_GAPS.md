# Measurement Gaps — Proposal vs. Current Implementation

Two gaps were identified by comparing the original project proposal against the
current implementation plan. Both have been implemented and are documented here
with the exact files and locations where each change lives.

---

## Gap 1 — Competing Background Traffic for the Congestion Condition

### What the proposal says

> *"Introduce competing background traffic to stress-test both protocols and observe
> congestion response."*

### What was missing

The `congested` condition in the emulation plan used dummynet to cap bandwidth
(`bw 2Mbit/s`) and add delay (`delay 20ms`). This simulates a **slow link**, but it
does not create actual TCP contention. The experiment's traffic was the only traffic
on the link — there was no rival flow competing for bandwidth.

### Why this matters

TCP's congestion control (AIMD — Additive Increase, Multiplicative Decrease) only
triggers when it detects packet loss or ECN signals caused by **queue overflow from
competing flows**. Dummynet artificially drops packets based on a fixed rate (`plr`)
or bandwidth cap, which bypasses the feedback loop that AIMD responds to. A real
competing TCP flood causes TCP to back off dynamically — which is the behaviour
the proposal is asking us to measure.

UDP has no such mechanism, so it will continue transmitting at the same rate while
TCP throttles. That contrast is the core result the congestion condition is meant
to produce.

### Implementation — `src/background_flood.py` (new file)

The solution is a background TCP flood: a second sender that continuously pushes
data to a separate port on loopback while the actual experiment runs. This saturates
the loopback queue and forces the experiment's TCP flow to compete for bandwidth.

**`src/background_flood.py`** exposes two public functions and a CLI entry point:

| Symbol | Location | What it does |
|--------|----------|--------------|
| `_flood_server()` | `src/background_flood.py` — `_flood_server` function | Binds TCP socket on port 5400, accepts one connection, reads and discards all data until stopped |
| `_flood_client()` | `src/background_flood.py` — `_flood_client` function | Connects to the flood server, sends 65 KB chunks in a tight loop as fast as the OS accepts them |
| `start_flood()` | `src/background_flood.py` — `start_flood` function | Spawns both threads as daemons, sleeps 0.2 s to let the flood reach full speed, returns a stop event |
| `stop_flood()` | `src/background_flood.py` — `stop_flood` function | Sets the stop event, waits 0.5 s for threads to close their sockets cleanly |

**Port used:** 5400 — separate from all measurement ports (5201, 5202, 5301) so
the flood never interferes with the experiments being measured.

#### How to use it from `run_experiments.sh` (once built)

```bash
# Option A — Python API (recommended, clean shutdown)
python3 -c "
from src.background_flood import start_flood, stop_flood
e = start_flood()
import time; time.sleep(30)   # replace with actual experiment calls
stop_flood(e)
"

# Option B — shell background process (simpler for bash scripts)
python3 src/background_flood.py &
FLOOD_PID=$!
# ... run congested condition experiments ...
kill $FLOOD_PID
```

#### What the data should show

- **TCP**: throughput drops as AIMD backs off; latency increases as the queue fills
- **UDP**: throughput stays near the send rate but loss rate rises as the queue overflows
- The side-by-side comparison directly demonstrates the TCP vs. UDP congestion
  tradeoff called out in the proposal

---

## Gap 2 — UDP One-Way Latency

### What the proposal says

> *"Metrics collected: throughput (Mbps), round-trip latency (ms), jitter (ms variance),
> and packet loss rate (%)."*

### What was missing

TCP covers round-trip latency via the ping-pong echo mode in `tcp_module.py`.
UDP previously recorded throughput, loss, and jitter — but **not latency**.

### Why this matters

TCP's RTT and UDP's one-way delay (OWD) are fundamentally different measurements:
- **RTT** includes processing time at the echo server and the return trip
- **OWD** measures only the forward path — how long it takes a datagram to travel
  from sender to receiver

Having both allows the report to make a direct latency comparison between TCP and
UDP under identical conditions. OWD also gives the clearest view of the bufferbloat
signature — latency grows before loss appears.

### Why it was easy to add

The datagram header already carried the send timestamp:

```
Bytes 0-7  : sequence number (uint64)
Bytes 8-15 : send timestamp in nanoseconds (uint64)   ← already in every datagram
```

The receiver already recorded `time.perf_counter_ns()` on arrival. Because both
sender and receiver run on the same machine and use the same monotonic clock,
the difference is a valid one-way delay:

```
owd_ns = arrival_ns - send_ns
```

This is valid **only on loopback** where both clocks are identical. It would not
be valid across two separate machines without clock synchronisation (PTP/NTP).

### Implementation — changes to `src/udp_module.py`

All changes are confined to `src/udp_module.py`:

| Change | Location | What it does |
|--------|----------|--------------|
| Added `avg_owd_ms` column | `CSV_HEADER` list | New column in `udp_results.csv` |
| Added `send_times_ns` accumulator | `_udp_receiver` — accumulator declarations | Parallel list to `arrival_times_ns`, stores `send_ns` from each datagram header |
| Capture `send_ns` instead of discarding | `_udp_receiver` — `struct.unpack_from` call | Changed `seq_num, _ = ...` to `seq_num, send_ns = ...` and appends to `send_times_ns` |
| OWD computation after receive loop | `_udp_receiver` — post-loop calculations | `owd_samples_ms = [(arrival - send) / 1e6 ...]`, then `statistics.mean()` |
| `avg_owd_ms` stored in results dict | `_udp_receiver` — results assignment block | Added `results["avg_owd_ms"] = avg_owd_ms` alongside existing metrics |
| Zero-value fallback | `_udp_receiver` — early return for zero packets | Added `results["avg_owd_ms"] = 0.0` to the no-packets guard clause |
| Updated return type | `run_udp_experiment` — function signature | `tuple[float, float, float]` → `tuple[float, float, float, float]` |
| `avg_owd_ms` passed to `save_result` | `run_udp_experiment` — `save_result` call | Added `"avg_owd_ms": round(avg_owd_ms, 4)` to the row dict |
| Updated return value | `run_udp_experiment` — return statement | Now returns `(throughput_mbps, loss_rate_pct, jitter_ms, avg_owd_ms)` |
| Updated CLI summary | `main()` — print statement | Added `owd={owd:.3f}ms` to the one-line output |
| Updated stale comment | `run_udp_sender` — header packing comment | Corrected note that `send_ns` is now used for OWD, not just jitter |

#### What the data should show

- Under `baseline`: OWD < 0.1 ms on loopback (sub-millisecond)
- Under `high_latency` (dummynet 50 ms delay): OWD jumps to ~50 ms
- Under `bufferbloat`: OWD grows gradually as the queue fills even while
  loss stays near zero — the classic bufferbloat signature
- Comparing UDP OWD to TCP RTT across all conditions gives a clean
  protocol-level latency comparison for the report

---

## Summary

| Gap | File(s) changed | Status |
|-----|----------------|--------|
| Background flood for congestion | `src/background_flood.py` (new file) | Implemented |
| UDP one-way latency | `src/udp_module.py` | Implemented |

All 14 existing tests continue to pass after both changes. The integration test
(`test_experiment_basic_loopback`) was updated to unpack the new 4-tuple return
value and assert `avg_owd_ms >= 0.0` and `avg_owd_ms < 100.0` as a sanity check.
