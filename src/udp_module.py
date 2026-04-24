"""
UDP Measurement Module
----------------------
Runs a UDP sender/receiver pair on localhost (loopback, 127.0.0.1).
Measures three performance metrics:
  - Throughput  : how many megabits per second arrived at the receiver
  - Packet loss : what percentage of sent datagrams never arrived
  - Jitter      : how irregular the inter-arrival gaps were (RFC 3550 definition)

Key difference from TCP: UDP is connectionless and has no delivery guarantee.
There is no retransmission, no ordering, and no flow control built in.
That is why we manually stamp every datagram with a sequence number and
send-timestamp — so the receiver can detect gaps (loss) and measure how
consistently datagrams are arriving (jitter).

Usage (via uv):
    uv run python src/udp_module.py --payload 1024 --buffer 65536 \
                                    --messages 1000 --rate 500 \
                                    --label baseline --run 1

Datagram wire format (all fields big-endian, defined by HEADER_FMT):
    Bytes 0-7  : sequence number (uint64) — monotonically increasing counter
    Bytes 8-15 : send timestamp in nanoseconds (uint64) — from perf_counter_ns()
    Bytes 16+  : zero-padding to fill out the requested payload_size
"""

import socket       # BSD socket API — creates UDP/TCP sockets
import time         # time.perf_counter_ns() — nanosecond-resolution clock
import threading    # Thread — runs receiver concurrently with sender
import csv          # DictWriter — writes rows to CSV files
import os           # path helpers, makedirs
import struct       # pack/unpack — converts Python ints to raw bytes and back
import statistics   # statistics.mean() — average of a list
import argparse     # ArgumentParser — parses command-line flags

# ─── Constants ────────────────────────────────────────────────────────────────

HOST = "127.0.0.1"   # loopback address — stays on this machine, never hits NIC

RECV_PORT = 5301     # UDP port the receiver binds to; sender sends to this port

# struct format string: ">" = big-endian, "Q" = unsigned 64-bit int (8 bytes)
# Two Q fields = 16 bytes total: [seq_num (8)] + [send_ns (8)]
HEADER_FMT = ">QQ"

# struct.calcsize returns the byte-length of a format string.
# We use this to know how much to unpack and how much padding to add.
HEADER_SIZE = struct.calcsize(HEADER_FMT)   # 16 bytes

# How long the receiver waits for the next datagram before deciding the
# transfer is done. 2 seconds gives plenty of headroom even at low send rates.
RECV_TIMEOUT = 2.0   # seconds

# Path to the output CSV, relative to this file's location.
# os.path.dirname(__file__) gives the directory containing this script (src/).
# ".." steps up one level to the project root, then down into results/.
RESULTS_PATH = os.path.join(os.path.dirname(__file__), "..", "results", "udp_results.csv")

