"""
Tests for src/udp_module.py
============================

How to run all tests:
    uv run pytest tests/ -v

How to run just this file:
    uv run pytest tests/test_udp_module.py -v

The -v flag ("verbose") prints each test name and PASSED/FAILED instead of
just dots. Useful while learning what is being tested.

Test organisation
-----------------
This file is split into three sections:

1. Unit tests for compute_jitter()
   - No sockets, no threads, no I/O. Just pass numbers in, check numbers out.
   - Fast and deterministic.

2. Unit tests for save_result()
   - Writes to a real temporary file (not a mock) so we verify actual CSV output.
   - Still no networking.

3. Unit tests for run_udp_sender() validation
   - Checks that the function rejects bad input before opening any socket.

4. Integration test for run_udp_experiment()
   - Spins up a real receiver thread and a real sender on loopback.
   - Slower (~3-5 s) but verifies the whole pipeline end-to-end.

Why pytest instead of unittest?
--------------------------------
pytest lets you write plain functions (def test_...) instead of classes that
inherit from unittest.TestCase. The assertion messages are also much clearer
when a test fails — pytest shows the actual vs. expected values automatically.
"""

import os
import csv
import tempfile   # tempfile.NamedTemporaryFile — creates a throwaway file for tests

import pytest     # pytest.raises — checks that a specific exception is raised

# Add the src/ directory to the import path so Python can find udp_module.
# This is needed because the tests/ folder is a sibling of src/, not inside it.
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from udp_module import (
    compute_jitter,
    save_result,
    run_udp_sender,
    run_udp_experiment,
    HEADER_SIZE,
    CSV_HEADER,
)

# ─── Section 1: Unit tests for compute_jitter() ───────────────────────────────
#
# compute_jitter(arrival_times_ns: list[int]) -> float
#
# Each test name starts with "test_" — pytest automatically discovers and runs
# any function whose name starts with "test_".

def test_jitter_empty_list():
    """
    An empty list has no inter-arrival gaps, so jitter must be 0.0.
    This tests the guard clause at the top of compute_jitter().
    """
    result = compute_jitter([])
    assert result == 0.0, f"Expected 0.0 for empty list, got {result}"


def test_jitter_single_packet():
    """
    One packet produces no gaps at all. Jitter must be 0.0.
    """
    result = compute_jitter([1_000_000])   # one timestamp (1 ms in ns)
    assert result == 0.0


def test_jitter_two_packets():
    """
    Two packets produce one gap but zero gap-deltas (you need at least two gaps
    to compute a delta between them). Jitter must be 0.0.
    """
    result = compute_jitter([0, 1_000_000])   # arrivals at t=0 and t=1ms
    assert result == 0.0


def test_jitter_perfectly_uniform():
    """
    If packets arrive exactly 10 ms apart, every gap is identical (10,000,000 ns).
    The deltas between consecutive gaps are all zero, so jitter = 0.0.

    This verifies that a steady stream registers as zero jitter.
    """
    # Five packets arriving every 10 ms (10,000,000 ns).
    uniform_arrivals = [i * 10_000_000 for i in range(5)]
    result = compute_jitter(uniform_arrivals)
    assert result == 0.0, f"Uniform stream should have 0.0 jitter, got {result}"


def test_jitter_alternating_gaps():
    """
    Alternating gap sizes (5ms, 15ms, 5ms, 15ms ...) produce large and
    consistent variation — jitter should be non-zero and match what we
    can calculate by hand.

    Hand calculation:
      arrivals (ms): 0, 5, 20, 25, 40
      gaps (ms):     5, 15,  5, 15
      deltas (ms):  |15-5|=10, |5-15|=10, |15-5|=10
      mean delta:   10 ms  →  jitter = 10.0 ms
    """
    # Arrivals in nanoseconds: 0, 5ms, 20ms, 25ms, 40ms
    arrivals_ns = [0, 5_000_000, 20_000_000, 25_000_000, 40_000_000]
    result = compute_jitter(arrivals_ns)

    # Use pytest.approx() because floating-point arithmetic is not exact.
    # rel=1e-6 means "within 0.0001% of the expected value".
    assert result == pytest.approx(10.0, rel=1e-6), (
        f"Expected 10.0 ms jitter for alternating gaps, got {result}"
    )


def test_jitter_returns_float():
    """
    compute_jitter() must always return a float, not an int.
    Python's statistics.mean() returns a float, but this guards against
    accidental integer division or type changes in the future.
    """
    result = compute_jitter([0, 1_000_000, 3_000_000, 6_000_000])
    assert isinstance(result, float), f"Expected float, got {type(result)}"


# ─── Section 2: Unit tests for save_result() ─────────────────────────────────
#
# save_result(row: dict, filepath: str) -> None
#
# We write to temporary files so tests never pollute results/udp_results.csv.
# tempfile.NamedTemporaryFile creates a real file that is deleted automatically
# when the test ends.

