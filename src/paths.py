"""Repository paths."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    return ROOT / "config"
