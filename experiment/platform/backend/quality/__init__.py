"""Run quality gates — checklist de validação por experimento.

Valida, por run, que todas as etapas do pipeline produziram o que
deveriam. Nasceu da constatação de bugs silenciosos no histórico
(ex.: ``traces_raw`` vazio em 193/193 runs sem nenhum alerta).

Componentes:

* :mod:`gates` — catálogo de gates; cada gate é uma função pura
  ``(RunBundle) -> GateResult``.
* :mod:`runner` — executa o catálogo sobre um bundle e produz o
  ``run_quality.json`` (usable + gates + gaps).
* :mod:`report` — consolida timeline + artefatos + quality no
  ``run_health.json`` (o documento único "o que aconteceu neste run").

O mesmo catálogo roda online (engine, ao fim do run) e offline
(retroativo sobre runs históricos, via ``experiment.eval.gat.checklist``).
"""
from .gates import GATES, GateResult, RunBundle
from .runner import run_gates
from .report import build_run_health

__all__ = [
    "GATES",
    "GateResult",
    "RunBundle",
    "run_gates",
    "build_run_health",
]
