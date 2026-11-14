from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_FAULT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,79}$")
MAX_ADDITIONAL_MCP_CHECKS = 4


def validate_possible_fault_types(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("possible_fault_types must be a list of at most 20 labels")
    if any(
        not isinstance(item, str) or not _FAULT_TYPE_PATTERN.fullmatch(item)
        for item in value
    ):
        raise ValueError("possible_fault_types contains an invalid label")
    if len(value) != len(set(value)):
        raise ValueError("possible_fault_types must not contain duplicates")
    return value


class RCABenchmarkOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    possible_fault_types: list[str] = Field(default_factory=list, max_length=20)
    require_best_guess: bool = False
    model: str | None = Field(default=None, min_length=1, max_length=128)
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    seed: int | None = Field(default=None, ge=0, le=4_294_967_295)
    resource_signature_policy: Literal["none", "cpu-memory-v1"] = "none"
    max_additional_checks: int = Field(
        default=MAX_ADDITIONAL_MCP_CHECKS,
        ge=0,
        le=MAX_ADDITIONAL_MCP_CHECKS,
    )

    @field_validator("possible_fault_types")
    @classmethod
    def valid_fault_types(cls, value: list[str]) -> list[str]:
        return validate_possible_fault_types(value)


class SkillRCABenchmarkOptions(RCABenchmarkOptions):
    max_model_calls: int = Field(default=30, ge=6, le=30)
    max_total_tokens: int = Field(default=200_000, ge=8_000, le=500_000)
    timeout_seconds: int = Field(default=600, ge=30, le=1800)
