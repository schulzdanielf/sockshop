"""Run health report — o documento único "o que aconteceu neste run".

Consolida, por run: timeline de eventos, artefatos esperados vs
presentes, timings das fases e o resultado dos gates (run_quality).
É gerado *a partir* dos dados estruturados (nunca o contrário), para
que o mesmo JSON alimente humanos (relatório) e o futuro agente de LLM
(auditoria programática).
"""
from __future__ import annotations

from typing import Any, Dict, List

from .gates import RunBundle
from .runner import run_gates

SCHEMA_VERSION = "1.0"

# Artefatos que o engine deve produzir por run (ver engine._execute_run).
EXPECTED_ARTIFACTS = (
    "metrics_raw",
    "traces_raw",
    "load_summary",
    "chaos_artifacts",
    "run_features",
    "run_summary_l2",
)


def _artifact_checklist(bundle: RunBundle) -> Dict[str, Any]:
    present = [a for a in EXPECTED_ARTIFACTS if a in bundle.artifact_names]
    missing = [a for a in EXPECTED_ARTIFACTS if a not in bundle.artifact_names]
    extra = [a for a in bundle.artifact_names if a not in EXPECTED_ARTIFACTS]
    return {
        "expected": list(EXPECTED_ARTIFACTS),
        "present": present,
        "missing": missing,
        "extra": sorted(extra),
    }


def _timeline(bundle: RunBundle) -> List[Dict[str, Any]]:
    """Timeline de eventos ordenada, no formato em que foram gravados."""
    out = []
    for e in bundle.events:
        out.append(
            {
                "ts": e.get("ts"),
                "event_type": e.get("event_type"),
                "payload": e.get("payload") or {},
            }
        )
    out.sort(key=lambda x: x.get("ts") or "")
    return out


def build_run_health(bundle: RunBundle) -> Dict[str, Any]:
    """Produz o conteúdo do ``run_health.json`` para um run.

    Executa o gate runner internamente e embute o run_quality — o
    relatório fica autocontido (um único arquivo conta a história
    completa do run).
    """
    quality = run_gates(bundle)

    features = bundle.features or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": bundle.run_id,
        "experiment_id": bundle.experiment_id,
        "status": bundle.status,
        "verdict": features.get("verdict"),
        "chaos_type": features.get("chaos_type"),
        "ground_truth": {
            "service": features.get("ground_truth_service"),
            "fault_category": features.get("ground_truth_fault_category"),
        },
        "timings": bundle.timings,
        "timeline": _timeline(bundle),
        "artifacts": _artifact_checklist(bundle),
        "quality": quality,
    }
