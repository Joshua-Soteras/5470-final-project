"""
TCP Measurement Module
----------------------
Runs a TCP sender/receiver pair on localhost.
Measures throughput (Mbps) and round-trip latency (ms).

Usage:
    python3 tcp_module.py --payload 1024 --buffer 65536 --messages 1000 --label baseline --run 1

Two modes are run per invocation:
    1. Throughput mode  — bulk transfer, measure total bytes / elapsed time
    2. Latency mode     — ping-pong echo, measure per-message RTT
"""

import socket
import time
import threading
import csv
import os
import struct
import statistics
import argparse

# ─── Constants ────────────────────────────────────────────────────────────────

HOST            = "127.0.0.1"
THROUGHPUT_PORT = 5201
LATENCY_PORT    = 5202
RESULTS_PATH    = os.path.join(os.path.dirname(__file__), "..", "results", "tcp_results.csv")
CSV_HEADER      = ["protocol", "payload_bytes", "buffer_bytes", "condition",
                   "throughput_mbps", "avg_latency_ms", "latency_stdev_ms", "run_index"]

# ─── CSV Helper ───────────────────────────────────────────────────────────────

def save_result(row: dict, filepath: str = RESULTS_PATH) -> None:
    """Append one result row to the CSV. Writes the header if the file is new."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    write_header = not os.path.exists(filepath) or os.path.getsize(filepath) == 0
    with open(filepath, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        if write_header:
            writer.writeheader()
        writer.writerow(row)

# ─── Throughput Mode ──────────────────────────────────────────────────────────

def _throughput_server(host: str, port: int, buffer_size: int, results: dict) -> None:
    """
    Receives all bytes from a single client connection and records elapsed time.
    Stores 'elapsed_s' and 'total_bytes' into the shared results dict.

    TODO:
        1. Create a TCP socket and set SO_RCVBUF to buffer_size.
        2. Bind to (host, port), listen, and accept one connection.
        3. Record start time, recv in a loop until the connection closes (recv returns b'').
        4. Record end time and total bytes received.
        5. Store results['elapsed_s'] and results['total_bytes'].
        6. Close the connection and the server socket.
    """
    raise NotImplementedError("TODO: implement server receive loop")


def run_throughput_client(host: str, port: int, payload_size: int,
                          buffer_size: int, n_messages: int) -> float:
    """
    Sends n_messages of payload_size bytes over TCP and returns throughput in Mbps.

    TODO:
        1. Create a TCP socket and set SO_SNDBUF to buffer_size.
        2. Also set TCP_NODELAY to 1 (disables Nagle — important for small payloads).
        3. Connect to (host, port).
        4. Build a payload of exactly payload_size bytes (os.urandom or b'x' * payload_size).
        5. Send it n_messages times with sock.sendall().
        6. Close the socket (this triggers the server's recv loop to exit).
        7. Return total_bytes_sent / elapsed_s, converted to Mbps.

    Note: measure elapsed time around the send loop only, not the connect.
    """
    raise NotImplementedError("TODO: implement send loop and return throughput")


def measure_throughput(payload_size: int, buffer_size: int,
                       n_messages: int, label: str, run_index: int) -> float:
    """
    Spawns the server thread, runs the client, waits for server to finish,
    then saves and returns throughput in Mbps.

    TODO:
        1. Create a shared results dict: results = {}.
        2. Start _throughput_server in a daemon thread, passing results.
        3. Sleep briefly (e.g. 0.05s) to let the server bind before the client connects.
        4. Call run_throughput_client and capture its return value.
        5. Join the server thread (with a timeout of 10s).
        6. Compute throughput_mbps from results['total_bytes'] and results['elapsed_s'].
        7. Call save_result with the assembled row dict.
        8. Return throughput_mbps.
    """
    raise NotImplementedError("TODO: implement orchestration and thread join")

# ─── Latency (Ping-Pong) Mode ─────────────────────────────────────────────────

def _latency_server(host: str, port: int, buffer_size: int,
                    n_pings: int, stop_event: threading.Event) -> None:
    """
    Echo server: reads one message and immediately sends it back, n_pings times.

    TODO:
        1. Create a TCP socket with SO_RCVBUF = buffer_size and TCP_NODELAY = 1.
        2. Bind, listen, accept one connection.
        3. Loop n_pings times: recv the length prefix first (4 bytes), then recv
           exactly that many bytes, then sendall them back.
        4. Close once done or when stop_event is set.

    Note: recv may return partial data. Keep calling recv until you have a full message.
    The client sends a fixed 4-byte big-endian length prefix before each message so
    the server knows exactly how many bytes to wait for.
    """
    raise NotImplementedError("TODO: implement echo loop")


def run_latency_client(host: str, port: int, payload_size: int,
                       buffer_size: int, n_pings: int) -> list[float]:
    """
    Sends n_pings messages and records the RTT for each. Returns a list of RTTs in ms.

    TODO:
        1. Create a TCP socket with SO_SNDBUF = buffer_size and TCP_NODELAY = 1.
        2. Connect to (host, port).
        3. Build a payload of payload_size bytes.
        4. For each ping:
            a. Prepend a 4-byte big-endian length header: struct.pack('>I', payload_size).
            b. Record t0 = time.perf_counter().
            c. sendall(header + payload).
            d. recv the exact same number of bytes back (loop until full).
            e. Record t1 = time.perf_counter().
            f. Append (t1 - t0) * 1000 to the RTT list.
        5. Close and return the RTT list.
    """
    raise NotImplementedError("TODO: implement ping-pong and return RTT list")


def measure_latency(payload_size: int, buffer_size: int,
                    n_pings: int, label: str, run_index: int) -> tuple[float, float]:
    """
    Spawns the echo server, runs the latency client, returns (mean_rtt_ms, stdev_rtt_ms).

    TODO:
        1. Create a threading.Event() for coordinated shutdown.
        2. Start _latency_server in a daemon thread.
        3. Sleep 0.05s, then call run_latency_client.
        4. Set the stop_event and join the server thread.
        5. Compute mean and stdev from the RTT list (use statistics.mean / statistics.stdev).
        6. Save result and return (mean, stdev).
    """
    raise NotImplementedError("TODO: implement orchestration and return stats")

# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TCP measurement module")
    parser.add_argument("--payload",  type=int, required=True, help="Payload size in bytes")
    parser.add_argument("--buffer",   type=int, required=True, help="Socket buffer size in bytes")
    parser.add_argument("--messages", type=int, default=1000,  help="Number of messages to send")
    parser.add_argument("--label",    type=str, default="baseline", help="Condition label")
    parser.add_argument("--run",      type=int, default=1,     help="Run index (1-3)")
    args = parser.parse_args()

    # TODO: call measure_throughput and measure_latency with the parsed args.
    # Print a one-line summary per measurement so run_experiments.sh can follow progress.
    # Example: print(f"TCP | {args.label} | payload={args.payload}B | throughput={tput:.2f} Mbps")
    raise NotImplementedError("TODO: wire up main()")


if __name__ == "__main__":
    main()
