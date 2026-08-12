"""轻量日志工具——只提供 write_log，不依赖外部服务。"""

import json
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

LOG_FILE = pathlib.Path(__file__).parent / "logs.jsonl"
TZ = ZoneInfo("Asia/Shanghai")


def write_log(
    level: str,
    category: str,
    message: str,
    detail: dict = None,
    *,
    log_file: pathlib.Path = None,
) -> dict:
    entry = {
        "timestamp": datetime.now(TZ).isoformat(),
        "level": level,
        "category": category,
        "message": message,
    }
    if detail:
        entry["detail"] = detail
    with open(log_file or LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read_logs(filter_category: str = "all", limit: int = 50) -> list:
    if not LOG_FILE.exists():
        return []
    logs = []
    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if filter_category != "all" and entry.get("category") != filter_category:
                continue
            logs.append(entry)
    return logs[-limit:]
