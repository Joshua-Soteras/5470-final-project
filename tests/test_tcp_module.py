"""
Tests for src/tcp_module.py
============================

How to run:
    uv run pytest tests/test_tcp_module.py -v

Test organisation
-----------------
1. Unit tests for save_result()
   - No sockets, no threads — just file I/O with a temp CSV.

2. Unit tests for _recv_exact()
   - Uses a real loopback TCP connection (two sockets) to verify that
     the helper correctly reassembles fragmented data.

3. Integration tests for measure_throughput()
   - Spins up a real server and client on loopback, checks the result
     is a positive number and a CSV row is written.

4. Integration tests for measure_latency()
   - Runs the ping-pong echo on loopback, checks RTT is positive and
     stdev is non-negative.

5. Integration test for run_tcp_experiment()
   - End-to-end: both measurements, one CSV row with all fields.

6. Flood comparison test
   - Runs run_tcp_experiment() with the background flood active and
     verifies it still returns valid (positive) metrics.
     NOTE: this test verifies correctness, not that the flood reduces
     throughput — that comparison is best done manually via the CLI
     because the magnitude depends on OS scheduling and is not
     deterministic enough for a tight assertion.
"""

import os
import csv
import socket
import threading
import tempfile

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tcp_module import (
    save_result,
    _recv_exact,
    measure_throughput,
    measure_latency,
    run_tcp_experiment,
    CSV_HEADER,
    LENGTH_FMT,
    LENGTH_SIZE,
)

# ─── Section 1: Unit tests for save_result() ─────────────────────────────────
#
# These are identical in structure to the UDP module's save_result tests —
# the function is the same pattern, but writes to tcp_results.csv with
# different columns.

def _make_tcp_row(**overrides) -> dict:
    """
    Helper that builds a valid TCP result row with sensible defaults.
    Pass keyword arguments to override specific fields.
    """
    base = {
        "protocol":          "TCP",
        "payload_bytes":     1024,
        "buffer_bytes":      65536,
        "condition":         "baseline",
        "throughput_mbps":   500.0,
        "avg_latency_ms":    0.25,
        "latency_stdev_ms":  0.05,
        "run_index":         1,
    }
    base.update(overrides)
    return base


def test_tcp_save_result_creates_file():
    """
    save_result() should create the CSV file and write the header row
    followed by the data row when called on a path that does not exist.
    """
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp_path = tmp.name
    os.remove(tmp_path)   # delete so save_result creates it fresh

    try:
        save_result(_make_tcp_row(), filepath=tmp_path)

        assert os.path.exists(tmp_path), "save_result() did not create the file"

        with open(tmp_path, newline="") as f:
            rows = list(csv.DictReader(f))

        assert len(rows) == 1
        assert rows[0]["protocol"] == "TCP"
        assert rows[0]["payload_bytes"] == "1024"
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_tcp_save_result_header_written_once():
    """
    The column header row must appear exactly once no matter how many
    times save_result() is called on the same file.
    """
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp_path = tmp.name
    os.remove(tmp_path)

    try:
        for i in range(1, 4):
            save_result(_make_tcp_row(run_index=i), filepath=tmp_path)

        with open(tmp_path, newline="") as f:
            lines = f.readlines()

        header_lines = [l for l in lines if l.startswith("protocol")]
        assert len(header_lines) == 1, (
            f"Header appears {len(header_lines)} times; expected 1"
        )
        assert len(lines) == 4, f"Expected 4 lines (1 header + 3 data), got {len(lines)}"
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_tcp_save_result_appends_rows():
    """Each call to save_result() adds a new data row."""
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp_path = tmp.name
    os.remove(tmp_path)

    try:
        save_result(_make_tcp_row(run_index=1, throughput_mbps=400.0), filepath=tmp_path)
        save_result(_make_tcp_row(run_index=2, throughput_mbps=420.0), filepath=tmp_path)

        with open(tmp_path, newline="") as f:
            rows = list(csv.DictReader(f))

        assert len(rows) == 2
        assert float(rows[0]["throughput_mbps"]) == pytest.approx(400.0)
        assert float(rows[1]["throughput_mbps"]) == pytest.approx(420.0)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_tcp_save_result_all_columns_present():
    """The saved row must contain every column defined in CSV_HEADER."""
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp_path = tmp.name
    os.remove(tmp_path)

    try:
        save_result(_make_tcp_row(), filepath=tmp_path)

        with open(tmp_path, newline="") as f:
            reader = csv.DictReader(f)
            next(reader)   # advance so fieldnames is populated

        fieldnames = reader.fieldnames or []
        assert set(fieldnames) == set(CSV_HEADER), (
            f"Column mismatch.\n  Expected: {sorted(CSV_HEADER)}\n  Got: {sorted(fieldnames)}"
        )
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# ─── Section 2: Unit tests for _recv_exact() ─────────────────────────────────
#
# _recv_exact() is the helper that loops recv() until exactly N bytes arrive.
# We test it by creating a real loopback socket pair: one thread sends data
# in small chunks and the main thread calls _recv_exact() to reassemble them.

