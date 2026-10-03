"""Deterministic, per-container resource-hog intensity calculations."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Dict, Mapping


_QUANTITY_RE = re.compile(
    r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)$"
)
_CPU_SUFFIXES = {
    "": Decimal(1),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal(1000),
    "K": Decimal(1000),
    "M": Decimal(1000**2),
    "G": Decimal(1000**3),
    "T": Decimal(1000**4),
    "P": Decimal(1000**5),
    "E": Decimal(1000**6),
}
_MEMORY_SUFFIXES = {
    "": Decimal(1),
    "k": Decimal(1000),
    "K": Decimal(1000),
    "M": Decimal(1000**2),
    "G": Decimal(1000**3),
    "T": Decimal(1000**4),
    "P": Decimal(1000**5),
    "E": Decimal(1000**6),
    "Ki": Decimal(1024),
    "Mi": Decimal(1024**2),
    "Gi": Decimal(1024**3),
    "Ti": Decimal(1024**4),
    "Pi": Decimal(1024**5),
    "Ei": Decimal(1024**6),
}


def _parse_quantity(value: Any, suffixes: Mapping[str, Decimal], resource: str) -> Decimal:
    match = _QUANTITY_RE.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f"invalid Kubernetes {resource} quantity: {value!r}")
    number, suffix = match.groups()
    multiplier = suffixes.get(suffix)
    if multiplier is None:
        raise ValueError(f"unsupported Kubernetes {resource} suffix: {suffix!r}")
    try:
        quantity = Decimal(number) * multiplier
    except InvalidOperation as exc:
        raise ValueError(f"invalid Kubernetes {resource} quantity: {value!r}") from exc
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError(f"Kubernetes {resource} limit must be positive: {value!r}")
    return quantity


def resource_chaos_environment(
    chaos_type: str,
    limits: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> Dict[str, str]:
    """Build Litmus env overrides from one target container's resource limits."""
    duration = int(settings["duration_seconds"])
    if duration <= 0:
        raise ValueError("resource chaos duration_seconds must be positive")

    if chaos_type == "cpu-hog":
        _parse_quantity(limits.get("cpu"), _CPU_SUFFIXES, "CPU")
        load_pct = int(settings["cpu_load_percentage"])
        if not 1 <= load_pct <= 100:
            raise ValueError("cpu_load_percentage must be between 1 and 100")
        return {
            "CPU_CORES": "0",
            "CPU_LOAD": str(load_pct),
            "TOTAL_CHAOS_DURATION": str(duration),
        }

    if chaos_type == "memory-hog":
        memory_bytes = _parse_quantity(
            limits.get("memory"), _MEMORY_SUFFIXES, "memory"
        )
        memory_pct = Decimal(str(settings["memory_load_percentage"]))
        if not Decimal(1) <= memory_pct <= Decimal(100):
            raise ValueError("memory_load_percentage must be between 1 and 100")
        memory_mb = int(
            (memory_bytes * memory_pct / Decimal(100) / Decimal(1_000_000)).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
        if memory_mb <= 0:
            raise ValueError("memory limit is too small for the requested stress")
        return {
            "MEMORY_CONSUMPTION": str(memory_mb),
            "TOTAL_CHAOS_DURATION": str(duration),
        }

    raise ValueError(f"unsupported resource chaos type: {chaos_type!r}")
