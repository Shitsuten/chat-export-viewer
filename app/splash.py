import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime


ROOT = Path(os.environ.get("AGENT_APP_ROOT", Path(__file__).resolve().parent.parent))
POOL_PATH = ROOT / "splash_lines.json"
FALLBACK = "What's on your mind?"


def current_period() -> str:
    hour = datetime.now(ZoneInfo("Asia/Shanghai")).hour
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "latenight"


def read_pool() -> dict[str, list[str]]:
    try:
        data = json.loads(POOL_PATH.read_text(encoding="utf-8"))
        return {
            key: [str(line) for line in data.get(key, []) if str(line).strip()]
            for key in ("morning", "afternoon", "evening", "latenight")
        }
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {key: [] for key in ("morning", "afternoon", "evening", "latenight")}


def random_line(period: str) -> str:
    import secrets

    lines = read_pool().get(period, [])
    return secrets.choice(lines) if lines else FALLBACK
