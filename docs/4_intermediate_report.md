# CS 5470 — Intermediate Progress Report
**Project:** Network Performance Analyzer: MTU, Bufferbloat, TCP vs UDP  
**Student:** Joshua Soteras  
**Date:** April 24, 2026

---

## Project Overview

This project measures how **application payload size** (an MTU sweep from 64 bytes
to 64 KB) and **socket buffer settings** (SO_SNDBUF / SO_RCVBUF) affect network
performance under both TCP and UDP. All experiments run over the loopback interface
(`127.0.0.1`) on macOS, with real network conditions emulated via `dummynet`
(`pfctl`), the macOS/BSD equivalent of Linux's `tc`/`netem`.

The goal is a comparative analysis of how each protocol responds to five named
conditions — baseline, high latency, bufferbloat, lossy, and congested — producing
CSV data that feeds into five comparative plots. The project was originally scoped
for a three-person team; this is a solo implementation.

---

## Implementation Status

| Component | Status |
|-----------|--------|
| `src/udp_module.py` | Complete |
| `src/tcp_module.py` | Complete |
| `src/background_flood.py` | Complete |
| `tests/test_udp_module.py` | Complete — 14 tests |
| `tests/test_tcp_module.py` | Complete — 14 tests |
| `src/emulate.sh` | Not yet built |
| `src/analyze.py` | Not yet built |
| `run_experiments.sh` | Not yet built |

---

## What Was Built

The following is a complete list of everything implemented for this project so far,
including all source files, test files, and supporting documentation.

### Source files

**`src/udp_module.py`**
- `compute_jitter(arrival_times_ns)` — RFC 3550 jitter calculation from a list of arrival timestamps
- `save_result(row, filepath)` — appends one result row to the CSV, writes header if new
- `_udp_receiver(host, port, buffer_size, n_expected, results)` — background thread: binds UDP socket with SO_RCVBUF, drains datagrams via timeout loop, computes loss/jitter/OWD
- `run_udp_sender(host, port, payload_size, buffer_size, n_messages, send_rate_pps)` — rate-paced sender: validates payload size, sets SO_SNDBUF, stamps every datagram with seq_num + send timestamp
- `run_udp_experiment(payload_size, buffer_size, n_messages, send_rate_pps, label, run_index)` — orchestrator: starts receiver thread, runs sender, joins thread, saves one CSV row, returns `(throughput_mbps, loss_rate_pct, jitter_ms, avg_owd_ms)`
- `main()` — argparse CLI with `--payload`, `--buffer`, `--messages`, `--rate`, `--label`, `--run`

**`src/tcp_module.py`**
- `save_result(row, filepath)` — same CSV pattern as UDP module
- `_recv_exact(sock, n)` — loops `recv()` until exactly n bytes are read; handles TCP fragmentation
- `_throughput_server(host, port, buffer_size, results)` — background thread: sets SO_RCVBUF + SO_REUSEADDR, accepts one connection, reads until close, records elapsed time and total bytes
- `run_throughput_client(host, port, payload_size, buffer_size, n_messages)` — sets SO_SNDBUF + TCP_NODELAY, connects, sends n_messages × payload_size with sendall(), returns client-side Mbps
- `measure_throughput(payload_size, buffer_size, n_messages)` — orchestrator for throughput mode; returns server-side throughput in Mbps
- `_latency_server(host, port, buffer_size, n_pings, stop_event)` — background thread: echo server with 4-byte length-prefix framing; reads prefix → reads body → sends body back, n_pings times
- `run_latency_client(host, port, payload_size, buffer_size, n_pings)` — ping-pong client: sends `[4-byte prefix][payload]`, recvs echo, records RTT per message; returns list of RTTs in ms
- `measure_latency(payload_size, buffer_size, n_pings)` — orchestrator for latency mode; returns `(avg_rtt_ms, stdev_rtt_ms)`
- `run_tcp_experiment(payload_size, buffer_size, n_messages, label, run_index)` — calls both modes, saves one combined CSV row, returns `(throughput_mbps, avg_latency_ms, latency_stdev_ms)`
- `main()` — argparse CLI with `--payload`, `--buffer`, `--messages`, `--label`, `--run`, `--flood`

