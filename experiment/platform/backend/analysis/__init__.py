"""L2 summary and retrieval utilities (RAG-ready)."""
from .embeddings import (
    EmbeddingProvider,
    HashingEmbeddingProvider,
    SentenceTransformerProvider,
    anonymize_services,
    blob_to_vector,
    get_embedding_provider,
    reset_embedding_provider,
    vector_to_blob,
)
from .fault_category_validator import validate_fault_category
from .llm_client import (
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_URL,
    LLMClientError,
    call_llm,
    call_llm_messages,
    llm_health,
    parse_verdict_response,
)
from .prompt import assemble_prompt, estimate_tokens
from .retrieval import (
    cosine_similarity,
    rank_by_embedding,
    rank_hybrid,
    rank_similar_runs,
    score_graph_similarity,
    score_similarity,
)
from .service_localizer import localize_service
from .summary import build_run_summary_l2, build_summary_tags
from .system_card import (
    SystemCardError,
    list_system_cards,
    load_system_card,
    render_system_card,
    system_card_meta,
)
from .temporal import compute_temporal_features

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
    "call_llm_messages",
    "parse_verdict_response",
    "llm_health",
    "LLMClientError",
    "compute_temporal_features",
    "load_system_card",
    "render_system_card",
    "list_system_cards",
    "system_card_meta",
    "SystemCardError",
    "validate_fault_category",
    "localize_service",
]
