from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def log_audit(event_type: str, payload: dict[str, Any], actor: str, path: str | Path, timestamp: str | None = None) -> dict[str, Any]:
    event = {"event_type": event_type, "payload": payload, "actor": actor, "timestamp": timestamp or datetime.now(timezone.utc).isoformat()}
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    return event
