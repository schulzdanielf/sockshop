"""Gate runner — executa o catálogo de gates e produz o run_quality.json.

O resultado é o artefato que responde "este run é utilizável? por quê?"
— tanto para o engine online quanto para a auditoria offline de runs
históricos, e tanto para humanos quanto para o futuro agente de LLM.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .gates import GATES, RunBundle

SCHEMA_VERSION = "1.0"


def run_gates(bundle: RunBundle) -> Dict[str, Any]:
    """Executa todos os gates sobre o bundle e consolida o resultado.

    Retorna o conteúdo do ``run_quality.json``: um documento
    schema-versionado, autocontido, com cada decisão como dado
    estruturado (gate, status, razão, evidência).
    """
    results = [gate(bundle) for gate in GATES]

    blocking_failures = [
        r for r in results if r.status == "fail" and r.blocking
    ]
    gaps: List[str] = sorted({r.reason for r in results if r.status == "fail"})

    weak_signal = any(
        r.gate == "symptom_presence" and r.reason == "weak_signal" for r in results
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": bundle.run_id,
        "experiment_id": bundle.experiment_id,
        "status": bundle.status,
        "usable": not blocking_failures,
        "weak_signal": weak_signal,
        "gaps": gaps,
        "gates": [r.to_dict() for r in results],
    }
