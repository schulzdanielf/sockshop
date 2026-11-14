#!/usr/bin/env python3
"""Generate a self-contained HTML comparison report from test-base-v6 results."""

from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EVALUATION_ID = "test-base-v6-20261107T215646147881Z"
SOURCE = (
    ROOT
    / "experiment/eval/memory_hog/out/knowledge_evaluation"
    / EVALUATION_ID
    / "test_base_rag_evaluation.json"
)
OUTPUT = (
    ROOT
    / "research/diario"
    / f"{EVALUATION_ID}-comparacoes-skill-rag.html"
)

SKILL = "original"
RAG = "exploratory_rag"
METHOD_NAMES = {SKILL: "Skill", RAG: "RAG"}
METHOD_COLORS = {SKILL: "#5966b3", RAG: "#168878"}
SERVICE_ORDER = ["carts", "catalogue", "front-end", "orders", "payment", "shipping", "user"]
FAULT_ORDER = ["container-kill", "cpu-hog", "http-status-code", "memory-hog", "network-latency"]


def escaped(value: Any) -> str:
    return html.escape(str(value), quote=True)


def pct(count: int, denominator: int) -> float:
    return count / denominator * 100 if denominator else 0.0


def svg_text(x: float, y: float, value: str, **attrs: str) -> str:
    attributes = " ".join(
        f'{"class" if key == "class_" else key.replace("_", "-")}="{escaped(item)}"'
        for key, item in attrs.items()
    )
    return f'<text x="{x:.1f}" y="{y:.1f}" {attributes}>{escaped(value)}</text>'


def grouped_bars(
    title: str,
    rows: list[dict[str, Any]],
    series: list[dict[str, str]],
    *,
    scale: float = 100.0,
    width: int = 980,
) -> str:
    left = 185
    right = 95
    plot_width = width - left - right
    top = 60
    row_height = 58
    height = top + row_height * len(rows) + 52
    pieces = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escaped(title)}">',
        f"<title>{escaped(title)}</title>",
        '<style>text{font-family:Arial,sans-serif;fill:#253044}.muted{fill:#667085;font-size:12px}</style>',
        svg_text(12, 24, title, font_size="17", font_weight="700"),
    ]
    for index, item in enumerate(series):
        x = left + index * 160
        pieces.append(f'<rect x="{x}" y="38" width="12" height="12" rx="2" fill="{item["color"]}"/>')
        pieces.append(svg_text(x + 18, 49, item["label"], font_size="12"))
    for tick in range(0, 101, 25):
        x = left + plot_width * tick / 100
        pieces.append(f'<line x1="{x:.1f}" y1="{top - 4}" x2="{x:.1f}" y2="{top + row_height * len(rows) - 6}" stroke="#e6e9ef"/>')
        pieces.append(svg_text(x, height - 19, f"{tick}%", text_anchor="middle", class_="muted"))
    for row_index, row in enumerate(rows):
        y = top + row_index * row_height
        pieces.append(svg_text(12, y + 28, row["label"], font_size="13", font_weight="600"))
        denominator = row["denominator"]
        for series_index, item in enumerate(series):
            count = row[item["key"]]
            value = pct(count, denominator)
            bar_y = y + 3 + series_index * 23
            bar_width = plot_width * value / scale
            pieces.append(
                f'<rect x="{left}" y="{bar_y}" width="{bar_width:.1f}" height="15" rx="3" '
                f'fill="{item["color"]}"/>'
            )
            label = f"{count}/{denominator} ({value:.1f}%)"
            label_x = min(left + bar_width + 7, left + plot_width - 3)
            anchor = "start" if value < 88 else "end"
            if anchor == "end":
                label_x = left + plot_width - 4
            pieces.append(svg_text(label_x, bar_y + 12, label, font_size="11", text_anchor=anchor))
    pieces.append("</svg>")
    return "".join(pieces)


