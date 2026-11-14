from __future__ import annotations

import hashlib
from typing import Any, Literal

ResourceSignaturePolicy = Literal["none", "cpu-memory-v1"]

_CPU_MEMORY_V1 = (
    "Compare CPU e memoria do mesmo componente entre baseline, fault e, quando "
    "disponivel, post, respeitando identidade de servico/pod/container, unidade, "
    "limites, agregacao e cobertura. Neste ambiente experimental, aumento "
    "expressivo de memoria acompanhado de aumento de CPU favorece a hipotese "
    "`memory-hog`; aumento expressivo de CPU com memoria aproximadamente estavel, "
    "ou com variacao pequena frente ao baseline, favorece `cpu-hog`. Pequenos "
    "aumentos de memoria nao excluem `cpu-hog`. Essa orientacao e uma hipotese "
    "discriminativa, nao uma regra deterministica: confronte carga, alocacao/GC, "
    "throttling e outras explicacoes com evidencias do incidente atual. Ausencia "
    "de dados de memoria nao significa estabilidade. Nao infira leak, GC ou OOM "
    "apenas desses sinais. CPU e memoria sao uma unica familia de evidencia e "
    "nao bastam para confirmar causalidade. Use somente os tipos de falha "
    "permitidos no contexto; mantenha `inconclusive` quando faltar corroboracao."
)
_POLICY_TEXTS = {"cpu-memory-v1": _CPU_MEMORY_V1}


def resource_signature_text(policy: ResourceSignaturePolicy) -> str | None:
    if policy == "none":
        return None
    return _POLICY_TEXTS[policy]


def resource_signature_metadata(policy: ResourceSignaturePolicy) -> dict[str, Any]:
    text = resource_signature_text(policy)
    return {
        "policy": policy,
        "text_sha256": (
            hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
        ),
    }


def options_fingerprint(options: Any) -> dict[str, Any]:
    """Keep disabled-policy fingerprints compatible with pre-policy records."""
    data = options.model_dump()
    if data.get("resource_signature_policy") == "none":
        data.pop("resource_signature_policy")
    return data