def _make_row(**overrides) -> dict:
    """
    Helper that builds a valid result row with sensible defaults.
    Pass keyword arguments to override specific fields.

    Example:
        _make_row(loss_rate_pct=5.0)   # overrides just loss_rate_pct
    """
    base = {
        "protocol":        "UDP",
        "payload_bytes":   1024,
        "buffer_bytes":    65536,
        "condition":       "baseline",
        "throughput_mbps": 8.0,
        "loss_rate_pct":   0.0,
        "jitter_ms":       0.5,
        "run_index":       1,
    }
    base.update(overrides)
    return base


def test_save_result_creates_file():
    """
    Calling save_result() on a path that does not exist yet should create
    the file and write the header row followed by the data row.
    """
    # NamedTemporaryFile(delete=False) creates a real file we can pass to
    # save_result(). We delete it manually at the end.
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp_path = tmp.name

    # The file now exists but is empty — delete it so save_result creates it.
    os.remove(tmp_path)

    try:
        save_result(_make_row(), filepath=tmp_path)

        assert os.path.exists(tmp_path), "save_result() did not create the file"

        with open(tmp_path, newline="") as f:
            rows = list(csv.DictReader(f))

        # Should have exactly one data row.
        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
        assert rows[0]["protocol"] == "UDP"
        assert rows[0]["payload_bytes"] == "1024"   # CSV values are always strings
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_save_result_writes_header_once():
    """
    The header row (column names) should appear exactly once even when
    save_result() is called multiple times. If we called writeheader()
    unconditionally, the CSV would have a header between every row.
    """
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp_path = tmp.name
    os.remove(tmp_path)

    try:
        save_result(_make_row(run_index=1), filepath=tmp_path)
        save_result(_make_row(run_index=2), filepath=tmp_path)
        save_result(_make_row(run_index=3), filepath=tmp_path)

        with open(tmp_path, newline="") as f:
            # Read raw lines to count header occurrences.
            lines = f.readlines()

        # The first line should be the header. Check it appears only once.
        header_lines = [l for l in lines if l.startswith("protocol")]
        assert len(header_lines) == 1, (
            f"Expected header to appear once, found {len(header_lines)} times"
        )

        # And we should have 3 data rows + 1 header = 4 lines total.
        assert len(lines) == 4, f"Expected 4 lines (1 header + 3 data), got {len(lines)}"
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_save_result_appends_rows():
    """
    Each call to save_result() appends a new row. The CSV should contain
    all rows in order.
    """
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp_path = tmp.name
    os.remove(tmp_path)

    try:
        save_result(_make_row(run_index=1, throughput_mbps=10.0), filepath=tmp_path)
        save_result(_make_row(run_index=2, throughput_mbps=12.0), filepath=tmp_path)

        with open(tmp_path, newline="") as f:
            rows = list(csv.DictReader(f))

        assert len(rows) == 2
        assert rows[0]["run_index"] == "1"
        assert rows[1]["run_index"] == "2"
        assert float(rows[1]["throughput_mbps"]) == pytest.approx(12.0)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_save_result_all_columns_present():
    """
    The saved row must contain every column defined in CSV_HEADER —
    no missing fields, no extra fields.
    """
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp_path = tmp.name
    os.remove(tmp_path)

    try:
        save_result(_make_row(), filepath=tmp_path)

        with open(tmp_path, newline="") as f:
            reader = csv.DictReader(f)
            next(reader)  # advance past header so fieldnames is populated

        # reader.fieldnames comes from the header row written by save_result().
        # We guard against None (empty file) even though save_result() always
        # writes a header — this makes the type checker happy.
        fieldnames = reader.fieldnames or []
        assert set(fieldnames) == set(CSV_HEADER), (
            f"Column mismatch.\n  Expected: {sorted(CSV_HEADER)}\n  Got: {sorted(fieldnames)}"
        )
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# ─── Section 3: Unit tests for run_udp_sender() input validation ──────────────
#
# These tests check the guard clause at the top of run_udp_sender() without
# actually sending any network traffic.

def test_sender_rejects_payload_below_header_size():
    """
    A payload smaller than HEADER_SIZE (16 bytes) can't fit the sequence number
    and timestamp. run_udp_sender() must raise ValueError immediately.

    pytest.raises(ValueError) is a context manager: the test passes if the
    indented block raises ValueError, and fails if it does not.
    """
    with pytest.raises(ValueError, match="HEADER_SIZE"):
        # payload_size=8 is below the 16-byte minimum.
        # The other arguments don't matter — the exception fires before any I/O.
        run_udp_sender(
            host          = "127.0.0.1",
            port          = 5301,
            payload_size  = 8,        # too small — should raise ValueError
            buffer_size   = 65536,
            n_messages    = 1,
            send_rate_pps = 1,
        )