def paired_outcomes_chart(comparison: dict[str, int]) -> str:
    width, height = 780, 300
    left, top, plot_width = 180, 62, 500
    rows = [
        ("Somente Skill correta", comparison["skill_only_correct"], "#5966b3"),
        ("Somente RAG correta", comparison["rag_only_correct"], "#168878"),
        ("Ambas corretas", comparison["both_correct"], "#375a7f"),
        ("Nenhuma correta", comparison["neither_correct"], "#98a2b3"),
    ]
    maximum = max(value for _, value, _ in rows)
    pieces = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Resultados pareados por incidente">',
        "<title>Resultados pareados por incidente</title>",
        '<style>text{font-family:Arial,sans-serif;fill:#253044}.muted{fill:#667085;font-size:12px}</style>',
        svg_text(12, 24, "Comparação pareada — cada incidente conta uma vez", font_size="17", font_weight="700"),
    ]
    for tick in range(maximum + 1):
        x = left + plot_width * tick / maximum
        pieces.append(f'<line x1="{x:.1f}" y1="{top - 5}" x2="{x:.1f}" y2="{top + 4 * 48 - 7}" stroke="#e6e9ef"/>')
        pieces.append(svg_text(x, height - 18, str(tick), text_anchor="middle", class_="muted"))
    for index, (label, value, color) in enumerate(rows):
        y = top + index * 48
        pieces.append(svg_text(12, y + 19, label, font_size="13", font_weight="600"))
        bar_width = plot_width * value / maximum
        pieces.append(f'<rect x="{left}" y="{y}" width="{bar_width:.1f}" height="25" rx="4" fill="{color}"/>')
        pieces.append(svg_text(left + bar_width + 8, y + 18, f"{value}/32 ({pct(value, 32):.1f}%)", font_size="12"))
    pieces.append("</svg>")
    return "".join(pieces)


def retrieval_chart(relevance: dict[str, Any]) -> str:
    width, height = 900, 245
    left, top, plot_width = 210, 62, 600
    groups = [
        ("Par relevante no Top-3", relevance["with_relevant_retrieval"]),
        ("Sem par relevante no Top-3", relevance["without_relevant_retrieval"]),
    ]
    pieces = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Acerto da RAG condicionado ao resultado da recuperação">',
        "<title>Acerto RAG condicionado ao resultado da recuperação</title>",
        '<style>text{font-family:Arial,sans-serif;fill:#253044}.muted{fill:#667085;font-size:12px}</style>',
        svg_text(12, 24, "Diagnóstico da RAG: acerto conjunto condicionado ao Hit@3", font_size="17", font_weight="700"),
    ]
    for tick in range(0, 101, 25):
        x = left + plot_width * tick / 100
        pieces.append(f'<line x1="{x:.1f}" y1="{top - 5}" x2="{x:.1f}" y2="{top + 2 * 68 - 8}" stroke="#e6e9ef"/>')
        pieces.append(svg_text(x, height - 18, f"{tick}%", text_anchor="middle", class_="muted"))
    for index, (label, group) in enumerate(groups):
        y = top + index * 68
        denominator = group["denominator"]
        correct = group["jointly_correct"]
        wrong = denominator - correct
        pieces.append(svg_text(12, y + 29, label, font_size="13", font_weight="600"))
        correct_width = plot_width * correct / denominator
        wrong_width = plot_width * wrong / denominator
        pieces.append(f'<rect x="{left}" y="{y}" width="{correct_width:.1f}" height="30" rx="4" fill="#168878"/>')
        pieces.append(f'<rect x="{left + correct_width:.1f}" y="{y}" width="{wrong_width:.1f}" height="30" fill="#d97757"/>')
        pieces.append(svg_text(left + correct_width / 2, y + 20, f"Correto {correct}/{denominator}", fill="#ffffff", text_anchor="middle", font_size="11"))
        if wrong:
            pieces.append(svg_text(left + correct_width + wrong_width / 2, y + 20, f"Errado {wrong}/{denominator}", fill="#ffffff", text_anchor="middle", font_size="11"))
    pieces.append("</svg>")
    return "".join(pieces)


