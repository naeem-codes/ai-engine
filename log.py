from datetime import datetime
from pathlib import Path

_LOG = Path(__file__).parent / "ai-engine.log"
_SEP = "─" * 80


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def section(title: str) -> None:
    log(_SEP)
    log(f"  {title}")
    log(_SEP)


def get_log_path() -> str:
    return str(_LOG)
