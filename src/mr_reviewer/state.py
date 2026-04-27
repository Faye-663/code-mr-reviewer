from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"lastMessageId": None, "processed": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def is_processed(self, message_id: str) -> bool:
        return message_id in self.data.get("processed", {})

    def mark_processed(self, message_id: str, task_id: str, status: str, error: str | None = None) -> None:
        processed = self.data.setdefault("processed", {})
        entry = {
            "task_id": task_id,
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if error:
            entry["error"] = error
        processed[message_id] = entry
        self.data["lastMessageId"] = message_id
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temp_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.path)
