from pathlib import Path
from typing import TypeVar

from arc_v2_util.constants import Constant
from dotenv import load_dotenv

T = TypeVar("T")


ENV_PATH = Constant(default="~/arc/.env", type=str).to_path()


def load_dot_env() -> bool:
    """Load ARC's .env file.

    Returns:
        True if the .env file was loaded.
        False if the file does not exist.
    """
    if not ENV_PATH.is_file():
        return False

    return load_dotenv(
        ENV_PATH,
        override=False,
    )


# IMPORTANT: load before evaluating configuration constants
ENV_LOADED = load_dot_env()


def set_env(key: str, value: T):  # pyright: ignore[reportInvalidTypeVarUse]
    _path = ENV_PATH
    _path.parent.mkdir(parents=True, exist_ok=True)

    if _path.is_file():
        lines = _path.read_text("utf-8").splitlines()
    else:
        lines = []

    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")

    _ = _path.write_text("\n".join(lines) + "\n", encoding="utf-8")


_DEV = {
    "TERMINAL_NO_COLOR": "false",
    "PYTHONUNBUFFERED": "true",
    "STRICT_ERRORS": "true",
    # ---
    "WAIT_ON_DEPENDENCIES": "true",
    "MAX_UNHEALTHY_SERVICE_CHECKS": "3",
    # ---
    "ARC_DIR": "~/arc",
    "SERVICE_RUNTIME_DIR": "~/arc/runtime",
    "SERVICE_LOCK": "~/arc/arc.lock",
    "SERVICE_CONFIG": "~/arc/services.arc.yaml",
    # ---
    "LOG_LEVEL": "INFO",
    "LOG_FILE": "~/arc/arc.log",
    "LOG_CONSOLE": "true",
    "LOG_JSON": "false",
    "LOG_ROTATE": "true",
    "LOG_MAX_BYTES": "10485760",
    "LOG_BACKUP_COUNT": "2",
    # ---
}

# System
TERMINAL_NO_COLOR = Constant(
    default=_DEV["TERMINAL_NO_COLOR"], type=bool, auto_conversion=True
)
STRICT_ERRORS = Constant(default=_DEV["STRICT_ERRORS"], type=bool, auto_conversion=True)
WAIT_ON_DEPENDENCIES = Constant(
    default=_DEV["WAIT_ON_DEPENDENCIES"], type=bool, auto_conversion=True
)
MAX_UNHEALTHY_SERVICE_CHECKS = Constant(
    default=_DEV["MAX_UNHEALTHY_SERVICE_CHECKS"], type=int, auto_conversion=True
)

# Project Directories and file locations
ARC_DIR = Constant(default=_DEV["ARC_DIR"]).to_path()
SERVICE_LOCK = Constant(default=_DEV["SERVICE_LOCK"]).to_path()
SERVICE_CONFIG = Constant(default=_DEV["SERVICE_CONFIG"]).to_path()
SERVICE_RUNTIME_DIR = Constant(default=_DEV["SERVICE_RUNTIME_DIR"]).to_path()

# Logging
LOG_LEVEL = Constant(default=_DEV["LOG_LEVEL"])
LOG_FILE = Constant(default=_DEV["LOG_FILE"]).to_path()
LOG_CONSOLE = Constant(default=_DEV["LOG_CONSOLE"], type=bool, auto_conversion=True)
LOG_JSON = Constant(default=_DEV["LOG_JSON"], type=bool, auto_conversion=True)
LOG_ROTATE = Constant(default=_DEV["LOG_ROTATE"], type=bool, auto_conversion=True)
LOG_MAX_BYTES = Constant(default=_DEV["LOG_MAX_BYTES"], type=int, auto_conversion=True)
LOG_BACKUP_COUNT = Constant(
    default=_DEV["LOG_BACKUP_COUNT"], type=int, auto_conversion=True
)

CONTROL_SOCKET = ARC_DIR / "arc.sock"


def make_default_env() -> Path | None:
    path = ENV_PATH

    if path.exists():
        return None

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    content = "\n".join(f"{key}={value}" for key, value in _DEV.items()) + "\n"

    _ = path.write_text(
        content,
        encoding="utf-8",
    )

    return path
