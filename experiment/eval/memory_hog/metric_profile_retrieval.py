"""Quantitative A-only retrieval for exploratory ranking experiments."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from experiment.platform.backend.rca_knowledge import IncidentA, MetricFact

METRIC_PROFILE_VERSION = "metric-profile-gated-rrf-v1"
DEFAULT_SIGNAL_THRESHOLD = 10.0
DEFAULT_NUMERIC_WEIGHT = 0.75
RRF_CONSTANT = 10
PROFILE_METHODS = ("delta", "logdelta", "relative", "positive", "clip")

FeatureKey = tuple[str, str, str, str]
MetricKey = tuple[str, str, str]


@dataclass(frozen=True)
class ProfileCandidate:
    pair_id: str
    source_run_id: str
    incident: IncidentA


def fuse_rankings(
    lexical: list[str], numeric: list[str], numeric_weight: float
) -> list[str]:
    return [
        pair_id
        for pair_id, _ in fuse_rankings_with_scores(lexical, numeric, numeric_weight)
    ]


def fuse_rankings_with_scores(
    lexical: list[str], numeric: list[str], numeric_weight: float
) -> list[tuple[str, float]]:
    if not math.isfinite(numeric_weight) or not 0 <= numeric_weight <= 1:
        raise ValueError("numeric_weight must be finite and between zero and one")
    scores: dict[str, float] = {}
    for ranking, weight in ((lexical, 1 - numeric_weight), (numeric, numeric_weight)):
        if len(ranking) != len(set(ranking)):
            raise ValueError("Rankings must not contain duplicate pair IDs")
        if weight == 0:
            continue
        for rank, pair_id in enumerate(ranking, 1):
            scores[pair_id] = scores.get(pair_id, 0) + weight / (RRF_CONSTANT + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


class MetricProfileIndex:
    """Fit scales on corpus A only; never consume B or evaluation labels."""

    def __init__(self, candidates: list[ProfileCandidate]) -> None:
        if not candidates:
            raise ValueError("Metric profiles require a non-empty corpus")
        pair_ids = [candidate.pair_id for candidate in candidates]
        if len(pair_ids) != len(set(pair_ids)) or any(
            not c.pair_id or not c.source_run_id for c in candidates
        ):
            raise ValueError(
                "Metric profile candidates need unique IDs and source runs"
            )
        self.candidates = candidates
        features: set[FeatureKey] = set()
        deltas: dict[MetricKey, list[float]] = defaultdict(list)
        baselines: dict[MetricKey, list[float]] = defaultdict(list)
        for candidate in candidates:
            for fact in self._facts(candidate.incident).values():
                features.add((fact.service, fact.metric, fact.unit, fact.statistic))
                group = (fact.metric, fact.unit, fact.statistic)
                if fact.delta != 0:
                    deltas[group].append(abs(fact.delta))
                if fact.baseline_value > 0:
                    baselines[group].append(fact.baseline_value)
        self.features = sorted(features)
        self.scales = {
            group: float(np.median(values)) for group, values in deltas.items()
        }
        self.floors = {
            group: float(np.percentile(values, 25))
            for group, values in baselines.items()
        }
        if any(
            not math.isfinite(value) or value <= 0
            for value in (*self.scales.values(), *self.floors.values())
        ):
            raise ValueError("Corpus metric scales must be finite and positive")
        self.matrices = {
            method: np.asarray(
                [self.profile(c.incident, method) for c in candidates], dtype=float
            )
            for method in PROFILE_METHODS
        }

    @staticmethod
    def _facts(incident: IncidentA) -> dict[FeatureKey, MetricFact]:
        facts: dict[FeatureKey, MetricFact] = {}
        for fact in incident.metric_facts:
            key = (fact.service, fact.metric, fact.unit, fact.statistic)
            if key in facts:
                raise ValueError(f"Duplicate metric profile fact: {key}")
            if not all(
                math.isfinite(value)
                for value in (fact.baseline_value, fact.fault_value, fact.delta)
            ):
                raise ValueError(f"Non-finite metric profile fact: {key}")
            facts[key] = fact
        return facts

    def profile(self, incident: IncidentA, method: str) -> np.ndarray:
        if method not in PROFILE_METHODS:
            raise ValueError(f"Unsupported metric profile method: {method}")
        facts = self._facts(incident)
        values = []
        for key in self.features:
            fact = facts.get(key)
            scale = self.scales.get(key[1:])
            # Zero-filling is a similarity approximation, not an absence assertion.
            if fact is None or scale is None:
                values.append(0.0)
                continue
            delta = fact.delta / scale
            if method == "logdelta":
                value = math.copysign(math.log1p(abs(delta)), delta)
            elif method == "relative":
                floor = self.floors.get(key[1:], scale)
                value = fact.delta / max(abs(fact.baseline_value), floor)
            elif method == "positive":
                value = max(delta, 0.0)
            elif method == "clip":
                value = max(-10.0, min(10.0, delta))
            else:
                value = delta
            values.append(value)
        vector = np.asarray(values, dtype=float)
        if not np.all(np.isfinite(vector)):
            raise ValueError("Non-finite normalized metric profile")
        if vector.size == 0 or not np.any(vector):
            return vector
        vector /= float(np.max(np.abs(vector)))
        norm = float(np.linalg.norm(vector))
        return vector / norm

    def strength(self, incident: IncidentA) -> float:
        strength = max(
            (
                max(fact.delta, 0.0) / self.scales[key[1:]]
                for key, fact in self._facts(incident).items()
                if key in self.features and key[1:] in self.scales
            ),
            default=0.0,
        )
        if not math.isfinite(strength):
            raise ValueError("Non-finite metric signal strength")
        return strength

    def rank(
        self,
        incident: IncidentA,
        *,
        source_run_id: str,
        allowed_pair_ids: set[str],
        method: str = "positive",
    ) -> list[str]:
        unknown = allowed_pair_ids - {c.pair_id for c in self.candidates}
        if unknown:
            raise ValueError("Allowed metric candidates are not in the fitted corpus")
        vector = self.profile(incident, method)
        if not np.any(vector):
            return []
        similarities = self.matrices[method] @ vector
        indices = [
            i
            for i, candidate in enumerate(self.candidates)
            if candidate.pair_id in allowed_pair_ids
            and candidate.source_run_id != source_run_id
            and np.any(self.matrices[method][i])
        ]
        indices.sort(key=lambda i: (-similarities[i], self.candidates[i].pair_id))
        return [self.candidates[i].pair_id for i in indices]

    def rerank(
        self,
        lexical: list[str],
        incident: IncidentA,
        *,
        source_run_id: str,
        allowed_pair_ids: set[str],
        threshold: float = DEFAULT_SIGNAL_THRESHOLD,
        numeric_weight: float = DEFAULT_NUMERIC_WEIGHT,
    ) -> list[str]:
        if not math.isfinite(threshold) or threshold < 0:
            raise ValueError("Signal threshold must be finite and non-negative")
        if not math.isfinite(numeric_weight) or not 0 <= numeric_weight <= 1:
            raise ValueError("numeric_weight must be finite and between zero and one")
        if allowed_pair_ids - {c.pair_id for c in self.candidates}:
            raise ValueError("Allowed metric candidates are not in the fitted corpus")
        if len(lexical) != len(set(lexical)):
            raise ValueError("Rankings must not contain duplicate pair IDs")
        allowed = {
            c.pair_id
            for c in self.candidates
            if c.pair_id in allowed_pair_ids and c.source_run_id != source_run_id
        }
        if not set(lexical) <= allowed:
            raise ValueError(
                "Lexical ranking violates candidate eligibility or exclusion"
            )
        if self.strength(incident) < threshold:
            return list(lexical)
        numeric = self.rank(
            incident, source_run_id=source_run_id, allowed_pair_ids=allowed
        )
        if not numeric:
            return list(lexical)
        return fuse_rankings(lexical, numeric, numeric_weight)
