"""
Background TCP Flood
--------------------
Generates a continuous stream of competing TCP traffic on loopback to simulate
real network congestion. Used alongside the measurement modules to trigger TCP's
AIMD (Additive Increase, Multiplicative Decrease) congestion control — which
does NOT activate from dummynet's bandwidth cap alone.

Why this is necessary
---------------------
dummynet limits bandwidth by dropping or delaying packets at a fixed rate, but
the experiment's TCP flow is still the only flow on the wire. TCP's AIMD only
backs off when it detects loss caused by a queue filling up due to competition.
A background flood creates that competition: both the flood and the experiment
share the same bottleneck link, the queue overflows, loss events fire, and the
experiment's TCP sender reduces its congestion window — which is exactly the
behaviour the congestion condition is meant to measure.

UDP is unaffected by this (it has no congestion control), so the contrast
between UDP maintaining throughput and TCP backing off becomes clearly visible
in the results.

Usage — as a library (called from run_experiments.sh via Python):
    from background_flood import start_flood, stop_flood
    stop_event = start_flood()
    # ... run experiment ...
    stop_flood(stop_event)

Usage — as a standalone process (called directly from shell scripts):
    python3 src/background_flood.py &
    FLOOD_PID=$!
    # ... run experiment ...
    kill $FLOOD_PID

Port used: 5400 — separate from all measurement ports (5201, 5202, 5301).
"""

import socket
import threading
import time
import argparse

# ─── Constants ────────────────────────────────────────────────────────────────

HOST       = "127.0.0.1"   # loopback — flood stays on this machine
FLOOD_PORT = 5400           # dedicated port, never conflicts with measurement modules

# Large chunk size maximises throughput pressure on the loopback queue.
# 65535 bytes is the maximum single TCP send that fits in one IP datagram
# before fragmentation on most systems.
FLOOD_CHUNK = b"\x00" * 65535

# ─── Flood Server (receiver side) ─────────────────────────────────────────────

def _flood_server(stop_event: threading.Event, ready_event: threading.Event,
                  port: int) -> None:
    """
    Binds a TCP socket on `port` and accepts one connection from the flood
    client. Reads and discards all incoming bytes until the stop event is set.

    This thread must start and bind before the client thread tries to connect,
    which is coordinated via ready_event.

    Parameters
    ----------
    stop_event : threading.Event
        Shared event. When set (by stop_flood()), this thread exits its read loop
        and closes the socket.
    ready_event : threading.Event
        Set by this thread once the socket is bound and listening. The client
        thread waits on this before attempting to connect, preventing a race
        condition where the client calls connect() before the server calls listen().
    port : int
        TCP port to bind to.
    """
    # SO_REUSEADDR lets the OS immediately reuse the port after a previous run
    # closed it. Without this, you'd get "Address already in use" if the flood
    # is restarted quickly.
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((HOST, port))
    server_sock.listen(1)

    # Signal the client thread that it is safe to connect now.
    ready_event.set()

    # accept() blocks until the flood client connects. We set a short timeout
    # so the thread can notice if stop_event is set before the client arrives.
    server_sock.settimeout(1.0)
    conn = None

    try:
        while not stop_event.is_set():
            try:
                conn, _ = server_sock.accept()
                break   # got a connection — drop into the read loop below
            except socket.timeout:
                continue   # no connection yet, check stop_event and retry

        if conn is None:
            return   # stop_event was set before the client connected

        # Receive and discard data as fast as possible.
        # recv(65535) reads up to 65535 bytes per call.
        # Returning b"" signals the client closed the connection.
        conn.settimeout(0.5)
        while not stop_event.is_set():
            try:
                data = conn.recv(65535)
                if not data:
                    break   # client disconnected cleanly
            except socket.timeout:
                continue    # nothing to read right now, check stop_event

    finally:
        if conn:
            conn.close()
        server_sock.close()

# ─── Flood Client (sender side) ───────────────────────────────────────────────

