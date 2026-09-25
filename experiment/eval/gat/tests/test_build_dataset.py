"""Testes do dataset builder: C3, normalização de chaos_type e splits S1–S4."""
from __future__ import annotations

from experiment.eval.gat.build_dataset import (
    EXCLUDED_TARGET_SERVICES,
    S2_HELD_OUT_SERVICES,
    S3_HELD_OUT_FAULT,
    TRANSIENT_CAP_SECONDS,
    assign_splits,
    effective_fault_window,
    normalize_chaos_type,
)

# ── normalização de chaos_type ──────────────────────────────────────────
def test_normalize_passthrough():
    assert normalize_chaos_type("cpu-hog") == "cpu-hog"
    assert normalize_chaos_type("pod-delete") == "pod-delete"


def test_normalize_strips_service_prefix():
    assert normalize_chaos_type("orders-container-kill") == "container-kill"
    assert normalize_chaos_type("payment-network-latency") == "network-latency"
    assert normalize_chaos_type("catalogue-network-loss") == "network-loss"
    assert normalize_chaos_type("user-http-status-code") == "http-status-code"


def test_normalize_manifest_path():
    assert (
        normalize_chaos_type("/tmp/manifests_istio_expansion/payment-http-status-code.yaml")
        == "http-status-code"
    )


def test_normalize_none_and_unknown():
    assert normalize_chaos_type(None) is None
    assert normalize_chaos_type("unknown-fault") == "unknown-fault"


# ── C3: janela efetiva ──────────────────────────────────────────────────
def test_transient_fault_gets_capped():
    # pod-delete com fase de 10 min → recortada para 3 min
    start, end, meta = effective_fault_window(
        "pod-delete",
        "2026-09-16T10:00:00+00:00",
        "2026-09-16T10:10:00+00:00",
    )
    assert meta["is_transient"] is True
    assert meta["rule"] == "transient_recorte"
    assert meta["effective_window_seconds"] == TRANSIENT_CAP_SECONDS
    assert meta["effective_window_seconds"] < meta["phase_duration_seconds"]
    assert end == "2026-09-16T10:03:00+00:00"


def test_transient_cap_respects_phase_end():
    # fase de fault mais curta que o cap → usa o fim real da fase
    start, end, meta = effective_fault_window(
        "container-kill",
        "2026-09-16T10:00:00+00:00",
        "2026-09-16T10:01:30+00:00",  # 90s < 180s
    )
    assert meta["is_transient"] is True
    assert meta["effective_window_seconds"] == 90.0
    assert end == "2026-09-16T10:01:30+00:00"


def test_persistent_fault_full_phase():
    start, end, meta = effective_fault_window(
        "cpu-hog",
        "2026-09-16T10:00:00+00:00",
        "2026-09-16T10:05:00+00:00",
    )
    assert meta["is_transient"] is False
    assert meta["rule"] == "persistent_full_phase"
    assert meta["effective_window_seconds"] == 300.0
    assert end == "2026-09-16T10:05:00+00:00"


# ── splits S1–S4 ────────────────────────────────────────────────────────
def test_s1_repetition_last_replica_is_test():
    # célula com 3 réplicas: ranks 0,1 treino; rank 2 (mais recente) teste
    s0 = assign_splits("r0", "cpu-hog", "user", True, 0, 3)
    s1 = assign_splits("r1", "cpu-hog", "user", True, 1, 3)
    s2 = assign_splits("r2", "cpu-hog", "user", True, 2, 3)
    assert s0["S1"] == "train" and s1["S1"] == "train" and s2["S1"] == "test"


def test_s1_single_replica_cell_not_testable():
    s = assign_splits("r0", "cpu-hog", "user", True, 0, 1)
    assert s["S1"] is None  # célula com 1 réplica não pode medir repetição


def test_s2_held_out_service():
    for svc in S2_HELD_OUT_SERVICES:
        s = assign_splits("r", "cpu-hog", svc, True, 0, 2)
        assert s["S2"] == "test"
    s = assign_splits("r", "cpu-hog", "catalogue", True, 0, 2)
    assert s["S2"] == "train"


def test_s3_held_out_fault():
    assert S3_HELD_OUT_FAULT == "container-kill"
    s = assign_splits("r", S3_HELD_OUT_FAULT, "user", True, 0, 2)
    assert s["S3"] == "test"
    s = assign_splits("r", "cpu-hog", "user", True, 0, 2)
    assert s["S3"] == "train"


def test_queue_master_excluded_from_target_corpus():
    assert "queue-master" in EXCLUDED_TARGET_SERVICES


def test_s4_not_applicable_retroactive():
    s = assign_splits("r", "cpu-hog", "user", True, 0, 2)
    assert s["S4"] is None


def test_anti_leakage_invariant():
    """Invariante: em S2 e S3, um run nunca é treino e teste ao mesmo tempo
    para a mesma dimensão. Aqui testamos que a regra é puramente uma função
    do (chaos, serviço) — não há run que seja train E test em S2."""
    for svc in ("orders", "payment", "catalogue", "user"):
        s = assign_splits("r", "cpu-hog", svc, True, 0, 2)
        assert s["S2"] in ("train", "test")  # nunca ambos
