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

from experiment.platform.backend.rca_knowledge import (
    A_TEXT_VERSION,
    IncidentA,
    OPENSEARCH_RETRIEVER_V1,
    OPENSEARCH_RETRIEVER_VERSION,
    compact_exploratory_history,
    digest,
)

DEFAULT_OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200")
_INDEX_SCHEMA_VERSIONS = {
    OPENSEARCH_RETRIEVER_V1: 1,
    OPENSEARCH_RETRIEVER_VERSION: 2,
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
            if self.schema_version == 2:
                properties.update(
                    {
                        field: {"type": "text"}
                        for field in _LEXICAL_FIELDS
                    }
                )
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
            if (
                pair.get("guide_status") != "pending"
                or pair.get("causal_status") != "inconclusive"
                or not isinstance(pair.get("effect_validated"), bool)
            ):
                raise OpenSearchError(f"Unexpected validation state for pair {pair_id}")
            vector = np.asarray(vectors.get(pair_id), dtype=np.float32)
            if vector.shape != (embedding_dim,) or not np.isfinite(vector).all():
                raise OpenSearchError(f"Invalid embedding for pair {pair_id}")
            incident = IncidentA.model_validate(pair["a"])
            a_text = incident.retrieval_text(version=a_text_version)
            history = compact_exploratory_history(
                pair, include_executed_checks=self.schema_version == 2
            )
            if {"labels", "diagnosis", "source_run_id"} & set(history):
                raise OpenSearchError("Historical labels cannot be indexed as guidance")
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
            if self.schema_version == 2:
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

        count = self._request("GET", f"/{self.index_name}/_count")
        if (count or {}).get("count") != metadata["document_count"]:
            raise OpenSearchError("Existing OpenSearch index document count changed")
        response = self._request(
            "POST",
            f"/{self.index_name}/_search",
            {
                "size": metadata["document_count"],
                "_source": [
                    "pair_id",
                    "source_run_id",
                    "effect_validated",
                    "guide_status",
                    "causal_status",
                    "history",
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
                or source.get("guide_status") != "pending"
                or source.get("causal_status") != "inconclusive"
                or not isinstance(source.get("history"), dict)
                or source["history"].get("pair_id") != pair_id
                or {"labels", "diagnosis", "source_run_id"}
                & set(source["history"])
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
        }

    @staticmethod
    def _rank_hits(response: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
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
        return [(pair_id, source) for _, pair_id, source in hits]

    def search(
        self,
        query_text: str,
        query_vector: np.ndarray,
        *,
        source_run_id: str,
        top_k: int,
        include_unvalidated_experimental: bool,
        min_score: float = 0.05,
    ) -> list[dict[str, Any]]:
        if self.document_count < 1 or self._index_metadata is None:
            raise OpenSearchError("OpenSearch experiment index has not been prepared")
        if not source_run_id or top_k < 1:
            raise OpenSearchError("A source run ID and positive top_k are required")
        if not math.isfinite(min_score) or not 0 <= min_score <= 1:
            raise OpenSearchError("min_score must be finite and between zero and one")
        vector = np.asarray(query_vector, dtype=np.float32)
        if (
            vector.shape != (self._index_metadata["embedding_dim"],)
            or not np.isfinite(vector).all()
        ):
            raise OpenSearchError("Query embedding does not match the OpenSearch index")
        filters = [
            {"term": {"guide_status": "pending"}},
            {"term": {"causal_status": "inconclusive"}},
        ]
        if not include_unvalidated_experimental:
            filters.append({"term": {"effect_validated": True}})
        exclusion = [{"term": {"source_run_id": source_run_id}}]
        candidate_count = self.document_count
        dense_hits: list[tuple[str, dict[str, Any]]] = []
        if query_text.strip() and float(np.linalg.norm(vector)) > 0:
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
        if query_text.strip():
            lexical_query = (
                {
                    "multi_match": {
                        "query": query_text,
                        "fields": _LEXICAL_FIELDS,
                        "type": "best_fields",
                        "tie_breaker": 0.3,
                    }
                }
                if self.schema_version == 2
                else {"match": {"a_text": {"query": query_text}}}
            )
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
            lexical_hits = self._rank_hits(lexical_response or {})
        dense_ranks = {pair_id: rank for rank, (pair_id, _) in enumerate(dense_hits, 1)}
        lexical_ranks = {
            pair_id: rank for rank, (pair_id, _) in enumerate(lexical_hits, 1)
        }
        documents = dict(dense_hits)
        documents.update(lexical_hits)
        max_score = 2.0 / (_RRF_CONSTANT + 1)
        ranked = []
        for pair_id, document in documents.items():
            dense_rank = dense_ranks.get(pair_id)
            lexical_rank = lexical_ranks.get(pair_id)
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
            ranked.append(
                {
                    "pair_id": pair_id,
                    "source_run_id": document["source_run_id"],
                    "effect_validated": document["effect_validated"],
                    "guide_status": document["guide_status"],
                    "causal_status": document["causal_status"],
                    "history": history,
                    "score": round(score, 6),
                    "score_components": {
                        "dense_rank": dense_rank,
                        "bm25_rank": lexical_rank,
                    },
                }
            )
        ranked.sort(key=lambda item: (-item["score"], item["pair_id"]))
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
