#!/usr/bin/env python3
"""Generate per-service `pod-memory-hog` Argo Workflow manifests.

We take the existing `deploy/kubernetes/manifests-chaos/catalogue-memory-hog.yaml`
as a baseline and substitute the catalogue-specific tokens for each target
service declared in `config.yaml`. The output files can be applied once with::

    kubectl apply -f experiment/eval/memory_hog/out/manifests/

After that, every workflow template name (e.g. ``orders-memory-hog``) exists
in the cluster and the platform's Litmus plugin can resubmit it via the
``chaos_engine`` mode (no manual UI step needed afterwards).

This script is **idempotent** — running it again just overwrites the YAMLs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

import yaml

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_PATH = ROOT / "deploy" / "kubernetes" / "manifests-chaos" / "catalogue-memory-hog.yaml"


def _render(template_text: str, service: str, mem_mb: int, duration_s: int) -> str:
    """Replace catalogue-specific tokens with `service`-specific ones.

    The catalogue baseline contains exactly these catalogue references:
      * workflow `metadata.name`: ``catalogue-memory-hog``
      * workflow_name label: ``catalogue-memory-hog``
      * probeRef annotation: ``catalogue-delay`` (probe — strip it, we don't
        ship custom probes from the harness)
      * ChaosEngine applabel: ``name=catalogue``
      * TARGET_CONTAINER: ``catalogue``

    We rely on plain string replacement because the YAML embeds large raw
    blocks of YAML-as-text (Argo ``raw.data``) where yaml.safe_load would
    re-quote everything and break Litmus parsing.
    """
    text = template_text

    # Strip the probeRef annotation line — the catalogue-delay probe is not
    # shipped with the harness and would fail the workflow.
    text = "\n".join(
        line for line in text.splitlines() if "probeRef" not in line
    )

    # Order matters: replace the most specific tokens first.
    replacements = [
        ("catalogue-memory-hog", f"{service}-memory-hog"),
        ("applabel: name=catalogue", f"applabel: name={service}"),
        ("- name: TARGET_CONTAINER\n                              value: catalogue",
         f"- name: TARGET_CONTAINER\n                              value: {service}"),
        # Chaos parameters
        ("- name: MEMORY_CONSUMPTION\n                              value: \"60\"",
         f"- name: MEMORY_CONSUMPTION\n                              value: \"{mem_mb}\""),
        ("- name: TOTAL_CHAOS_DURATION\n                              value: \"30\"",
         f"- name: TOTAL_CHAOS_DURATION\n                              value: \"{duration_s}\""),
    ]
    for old, new in replacements:
        text = text.replace(old, new)

    # Sanity check (skip when service IS catalogue — no rewrite happens).
    if service != "catalogue":
        leftover = [
            ln for ln in text.splitlines()
            if "catalogue" in ln.lower() and not ln.strip().startswith("#")
        ]
        if leftover:
            raise RuntimeError(
                f"Unreplaced 'catalogue' tokens for service={service!r}:\n  "
                + "\n  ".join(leftover[:5])
            )
    return text


def generate(
    services: List[str],
    output_dir: Path,
    mem_mb: int,
    duration_s: int,
) -> List[Path]:
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Template not found: {TEMPLATE_PATH}")
    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")

    output_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for svc in services:
        rendered = _render(template_text, svc, mem_mb, duration_s)
        out_path = output_dir / f"{svc}-memory-hog.yaml"
        out_path.write_text(rendered, encoding="utf-8")
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
    services = sorted(set(cfg["train_services"] + cfg["test_services"]))
    output_dir = Path(__file__).with_name("out") / cfg["output"]["manifests_dir"]
    mem_mb = int(cfg["chaos"]["memory_consumption_mb"])
    duration_s = int(cfg["chaos"]["total_chaos_duration_seconds"])

    written = generate(services, output_dir, mem_mb, duration_s)
    print(f"Wrote {len(written)} manifest(s) to {output_dir}")
    for path in written:
        print(f"  {path.relative_to(ROOT)}")
    print()
    print("Next: apply them ONCE so Litmus knows the workflow templates:")
    print(f"  kubectl apply -f {output_dir.relative_to(ROOT)}/")
    print("(Each apply will trigger one workflow run — that's expected; "
          "the workflow name persists afterwards and the harness clones it.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
