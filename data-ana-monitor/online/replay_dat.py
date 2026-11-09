#!/usr/bin/env python3
"""Append an existing DAT into a NEW directory, including arbitrary half-frame rotations."""
import argparse
from datetime import datetime
from pathlib import Path
import random
import time

from acquisition import NAME


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("input", type=Path)
    cli.add_argument("--output-dir", type=Path, required=True, help="必须为尚不存在的新目录")
    cli.add_argument("--mib-per-second", type=float, default=1.)
    cli.add_argument("--rotate-mib", type=float, default=100.)
    args = cli.parse_args()
    if not args.input.is_file() or args.mib_per_second <= 0 or args.rotate_mib <= 0:
        cli.error("输入文件不存在或速率/分卷大小无效")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    match = NAME.match(args.input.name)
    prefix, stamp = (match[1], match[3]) if match else ("data_fee_demo_run000001", datetime.now().strftime("%Y%m%d%H%M%S"))
    volume_size = max(1, int(args.rotate_mib * 1024**2))
    rate = args.mib_per_second * 1024**2
    rng = random.Random(20260809)
    total, volume, written, output = 0, 0, 0, None
    start = time.monotonic()
    print("Growing DAT directory: " + str(args.output_dir.resolve()), flush=True)
    try:
        with args.input.open("rb") as source:
            while True:
                if output is None or written == volume_size:
                    if output:
                        output.close()
                    volume += 1
                    written = 0
                    output = (args.output_dir / f"{prefix}_idx{volume:03d}_{stamp}.dat").open("xb")
                data = source.read(min(volume_size - written, rng.randint(1, 32768)))
                if not data:
                    break
                output.write(data)
                output.flush()
                total += len(data)
                written += len(data)
                # Small sleeps keep Ctrl+C responsive and expose partial headers/payloads to readers.
                delay = total / rate - (time.monotonic() - start)
                while delay > 0:
                    time.sleep(min(delay, .2))
                    delay = total / rate - (time.monotonic() - start)
        print(f"Replay complete: {total} bytes, {volume} volumes. Hardware timestamps unchanged.", flush=True)
    except KeyboardInterrupt:
        print("Replay stopped; partial last record is intentionally retained.", flush=True)
    finally:
        if output:
            output.close()


if __name__ == "__main__":
    main()
