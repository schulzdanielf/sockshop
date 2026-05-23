"""L2 summary and retrieval utilities (RAG-ready)."""
from .summary import build_run_summary_l2, build_summary_tags
from .retrieval import (
    score_similarity,
    rank_similar_runs,
    rank_by_embedding,
    cosine_similarity,
    score_graph_similarity,
    rank_hybrid,
)
from .embeddings import (
    EmbeddingProvider,
    HashingEmbeddingProvider,
    SentenceTransformerProvider,
    get_embedding_provider,
    reset_embedding_provider,
    vector_to_blob,
    blob_to_vector,
)
from .prompt import assemble_prompt, estimate_tokens
from .llm_client import call_llm, parse_verdict_response, llm_health, LLMClientError
from .temporal import compute_temporal_features
from .system_card import (
    load_system_card,
    render_system_card,
    list_system_cards,
    system_card_meta,
    SystemCardError,
)

__all__ = [
    "build_run_summary_l2",
    "build_summary_tags",
    "score_similarity",
    "rank_similar_runs",
    "rank_by_embedding",
    "cosine_similarity",
    "score_graph_similarity",
    "rank_hybrid",
    "EmbeddingProvider",
    "HashingEmbeddingProvider",
    "SentenceTransformerProvider",
    "get_embedding_provider",
    "reset_embedding_provider",
    "vector_to_blob",
    "blob_to_vector",
    "assemble_prompt",
    "estimate_tokens",
    "call_llm",
    "parse_verdict_response",
    "llm_health",
    "LLMClientError",
    "compute_temporal_features",
    "load_system_card",
    "render_system_card",
    "list_system_cards",
    "system_card_meta",
    "SystemCardError",
]
