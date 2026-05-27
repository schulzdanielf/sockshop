from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class CreateExperimentRequest(BaseModel):
    initiated_by: str = Field(..., min_length=1)
    spec: Dict[str, Any]


class StartRunRequest(BaseModel):
    experiment_id: str
    version: Optional[int] = None
    initiated_by: str = Field(..., min_length=1)
    idempotency_key: Optional[str] = None
    approved_by: Optional[str] = None
    # Whether this run is part of the training corpus (eligible as RAG
    # neighbour) or a held-out test instance. Defaults to True for
    # backward compatibility with non-experiment callers.
    is_training: bool = True


class StopRunRequest(BaseModel):
    requested_by: str = Field(..., min_length=1)
    reason: str = "manual_stop"


class ApproveRunRequest(BaseModel):
    approved_by: str = Field(..., min_length=1)


class ManualConclusionRequest(BaseModel):
    conclusion: str = Field(..., min_length=1)


class OperatorLabelRequest(BaseModel):
    label: Literal[
        "resilient",
        "degraded_recoverable",
        "degraded_persistent",
        "unsure",
    ]
    by: Optional[str] = None
    note: Optional[str] = None