**`src/background_flood.py`**
- `_flood_server(stop_event, ready_event, port)` — background thread: TCP server on port 5400, accepts one connection, drains data until stop event is set
- `_flood_client(stop_event, ready_event, port, buffer_size)` — background thread: connects to flood server, sends 65 KB chunks as fast as the OS accepts them until stop event is set
- `start_flood(port, buffer_size)` — spawns both threads as daemons, waits 0.2 s for flood to reach full speed, returns a `threading.Event` stop handle
- `stop_flood(stop_event)` — sets the stop event, waits 0.5 s for threads to clean up
- `main()` — standalone CLI entry point; runs the flood until Ctrl+C or `kill`

### Test files

**`tests/test_udp_module.py` — 14 tests**
- 6 unit tests for `compute_jitter()`: empty list, single packet, two packets, uniform stream, alternating gaps (hand-calculated), return type assertion
- 4 unit tests for `save_result()`: file creation, header written exactly once, row appending, all CSV columns present
- 3 unit tests for `run_udp_sender()` input validation: rejects payload < 16 bytes, rejects payload = 0, accepts payload = 16 (minimum valid)
- 1 integration test for `run_udp_experiment()`: real loopback experiment — asserts throughput > 0, loss < 1%, jitter ≥ 0, OWD ≥ 0 and < 100 ms, CSV row written with correct fields

**`tests/test_tcp_module.py` — 14 tests**
- 4 unit tests for `save_result()`: same four checks as UDP
- 3 unit tests for `_recv_exact()`: full message, fragmented message (1000 bytes sent in 10-byte chunks), raises `ConnectionError` on early disconnect
- 2 integration tests for `measure_throughput()`: positive result at 512-byte and 4096-byte payloads
- 3 integration tests for `measure_latency()`: positive RTT below 100 ms, non-negative stdev, stdev = 0.0 for single ping
- 1 integration test for `run_tcp_experiment()`: end-to-end — both modes run, one CSV row written, returned values match CSV
- 1 integration test for flood comparison: `run_tcp_experiment()` returns valid positive metrics while background flood is active

### Documentation files

- `README.md` — setup instructions, module usage, congestion comparison guide, test breakdown, project structure, CSV schemas, network emulation reference
- `docs/MEASUREMENT_GAPS.md` — gap analysis comparing the proposal against the initial plan; documents the background flood and OWD additions with exact file/function references
- `docs/intermediate_report.md` — this document

---

## Completed Work

### 1. UDP Measurement Module — `src/udp_module.py`

#### What it does

The UDP module runs a paced datagram sender and a timeout-driven receiver as
concurrent threads on loopback. One invocation produces one row in
`results/udp_results.csv` containing four metrics.

#### The datagram wire format

Every datagram sent by the UDP sender has a fixed 16-byte header followed by
zero-padding to reach the requested payload size:

```
Bytes 0–7   : sequence number    (uint64, big-endian)
Bytes 8–15  : send timestamp ns  (uint64, big-endian) ← nanoseconds from perf_counter_ns()
Bytes 16+   : zero-padding       (to fill payload_size)
```

The header is encoded with Python's `struct` module using the format string
`">QQ"` — `>` means big-endian, `Q` means unsigned 64-bit integer. The minimum
valid payload size is 16 bytes (the header size). Anything smaller raises a
`ValueError` before any socket is opened.

#### Metric 1 — Throughput

Measured on the receiver side:

```
throughput_mbps = (total_bytes_received × 8) / (elapsed_s × 1,000,000)
```

`elapsed_s` is the wall time from the first datagram arriving to the last. Using
receiver-side timing (not sender-side) means the measurement reflects what was
actually delivered, not just what was put on the wire.

#### Metric 2 — Packet Loss

UDP has no retransmission. The sender stamps every datagram with a monotonically
increasing sequence number (`seq_num`). The receiver collects all sequence numbers
that arrive and counts how many are missing compared to what was expected:

