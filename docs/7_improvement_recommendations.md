# Improvement Recommendations

Prioritized list of things to add or fix to strengthen this project for a
Master's-level course. Ordered by impact and effort.

---

## Priority 1 — Buffer Size Sweep ✅ DONE

**Implemented in:** `scripts/05_buffer_sweep.sh`, `src/analyze.py` (plots 6 & 7)

---

## ~~Priority 1~~ — Buffer Size Sweep (HIGH IMPACT) — archived description

**Why it's #1:** The project is titled "how MTU/payload size and *socket buffer
settings* affect performance" but `buffer_bytes` is always `65536` in every row
of both CSVs. The stated research question about buffer settings was never
answered. A reviewer or professor will notice immediately.

**What to build:**
- A new script `scripts/05_buffer_sweep.sh` that fixes payload at 1024B and
  varies `--buffer` across `4096 16384 65536 131072` (or similar), 3 runs each,
  under `baseline` and `bufferbloat` conditions
- Both TCP and UDP
- Update `analyze.py` to add a new plot: Throughput/Jitter vs Buffer Size

**What it would show:**
- Small `SO_RCVBUF` (4096B) causes the OS to drop packets before they're read —
  UDP loss and TCP throughput degradation even at baseline
- Large buffers reduce loss but increase jitter and OWD — packets queue in the
  socket buffer instead of being dropped (socket-layer bufferbloat vs
  network-layer bufferbloat)
- This is the key result the project title promises

**Effort:** Medium — one new script, one new plot function in analyze.py

---

## Priority 2 — TCP_NODELAY Comparison (HIGH IMPACT, LOW EFFORT)

**Why it matters:** The proposal listed `TCP_NODELAY` as a key socket option.
Nagle's algorithm coalesces small messages, holding them for up to 200ms waiting
for more data. At 64B and 256B payloads, enabling `TCP_NODELAY` produces a
dramatic latency reduction. This is a classic, well-known result that directly
supports the small-payload analysis already in the data.

**What to build:**
- Add `--nodelay` flag to `src/tcp_module.py` that sets `TCP_NODELAY` on the
  client socket
- Run the existing payload sweep with `--nodelay` for a `baseline_nodelay`
  condition label
- Add a comparison plot or note in the analysis: RTT with vs without Nagle at
  small payloads

**What it would show:**
- At 64B: RTT drops significantly (Nagle adds up to 200ms of coalescing wait)
- At 1024B+: minimal difference (messages are large enough to send immediately)
- Directly demonstrates why real-time applications (gaming, SSH, trading) always
  set TCP_NODELAY

**Effort:** Low — ~10 lines in tcp_module.py, one extra sweep run

---

## Priority 3 — Confidence Intervals Instead of Raw Stdev (LOW EFFORT)

**Why it matters:** 3 runs gives mean ± stdev but no statistical significance.
Switching error bars to 95% confidence intervals looks more rigorous in a
graduate-level report.

**Formula:** `CI = 1.96 × stdev / sqrt(n)` where n = 3

**What to change:**
- In `src/analyze.py`, the `aggregate()` function already computes `_std`
  columns. Add a `_ci95` column: `agg[f"{col}_ci95"] = agg[f"{col}_std"] * 1.96 / (n ** 0.5)`
- Change all `yerr=..._std` to `yerr=..._ci95` in the plot functions

**Effort:** ~10 lines in analyze.py

---

## Priority 4 — Explain the Congested Condition Anomaly (NO CODE, JUST ANALYSIS)

**Why it matters:** The TCP congested throughput is *higher* than baseline at
large payloads:

| Payload | Baseline avg (Mbps) | Congested avg (Mbps) |
|---------|--------------------|--------------------|
| 16384B  | ~7,600             | ~11,579            |
| 4096B   | ~3,025             | ~4,139             |

TCP under congestion is faster than baseline — the opposite of what AIMD
predicts. This anomaly must be acknowledged and explained in the report or it
looks like an error.

**Likely explanation:** The flood creates competing traffic that fills the OS
send queue, which causes the kernel to increase the effective send window size
for the experiment flow. On loopback (which is not a real physical link), the
"congestion" signal from the flood may actually improve pacing for large
messages. Alternatively, the flood warms up the kernel's TCP fast path.

