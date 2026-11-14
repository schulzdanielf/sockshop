from experiment.platform.backend.rca_options import RCABenchmarkOptions
from experiment.platform.backend.resource_signature import (
    options_fingerprint,
    resource_signature_metadata,
    resource_signature_text,
)


def test_disabled_policy_preserves_legacy_fingerprint_shape():
    options = RCABenchmarkOptions()

    assert "resource_signature_policy" not in options_fingerprint(options)
    assert resource_signature_metadata("none") == {
        "policy": "none",
        "text_sha256": None,
    }


def test_cpu_memory_policy_is_versioned_and_cautious():
    text = resource_signature_text("cpu-memory-v1")
    metadata = resource_signature_metadata("cpu-memory-v1")

    assert text is not None
    assert "`memory-hog`" in text
    assert "`cpu-hog`" in text
    assert "Ausencia de dados de memoria nao significa estabilidade" in text
    assert "uma unica familia de evidencia" in text
    assert len(metadata["text_sha256"]) == 64
    assert metadata["policy"] == "cpu-memory-v1"


def test_unknown_resource_signature_policy_is_rejected():
    try:
        RCABenchmarkOptions(resource_signature_policy="cpu-memory-v2")
    except ValueError as exc:
        assert "resource_signature_policy" in str(exc)
    else:
        raise AssertionError("unknown resource signature policy was accepted")
