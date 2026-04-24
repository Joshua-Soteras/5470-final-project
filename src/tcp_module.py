"""
TCP Measurement Module
----------------------
Runs a TCP sender/receiver pair on localhost (loopback, 127.0.0.1).
Measures two performance metrics per experiment:
  - Throughput    : how many megabits per second the receiver absorbed (bulk transfer)
  - Round-trip latency (RTT) : how long a ping-pong message round-trip takes (ms)

Two modes run per invocation:
  1. Throughput mode  — client sends N messages as fast as possible; server measures
                        total bytes received and elapsed time.
  2. Latency mode     — client sends one message and waits for an echo before sending
                        the next; RTT is measured per message and averaged.

Key difference from UDP: TCP guarantees delivery and ordering. There is no packet
loss or jitter to measure. Instead the interesting variables are how buffer size
affects throughput (bufferbloat) and how congestion from competing traffic causes
AIMD backoff, which raises latency and reduces throughput.

Flood comparison
----------------
Pass --flood on the CLI to start a background TCP flood (background_flood.py) that
competes for the same loopback bandwidth before running the experiment. Compare the
output with and without --flood to directly observe AIMD congestion control behaviour.

    # Baseline (no competing traffic)
    uv run python src/tcp_module.py --payload 1024 --buffer 65536 --label baseline --run 1

    # Congested (background flood active during the experiment)
    uv run python src/tcp_module.py --payload 1024 --buffer 65536 --label congested --run 1 --flood

TCP framing (latency mode only)
--------------------------------
Because TCP is a byte stream with no message boundaries, the latency echo server
needs to know how many bytes to wait for before echoing. The client prefixes every
message with a 4-byte big-endian unsigned int (the payload length) so the server can
read exactly that many bytes before replying.

Wire format per ping:
    [4 bytes: payload_size as big-endian uint32] [payload_size bytes: message body]

The server strips the prefix, echoes the body only, and the client recvs exactly
payload_size bytes back.

Usage (via uv):
    uv run python src/tcp_module.py --payload 1024 --buffer 65536 \
                                    --messages 1000 --label baseline --run 1
"""

import socket       # BSD socket API — creates TCP/UDP sockets
import time         # time.perf_counter() — sub-microsecond resolution clock
import threading    # Thread, Event — runs server concurrently with client
import csv          # DictWriter — writes rows to CSV files
import os           # path helpers, makedirs
import struct       # pack/unpack — encodes the 4-byte length prefix
import statistics   # mean(), stdev() — aggregates RTT samples
import argparse     # ArgumentParser — parses command-line flags

# ─── Constants ────────────────────────────────────────────────────────────────

HOST = "127.0.0.1"      # loopback — all traffic stays on this machine

THROUGHPUT_PORT = 5201  # port for the bulk-transfer (throughput) server
LATENCY_PORT    = 5202  # port for the echo (latency) server

# 4-byte big-endian unsigned int: ">I" = big-endian, "I" = uint32
# Prepended to every latency message so the echo server knows the body length.
LENGTH_FMT  = ">I"
LENGTH_SIZE = struct.calcsize(LENGTH_FMT)   # 4 bytes

# Path to the output CSV, relative to this file's location (src/).
# os.path.dirname(__file__) = src/, ".." = project root, then results/.
RESULTS_PATH = os.path.join(os.path.dirname(__file__), "..", "results", "tcp_results.csv")

# Column names for the CSV — every row must have exactly these keys.
CSV_HEADER = [
    "protocol",           # always "TCP" for this module
    "payload_bytes",      # message size in bytes
    "buffer_bytes",       # socket buffer size that was requested
    "condition",          # network condition label, e.g. "baseline" or "congested"
    "throughput_mbps",    # measured goodput in megabits per second
    "avg_latency_ms",     # mean round-trip time in milliseconds
    "latency_stdev_ms",   # standard deviation of RTT samples in milliseconds
    "run_index",          # which repetition this is (1, 2, or 3)
]

