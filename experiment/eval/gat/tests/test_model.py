"""Testes de sanidade do modelo GAT: conversão PyG, shapes, overfit."""
from __future__ import annotations

import numpy as np
import torch

from experiment.eval.gat.model import (
    EDGE_DIM,
    NODE_DIM,
    GATLocalizer,
    graph_to_pyg,
    set_seed,
)


def _toy_graph(label_idx: int = 1):
    """Grafo 3-nós: nó `label_idx` claramente anômalo."""
    nodes = []
    for i in range(3):
        anomaly = 5.0 if i == label_idx else 0.0
        nodes.append({
            "idx": i,
            "service": f"svc{i}",
            "features": {
                "traffic_lr": anomaly,
                "latency_p95_lr": anomaly if i == label_idx else None,
                "error_rate_lr": None,
                "span_count": 100.0,
            },
        })
    edges = [
        {"src": 0, "dst": 1, "direction": "real", "features": {}},
        {"src": 1, "dst": 0, "direction": "reverse", "features": {}},
        {"src": 1, "dst": 2, "direction": "real", "features": {}},
        {"src": 2, "dst": 1, "direction": "reverse", "features": {}},
    ]
    return {
        "run_id": f"toy-{label_idx}",
        "nodes": nodes,
        "edges": edges,
        "label": {"root_service_idx": label_idx},
    }


def test_graph_to_pyg_shapes():
    data = graph_to_pyg(_toy_graph())
    assert data.x.shape == (3, NODE_DIM)
    assert data.edge_index.shape == (2, 4)
    assert data.edge_attr.shape == (4, EDGE_DIM)
    assert int(data.y) == 1


def test_graph_to_pyg_imputes_and_masks():
    data = graph_to_pyg(_toy_graph(label_idx=1))
    # nó 0: traffic=0 (não anômalo), latency None → imputado 0, mask 0
    x0 = data.x[0]
    # ordem: [traffic, latency, error, mask_traffic, mask_latency, mask_error, log_span]
    assert x0[1].item() == 0.0  # latency imputado
    assert x0[4].item() == 0.0  # mask latency = 0 (era None)
    # nó 1: latency observado → mask 1
    x1 = data.x[1]
    assert x1[4].item() == 1.0


def test_model_forward_shape():
    set_seed(0)
    model = GATLocalizer()
    data = graph_to_pyg(_toy_graph())
    out = model(data)
    assert out.shape == (3,)


def test_model_overfits_single_graph():
    """Sanity: o modelo deve conseguir memorizar um grafo trivial (loss→0)."""
    import torch.nn.functional as F

    set_seed(0)
    model = GATLocalizer(dropout=0.0)
    opt = torch.optim.Adam(model.parameters(), lr=0.05)
    data = graph_to_pyg(_toy_graph(label_idx=1))
    for _ in range(200):
        model.train()
        opt.zero_grad()
        scores = model(data).unsqueeze(0)
        loss = F.cross_entropy(scores, data.y)
        loss.backward()
        opt.step()
    model.eval()
    with torch.no_grad():
        pred = int(torch.argmax(model(data)))
    assert pred == 1  # aprendeu a apontar o nó anômalo


def test_model_no_nan_with_missing_features():
    """Grafo com muitas features None não deve produzir NaN."""
    set_seed(0)
    g = _toy_graph()
    for n in g["nodes"]:
        n["features"] = {"traffic_lr": None, "latency_p95_lr": None, "error_rate_lr": None, "span_count": 0.0}
    model = GATLocalizer()
    out = model(graph_to_pyg(g))
    assert not torch.isnan(out).any()