```
loss_rate_pct = (n_expected − n_received) / n_expected × 100
```

Gaps in the sequence number list reveal dropped datagrams. This method also
detects reordering (a datagram arriving after the timeout window is counted as
lost even if it eventually arrives).

#### Metric 3 — Jitter

Jitter measures how irregular the inter-arrival spacing is. A perfectly uniform
stream has zero jitter; queuing and scheduling cause spacing to vary. The
implementation follows the simplified RFC 3550 §A.8 definition:

1. Compute inter-arrival gaps: `gap[i] = arrival[i] − arrival[i−1]`
2. Compute how much consecutive gaps differ: `delta[i] = |gap[i] − gap[i−1]|`
3. Jitter = mean of all deltas, converted from nanoseconds to milliseconds

At least three packets are needed to produce a non-zero result (two gaps needed
to form one delta). The function returns `0.0` for fewer than three arrivals.

#### Metric 4 — One-Way Delay (OWD)

OWD is the time it takes a datagram to travel from the sender to the receiver —
the forward path only, unlike TCP's round-trip time which includes the return
journey. It is computed per datagram as:

```
owd_ms = (arrival_ns − send_ns) / 1,000,000
```

`send_ns` comes from the datagram header (stamped by the sender just before
`sendto()` is called). `arrival_ns` is recorded by the receiver immediately after
`recvfrom()` returns. The mean across all received datagrams is stored as
`avg_owd_ms`.

**Why OWD is valid on loopback:** Both the sender and receiver are threads in the
same Python process on the same machine. `time.perf_counter_ns()` is a monotonic
clock that returns nanoseconds from a fixed reference point. Because both threads
read from the same clock, the subtraction `arrival_ns − send_ns` gives a true
elapsed time. This would **not** be valid across two separate physical machines,
where clocks drift relative to each other and would need synchronisation via PTP
or NTP to produce meaningful OWD measurements.

**What OWD reveals that RTT does not:** OWD grows as queuing delay builds up on
the forward path. Under the bufferbloat condition (slow link + large queue),
datagrams accumulate in the kernel buffer before being forwarded. OWD will
increase steadily while packet loss stays near zero — this is the defining
signature of bufferbloat, and it is visible in OWD before it shows up in
throughput or loss.

#### Design decision — rate-paced sending

The sender does not transmit datagrams as fast as possible. Instead it sleeps
`1 / send_rate_pps` seconds between each send. This is controlled by the `--rate`
flag (default: 500 packets per second).

The reason: loopback on macOS is essentially infinitely fast. An unthrottled UDP
sender can fill the kernel receive buffer before the receiver thread has read a
single datagram, producing artificial loss that has nothing to do with socket
buffer size or network conditions. Rate pacing ensures the queue builds up
gradually and the buffer-size comparison is meaningful.

#### CLI flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--payload` | int | required | Total datagram size in bytes (must be ≥ 16) |
| `--buffer` | int | required | Socket buffer size in bytes (SO_SNDBUF / SO_RCVBUF) |
| `--messages` | int | 1000 | Number of datagrams to send |
| `--rate` | int | 500 | Send rate in packets per second |
| `--label` | str | baseline | Network condition label written to the CSV |
| `--run` | int | 1 | Run index 1–3 for repeated trials |

#### Example output

```
UDP | baseline | payload=1024B | throughput=4.09 Mbps | loss=0.00% | jitter=0.12ms | owd=0.031ms
```

---

### 2. TCP Measurement Module — `src/tcp_module.py`

#### What it does

The TCP module runs two measurements back-to-back for each invocation and saves
both results in a single combined row in `results/tcp_results.csv`:

1. **Throughput mode** — bulk transfer: the client sends N messages as fast as
   possible and the server measures total bytes received over elapsed time.
2. **Latency mode** — ping-pong echo: the client sends one message and waits for
   the full echo to return before sending the next. RTT is measured per message
   and aggregated.

#### Key difference from UDP — TCP is a byte stream

