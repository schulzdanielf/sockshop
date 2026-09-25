#!/usr/bin/env python3
"""Teste pequeno de validação: todas as métricas necessárias estão disponíveis?

Valida, ANTES de rodar um experimento longo, que o pipeline consegue coletar
tudo o que o graph_builder (Opção D + Istio) precisa para um run:

* **Node features** (golden metrics por serviço): ``traffic``, ``latency_p95``,
  ``error_rate`` por serviço — vindas do metrics_raw (já existentes).
* **Edge features** (golden metrics por aresta): ``istio_requests_total``,
  ``istio_request_duration_milliseconds_{sum,count}`` por aresta
  source→destination — vindas do Istio (novo).

Saída: relatório no stdout + ``out/validation_check.json`` (schema versionado,
consumível por agente). Se algo faltar, o teste FALHA com a razão — não rodamos
o experimento longo com dado incompleto.

Uso
---
    # 1) gere tráfego pela topologia (o script diz como), depois:
    python -m experiment.eval.gat.validate_metrics --window-minutes 3
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiment.platform.backend.analysis.graph_builder import STATIC_TOPOLOGY  # noqa: E402

SCHEMA_VERSION = "1.0"
PROM_URL = "http://127.0.0.1:9091"  # Prometheus-v3 (Istio + traces)
NAMESPACE = "sock-shop"

# Golden metrics de nó (Istio por destination) e de aresta (Istio source→dest).
# Usamos as métricas Istio reais — as que o mesh produz — não os nomes de query
# do nosso pipeline (traffic/error_rate/latency_p95 são transformações, não
# métricas brutas do Prometheus).
NODE_SERVICES = ("front-end", "catalogue", "carts", "orders", "payment", "shipping", "user", "queue-master")
ISTIO_EDGE_METRICS = (
    "istio_requests_total",
    "istio_request_duration_milliseconds_sum",
    "istio_request_duration_milliseconds_count",
)
# Arestas core (sem os *-db, que são backing services fora do mesh de app).
CORE_EDGES = [(s, d) for s, d in STATIC_TOPOLOGY if not d.endswith("-db")]


def _prom_query(query: str, time_iso: str) -> List[Dict[str, Any]]:
    url = f"{PROM_URL}/api/v1/query?" + urllib.parse.urlencode(
        {"query": query, "time": time_iso}
    )
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    return (data.get("data") or {}).get("result") or []


def _prom_range(query: str, start: datetime, end: datetime, step: int = 30) -> List[Dict[str, Any]]:
    """Range query — mais robusto que instant para métricas recém-coletadas."""
    url = f"{PROM_URL}/api/v1/query_range?" + urllib.parse.urlencode({
        "query": query,
        "start": start.timestamp(),
        "end": end.timestamp(),
        "step": step,
    })
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    return (data.get("data") or {}).get("result") or []


def _check_node_metrics(window_start: datetime) -> Dict[str, Any]:
    """Verifica se as golden metrics por serviço existem na janela.

    Golden metrics por nó derivam de métricas Istio por *destination*:
    request rate (istio_requests_total), latência (duration_sum/count) e
    erro% (5xx/total) — todas por ``destination_workload``.
    """
    ts = window_start.isoformat().replace("+00:00", "Z")
    covered = set()
    res = _prom_query(
        f'istio_requests_total{{destination_workload_namespace="{NAMESPACE}"}}', ts
    )
    for r in res:
        m = r.get("metric") or {}
        dst = m.get("destination_workload")
        if dst:
            covered.add(dst)
    missing = [s for s in NODE_SERVICES if s not in covered]
    return {
        "ok": len(missing) == 0,
        "covered_services": sorted(covered),
        "missing_services": missing,
    }


def _check_edge_metrics(window_start: datetime, window_end: datetime) -> Dict[str, Any]:
    """Verifica cobertura das arestas da topologia pelas métricas Istio.

    Usa range query na janela (mais robusto que instant: o Prometheus agrega
    a cada ~15s, e um instant pode cair entre scrapes).
    """
    observed = set()
    for res_metric in ("istio_requests_total",):
        res = _prom_range(
            f'istio_requests_total{{destination_workload_namespace="{NAMESPACE}"}}',
            window_start, window_end,
        )
        for r in res:
            m = r.get("metric") or {}
            src, dst = m.get("source_workload"), m.get("destination_workload")
            if src and dst and src != "unknown" and dst != "unknown":
                observed.add((src, dst))

    covered = [f"{s}->{d}" for s, d in CORE_EDGES if (s, d) in observed]
    missing = [f"{s}->{d}" for s, d in CORE_EDGES if (s, d) not in observed]

    # as métricas Istio de latência existem?
    metrics_present = {}
    for m in ISTIO_EDGE_METRICS:
        res = _prom_range(
            f'{m}{{destination_workload_namespace="{NAMESPACE}"}}',
            window_start, window_end,
        )
        metrics_present[m] = len(res)

    return {
        "ok": len(missing) == 0 and all(v > 0 for v in metrics_present.values()),
        "covered_edges": covered,
        "missing_edges": missing,
        "istio_metric_series": metrics_present,
        "all_observed_edges": sorted(f"{s}->{d}" for s, d in observed),
    }


def run_validation(window_minutes: int) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=window_minutes)

    node = _check_node_metrics(window_start)
    edge = _check_edge_metrics(window_start, now)

    ok = node["ok"] and edge["ok"]
    return {
        "schema_version": SCHEMA_VERSION,
        "checked_at": now.isoformat(),
        "window_minutes": window_minutes,
        "ok": ok,
        "node_metrics": node,
        "edge_metrics": edge,
        "verdict": "READY" if ok else "NOT READY — rode tráfego pela topologia e revalide",
    }


def render(result: Dict[str, Any]) -> str:
    lines = ["=" * 78, "VALIDAÇÃO DE MÉTRICAS (teste pequeno)", "=" * 78]
    lines.append(f"Janela: últimos {result['window_minutes']} min | Veredito: {result['verdict']}")
    lines.append("")
    lines.append("Node metrics (golden por serviço, Istio por destination):")
    nm = result["node_metrics"]
    for s in nm["covered_services"]:
        lines.append(f"  ✓ {s}")
    for s in nm.get("missing_services", []):
        lines.append(f"  ✗ FALTA: {s}")
    lines.append("")
    em = result["edge_metrics"]
    lines.append(f"Edge metrics (Istio) — arestas cobertas: {len(em['covered_edges'])}/{len(CORE_EDGES)}")
    for e in em["covered_edges"]:
        lines.append(f"  ✓ {e}")
    for e in em["missing_edges"]:
        lines.append(f"  ✗ FALTA: {e}")
    lines.append("")
    lines.append("Séries Istio por métrica:")
    for m, n in em["istio_metric_series"].items():
        lines.append(f"  {m}: {n} séries")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Valida disponibilidade das métricas do GAT")
    parser.add_argument("--window-minutes", type=int, default=3)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    result = run_validation(args.window_minutes)
    print(render(result))

    if args.save:
        out = HERE / "out" / "validation_check.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nSalvo em {out}")

    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