def _send_in_chunks(conn: socket.socket, data: bytes, chunk_size: int) -> None:
    """
    Helper thread target: sends `data` to `conn` in chunks of `chunk_size`.
    Used to simulate TCP fragmentation for _recv_exact() tests.
    """
    for i in range(0, len(data), chunk_size):
        conn.sendall(data[i : i + chunk_size])
    conn.close()


def _make_loopback_pair() -> tuple[socket.socket, socket.socket]:
    """
    Creates a connected loopback socket pair.
    Returns (client_socket, accepted_server_connection).
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))   # port 0 = OS picks a free port
    server.listen(1)
    port = server.getsockname()[1]

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(("127.0.0.1", port))

    conn, _ = server.accept()
    server.close()
    return client, conn


def test_recv_exact_full_message():
    """
    _recv_exact() should return exactly the requested number of bytes
    when the sender delivers them as a single chunk.
    """
    sender, receiver = _make_loopback_pair()
    data = b"hello world"   # 11 bytes

    # Send all 11 bytes at once in a background thread.
    t = threading.Thread(target=_send_in_chunks, args=(sender, data, len(data)))
    t.start()

    result = _recv_exact(receiver, len(data))
    t.join()
    receiver.close()

    assert result == data, f"Expected {data!r}, got {result!r}"


def test_recv_exact_fragmented_message():
    """
    _recv_exact() must correctly reassemble a message that arrives in
    multiple small TCP segments. This is the main reason the helper exists.
    """
    sender, receiver = _make_loopback_pair()
    data = b"A" * 1000   # 1000 bytes sent as 10-byte chunks

    t = threading.Thread(target=_send_in_chunks, args=(sender, data, 10))
    t.start()

    result = _recv_exact(receiver, 1000)
    t.join()
    receiver.close()

    assert len(result) == 1000
    assert result == data


def test_recv_exact_raises_on_disconnect():
    """
    If the sender closes the connection before sending all expected bytes,
    _recv_exact() must raise ConnectionError rather than silently returning
    a short read.
    """
    sender, receiver = _make_loopback_pair()

    # Send only 5 bytes, then close — but we'll ask for 100.
    sender.sendall(b"short")
    sender.close()

    with pytest.raises(ConnectionError):
        _recv_exact(receiver, 100)

    receiver.close()


# ─── Section 3: Integration tests for measure_throughput() ───────────────────
#
# These tests spin up a real TCP server + client on loopback.
# We use a small message count to keep each test fast (~0.1s).

def test_measure_throughput_returns_positive():
    """
    measure_throughput() must return a positive throughput value on loopback.
    We use 100 messages × 512 bytes = 50 KB — fast enough to finish in under 1s.
    """
    throughput = measure_throughput(
        payload_size = 512,
        buffer_size  = 65536,
        n_messages   = 100,
    )
    assert throughput > 0.0, f"Expected positive throughput, got {throughput}"


def test_measure_throughput_larger_payload():
    """
    Throughput should be measurably positive even at a larger payload (4096 bytes).
    Larger payloads typically yield higher throughput due to lower per-message overhead.
    """
    throughput = measure_throughput(
        payload_size = 4096,
        buffer_size  = 65536,
        n_messages   = 50,
    )
    assert throughput > 0.0, f"Expected positive throughput, got {throughput}"


# ─── Section 4: Integration tests for measure_latency() ──────────────────────

def test_measure_latency_returns_positive_rtt():
    """
    On loopback, the average RTT should be positive (non-zero) and below
    100 ms. We use 20 pings to keep the test fast.
    """
    avg_rtt, stdev_rtt = measure_latency(
        payload_size = 256,
        buffer_size  = 65536,
        n_pings      = 20,
    )
    assert avg_rtt > 0.0, f"Expected positive RTT, got {avg_rtt}"
    assert avg_rtt < 100.0, f"RTT suspiciously large on loopback: {avg_rtt:.2f}ms"


def test_measure_latency_stdev_non_negative():
    """
    Standard deviation of RTT samples must be non-negative.
    With >= 2 pings, stdev should also be a small positive value on loopback.
    """
    avg_rtt, stdev_rtt = measure_latency(
        payload_size = 256,
        buffer_size  = 65536,
        n_pings      = 10,
    )
    assert stdev_rtt >= 0.0, f"stdev must be non-negative, got {stdev_rtt}"


def test_measure_latency_single_ping_stdev_zero():
    """
    With exactly 1 ping there is only one sample — stdev is undefined and
    should be returned as 0.0 rather than raising an exception.
    """
    avg_rtt, stdev_rtt = measure_latency(
        payload_size = 128,
        buffer_size  = 65536,
        n_pings      = 1,
    )
    assert stdev_rtt == 0.0, f"Expected stdev=0.0 for single ping, got {stdev_rtt}"
    assert avg_rtt > 0.0


# ─── Section 5: Integration test for run_tcp_experiment() ────────────────────

def test_run_tcp_experiment_basic():
    """
    End-to-end smoke test: runs both throughput and latency on loopback,
    verifies all three returned metrics are sane, and checks that exactly
    one row was written to the CSV with the correct fields.

    We redirect CSV output to a temp file to avoid polluting tcp_results.csv.
    """
    import tcp_module

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp_path = tmp.name
    os.remove(tmp_path)

    original_path = tcp_module.RESULTS_PATH
    tcp_module.RESULTS_PATH = tmp_path

    try:
        throughput, avg_lat, stdev_lat = run_tcp_experiment(
            payload_size = 512,
            buffer_size  = 65536,
            n_messages   = 50,
            label        = "test",
            run_index    = 1,
        )

        # ── Throughput ───────────────────────────────────────────────────────
        assert throughput > 0.0, f"Throughput should be positive, got {throughput}"

        # ── Latency ──────────────────────────────────────────────────────────
        assert avg_lat > 0.0, f"Average RTT should be positive, got {avg_lat}"
        assert avg_lat < 100.0, f"RTT too large on loopback: {avg_lat:.2f}ms"
        assert stdev_lat >= 0.0, f"RTT stdev must be non-negative, got {stdev_lat}"

        # ── CSV output ───────────────────────────────────────────────────────
        assert os.path.exists(tmp_path), "run_tcp_experiment() did not create the CSV"
        with open(tmp_path, newline="") as f:
            rows = list(csv.DictReader(f))

        assert len(rows) == 1, f"Expected 1 CSV row, got {len(rows)}"
        assert rows[0]["protocol"] == "TCP"
        assert rows[0]["condition"] == "test"
        assert float(rows[0]["throughput_mbps"]) == pytest.approx(throughput, rel=1e-3)

    finally:
        tcp_module.RESULTS_PATH = original_path
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# ─── Section 6: Flood comparison test ────────────────────────────────────────
#
# This test verifies that run_tcp_experiment() still returns valid metrics
# when the background flood is active. It does NOT assert that throughput
# is lower with the flood — that comparison depends on OS scheduling and
# is not deterministic enough for a CI test.
#
# For a meaningful comparison, run the CLI manually:
#
#   # Baseline (no flood)
#   uv run python src/tcp_module.py --payload 1024 --buffer 65536 --label baseline --run 1
#
#   # Congested (flood active)
#   uv run python src/tcp_module.py --payload 1024 --buffer 65536 --label congested --run 1 --flood

def test_run_tcp_experiment_with_flood():
    """
    run_tcp_experiment() must return valid (positive) metrics even when a
    background TCP flood is competing for loopback bandwidth.
    """
    import tcp_module
    from background_flood import start_flood, stop_flood

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp_path = tmp.name
    os.remove(tmp_path)

    original_path = tcp_module.RESULTS_PATH
    tcp_module.RESULTS_PATH = tmp_path

    # Start the flood before the experiment so it is active during measurement.
    flood_stop = start_flood()

    try:
        throughput, avg_lat, stdev_lat = run_tcp_experiment(
            payload_size = 512,
            buffer_size  = 65536,
            n_messages   = 50,
            label        = "congested",
            run_index    = 1,
        )

        assert throughput > 0.0, f"Throughput should be positive even with flood, got {throughput}"
        assert avg_lat > 0.0, f"RTT should be positive even with flood, got {avg_lat}"
        assert stdev_lat >= 0.0

    finally:
        stop_flood(flood_stop)
        tcp_module.RESULTS_PATH = original_path
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
