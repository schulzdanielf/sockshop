#!/usr/bin/env python3
"""Auditoria offline da avaliação RCA (run × strategy).

Reconstrói, SEM chamar o LLM e SEM precisar do servidor rodando, *exatamente*
o que foi enviado ao modelo em cada estratégia e qual foi o processo decisório
até o veredito final. Lê tudo do ``platform.db`` (SQLite) e do ``evaluation.csv``.

O que cada parte responde
-------------------------
* **"O que foi passado para o modelo?"**  → o prompt montado: system card +
  resumo do run alvo (já com o ground-truth mascarado) + vizinhos do RAG.
* **"Como foi o processo decisório?"**     → recuperação RAG (modo, vizinhos,
  scores), resposta crua do LLM (raw_response), veredito parseado e os dois
  pós-processadores baseados em regra (FaultCategoryValidator + ServiceLocalizer).
* **"Quais dados foram usados?"**          → os ``metric_hotspots`` (evidência
  medida: OOMKilled, restarts, saturação CPU/memória) e o ground-truth.

Uso
---
    # visão geral: todos os runs de teste, ground-truth e acertos por estratégia
    python -m experiment.eval.memory_hog.audit overview

    # auditoria completa de um run (todas as estratégias)
    python -m experiment.eval.memory_hog.audit run run-e8526d629558

    # só o prompt EXATO de uma estratégia (para copiar/colar)
    python -m experiment.eval.memory_hog.audit prompt run-e8526d629558 --strategy S2_rag_embeddings

    # só a evidência medida (os dados crus que sustentam a decisão)
    python -m experiment.eval.memory_hog.audit data run-e8526d629558

    # salvar bundles .txt/.json em out/audit/ para arquivar
    python -m experiment.eval.memory_hog.audit run run-e8526d629558 --save

Acrescente ``--config caminho.yaml`` para auditar a run mini (config-mini.yaml).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml

# ── Localiza a raiz do repo e injeta no sys.path para importar o backend ──
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiment.platform.backend.analysis import (  # noqa: E402
    assemble_prompt,
    blob_to_vector,
    rank_by_embedding,
    rank_hybrid,
    rank_similar_runs,
)
from experiment.platform.backend.storage.sqlite_storage import (  # noqa: E402
    SqliteStorage,
)

DEFAULT_DB = ROOT / "experiment" / "platform" / "data" / "platform.db"
DEFAULT_OBJ = ROOT / "experiment" / "platform" / "data" / "object_store"

SEP = "═" * 78
SUB = "─" * 78


# ── Reconstrução fiel do contexto RAG (espelha api._build_rag_context) ─────
def build_rag_context(
    storage: SqliteStorage, run_id: str, limit: int, mode: str
) -> Dict[str, Any]:
    """Reproduz a mesma recuperação de vizinhos que o endpoint usou.

    Usa o provider de embedding gravado no run alvo (sem instanciar modelo),
    então funciona offline para os modos embedding/hybrid/tags.
    """
    target = storage.get_run_summary(run_id)
    if target is None:
        raise SystemExit(f"run {run_id!r} não tem summary no banco")

    if mode == "hybrid":
        target_emb = storage.get_run_embedding(run_id)
        target_features = storage.get_run_features(run_id) or {}
        target_graph = target_features.get("propagation_graph") or {}
        if target_emb is not None:
            target_vec = blob_to_vector(target_emb["blob"], target_emb["dim"])
            rows = storage.iter_run_embeddings(
                provider=target_emb["provider"],
                limit=1000,
                include_features=True,
            )
            candidates: List[Dict[str, Any]] = []
            for row in rows:
                try:
                    row["vector"] = blob_to_vector(row["blob"], row["dim"])
                except ValueError:
                    continue
                row["graph"] = (row.get("features") or {}).get(
                    "propagation_graph"
                ) or {}
                candidates.append(row)
            ranked = rank_hybrid(
                target_vec,
                target_graph,
                candidates,
                limit=limit,
                exclude_run_id=run_id,
            )
            return {
                "mode": "hybrid",
                "target": target,
                "neighbours": [
                    {
                        "run_id": n["run_id"],
                        "score": n["score"],
                        "semantic_score": n.get("semantic_score"),
                        "graph_score": n.get("graph_score"),
                        "cascade_overlap_services": n.get(
                            "cascade_overlap_services", []
                        ),
                        "verdict": n["verdict"],
                        "summary_text": n.get("summary_text", ""),
                    }
                    for n in ranked
                ],
            }
        mode = "tags"

    use_embedding = mode == "embedding"
    target_emb = storage.get_run_embedding(run_id) if use_embedding else None
    if use_embedding and target_emb is None:
        use_embedding = False

    if use_embedding and target_emb is not None:
        target_vec = blob_to_vector(target_emb["blob"], target_emb["dim"])
        rows = storage.iter_run_embeddings(provider=target_emb["provider"], limit=1000)
        candidates = []
        for row in rows:
            try:
                row["vector"] = blob_to_vector(row["blob"], row["dim"])
            except ValueError:
                continue
            candidates.append(row)
        ranked = rank_by_embedding(
            target_vec, candidates, limit=limit, exclude_run_id=run_id
        )
        used_mode = "embedding"
    else:
        cand = storage.list_run_summaries(limit=500)
        ranked = rank_similar_runs(
            target.get("tags") or [], cand, limit=limit, exclude_run_id=run_id
        )
        used_mode = "tags"

    return {
        "mode": used_mode,
        "target": target,
        "neighbours": [
            {
                "run_id": n["run_id"],
                "score": n["score"],
                "verdict": n["verdict"],
                "summary_text": n.get("summary_text", ""),
            }
            for n in ranked
        ],
    }


# ── Carregamento de config / CSVs ──────────────────────────────────────────
def load_cfg(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def strategy_by_name(cfg: Dict[str, Any], name: str) -> Dict[str, Any]:
    for s in cfg["strategies"]:
        if s["name"] == name:
            return s
    raise SystemExit(
        f"estratégia {name!r} não existe. Opções: "
        + ", ".join(s["name"] for s in cfg["strategies"])
    )


def out_dir(cfg: Dict[str, Any]) -> Path:
    d = Path(cfg["output"]["results_dir"])
    if not d.is_absolute():
        d = ROOT / d
    return d


def load_eval_rows(cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    path = out_dir(cfg) / cfg["output"]["evaluation_csv"]
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_runs_rows(cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    path = out_dir(cfg) / cfg["output"]["runs_csv"]
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fault_category_for(cfg: Dict[str, Any], chaos_type: str) -> str:
    for e in cfg.get("chaos_types", []) or []:
        if e.get("name") == chaos_type:
            return e.get("fault_category", "unknown")
    return "unknown"


# ── Renderizadores ─────────────────────────────────────────────────────────
def _ground_truth(
    storage: SqliteStorage,
    cfg: Dict[str, Any],
    run_id: str,
    runs_by_id: Dict[str, Dict[str, str]],
) -> Dict[str, str]:
    feats = storage.get_run_features(run_id) or {}
    gt_service = feats.get("ground_truth_service")
    gt_fault = feats.get("ground_truth_fault_category")
    row = runs_by_id.get(run_id, {})
    if not gt_service:
        gt_service = row.get("chaos_target", "?")
    if not gt_fault:
        gt_fault = fault_category_for(cfg, row.get("chaos_type", ""))
    return {
        "service": gt_service or "?",
        "fault_category": gt_fault or "?",
        "chaos_type": row.get("chaos_type", "?"),
    }


def render_prompt(
    storage: SqliteStorage, run_id: str, strat: Dict[str, Any]
) -> Dict[str, Any]:
    """Monta o prompt EXATO daquela estratégia + metadados."""
    rag = build_rag_context(storage, run_id, strat["limit"], strat["mode"])
    system_id = strat["system_id"]
    # "__none__" força o caminho sem system card (igual ao runner).
    sid = None if system_id == "__none__" else system_id
    prompt, meta = assemble_prompt(
        rag,
        budget_tokens=strat["budget_tokens"],
        max_neighbours=strat["limit"],
        system_id=sid,
    )
    return {"prompt": prompt, "meta": meta, "rag": rag}


def print_prompt_bundle(
    storage: SqliteStorage, run_id: str, strat: Dict[str, Any]
) -> str:
    b = render_prompt(storage, run_id, strat)
    rag = b["rag"]
    lines: List[str] = []
    lines.append(SEP)
    lines.append(
        f"PROMPT EXATO ENVIADO AO MODELO — run={run_id} strategy={strat['name']}"
    )
    lines.append(
        f"mode(pedido)={strat['mode']}  mode(efetivo)={rag['mode']}  "
        f"limit={strat['limit']}  budget_tokens={strat['budget_tokens']}  "
        f"system_id={strat['system_id']}"
    )
    lines.append(
        f"tokens_estimados(prompt)={b['meta']['prompt_tokens_estimate']}  "
        f"vizinhos_usados={b['meta']['neighbour_count']}"
    )
    lines.append(SEP)
    lines.append("")
    lines.append(b["prompt"])
    lines.append("")
    return "\n".join(lines)


def print_run_bundle(
    storage: SqliteStorage,
    cfg: Dict[str, Any],
    run_id: str,
    runs_by_id: Dict[str, Dict[str, str]],
    eval_by_run: Dict[str, List[Dict[str, str]]],
) -> str:
    gt = _ground_truth(storage, cfg, run_id, runs_by_id)
    feats = storage.get_run_features(run_id) or {}
    stored = storage.get_llm_analysis(run_id) or {}
    analysis = stored.get("analysis") or {}

    out: List[str] = []
    out.append(SEP)
    out.append(f"AUDITORIA DE RUN — {run_id}")
    out.append(SEP)
    out.append(
        f"GROUND TRUTH  →  serviço={gt['service']}  "
        f"falha={gt['fault_category']}  (chaos_type={gt['chaos_type']})"
    )
    out.append("Esse é o gabarito injetado pelo harness. NÃO é mostrado ao modelo;")
    out.append("o prompt do alvo é mascarado (veja a seção de máscara abaixo).")
    out.append("")

    # 1) Evidência medida (dados crus)
    out.append(SUB)
    out.append("1) DADOS USADOS — evidência medida (metric_hotspots por fase)")
    out.append(SUB)
    out.append(_render_evidence(feats))
    out.append("")

    # 2) Resumo alvo: cru vs mascarado
    out.append(SUB)
    out.append("2) O QUE FOI ESCONDIDO DO MODELO — máscara do resumo alvo")
    out.append(SUB)
    out.append(_render_mask_diff(storage, run_id))
    out.append("")

    # 3) Prompt + RAG por estratégia
    out.append(SUB)
    out.append(
        "3) O QUE FOI ENVIADO AO MODELO — prompt e recuperação RAG por estratégia"
    )
    out.append(SUB)
    for strat in cfg["strategies"]:
        try:
            b = render_prompt(storage, run_id, strat)
        except SystemExit as exc:
            out.append(f"  [{strat['name']}] erro: {exc}")
            continue
        rag = b["rag"]
        out.append(
            f"  • {strat['name']}: mode={rag['mode']} "
            f"tokens≈{b['meta']['prompt_tokens_estimate']} "
            f"vizinhos={b['meta']['neighbour_count']} "
            f"system_card={'sim' if strat['system_id'] != '__none__' else 'NÃO'}"
        )
        for n in rag.get("neighbours", []):
            extra = ""
            if "semantic_score" in n:
                extra = (
                    f" [sem={_fmt(n.get('semantic_score'))} "
                    f"graph={_fmt(n.get('graph_score'))}]"
                )
            nb_id = n["run_id"]
            nb_gt = _ground_truth(storage, cfg, nb_id, runs_by_id)
            out.append(
                f"       └ vizinho {nb_id} score={_fmt(n.get('score'))}{extra} "
                f"verdict={n.get('verdict')}  "
                f"(gabarito vizinho: {nb_gt['service']}/{nb_gt['fault_category']})"
            )
        if not rag.get("neighbours"):
            out.append("       └ (nenhum vizinho — baseline sem RAG)")
    out.append("")
    out.append("  → para ver o texto integral de um prompt:")
    out.append(
        f"     python -m experiment.eval.memory_hog.audit prompt {run_id} "
        f"--strategy {cfg['strategies'][0]['name']}"
    )
    out.append("")

    # 4) Decisão do modelo + pós-processadores (do último analysis no banco)
    out.append(SUB)
    out.append("4) PROCESSO DECISÓRIO DO MODELO (analysis persistido no banco)")
    out.append(SUB)
    if not analysis:
        out.append("  (sem analysis persistido para este run)")
    else:
        out.append(_render_decision(analysis))
    out.append("")

    # 5) Placar por estratégia (evaluation.csv)
    out.append(SUB)
    out.append("5) RESULTADO POR ESTRATÉGIA (evaluation.csv) — predição vs gabarito")
    out.append(SUB)
    rows = eval_by_run.get(run_id, [])
    if not rows:
        out.append("  (sem linhas em evaluation.csv para este run)")
    else:
        out.append(
            f"  {'strategy':<26}{'pred_serviço':<14}{'pred_falha':<20}"
            f"{'where':<7}{'why':<6}{'both':<6}{'conf':<6}val/loc"
        )
        for r in rows:
            valloc = []
            if r.get("validator_fired") == "True":
                valloc.append(f"val:{r.get('validator_rule')}")
            if r.get("localizer_fired") == "True":
                valloc.append(f"loc:{r.get('localizer_rule')}")
            out.append(
                f"  {r['strategy']:<26}{(r.get('predicted_rca') or '-'):<14}"
                f"{(r.get('fault_category') or '-'):<20}"
                f"{_chk(r.get('correct_target')):<7}{_chk(r.get('correct_fault')):<6}"
                f"{_chk(r.get('correct_full')):<6}{(r.get('confidence') or '-'):<6}"
                f"{' '.join(valloc)}"
            )
    out.append("")
    return "\n".join(out)


def _render_evidence(feats: Dict[str, Any]) -> str:
    hot = feats.get("metric_hotspots") or {}
    if not hot:
        return "  (sem metric_hotspots)"
    # Métricas mais discriminantes para causa-raiz.
    focus = [
        ("oom_killed", "OOMKilled (0→1 = memória estourou)"),
        ("pod_restarts_total", "restarts acumulados (Δ = pod reiniciou/substituído)"),
        ("memory_saturation_pct", "memória % do limite"),
        ("cpu_saturation_pct", "CPU % do limite"),
        ("cpu_throttled", "CPU throttled s/s (cgroup limitou)"),
        ("error_rate", "taxa de erro 5xx"),
        ("latency_p99", "latência p99"),
    ]
    lines: List[str] = []
    for mid, desc in focus:
        entry = hot.get(mid) or {}
        rows = [r for r in (entry.get("top") or []) if isinstance(r, dict)]
        rows = [
            r
            for r in rows
            if abs(float(r.get("delta_abs") or 0)) > 0
            or float(r.get("fault_mean") or 0) > 0
        ]
        if not rows:
            continue
        lines.append(f"  {mid}  — {desc}")
        for r in rows[:4]:
            lines.append(
                f"     {r.get('label', '?'):<22} "
                f"baseline={_fmt(r.get('baseline_mean'))} "
                f"fault={_fmt(r.get('fault_mean'))} "
                f"Δ={_fmt(r.get('delta_abs'))}"
            )
    return "\n".join(lines) if lines else "  (nenhum hotspot com sinal)"


def _render_mask_diff(storage: SqliteStorage, run_id: str) -> str:
    """Mostra o que o mascaramento remove do resumo alvo."""
    from experiment.platform.backend.analysis.prompt import _mask_target_summary

    target = storage.get_run_summary(run_id) or {}
    raw = target.get("summary_text") or ""
    masked = _mask_target_summary(raw)
    raw_lines = raw.splitlines()
    masked_set = set(masked.splitlines())
    removed = [ln for ln in raw_lines if ln not in masked_set and ln.strip()]
    lines: List[str] = []
    lines.append(
        "  Trechos REMOVIDOS do resumo antes de ir ao modelo (anti-vazamento):"
    )
    if not removed:
        lines.append("     (nada removido — resumo já estava limpo)")
    for ln in removed[:25]:
        lines.append(f"     - {ln.strip()[:110]}")
    lines.append(
        "  Tags do alvo também são filtradas (prefixos chaos:/svc:/fault_category:)."
    )
    return "\n".join(lines)


def _render_decision(analysis: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(
        f"  veredito={analysis.get('verdict')}  "
        f"rca(serviço)={analysis.get('rca')}  "
        f"fault_category={analysis.get('fault_category')}  "
        f"confiança={analysis.get('confidence')}"
    )
    lines.append(
        f"  retry_count={analysis.get('retry_count')}  "
        f"rag_mode={analysis.get('rag_mode')}  "
        f"latência_ms={analysis.get('llm_latency_ms')}"
    )
    reasoning = (analysis.get("reasoning") or "").strip()
    if reasoning:
        lines.append("  raciocínio do LLM:")
        lines.append(_indent_wrap(reasoning, 6))
    cites = analysis.get("citations") or []
    if cites:
        lines.append(f"  citações: {', '.join(map(str, cites))}")

    vm = analysis.get("validator_meta") or {}
    if vm:
        lines.append("  ── FaultCategoryValidator (regra sobre a evidência medida) ──")
        lines.append(f"     disparou={vm.get('fired')}  regra={vm.get('rule')}")
        lines.append(
            f"     fault original(LLM)={vm.get('original_fault_category')} "
            f"→ novo={vm.get('new_fault_category')}"
        )
        if vm.get("conflict"):
            lines.append(f"     CONFLITO={vm.get('conflict')} (manteve o LLM)")
        if vm.get("reason"):
            lines.append(f"     motivo: {vm.get('reason')}")
        for ev in (vm.get("evidence") or [])[:4]:
            lines.append(f"     evidência: {json.dumps(ev, ensure_ascii=False)}")
        loc = vm.get("localizer") or {}
        if loc:
            lines.append(
                "  ── ServiceLocalizerValidator (ancora no fault corrigido) ──"
            )
            lines.append(f"     disparou={loc.get('fired')}  regra={loc.get('rule')}")
            lines.append(
                f"     rca original(LLM)={loc.get('original_rca')} "
                f"→ novo={loc.get('new_rca')}"
            )
            if loc.get("reason"):
                lines.append(f"     motivo: {loc.get('reason')}")

    raw = (analysis.get("raw_response") or "").strip()
    if raw:
        lines.append("  resposta CRUA do LLM (antes do parse/validators):")
        lines.append(_indent_wrap(raw[:1200], 6))
    return "\n".join(lines)


# ── helpers de formatação ──────────────────────────────────────────────────
def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v):.3f}"
    except (TypeError, ValueError):
        return str(v)


def _chk(v: Any) -> str:
    return "OK" if str(v) == "True" else ("·" if v in (None, "", "None") else "X")


def _indent_wrap(text: str, indent: int) -> str:
    pad = " " * indent
    out: List[str] = []
    for para in text.splitlines() or [text]:
        while len(para) > 100:
            cut = para.rfind(" ", 0, 100)
            cut = cut if cut > 0 else 100
            out.append(pad + para[:cut])
            para = para[cut:].lstrip()
        out.append(pad + para)
    return "\n".join(out)


def render_overview(
    storage: SqliteStorage,
    cfg: Dict[str, Any],
    runs_by_id: Dict[str, Dict[str, str]],
    eval_by_run: Dict[str, List[Dict[str, str]]],
) -> str:
    out: List[str] = []
    out.append(SEP)
    out.append("VISÃO GERAL — runs de TESTE, gabarito e acerto por estratégia")
    out.append(SEP)
    test_runs = [r for r in runs_by_id.values() if r.get("phase") == "test"]
    if not test_runs:
        test_runs = list(runs_by_id.values())
    for r in test_runs:
        run_id = r.get("run_id") or ""
        if not run_id:
            continue
        gt = _ground_truth(storage, cfg, run_id, runs_by_id)
        out.append("")
        out.append(
            f"{run_id}  →  gabarito: {gt['service']}/{gt['fault_category']} "
            f"(chaos={gt['chaos_type']}, status={r.get('status')})"
        )
        rows = eval_by_run.get(run_id, [])
        for er in rows:
            out.append(
                f"    {er['strategy']:<26} pred={er.get('predicted_rca') or '-':<12}/"
                f"{er.get('fault_category') or '-':<18} "
                f"where={_chk(er.get('correct_target'))} "
                f"why={_chk(er.get('correct_fault'))} "
                f"both={_chk(er.get('correct_full'))}"
            )
    out.append("")
    out.append("Legenda: where=acertou o serviço | why=acertou a causa | both=ambos")
    out.append("")
    out.append("Para auditar um run em detalhe:")
    out.append("  python -m experiment.eval.memory_hog.audit run <run_id>")
    return "\n".join(out)


# ── CLI ────────────────────────────────────────────────────────────────────
def _maybe_save(cfg: Dict[str, Any], name: str, content: str, save: bool) -> None:
    if not save:
        return
    d = out_dir(cfg) / "audit"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(content, encoding="utf-8")
    print(f"\n[salvo] {path.relative_to(ROOT)}", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--config",
        default=str(HERE / "config.yaml"),
        help="config.yaml da avaliação (use config-mini.yaml para a run mini)",
    )
    p.add_argument("--db", default=str(DEFAULT_DB), help="caminho do platform.db")
    p.add_argument(
        "--save", action="store_true", help="também grava o bundle em out/audit/"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("overview", help="lista runs de teste + acerto por estratégia")

    sp_run = sub.add_parser("run", help="auditoria completa de um run")
    sp_run.add_argument("run_id")

    sp_prompt = sub.add_parser(
        "prompt", help="imprime o prompt exato de uma estratégia"
    )
    sp_prompt.add_argument("run_id")
    sp_prompt.add_argument("--strategy", required=True)

    sp_data = sub.add_parser(
        "data", help="imprime a evidência medida (metric_hotspots)"
    )
    sp_data.add_argument("run_id")

    args = p.parse_args()
    cfg = load_cfg(Path(args.config))
    storage = SqliteStorage(db_path=Path(args.db), object_store_root=DEFAULT_OBJ)

    runs_rows = load_runs_rows(cfg)
    runs_by_id = {r["run_id"]: r for r in runs_rows if r.get("run_id")}
    eval_rows = load_eval_rows(cfg)
    eval_by_run: Dict[str, List[Dict[str, str]]] = {}
    for r in eval_rows:
        eval_by_run.setdefault(r["run_id"], []).append(r)

    if args.cmd == "overview":
        content = render_overview(storage, cfg, runs_by_id, eval_by_run)
        print(content)
        _maybe_save(cfg, "overview.txt", content, args.save)
    elif args.cmd == "run":
        content = print_run_bundle(storage, cfg, args.run_id, runs_by_id, eval_by_run)
        print(content)
        _maybe_save(cfg, f"{args.run_id}.txt", content, args.save)
    elif args.cmd == "prompt":
        strat = strategy_by_name(cfg, args.strategy)
        content = print_prompt_bundle(storage, args.run_id, strat)
        print(content)
        _maybe_save(
            cfg, f"{args.run_id}-{args.strategy}-prompt.txt", content, args.save
        )
    elif args.cmd == "data":
        feats = storage.get_run_features(args.run_id) or {}
        content = f"EVIDÊNCIA MEDIDA — {args.run_id}\n{SUB}\n" + _render_evidence(feats)
        print(content)
        _maybe_save(cfg, f"{args.run_id}-data.txt", content, args.save)
    return 0


if __name__ == "__main__":
    sys.exit(main())