def _flood_client(stop_event: threading.Event, ready_event: threading.Event,
                  port: int, buffer_size: int) -> None:
    """
    Waits for the flood server to be ready, then connects and sends data
    in a tight loop as fast as the OS will accept it.

    No rate limiting — the goal is to saturate the loopback queue so the
    experiment's TCP flow experiences real congestion, not an artificial cap.

    Parameters
    ----------
    stop_event : threading.Event
        Shared event. When set, this thread closes its socket and exits.
    ready_event : threading.Event
        Waited on before connecting to guarantee the server is listening.
        Prevents "Connection refused" errors from a timing race.
    port : int
        TCP port to connect to — must match what the server thread is bound to.
    buffer_size : int
        SO_SNDBUF size for the flood socket.
    """
    # Wait up to 5 seconds for the server thread to bind and listen.
    ready_event.wait(timeout=5.0)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # SO_SNDBUF — large send buffer so the OS can queue many bytes between
    # send() calls, keeping the link fully saturated without busy-waiting.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, buffer_size)

    try:
        sock.connect((HOST, port))

        # Send data in a tight loop. sock.send() returns the number of bytes
        # the OS accepted (may be less than len(FLOOD_CHUNK) if the buffer is
        # full), so we don't check the return value — we just keep hammering.
        while not stop_event.is_set():
            try:
                sock.send(FLOOD_CHUNK)
            except (BrokenPipeError, ConnectionResetError):
                # Server closed — stop gracefully.
                break
    finally:
        sock.close()

# ─── Public API ───────────────────────────────────────────────────────────────

def start_flood(port: int = FLOOD_PORT, buffer_size: int = 1 << 20) -> threading.Event:
    """
    Starts the background flood and returns a stop event.

    Spawns two daemon threads: one TCP server (receiver) and one TCP client
    (sender). Both run until stop_flood() is called with the returned event.

    Parameters
    ----------
    port : int
        TCP port for the flood. Defaults to FLOOD_PORT (5400). Override in
        tests to avoid conflicts with other running floods.
    buffer_size : int
        Send buffer size for the flood client socket. Defaults to 1 MB.
        Larger buffers let the OS queue more data, increasing flood intensity.

    Returns
    -------
    threading.Event
        Pass this to stop_flood() to cleanly shut down both threads.
    """
    # A single shared event signals both threads to exit at the same time.
    stop_event = threading.Event()

    # ready_event prevents the client from calling connect() before the
    # server has called listen() — a race condition that causes ConnectionRefused.
    ready_event = threading.Event()

    # daemon=True means both threads are killed automatically if the main
    # process exits, so the flood never keeps the process alive by itself.
    server_thread = threading.Thread(
        target=_flood_server,
        args=(stop_event, ready_event, port),
        daemon=True,
    )
    client_thread = threading.Thread(
        target=_flood_client,
        args=(stop_event, ready_event, port, buffer_size),
        daemon=True,
    )

    server_thread.start()
    client_thread.start()

    # Give the flood a moment to reach full speed before the experiment starts.
    # 0.2 s is enough for the TCP connection to complete and fill the send buffer.
    time.sleep(0.2)

    return stop_event


def stop_flood(stop_event: threading.Event) -> None:
    """
    Signals the flood threads to exit and waits briefly for them to clean up.

    Parameters
    ----------
    stop_event : threading.Event
        The event returned by start_flood(). Setting it tells both the server
        and client threads to close their sockets and return.
    """
    stop_event.set()
    # Give threads up to 2 seconds to close their sockets gracefully.
    # We don't join them directly because they are daemon threads — they will
    # be cleaned up by the OS if the process exits before they finish.
    time.sleep(0.5)

# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    """
    Standalone entry point for use from shell scripts.

    Starts the flood and runs until interrupted with Ctrl+C or killed with
    SIGTERM (e.g. `kill $FLOOD_PID` from run_experiments.sh).

    Example from run_experiments.sh:
        python3 src/background_flood.py &
        FLOOD_PID=$!
        # ... run congested condition experiments ...
        kill $FLOOD_PID
    """
    parser = argparse.ArgumentParser(description="Background TCP flood for congestion testing")
    parser.add_argument("--port",   type=int, default=FLOOD_PORT,
                        help=f"TCP port to use for the flood (default: {FLOOD_PORT})")
    parser.add_argument("--buffer", type=int, default=1 << 20,
                        help="Flood client send buffer size in bytes (default: 1048576)")
    args = parser.parse_args()

    print(f"Starting background TCP flood on {HOST}:{args.port} — press Ctrl+C to stop")

    stop_event = start_flood(port=args.port, buffer_size=args.buffer)

    try:
        # Block the main thread indefinitely so the daemon threads keep running.
        # The flood only stops when this process is killed or Ctrl+C is pressed.
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping flood...")
        stop_flood(stop_event)


if __name__ == "__main__":
    main()