# ─── CSV Helper ───────────────────────────────────────────────────────────────

def save_result(row: dict, filepath: str = RESULTS_PATH) -> None:
    """
    Appends one result row to the CSV file at `filepath`.
    Writes the header line automatically if the file does not yet exist
    or is empty — so the first call creates the file and every subsequent
    call just appends a data row.

    Parameters
    ----------
    row : dict
        Dictionary whose keys match CSV_HEADER exactly.
    filepath : str
        Destination CSV path. Defaults to RESULTS_PATH (results/tcp_results.csv).
        Override in tests to write to a temporary file.
    """
    # Create results/ directory if it does not exist yet.
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    # Only write the header row if the file is new or empty.
    write_header = not os.path.exists(filepath) or os.path.getsize(filepath) == 0

    # Open in append mode so previous runs are never overwritten.
    # newline="" is required by the csv module on all platforms.
    with open(filepath, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        if write_header:
            writer.writeheader()
        writer.writerow(row)

# ─── Private Helper ───────────────────────────────────────────────────────────

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """
    Reads exactly `n` bytes from `sock`, blocking until all bytes arrive.

    TCP is a byte stream — a single recv() call may return fewer bytes than
    requested if the data arrives in multiple segments. This helper loops
    until the full `n` bytes have been accumulated.

    Parameters
    ----------
    sock : socket.socket
        A connected TCP socket to read from.
    n : int
        Exact number of bytes to read.

    Returns
    -------
    bytes
        Exactly `n` bytes read from the socket.

    Raises
    ------
    ConnectionError
        If the connection closes before `n` bytes are received (the remote
        side disconnected unexpectedly mid-message).
    """
    # bytearray is mutable and efficient for incremental appending.
    buf = bytearray()
    while len(buf) < n:
        # recv(n - len(buf)) asks for exactly as many bytes as are still missing.
        chunk = sock.recv(n - len(buf))
        if not chunk:
            # recv() returns b"" when the remote side closes the connection.
            raise ConnectionError(
                f"Connection closed after {len(buf)} bytes; expected {n}"
            )
        buf.extend(chunk)
    return bytes(buf)

# ─── Throughput Mode ──────────────────────────────────────────────────────────

def _throughput_server(host: str, port: int, buffer_size: int, results: dict) -> None:
    """
    Runs in a background daemon thread. Binds a TCP socket, accepts one
    connection, reads all bytes until the client closes the connection,
    then records timing and total bytes into the shared `results` dict.

    Parameters
    ----------
    host : str
        IP address to bind to. "127.0.0.1" for loopback.
    port : int
        TCP port to listen on. Must match what the client connects to.
    buffer_size : int
        Requested kernel receive-buffer size in bytes (SO_RCVBUF).
        Larger values allow the OS to queue more unread bytes, which reduces
        back-pressure on the sender and can increase throughput.
    results : dict
        Shared dictionary written by this thread and read by the orchestrator
        after the thread exits. Keys set:
            'total_bytes' — int,   total payload bytes received
            'elapsed_s'   — float, wall time from first byte to connection close
    """
    # AF_INET = IPv4; SOCK_STREAM = TCP (reliable ordered byte stream).
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # SO_REUSEADDR lets the OS immediately reuse this port after the previous
    # run closed it. Without this, "Address already in use" errors appear when
    # re-running experiments quickly.
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # SO_RCVBUF sets the kernel receive buffer. A large buffer means the OS
    # can hold more unacknowledged bytes, keeping the sender from stalling.
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, buffer_size)

    server_sock.bind((host, port))

    # listen(1) prepares the socket to accept incoming connections.
    # The argument is the backlog — max pending connections before the OS
    # starts refusing. 1 is enough since we expect exactly one client.
    server_sock.listen(1)

    # accept() blocks until a client connects. Returns (connection_socket, address).
    conn, _ = server_sock.accept()

    total_bytes = 0
    start_time: float | None = None

    try:
        while True:
            # recv(65536) reads up to 65536 bytes per call.
            # Returns b"" when the client closes its side of the connection.
            chunk = conn.recv(65536)
            if not chunk:
                break   # client called close() — transfer is done

            # Record the start time on the first chunk, not on accept(),
            # so elapsed_s reflects data transfer time only.
            if start_time is None:
                start_time = time.perf_counter()

            total_bytes += len(chunk)
    finally:
        conn.close()
        server_sock.close()

    end_time = time.perf_counter()

    results["total_bytes"] = total_bytes
    results["elapsed_s"]   = (end_time - start_time) if start_time is not None else 0.0


