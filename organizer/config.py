"""Configuration model and board-to-string (G0..G6) mapping for SeaStar v2 organizer."""
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, Optional
import warnings

DEFAULT_BOARD_MAP: Dict[str, str] = {str(i): f"G{i}" for i in range(7)}


@dataclass
class OrganizerConfig:
    raw_data_dir: str = "/home/ipc/disk/debug/data/both"
    decoder_bin: str = "../../build/decoder"
    decoder_config: str = "../../cfg/config.json"
    hadd_bin: str = "hadd"
    root_bin: str = "root"
    backend: str = "auto"  # "auto" | "ipc" | "pyroot"
    csv_mirror_dir: str = "~/.local/share/fee-monitor/mirror"
    output_root: str = "/home/ipc/disk/debug/data/organized"
    work_dir: str = ""
    layout: str = "group"  # "group" (YYYYMMDD_MMDD/Gx/{raw_data,rec_data,csv}) | "flat"
    board_map: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_BOARD_MAP))
    link_mode: str = "hardlink"  # "hardlink" | "copy"
    readonly_protect: bool = True
    settle_seconds: float = 3.0
    poll_seconds: float = 5.0
    csv_margin_seconds: float = 60.0
    csv_sync_wait_seconds: float = 15.0
    nice_increment: int = 10
    hadd_batch_size: int = 64

    def __post_init__(self) -> None:
        merged = dict(DEFAULT_BOARD_MAP)
        for key, value in (self.board_map or {}).items():
            merged[str(int(key)) if str(key).isdigit() else str(key)] = str(value).strip()
        self.board_map = merged
        if self.layout not in ("group", "flat"):
            raise ValueError(f"layout 必须是 'group' 或 'flat'，当前为: {self.layout}")
        if self.backend not in ("auto", "ipc", "pyroot"):
            raise ValueError(f"backend 必须是 'auto'、'ipc' 或 'pyroot'，当前为: {self.backend}")
        if self.link_mode not in ("hardlink", "copy"):
            raise ValueError(f"link_mode 必须是 'hardlink' 或 'copy'，当前为: {self.link_mode}")

    def resolve_raw_dir(self) -> Path:
        return Path(self.raw_data_dir).expanduser().resolve()

    def resolve_output_root(self) -> Path:
        return Path(self.output_root).expanduser().resolve()

    def resolve_work_dir(self) -> Path:
        if self.work_dir:
            return Path(self.work_dir).expanduser().resolve()
        return (self.resolve_output_root() / ".organizer_work").resolve()

    def resolve_csv_dir(self) -> Path:
        return Path(self.csv_mirror_dir).expanduser().resolve()

    def resolve_decoder_bin(self) -> Path:
        candidate = Path(self.decoder_bin).expanduser()
        if candidate.is_absolute():
            return candidate
        return (self.resolve_raw_dir() / candidate).resolve()

    def resolve_decoder_config(self) -> Path:
        candidate = Path(self.decoder_config).expanduser()
        if candidate.is_absolute():
            return candidate
        resolved = (self.resolve_raw_dir() / candidate).resolve()
        if self.decoder_config == "../cfg/config.json" and not resolved.exists():
            corrected = (self.resolve_raw_dir() / "../../cfg/config.json").resolve()
            if corrected.is_file():
                warnings.warn(
                    f"旧版 decoder_config={self.decoder_config} 不存在；使用 {corrected}。"
                    "建议将 decoder_config 更新为 ../../cfg/config.json。",
                    RuntimeWarning,
                )
                return corrected
        return resolved

    def string_for_board(self, board_id: int) -> str:
        key = str(int(board_id))
        if key in self.board_map and self.board_map[key]:
            return self.board_map[key]
        if 0 <= int(board_id) <= 6:
            return f"G{int(board_id)}"
        return f"FEE{int(board_id)}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def load_organizer_config(
    path: Optional[Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> OrganizerConfig:
    values: Dict[str, Any] = {}
    if path is not None:
        config_path = Path(path).expanduser()
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "organizer" in raw and isinstance(raw["organizer"], dict):
            values.update(raw["organizer"])
        elif isinstance(raw, dict):
            valid_keys = set(OrganizerConfig.__dataclass_fields__.keys())
            values.update({k: v for k, v in raw.items() if k in valid_keys})
    if overrides:
        for key, value in overrides.items():
            if value is not None:
                if key == "board_map" and isinstance(values.get("board_map"), dict) and isinstance(value, dict):
                    merged = dict(values["board_map"])
                    merged.update(value)
                    values["board_map"] = merged
                else:
                    values[key] = value
    return OrganizerConfig(**values)