def combination_heatmap(runs: list[dict[str, Any]]) -> str:
    cells: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"n": 0, "skill": 0, "rag": 0, "eligible": 0, "hit": 0}
    )
    for run in runs:
        cell = cells[(run["service"], run["fault_type"])]
        cell["n"] += 1
        cell["skill"] += int(run["methods"][SKILL]["joint_correct"])
        cell["rag"] += int(run["methods"][RAG]["joint_correct"])
        cell["eligible"] += int(bool(run["eligible_exact_pair_ids"]))
        cell["hit"] += int(run["retrieval"]["hit_at_3"])

    left, top, cell_width, cell_height = 145, 75, 110, 75
    width = left + len(SERVICE_ORDER) * cell_width + 12
    height = top + len(FAULT_ORDER) * cell_height + 30
    fills = {
        "rag": "#d9f0e9",
        "skill": "#fbe7d2",
        "both": "#dce8f5",
        "neither": "#f0f2f5",
        "empty": "#ffffff",
    }
    pieces = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Mapa de acertos por combinação de serviço e falha">',
        "<title>Acertos por combinação de serviço e falha</title>",
        '<style>text{font-family:Arial,sans-serif;fill:#253044}.muted{fill:#667085;font-size:11px}</style>',
        svg_text(12, 24, "Combinações serviço × falha — acerto conjunto e elegibilidade", font_size="17", font_weight="700"),
    ]
    for service_index, service in enumerate(SERVICE_ORDER):
        x = left + service_index * cell_width + cell_width / 2
        pieces.append(svg_text(x, 58, service, text_anchor="middle", font_size="11", font_weight="600"))
    for fault_index, fault in enumerate(FAULT_ORDER):
        y = top + fault_index * cell_height
        pieces.append(svg_text(12, y + 37, fault, font_size="11", font_weight="600"))
        for service_index, service in enumerate(SERVICE_ORDER):
            x = left + service_index * cell_width
            cell = cells.get((service, fault))
            if not cell:
                pieces.append(f'<rect x="{x}" y="{y}" width="{cell_width - 4}" height="{cell_height - 4}" rx="5" fill="{fills["empty"]}" stroke="#e6e9ef"/>')
                pieces.append(svg_text(x + cell_width / 2 - 2, y + 38, "—", text_anchor="middle", class_="muted"))
                continue
            if cell["skill"] and cell["rag"]:
                key = "both"
            elif cell["skill"] > cell["rag"]:
                key = "skill"
            elif cell["rag"] > cell["skill"]:
                key = "rag"
            else:
                key = "neither"
            pieces.append(f'<rect x="{x}" y="{y}" width="{cell_width - 4}" height="{cell_height - 4}" rx="5" fill="{fills[key]}" stroke="#d7dce5"/>')
            pieces.append(svg_text(x + cell_width / 2 - 2, y + 29, f'n={cell["n"]}  S={cell["skill"]}  R={cell["rag"]}', text_anchor="middle", font_size="10", font_weight="600"))
            pieces.append(svg_text(x + cell_width / 2 - 2, y + 48, f'E={cell["eligible"]}  H={cell["hit"]}', text_anchor="middle", class_="muted"))
    legend = [
        ("Só RAG melhor", fills["rag"]),
        ("Só Skill melhor", fills["skill"]),
        ("Ambas acertam", fills["both"]),
        ("Nenhuma acerta", fills["neither"]),
        ("Sem caso", fills["empty"]),
    ]
    legend_y = height - 11
    cursor = 12
    for label, color in legend:
        pieces.append(f'<rect x="{cursor}" y="{legend_y - 10}" width="10" height="10" rx="2" fill="{color}" stroke="#d7dce5"/>')
        pieces.append(svg_text(cursor + 15, legend_y, label, class_="muted"))
        cursor += 115 if label != "Só Skill melhor" else 125
    pieces.append("</svg>")
    return "".join(pieces)