def run_throughput_client(host: str, port: int, payload_size: int,
                          buffer_size: int, n_messages: int) -> float:
    """
    Sends `n_messages` messages of `payload_size` bytes each over TCP and
    returns the measured client-side throughput in Mbps.

    Parameters
    ----------
    host : str
        Destination IP address. "127.0.0.1" to target the local server.
    port : int
        Destination TCP port. Must match what the server is listening on.
    payload_size : int
        Size of each message in bytes.
    buffer_size : int
        Requested kernel send-buffer size in bytes (SO_SNDBUF). Larger values
        let the OS accept more bytes from sendall() before blocking, which
        allows the sender to stay ahead of the network.
    n_messages : int
        Number of messages to send (e.g. 1000).

    Returns
    -------
    float
        Client-side throughput in Mbps: total bytes sent / elapsed send time.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # SO_SNDBUF — kernel send buffer. See note above on SO_RCVBUF.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, buffer_size)

    # TCP_NODELAY disables Nagle's algorithm, which by default coalesces small
    # writes into larger segments to improve throughput. For small payloads
    # Nagle adds up to 200 ms of buffering delay — disabling it ensures each
    # sendall() is transmitted immediately. Important for latency measurements
    # and for accurately observing per-message behaviour at small payload sizes.
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    sock.connect((host, port))

    # Build the payload once outside the loop — allocating bytes in a hot loop
    # would add GC pressure and inflate the measured send time.
    payload = b"x" * payload_size

    total_bytes_sent = 0
    start_time = time.perf_counter()   # start timer just before the send loop

    try:
        for _ in range(n_messages):
            # sendall() guarantees the entire buffer is sent, retrying internally
            # if the OS only accepts a partial write. Unlike send(), it never
            # returns before all bytes are queued.
            sock.sendall(payload)
            total_bytes_sent += payload_size
    finally:
        # Closing the socket sends a FIN packet, which causes the server's
        # recv() to return b"" — signalling the transfer is complete.
        sock.close()

    elapsed_s = time.perf_counter() - start_time

    if elapsed_s > 0:
        return (total_bytes_sent * 8) / (elapsed_s * 1e6)   # bits / (s * 1e6) = Mbps
    return 0.0


def measure_throughput(payload_size: int, buffer_size: int,
                       n_messages: int) -> float:
    """
    Coordinates the throughput server and client on loopback. Returns the
    server-side measured throughput in Mbps.

    Uses server-side timing (from first byte received to connection close)
    rather than client-side timing to measure actual delivered throughput,
    not just send rate.

    Parameters
    ----------
    payload_size : int
        Message size in bytes for this experiment cell.
    buffer_size : int
        Socket buffer size in bytes (applied to both SO_SNDBUF and SO_RCVBUF).
    n_messages : int
        Number of messages to send.

    Returns
    -------
    float
        Server-side throughput in Mbps.
    """
    results: dict = {}

    # Start the server in a daemon thread so it is ready to accept before
    # the client calls connect(). daemon=True means the thread is killed
    # automatically if the main process exits unexpectedly.
    server_thread = threading.Thread(
        target=_throughput_server,
        args=(HOST, THROUGHPUT_PORT, buffer_size, results),
        daemon=True,
    )
    server_thread.start()

    # Brief pause to let the server reach listen() before the client connects.
    time.sleep(0.05)

    # Run the client. This blocks the calling thread until all sends complete
    # and the socket is closed.
    run_throughput_client(HOST, THROUGHPUT_PORT, payload_size, buffer_size, n_messages)

    # Wait up to 10 seconds for the server to finish draining its recv buffer.
    server_thread.join(timeout=10.0)

    # Compute throughput from the server's perspective — bytes that actually
    # arrived at the receiver, not bytes queued by the sender.
    elapsed_s   = results.get("elapsed_s", 0.0)
    total_bytes = results.get("total_bytes", 0)

    if elapsed_s > 0:
        return (total_bytes * 8) / (elapsed_s * 1e6)
    return 0.0

# ─── Latency (Ping-Pong) Mode ─────────────────────────────────────────────────

def _latency_server(host: str, port: int, buffer_size: int,
                    n_pings: int, stop_event: threading.Event) -> None:
    """
    Runs in a background daemon thread. Accepts one connection and echoes
    exactly `n_pings` messages back to the client.

    Protocol:
      1. Receive the 4-byte length prefix to find out how many bytes follow.
      2. Receive exactly that many bytes (the message body).
      3. Send the body back unchanged (no prefix on the echo).
      4. Repeat n_pings times, then close.

    Parameters
    ----------
    host : str
        IP address to bind to.
    port : int
        TCP port to listen on.
    buffer_size : int
        Requested SO_RCVBUF size in bytes.
    n_pings : int
        Number of messages to echo. Must match what the client expects to send.
    stop_event : threading.Event
        Set by the orchestrator after the client finishes. Checked between pings
        so the thread exits promptly if the client disconnects early.
    """
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, buffer_size)

    # TCP_NODELAY on the server ensures the echo is sent immediately without
    # waiting for Nagle to coalesce it with hypothetical future writes.
    server_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    server_sock.bind((host, port))
    server_sock.listen(1)

    conn, _ = server_sock.accept()

    try:
        for _ in range(n_pings):
            if stop_event.is_set():
                break   # orchestrator signalled early exit

            # Step 1: read the 4-byte length prefix.
            # _recv_exact() handles the case where TCP delivers the 4 bytes
            # as multiple smaller chunks.
            raw_len = _recv_exact(conn, LENGTH_SIZE)

            # struct.unpack returns a tuple; [0] extracts the single integer.
            # ">I" = big-endian unsigned 32-bit int.
            msg_len = struct.unpack(LENGTH_FMT, raw_len)[0]

            # Step 2: read exactly msg_len bytes (the message body).
            body = _recv_exact(conn, msg_len)

            # Step 3: echo the body back without the prefix.
            conn.sendall(body)
    finally:
        conn.close()
        server_sock.close()


def run_latency_client(host: str, port: int, payload_size: int,
                       buffer_size: int, n_pings: int) -> list[float]:
    """
    Sends `n_pings` messages to the echo server and records the round-trip
    time (RTT) for each. Returns a list of RTTs in milliseconds.

    RTT is measured as: t_after_full_echo_received − t_before_send.
    This includes the time to send the prefix + body, the server's receive
    and send processing, and the time to receive the full echo.

    Parameters
    ----------
    host : str
        Destination IP address.
    port : int
        Destination TCP port — the latency server's port.
    payload_size : int
        Size of each message body in bytes. The actual bytes sent per ping are
        payload_size + 4 (body + length prefix).
    buffer_size : int
        Requested SO_SNDBUF size in bytes.
    n_pings : int
        Number of round-trips to measure.

    Returns
    -------
    list[float]
        RTT in milliseconds for each ping, in send order.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, buffer_size)

    # TCP_NODELAY is critical here — without it, Nagle's algorithm can hold
    # the small prefix+body in a buffer for up to 200 ms waiting to combine
    # it with the next write, which would inflate every RTT measurement.
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    sock.connect((host, port))

    # Build the fixed parts of the message outside the loop.
    # LENGTH_FMT = ">I": big-endian uint32 carrying the payload size.
    length_prefix = struct.pack(LENGTH_FMT, payload_size)
    payload = b"x" * payload_size

    rtt_list: list[float] = []

    try:
        for _ in range(n_pings):
            # Record t0 just before the send so the timer covers the full RTT.
            # time.perf_counter() has sub-microsecond resolution on macOS.
            t0 = time.perf_counter()

            # Send the framed message: [4-byte prefix][payload_size bytes].
            # sendall() ensures the entire buffer is transmitted before returning.
            sock.sendall(length_prefix + payload)

            # Receive exactly payload_size bytes (the echo, no prefix).
            # _recv_exact() loops until all bytes arrive, handling TCP fragmentation.
            _recv_exact(sock, payload_size)

            t1 = time.perf_counter()

            # Convert seconds to milliseconds: (t1 - t0) * 1000.
            rtt_list.append((t1 - t0) * 1000.0)
    finally:
        sock.close()

    return rtt_list


