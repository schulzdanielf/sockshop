#!/usr/bin/env python3
"""Generate per-(service, chaos_type) Argo Workflow manifests.

The harness drives several chaos types via the platform's Litmus plugin —
``memory-hog``, ``cpu-hog``, ``pod-delete``, ``network-latency``,
``network-loss``, ``dns-error``, ``io-stress``, ``http-status-code`` and
``container-kill`` (see ``RENDERERS``). The plugin runs each chaos by
*cloning* an existing Argo Workflow whose name matches
``<service>-<template_suffix>`` (e.g. ``carts-cpu-hog``), so each combination
must exist in the ``litmus`` namespace before the harness starts.

Sources:

* ``deploy/kubernetes/manifests-chaos/catalogue-memory-hog.yaml``
* ``deploy/kubernetes/manifests-chaos/catalogue-cpu-hog.yaml``     (note: the
  upstream workflow is named ``catalogue-cpu``; we normalize it to
  ``catalogue-cpu-hog`` for symmetry with the other templates)
* ``pod-delete`` has no upstream baseline so we build it inline from the
  cpu-hog skeleton.

Output: ``experiment/eval/memory_hog/out/manifests/<svc>-<chaos>.yaml`` —
all files can be applied at once with::

    kubectl apply -f experiment/eval/memory_hog/out/manifests/

Idempotent — re-running just overwrites the generated YAMLs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

import yaml

ROOT = Path(__file__).resolve().parents[3]
CHAOS_MANIFEST_DIR = ROOT / "deploy" / "kubernetes" / "manifests-chaos"


# ── Per-chaos-type rendering specs ─────────────────────────────────────────
# Each chaos type knows:
#   * how to fetch its baseline template text (from a file or an inline string)
#   * which tokens to rewrite for the target service
#   * which env vars to substitute from the harness config


def _strip_probe_ref(text: str) -> str:
    """Remove the `probeRef` annotation line — the harness ships no probes."""
    return "\n".join(line for line in text.splitlines() if "probeRef" not in line)


def _replace_in_order(text: str, replacements: List[tuple[str, str]]) -> str:
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def _render_memory_hog(service: str, env: Dict[str, Any]) -> str:
    src = (CHAOS_MANIFEST_DIR / "catalogue-memory-hog.yaml").read_text(encoding="utf-8")
    text = _strip_probe_ref(src)
    mem_mb = int(env.get("memory_consumption_mb", 60))
    duration_s = int(env.get("total_chaos_duration_seconds", 30))
    text = _replace_in_order(
        text,
        [
            # Workflow + label (most specific first)
            ("catalogue-memory-hog", f"{service}-memory-hog"),
            ("applabel: name=catalogue", f"applabel: name={service}"),
            # TARGET_CONTAINER (multi-line context)
            (
                "- name: TARGET_CONTAINER\n                              value: catalogue",
                f"- name: TARGET_CONTAINER\n                              value: {service}",
            ),
            # Chaos parameters (only the ChaosEngine values — the ChaosExperiment
            # defaults shipped inside install-chaos-faults stay at the upstream
            # values; the engine values are what actually drive the run).
            (
                '- name: MEMORY_CONSUMPTION\n                              value: "60"',
                f'- name: MEMORY_CONSUMPTION\n                              value: "{mem_mb}"',
            ),
            (
                '- name: TOTAL_CHAOS_DURATION\n                              value: "30"',
                f'- name: TOTAL_CHAOS_DURATION\n                              value: "{duration_s}"',
            ),
        ],
    )
    return text


def _render_cpu_hog(service: str, env: Dict[str, Any]) -> str:
    src = (CHAOS_MANIFEST_DIR / "catalogue-cpu-hog.yaml").read_text(encoding="utf-8")
    text = _strip_probe_ref(src)
    cpu_cores = int(env.get("cpu_cores", 1))
    duration_s = int(env.get("total_chaos_duration_seconds", 60))
    text = _replace_in_order(
        text,
        [
            # NOTE: upstream workflow is named `catalogue-cpu` (no `-hog`).
            # Normalize to `<service>-cpu-hog` so all chaos types share the
            # `<service>-<template_suffix>` convention the plugin clones from.
            ("catalogue-cpu", f"{service}-cpu-hog"),
            ("applabel: name=catalogue", f"applabel: name={service}"),
            (
                "- name: TARGET_CONTAINER\n                              value: catalogue",
                f"- name: TARGET_CONTAINER\n                              value: {service}",
            ),
            (
                '- name: CPU_CORES\n                              value: "1"',
                f'- name: CPU_CORES\n                              value: "{cpu_cores}"',
            ),
            (
                '- name: TOTAL_CHAOS_DURATION\n                              value: "60"',
                f'- name: TOTAL_CHAOS_DURATION\n                              value: "{duration_s}"',
            ),
        ],
    )
    return text


# ── Inline pod-delete workflow (no upstream baseline shipped for it) ───────
# Skeleton mirrors the cpu-hog / memory-hog wrappers: install ChaosExperiment,
# apply ChaosEngine, cleanup. The ChaosExperiment definition is the upstream
# Litmus pod-delete spec.
POD_DELETE_TEMPLATE = """kind: Workflow
apiVersion: argoproj.io/v1alpha1
metadata:
  name: {service}-pod-delete
  namespace: litmus
  labels:
    infra_id: f21787ba-08ee-4649-ab66-c880492636dc
    workflows.argoproj.io/controller-instanceid: f21787ba-08ee-4649-ab66-c880492636dc
