from __future__ import annotations

from typing import Any, Dict


class NoopNotificationAdapter:
    def notify(self, event_type: str, payload: Dict[str, Any]) -> None:
        _ = (event_type, payload)
