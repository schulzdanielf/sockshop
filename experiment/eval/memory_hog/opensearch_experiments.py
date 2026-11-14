"""Local OpenSearch index for frozen exploratory RCA experiment pairs."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import numpy as np

from experiment.eval.memory_hog.metric_profile_retrieval import (
    DEFAULT_NUMERIC_WEIGHT,
    DEFAULT_SIGNAL_THRESHOLD,
    METRIC_PROFILE_VERSION,
    RRF_CONSTANT as METRIC_RRF_CONSTANT,
    MetricProfileIndex,
    ProfileCandidate,
    fuse_rankings_with_scores,
)
from experiment.platform.backend.rca_knowledge import (
    A_TEXT_VERSION,
    IncidentA,
    OPENSEARCH_RETRIEVER_V1,
    OPENSEARCH_RETRIEVER_VERSION,
    OPENSEARCH_RETRIEVER_V3,
    OPENSEARCH_RETRIEVER_V4,
    OPENSEARCH_RETRIEVER_V5,
    OPENSEARCH_RETRIEVER_V6,
    QWEN35_MODEL_ID,
    TRAINING_B_SCHEMA_VERSION,
    TRAINING_B_PROCEDURAL_SCHEMA_VERSION,
    compact_exploratory_history,
    digest,
    procedural_metric_step_matches_target,
)

DEFAULT_OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200")
_INDEX_SCHEMA_VERSIONS = {
    OPENSEARCH_RETRIEVER_V1: 1,
    OPENSEARCH_RETRIEVER_VERSION: 2,
    OPENSEARCH_RETRIEVER_V3: 3,
    OPENSEARCH_RETRIEVER_V4: 4,
    OPENSEARCH_RETRIEVER_V5: 4,
    OPENSEARCH_RETRIEVER_V6: 4,
}
_RRF_CONSTANT = 60
_LEXICAL_FIELDS = [
    "a_services",
    "a_service_relations",
    "a_symptoms",
    "a_metric_facts",
    "a_log_patterns",
    "a_trace_patterns",
    "a_temporal_sequence",
    "a_limitations",
]
_MAX_LEXICAL_TERMS_PER_FIELD = 24
_LEXICAL_TOKEN = re.compile(r"\w+", re.UNICODE)
_ERROR_CODE = re.compile(
    r"\b(?:http(?:\s+status)?|status(?:\s+code)?|"
    r"error(?:\s+code)?|code)\s*[:=]?\s*([1-5]\d{2})\b",
    re.IGNORECASE,
)
_QUERY_STOPWORDS = set(
    "the a an and or to of in on for by is are was were with from during phase "
    "fault baseline service metric unit statistic delta direction signal increase "
    "decrease unchanged s pct ms percent value mean p95 request rate error latency "
    "saturation memory cpu network symptom symptoms logs traces observed shows showed "
    "significant de no na para por como pelo pela durante fase evidencias evidencia "
    "aumento reducao da do dos das e em".split()
)
_FIELD_BOOSTS = {
    "a_services": 1.5,
    "a_service_relations": 1.0,
    "a_symptoms": 1.0,
    "a_metric_facts": 1.25,
    "a_log_patterns": 2.5,
    "a_trace_patterns": 2.0,
    "a_temporal_sequence": 1.0,
    "a_limitations": 0.2,
}


def _informative_query_fields(
    incident: IncidentA, corpus_pairs: list[dict[str, Any]]
) -> dict[str, list[str]]:
    if not corpus_pairs:
        raise OpenSearchError("BM25 IDF retrieval requires a non-empty A corpus")
    document_frequencies = {field: {} for field in _LEXICAL_FIELDS}
    for pair in corpus_pairs:
        try:
            corpus_incident = IncidentA.model_validate(pair["a"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OpenSearchError(
                "BM25 IDF corpus contains an invalid incident A"
            ) from exc
        fields = corpus_incident.retrieval_fields()
        for field in _LEXICAL_FIELDS:
            tokens = {
                term.casefold()
                for value in fields.get(field, [])
                for term in _LEXICAL_TOKEN.findall(value)
            }
            for term in tokens:
                document_frequencies[field][term] = (
                    document_frequencies[field].get(term, 0) + 1
                )

    fields = incident.retrieval_fields()
    output: dict[str, list[str]] = {}
    document_count = len(corpus_pairs)
    for field in _LEXICAL_FIELDS:
        values = fields.get(field, [])
        if field == "a_metric_facts":
            raw_terms = []
            for fact in incident.metric_facts:
                direction = (
                    "increase"
                    if fact.delta > 0
                    else "decrease" if fact.delta < 0 else "unchanged"
                )
                raw_terms.extend((fact.service, fact.metric, direction))
        else:
            raw_terms = [
                term.casefold()
                for value in values
                for term in _LEXICAL_TOKEN.findall(value)
            ]

        codes = []
        if field in {"a_log_patterns", "a_trace_patterns", "a_symptoms"}:
            codes = list(
                dict.fromkeys(
                    code
                    for value in values
                    for code in _ERROR_CODE.findall(value)
                )
            )
        ranked_terms = []
        for position, term in enumerate(dict.fromkeys(raw_terms)):
            if (
                term in _QUERY_STOPWORDS
                or term.isdecimal()
                or len(term) < 2
            ):
                continue
            frequency = document_frequencies[field].get(term, 0)
            inverse_frequency = math.log(
                1
                + (document_count - frequency + 0.5)
                / (frequency + 0.5)
            )
            if "_" in term or "-" in term or any(char.isdigit() for char in term):
                inverse_frequency += 2.0
            ranked_terms.append((inverse_frequency, position, term))
        ranked_terms.sort(key=lambda item: (-item[0], item[1]))
        code_terms = [code for code in codes if code not in _QUERY_STOPWORDS]
        selected = [term for _, _, term in ranked_terms[:_MAX_LEXICAL_TERMS_PER_FIELD]]
        selected.extend(code_terms)
        selected = list(dict.fromkeys(selected))
        if selected:
            output[field] = selected
    return output


def _bounded_field_queries(
    query_fields: dict[str, list[str]],
) -> list[dict[str, Any]]:
    queries = []
    for field in _LEXICAL_FIELDS:
        values = query_fields.get(field, [])
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise OpenSearchError(f"Invalid lexical query field: {field}")
        if not values:
            continue
        per_value_limit = max(
            1, math.ceil(_MAX_LEXICAL_TERMS_PER_FIELD / len(values))
        )
        terms = []
        for value in values:
            value_terms = _LEXICAL_TOKEN.findall(value)
            remaining = _MAX_LEXICAL_TERMS_PER_FIELD - len(terms)
            if remaining <= 0:
                break
            limit = min(per_value_limit, remaining)
            if len(value_terms) > limit:
                head = limit // 2
                tail = limit - head
                value_terms = value_terms[:head] + (
                    value_terms[-tail:] if tail else []
                )
            terms.extend(value_terms)
        terms = list(dict.fromkeys(terms))[:_MAX_LEXICAL_TERMS_PER_FIELD]
        if terms:
            queries.append({"match": {field: {"query": " ".join(terms)}}})
    return queries


class OpenSearchError(RuntimeError):
    """Raised when the local OpenSearch index cannot be used safely."""


class OpenSearchExperimentIndex:
    def __init__(
        self,
        url: str,
        base_sha256: str,
        *,
        retriever_version: str = OPENSEARCH_RETRIEVER_VERSION,
    ) -> None:
        parsed = urlparse(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise OpenSearchError(
                "OpenSearch evaluation must use a credential-free loopback URL"
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise OpenSearchError("Invalid OpenSearch URL port") from exc
        if not re.fullmatch(r"[a-f0-9]{64}", base_sha256):
            raise OpenSearchError("Invalid frozen knowledge-base SHA-256")
        if retriever_version not in _INDEX_SCHEMA_VERSIONS:
            raise OpenSearchError("Unsupported OpenSearch retriever version")
        self.base_url = f"http://{parsed.hostname}"
        if port is not None:
            self.base_url += f":{port}"
        self.base_sha256 = base_sha256
        self.retriever_version = retriever_version
        self.schema_version = _INDEX_SCHEMA_VERSIONS[retriever_version]
        self.index_name = (
            f"sockshop-rca-experiments-v{self.schema_version}-{base_sha256[:16]}"
        )
        self.document_count = 0
        self.server_version = ""
        self._index_metadata: dict[str, Any] | None = None
        self.search_metadata: dict[str, Any] | None = None

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | bytes | None = None,
        *,
        content_type: str = "application/json",
        allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        if isinstance(payload, bytes):
            body = payload
        elif payload is not None:
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        else:
            body = None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type
        request = Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=20) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if allow_not_found and exc.code == 404:
                return None
            raise OpenSearchError(
                f"OpenSearch {method} {path} returned HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise OpenSearchError(
                f"Cannot reach local OpenSearch at {self.base_url}: {exc.reason}"
            ) from exc
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OpenSearchError(
                f"OpenSearch {method} {path} returned invalid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise OpenSearchError(
                f"OpenSearch {method} {path} returned a non-object response"
            )
        return value

    def index_base(
        self, base: dict[str, Any], *, a_text_version: str
    ) -> dict[str, Any]:
        if base.get("sha256") != self.base_sha256:
            raise OpenSearchError("OpenSearch index does not match the frozen base")
        if self.schema_version >= 3 and digest(
            {key: value for key, value in base.items() if key != "sha256"}
        ) != self.base_sha256:
            raise OpenSearchError("Qwen 3.5 B base checksum does not match its contents")
        pairs = base.get("pairs")
        vectors = base.get("index")
        embedding_provider = base.get("embedding_provider")
        embedding_dim = base.get("embedding_dim")
        if (
            not isinstance(pairs, list)
            or not pairs
            or not isinstance(vectors, dict)
            or not isinstance(embedding_provider, str)
            or not isinstance(embedding_dim, int)
            or embedding_dim < 1
        ):
            raise OpenSearchError("Frozen base has invalid index metadata")
        root = self._request("GET", "/")
        version = ((root or {}).get("version") or {}).get("number")
        if not isinstance(version, str) or not version:
            raise OpenSearchError(
                "OpenSearch version is missing from its root response"
            )
        self.server_version = version
        metadata = {
            "schema_version": self.schema_version,
            "base_sha256": self.base_sha256,
            "a_text_version": a_text_version,
            "embedding_provider": embedding_provider,
            "embedding_dim": embedding_dim,
            "document_count": len(pairs),
        }
        if self.schema_version in {3, 4}:
            expected_b_version = (
                TRAINING_B_SCHEMA_VERSION
                if self.schema_version == 3
                else TRAINING_B_PROCEDURAL_SCHEMA_VERSION
            )
            if (
                base.get("b_rebuild_version") != expected_b_version
                or base.get("model") != QWEN35_MODEL_ID
            ):
                raise OpenSearchError(
                    "v3 index requires the frozen Qwen 3.5 B guide corpus"
                )
            metadata["b_rebuild_version"] = TRAINING_B_SCHEMA_VERSION
            metadata["model"] = QWEN35_MODEL_ID
            if self.schema_version == 4:
                metadata["b_rebuild_version"] = TRAINING_B_PROCEDURAL_SCHEMA_VERSION
        existing = self._request("GET", f"/{self.index_name}", allow_not_found=True)
        if existing is None:
            properties = {
                "pair_id": {"type": "keyword"},
                "source_run_id": {"type": "keyword"},
                "effect_validated": {"type": "boolean"},
                "guide_status": {"type": "keyword"},
                "causal_status": {"type": "keyword"},
                "a_text": {"type": "text"},
                "a_vector": {
                    "type": "knn_vector",
                    "dimension": embedding_dim,
                    "method": {
                        "name": "hnsw",
                        "engine": "lucene",
                        "space_type": "cosinesimil",
                        "parameters": {
                            "ef_construction": 128,
                            "m": 16,
                        },
                    },
                },
                "history": {"type": "object", "enabled": False},
                "source_document_sha256": {"type": "keyword"},
            }
            if self.schema_version >= 2:
                properties.update(
                    {
                        field: {"type": "text"}
                        for field in _LEXICAL_FIELDS
                    }
                )
            if self.schema_version == 4:
                properties["b_procedural_status"] = {"type": "keyword"}
                properties["review_status"] = {"type": "keyword"}
            created = self._request(
                "PUT",
                f"/{self.index_name}",
                {
                    "settings": {
                        "index": {
                            "knn": True,
                            "number_of_shards": 1,
                            "number_of_replicas": 0,
                        }
                    },
                    "mappings": {
                        "_meta": metadata,
                        "properties": properties,
                    },
                },
            )
            if not (created or {}).get("acknowledged"):
                raise OpenSearchError("OpenSearch did not acknowledge index creation")
        else:
            actual_metadata = (
                (existing.get(self.index_name) or {}).get("mappings") or {}
            ).get("_meta")
            if actual_metadata != metadata:
                raise OpenSearchError(
                    "Existing OpenSearch index metadata differs from the frozen base"
                )

        seen: set[str] = set()
        lines: list[str] = []
        for pair in pairs:
            pair_id = pair.get("pair_id")
            source_run_id = pair.get("source_run_id")
            if not isinstance(pair_id, str) or not pair_id or pair_id in seen:
                raise OpenSearchError("Frozen base has a missing or duplicate pair ID")
            seen.add(pair_id)
            if not isinstance(source_run_id, str) or not source_run_id:
                raise OpenSearchError(f"Missing source run ID for pair {pair_id}")
            expected_states = (
                {
                    ("validated_success", "supported"),
                    ("insufficient_evidence", "inconclusive"),
                    ("effect_unvalidated", "inconclusive"),
                }
                if self.schema_version == 3
                else (
                    {
                        ("procedurally_validated", "not_assessed"),
                        ("procedural_pending", "not_assessed"),
                    }
                    if self.schema_version == 4
                    else {("pending", "inconclusive")}
                )
            )
            if (
                (pair.get("guide_status"), pair.get("causal_status"))
                not in expected_states
                or not isinstance(pair.get("effect_validated"), bool)
                or (
                    self.schema_version == 3
                    and pair.get("b_rebuild_version") != TRAINING_B_SCHEMA_VERSION
                )
                or (
                    self.schema_version == 4
                    and pair.get("b_rebuild_version")
                    != TRAINING_B_PROCEDURAL_SCHEMA_VERSION
                )
                or (
                    self.schema_version == 3
                    and pair.get("guide_status") == "validated_success"
                    and pair.get("effect_validated") is not True
                )
            ):
                raise OpenSearchError(f"Unexpected validation state for pair {pair_id}")
            if self.schema_version == 3:
                diagnosis = (pair.get("b") or {}).get("diagnosis") or {}
                labels = pair.get("labels") or {}
                gate_reasons = pair.get("b_rebuild_gate_reasons")
                if (
                    pair.get("sha256")
                    != digest(
                        {
                            key: value
                            for key, value in pair.items()
                            if key != "sha256"
                        }
                    )
                    or not isinstance(gate_reasons, list)
                    or pair.get("b_rebuild_version") != TRAINING_B_SCHEMA_VERSION
                    or (pair.get("b") or {}).get("rebuild_version")
                    != TRAINING_B_SCHEMA_VERSION
                ):
                    raise OpenSearchError(
                        f"Rebuilt B provenance is invalid for pair {pair_id}"
                    )
                if pair["guide_status"] == "validated_success":
                    if (
                        pair.get("effect_validated") is not True
                        or gate_reasons
                        or diagnosis.get("status") != "confirmed"
                        or diagnosis.get("primary_service") != labels.get("service")
                        or diagnosis.get("fault_type") != labels.get("fault_type")
                        or diagnosis.get("category") != labels.get("causal_category")
                        or (pair.get("retrieval_history") or {}).get(
                            "validated_outcome"
                        )
                        != {
                            "service": diagnosis.get("primary_service"),
                            "fault_type": diagnosis.get("fault_type"),
                            "category": diagnosis.get("category"),
                            "supporting_evidence": diagnosis.get(
                                "supporting_evidence"
                            ),
                        }
                    ):
                        raise OpenSearchError(
                            f"Unverified Qwen 3.5 B success for pair {pair_id}"
                        )
                elif (
                    diagnosis.get("status") != "inconclusive"
                    or not gate_reasons
                    or (pair.get("retrieval_history") or {}).get(
                        "validated_outcome"
                    )
                ):
                    raise OpenSearchError(
                        f"Inconclusive Qwen 3.5 B pair was promoted: {pair_id}"
                    )
            elif self.schema_version == 4:
                diagnosis = (pair.get("b") or {}).get("diagnosis") or {}
                labels = pair.get("labels") or {}
                gate_reasons = pair.get("b_rebuild_gate_reasons")
                review = (pair.get("b") or {}).get("procedural_review") or {}
                history = pair.get("retrieval_history") or {}
                passed = pair.get("b_procedural_status") == "passed"
                if (
                    pair.get("sha256")
                    != digest({
                        key: value for key, value in pair.items() if key != "sha256"
                    })
                    or pair.get("b_rebuild_version")
                    != TRAINING_B_PROCEDURAL_SCHEMA_VERSION
                    or (pair.get("b") or {}).get("rebuild_version")
                    != TRAINING_B_PROCEDURAL_SCHEMA_VERSION
                    or not isinstance(gate_reasons, list)
                    or not isinstance(pair.get("b_procedural_status"), str)
                ):
                    raise OpenSearchError(
                        f"Procedural B provenance is invalid for pair {pair_id}"
                    )
                if passed:
                    metric_step_id = (pair.get("b") or {}).get(
                        "metric_confirmation_step_id"
                    )
                    metric_steps = [
                        step for step in (pair.get("b") or {}).get(
                            "investigation_steps", []
                        )
                        if step.get("step_id") == metric_step_id
                        and procedural_metric_step_matches_target(
                            step,
                            labels.get("fault_type", ""),
                            labels.get("service", ""),
                        )
                    ]
                    if (
                        pair.get("guide_status") != "procedurally_validated"
                        or pair.get("causal_status") != "not_assessed"
                        or gate_reasons
                        or diagnosis.get("status") != "confirmed"
                        or diagnosis.get("primary_service") != labels.get("service")
                        or diagnosis.get("fault_type") != labels.get("fault_type")
                        or diagnosis.get("category") != labels.get("causal_category")
                        or review.get("result") != "pass"
                        or review.get("target_matches") is not True
                        or review.get("path_reaches_target") is not True
                        or review.get("metric_confirmation") is not True
                        or review.get("alternatives_explained") is not True
                        or not metric_steps
                        or history.get("procedural_status") != "passed"
                        or history.get("historical_status")
                        != "procedurally_validated"
                        or history.get("validated_outcome", {}).get("service")
                        != labels.get("service")
                    ):
                        raise OpenSearchError(
                            f"Unverified procedural Qwen 3.5 B for pair {pair_id}"
                        )
                elif (
                    pair.get("guide_status") != "procedural_pending"
                    or pair.get("causal_status") != "not_assessed"
                    or pair.get("b_procedural_status") != "pending"
                    or not gate_reasons
                    or diagnosis.get("status") != "inconclusive"
                    or "validated_outcome" in history
                ):
                    raise OpenSearchError(
                        f"Pending procedural B pair was promoted: {pair_id}"
                    )
            vector = np.asarray(vectors.get(pair_id), dtype=np.float32)
            if vector.shape != (embedding_dim,) or not np.isfinite(vector).all():
                raise OpenSearchError(f"Invalid embedding for pair {pair_id}")
            incident = IncidentA.model_validate(pair["a"])
            a_text = incident.retrieval_text(version=a_text_version)
            history = compact_exploratory_history(
                pair, include_executed_checks=self.schema_version >= 2
            )
            if {"labels", "diagnosis", "source_run_id"} & set(history):
                raise OpenSearchError("Historical labels cannot be indexed as guidance")
            if (
                self.schema_version in {3, 4}
                and history.get("historical_status") != pair.get("guide_status")
            ):
                raise OpenSearchError(
                    f"Rebuilt guidance status mismatch for pair {pair_id}"
                )
            document = {
                "pair_id": pair_id,
                "source_run_id": source_run_id,
                "effect_validated": pair["effect_validated"],
                "guide_status": pair["guide_status"],
                "causal_status": pair["causal_status"],
                "a_text": a_text,
                "a_vector": vector.tolist(),
                "history": history,
            }
            if self.schema_version == 4:
                document["b_procedural_status"] = pair["b_procedural_status"]
                if isinstance(pair.get("review_status"), str):
                    document["review_status"] = pair["review_status"]
            if self.schema_version >= 2:
                document.update(incident.retrieval_fields())
            document["source_document_sha256"] = digest(document)
            lines.append(
                json.dumps(
                    {"index": {"_index": self.index_name, "_id": pair_id}},
                    separators=(",", ":"),
                )
            )
            lines.append(
                json.dumps(
                    document,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        bulk = self._request(
            "POST",
            f"/{self.index_name}/_bulk",
            ("\n".join(lines) + "\n").encode("utf-8"),
            content_type="application/x-ndjson",
        )
        if (bulk or {}).get("errors") is not False:
            raise OpenSearchError("OpenSearch bulk indexing reported item errors")
        items = (bulk or {}).get("items")
        if not isinstance(items, list) or len(items) != len(pairs):
            raise OpenSearchError(
                "OpenSearch bulk response has an unexpected item count"
            )
        for item in items:
            action = item.get("index") if isinstance(item, dict) else None
            if not isinstance(action, dict) or action.get("status") not in {200, 201}:
                raise OpenSearchError("OpenSearch did not index every experiment")
        self._request("POST", f"/{self.index_name}/_refresh")
        count_response = self._request("GET", f"/{self.index_name}/_count")
        count = (count_response or {}).get("count")
        if count != len(pairs):
            raise OpenSearchError(
                f"OpenSearch index contains {count} documents; expected {len(pairs)}"
            )
        self.document_count = count
        self._index_metadata = metadata
        return {
            "index_name": self.index_name,
            "base_sha256": self.base_sha256,
            "opensearch_version": self.server_version,
            "document_count": count,
            "embedding_provider": embedding_provider,
            "embedding_dim": embedding_dim,
            "a_text_version": a_text_version,
        }

    def connect_existing(
        self,
        *,
        expected_pair_ids: set[str],
        expected_a_text_version: str,
    ) -> dict[str, Any]:
        root = self._request("GET", "/")
        version = ((root or {}).get("version") or {}).get("number")
        if not isinstance(version, str) or not version:
            raise OpenSearchError("OpenSearch version is missing from its root response")
        self.server_version = version

        existing = self._request("GET", f"/{self.index_name}")
        index = (existing or {}).get(self.index_name)
        metadata = ((index or {}).get("mappings") or {}).get("_meta")
        if not isinstance(metadata, dict):
            raise OpenSearchError("Existing OpenSearch index has no frozen metadata")
        if (
            metadata.get("schema_version") != self.schema_version
            or metadata.get("base_sha256") != self.base_sha256
            or metadata.get("a_text_version") != expected_a_text_version
            or not isinstance(metadata.get("embedding_provider"), str)
            or not isinstance(metadata.get("embedding_dim"), int)
            or isinstance(metadata.get("embedding_dim"), bool)
            or metadata["embedding_dim"] < 1
            or not isinstance(metadata.get("document_count"), int)
            or isinstance(metadata.get("document_count"), bool)
            or metadata["document_count"] != len(expected_pair_ids)
        ):
            raise OpenSearchError("Existing OpenSearch index metadata is incompatible")
        if self.schema_version in {3, 4}:
            expected_b_version = (
                TRAINING_B_SCHEMA_VERSION
                if self.schema_version == 3
                else TRAINING_B_PROCEDURAL_SCHEMA_VERSION
            )
            if (
                metadata.get("b_rebuild_version") != expected_b_version
                or metadata.get("model") != QWEN35_MODEL_ID
            ):
                raise OpenSearchError(
                    "Existing Qwen 3.5 B index metadata is incompatible"
                )

        count = self._request("GET", f"/{self.index_name}/_count")
        if (count or {}).get("count") != metadata["document_count"]:
            raise OpenSearchError("Existing OpenSearch index document count changed")
        source_fields = [
            "pair_id", "source_run_id", "effect_validated",
            "guide_status", "causal_status", "history",
        ]
        if self.schema_version == 4:
            source_fields.append("b_procedural_status")
            source_fields.append("review_status")
        response = self._request(
            "POST",
            f"/{self.index_name}/_search",
            {
                "size": metadata["document_count"],
                "_source": [
                    *source_fields,
                ],
                "query": {"match_all": {}},
            },
        )
        hits = ((response or {}).get("hits") or {}).get("hits")
        if not isinstance(hits, list) or len(hits) != metadata["document_count"]:
            raise OpenSearchError("Could not verify every frozen OpenSearch document")
        pair_ids: set[str] = set()
        for hit in hits:
            if not isinstance(hit, dict):
                raise OpenSearchError("OpenSearch returned a malformed frozen document")
            source = hit.get("_source")
            pair_id = hit.get("_id")
            if (
                not isinstance(source, dict)
                or not isinstance(pair_id, str)
                or not pair_id
                or source.get("pair_id") != pair_id
                or pair_id in pair_ids
                or not isinstance(source.get("source_run_id"), str)
                or not source["source_run_id"]
                or not isinstance(source.get("effect_validated"), bool)
                or (
                    (source.get("guide_status"), source.get("causal_status"))
                    not in (
                        {
                            ("validated_success", "supported"),
                            ("insufficient_evidence", "inconclusive"),
                            ("effect_unvalidated", "inconclusive"),
                        }
                        if self.schema_version == 3
                        else (
                            {
                                ("procedurally_validated", "not_assessed"),
                                ("procedural_pending", "not_assessed"),
                            }
                            if self.schema_version == 4
                            else {("pending", "inconclusive")}
                        )
                    )
                )
                or not isinstance(source.get("history"), dict)
                or source["history"].get("pair_id") != pair_id
                or {"labels", "diagnosis", "source_run_id"}
                & set(source["history"])
                or (
                    self.schema_version == 3
                    and (
                        source["history"].get("historical_status")
                        != source.get("guide_status")
                        or (
                            source.get("guide_status") == "validated_success"
                            and (
                                source.get("effect_validated") is not True
                                or source.get("causal_status") != "supported"
                            )
                        )
                    )
                    or (
                        self.schema_version == 4
                        and (
                            source["history"].get("historical_status")
                            != source.get("guide_status")
                            or source.get("causal_status") != "not_assessed"
                            or (
                                source.get("b_procedural_status") == "passed"
                                and source.get("guide_status")
                                != "procedurally_validated"
                            )
                            or (
                                source.get("b_procedural_status") != "passed"
                                and source.get("guide_status") != "procedural_pending"
                            )
                            or source["history"].get("procedural_status")
                            != source.get("b_procedural_status")
                        )
                    )
                )
            ):
                raise OpenSearchError("OpenSearch contains an invalid frozen document")
            pair_ids.add(pair_id)
        if pair_ids != expected_pair_ids:
            raise OpenSearchError(
                "Existing OpenSearch document IDs differ from the selected corpus"
            )

        self.document_count = len(pair_ids)
        self._index_metadata = metadata
        return {
            "index_name": self.index_name,
            "base_sha256": self.base_sha256,
            "opensearch_version": self.server_version,
            "document_count": self.document_count,
            "embedding_provider": metadata["embedding_provider"],
            "embedding_dim": metadata["embedding_dim"],
            "a_text_version": metadata["a_text_version"],
            **(
                {
                    "b_rebuild_version": metadata["b_rebuild_version"],
                    "model": metadata["model"],
                }
                if self.schema_version in {3, 4}
                else {}
            ),
        }

    @staticmethod
    def _rank_hits_with_scores(
        response: dict[str, Any],
    ) -> list[tuple[float, str, dict[str, Any]]]:
        raw_hits = (response.get("hits") or {}).get("hits")
        if not isinstance(raw_hits, list):
            raise OpenSearchError("OpenSearch search response is missing hits")
        hits: list[tuple[float, str, dict[str, Any]]] = []
        seen: set[str] = set()
        for hit in raw_hits:
            if not isinstance(hit, dict):
                raise OpenSearchError("OpenSearch returned a malformed hit")
            source = hit.get("_source")
            pair_id = hit.get("_id")
            score = hit.get("_score")
            if (
                not isinstance(source, dict)
                or not isinstance(pair_id, str)
                or source.get("pair_id") != pair_id
                or pair_id in seen
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
            ):
                raise OpenSearchError("OpenSearch returned an invalid experiment hit")
            seen.add(pair_id)
            hits.append((float(score), pair_id, source))
        hits.sort(key=lambda hit: (-hit[0], hit[1]))
        return hits

    @staticmethod
    def _rank_hits(response: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        return [
            (pair_id, source)
            for _, pair_id, source in OpenSearchExperimentIndex._rank_hits_with_scores(
                response
            )
        ]

    def search(
        self,
        query_text: str,
        query_vector: np.ndarray | None,
        *,
        source_run_id: str,
        top_k: int,
        include_unvalidated_experimental: bool,
        include_unvalidated_procedural_experimental: bool = False,
        min_score: float = 0.05,
        query_fields: dict[str, list[str]] | None = None,
        query_incident: IncidentA | None = None,
        corpus_pairs: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if self.document_count < 1 or self._index_metadata is None:
            raise OpenSearchError("OpenSearch experiment index has not been prepared")
        if not source_run_id or top_k < 1:
            raise OpenSearchError("A source run ID and positive top_k are required")
        if not math.isfinite(min_score) or not 0 <= min_score <= 1:
            raise OpenSearchError("min_score must be finite and between zero and one")
        if include_unvalidated_procedural_experimental and self.schema_version != 4:
            raise OpenSearchError(
                "unvalidated procedural inclusion is supported only by v4 retrieval"
            )
        self.search_metadata = None
        is_metric_v6 = self.retriever_version == OPENSEARCH_RETRIEVER_V6
        is_bm25 = self.retriever_version in {
            OPENSEARCH_RETRIEVER_V5,
            OPENSEARCH_RETRIEVER_V6,
        }
        if is_bm25:
            if query_incident is None or corpus_pairs is None:
                raise OpenSearchError(
                    "BM25 retrieval requires the query incident and frozen A corpus"
                )
            vector = None
        else:
            if query_vector is None:
                raise OpenSearchError("Hybrid retrieval requires a query embedding")
            vector = np.asarray(query_vector, dtype=np.float32)
            if (
                vector.shape != (self._index_metadata["embedding_dim"],)
                or not np.isfinite(vector).all()
            ):
                raise OpenSearchError(
                    "Query embedding does not match the OpenSearch index"
                )
        if self.schema_version == 3:
            filters = [
                {"term": {"guide_status": "validated_success"}},
                {"term": {"causal_status": "supported"}},
                {"term": {"effect_validated": True}},
            ]
        elif self.schema_version == 4:
            if include_unvalidated_procedural_experimental:
                filters = [
                    {
                        "bool": {
                            "should": [
                                {
                                    "bool": {
                                        "filter": [
                                            {
                                                "term": {
                                                    "guide_status": "procedurally_validated"
                                                }
                                            },
                                            {"term": {"b_procedural_status": "passed"}},
                                        ]
                                    }
                                },
                                {
                                    "bool": {
                                        "filter": [
                                            {
                                                "term": {
                                                    "guide_status": "procedural_pending"
                                                }
                                            },
                                            {
                                                "term": {
                                                    "b_procedural_status": "pending"
                                                }
                                            },
                                        ]
                                    }
                                },
                            ],
                            "minimum_should_match": 1,
                        }
                    }
                ]
            else:
                filters = [
                    {"term": {"guide_status": "procedurally_validated"}},
                    {"term": {"b_procedural_status": "passed"}},
                ]
        else:
            filters = [
                {"term": {"guide_status": "pending"}},
                {"term": {"causal_status": "inconclusive"}},
            ]
        if not include_unvalidated_experimental and self.schema_version != 4:
            filters.append({"term": {"effect_validated": True}})
        exclusion = [{"term": {"source_run_id": source_run_id}}]
        candidate_count = self.document_count
        dense_hits: list[tuple[str, dict[str, Any]]] = []
        if (
            not is_bm25
            and query_text.strip()
            and vector is not None
            and float(np.linalg.norm(vector)) > 0
        ):
            dense_response = self._request(
                "POST",
                f"/{self.index_name}/_search",
                {
                    "size": candidate_count,
                    "query": {
                        "knn": {
                            "a_vector": {
                                "vector": vector.tolist(),
                                "k": candidate_count,
                                "filter": {
                                    "bool": {
                                        "filter": filters,
                                        "must_not": exclusion,
                                    }
                                },
                            }
                        }
                    },
                },
            )
            dense_hits = self._rank_hits(dense_response or {})
        lexical_hits: list[tuple[str, dict[str, Any]]] = []
        bm25_scores: dict[str, float] = {}
        if query_text.strip():
            if is_bm25:
                smart_fields = _informative_query_fields(query_incident, corpus_pairs)
                field_queries = [
                    {
                        "match": {
                            field: {
                                "query": " ".join(terms),
                                "boost": _FIELD_BOOSTS[field],
                            }
                        }
                    }
                    for field, terms in smart_fields.items()
                ]
                lexical_query = (
                    {
                        "bool": {
                            "should": field_queries,
                            "minimum_should_match": 1,
                        }
                    }
                    if field_queries
                    else None
                )
            elif self.schema_version >= 2 and query_fields is not None:
                field_queries = _bounded_field_queries(query_fields)
                lexical_query = (
                    {
                        "bool": {
                            "should": field_queries,
                            "minimum_should_match": 1,
                        }
                    }
                    if field_queries
                    else None
                )
            else:
                lexical_text = query_text
                if self.schema_version >= 2:
                    words = _LEXICAL_TOKEN.findall(query_text)
                    if len(words) > _MAX_LEXICAL_TERMS_PER_FIELD:
                        half = _MAX_LEXICAL_TERMS_PER_FIELD // 2
                        lexical_text = " ".join(words[:half] + words[-half:])
                lexical_query = (
                    {
                        "multi_match": {
                            "query": lexical_text,
                            "fields": _LEXICAL_FIELDS,
                            "type": "best_fields",
                            "tie_breaker": 0.3,
                        }
                    }
                    if self.schema_version >= 2
                    else {"match": {"a_text": {"query": query_text}}}
                )
            if lexical_query is not None:
                lexical_response = self._request(
                    "POST",
                    f"/{self.index_name}/_search",
                    {
                        "size": candidate_count,
                        "query": {
                            "bool": {
                                "must": [lexical_query],
                                "filter": filters,
                                "must_not": exclusion,
                            }
                        },
                    },
                )
                if is_bm25:
                    scored_hits = self._rank_hits_with_scores(lexical_response or {})
                    lexical_hits = [
                        (pair_id, source) for _, pair_id, source in scored_hits
                    ]
                    bm25_scores = {pair_id: score for score, pair_id, _ in scored_hits}
                else:
                    lexical_hits = self._rank_hits(lexical_response or {})
        dense_ranks = {pair_id: rank for rank, (pair_id, _) in enumerate(dense_hits, 1)}
        lexical_ranks = {
            pair_id: rank for rank, (pair_id, _) in enumerate(lexical_hits, 1)
        }
        documents = dict(dense_hits)
        documents.update(lexical_hits)
        max_score = 2.0 / (_RRF_CONSTANT + 1)
        max_bm25_score = max(bm25_scores.values(), default=0.0)
        numeric_ranks: dict[str, int] = {}
        fusion_scores: dict[str, float] = {}
        numeric_route_active = False
        if is_metric_v6:
            if query_incident is None or corpus_pairs is None:
                raise OpenSearchError(
                    "Quantitative retrieval requires A and its corpus"
                )
            try:
                if query_incident.text_version != A_TEXT_VERSION:
                    raise ValueError("Quantitative queries require incident-a-v2")
                candidates = [
                    ProfileCandidate(
                        pair["pair_id"],
                        pair["source_run_id"],
                        IncidentA.model_validate(pair["a"]),
                    )
                    for pair in corpus_pairs
                ]
                if len(candidates) != self.document_count or any(
                    candidate.incident.text_version != A_TEXT_VERSION
                    for candidate in candidates
                ):
                    raise ValueError(
                        "Quantitative corpus does not match the indexed A version/count"
                    )
                profile = MetricProfileIndex(candidates)
                candidate_runs = {
                    candidate.pair_id: candidate.source_run_id
                    for candidate in candidates
                }
                for pair_id, document in lexical_hits:
                    if (
                        pair_id not in candidate_runs
                        or document.get("source_run_id") != candidate_runs[pair_id]
                        or document.get("source_run_id") == source_run_id
                    ):
                        raise ValueError(
                            "Lexical candidate identity differs from the frozen corpus"
                        )
                strength = profile.strength(query_incident)
                numeric_route_active = strength >= DEFAULT_SIGNAL_THRESHOLD
                if numeric_route_active:
                    pool_response = self._request(
                        "POST",
                        f"/{self.index_name}/_search",
                        {
                            "size": candidate_count,
                            "query": {
                                "bool": {
                                    "filter": filters,
                                    "must_not": exclusion,
                                }
                            },
                        },
                    )
                    pool = dict(self._rank_hits(pool_response or {}))
                    for pair_id, document in pool.items():
                        if (
                            pair_id not in candidate_runs
                            or document.get("source_run_id") != candidate_runs[pair_id]
                            or document.get("source_run_id") == source_run_id
                        ):
                            raise ValueError(
                                "Numeric candidate identity differs from the frozen corpus"
                            )
                    if not set(lexical_ranks) <= set(pool):
                        raise ValueError(
                            "Lexical and numeric candidate eligibility differ"
                        )
                    numeric = profile.rank(
                        query_incident,
                        source_run_id=source_run_id,
                        allowed_pair_ids=set(pool),
                    )
                    numeric_ranks = {
                        pair_id: rank for rank, pair_id in enumerate(numeric, 1)
                    }
                    fusion_scores = dict(
                        fuse_rankings_with_scores(
                            [pair_id for pair_id, _ in lexical_hits],
                            numeric,
                            DEFAULT_NUMERIC_WEIGHT,
                        )
                    )
                    documents.update(pool)
                self.search_metadata = {
                    "profile_version": METRIC_PROFILE_VERSION,
                    "numeric_route_active": numeric_route_active,
                    "numeric_strength": strength,
                    "signal_threshold": DEFAULT_SIGNAL_THRESHOLD,
                    "numeric_weight": DEFAULT_NUMERIC_WEIGHT,
                    "rrf_constant": METRIC_RRF_CONSTANT,
                    "scale_fit": "frozen corpus A only",
                    "numeric_candidate_count": len(numeric_ranks),
                    "bm25_candidate_count": len(lexical_ranks),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise OpenSearchError(f"Quantitative retrieval failed: {exc}") from exc
        ranked = []
        for pair_id, document in documents.items():
            dense_rank = dense_ranks.get(pair_id)
            lexical_rank = lexical_ranks.get(pair_id)
            if is_metric_v6 and numeric_route_active:
                if pair_id not in fusion_scores:
                    continue
                score = fusion_scores[pair_id] * (METRIC_RRF_CONSTANT + 1)
            elif is_bm25:
                score = (
                    bm25_scores[pair_id] / max_bm25_score if max_bm25_score > 0 else 0.0
                )
            else:
                reciprocal_score = sum(
                    1.0 / (_RRF_CONSTANT + rank)
                    for rank in (dense_rank, lexical_rank)
                    if rank is not None
                )
                score = reciprocal_score / max_score
            if score < min_score:
                continue
            history = document.get("history")
            if not isinstance(history, dict) or history.get("pair_id") != pair_id:
                raise OpenSearchError(f"Missing compact history for pair {pair_id}")
            if {"labels", "diagnosis", "source_run_id"} & set(history):
                raise OpenSearchError("Retrieved guidance contains historical labels")
            if self.schema_version == 3 and (
                document.get("guide_status") != "validated_success"
                or document.get("causal_status") != "supported"
                or document.get("effect_validated") is not True
                or history.get("historical_status") != "validated_success"
                or "validated_outcome" not in history
            ):
                raise OpenSearchError(
                    "v3 retrieval returned a guide that was not validated for publication"
                )
            if self.schema_version == 4:
                validated_guide = (
                    document.get("guide_status") == "procedurally_validated"
                    and document.get("b_procedural_status") == "passed"
                    and history.get("historical_status") == "procedurally_validated"
                    and history.get("procedural_status") == "passed"
                    and "validated_outcome" in history
                )
                pending_guide = (
                    document.get("guide_status") == "procedural_pending"
                    and document.get("b_procedural_status") == "pending"
                    and history.get("historical_status") == "procedural_pending"
                    and history.get("procedural_status") == "pending"
                    and "validated_outcome" not in history
                )
                if not validated_guide and not (
                    include_unvalidated_procedural_experimental and pending_guide
                ):
                    raise OpenSearchError(
                        "v4 retrieval returned a guide outside its procedural policy"
                    )
            ranked.append(
                {
                    "pair_id": pair_id,
                    "source_run_id": document["source_run_id"],
                    "effect_validated": document["effect_validated"],
                    "guide_status": document["guide_status"],
                    "causal_status": document["causal_status"],
                    **(
                        {"review_status": document["review_status"]}
                        if isinstance(document.get("review_status"), str)
                        else {}
                    ),
                    **(
                        {"b_procedural_status": document["b_procedural_status"]}
                        if self.schema_version == 4
                        else {}
                    ),
                    "history": history,
                    "score": round(score, 6),
                    "score_components": {
                        "dense_rank": dense_rank,
                        "bm25_rank": lexical_rank,
                        **(
                            {"numeric_rank": numeric_ranks.get(pair_id)}
                            if is_metric_v6
                            else {}
                        ),
                    },
                }
            )
        ranked.sort(
            key=lambda item: (
                (
                    -fusion_scores[item["pair_id"]]
                    if is_metric_v6 and numeric_route_active
                    else -item["score"]
                ),
                item["pair_id"],
            )
        )
        return ranked[:top_k]


def index_frozen_base(
    base_file: str | Path, *, url: str = DEFAULT_OPENSEARCH_URL
) -> dict[str, Any]:
    path = Path(base_file)
    try:
        base = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OpenSearchError(f"Frozen base file is invalid JSON: {path}") from exc
    if not isinstance(base, dict):
        raise OpenSearchError("Frozen base file must contain a JSON object")
    payload = {key: value for key, value in base.items() if key != "sha256"}
    base_sha256 = base.get("sha256")
    if not isinstance(base_sha256, str) or digest(payload) != base_sha256:
        raise OpenSearchError("Frozen base checksum does not match its contents")
    index = OpenSearchExperimentIndex(
        url, base_sha256, retriever_version=OPENSEARCH_RETRIEVER_VERSION
    )
    return index.index_base(base, a_text_version=A_TEXT_VERSION)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a separate v2 OpenSearch index from a frozen A/B base."
    )
    parser.add_argument(
        "--base-file",
        type=Path,
        required=True,
        help="Path to the frozen exploratory_base.json file",
    )
    args = parser.parse_args()
    result = index_frozen_base(args.base_file)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
