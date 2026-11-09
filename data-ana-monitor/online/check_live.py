#!/usr/bin/env python3
"""Replay a real DAT with random appends, split volumes and a mid-stream restart."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import threading
import time

from acquisition import DatParser, NAME
from server import Monitor


def check(sample):
    data = sample.read_bytes()
    expected_triggers = expected_waves = 0
    for record in DatParser().feed(data):
        expected_triggers += record["type"] == "trigger"
        expected_waves += record["type"] == "wave"
    match = NAME.match(sample.name)
    prefix, stamp = (match[1], match[3]) if match else ("data_fee_demo_run000001", "20260101000000")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        capture, state = root / "capture", root / "state"
        capture.mkdir()
        stopped, errors = threading.Event(), []
        def writer():
            rng = random.Random(17)
            start = time.monotonic()
            pos, volume = 0, 0
            try:
                while pos < len(data) and not stopped.is_set():
                    volume += 1
                    end = min(len(data), pos + 256 * 1024 + 7)  # intentionally unaligned
                    with (capture / f"{prefix}_idx{volume:03d}_{stamp}.dat").open("xb") as stream:
                        while pos < end and not stopped.is_set():
                            size = min(end - pos, rng.randint(1, 8192))
                            stream.write(data[pos:pos + size]);stream.flush();pos += size
                            delay = pos / (3 * 1024**2) - (time.monotonic() - start)
                            if delay > 0:
                                stopped.wait(delay)
            except Exception as error:
                errors.append(str(error))
        monitor = Monitor(capture, state, poll=.1)
        monitor.start()
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        start = time.monotonic()
        def wait_for(predicate):
            while time.monotonic() - start < 90:
                snap = monitor.snapshot()
                if errors or snap.get("error"):
                    raise AssertionError(errors or snap["error"])
                if predicate(snap):
                    return snap
                time.sleep(.05)
            raise AssertionError("Timed out while replaying real data")
        try:
            before = wait_for(lambda s: s.get("waves", 0) >= min(1000, expected_waves))
            run_id = before["run_id"]
            monitor.close()
            monitor = Monitor(capture, state, poll=.1)
            monitor.start()
            after = wait_for(lambda s: not thread.is_alive() and s.get("waves") == expected_waves and
                             s.get("triggers") == expected_triggers and not s.get("pending_bytes") and not s.get("backlog"))
            assert run_id == after["run_id"], "restart did not restore the same session"
            assert not after["bad_bytes"] and not after["orphan_waves"]
            files = sorted(capture.glob("*.dat"))
            assert hashlib.sha256(b"".join(f.read_bytes() for f in files)).digest() == hashlib.sha256(data).digest()
            report = {"input": sample.name, "input_sha256": hashlib.sha256(data).hexdigest(),
                      "random_appends": True, "unaligned_volumes": len(files), "midstream_restart": True,
                      "same_session_restored": True, "triggers": after["triggers"], "waves": after["waves"],
                      "good": after["good"], "pending_bytes": after["pending_bytes"],
                      "bad_bytes": after["bad_bytes"], "relative_span_s": after["relative_s"],
                      "analysis_sha256": after["analysis_sha256"], "elapsed_s": time.monotonic() - start,
                      "live_hardware_connected": False}
        finally:
            stopped.set();thread.join();monitor.close()
        db = sqlite3.connect(state / "analysis.sqlite3")
        count, distinct = db.execute("SELECT COUNT(*),COUNT(DISTINCT seq) FROM metrics WHERE run=?", (run_id,)).fetchone()
        db.close()
        assert count == distinct == expected_waves, "metric duplication or loss after restart"
        report.update({"persisted_metrics": count, "unique_metric_seq": distinct, "passed": True})
        return report


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("sample", type=Path)
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    result = check(args.sample)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