def test_sender_rejects_zero_payload():
    """
    Zero is also below HEADER_SIZE. Tests that the check handles the boundary.
    """
    with pytest.raises(ValueError):
        run_udp_sender("127.0.0.1", 5301, 0, 65536, 1, 1)


def test_sender_accepts_minimum_payload():
    """
    payload_size == HEADER_SIZE (16 bytes) is the minimum valid size.
    The sender should start without raising. We send exactly 1 message to keep
    the test fast.

    Note: this test sends a real datagram to port 5301. If no receiver is
    listening, the datagram is silently dropped by the OS — that is normal UDP
    behavior and does not cause an error on the sender side.
    """
    # Should complete without raising any exception.
    run_udp_sender(
        host          = "127.0.0.1",
        port          = 5301,
        payload_size  = HEADER_SIZE,   # exactly 16 bytes — the minimum valid value
        buffer_size   = 65536,
        n_messages    = 1,
        send_rate_pps = 500,
    )


# ─── Section 4: Integration test for run_udp_experiment() ────────────────────
#
# This test spins up the real receiver thread and real sender on loopback.
# It is slower than the unit tests (a few seconds) because it sends real
# datagrams and waits for the receiver timeout.
#
# We use a small message count (50 messages at 200 pps = ~0.25 s of sending
# + 2 s receiver timeout = ~2.5 s total) to keep CI fast.

def test_experiment_basic_loopback():
    """
    End-to-end smoke test: send 50 datagrams on loopback and verify that
    the returned metrics are in sensible ranges.

    We write results to a temporary file to avoid polluting the real CSV.

    Why < 1.0% loss instead of == 0.0?
    ------------------------------------
    Loopback is essentially lossless, but test environments (CI, shared VMs)
    can occasionally drop a packet due to scheduling jitter or OS buffer limits.
    Asserting strict zero would make the test flaky. Less-than-1% is tight
    enough to catch real bugs while tolerating rare OS noise.
    """
    import udp_module   # import the module so we can temporarily patch RESULTS_PATH

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp_path = tmp.name
    os.remove(tmp_path)

    # Temporarily redirect CSV output to the temp file.
    original_path = udp_module.RESULTS_PATH
    udp_module.RESULTS_PATH = tmp_path

    try:
        throughput_mbps, loss_rate_pct, jitter_ms, avg_owd_ms = run_udp_experiment(
            payload_size  = 512,    # 512-byte datagrams — well above HEADER_SIZE
            buffer_size   = 65536,  # 64 KB socket buffers — generous for 50 packets
            n_messages    = 50,     # small count so the test finishes in ~2.5 s
            send_rate_pps = 200,    # 200 pps → one packet every 5 ms
            label         = "test", # condition label written to CSV
            run_index     = 1,
        )

        # ── Throughput ───────────────────────────────────────────────────────
        # At 200 pps × 512 B = 102,400 B/s ≈ 0.82 Mbps.
        # We allow a wide range because loopback can be faster or slower
        # depending on scheduling.
        assert throughput_mbps > 0.0, (
            f"Throughput should be positive, got {throughput_mbps}"
        )

        # ── Loss ─────────────────────────────────────────────────────────────
        # Loopback should deliver all 50 packets. Allow up to 1% (< 1 packet).
        assert loss_rate_pct < 1.0, (
            f"Expected < 1% loss on loopback, got {loss_rate_pct:.2f}%"
        )

        # ── Jitter ───────────────────────────────────────────────────────────
        # Jitter must be a non-negative number. We don't assert a tight upper
        # bound because macOS scheduler resolution varies; we just check sanity.
        assert jitter_ms >= 0.0, f"Jitter must be non-negative, got {jitter_ms}"

        # ── One-way delay ────────────────────────────────────────────────────
        # OWD is arrival_ns - send_ns on the same machine, so it should be a
        # small positive number (microseconds to low milliseconds on loopback).
        # We just assert it is non-negative and below 100 ms as a sanity check.
        assert avg_owd_ms >= 0.0, f"OWD must be non-negative, got {avg_owd_ms}"
        assert avg_owd_ms < 100.0, f"OWD suspiciously large on loopback: {avg_owd_ms:.2f}ms"

        # ── CSV output ───────────────────────────────────────────────────────
        # Verify the experiment actually wrote one row to the CSV.
        assert os.path.exists(tmp_path), "run_udp_experiment() did not create the CSV"
        with open(tmp_path, newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1, f"Expected 1 CSV row, got {len(rows)}"
        assert rows[0]["protocol"] == "UDP"
        assert rows[0]["condition"] == "test"

    finally:
        # Restore the original path regardless of whether the test passed.
        udp_module.RESULTS_PATH = original_path
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