UDP preserves message boundaries — each `sendto()` call produces exactly one
datagram that the receiver reads with one `recvfrom()`. TCP provides no such
guarantee. TCP is a byte stream, which means:

- The OS may coalesce two small `send()` calls into one segment.
- A single large `send()` may arrive at the receiver as multiple smaller chunks.
- There are no message boundaries at the TCP layer — the application must impose
  its own framing.

This has two consequences for the implementation:

**1. `_recv_exact(sock, n)` helper:** A simple `recv(n)` call may return fewer
than `n` bytes if the data arrived in multiple TCP segments. The `_recv_exact`
helper loops `recv()` until exactly `n` bytes have been accumulated:

```python
def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(...)
        buf.extend(chunk)
    return bytes(buf)
```

This is used in both the latency server (to read the message body) and the
latency client (to read the echo).

**2. Length-prefix framing for latency mode:** The throughput server just reads
until `recv()` returns `b""` (connection closed), so framing is not needed there.
But the latency echo server needs to know exactly how many bytes constitute one
message before echoing it. The client prepends a 4-byte big-endian unsigned
integer carrying the payload length before every message:

```
Wire format per ping:
  [4 bytes: payload_size as big-endian uint32] [payload_size bytes: message body]
```

The server reads the 4-byte prefix first to learn the length, then reads exactly
that many bytes, then echoes the body back (without the prefix). The client
reads exactly `payload_size` bytes back.

#### Metric 1 — Throughput

Measured on the **server side** (what was received), not the client side (what
was sent). The server records `start_time` when the first bytes arrive (not when
`accept()` returns — that would include connection setup time), and `end_time`
when `recv()` returns `b""` (the client closed the connection):

```
throughput_mbps = (total_bytes_received × 8) / (elapsed_s × 1,000,000)
```

#### Metric 2 — Round-Trip Time (RTT)

RTT is measured per ping as the wall time from just before `sendall(prefix + body)`
to just after `_recv_exact(payload_size)` returns:

```
rtt_ms = (t1 − t0) × 1000
```

This includes: time to write the outgoing bytes to the kernel send buffer, time
for the kernel to transmit the bytes on loopback, time for the server to read and
echo them, and time for the client to receive the full echo. The mean and standard
deviation across all `--messages` pings are stored in the CSV.

RTT is a round-trip measurement and is therefore approximately **twice** the
one-way propagation delay under symmetric conditions. Under bufferbloat, the
queue builds up in both directions, so RTT can grow much faster than OWD.

#### Design decision — TCP_NODELAY

Both the throughput client and the latency server/client set `TCP_NODELAY = 1`.
This disables **Nagle's algorithm**, which by default coalesces small writes into
larger segments, waiting up to ~200 ms for more data before transmitting. Nagle
is useful for interactive protocols on slow links but is harmful here:

- For small payloads (e.g. 64 bytes), Nagle would hold each message in a buffer
  waiting for more data, inflating every RTT measurement by up to 200 ms.
- For the throughput test, Nagle could cause the first few messages to be delayed
  even though we want to measure maximum send rate.

`TCP_NODELAY` ensures every `sendall()` call is transmitted immediately.

#### Design decision — SO_REUSEADDR

Both the throughput server and latency server set `SO_REUSEADDR = 1`. When a TCP
socket closes, the OS holds the port in a `TIME_WAIT` state for up to 60 seconds
to ensure any delayed packets from the previous connection are discarded. Without
`SO_REUSEADDR`, re-running the experiment within that window produces an "Address
already in use" error. Setting this option lets the OS immediately reuse the port.

#### The `--flood` flag — congestion comparison

The `--flood` flag is unique to the TCP module. When passed, it calls
`start_flood()` from `background_flood.py` before running the experiment and
`stop_flood()` after. This makes it possible to run two otherwise identical
experiments — one clean, one with competing traffic — and compare the output
directly:

```bash
# Baseline — no competing traffic
uv run python src/tcp_module.py --payload 1024 --buffer 65536 --label baseline --run 1

# Congested — background flood active
uv run python src/tcp_module.py --payload 1024 --buffer 65536 --label congested --run 1 --flood
```

