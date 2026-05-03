"""
Tests for the buffer size sweep
================================

How to run:
    uv run pytest tests/test_buffer_sweep.py -v

What these tests cover
-----------------------
1. TCP module accepts a small buffer (4096B) and returns valid metrics
2. TCP module accepts a large buffer (262144B) and returns valid metrics
3. UDP module accepts a small buffer (4096B) and returns valid metrics
4. UDP module accepts a large buffer (262144B) and returns valid metrics
5. buffer_bytes is recorded correctly in the CSV for both modules
6. scripts/05_buffer_sweep.sh exists and is executable
7. analyze.py plot_6 and plot_7 skip gracefully when no buffer-sweep data exists
8. analyze.py plot_6 and plot_7 run successfully with buffer-sweep data

Notes
-----
- We do NOT assert that small buffers produce lower throughput than large ones.
  On loopback the OS often overrides SO_SNDBUF/SO_RCVBUF silently (it doubles
  the requested value and may also apply system-level caps). The test verifies
  the pipeline functions correctly with different buffer values, not that the
  kernel honours the exact size requested.

- Tests redirect CSV output to a tempfile via the RESULTS_PATH patch pattern
  used throughout this project — see tests/test_tcp_module.py for background.
"""

import os
import csv
import tempfile

import pytest
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pandas as pd

from tcp_module import run_tcp_experiment
from udp_module import run_udp_experiment

# ─── Helpers ────────────────────────────��────────────────────────────��────────

def _tcp_run(buffer_size: int, tmp_path: str) -> tuple[float, float, float]:
    """Run one TCP experiment cell redirected to tmp_path."""
    import tcp_module
    original = tcp_module.RESULTS_PATH
    tcp_module.RESULTS_PATH = tmp_path
    try:
        return run_tcp_experiment(
            payload_size = 512,
            buffer_size  = buffer_size,
            n_messages   = 50,
            label        = "test",
            run_index    = 1,
        )
    finally:
        tcp_module.RESULTS_PATH = original


def _udp_run(buffer_size: int, tmp_path: str) -> tuple[float, float, float, float]:
    """Run one UDP experiment cell redirected to tmp_path."""
    import udp_module
    original = udp_module.RESULTS_PATH
    udp_module.RESULTS_PATH = tmp_path
    try:
        return run_udp_experiment(
            payload_size  = 512,
            buffer_size   = buffer_size,
            n_messages    = 50,
            send_rate_pps = 200,
            label         = "test",
            run_index     = 1,
        )
    finally:
        udp_module.RESULTS_PATH = original


# ─── Section 1: TCP with small and large buffers ──────────────────────────────

def test_tcp_small_buffer_valid_metrics():
    """
    TCP experiment with a 4KB socket buffer must return positive throughput
    and a valid RTT. A small buffer may reduce throughput but must not crash.
    """
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        tmp = f.name
    os.remove(tmp)

    try:
        throughput, avg_lat, stdev_lat = _tcp_run(4096, tmp)
        assert throughput > 0.0,   f"Expected positive throughput, got {throughput}"
        assert avg_lat > 0.0,      f"Expected positive RTT, got {avg_lat}"
        assert stdev_lat >= 0.0,   f"stdev must be non-negative, got {stdev_lat}"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def test_tcp_large_buffer_valid_metrics():
    """
    TCP experiment with a 262144B (256KB) socket buffer must return positive
    throughput. A large buffer should not cause errors.
    """
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        tmp = f.name
    os.remove(tmp)

    try:
        throughput, avg_lat, stdev_lat = _tcp_run(262144, tmp)
        assert throughput > 0.0,  f"Expected positive throughput, got {throughput}"
        assert avg_lat > 0.0,     f"Expected positive RTT, got {avg_lat}"
        assert stdev_lat >= 0.0,  f"stdev must be non-negative, got {stdev_lat}"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ─── Section 2: UDP with small and large buffers ──────────────────────────────

def test_udp_small_buffer_valid_metrics():
    """
    UDP experiment with a 4KB socket buffer must return valid metrics.
    A small SO_RCVBUF may cause some loss (the OS drops datagrams when the
    buffer fills), so we assert loss_rate >= 0 rather than == 0.
    """
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        tmp = f.name
    os.remove(tmp)

    try:
        throughput, loss, jitter, owd = _udp_run(4096, tmp)
        assert throughput >= 0.0,  f"Expected non-negative throughput, got {throughput}"
        assert 0.0 <= loss <= 100.0, f"Loss must be 0–100%, got {loss}"
        assert jitter >= 0.0,      f"jitter must be non-negative, got {jitter}"
        assert owd >= 0.0,         f"OWD must be non-negative, got {owd}"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def test_udp_large_buffer_valid_metrics():
    """
    UDP experiment with a 262144B (256KB) socket buffer must return valid
    metrics. A large receive buffer should cause no loss on a quiet loopback.
    """
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        tmp = f.name
    os.remove(tmp)

    try:
        throughput, loss, jitter, owd = _udp_run(262144, tmp)
        assert throughput > 0.0,  f"Expected positive throughput, got {throughput}"
        assert loss == 0.0,       f"Expected zero loss with large buffer, got {loss}%"
        assert jitter >= 0.0,     f"jitter must be non-negative, got {jitter}"
        assert owd > 0.0,         f"Expected positive OWD, got {owd}"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ─── Section 3: buffer_bytes recorded correctly in CSV ────────────────────────