spec:
  templates:
    - name: argowf-chaos
      inputs: {{}}
      outputs: {{}}
      metadata: {{}}
      steps:
        - - name: install-chaos-faults
            template: install-chaos-faults
            arguments: {{}}
        - - name: run-chaos
            template: run-chaos
            arguments: {{}}
        - - name: cleanup-chaos-resources
            template: cleanup-chaos-resources
            arguments: {{}}
    - name: install-chaos-faults
      inputs:
        artifacts:
          - name: install-chaos-faults
            path: /tmp/pod-delete.yaml
            raw:
              data: >
                apiVersion: litmuschaos.io/v1alpha1

                description:
                  message: |
                    Deletes a pod belonging to a deployment/statefulset/daemonset
                kind: ChaosExperiment

                metadata:
                  name: pod-delete
                spec:
                  definition:
                    scope: Namespaced
                    permissions:
                      - apiGroups: [""]
                        resources: ["pods","events"]
                        verbs: ["create","list","get","patch","update","delete","deletecollection"]
                      - apiGroups: [""]
                        resources: ["pods/log"]
                        verbs: ["get","list","watch"]
                      - apiGroups: ["batch"]
                        resources: ["jobs"]
                        verbs: ["create","list","get","delete","deletecollection"]
                      - apiGroups: ["apps"]
                        resources: ["deployments","statefulsets","daemonsets","replicasets"]
                        verbs: ["list","get"]
                      - apiGroups: ["litmuschaos.io"]
                        resources: ["chaosengines","chaosexperiments","chaosresults"]
                        verbs: ["create","list","get","patch","update","delete"]
                    image: "litmuschaos.docker.scarf.sh/litmuschaos/go-runner:3.28.0"
                    imagePullPolicy: Always
                    args:
                    - -c
                    - ./experiments -name pod-delete
                    command:
                    - /bin/bash
                    env:
                    - name: TOTAL_CHAOS_DURATION
                      value: '15'
                    - name: RAMP_TIME
                      value: ''
                    - name: FORCE
                      value: 'true'
                    - name: CHAOS_INTERVAL
                      value: '5'
                    - name: PODS_AFFECTED_PERC
                      value: ''
                    - name: TARGET_PODS
                      value: ''
                    - name: SEQUENCE
                      value: 'parallel'
                    labels:
                      name: pod-delete
      outputs: {{}}
      metadata: {{}}
      container:
        name: ""
        image: litmuschaos/k8s:latest
        command:
          - sh
          - -c
        args:
          - kubectl apply -f /tmp/pod-delete.yaml -n
            {{{{workflow.parameters.adminModeNamespace}}}}
        resources: {{}}
    - name: run-chaos
      inputs:
        artifacts:
          - name: run-chaos
            path: /tmp/chaosengine-run-chaos.yaml
            raw:
              data: >
                apiVersion: litmuschaos.io/v1alpha1

                kind: ChaosEngine

                metadata:
                  namespace: "{{{{workflow.parameters.adminModeNamespace}}}}"
                  labels:
                    context: sock-shop_kube-proxy
                    workflow_run_id: "{{{{ workflow.uid }}}}"
                    workflow_name: {service}-pod-delete
                  generateName: run-chaos
                spec:
                  appinfo:
                    appns: sock-shop
                    applabel: name={service}
                    appkind: deployment
                  jobCleanUpPolicy: retain
                  engineState: active
                  chaosServiceAccount: litmus-admin
                  experiments:
                    - name: pod-delete
                      spec:
                        components:
                          env:
                            - name: TOTAL_CHAOS_DURATION
                              value: "{duration_s}"
                            - name: CHAOS_INTERVAL
                              value: "{interval_s}"
                            - name: FORCE
                              value: "{force}"
                            - name: SEQUENCE
                              value: "parallel"
      outputs: {{}}
      metadata:
        labels:
          weight: "10"
      container:
        name: ""
        image: litmuschaos/litmus-checker:2.11.0
        args:
          - -file=/tmp/chaosengine-run-chaos.yaml
          - -saveName=/tmp/engine-name
        resources: {{}}
    - name: cleanup-chaos-resources
      inputs: {{}}
      outputs: {{}}
      metadata: {{}}
      container:
        name: ""
        image: litmuschaos/k8s:latest
        command:
          - sh
          - -c
        args:
          - kubectl delete chaosengine -l workflow_run_id={{{{workflow.uid}}}} -n
            {{{{workflow.parameters.adminModeNamespace}}}}
        resources: {{}}
  entrypoint: argowf-chaos
  arguments:
    parameters:
      - name: adminModeNamespace
        value: litmus
      - name: appNamespace
        value: sock-shop
  serviceAccountName: argo-chaos
  securityContext:
    runAsUser: 1000
    runAsNonRoot: true