With the flood active, TCP's AIMD congestion control reduces the congestion
window (the amount of unacknowledged data allowed in flight), which lowers
throughput and increases RTT. UDP has no such mechanism, which is why the
contrast between the two protocols under congestion is one of the key findings
the project is designed to produce.

#### CLI flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--payload` | int | required | Message size in bytes |
| `--buffer` | int | required | Socket buffer size in bytes (SO_SNDBUF / SO_RCVBUF) |
| `--messages` | int | 1000 | Number of messages (used for both throughput and latency) |
| `--label` | str | baseline | Network condition label written to the CSV |
| `--run` | int | 1 | Run index 1–3 for repeated trials |
| `--flood` | flag | off | Start background TCP flood to simulate competing traffic |

#### Example output

```
TCP | baseline | payload=1024B | throughput=3241.57 Mbps | avg_rtt=0.018ms | rtt_stdev=0.004ms
TCP | congested [flood active] | payload=1024B | throughput=812.34 Mbps | avg_rtt=0.091ms | rtt_stdev=0.023ms
```

---

### 3. Background TCP Flood — `src/background_flood.py`

#### Why it was needed

The original proposal specified: *"Introduce competing background traffic to
stress-test both protocols and observe congestion response."*

The existing plan used `dummynet` to create a `congested` condition by capping
bandwidth to 2 Mbit/s. This simulates a slow link but **does not trigger AIMD**,
because:

- AIMD (Additive Increase, Multiplicative Decrease) is TCP's congestion control
  algorithm. It increases the congestion window by one MSS per round-trip when
  there are no losses (the "additive increase" phase), and halves it when a loss
  event is detected (the "multiplicative decrease" phase).
- A loss event is detected via either a timeout or three duplicate ACKs — both
  of which occur when a packet is **dropped at a queue that has overflowed due
  to competition**.
- dummynet's `plr` (packet loss rate) drops packets at a fixed rate regardless
  of queue state. The sender sees loss but does not know it came from contention
  — the AIMD backoff happens, but it is responding to artificial loss rather than
  real queue overflow. The congestion window never experiences the feedback loop
  that characterises real multi-flow congestion.

A real competing TCP flow fills the same queue. When both flows try to fill the
queue simultaneously, it overflows and drops packets from both. Both senders
reduce their congestion windows. The experiment's flow backs off — which is
exactly the dynamic we want to measure.

#### How it works

`background_flood.py` spawns two daemon threads:

- **`_flood_server`**: binds a TCP socket on port 5400, accepts one connection,
  and reads and discards incoming bytes in a loop until the stop event is set.
- **`_flood_client`**: connects to the flood server and sends 65,535-byte chunks
  in a tight loop with no rate limiting, as fast as the OS will accept them.

The large send buffer (`SO_SNDBUF = 1 MB`) on the client ensures the OS can
queue a large backlog of bytes, keeping the link saturated continuously.

Both threads are daemon threads — they are killed automatically if the main
process exits, so the flood never keeps a process alive after the experiment ends.

The `ready_event` pattern prevents a race condition: the client thread waits on
`ready_event` before calling `connect()`, and the server thread sets `ready_event`
only after `listen()` succeeds. Without this, the client could attempt to connect
before the server has bound the socket, producing a "Connection refused" error.

#### Public API

```python
from background_flood import start_flood, stop_flood

stop_event = start_flood()   # spawns threads, returns a stop event
# ... run experiment ...
stop_flood(stop_event)       # signals threads to exit, waits for cleanup
```

It can also be run as a standalone process from shell scripts:

```bash
python3 src/background_flood.py &
FLOOD_PID=$!
# ... run experiment ...
kill $FLOOD_PID
```

#### Port

Port 5400 is used, separate from all measurement ports (5201, 5202, 5301) so
the flood traffic never appears in the experiment's measurements.

---

### 4. Test Suite — 28 Tests, All Passing

