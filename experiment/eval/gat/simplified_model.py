"""GATv2 minimalista para a ablação de topologia e capacidade."""
from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.data import Data
from torch_geometric.nn import GATv2Conv

from experiment.eval.gat.model import EDGE_DIM, NODE_DIM


class SimplifiedGATLocalizer(nn.Module):
    """Uma camada GATv2 com residual local e um score por nó."""

    def __init__(
        self,
        node_dim: int = NODE_DIM,
        edge_dim: int = EDGE_DIM,
        hidden: int = 8,
        heads: int = 1,
        dropout: float = 0.1,
    ):
        super().__init__()
        self.conv = GATv2Conv(
            node_dim,
            hidden,
            heads=heads,
            edge_dim=edge_dim,
            dropout=dropout,
        )
        self.score = nn.Linear(node_dim + hidden * heads, 1)
        self.dropout = dropout

    def forward(self, data: Data) -> torch.Tensor:
        x = data.x
        message = F.elu(self.conv(x, data.edge_index, data.edge_attr))
        message = F.dropout(message, p=self.dropout, training=self.training)
        return self.score(torch.cat((x, message), dim=-1)).squeeze(-1)