def test_tcp_buffer_bytes_written_to_csv():
    """
    The buffer_bytes column in tcp_results.csv must reflect the buffer size
    that was passed to run_tcp_experiment(), not the default 65536.
    """
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        tmp = f.name
    os.remove(tmp)

    try:
        _tcp_run(16384, tmp)

        with open(tmp, newline="") as f:
            rows = list(csv.DictReader(f))

        assert len(rows) == 1
        assert int(rows[0]["buffer_bytes"]) == 16384, (
            f"Expected buffer_bytes=16384, got {rows[0]['buffer_bytes']}"
        )
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def test_udp_buffer_bytes_written_to_csv():
    """
    The buffer_bytes column in udp_results.csv must reflect the buffer size
    that was passed to run_udp_experiment(), not the default 65536.
    """
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        tmp = f.name
    os.remove(tmp)

    try:
        _udp_run(16384, tmp)

        with open(tmp, newline="") as f:
            rows = list(csv.DictReader(f))

        assert len(rows) == 1
        assert int(rows[0]["buffer_bytes"]) == 16384, (
            f"Expected buffer_bytes=16384, got {rows[0]['buffer_bytes']}"
        )
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ─── Section 4: script exists and is executable ───────────────────────────────

def test_buffer_sweep_script_exists():
    """scripts/05_buffer_sweep.sh must be present in the repository."""
    project_root = os.path.join(os.path.dirname(__file__), "..")
    script_path = os.path.join(project_root, "scripts", "05_buffer_sweep.sh")
    assert os.path.isfile(script_path), (
        f"scripts/05_buffer_sweep.sh not found at {script_path}"
    )


def test_buffer_sweep_script_is_executable():
    """scripts/05_buffer_sweep.sh must have the executable bit set."""
    project_root = os.path.join(os.path.dirname(__file__), "..")
    script_path = os.path.join(project_root, "scripts", "05_buffer_sweep.sh")
    assert os.access(script_path, os.X_OK), (
        f"scripts/05_buffer_sweep.sh is not executable. Run: chmod +x {script_path}"
    )


# ─── Section 5: analyze.py plot_6 and plot_7 handle empty data gracefully ─────

def test_plot_6_skips_on_empty_dataframe(capsys):
    """
    plot_6_throughput_vs_buffer() must print a skip message and return
    without crashing when passed empty DataFrames (no buffer-sweep data yet).
    """
    import analyze
    empty_tcp = pd.DataFrame(columns=["buffer_bytes", "condition",
                                       "throughput_mbps_mean", "throughput_mbps_std"])
    empty_udp = pd.DataFrame(columns=["buffer_bytes", "condition",
                                       "throughput_mbps_mean", "throughput_mbps_std"])

    analyze.plot_6_throughput_vs_buffer(empty_tcp, empty_udp)

    captured = capsys.readouterr()
    assert "Skipping" in captured.out, (
        "Expected 'Skipping' in output when no buffer-sweep data is present"
    )


def test_plot_7_skips_on_empty_dataframe(capsys):
    """
    plot_7_owd_vs_buffer() must print a skip message and return without
    crashing when passed an empty DataFrame.
    """
    import analyze
    empty_udp = pd.DataFrame(columns=["buffer_bytes", "condition",
                                       "avg_owd_ms_mean", "avg_owd_ms_std"])

    analyze.plot_7_owd_vs_buffer(empty_udp)

    captured = capsys.readouterr()
    assert "Skipping" in captured.out, (
        "Expected 'Skipping' in output when no buffer-sweep data is present"
    )


# ─── Section 6: plot_6 and plot_7 produce files with real data ────────────────

def test_plot_6_generates_file():
    """
    plot_6_throughput_vs_buffer() must save a PNG when valid buffer-sweep
    data is provided.
    """
    import analyze

    # Build a minimal aggregated DataFrame with two buffer sizes.
    tcp_agg = pd.DataFrame({
        "buffer_bytes":          [4096,  65536],
        "condition":             ["baseline", "baseline"],
        "throughput_mbps_mean":  [200.0, 800.0],
        "throughput_mbps_std":   [10.0,  20.0],
    })
    udp_agg = pd.DataFrame({
        "buffer_bytes":          [4096,  65536],
        "condition":             ["baseline", "baseline"],
        "throughput_mbps_mean":  [3.0,   3.2],
        "throughput_mbps_std":   [0.1,   0.1],
    })

    # Redirect plots to a temp directory.
    original_dir = analyze.PLOTS_DIR
    with tempfile.TemporaryDirectory() as tmpdir:
        analyze.PLOTS_DIR = tmpdir
        try:
            analyze.plot_6_throughput_vs_buffer(tcp_agg, udp_agg)
            assert os.path.exists(os.path.join(tmpdir, "06_throughput_vs_buffer.png")), (
                "plot_6 did not create 06_throughput_vs_buffer.png"
            )
        finally:
            analyze.PLOTS_DIR = original_dir


def test_plot_7_generates_file():
    """
    plot_7_owd_vs_buffer() must save a PNG when valid buffer-sweep data is provided.
    """
    import analyze

    udp_agg = pd.DataFrame({
        "buffer_bytes":      [4096,  65536,  4096,   65536],
        "condition":         ["baseline", "baseline", "bufferbloat", "bufferbloat"],
        "avg_owd_ms_mean":   [0.08,  0.09,   1.2,    5.8],
        "avg_owd_ms_std":    [0.01,  0.01,   0.1,    0.3],
    })

    original_dir = analyze.PLOTS_DIR
    with tempfile.TemporaryDirectory() as tmpdir:
        analyze.PLOTS_DIR = tmpdir
        try:
            analyze.plot_7_owd_vs_buffer(udp_agg)
            assert os.path.exists(os.path.join(tmpdir, "07_owd_vs_buffer.png")), (
                "plot_7 did not create 07_owd_vs_buffer.png"
            )
        finally:
            analyze.PLOTS_DIR = original_dir