def measure_latency(payload_size: int, buffer_size: int,
                    n_pings: int) -> tuple[float, float]:
    """
    Coordinates the echo server and ping-pong client on loopback. Returns
    the mean and standard deviation of all RTT samples.

    Parameters
    ----------
    payload_size : int
        Message body size in bytes.
    buffer_size : int
        Socket buffer size in bytes.
    n_pings : int
        Number of round-trips to measure. More pings = more stable average.

    Returns
    -------
    tuple[float, float]
        (avg_latency_ms, latency_stdev_ms)
        stdev is 0.0 if fewer than 2 pings were completed.
    """
    # threading.Event() is a simple boolean flag shared between threads.
    # The orchestrator sets it after the client finishes to signal the
    # server thread to exit its accept loop and clean up.
    stop_event = threading.Event()

    server_thread = threading.Thread(
        target=_latency_server,
        args=(HOST, LATENCY_PORT, buffer_size, n_pings, stop_event),
        daemon=True,
    )
    server_thread.start()

    time.sleep(0.05)   # let server reach listen() before client connects

    rtt_list = run_latency_client(HOST, LATENCY_PORT, payload_size, buffer_size, n_pings)

    # Signal the server to stop and wait up to 5 seconds for it to clean up.
    stop_event.set()
    server_thread.join(timeout=5.0)

    if len(rtt_list) < 2:
        # statistics.stdev() requires at least 2 samples — return 0 if we got fewer.
        avg   = rtt_list[0] if rtt_list else 0.0
        stdev = 0.0
    else:
        avg   = statistics.mean(rtt_list)
        stdev = statistics.stdev(rtt_list)

    return avg, stdev

