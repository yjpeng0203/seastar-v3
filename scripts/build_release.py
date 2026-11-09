#!/usr/bin/env python3
"""Build a clean, reproducible offline deployment archive (seastar-v3-ipc.tar.gz) for the Linux IPC."""
import argparse
import hashlib
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".organizer_work",
    ".sync",
    ".archive",
    "runtime",
    "state",
    "mirror",
    "dist",
    "build",
    "results",
}

EXCLUDED_FILES = {
    ".DS_Store",
    "ipc.json",
    "replay.json",
}

EXCLUDED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".sqlite3",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".dat",
    ".root",
    ".tar.gz",
}


def should_include(rel_path: Path) -> bool:
    for part in rel_path.parts[:-1]:
        if part in EXCLUDED_DIRS or part.startswith("."):
            return False
    name = rel_path.name
    if name in EXCLUDED_FILES or name.startswith("."):
        return False
    for suffix in EXCLUDED_SUFFIXES:
        if name.lower().endswith(suffix):
            return False
    if rel_path.parts[:2] == ("fee-monitor", "config.json"):
        return False
    if rel_path.parts[:2] == ("fee-monitor", "data") and name.lower().endswith(".csv"):
        return False
    return True


def build_archive(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / "seastar-v3-ipc.tar.gz"
    sha_path = output_dir / "seastar-v3-ipc.tar.gz.sha256"

    files = []
    for path in sorted(ROOT.rglob("*"), key=lambda p: p.as_posix()):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if should_include(rel):
            files.append((path, rel))

    def _clean_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        if info.name.endswith((".sh", "ssh_askpass.py", "build_release.py")):
            info.mode = 0o755
        else:
            info.mode = 0o644
        return info

    with tarfile.open(archive_path, "w:gz") as tar:
        for abs_path, rel in files:
            arcname = f"seastar-v3/{rel.as_posix()}"
            tar.add(abs_path, arcname=arcname, recursive=False, filter=_clean_tarinfo)
        # Include empty fee-monitor/data directory placeholder
        data_info = tarfile.TarInfo("seastar-v3/fee-monitor/data")
        data_info.type = tarfile.DIRTYPE
        data_info.mode = 0o755
        data_info.mtime = 1730000000
        tar.addfile(data_info)

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    sha_path.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
    return archive_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "dist",
        help="输出目录（默认 seastar-v3/dist）",
    )
    args = parser.parse_args()
    archive = build_archive(args.output_dir.resolve())
    size_mib = archive.stat().st_size / (1024 * 1024)
    digest = (archive.parent / f"{archive.name}.sha256").read_text(encoding="utf-8").split()[0]
    print(f"已生成工控机离线部署包: {archive} ({size_mib:.2f} MiB)")
    print(f"SHA-256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
