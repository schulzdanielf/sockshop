from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_FAULT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,79}$")


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

    @field_validator("possible_fault_types")
    @classmethod
    def valid_fault_types(cls, value: list[str]) -> list[str]:
        return validate_possible_fault_types(value)