The test suite lives in `tests/` and is run with `uv run pytest tests/ -v`.
Tests are split by module and further divided into unit tests (no sockets, no
threads) and integration tests (real loopback connections).

#### UDP tests — `tests/test_udp_module.py` (14 tests)

| Group | Count | What is verified |
|-------|-------|-----------------|
| `test_jitter_*` | 6 | `compute_jitter()`: empty list returns 0.0, single packet returns 0.0, two packets returns 0.0, uniform stream returns 0.0, alternating gaps match hand-calculated value of 10.0 ms, return type is float |
| `test_save_result_*` | 4 | File is created when it doesn't exist, header row appears exactly once across multiple calls, rows are appended in order, all CSV_HEADER columns are present |
| `test_sender_*` | 3 | `run_udp_sender()` raises ValueError for payload < 16, raises ValueError for payload = 0, accepts exactly 16 (minimum valid) |
| Integration | 1 | `run_udp_experiment()`: throughput > 0, loss < 1%, jitter ≥ 0, OWD ≥ 0 and < 100 ms, CSV written with correct fields |

The integration test sends 50 datagrams at 200 pps, taking approximately 2.5
seconds (0.25 s sending + 2 s receiver timeout).

#### TCP tests — `tests/test_tcp_module.py` (14 tests)

| Group | Count | What is verified |
|-------|-------|-----------------|
| `test_tcp_save_result_*` | 4 | Same four CSV checks as UDP: file creation, single header, appending, column completeness |
| `test_recv_exact_*` | 3 | Full message received correctly, fragmented message (1000 bytes sent in 10-byte chunks) reassembled correctly, `ConnectionError` raised when the connection closes before all bytes arrive |
| `test_measure_throughput_*` | 2 | Positive throughput at 512-byte payload, positive throughput at 4096-byte payload |
| `test_measure_latency_*` | 3 | Positive RTT on loopback and below 100 ms, non-negative stdev, stdev = 0.0 when exactly 1 ping is sent |
| `test_run_tcp_experiment_basic` | 1 | End-to-end: both measurements run, one CSV row written with all fields, returned values match CSV |
| `test_run_tcp_experiment_with_flood` | 1 | Experiment still returns valid positive metrics when the background flood is active |

The `_recv_exact` fragmentation test is particularly important — it creates a
real loopback socket pair, sends data in 10-byte chunks from a background thread,
and verifies that `_recv_exact(sock, 1000)` correctly accumulates all fragments
before returning.

#### Testing philosophy

All integration tests redirect CSV output to a `tempfile.NamedTemporaryFile` by
patching `tcp_module.RESULTS_PATH` / `udp_module.RESULTS_PATH` before calling
the experiment function. This keeps tests isolated — running the test suite never
touches `results/tcp_results.csv` or `results/udp_results.csv`.

The redirect works because module-level variable references inside function bodies
are looked up in the module's global namespace **at call time**, not at
function-definition time. Patching the module attribute after import (e.g.
`tcp_module.RESULTS_PATH = tmp_path`) causes the lookup inside `run_tcp_experiment`
to find the patched value. Default parameter values (e.g. `filepath=RESULTS_PATH`)
are captured at definition time and cannot be patched this way — which is why
`run_tcp_experiment` passes `filepath=RESULTS_PATH` explicitly rather than relying
on the default.

---

## Design Decisions

### macOS vs. Linux network emulation

The proposal referenced `tc`/`netem` for network emulation. These are Linux tools
that hook into the kernel's traffic control subsystem. macOS is BSD-based and does
not include them. The macOS equivalent is `dummynet`, accessed via `dnctl` and
`pfctl`. It supports the same four knobs needed for this project:

| Parameter | dummynet syntax | Effect |
|-----------|----------------|--------|
| Propagation delay | `delay 50` | Adds 50 ms one-way delay to all packets on the interface |
| Bandwidth cap | `bw 1Mbit/s` | Limits throughput to 1 Mbit/s — simulates a slow link |
| Queue depth | `queue 1000` | Allows up to 1000 packets to queue before dropping — simulates bufferbloat |
| Packet loss | `plr 0.05` | Randomly drops 5% of packets — simulates a lossy link |

