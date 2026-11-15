"""Tests for the disk-backed ResultStore.

The store must keep process memory flat while results accumulate on disk,
never hand out links for results that failed to save, reject expired tokens,
and let downloads that already started run to completion even when expiry
cleanup removes the file mid-stream.
"""

import gc
import os
import time

import pytest

from app.main import ResultStore


@pytest.fixture()
def store(tmp_path):
    instance = ResultStore(tmp_path / "results", sweep_interval=None)
    yield instance
    instance.close()


def read_all(opened):
    item, handle = opened
    try:
        return item, handle.read()
    finally:
        handle.close()


def test_put_then_download_returns_full_payload(store):
    payload = "厂区,月份\n华东厂,2025-01\n".encode("utf-8-sig")
    token = store.put("ledger_standardized.csv", payload)
    opened = store.open_download(token)
    assert opened is not None
    item, data = read_all(opened)
    assert item.filename == "ledger_standardized.csv"
    assert item.size == len(payload)
    assert data == payload


def test_unknown_token_is_rejected(store):
    assert store.open_download("not-a-real-token") is None


def test_expired_token_is_rejected(tmp_path):
    instance = ResultStore(tmp_path, ttl=0.05, sweep_interval=None)
    try:
        token = instance.put("a.csv", b"x")
        time.sleep(0.1)
        assert instance.open_download(token) is None
        assert list(tmp_path.iterdir()) == []  # file cleaned up too
    finally:
        instance.close()


def test_inflight_download_survives_expiry_cleanup(tmp_path):
    # A real background sweeper removes the expired entry while the download
    # is in progress; the open descriptor must keep reading to the end.
    instance = ResultStore(tmp_path, ttl=0.05, sweep_interval=0.01)
    try:
        payload = b"data" * 16384  # 64 KiB
        token = instance.put("a.csv", payload)
        item, handle = instance.open_download(token)  # download starts
        assert handle is not None
        time.sleep(0.3)  # entry expires; sweeper unlinks the file
        assert instance.open_download(token) is None  # new requests rejected
        assert not item.path.exists()  # cleanup really removed the file
        chunks = []
        while True:
            chunk = handle.read(4096)
            if not chunk:
                break
            chunks.append(chunk)
        handle.close()
        assert b"".join(chunks) == payload  # started download completes whole
    finally:
        instance.close()


def test_capacity_evicts_oldest(tmp_path):
    instance = ResultStore(tmp_path, ttl=3600, capacity=2, sweep_interval=None)
    try:
        tokens = [instance.put(f"{i}.csv", str(i).encode()) for i in range(3)]
        assert instance.open_download(tokens[0]) is None
        for token in tokens[1:]:
            opened = instance.open_download(token)
            assert opened is not None
            opened[1].close()
        assert len(list(tmp_path.glob("*.csv"))) == 2
    finally:
        instance.close()


def test_failed_save_raises_and_registers_nothing(store):
    os.chmod(store._dir, 0o555)
    try:
        with pytest.raises(OSError):
            store.put("a.csv", b"payload")
    finally:
        os.chmod(store._dir, 0o755)
    assert len(store) == 0
    assert list(store._dir.iterdir()) == []  # no partial file left behind


def test_restart_wipes_previous_results(tmp_path):
    first = ResultStore(tmp_path, sweep_interval=None)
    token = first.put("a.csv", b"stale")
    first.close()
    second = ResultStore(tmp_path, sweep_interval=None)
    try:
        assert second.open_download(token) is None
        assert list(tmp_path.iterdir()) == []
    finally:
        second.close()


def _rss_bytes() -> int:
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    raise RuntimeError("VmRSS not found")


@pytest.mark.skipif(
    not os.path.exists("/proc/self/status"), reason="needs procfs for RSS"
)
def test_memory_stays_flat_while_results_accumulate(tmp_path):
    # 45 ledgers x 4 MiB = 180 MiB of results: far more than the container
    # could ever hold alongside the app, so any per-result retention shows.
    instance = ResultStore(tmp_path, ttl=3600, sweep_interval=None)
    try:
        payload = os.urandom(4 * 1024 * 1024)
        warm = instance.put("warmup.csv", payload)  # absorb one-off allocator growth
        instance.open_download(warm)[1].close()
        gc.collect()
        before = _rss_bytes()
        tokens = [instance.put(f"ledger_{i}.csv", payload) for i in range(45)]
        gc.collect()
        growth = _rss_bytes() - before
        total = 45 * len(payload)
        assert growth < total // 4, (
            f"RSS grew by {growth} bytes for {total} bytes of results"
        )
        # Every link still serves its own complete file.
        check = tokens[0], tokens[22], tokens[-1]
        for token in check:
            item, data = read_all(instance.open_download(token))
            assert item.size == len(payload)
            assert data == payload
        assert len(list(tmp_path.glob("*.csv"))) == 46
    finally:
        instance.close()