def comparison_table(
    groups: dict[str, Any],
    runs: list[dict[str, Any]],
    order: list[str],
    label: str,
) -> str:
    group_label = {"service": "Serviço", "fault_type": "Falha"}[label]
    rows = []
    for name in order:
        group_runs = [run for run in runs if run[label] == name]
        denominator = len(group_runs)
        skill = sum(run["methods"][SKILL]["joint_correct"] for run in group_runs)
        rag = sum(run["methods"][RAG]["joint_correct"] for run in group_runs)
        stats = groups[name]
        delta = rag - skill
        delta_text = f"{delta:+d} ({pct(rag, denominator) - pct(skill, denominator):+.1f} pp)"
        rows.append(
            "<tr>"
            f"<th scope='row'>{escaped(name)}</th>"
            f"<td>{denominator}</td>"
            f"<td>{skill}/{denominator} ({pct(skill, denominator):.1f}%)</td>"
            f"<td>{rag}/{denominator} ({pct(rag, denominator):.1f}%)</td>"
            f"<td class='{'positive' if delta > 0 else 'negative' if delta < 0 else ''}'>{delta_text}</td>"
            f"<td>{stats['eligible_exact_pair_cases']}/{denominator} ({pct(stats['eligible_exact_pair_cases'], denominator):.1f}%)</td>"
            f"<td>{stats['hit_at_3_cases']}/{denominator} ({pct(stats['hit_at_3_cases'], denominator):.1f}%)</td>"
            "</tr>"
        )
    return (
        "<div class='table-wrap'><table><thead><tr>"
        f"<th>{group_label}</th><th>n</th><th>Skill: conjunto</th>"
        "<th>RAG: conjunto</th><th>Delta RAG−Skill</th><th>Par elegível</th><th>Hit@3</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def case_table(runs: list[dict[str, Any]], *, winners: str) -> str:
    selected = []
    for run in runs:
        skill_ok = run["methods"][SKILL]["joint_correct"]
        rag_ok = run["methods"][RAG]["joint_correct"]
        if winners == "rag" and rag_ok and not skill_ok:
            selected.append(run)
        elif winners == "skill" and skill_ok and not rag_ok:
            selected.append(run)
    rows = []
    for run in selected:
        rows.append(
            "<tr>"
            f"<td>{escaped(run['run_id'])}</td>"
            f"<td>{escaped(run['service'])} / {escaped(run['fault_type'])}</td>"
            f"<td>{escaped(run['predictions'][SKILL]['service'])} / {escaped(run['predictions'][SKILL]['fault_type'])}</td>"
            f"<td>{escaped(run['predictions'][RAG]['service'])} / {escaped(run['predictions'][RAG]['fault_type'])}</td>"
            f"<td>{'sim' if run['eligible_exact_pair_ids'] else 'não'}</td>"
            f"<td>{'sim' if run['retrieval']['hit_at_3'] else 'não'}</td>"
            "</tr>"
        )
    return (
        "<div class='table-wrap'><table><thead><tr><th>Run</th><th>Rótulo</th>"
        "<th>Predição Skill</th><th>Predição RAG</th><th>Par elegível</th><th>Hit@3</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def retrieval_mismatch_table(runs: list[dict[str, Any]]) -> str:
    rows = []
    for run in runs:
        hit = run["retrieval"]["hit_at_3"]
        rag_ok = run["methods"][RAG]["joint_correct"]
        if hit == rag_ok:
            continue
        condition = "Hit@3, diagnóstico errado" if hit else "Sem Hit@3, diagnóstico correto"
        rows.append(
            "<tr>"
            f"<td>{condition}</td>"
            f"<td>{escaped(run['run_id'])}</td>"
            f"<td>{escaped(run['service'])} / {escaped(run['fault_type'])}</td>"
            f"<td>{escaped(run['predictions'][RAG]['service'])} / {escaped(run['predictions'][RAG]['fault_type'])}</td>"
            f"<td>{'sim' if run['eligible_exact_pair_ids'] else 'não'}</td>"
            "</tr>"
        )
    return (
        "<div class='table-wrap'><table><thead><tr><th>Resultado</th><th>Run</th>"
        "<th>Rótulo</th><th>Predição RAG</th><th>Par exato elegível</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def generate(data: dict[str, Any]) -> str:
    runs = data["per_run"]
    comparison = data["method_comparison"]
    service_rows = []
    for service in SERVICE_ORDER:
        group = data["by_service"][service]
        service_rows.append(
            {
                "label": service,
                "denominator": group["denominator"],
                SKILL: sum(run["methods"][SKILL]["joint_correct"] for run in runs if run["service"] == service),
                RAG: group["jointly_correct"],
            }
        )
    fault_rows = []
    for fault in FAULT_ORDER:
        group = data["by_fault"][fault]
        fault_rows.append(
            {
                "label": fault,
                "denominator": group["denominator"],
                SKILL: sum(run["methods"][SKILL]["joint_correct"] for run in runs if run["fault_type"] == fault),
                RAG: group["jointly_correct"],
            }
        )
    accuracy_rows = []
    for label, key in [
        ("Serviço correto", "service_correct"),
        ("Falha correta", "fault_type_correct"),
        ("Serviço + falha", "jointly_correct"),
    ]:
        accuracy_rows.append(
            {
                "label": label,
                "denominator": 32,
                SKILL: data["diagnosis_summary"][SKILL][key],
                RAG: data["diagnosis_summary"][RAG][key],
            }
        )
    service_eligibility = [
        {
            "label": service,
            "denominator": data["by_service"][service]["denominator"],
            "eligible": data["by_service"][service]["eligible_exact_pair_cases"],
            "hit": data["by_service"][service]["hit_at_3_cases"],
        }
        for service in SERVICE_ORDER
    ]
    fault_eligibility = [
        {
            "label": fault,
            "denominator": data["by_fault"][fault]["denominator"],
            "eligible": data["by_fault"][fault]["eligible_exact_pair_cases"],
            "hit": data["by_fault"][fault]["hit_at_3_cases"],
        }
        for fault in FAULT_ORDER
    ]
    service_metric_rows = [
        {
            "label": row["label"],
            "denominator": row["denominator"],
            "skill": row[SKILL],
            "rag": row[RAG],
        }
        for row in service_rows
    ]
    fault_metric_rows = [
        {
            "label": row["label"],
            "denominator": row["denominator"],
            "skill": row[SKILL],
            "rag": row[RAG],
        }
        for row in fault_rows
    ]
    skill_patterns = defaultdict(int)
    rag_patterns = defaultdict(int)
    for run in runs:
        for method, counts in [(SKILL, skill_patterns), (RAG, rag_patterns)]:
            result = run["methods"][method]
            if result["service_correct"] and result["fault_type_correct"]:
                pattern = "Ambos corretos"
            elif result["service_correct"]:
                pattern = "Só serviço correto"
            elif result["fault_type_correct"]:
                pattern = "Só falha correta"
            else:
                pattern = "Nenhum correto"
            counts[pattern] += 1
    pattern_rows = [
        "<tr>"
        f"<th scope='row'>{METHOD_NAMES[method]}</th>"
        + "".join(f"<td>{counts[pattern]}/32 ({pct(counts[pattern], 32):.1f}%)</td>" for pattern in [
            "Ambos corretos",
            "Só serviço correto",
            "Só falha correta",
            "Nenhum correto",
        ])
        + "</tr>"
        for method, counts in [(SKILL, skill_patterns), (RAG, rag_patterns)]
    ]
    relevance = data["diagnosis_relevance"]
    skill_summary = data["diagnosis_summary"][SKILL]
    rag_summary = data["diagnosis_summary"][RAG]
    service_delta = max(
        SERVICE_ORDER,
        key=lambda service: data["by_service"][service]["jointly_correct"]
        - sum(run["methods"][SKILL]["joint_correct"] for run in runs if run["service"] == service),
    )
    service_regression = min(
        SERVICE_ORDER,
        key=lambda service: data["by_service"][service]["jointly_correct"]
        - sum(run["methods"][SKILL]["joint_correct"] for run in runs if run["service"] == service),
    )
    fault_delta = max(
        FAULT_ORDER,
        key=lambda fault: data["by_fault"][fault]["jointly_correct"]
        - sum(run["methods"][SKILL]["joint_correct"] for run in runs if run["fault_type"] == fault),
    )
    fault_regression = min(
        FAULT_ORDER,
        key=lambda fault: data["by_fault"][fault]["jointly_correct"]
        - sum(run["methods"][SKILL]["joint_correct"] for run in runs if run["fault_type"] == fault),
    )

    sections = [
        ("01. Dimensões do diagnóstico", grouped_bars(
            "Acurácia por dimensão — Skill versus RAG",
            accuracy_rows,
            [
                {"key": SKILL, "label": "Skill", "color": METHOD_COLORS[SKILL]},
                {"key": RAG, "label": "RAG", "color": METHOD_COLORS[RAG]},
            ],
        )),
        ("02. Resultado pareado", paired_outcomes_chart(comparison)),
        ("03. Serviço", grouped_bars(
            "Acerto conjunto por serviço",
            service_metric_rows,
            [
                {"key": "skill", "label": "Skill", "color": METHOD_COLORS[SKILL]},
                {"key": "rag", "label": "RAG", "color": METHOD_COLORS[RAG]},
            ],
        )),
        ("04. Tipo de falha", grouped_bars(
            "Acerto conjunto por tipo de falha",
            fault_metric_rows,
            [
                {"key": "skill", "label": "Skill", "color": METHOD_COLORS[SKILL]},
                {"key": "rag", "label": "RAG", "color": METHOD_COLORS[RAG]},
            ],
        )),
        ("05. Elegibilidade por serviço", grouped_bars(
            "Cobertura de par exato e Hit@3 por serviço",
            service_eligibility,
            [
                {"key": "eligible", "label": "Par exato elegível", "color": "#e0a12f"},
                {"key": "hit", "label": "Hit@3", "color": "#386cb0"},
            ],
        )),
        ("06. Elegibilidade por falha", grouped_bars(
            "Cobertura de par exato e Hit@3 por tipo de falha",
            fault_eligibility,
            [
                {"key": "eligible", "label": "Par exato elegível", "color": "#e0a12f"},
                {"key": "hit", "label": "Hit@3", "color": "#386cb0"},
            ],
        )),
        ("07. Resultado condicionado à recuperação", retrieval_chart(relevance)),
        ("08. Combinação serviço × falha", combination_heatmap(runs)),
    ]
    chart_sections = "".join(
        f"<section class='chart-card'><h2>{heading}</h2>{chart}</section>"
        for heading, chart in sections
    )

    skill_seconds = data["performance"][SKILL]["mean_response_duration_ms"] / 1000
    rag_seconds = data["performance"][RAG]["mean_response_duration_ms"] / 1000
    elapsed_ratio = rag_seconds / skill_seconds
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Comparações Skill × RAG — {EVALUATION_ID}</title>
<style>
:root{{color-scheme:light;--ink:#172033;--muted:#586579;--line:#dce2ea;--panel:#fff;--bg:#f4f6fa;--skill:#5966b3;--rag:#168878}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 Arial,sans-serif}}
main{{max-width:1220px;margin:0 auto;padding:32px 22px 56px}}h1{{font-size:30px;margin:0 0 8px}}h2{{font-size:20px;margin:0 0 14px}}h3{{font-size:17px;margin:24px 0 10px}}
p{{margin:8px 0}}.muted{{color:var(--muted)}}.subtitle{{font-size:16px;color:var(--muted)}}
.notice{{border-left:5px solid #e0a12f;background:#fff8e6;padding:14px 18px;margin:20px 0;border-radius:4px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(185px,1fr));gap:12px;margin:20px 0}}
.card,.chart-card,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px}}
.card strong{{display:block;font-size:26px;line-height:1.2}}.card span{{color:var(--muted);font-size:13px}}
.charts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,520px),1fr));gap:14px;margin:16px 0}}
.chart-card{{min-width:0;overflow:hidden}}svg{{display:block;width:100%;height:auto;overflow:visible}}
.panel{{margin:16px 0}}.table-wrap{{overflow-x:auto;margin:8px 0 18px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap}}
thead th{{background:#f0f3f8;color:#354155}}tbody tr:hover{{background:#f8fafc}}
.positive{{color:#087a57;font-weight:700}}.negative{{color:#b54728;font-weight:700}}
.callouts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}}
.callout{{padding:14px;background:#f8fafc;border:1px solid var(--line);border-radius:8px}}
.skill{{color:var(--skill);font-weight:700}}.rag{{color:var(--rag);font-weight:700}}
code{{background:#f0f3f8;padding:2px 5px;border-radius:4px}}footer{{border-top:1px solid var(--line);margin-top:30px;padding-top:16px;color:var(--muted);font-size:13px}}
@media(max-width:640px){{main{{padding:20px 12px}}h1{{font-size:24px}}.chart-card{{padding:12px}}}}
</style>
</head>
<body><main>
<h1>Skill × RAG: onde cada abordagem acerta</h1>
<div class="subtitle">Visualização do relatório <code>{EVALUATION_ID}</code> · 32 incidentes · sem nova inferência</div>
<div class="notice"><strong>Como ler:</strong> “acerto” significa correspondência exata de serviço e/ou tipo de falha com o rótulo do experimento. Não significa confirmação causal. Os 32 diagnósticos foram classificados como inconclusivos; nenhum foi confirmado.</div>
<div class="cards">
<div class="card"><strong>{skill_summary["jointly_correct"]}/32 → {rag_summary["jointly_correct"]}/32</strong><span>Acerto conjunto Skill → RAG ({pct(rag_summary["jointly_correct"], 32)-pct(skill_summary["jointly_correct"], 32):+.1f} pontos percentuais)</span></div>
<div class="card"><strong>{comparison["rag_only_correct"]} vs {comparison["skill_only_correct"]}</strong><span>Casos exclusivos: RAG correta vs. Skill correta</span></div>
<div class="card"><strong>{data["retrieval"]["eligible_exact_pair_cases"]}/32</strong><span>Casos com par exato elegível no corpus ({pct(data["retrieval"]["eligible_exact_pair_cases"], 32):.1f}%)</span></div>
<div class="card"><strong>{data["retrieval"]["hit_at_3_cases"]}/32</strong><span>Casos com par relevante no Top-3 ({pct(data["retrieval"]["hit_at_3_cases"], 32):.1f}%)</span></div>
<div class="card"><strong>{elapsed_ratio:.2f}×</strong><span>Tempo médio RAG/Skill ({rag_seconds:.1f}s vs. {skill_seconds:.1f}s)</span></div>
</div>
<div class="charts">{chart_sections}</div>
<section class="panel">
<h2>Resultados por dimensão</h2>
<div class="table-wrap"><table><thead><tr><th>Método</th><th>Serviço correto</th><th>Falha correta</th><th>Serviço + falha corretos</th><th>Confirmados</th><th>Inconclusivos</th></tr></thead><tbody>
<tr><th class="skill">Skill</th><td>18/32 (56.2%)</td><td>19/32 (59.4%)</td><td>13/32 (40.6%)</td><td>0</td><td>32</td></tr>
<tr><th class="rag">RAG</th><td>21/32 (65.6%)</td><td>22/32 (68.8%)</td><td>16/32 (50.0%)</td><td>0</td><td>32</td></tr>
</tbody></table></div>
<h3>Quando só uma dimensão ficou correta</h3>
<div class="table-wrap"><table><thead><tr><th>Método</th><th>Serviço e falha corretos</th><th>Só serviço correto</th><th>Só falha correta</th><th>Nenhum correto</th></tr></thead><tbody>{''.join(pattern_rows)}</tbody></table></div>
<p class="muted">Ambas as abordagens classificaram os 32 casos, mas uma classificação completa não implica que a causa tenha sido confirmada.</p>
</section>
<section class="panel">
<h2>Por serviço</h2>
{comparison_table(data["by_service"], runs, SERVICE_ORDER, "service")}
<h2>Por tipo de falha</h2>
{comparison_table(data["by_fault"], runs, FAULT_ORDER, "fault_type")}
</section>
<section class="panel">
<h2>Casos em que apenas uma abordagem acertou o par serviço + falha</h2>
<p class="muted">As predições abaixo mostram exatamente onde cada método ganhou ou perdeu. “Par elegível” indica que havia um guia validado para o par; Hit@3 indica se ele foi recuperado.</p>
<h3><span class="rag">Somente RAG correta</span> ({comparison["rag_only_correct"]} casos)</h3>
{case_table(runs, winners="rag")}
<h3><span class="skill">Somente Skill correta</span> ({comparison["skill_only_correct"]} casos)</h3>
{case_table(runs, winners="skill")}
</section>
<section class="panel">
<h2>Diagnóstico correto ou incorreto dado o resultado da recuperação</h2>
<p>Com Hit@3, a RAG acertou conjuntamente em {relevance["with_relevant_retrieval"]["jointly_correct"]}/{relevance["with_relevant_retrieval"]["denominator"]} casos; sem Hit@3, acertou em {relevance["without_relevant_retrieval"]["jointly_correct"]}/{relevance["without_relevant_retrieval"]["denominator"]}. O Hit@3 está associado a maior acerto nesta coorte, mas não prova que a recuperação causou o ganho.</p>
{retrieval_mismatch_table(runs)}
</section>
<section class="panel">
<h2>Onde otimizar primeiro</h2>
<div class="callouts">
<div class="callout"><strong>Cobertura antes de ranking:</strong> há par exato elegível em {data["retrieval"]["eligible_exact_pair_cases"]}/32 casos. Para os outros {32-data["retrieval"]["eligible_exact_pair_cases"]}, reranquear não resolve a ausência do guia. <code>http-status-code</code> não tem par elegível em nenhum dos 7 casos.</div>
<div class="callout"><strong>Discriminar falhas de memória:</strong> Skill acertou {sum(run["methods"][SKILL]["joint_correct"] for run in runs if run["fault_type"] == "memory-hog")}/7 em <code>memory-hog</code> contra {data["by_fault"]["memory-hog"]["jointly_correct"]}/7 da RAG. A RAG localizou o serviço em 6/7, mas classificou o tipo de falha corretamente em apenas 2/7.</div>
<div class="callout"><strong>Investigar serviço em HTTP:</strong> a RAG identificou o tipo <code>http-status-code</code> em 7/7 casos, mas o serviço em apenas 2/7; o acerto conjunto foi 2/7. Priorize desambiguação do serviço e guias elegíveis, não a classificação do tipo.</div>
<div class="callout"><strong>Preservar os ganhos:</strong> maior ganho observado da RAG por tipo foi <code>{fault_delta}</code>; por serviço, <code>{service_delta}</code>. A maior regressão relativa à Skill foi <code>{fault_regression}</code> e <code>{service_regression}</code>, respectivamente.</div>
<div class="callout"><strong>Usar melhor um guia recuperado:</strong> em 5/16 casos com Hit@3, a RAG ainda errou o par conjunto. Inspecione esses casos para ver se o guia era relevante, mas insuficiente ou mal aplicado.</div>
<div class="callout"><strong>Considerar o custo:</strong> nesta execução, a RAG levou em média {rag_seconds:.1f}s, contra {skill_seconds:.1f}s da Skill. Compare custo por ganho antes de aumentar chamadas ou contexto.</div>
</div>
</section>
<section class="panel">
<h2>Limitações</h2>
<ul>
<li>É uma coorte retrospectiva/exploratória já utilizada em avaliações anteriores; não é teste cego independente.</li>
<li>A maioria das combinações serviço × falha tem apenas um incidente. Use o mapa para formular hipóteses, não para declarar uma causa de erro estável.</li>
<li>O diagnóstico é pontuado por serviço/tipo de falha; a avaliação de causalidade exige examinar evidências, alternativas e contradições.</li>
<li>Elegibilidade mede se havia um par exato validado; Hit@3 mede recuperação desse par. São gargalos diferentes.</li>
</ul>
</section>
<footer>Fonte: <code>test_base_rag_evaluation.json</code> e relatório comparativo da avaliação {EVALUATION_ID}. Gráficos gerados localmente a partir dos resultados por incidente e agregados.</footer>
</main></body></html>"""


def main() -> None:
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    if data["evaluation_id"] != EVALUATION_ID or len(data["per_run"]) != 32:
        raise ValueError(f"Unexpected evaluation input: {SOURCE}")
    document = generate(data)
    OUTPUT.write_text(document, encoding="utf-8")
    print(f"Generated {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
