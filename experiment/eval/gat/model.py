"""Modelo GAT para localização de falha (WHERE) e conversão grafo→PyG.

Arquitetura (H001, decisões de 2026-09-19):

* Entrada por nó: 3 golden metrics em log-ratio **imputadas** (None→0) +
  **máscara binária** por métrica (1=observado, 0=imputado) + span_count.
  A máscara é essencial: 69% dos ``error_rate`` são None; sem ela, o modelo
  não distingue "sem mudança" de "sem dado".
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


def _feat_and_mask(value: Optional[float]) -> Tuple[float, float]:
    """Imputa None→0.0 e retorna (valor, máscara). Sanitiza NaN/Inf."""
    if value is None:
        return 0.0, 0.0
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        return 0.0, 0.0
    return v, 1.0


def graph_to_pyg(graph: Dict) -> Data:
    """Converte o ``gat_graph.json`` num ``torch_geometric.data.Data``.

    Node features: [m1, m2, m3, mask1, mask2, mask3, log1p(span_count)].
    Edge features: [e1, e2, e3, emask1, emask2, emask3, direction_flag].
    """
    nodes = sorted(graph["nodes"], key=lambda n: n["idx"])
    x_rows: List[List[float]] = []
    for node in nodes:
        feats = node["features"]
        row: List[float] = []
        masks: List[float] = []
        for k in NODE_METRIC_KEYS:
            v, m = _feat_and_mask(feats.get(k))
            row.append(v)
            masks.append(m)
        span = feats.get("span_count") or 0.0
        row = row + masks + [float(np.log1p(max(0.0, span)))]
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
            v, m = _feat_and_mask(feats.get(k))
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
