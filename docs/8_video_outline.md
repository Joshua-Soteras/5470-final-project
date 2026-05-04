# Project Video Outline

**Course:** CS 5470 — Advanced Computer Networks
**Target length:** ~10 minutes
**Audience:** Professor and classmates (graduate level)

---

## Section 1 — Hook (0:00–0:45)

- Open with the core question: *"TCP and UDP are the two protocols that everything
  on the internet runs on — but what actually happens to their performance when you
  change payload size or socket buffer settings?"*
- Why it matters in practice: bufferbloat kills real-time apps, buffer tuning is
  critical in production, TCP and UDP make fundamentally different trade-offs
- One sentence setting up the rest: this project measures those trade-offs directly
  using a custom measurement platform on a controlled loopback environment

---

## Section 2 — Project Overview (0:45–1:30)

- Solo implementation of a full networking measurement platform
- Two independent variables:
  - **Payload size** — 64B to 64KB (TCP) / 64B to 16KB (UDP)
  - **Socket buffer size** — 4KB to 256KB (SO_SNDBUF / SO_RCVBUF)
- Five network conditions: baseline, congested, high_latency, bufferbloat, lossy
- Output: 7 plots generated from 230+ TCP rows and 211+ UDP rows across all conditions
- All data collected, all plots generated, all tests passing (40 pytest tests)

---

## Section 3 — Architecture and Tools (1:30–2:30)

### How one experiment run works

- A **server/receiver** thread starts in the background and binds to a port
- A **client/sender** runs in the main thread, sends messages, collects metrics
- Results are appended to a CSV file (`results/tcp_results.csv` or `udp_results.csv`)
- Three runs per condition cell → mean ± standard deviation in `analyze.py`

### Tools and why each was chosen

| Tool | Why |
|------|-----|
| **Python 3.14 + uv** | Fast socket programming; uv gives reproducible environments without virtualenv boilerplate |
| **`socket` (stdlib)** | Raw TCP/UDP access with no abstraction — lets us call `setsockopt(SO_SNDBUF)` directly |
| **dummynet via `pfctl`** | macOS-native network emulator; `tc`/`netem` are Linux-only. Applies delay, bandwidth caps, queue limits, and packet loss to the loopback interface |
| **pandas + matplotlib** | Aggregation (mean/stdev across 3 runs) and plot generation with error bars |
| **pytest** | Test isolation via `RESULTS_PATH` patching — redirects CSV output to a temp file per test so experiments don't touch real data |

### Script pipeline

```
01_smoke_test.sh  →  02_baseline_sweep.sh  →  03_congested_sweep.sh
  →  04_emulated_conditions.sh  →  05_buffer_sweep.sh  →  analyze.py
```

---

## Section 4 — Results: Payload Sweep (2:30–6:00)

### Plot 1 — Throughput vs Payload Size (Baseline)

- TCP scales from ~82 Mbps at 64B to ~9,300 Mbps at 64KB — 128× increase
- UDP stays flat (rate-paced at 500 pps — throughput = rate × payload)
- Key mechanism: TCP amortizes per-syscall overhead over larger messages; UDP does not

### Plot 2 — TCP Latency vs Payload Size (All Conditions)

- The most important plot in the dataset
- Baseline: ~0.07ms; High latency: stable ~103ms (2 × 50ms dummynet delay)
- **Bufferbloat:** RTT explodes to ~1,060ms at 16KB with ±740ms standard deviation
  — one run measured 20ms, another 1,604ms on the same machine
- Lossy: ~11–15ms mean but 17–64ms stdev from retransmit timing variance

### Plot 3 — UDP Packet Loss vs Payload Size

- Lossy holds at ~4.9% across all payload sizes — dummynet drops per-packet, not
  per-byte, so loss is payload-independent
- Congested shows 0% loss — the TCP flood does not overwhelm the loopback queue
  at 200 pps UDP send rate

### Plot 4 — UDP Jitter vs Payload Size (All Conditions)

- Counterintuitive result: bufferbloat jitter is *highest* at small payloads and
  *lowest* at 16KB — a saturated queue becomes its own pacer, regularizing arrivals
- Lossy jitter rises with payload — each dropped datagram leaves a proportional gap

### Plot 5 — TCP vs UDP Throughput Under Congestion

- TCP fills the chart; UDP is barely visible at the bottom
- Known anomaly: congested TCP exceeds baseline at large payloads — explained by
  AIMD recovery in microseconds on loopback (~0.04ms RTT) and kernel warm-path
  effect from the flood; this is loopback-specific, acknowledged in the report

---

## Section 5 — Results: Buffer Sweep (6:00–7:30)

### Plot 6 — Throughput vs Socket Buffer Size (Baseline, payload=1024B)

- TCP spans 1,063–1,334 Mbps across 4KB–256KB — only a 25% swing
- Expected dramatic collapse at 4KB did not appear
- Two reasons: macOS silently doubles SO_SNDBUF/SO_RCVBUF; loopback RTT ~0.09ms
  gives a bandwidth-delay product of ~11KB — smaller than even the doubled buffer
- UDP perfectly flat — rate-pacing controls throughput, not the socket buffer

### Plot 7 — UDP OWD vs Socket Buffer Size (payload=1024B)

- OWD flat at ~516ms under bufferbloat regardless of SO_RCVBUF
- The hypothesized "OWD grows with buffer size" effect did not appear
- Root cause: dummynet intercepts packets *before* they reach the socket buffer;
  all queuing happens in the network layer, not the socket layer
- Valid negative result: demonstrates that socket-layer and network-layer
  bufferbloat are distinct phenomena

---

## Section 6 — Key Takeaways (7:30–8:45)

1. **TCP amortizes overhead; payload size is the primary throughput lever** — 128×
   range from 64B to 64KB is driven entirely by syscall and framing cost amortization

2. **Bufferbloat is uniquely damaging because it is unpredictable, not just slow**
   — 103ms high-latency RTT is stable and adaptable; 20–1,604ms bufferbloat RTT
   on the same connection is not

3. **UDP exposes network problems directly** — loss, jitter, and OWD are unfiltered
   signals of what the network is doing; TCP hides them in latency and variance

4. **Socket buffer effects require real propagation delay to measure** — loopback's
   near-zero RTT collapses the bandwidth-delay product, making buffer size appear
   irrelevant; in production at 50ms RTT a 4KB buffer caps TCP at ~328 Kbps

---

## Section 7 — Limitations and Close (8:45–10:00)

- Two known anomalies acknowledged and explained in the report:
  - Congested > baseline (AIMD sub-ms recovery on loopback)
  - Buffer sweep subtle (BDP ~11KB, macOS buffer doubling)
- Future work: re-run buffer sweep with 50ms dummynet delay to expose the full
  buffer-size effect; add slow-reader test to demonstrate socket-layer bufferbloat
- Wrap: the platform is fully extensible — any new condition or variable can be
  added by writing one script and one plot function

---