**What to do:** Add a "Limitations and Anomalies" section to the final report
explaining this result. Do not leave it unexplained — a graduate reviewer will
ask about it.

**Effort:** Zero code. Just writing.

---

## Priority 5 — Tail Latency / CDF Plot (MEDIUM EFFORT)

**Why it matters:** Mean ± CI hides the distribution. For networking research,
tail latency (P95, P99) matters more than the mean — it's what users actually
experience. A CDF of per-message RTT (TCP) or OWD (UDP) under each condition is
more informative than error bars on a mean.

**What to build:**
- Modify `measure_latency()` in `tcp_module.py` to return all individual RTT
  samples (currently it returns only mean and stdev)
- Modify `_udp_receiver()` in `udp_module.py` to return all OWD samples
- Store them in the CSV (either as a separate per-sample file or as percentile
  columns: `p50_latency_ms`, `p95_latency_ms`, `p99_latency_ms`)
- Add a CDF plot to analyze.py for the high_latency and bufferbloat conditions

**What it would show:**
- Under bufferbloat: the long tail (P99 >> P50) — most packets arrive quickly
  but a significant fraction queue for hundreds of milliseconds
- Under lossy: bimodal distribution — delivered packets arrive fast, retransmits
  cause a second hump at higher latencies
- This is standard methodology in networking papers (Google, SIGCOMM, etc.)

**Effort:** Medium — requires changes to both modules, CSV schema, and analyze.py

---

## Priority 6 — Goodput vs Throughput Distinction (MEDIUM EFFORT)

**Why it matters:** The current `throughput_mbps` counts all bytes transferred,
including retransmissions and headers. True *goodput* is application-level useful
bytes delivered per second. The difference is significant under lossy conditions
where TCP retransmits heavily.

**What to build:**
- For TCP: track `n_messages × payload_size` as the goodput numerator vs
  total bytes sent (which includes retransmits, measurable via `getsockopt
  TCP_INFO` on macOS/Linux)
- For UDP: goodput = `(n_received × payload_size) / elapsed_time` — already
  computable from existing data (`loss_rate_pct` gives you `n_received`)
- Add `goodput_mbps` column to both CSVs
- Add goodput efficiency plot: `goodput / throughput` ratio per condition

**Effort:** Medium for TCP (requires socket stats), low for UDP (computable
from existing columns)

---

## Priority 7 — Header Overhead Ratio Analysis (LOW EFFORT, ANALYSIS ONLY)

**Why it matters:** The proposal mentioned this and it directly explains the
small-payload throughput behavior already visible in the data.

**Formula:**
- TCP header = 20–60 bytes (typical ~40 bytes with options)
- UDP header = 8 bytes
- IP header = 20 bytes
- Overhead ratio = `(payload + header) / payload`

At 64B payload: TCP overhead ratio ≈ 1.9 (60B header on 64B payload = 94% overhead)
At 16384B payload: TCP overhead ratio ≈ 1.002 (negligible)

**What to build:**
- Add a static computation in analyze.py that prints an overhead table
- Optionally add a small inset or annotation on Plot 1 showing where header
  overhead becomes negligible (around 1024B)

**Effort:** ~20 lines in analyze.py, no data collection needed

---

## Summary Table

| Priority | Task | Code changes | Data collection | Impact |
|----------|------|-------------|----------------|--------|
| 1 | Buffer size sweep | New script + 1 plot | Yes — ~10 min | Fills the core research gap |
| 2 | TCP_NODELAY comparison | 10 lines in tcp_module | Yes — ~5 min | Classic result, directly supports proposal |
| 3 | Confidence intervals | 10 lines in analyze.py | No | Statistical rigor |
| 4 | Explain congested anomaly | None | No | Avoids looking like an error |
| 5 | Tail latency / CDF plot | Both modules + analyze.py | Yes | Standard grad-level methodology |
| 6 | Goodput vs throughput | Both modules + analyze.py | No (UDP), maybe TCP | Accuracy |
| 7 | Header overhead analysis | analyze.py only | No | Explains existing data |