status: {{}}
"""


def _render_pod_delete(service: str, env: Dict[str, Any]) -> str:
    return POD_DELETE_TEMPLATE.format(
        service=service,
        duration_s=int(env.get("total_chaos_duration_seconds", 30)),
        interval_s=int(env.get("chaos_interval_seconds", 10)),
        force=str(bool(env.get("force", False))).lower(),
    )


# ── Extended fault catalog (network / dns / io / http / container) ──────────
# These faults ship a hand-authored baseline workflow under
# ``deploy/kubernetes/manifests-chaos/catalogue-<fault>.yaml``. Unlike the
# memory-hog/cpu-hog baselines (imported verbatim from the Litmus Portal),
# these are authored so that:
#   * the token ``catalogue`` appears ONLY where it identifies the target
#     service (workflow name, ``workflow_name`` label, ``applabel`` and the
#     engine ``TARGET_CONTAINER``) — so a global replace is safe; and
#   * every ChaosExperiment default uses single-quoted env values while the
#     ChaosEngine (the block that actually drives the run) uses double-quoted
#     values — so a substitution like ``value: "60"`` targets the engine only
#     and never the experiment default.
# ``_sanity_check`` still guards against any accidental leftover ``catalogue``.
def _render_from_baseline(
    filename: str,
    service: str,
    engine_value_subs: List[tuple[str, str]],
) -> str:
    src = (CHAOS_MANIFEST_DIR / filename).read_text(encoding="utf-8")
    text = _strip_probe_ref(src)
    text = text.replace("catalogue", service)
    text = _replace_in_order(text, engine_value_subs)
    return text


def _render_network_latency(service: str, env: Dict[str, Any]) -> str:
    latency_ms = int(env.get("network_latency_ms", 2000))
    duration_s = int(env.get("total_chaos_duration_seconds", 60))
    return _render_from_baseline(
        "catalogue-network-latency.yaml",
        service,
        [
            ('value: "2000"', f'value: "{latency_ms}"'),
            ('value: "60"', f'value: "{duration_s}"'),
        ],
    )


def _render_network_loss(service: str, env: Dict[str, Any]) -> str:
    loss_pct = int(env.get("network_packet_loss_percentage", 100))
    duration_s = int(env.get("total_chaos_duration_seconds", 60))
    return _render_from_baseline(
        "catalogue-network-loss.yaml",
        service,
        [
            ('value: "100"', f'value: "{loss_pct}"'),
            ('value: "60"', f'value: "{duration_s}"'),
        ],
    )


def _render_dns_error(service: str, env: Dict[str, Any]) -> str:
    duration_s = int(env.get("total_chaos_duration_seconds", 60))
    return _render_from_baseline(
        "catalogue-dns-error.yaml",
        service,
        [
            ('value: "60"', f'value: "{duration_s}"'),
        ],
    )


def _render_io_stress(service: str, env: Dict[str, Any]) -> str:
    fs_pct = int(env.get("filesystem_utilization_percentage", 10))
    workers = int(env.get("number_of_workers", 4))
    duration_s = int(env.get("total_chaos_duration_seconds", 120))
    return _render_from_baseline(
        "catalogue-io-stress.yaml",
        service,
        [
            ('value: "10"', f'value: "{fs_pct}"'),
            ('value: "4"', f'value: "{workers}"'),
            ('value: "120"', f'value: "{duration_s}"'),
        ],
    )


def _render_http_status_code(service: str, env: Dict[str, Any]) -> str:
    status_code = int(env.get("status_code", 500))
    duration_s = int(env.get("total_chaos_duration_seconds", 60))
    return _render_from_baseline(
        "catalogue-http-status-code.yaml",
        service,
        [
            ('value: "500"', f'value: "{status_code}"'),
            ('value: "60"', f'value: "{duration_s}"'),
        ],
    )


def _render_container_kill(service: str, env: Dict[str, Any]) -> str:
    duration_s = int(env.get("total_chaos_duration_seconds", 20))
    interval_s = int(env.get("chaos_interval_seconds", 10))
    return _render_from_baseline(
        "catalogue-container-kill.yaml",
        service,
        [
            ('value: "20"', f'value: "{duration_s}"'),
            ('value: "10"', f'value: "{interval_s}"'),
        ],
    )


RENDERERS: Dict[str, Callable[[str, Dict[str, Any]], str]] = {
    "memory-hog": _render_memory_hog,
    "cpu-hog": _render_cpu_hog,
    "pod-delete": _render_pod_delete,
    "network-latency": _render_network_latency,
    "network-loss": _render_network_loss,
    "dns-error": _render_dns_error,
    "io-stress": _render_io_stress,
    "http-status-code": _render_http_status_code,
    "container-kill": _render_container_kill,
}


# ── Driver ─────────────────────────────────────────────────────────────────
def _collect_cells(cfg: Dict[str, Any]) -> List[tuple[str, str]]:
    """Unique (service, chaos_type) pairs across train + test experiments."""
    pairs: set[tuple[str, str]] = set()
    for key in ("train_experiments", "test_experiments"):
        for cell in cfg.get(key, []) or []:
            pairs.add((cell["service"], cell["chaos_type"]))
    return sorted(pairs)


def _chaos_env(cfg: Dict[str, Any], chaos_type: str) -> Dict[str, Any]:
    for entry in cfg.get("chaos_types", []) or []:
        if entry.get("name") == chaos_type:
            return entry.get("env") or {}
    raise KeyError(f"chaos_type {chaos_type!r} not declared in config.chaos_types")


def _sanity_check(text: str, service: str, chaos_type: str) -> None:
    """Catch un-replaced 'catalogue' tokens when the target isn't catalogue."""
    if service == "catalogue":
        return
    leftover = [
        ln
        for ln in text.splitlines()
        if "catalogue" in ln.lower() and not ln.strip().startswith("#")
    ]
    if leftover:
        raise RuntimeError(
            f"Unreplaced 'catalogue' tokens for service={service!r} "
            f"chaos_type={chaos_type!r}:\n  " + "\n  ".join(leftover[:5])
        )


