from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    PENDING_APPROVAL = "pending_approval"


@dataclass
class ExperimentDefinition:
    experiment_id: str
    name: str
    description_manual: str
    hypothesis: str
    scope: Dict[str, Any]
    timeline: Dict[str, int]
    load_profile: Dict[str, Any]
    chaos_profile: Dict[str, Any]
    observability: Dict[str, Any]
    governance: Dict[str, Any]
    analysis: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)


@dataclass
class ExperimentVersion:
    experiment_id: str
    version: int
    schema_version: str
    created_at: str
    created_by: str
    spec: Dict[str, Any]


@dataclass
class RunRecord:
    run_id: str
    experiment_id: str
    experiment_version: int
    status: RunStatus
    initiated_by: str
    started_at: str
    ended_at: Optional[str] = None
    verdict: Optional[str] = None
    timings: Dict[str, Any] = field(default_factory=dict)
    summary: Dict[str, Any] = field(default_factory=dict)
    manual_conclusion: Optional[str] = None


@dataclass
class DomainEvent:
    run_id: str
    event_type: str
    ts: str
    payload: Dict[str, Any]