# Column names for the CSV — every row must have exactly these keys.
CSV_HEADER = [
    "protocol",        # always "UDP" for this module
    "payload_bytes",   # datagram size in bytes (including the 16-byte header)
    "buffer_bytes",    # socket buffer size that was requested
    "condition",       # network condition label, e.g. "baseline" or "lossy"
    "throughput_mbps", # measured goodput in megabits per second
    "loss_rate_pct",   # percentage of datagrams that never arrived
    "jitter_ms",       # mean absolute deviation of inter-arrival gaps (ms)
    "run_index",       # which repetition this is (1, 2, or 3)
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
        A dictionary whose keys match CSV_HEADER exactly.
        Example: {"protocol": "UDP", "payload_bytes": 1024, ...}
    filepath : str
        Destination CSV path. Defaults to RESULTS_PATH (results/udp_results.csv).
        Override this in tests to write to a temporary file.
    """
    # Create the results/ directory if it does not exist yet.
    # exist_ok=True means "don't raise an error if it already exists".
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    # Check whether the file is new or empty so we know whether to write
    # the header row. os.path.exists returns False if the path doesn't exist;
    # os.path.getsize returns 0 for an empty file.
    write_header = not os.path.exists(filepath) or os.path.getsize(filepath) == 0

    # Open in append mode ("a") so we never overwrite previous runs.
    # newline="" is required by the csv module on all platforms.
    with open(filepath, "a", newline="") as f:
        # DictWriter maps dictionary keys to CSV columns in the order of fieldnames.
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        if write_header:
            writer.writeheader()   # writes the column name row
        writer.writerow(row)       # writes the data row

# ─── Jitter Helper ────────────────────────────────────────────────────────────

def compute_jitter(arrival_times_ns: list[int]) -> float:
    """
    Computes jitter in milliseconds using a simplified RFC 3550 (RTP) definition.

    Jitter measures how *irregular* the spacing between arriving packets is.
    A perfectly uniform stream has jitter = 0. Network queuing and scheduling
    cause gaps to vary, producing non-zero jitter.

    Algorithm (RFC 3550 §A.8, simplified):
      1. Compute inter-arrival gaps: gap[i] = arrival[i] - arrival[i-1]
      2. Compute how much each consecutive gap changed: delta[i] = |gap[i] - gap[i-1]|
      3. Jitter = mean(delta) converted from nanoseconds to milliseconds

    Parameters
    ----------
    arrival_times_ns : list[int]
        Ordered list of packet arrival timestamps in nanoseconds,
        as returned by time.perf_counter_ns().
        Must have at least 2 entries to produce a meaningful result.

    Returns
    -------
    float
        Jitter in milliseconds. Returns 0.0 if fewer than 2 packets arrived
        (not enough data to compute gaps) or fewer than 3 packets arrived
        (not enough gaps to compute gap deltas).
    """
    # Need at least 2 arrivals to form one gap, and at least 3 to form one delta.
    if len(arrival_times_ns) < 3:
        return 0.0

    # Step 1: inter-arrival gaps in nanoseconds.
    # gaps[0] = arrival[1] - arrival[0], gaps[1] = arrival[2] - arrival[1], ...
    gaps = [
        arrival_times_ns[i] - arrival_times_ns[i - 1]
        for i in range(1, len(arrival_times_ns))
    ]

    # Step 2: absolute change between consecutive gaps.
    # This captures how much the spacing varies from one packet to the next.
    jitter_samples = [
        abs(gaps[i] - gaps[i - 1])
        for i in range(1, len(gaps))
    ]

    # Step 3: mean of the deltas, converted from nanoseconds to milliseconds.
    # 1 ms = 1,000,000 ns, so divide by 1e6.
    return statistics.mean(jitter_samples) / 1e6

# ─── Receiver ─────────────────────────────────────────────────────────────────

def _udp_receiver(host: str, port: int, buffer_size: int,
                  n_expected: int, results: dict) -> None:
    """
    Runs in a background daemon thread. Binds a UDP socket, receives datagrams
    until RECV_TIMEOUT seconds pass with no new data, then computes metrics
    and stores them in the shared `results` dictionary.

    The leading underscore in the name is a Python convention meaning
    "internal / private" — callers should use run_udp_experiment() instead.

    Parameters
    ----------
    host : str
        IP address to bind to. "127.0.0.1" for loopback.
    port : int
        UDP port to listen on. Must match the port the sender targets.
    buffer_size : int
        Requested kernel receive-buffer size in bytes (SO_RCVBUF).
        The OS may silently cap this to /proc/sys/net/core/rmem_max on Linux
        or the equivalent sysctl on macOS.
    n_expected : int
        How many datagrams the sender intends to send.
        Used to compute loss_rate_pct = (n_expected - received) / n_expected * 100.
    results : dict
        Shared dictionary that the orchestrator reads after this thread finishes.
        This function writes the following keys:
            'received'      — int,   number of datagrams that arrived
            'total_bytes'   — int,   sum of payload bytes received
            'elapsed_s'     — float, wall time from first to last datagram
            'loss_rate_pct' — float, percentage of lost datagrams
            'jitter_ms'     — float, jitter computed by compute_jitter()
            'seq_nums'      — list[int], sorted sequence numbers received
    """
    # AF_INET = IPv4 address family; SOCK_DGRAM = UDP (unreliable datagrams).
    # Compare with SOCK_STREAM, which is TCP (reliable byte stream).
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # SO_RCVBUF controls the kernel-side receive buffer for incoming datagrams.
    # A larger buffer lets the OS queue more packets before the application reads
    # them, which reduces loss under burst traffic.
    # SOL_SOCKET = "socket-level option" (as opposed to IP-level or TCP-level).
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, buffer_size)

    # bind() claims exclusive ownership of (host, port) for this socket.
    # After this call, any UDP datagram sent to 127.0.0.1:5301 lands here.
    sock.bind((host, port))

    # settimeout() makes recvfrom() raise socket.timeout after RECV_TIMEOUT
    # seconds of silence instead of blocking forever. This is how the receiver
    # knows the transfer is done — it waits for one timeout period after the
    # last datagram arrives.
    sock.settimeout(RECV_TIMEOUT)

    # Accumulators filled during the receive loop.
    seq_nums: list[int] = []          # sequence numbers of received datagrams
    arrival_times_ns: list[int] = []  # nanosecond timestamps of each arrival
    total_bytes = 0                   # sum of datagram sizes received
    first_time_ns: int | None = None  # timestamp of the very first datagram
    last_time_ns: int | None = None   # timestamp of the most recent datagram

    try:
        while True:
            try:
                # recvfrom() blocks until a datagram arrives or the timeout fires.
                # 65535 is the maximum UDP payload size (2^16 - 1 - IP/UDP headers).
                # It returns (data_bytes, (sender_ip, sender_port)).
                # We only need data; the address is ignored here.
                data, _ = sock.recvfrom(65535)

                # Record arrival time immediately after recvfrom() returns so
                # the timestamp reflects when the kernel delivered the packet,
                # not when we finished processing it.
                now_ns = time.perf_counter_ns()

                # struct.unpack_from() reads HEADER_FMT fields from the start
                # of `data` without requiring us to slice the bytes manually.
                # Returns a tuple: (seq_num, send_ns).
                seq_num, _ = struct.unpack_from(HEADER_FMT, data)

                seq_nums.append(seq_num)
                arrival_times_ns.append(now_ns)
                total_bytes += len(data)

                # Track the wall-clock span from first to last datagram.
                if first_time_ns is None:
                    first_time_ns = now_ns
                last_time_ns = now_ns

            except socket.timeout:
                # No datagram for RECV_TIMEOUT seconds — the sender is done.
                break
    finally:
        # Always close the socket, even if an unexpected exception occurs.
        sock.close()

    # ── Post-loop calculations ──────────────────────────────────────────────

    received = len(seq_nums)

    # If nothing arrived at all, fill in safe zero values to avoid division errors.
    if received == 0 or first_time_ns is None or last_time_ns is None:
        results["received"]      = 0
        results["total_bytes"]   = 0
        results["elapsed_s"]     = 0.0
        results["loss_rate_pct"] = 100.0
        results["jitter_ms"]     = 0.0
        results["seq_nums"]      = []
        return

    # Convert nanoseconds to seconds: 1 s = 1e9 ns.
    elapsed_s = (last_time_ns - first_time_ns) / 1e9

    # Loss = how many expected packets never arrived, as a percentage.
    # Clamp to 0 in case more arrived than expected (shouldn't happen on loopback).
    loss_rate_pct = max(0.0, (n_expected - received) / n_expected * 100.0)

    results["received"]      = received
    results["total_bytes"]   = total_bytes
    results["elapsed_s"]     = elapsed_s
    results["loss_rate_pct"] = loss_rate_pct
    results["jitter_ms"]     = compute_jitter(arrival_times_ns)
    results["seq_nums"]      = sorted(seq_nums)

# ─── Sender ───────────────────────────────────────────────────────────────────

def run_udp_sender(host: str, port: int, payload_size: int,
                   buffer_size: int, n_messages: int, send_rate_pps: int) -> None:
    """
    Sends `n_messages` datagrams of exactly `payload_size` bytes to (host, port)
    at a controlled rate of `send_rate_pps` packets per second.

    Rate-pacing is intentional: a fire-and-forget flood saturates the loopback
    queue before the receiver can drain it, producing artificial loss. Pacing
    produces observable queuing behavior that better reflects real networks.

    Parameters
    ----------
    host : str
        Destination IP address. "127.0.0.1" to target the local receiver.
    port : int
        Destination UDP port. Must match what the receiver is bound to.
    payload_size : int
        Total datagram size in bytes, including the 16-byte header.
        Must be >= HEADER_SIZE (16); raises ValueError otherwise.
    buffer_size : int
        Requested kernel send-buffer size in bytes (SO_SNDBUF).
        Controls how many bytes the OS will queue before blocking sendto().
    n_messages : int
        Number of datagrams to send (e.g. 1000).
    send_rate_pps : int
        Target send rate in packets per second (e.g. 500 means one packet
        every 2 ms). Controls the sleep duration between sends.
    """
    if payload_size < HEADER_SIZE:
        # A datagram smaller than the header can't fit seq_num + timestamp.
        raise ValueError(
            f"payload_size ({payload_size}) must be >= HEADER_SIZE ({HEADER_SIZE})"
        )

    # Create a UDP socket for sending. No bind() needed on the sender side —
    # the OS assigns an ephemeral source port automatically when sendto() is called.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # SO_SNDBUF sets the kernel send buffer. A larger buffer lets the OS accept
    # more bytes from sendto() calls before blocking, smoothing out bursts.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, buffer_size)

    # Pre-compute sleep time and padding so we don't repeat the division and
    # allocation inside the hot loop.
    inter_send_s = 1.0 / send_rate_pps                   # seconds between sends
    padding = b"\x00" * (payload_size - HEADER_SIZE)     # zero-fill after header

    try:
        for seq_num in range(n_messages):
            # Pack the header: struct.pack(fmt, *values) returns a bytes object.
            # HEADER_FMT = ">QQ": big-endian, two uint64 fields.
            # seq_num   — receiver uses this to detect gaps/reordering.
            # perf_counter_ns() — receiver could compute one-way delay if clocks
            #                     were synchronized; here it's mainly for jitter.
            header = struct.pack(HEADER_FMT, seq_num, time.perf_counter_ns())

            # sendto() transmits the datagram. Unlike TCP's send(), a single
            # sendto() call always sends exactly one datagram — no coalescing.
            # (host, port) is the destination address tuple.
            sock.sendto(header + padding, (host, port))

            # Sleep to pace sends. time.sleep() is not perfectly accurate at
            # sub-millisecond granularity on macOS, but it's sufficient to
            # prevent queue flooding at rates <= 1000 pps.
            time.sleep(inter_send_s)
    finally:
        sock.close()

# ─── Orchestrator ─────────────────────────────────────────────────────────────

def run_udp_experiment(payload_size: int, buffer_size: int, n_messages: int,
                       send_rate_pps: int, label: str, run_index: int) -> tuple[float, float, float]:
    """
    Top-level function that coordinates the receiver thread and sender,
    then collects, saves, and returns the measured metrics.

    Sequence of events:
      1. Start the receiver in a daemon thread so it is ready before any
         datagrams arrive.
      2. Sleep briefly to guarantee the receiver has called bind() before
         the sender calls sendto().
      3. Run the sender in the main thread (blocks until all sends complete).
      4. Join the receiver thread to wait for it to finish draining.
      5. Compute throughput and save results to CSV.

    Parameters
    ----------
    payload_size : int
        Datagram size in bytes (>= 16). Varies across experiment sweep.
    buffer_size : int
        Socket buffer size in bytes. Varies across experiment sweep.
    n_messages : int
        Number of datagrams to send per run (e.g. 1000).
    send_rate_pps : int
        Send rate in packets per second (e.g. 500).
    label : str
        Network condition label written to the CSV (e.g. "baseline", "lossy").
    run_index : int
        Which repetition this is (1, 2, or 3). Used to average results later.

    Returns
    -------
    tuple[float, float, float]
        (throughput_mbps, loss_rate_pct, jitter_ms)
    """
    # Shared dict passed to the receiver thread by reference.
    # Python dicts are mutable, so the thread can write into it and the
    # main thread can read the results after joining.
    results: dict = {}

    # threading.Thread(target=fn, args=(...)) creates a thread object.
    # daemon=True means the thread will be killed automatically if the main
    # process exits — prevents hanging if the sender crashes.
    receiver_thread = threading.Thread(
        target=_udp_receiver,
        args=(HOST, RECV_PORT, buffer_size, n_messages, results),
        daemon=True,
    )
    receiver_thread.start()

    # Give the receiver time to reach sock.bind() before the sender fires.
    # 0.1 s is generous — bind() completes in microseconds on loopback.
    time.sleep(0.1)

    # Run the sender. This blocks the main thread until all n_messages
    # datagrams have been handed to the OS send buffer.
    run_udp_sender(HOST, RECV_PORT, payload_size, buffer_size, n_messages, send_rate_pps)

    # Wait for the receiver to finish. The timeout accounts for the full
    # expected transfer time plus RECV_TIMEOUT seconds of silence at the end.
    # If the thread is still alive after timeout, results may be incomplete.
    join_timeout = (n_messages / send_rate_pps) + RECV_TIMEOUT + 2.0
    receiver_thread.join(timeout=join_timeout)

    # ── Compute throughput ──────────────────────────────────────────────────
    # Throughput = total bits delivered / elapsed time, in megabits per second.
    # total_bytes * 8 converts bytes → bits; dividing by 1e6 converts → megabits.
    elapsed_s = results.get("elapsed_s", 0.0)
    total_bytes = results.get("total_bytes", 0)

    if elapsed_s > 0:
        throughput_mbps = (total_bytes * 8) / (elapsed_s * 1e6)
    else:
        throughput_mbps = 0.0

    loss_rate_pct = results.get("loss_rate_pct", 100.0)
    jitter_ms     = results.get("jitter_ms", 0.0)

    # ── Save to CSV ─────────────────────────────────────────────────────────
    # Pass RESULTS_PATH explicitly so tests can patch udp_module.RESULTS_PATH
    # and redirect output to a temp file. Default parameters are captured at
    # function-definition time, but a name reference in the function body is
    # looked up in module globals at call time — so patching works here.
    save_result({
        "protocol":        "UDP",
        "payload_bytes":   payload_size,
        "buffer_bytes":    buffer_size,
        "condition":       label,
        "throughput_mbps": round(throughput_mbps, 4),
        "loss_rate_pct":   round(loss_rate_pct, 4),
        "jitter_ms":       round(jitter_ms, 4),
        "run_index":       run_index,
    }, filepath=RESULTS_PATH)

    return throughput_mbps, loss_rate_pct, jitter_ms

# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    """
    Command-line entry point. Parses arguments, runs one experiment,
    and prints a one-line summary so run_experiments.sh can follow progress.
    """
    parser = argparse.ArgumentParser(description="UDP measurement module")

    # Each add_argument call defines one CLI flag.
    # type=int means argparse converts the string input to an integer.
    # required=True means the script exits with an error if the flag is missing.
    # default=... provides a fallback value when the flag is omitted.
    parser.add_argument("--payload",  type=int, required=True,
                        help="Total datagram size in bytes (must be >= 16)")
    parser.add_argument("--buffer",   type=int, required=True,
                        help="Socket buffer size in bytes (SO_SNDBUF / SO_RCVBUF)")
    parser.add_argument("--messages", type=int, default=1000,
                        help="Number of datagrams to send (default: 1000)")
    parser.add_argument("--rate",     type=int, default=500,
                        help="Send rate in packets per second (default: 500)")
    parser.add_argument("--label",    type=str, default="baseline",
                        help="Network condition label written to CSV (default: baseline)")
    parser.add_argument("--run",      type=int, default=1,
                        help="Run index 1-3 for repeated trials (default: 1)")

    args = parser.parse_args()

    throughput, loss, jitter = run_udp_experiment(
        payload_size  = args.payload,
        buffer_size   = args.buffer,
        n_messages    = args.messages,
        send_rate_pps = args.rate,
        label         = args.label,
        run_index     = args.run,
    )

    # One-line summary for shell script parsing and human readability.
    print(
        f"UDP | {args.label} | payload={args.payload}B | "
        f"throughput={throughput:.2f} Mbps | "
        f"loss={loss:.2f}% | jitter={jitter:.2f}ms"
    )


if __name__ == "__main__":
    main()
