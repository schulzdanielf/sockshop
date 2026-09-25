"""Modelo GAT para localização de falha (WHERE) e conversão grafo→PyG.

Arquitetura (H001, decisões de 2026-09-19):

* Entrada por nó: 3 golden metrics em log-ratio **imputadas** (None→0) +
  **máscara binária** por métrica (1=observado, 0=imputado) + span_count.
  A máscara é essencial: 69% dos ``error_rate`` são None; sem ela, o modelo
  não distingue "sem mudança" de "sem dado".
* **Padronização (z-score)** dos valores observados (revisão 2026-09-22):
  as 3 golden metrics de nó/aresta têm escalas naturais bem diferentes
  entre si; sem padronizar, a métrica de maior magnitude domina o
  gradiente. As estatísticas (média/desvio) são ajustadas **apenas no
  conjunto de treino** (``compute_feature_stats``) e reaplicadas em
  val/test — nunca ajustadas com dados de teste. Valores imputados
  (mask=0) continuam em 0.0 (o canal de máscara já sinaliza "sem dado";
  padronizar o placeholder o transformaria num valor não-nulo arbitrário).
* GATv2 (2 camadas, multi-head) com ``edge_dim`` para edge features.
* Saída = 1 score por nó → softmax sobre nós → cross-entropy no nó raiz.
  Avaliação por top-k do ranking (não muda o treino).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.data import Data
from torch_geometric.nn import GATv2Conv

# Golden metrics de nó (log-ratio) na ordem canônica.
NODE_METRIC_KEYS = ("traffic_lr", "latency_p95_lr", "error_rate_lr")
# Edge features (log-ratio) na ordem canônica.
EDGE_METRIC_KEYS = ("req_rate_lr", "latency_lr", "error_lr")

# Dim de nó: 3 métricas + 3 máscaras + span_count(log1p) = 7
NODE_DIM = len(NODE_METRIC_KEYS) * 2 + 1
# Dim de aresta: 3 métricas + 3 máscaras + flag de direção = 7
EDGE_DIM = len(EDGE_METRIC_KEYS) * 2 + 1

# Estatísticas de padronização: {feature_key: (mean, std)}.
FeatureStats = Dict[str, Tuple[float, float]]


def compute_feature_stats(graphs: List[Dict]) -> FeatureStats:
    """Ajusta média/desvio por feature usando só valores OBSERVADOS (não-None).

    Chamar apenas com grafos de treino (nunca val/test) para não vazar
    informação de avaliação para dentro da normalização. ``span_count``
    entra como ``log1p`` (mesma transformação aplicada em ``graph_to_pyg``).
    Features sem nenhum valor observado ficam com (mean=0.0, std=1.0) —
    equivalente a não padronizar (seguro, evita divisão por zero).
    """
    samples: Dict[str, List[float]] = {k: [] for k in NODE_METRIC_KEYS + EDGE_METRIC_KEYS}
    span_samples: List[float] = []

    for g in graphs:
        for node in g.get("nodes", []):
            feats = node.get("features") or {}
            for k in NODE_METRIC_KEYS:
                v = feats.get(k)
                if v is not None and v == v and v not in (float("inf"), float("-inf")):
                    samples[k].append(float(v))
            span = feats.get("span_count")
            if span is not None:
                span_samples.append(float(np.log1p(max(0.0, span))))
        for e in g.get("edges", []):
            feats = e.get("features") or {}
            for k in EDGE_METRIC_KEYS:
                v = feats.get(k)
                if v is not None and v == v and v not in (float("inf"), float("-inf")):
                    samples[k].append(float(v))

    stats: FeatureStats = {}
    for k, vals in samples.items():
        if len(vals) >= 2:
            arr = np.asarray(vals, dtype=float)
            std = float(arr.std())
            stats[k] = (float(arr.mean()), std if std > 1e-8 else 1.0)
        else:
            stats[k] = (0.0, 1.0)
    if len(span_samples) >= 2:
        arr = np.asarray(span_samples, dtype=float)
        std = float(arr.std())
        stats["span_count"] = (float(arr.mean()), std if std > 1e-8 else 1.0)
    else:
        stats["span_count"] = (0.0, 1.0)
    return stats


def _feat_and_mask(value: Optional[float], stats: Optional[Tuple[float, float]] = None) -> Tuple[float, float]:
    """Imputa None→0.0 e retorna (valor, máscara). Sanitiza NaN/Inf.

    Se ``stats`` (mean, std) for passado, padroniza o valor OBSERVADO
    (z-score); valores imputados continuam 0.0.
    """
    if value is None:
        return 0.0, 0.0
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        return 0.0, 0.0
    if stats is not None:
        mean, std = stats
        v = (v - mean) / std
    return v, 1.0


def graph_to_pyg(graph: Dict, stats: Optional[FeatureStats] = None) -> Data:
    """Converte o ``gat_graph.json`` num ``torch_geometric.data.Data``.

    Node features: [m1, m2, m3, mask1, mask2, mask3, log1p(span_count)].
    Edge features: [e1, e2, e3, emask1, emask2, emask3, direction_flag].

    ``stats`` (opcional): saída de ``compute_feature_stats`` sobre o
    conjunto de treino — padroniza valores observados antes de empacotar.
    """
    nodes = sorted(graph["nodes"], key=lambda n: n["idx"])
    x_rows: List[List[float]] = []
    for node in nodes:
        feats = node["features"]
        row: List[float] = []
        masks: List[float] = []
        for k in NODE_METRIC_KEYS:
            v, m = _feat_and_mask(feats.get(k), stats.get(k) if stats else None)
            row.append(v)
            masks.append(m)
        span = feats.get("span_count") or 0.0
        span_log = float(np.log1p(max(0.0, span)))
        if stats is not None:
            mean, std = stats["span_count"]
            span_log = (span_log - mean) / std
        row = row + masks + [span_log]
        x_rows.append(row)
    x = torch.tensor(x_rows, dtype=torch.float)

    src: List[int] = []
    dst: List[int] = []
    edge_rows: List[List[float]] = []
    for e in graph["edges"]:
        src.append(e["src"])
        dst.append(e["dst"])
        feats = e.get("features") or {}
        row: List[float] = []
        masks: List[float] = []
        for k in EDGE_METRIC_KEYS:
            v, m = _feat_and_mask(feats.get(k), stats.get(k) if stats else None)
            row.append(v)
            masks.append(m)
        direction_flag = 1.0 if e.get("direction") == "real" else 0.0
        edge_rows.append(row + masks + [direction_flag])
    edge_index = torch.tensor([src, dst], dtype=torch.long)
    edge_attr = torch.tensor(edge_rows, dtype=torch.float) if edge_rows else torch.zeros((0, EDGE_DIM))

    label_idx = (graph.get("label") or {}).get("root_service_idx")
    y = torch.tensor([label_idx if label_idx is not None else -1], dtype=torch.long)

    data = Data(x=x, edge_index=edge_index, edge_attr=edge_attr, y=y)
    data.run_id = graph.get("run_id")
    return data


class GATLocalizer(nn.Module):
    """GATv2 de 2 camadas → 1 score por nó."""

    def __init__(
        self,
        node_dim: int = NODE_DIM,
        edge_dim: int = EDGE_DIM,
        hidden: int = 16,
        heads: int = 4,
        dropout: float = 0.3,
    ):
        super().__init__()
        self.conv1 = GATv2Conv(node_dim, hidden, heads=heads, edge_dim=edge_dim, dropout=dropout)
        self.conv2 = GATv2Conv(hidden * heads, hidden, heads=1, edge_dim=edge_dim, dropout=dropout)
        self.score = nn.Linear(hidden, 1)
        self.dropout = dropout

    def forward(self, data: Data) -> torch.Tensor:
        x, edge_index, edge_attr = data.x, data.edge_index, data.edge_attr
        x = F.elu(self.conv1(x, edge_index, edge_attr))
        x = F.dropout(x, p=self.dropout, training=self.training)
        x = F.elu(self.conv2(x, edge_index, edge_attr))
        return self.score(x).squeeze(-1)  # [n_nodes]


def set_seed(seed: int) -> None:
    import random

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def get_device() -> torch.device:
    """Device de treino: GPU (CUDA) se disponível, senão CPU.

    Fallback automático — nada a instalar nem configurar; em máquina sem
    GPU o comportamento é idêntico ao anterior (CPU)."""
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")
