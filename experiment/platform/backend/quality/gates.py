"""Catálogo de gates de qualidade por run.

Cada gate é uma função pura ``(RunBundle) -> GateResult``. Sem I/O
direto: o bundle carrega tudo o que os gates precisam (artefatos são
carregados lazy via ``bundle.load_artifact``).

Estados de um gate:

* ``pass`` — validação ok.
* ``fail`` — validação falhou. Se ``blocking=True``, o run é marcado
  ``unusable`` no run_quality (não entra em dataset/treino).
* ``skip`` — gate não se aplica a este run (ex.: gate de grafo num run
  sem propagation_graph). Nunca bloqueia.

Convenções
----------
* Toda falha/skip carrega ``reason`` (string curta, estável — usável
  como chave de agregação) e ``evidence`` (dados que sustentam a
  decisão, no padrão de ``validator_meta``).
* Thresholds são constantes de módulo, conservadoras — preferimos
  marcar um run a mais do que deixar passar dado ruim silenciosamente.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# ── Thresholds ──────────────────────────────────────────────────────────
# Métricas essenciais do desenho do GAT (golden metrics por serviço).
ESSENTIAL_METRICS = ("traffic", "error_rate", "latency_p95")

# Mínimo de pontos por fase para a janela ser estatisticamente útil.
MIN_PHASE_POINTS = 4

# |delta_abs| mínimo num hotspot para considerar que o run apresenta
# algum sintoma mensurável (gate informativo symptom_presence).
SYMPTOM_DELTA_MIN = 0.10


@dataclass
class RunBundle:
    """Tudo que os gates precisam saber sobre um run.

    ``load_artifact(name)`` retorna o conteúdo JSON do artefato ou
    ``None`` se ausente. A callable é injetada pelo chamador (engine
    online ou CLI offline), mantendo os gates puros e testáveis.
    """

    run_id: str
    experiment_id: str
    status: str
    timings: Dict[str, Any] = field(default_factory=dict)
    features: Optional[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    artifact_names: List[str] = field(default_factory=list)
    load_artifact: Callable[[str], Optional[Dict[str, Any]]] = lambda name: None


@dataclass
class GateResult:
    gate: str
    status: str  # "pass" | "fail" | "skip"
    blocking: bool
    reason: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gate": self.gate,
            "status": self.status,
            "blocking": self.blocking,
            "reason": self.reason,
            "evidence": self.evidence,
        }


def _pass(gate: str, blocking: bool, evidence: Optional[Dict[str, Any]] = None) -> GateResult:
    return GateResult(gate=gate, status="pass", blocking=blocking, evidence=evidence or {})


def _fail(gate: str, blocking: bool, reason: str, evidence: Optional[Dict[str, Any]] = None) -> GateResult:
    return GateResult(gate=gate, status="fail", blocking=blocking, reason=reason, evidence=evidence or {})


def _skip(gate: str, blocking: bool, reason: str) -> GateResult:
    return GateResult(gate=gate, status="skip", blocking=blocking, reason=reason)


# ── Gates ───────────────────────────────────────────────────────────────
def gate_collection_completeness(bundle: RunBundle) -> GateResult:
    """Cada artefato esperado existe E tem conteúdo.

    Teria capturado o bug histórico do ``traces_raw`` (193 runs com
    ``count: 0`` e nenhum alerta). Um arquivo que existe mas está vazio
    é tão inútil quanto um ausente — por isso o check é de *conteúdo*,
    não de existência.
    """
    gate = "collection_completeness"
    missing: List[str] = []
    empty: List[Dict[str, Any]] = []

    expected = ("metrics_raw", "traces_raw", "load_summary", "chaos_artifacts")
    for name in expected:
        if name not in bundle.artifact_names:
            missing.append(name)

    metrics = bundle.load_artifact("metrics_raw")
    if metrics is not None:
        data = metrics.get("data") or {}
        metric_gaps = []
        for mid in ESSENTIAL_METRICS:
            series = ((data.get(mid) or {}).get("data") or {}).get("result") or []
            if not series:
                metric_gaps.append(mid)
        if metric_gaps:
            empty.append({"artifact": "metrics_raw", "missing_series": metric_gaps})
    elif "metrics_raw" not in missing:
        empty.append({"artifact": "metrics_raw", "detail": "unreadable"})

    traces = bundle.load_artifact("traces_raw")
    if traces is not None:
        count = traces.get("count")
        if not count or int(count) <= 0:
            empty.append({"artifact": "traces_raw", "count": count or 0})
    elif "traces_raw" not in missing:
        empty.append({"artifact": "traces_raw", "detail": "unreadable"})

    if missing or empty:
        return _fail(
            gate,
            blocking=True,
            reason="artifacts_missing_or_empty",
            evidence={"missing": missing, "empty": empty},
        )
    return _pass(gate, blocking=True, evidence={"checked": list(expected)})


def gate_window_sanity(bundle: RunBundle) -> GateResult:
    """Fases têm timestamps coerentes e pontos suficientes."""
    gate = "window_sanity"
    timings = bundle.timings or {}
    required = ("t_start", "t_fault_start", "t_fault_end")
    missing_ts = [k for k in required if not timings.get(k)]
    if missing_ts:
        return _fail(gate, True, "missing_phase_timestamps", {"missing": missing_ts})

    fault_duration = timings.get("fault_injection_duration_seconds")
    if fault_duration is not None and float(fault_duration) <= 0:
        return _fail(gate, True, "non_positive_fault_duration", {"fault_duration": fault_duration})

    features = bundle.features or {}
    phase_metrics = features.get("phase_metrics") or {}
    if not phase_metrics:
        return _skip(gate, True, "no_phase_metrics")

    thin: List[str] = []
    for mid in ESSENTIAL_METRICS:
        stats = phase_metrics.get(mid) or {}
        for phase in ("baseline", "fault"):
            count = ((stats.get(phase) or {}).get("count")) or 0
            if count < MIN_PHASE_POINTS:
                thin.append(f"{mid}@{phase}={count}")
    if thin:
        return _fail(gate, True, "insufficient_phase_points", {"thin": thin, "min": MIN_PHASE_POINTS})
    return _pass(gate, True, evidence={"min_phase_points": MIN_PHASE_POINTS})


def gate_label_consistency(bundle: RunBundle) -> GateResult:
    """Ground truth existe e referencia um serviço observado no run."""
    gate = "label_consistency"
    features = bundle.features or {}
    gt_service = features.get("ground_truth_service")
    gt_fault = features.get("ground_truth_fault_category")
    missing = []
    if not gt_service:
        missing.append("ground_truth_service")
    if not gt_fault:
        missing.append("ground_truth_fault_category")
    if missing:
        return _fail(gate, True, "missing_ground_truth", {"missing": missing})

    # O serviço-alvo deve aparecer em algum lugar observável: labels das
    # métricas, nós do grafo de propagação ou serviços afetados.
    observed = set(features.get("affected_services") or [])
    graph = features.get("propagation_graph") or {}
    observed.update(n for n in (graph.get("nodes") or []) if isinstance(n, str))
    metrics = bundle.load_artifact("metrics_raw") or {}
    for mid in ESSENTIAL_METRICS:
        series = (((metrics.get("data") or {}).get(mid) or {}).get("data") or {}).get("result") or []
        for s in series:
            name = ((s.get("metric") or {}).get("name"))
            if name:
                observed.add(name)
    if gt_service not in observed:
        return _fail(
            gate,
            True,
            "ground_truth_service_not_observed",
            {"ground_truth_service": gt_service, "observed": sorted(observed)},
        )
    return _pass(gate, True, evidence={"ground_truth_service": gt_service})


def gate_graph_connectivity(bundle: RunBundle) -> GateResult:
    """Grafo de propagação tem nós/arestas mínimos e contém o alvo."""
    gate = "graph_connectivity"
    features = bundle.features or {}
    graph = features.get("propagation_graph")
    if not graph:
        return _skip(gate, True, "no_propagation_graph")

    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    if len(nodes) < 2 or len(edges) < 1:
        return _fail(
            gate,
            True,
            "graph_too_sparse",
            {"node_count": len(nodes), "edge_count": len(edges)},
        )

    gt_service = features.get("ground_truth_service")
    if gt_service and gt_service not in nodes:
        return _fail(
            gate,
            True,
            "ground_truth_not_in_graph",
            {"ground_truth_service": gt_service, "nodes": nodes},
        )
    return _pass(gate, True, evidence={"node_count": len(nodes), "edge_count": len(edges)})


def gate_feature_sanity(bundle: RunBundle) -> GateResult:
    """Features numéricas finitas nos hotspots (sem NaN/Inf)."""
    gate = "feature_sanity"
    features = bundle.features or {}
    hotspots = features.get("metric_hotspots")
    if not hotspots:
        return _skip(gate, True, "no_metric_hotspots")

    bad: List[str] = []
    for metric_id, entry in hotspots.items():
        rows = (entry or {}).get("top") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("baseline_mean", "fault_mean", "delta_abs"):
                value = row.get(key)
                if value is None:
                    continue
                try:
                    v = float(value)
                except (TypeError, ValueError):
                    bad.append(f"{metric_id}:{key}")
                    continue
                if v != v or v in (float("inf"), float("-inf")):
                    bad.append(f"{metric_id}:{key}")
    if bad:
        return _fail(gate, True, "non_finite_features", {"bad": bad[:10]})
    return _pass(gate, True)


def gate_symptom_presence(bundle: RunBundle) -> GateResult:
    """Informativo: ao menos um hotspot com |delta| acima do limiar.

    NUNCA bloqueia — uma falha transiente pode legitimamente apresentar
    sinal fraco na janela. Serve para marcar ``weak_signal`` e permitir
    análise posterior de cobertura por categoria.
    """
    gate = "symptom_presence"
    features = bundle.features or {}
    hotspots = features.get("metric_hotspots")
    if not hotspots:
        return _skip(gate, False, "no_metric_hotspots")

    strongest = 0.0
    strongest_metric = None
    for metric_id, entry in hotspots.items():
        for row in (entry or {}).get("top") or []:
            if not isinstance(row, dict):
                continue
            try:
                v = abs(float(row.get("delta_abs") or 0.0))
            except (TypeError, ValueError):
                continue
            if v > strongest:
                strongest = v
                strongest_metric = metric_id
    if strongest < SYMPTOM_DELTA_MIN:
        return _fail(
            gate,
            False,
            "weak_signal",
            {"strongest_delta": strongest, "threshold": SYMPTOM_DELTA_MIN},
        )
    return _pass(
        gate,
        False,
        evidence={"strongest_delta": strongest, "strongest_metric": strongest_metric},
    )


def gate_edge_features_present(bundle: RunBundle) -> GateResult:
    """Runs com mesh (Istio) devem ter edge features reais (não-neutras).

    Lê o artefato ``gat_graph.json`` e valida que ao menos uma aresta carrega
    feature não-nula. Sem grafo (runs históricos sem mesh), ``skip``.
    """
    gate = "edge_features_present"
    graph = bundle.load_artifact("gat_graph")
    if not graph:
        return _skip(gate, True, "no_gat_graph")

    edges = graph.get("edges") or []
    if not edges:
        return _fail(gate, True, "no_edges_in_graph", {})

    with_features = sum(
        1 for e in edges
        if isinstance(e, dict) and any(
            (e.get("features") or {}).get(k) is not None
            for k in ("req_rate_lr", "latency_lr", "error_lr")
        )
    )
    if with_features == 0:
        return _fail(
            gate,
            True,
            "all_edges_neutral",
            {"edge_count": len(edges), "with_features": 0},
        )
    return _pass(gate, True, evidence={"edge_count": len(edges), "with_features": with_features})


# ── Catálogo (ordem de execução) ────────────────────────────────────────
GATES: List[Callable[[RunBundle], GateResult]] = [
    gate_collection_completeness,
    gate_window_sanity,
    gate_label_consistency,
    gate_graph_connectivity,
    gate_feature_sanity,
    gate_edge_features_present,
    gate_symptom_presence,
]
