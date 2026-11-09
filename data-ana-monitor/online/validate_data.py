#!/usr/bin/env python3
"""Full data replay: parser integrity, shared-kernel results, DAT/ROOT identity."""
import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import time

import numpy as np

from acquisition import DatParser, DatReader, RootReader, discover
from analysis import Analyzer, Config, VERSION
from engine import Engine


def validate(directory, source, analyzer, config):
    parser = DatParser()
    reader = DatReader(directory) if source["kind"] == "dat" else RootReader(directory, source)
    engine = Engine(config, analyzer)
    digest = hashlib.sha256()
    points, negative_good = Counter(), 0
    start = time.perf_counter()
    try:
        while True:
            incoming = reader.read(source)
            if not incoming:
                break
            records = parser.feed(incoming) if source["kind"] == "dat" else incoming
            for record in records:
                result = engine.consume(record)
                if result is None:
                    continue
                points[len(record["samples"])] += 1
                negative_good += bool(result["good"] and result["area"] < 0)
                digest.update(struct.pack(">HQBBBQH", engine.pending["number"], engine.pending["ns"],
                                          record["board"], record["channel"], record["wave_id"],
                                          record["ns"], len(record["samples"])))
                digest.update(np.asarray(record["samples"], dtype="<u2").tobytes())
        snap = engine.snapshot()
        assert sum(c["n"] for c in snap["channels"]) == snap["waves"]
        assert sum(snap["multiplicity"].values()) == snap["triggers"]
        assert sum(int(k) * v for k, v in snap["multiplicity"].items()) == snap["waves"]
        assert not negative_good, "A good main-pulse integral should not be negative"
        assert not parser.bad_bytes and not parser.orphans, "DAT framing errors"
        assert not parser.buffer, "Input ends in an incomplete record"
        for channel in snap["channels"]:
            for population in channel["stats"].values():
                for distribution in population.values():
                    assert sum(n for _, n in distribution["hist"]) == distribution["n"]
        return {"source": source["key"], "kind": source["kind"], "files": source["files"],
                "triggers": snap["triggers"], "wave_events": snap["wave_events"],
                "waves": snap["waves"], "good": snap["good"], "wave_lengths": dict(points),
                "raw_header_backsteps": snap["header_backsteps"], "wave_clock_backsteps": snap["clock_backsteps"],
                "relative_span_s": snap["relative_s"], "wave_stream_sha256": digest.hexdigest(),
                "elapsed_s": time.perf_counter() - start,
                "channels": [{"key": c["key"], "waves": c["n"], "good": c["good"], "flags": c["flags"],
                              "all": c["stats"]["all"], "clean": c["stats"]["good"]} for c in snap["channels"]]}
    finally:
        if hasattr(reader, "close"):
            reader.close()


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent.parent)
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    config = Config()
    with tempfile.TemporaryDirectory() as build:
        analyzer = Analyzer(config, build)
        sources = discover(args.data_dir)
        results = []
        for source in sources:
            print("Validating " + source["key"], flush=True)
            result = validate(args.data_dir, source, analyzer, config)
            results.append(result)
            print(json.dumps({k: result[k] for k in ("kind", "triggers", "waves", "good", "relative_span_s", "elapsed_s")}), flush=True)
        dat_hashes = {r["wave_stream_sha256"] for r in results if r["kind"] == "dat"}
        matches = [r["source"] for r in results if r["kind"] == "root" and r["wave_stream_sha256"] in dat_hashes]
        by_source = {r["source"]: r for r in results}
        for result in results:
            if result["kind"] == "dat" and len(result["files"]) == 1:
                counterpart = str(Path(result["files"][0]["name"]).with_suffix(".root"))
                if counterpart in by_source:
                    assert result["wave_stream_sha256"] == by_source[counterpart]["wave_stream_sha256"], "DAT/ROOT stream mismatch"
        report = {"version": VERSION, "config": asdict(config), "results": results,
                  "kernel_sha256": hashlib.sha256((Path(__file__).parent / "adaptive_waveform.h").read_bytes()).hexdigest(),
                  "root_streams_identical_to_dat": matches,
                  "scope": "Full record/sample scan and algorithm consistency; not an absolute charge or detection-efficiency calibration."}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        print("Report: " + str(args.output.resolve()), flush=True)


if __name__ == "__main__":
    main()