def generate(cfg: Dict[str, Any], output_dir: Path) -> List[Path]:
    cells = _collect_cells(cfg)
    if not cells:
        raise RuntimeError(
            "No experiments declared — populate train_experiments / "
            "test_experiments in config.yaml"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for service, chaos_type in cells:
        renderer = RENDERERS.get(chaos_type)
        if renderer is None:
            raise KeyError(
                f"No renderer registered for chaos_type={chaos_type!r}. "
                f"Known: {sorted(RENDERERS)}"
            )
        env = _chaos_env(cfg, chaos_type)
        text = renderer(service, env)
        _sanity_check(text, service, chaos_type)
        out_path = output_dir / f"{service}-{chaos_type}.yaml"
        out_path.write_text(text, encoding="utf-8")
        written.append(out_path)
    return written


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--config",
        default=str(Path(__file__).with_name("config.yaml")),
        help="Path to harness config.yaml",
    )
    args = p.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    output_dir = Path(__file__).with_name("out") / cfg["output"]["manifests_dir"]

    written = generate(cfg, output_dir)
    print(f"Wrote {len(written)} manifest(s) to {output_dir}")
    for path in written:
        print(f"  {path.relative_to(ROOT)}")
    print()
    print("Next: apply them ONCE so Litmus / Argo know the workflow templates:")
    print(f"  kubectl apply -f {output_dir.relative_to(ROOT)}/")
    print(
        "(Each apply triggers one workflow run; the workflow NAME persists "
        "afterwards and the harness clones it for every chaos invocation.)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
