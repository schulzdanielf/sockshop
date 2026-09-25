#!/usr/bin/env python3
"""Verificador de falha sistêmica de infra durante uma janela de coleta.

Responde: "enquanto os experimentos rodavam, algum componente ESSENCIAL da
infra (que não é alvo do chaos) falhou?" — para garantir que as métricas
coletadas não foram contaminadas por instabilidade de fundo.

Checa, numa janela [start, end] (padrão: últimas N horas), via Prometheus:
  * **Restarts de pods essenciais** (otel-collector, prometheus, tempo, loki,
    rabbitmq, *-db) — sinal de infra caindo.
  * **otel-collector não-ready** — o guardião: se cai, Zipkin(9411) cai e
    derruba user/payment/catalogue (a falha observada em 2026-09-21).
  * **Deployments essenciais indisponíveis** (prometheus-v3, tempo, loki).
  * **Storm de 5xx no front-end** sem chaos (propagação de dependência).

Exit 0 = janela limpa; 1 = falha sistêmica detectada (revisar runs da janela).

Uso
---
    python -m experiment.eval.gat.check_infra                # últimas 6h
    python -m experiment.eval.gat.check_infra --hours 12
    python -m experiment.eval.gat.check_infra --start 2026-09-21T10:00:00Z --end 2026-09-21T18:00:00Z
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

PROM = os.environ.get("PROM_URL", "http://127.0.0.1:9091")

ESSENTIAL_PODS = r"otel-collector.*|prometheus.*|tempo.*|loki.*|rabbitmq.*|.*-db.*"


def _query_range(prom: str, start: datetime, end: datetime, step: int = 60) -> List[Dict[str, Any]]:
    url = (
        f"{PROM}/api/v1/query_range?query={urllib.parse.quote(prom)}"
        f"&start={start.timestamp()}&end={end.timestamp()}&step={step}"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.load(r).get("data", {}).get("result", [])
    except Exception as exc:  # noqa: BLE001
        return [{"_error": str(exc)}]


def _max_delta(result: List[Dict[str, Any]]) -> Dict[str, float]:
    """max-min por série (contador): quantas vezes reiniciou na janela."""
    out: Dict[str, float] = {}
    for s in result:
        if "_error" in s:
            continue
        pod = s.get("metric", {}).get("pod", "?")
        vals = [float(v[1]) for v in s.get("values", [])]
        if vals:
            out[pod] = max(out.get(pod, 0.0), max(vals) - min(vals))
    return out


def check(start: datetime, end: datetime) -> Dict[str, Any]:
    findings: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: Any):
        findings.append({"check": name, "ok": bool(ok), "detail": detail})

    # 1) restarts de pods essenciais na janela
    res = _query_range(
        f'kube_pod_container_status_restarts_total{{pod=~"{ESSENTIAL_PODS}"}}',
        start, end,
    )
    if res and "_error" in res[0]:
        add("restarts_essenciais", False, f"prometheus erro: {res[0]['_error']}")
    else:
        deltas = {p: d for p, d in _max_delta(res).items() if d > 1}
        add("restarts_essenciais", not deltas,
            deltas or "nenhum restart >1 em pods essenciais")

    # 2) otel-collector ready (min na janela)
    res = _query_range(
        'sum(kube_pod_container_status_ready{namespace="monitoring",pod=~"otel-collector.*"})',
        start, end,
    )
    if res and "_error" in res[0]:
        add("otel_collector_ready", False, f"prometheus erro: {res[0]['_error']}")
    else:
        vals = [float(v[1]) for s in res for v in s.get("values", [])]
        min_ready = min(vals) if vals else 0.0
        add("otel_collector_ready", min_ready >= 1.0,
            f"min_ready={min_ready:.0f} (esperado >=1)")

    # 3) deployments essenciais disponíveis (prometheus-v3, tempo, loki)
    # Avalia CADA um individualmente: min por deployment deve ser >= 1 na janela.
    res = _query_range(
        'kube_deployment_status_replicas_available{namespace="monitoring",'
        'deployment=~"prometheus-v3|tempo|loki"}', start, end,
    )
    if res and "_error" in res[0]:
        add("observabilidade_disponivel", False, f"prometheus erro: {res[0]['_error']}")
    else:
        per_dep: Dict[str, float] = {}
        for s in res:
            dep = s.get("metric", {}).get("deployment", "?")
            vals = [float(v[1]) for v in s.get("values", [])]
            if vals:
                per_dep[dep] = min(per_dep.get(dep, float("inf")), min(vals))
        # cobertura: os 3 essenciais devem aparecer e estar >=1
        expected = {"prometheus-v3", "tempo", "loki"}
        missing = expected - set(per_dep)
        down = {d: v for d, v in per_dep.items() if v < 1.0}
        ok3 = not missing and not down
        detail = {d: per_dep.get(d, "ausente") for d in sorted(expected)}
        add("observabilidade_disponivel", ok3, detail)

    # 4) storm de 5xx no front-end (propagação de dependência)
    res = _query_range(
        'sum(rate(istio_requests_total{namespace="sock-shop",'
        'destination_workload="front-end",response_code=~"5.."}[2m]))', start, end,
    )
    if res and "_error" in res[0]:
        add("front_end_5xx", False, f"prometheus erro: {res[0]['_error']}")
    else:
        vals = [float(v[1]) for s in res for v in s.get("values", [])]
        max_err = max(vals) if vals else 0.0
        add("front_end_5xx", max_err <= 0.5,
            f"max_5xx_rate={max_err:.2f} req/s (limiar 0.5)")

    ok_all = all(f["ok"] for f in findings)
    return {
        "schema_version": "1.0",
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "prometheus": PROM,
        "ok": ok_all,
        "findings": findings,
    }


def render(rep: Dict[str, Any]) -> str:
    lines = ["=" * 78, "VERIFICAÇÃO DE FALHA SISTÊMICA DE INFRA", "=" * 78]
    w = rep["window"]
    lines.append(f"Janela: {w['start'][:19]} → {w['end'][:19]}  (Prometheus {rep['prometheus']})")
    for f in rep["findings"]:
        mark = "✓" if f["ok"] else "✗"
        lines.append(f"  {mark} {f['check']:<28} {f['detail']}")
    lines.append("")
    lines.append("VEREDITO: " + ("LIMPA — sem falha sistêmica na janela"
                  if rep["ok"] else
                  "FALHA SISTÊMICA — revisar runs desta janela antes de usar no treino"))
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Verificador de falha sistêmica de infra")
    p.add_argument("--hours", type=float, default=6.0, help="janela = últimas N horas")
    p.add_argument("--start", default=None, help="ISO start (sobrepõe --hours)")
    p.add_argument("--end", default=None, help="ISO end (default: agora)")
    p.add_argument("--save", action="store_true", help="salva JSON em out/")
    args = p.parse_args()

    end = datetime.fromisoformat(args.end.replace("Z", "+00:00")) if args.end else datetime.now(timezone.utc)
    if args.start:
        start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    else:
        start = end - timedelta(hours=args.hours)

    rep = check(start, end)
    print(render(rep))

    if args.save:
        out_dir = os.path.join(os.path.dirname(__file__), "out")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "infra_check.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2, ensure_ascii=False)
        print(f"\nSalvo em {path}")

    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
