"""System card loader & renderer.

A system card is a static YAML document describing the target application
(topology, role of each service, controlled vocabulary). It is injected
into the LLM system prompt as a persistent moldura so the model has the
same baseline context on every call without spending RAG tokens on it.

Cards live under ``experiment/platform/data/system_cards/<system_id>.yaml``.
Loading is cached per process; the YAML is rendered to a compact text
block once per system card version.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

_CARDS_DIR = Path(__file__).resolve().parents[2] / "data" / "system_cards"


class SystemCardError(Exception):
    """Raised when a system card cannot be loaded or is malformed."""


def _cards_dir() -> Path:
    return _CARDS_DIR


@lru_cache(maxsize=16)
def load_system_card(system_id: str) -> Dict[str, Any]:
    """Read and parse a system card YAML. Returns an empty dict if the
    file does not exist — callers may then skip injection silently."""
    path = _cards_dir() / f"{system_id}.yaml"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - configuration error
        raise SystemCardError(f"invalid system card {system_id}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemCardError(f"system card {system_id} must be a mapping")
    return data


def _format_service(name: str, svc: Dict[str, Any]) -> str:
    role = (svc.get("role") or "").strip()
    role = " ".join(role.split())  # collapse multiline YAML folding
    stack = svc.get("stack") or "n/a"
    svc_type = svc.get("type") or "n/a"
    upstream = ", ".join(svc.get("upstream") or []) or "—"
    downstream = ", ".join(svc.get("downstream") or []) or "—"
    storage = ", ".join(svc.get("storage") or []) or "—"
    lines = [
        f"- {name} [{stack}, {svc_type}]",
        f"    role: {role}" if role else "    role: n/a",
        f"    upstream: {upstream}",
        f"    downstream: {downstream}",
    ]
    if svc.get("storage"):
        lines.append(f"    storage: {storage}")
    notes = svc.get("notes") or []
    for note in notes:
        note_str = " ".join(str(note).split())
        lines.append(f"    note: {note_str}")
    return "\n".join(lines)


def render_system_card(system_id: str) -> str:
    """Render a system card to a compact text block suitable for the
    LLM system prompt. Returns an empty string if the card is missing."""
    card = load_system_card(system_id)
    if not card:
        return ""

    parts: List[str] = []
    desc = (card.get("description") or "").strip()
    desc = " ".join(desc.split())
    header = f"## System under test: {card.get('system_id', system_id)}"
    version = card.get("version")
    if version:
        header += f" (card v{version})"
    parts.append(header)
    if desc:
        parts.append(desc)

    platform = card.get("platform") or {}
    if platform:
        plat_items = ", ".join(f"{k}={v}" for k, v in platform.items() if v)
        if plat_items:
            parts.append(f"Platform: {plat_items}")

    services = card.get("services") or {}
    if services:
        parts.append("\n### Services (role, stack, dependencies)")
        for name, svc in services.items():
            if isinstance(svc, dict):
                parts.append(_format_service(name, svc))

    rca_vocab = card.get("allowed_rca_targets") or []
    if rca_vocab:
        parts.append(
            "\n### Allowed values for the verdict `rca` field\n"
            "Reply MUST use exactly one of: " + ", ".join(rca_vocab) + "."
        )

    fault_vocab = card.get("allowed_fault_categories") or []
    if fault_vocab:
        parts.append(
            "### Allowed values for the verdict `fault_category` field\n"
            "Reply MUST use exactly one of: " + ", ".join(fault_vocab) + "."
        )

    return "\n".join(parts)


def list_system_cards() -> List[str]:
    """List system_ids for which a card YAML exists on disk."""
    if not _cards_dir().exists():
        return []
    return sorted(p.stem for p in _cards_dir().glob("*.yaml"))


def system_card_meta(system_id: str) -> Optional[Dict[str, Any]]:
    """Return a small metadata blob (id, version, service_count) or None."""
    card = load_system_card(system_id)
    if not card:
        return None
    services = card.get("services") or {}
    return {
        "system_id": card.get("system_id", system_id),
        "version": card.get("version"),
        "service_count": len(services),
        "allowed_rca_targets": card.get("allowed_rca_targets") or [],
    }
