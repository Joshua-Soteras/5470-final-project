## What Was Built

To see details on what implement, refer to the GitHub Repository. 

- https://github.com/Joshua-Soteras/5470-final-project

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