# ─── Orchestrator ─────────────────────────────────────────────────────────────

def run_tcp_experiment(payload_size: int, buffer_size: int, n_messages: int,
                       label: str, run_index: int) -> tuple[float, float, float]:
    """
    Runs both throughput and latency measurements for one experiment cell,
    saves a single combined row to the CSV, and returns all three metrics.

    This mirrors run_udp_experiment() in udp_module.py — one call produces
    one CSV row with all fields populated.

    Parameters
    ----------
    payload_size : int
        Message size in bytes. Varied across the experiment sweep.
    buffer_size : int
        Socket buffer size in bytes. Varied across the experiment sweep.
    n_messages : int
        Number of messages for throughput mode. Also used as n_pings for
        latency mode so the two modes run the same volume.
    label : str
        Network condition label written to the CSV (e.g. "baseline", "congested").
    run_index : int
        Which repetition this is (1, 2, or 3). Used to average results later.

    Returns
    -------
    tuple[float, float, float]
        (throughput_mbps, avg_latency_ms, latency_stdev_ms)
    """
    # Run throughput measurement first — bulk transfer, server-side timing.
    throughput_mbps = measure_throughput(payload_size, buffer_size, n_messages)

    # Run latency measurement second — ping-pong echo, per-message RTT.
    avg_latency_ms, latency_stdev_ms = measure_latency(payload_size, buffer_size, n_messages)

    # Save one combined row to the CSV.
    # filepath=RESULTS_PATH is passed explicitly (not using the default parameter)
    # so that patching tcp_module.RESULTS_PATH in tests redirects output correctly.
    # Python looks up the name RESULTS_PATH in module globals at call time,
    # not at function-definition time, so the patch takes effect here.
    save_result({
        "protocol":          "TCP",
        "payload_bytes":     payload_size,
        "buffer_bytes":      buffer_size,
        "condition":         label,
        "throughput_mbps":   round(throughput_mbps, 4),
        "avg_latency_ms":    round(avg_latency_ms, 4),
        "latency_stdev_ms":  round(latency_stdev_ms, 4),
        "run_index":         run_index,
    }, filepath=RESULTS_PATH)

    return throughput_mbps, avg_latency_ms, latency_stdev_ms

# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    """
    Command-line entry point. Parses arguments, optionally starts a background
    TCP flood, runs one experiment, and prints a two-line summary.

    The --flood flag starts background_flood.py's flood before the experiment
    and stops it after. Run the same configuration with and without --flood
    to compare AIMD congestion behaviour:

        # Baseline — no competing traffic
        uv run python src/tcp_module.py --payload 1024 --buffer 65536 --label baseline --run 1

        # Congested — background flood active during the experiment
        uv run python src/tcp_module.py --payload 1024 --buffer 65536 --label congested --run 1 --flood
    """
    parser = argparse.ArgumentParser(description="TCP measurement module")

    parser.add_argument("--payload",  type=int, required=True,
                        help="Message size in bytes")
    parser.add_argument("--buffer",   type=int, required=True,
                        help="Socket buffer size in bytes (SO_SNDBUF / SO_RCVBUF)")
    parser.add_argument("--messages", type=int, default=1000,
                        help="Number of messages to send (default: 1000)")
    parser.add_argument("--label",    type=str, default="baseline",
                        help="Network condition label written to CSV (default: baseline)")
    parser.add_argument("--run",      type=int, default=1,
                        help="Run index 1-3 for repeated trials (default: 1)")

    # store_true means --flood sets args.flood = True; omitting it gives False.
    # No value is needed after the flag — just passing --flood is enough.
    parser.add_argument("--flood",    action="store_true",
                        help="Start a background TCP flood to simulate competing traffic")

    args = parser.parse_args()

    # ── Optional flood ──────────────────────────────────────────────────────
    # Import background_flood only when needed — keeps the module lightweight
    # for the common (no-flood) case and avoids a hard dependency.
    flood_stop = None
    if args.flood:
        from background_flood import start_flood
        flood_stop = start_flood()
        print(f"Background flood started on port 5400 — competing for loopback bandwidth")

    try:
        throughput, avg_lat, stdev_lat = run_tcp_experiment(
            payload_size = args.payload,
            buffer_size  = args.buffer,
            n_messages   = args.messages,
            label        = args.label,
            run_index    = args.run,
        )
    finally:
        # Always stop the flood, even if the experiment raised an exception.
        if flood_stop is not None:
            from background_flood import stop_flood
            stop_flood(flood_stop)

    flood_note = " [flood active]" if args.flood else ""
    print(
        f"TCP | {args.label}{flood_note} | payload={args.payload}B | "
        f"throughput={throughput:.2f} Mbps | "
        f"avg_rtt={avg_lat:.3f}ms | rtt_stdev={stdev_lat:.3f}ms"
    )


if __name__ == "__main__":
    main()