All conditions are applied to the loopback interface (`lo0`). The commands require
`sudo` because they modify kernel packet-filtering rules.

### SO_RCVBUF vs SO_SNDBUF — choosing the right buffer option

TCP and UDP both expose two kernel-side socket buffers:

- **SO_SNDBUF** (send buffer): bytes the OS queues after `send()` / `sendto()`
  returns, waiting to be transmitted. Setting this on the **sender** controls how
  much data can be in flight from the application to the network layer.
- **SO_RCVBUF** (receive buffer): bytes the OS queues after they arrive from the
  network, waiting for the application to call `recv()`. Setting this on the
  **receiver** controls how much data can queue up before the OS must either
  block the sender (TCP) or drop datagrams (UDP).

The original UDP stub set `SO_SNDBUF` on the receiver, which had no effect on
receive-side queuing. The implementation correctly sets `SO_RCVBUF` on the
receiver socket and `SO_SNDBUF` on the sender socket.

For the bufferbloat experiment, inflating `SO_RCVBUF` is the primary lever — a
large receive buffer lets the OS queue many more bytes before the application
reads them, increasing queuing latency (which is the definition of bufferbloat).

### Why two ports for TCP

The TCP module uses port 5201 for throughput and port 5202 for latency. Running
them on the same port sequentially would risk a `TIME_WAIT` collision: after the
throughput test closes, the port enters a `TIME_WAIT` state for up to 60 seconds.
Even with `SO_REUSEADDR`, immediately re-binding can occasionally fail on some OS
versions. Using separate ports eliminates this race entirely.

### The `--run` flag and statistical averaging

A single run can be noisy due to OS scheduling, CPU load, and kernel timer
resolution. The `--run` flag (1, 2, or 3) doesn't change experiment behaviour —
it just labels the row so `analyze.py` can group the three runs and compute
mean ± standard deviation across them. Every configuration is intended to be run
three times before the results are considered reliable.

### Shared CSV architecture

Each module appends rows to its own CSV file rather than holding results in
memory. This means:
- Partial runs (e.g. a crash halfway through the sweep) still produce useful data
- The CSV can be inspected at any time during a long experiment run
- Multiple processes could write to the same file safely because each write is a
  single `writerow()` call (atomic at the Python level for typical row sizes)

The header is written only if the file is new or empty, checked via
`os.path.getsize()`. This prevents duplicate headers if the file grows over
multiple experiment sessions.

---

## Remaining Work

| Component | Description | Depends on |
|-----------|-------------|------------|
| `src/emulate.sh` | Shell script that applies/tears down each of the five named dummynet conditions | dummynet verified working |
| `run_experiments.sh` | Outer loop: 6 payload sizes × 4 buffer sizes × 5 conditions × 3 runs × 2 protocols = 720 experiment cells | `emulate.sh`, both modules |
| `src/analyze.py` | Loads both CSVs, groups by (payload, buffer, condition), computes mean ± stdev, generates 5 plots | Real experiment data |

### Planned plots

1. **Throughput vs. payload size** — TCP and UDP side-by-side, baseline condition, error bars ± stdev
2. **RTT latency vs. payload size** — TCP, all five conditions, shows bufferbloat spike
3. **Packet loss vs. payload size** — UDP, lossy and congested conditions
4. **Jitter vs. buffer size** — UDP, all conditions, shows how larger buffers increase jitter
5. **TCP vs. UDP throughput under congestion** — grouped bar chart per payload size

### Estimated remaining timeline

| Week | Milestone |
|------|-----------|
| Week 1 | `src/emulate.sh` — implement and test all five dummynet conditions |
| Week 1 | `run_experiments.sh` — implement full sweep, verify CSV output |
| Week 2 | Run all 720 experiment cells, collect CSVs |
| Week 2 | `src/analyze.py` — aggregation pipeline and all five plots |
| Week 3 | Report writing — analysis, discussion, figures |
