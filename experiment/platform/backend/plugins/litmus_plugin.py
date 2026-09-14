"""Litmus/Argo chaos provider adapter.

``LitmusChaosPlugin`` implements :class:`ports.ChaosProviderPort` by
cloning and launching pre-generated Argo Workflows in the ``litmus``
namespace (via ``kubectl``) to inject faults into target services.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Dict, List, Optional


class LitmusChaosPlugin:
    def validate(self, config: Dict[str, Any]) -> None:
        if config.get("provider") != "litmus":
            raise ValueError("chaos_profile.provider must be 'litmus'")
        if not config.get("namespace"):
            raise ValueError("chaos_profile.namespace is required")
        if not config.get("manifest_path") and not config.get("chaos_engine"):
            raise ValueError(
                "chaos_profile.chaos_engine (Litmus workflow template name, e.g. 'delete-user') "
                "is required when manifest_path is not set"
            )

    def prepare(
        self, context: Dict[str, Any], config: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {"ok": True}

    def inject(self, context: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        manifest_path = config.get("manifest_path")
        namespace = config["namespace"]

        # ── Path 1: local manifest (original behaviour) ──────────────────────
        if manifest_path:
            cmd = ["kubectl", "apply", "-f", str(manifest_path)]
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"Failed to apply chaos manifest: {proc.stderr}")
            return {
                "provider": "litmus",
                "mode": "manifest",
                "namespace": namespace,
                "chaos_engine": config.get("chaos_engine"),
                "chaos_result": config.get("chaos_result"),
                "manifest_path": manifest_path,
                "workflow_name": None,
            }

        # ── Path 2: Litmus UI workflow template (Argo Workflow resubmit) ─────
        workflow_template = config["chaos_engine"]  # e.g. "delete-user"
        created_name = self._submit_workflow(workflow_template)
        return {
            "provider": "litmus",
            "mode": "argo_workflow",
            "namespace": "litmus",  # Argo Workflow lives in litmus ns
            "target_namespace": namespace,  # app target (e.g. sock-shop)
            "workflow_template": workflow_template,
            "workflow_name": created_name,  # actual name, e.g. delete-user-ab3x9
            "chaos_engine": None,
            "chaos_result": None,
            "manifest_path": None,
        }

    def _submit_workflow(self, workflow_template: str) -> str:
        """Find the latest Argo Workflow for *workflow_template*, clone its spec
        and submit a new run. Returns the newly created workflow name."""
        cmd = ["kubectl", "get", "workflows.argoproj.io", "-n", "litmus", "-o", "json"]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"kubectl get workflows failed: {proc.stderr.strip()}")

        data = json.loads(proc.stdout)
        pattern = re.compile(rf"^{re.escape(workflow_template)}-(\d{{13}})$")
        latest_item: Optional[Dict[str, Any]] = None
        latest_ts = 0
        base_item: Optional[Dict[str, Any]] = None
        for item in data.get("items", []):
            name = item["metadata"].get("name", "")
            m = pattern.match(name)
            if m:
                ts = int(m.group(1))
                if ts > latest_ts:
                    latest_ts = ts
                    latest_item = item
            elif name == workflow_template:
                # Fallback: bare template workflow (no timestamp suffix), e.g.
                # one created directly via `kubectl apply` rather than a UI
                # resubmit. Use its spec only if no timestamped instance exists.
                base_item = item

        if latest_item is None:
            latest_item = base_item

        if latest_item is None:
            raise RuntimeError(
                f"No Argo Workflow found for template '{workflow_template}'. "
                "Run the experiment at least once from the Litmus UI to create the template."
            )

        new_wf = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Workflow",
            "metadata": {
                "generateName": f"{workflow_template}-",
                "namespace": "litmus",
                "labels": {
                    k: v
                    for k, v in latest_item["metadata"].get("labels", {}).items()
                    if not k.startswith("workflows.argoproj.io")
                    or k == "workflows.argoproj.io/controller-instanceid"
                },
            },
            "spec": latest_item["spec"],
        }

        create_proc = subprocess.run(
            ["kubectl", "create", "-f", "-"],
            input=json.dumps(new_wf),
            check=False,
            capture_output=True,
            text=True,
        )
        if create_proc.returncode != 0:
            raise RuntimeError(
                f"Failed to submit Argo Workflow: {create_proc.stderr.strip()}"
            )

        # Output: "workflow.argoproj.io/delete-user-ab3x9 created"
        m = re.search(r"/(\S+)\s+created", create_proc.stdout)
        if not m:
            raise RuntimeError(
                f"Could not parse created workflow name from: {create_proc.stdout!r}"
            )
        return m.group(1)

    def stop(self, context: Dict[str, Any], handle: Dict[str, Any]) -> Dict[str, Any]:
        # Argo Workflow mode
        workflow_name = handle.get("workflow_name")
        if workflow_name and handle.get("mode") == "argo_workflow":
            cmd = [
                "kubectl",
                "delete",
                "workflow.argoproj.io",
                "-n",
                "litmus",
                workflow_name,
                "--ignore-not-found=true",
            ]
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
            return {"deleted": proc.returncode == 0, "workflow_name": workflow_name}

        # Local manifest mode
        manifest_path = handle.get("manifest_path")
        if manifest_path:
            cmd = [
                "kubectl",
                "delete",
                "-f",
                str(manifest_path),
                "--ignore-not-found=true",
            ]
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
            return {
                "deleted": proc.returncode == 0,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }

        return {
            "deleted": False,
            "reason": "no workflow_name or manifest_path configured",
        }

    def status(self, context: Dict[str, Any], handle: Dict[str, Any]) -> Dict[str, Any]:
        # ── Argo Workflow mode ────────────────────────────────────────────────
        workflow_name = handle.get("workflow_name")
        if workflow_name and handle.get("mode") == "argo_workflow":
            cmd = [
                "kubectl",
                "get",
                "workflow.argoproj.io",
                "-n",
                "litmus",
                workflow_name,
                "-o",
                "json",
            ]
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
            if proc.returncode != 0:
                return {"state": "pending", "reason": proc.stderr.strip()}

            wf_data = json.loads(proc.stdout)
            phase = wf_data.get("status", {}).get("phase")
            wf_uid = wf_data.get("metadata", {}).get("uid", "")

            # Try to find the ChaosResult created by this workflow run
            chaos_verdict: Optional[str] = None
            chaos_phase: Optional[str] = None
            if wf_uid:
                cr_proc = subprocess.run(
                    [
                        "kubectl",
                        "get",
                        "chaosresults",
                        "-n",
                        "litmus",
                        "-l",
                        f"workflow_run_id={wf_uid}",
                        "-o",
                        "json",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if cr_proc.returncode == 0:
                    cr_items = json.loads(cr_proc.stdout).get("items", [])
                    if cr_items:
                        exp_st = (
                            cr_items[0].get("status", {}).get("experimentStatus", {})
                        )
                        chaos_verdict = exp_st.get("verdict")
                        chaos_phase = exp_st.get("phase")

            state_map = {
                "Running": "running",
                "Pending": "running",
                "Succeeded": "completed",
                "Failed": "failed",
                "Error": "failed",
            }
            return {
                "state": state_map.get(phase or "", "pending"),
                "workflow_phase": phase,
                "chaos_phase": chaos_phase,
                "chaos_verdict": chaos_verdict,
                "litmus_url": f"http://localhost:9091/chaos-center/experiments/run/{workflow_name}",
            }

        # ── Local manifest / ChaosResult mode ────────────────────────────────
        chaos_result = handle.get("chaos_result")
        namespace = handle.get("namespace")
        if not chaos_result or not namespace:
            return {
                "state": "unknown",
                "reason": "chaos_result or namespace not configured",
            }

        cmd = [
            "kubectl",
            "-n",
            str(namespace),
            "get",
            "chaosresult",
            str(chaos_result),
            "-o",
            "json",
        ]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            return {"state": "pending", "reason": proc.stderr.strip()}

        data = json.loads(proc.stdout)
        exp_st = data.get("status", {}).get("experimentStatus", {})
        verdict = exp_st.get("verdict")
        phase = exp_st.get("phase")
        state = (
            "completed"
            if verdict == "Pass"
            else "failed"
            if verdict == "Fail"
            else "running"
        )
        return {"state": state, "phase": phase, "verdict": verdict}

    def collect_artifacts(
        self, context: Dict[str, Any], handle: Dict[str, Any]
    ) -> Dict[str, Any]:
        base = {
            "provider": "litmus",
            "mode": handle.get("mode"),
            "namespace": handle.get("namespace"),
        }
        if handle.get("mode") == "argo_workflow":
            base.update(
                {
                    "workflow_template": handle.get("workflow_template"),
                    "workflow_name": handle.get("workflow_name"),
                    "litmus_ui_url": "http://localhost:9091",
                }
            )
        else:
            base.update(
                {
                    "chaos_engine": handle.get("chaos_engine"),
                    "chaos_result": handle.get("chaos_result"),
                }
            )
        return base

    def list_catalog(self, namespaces: List[str] | None = None) -> List[Dict[str, Any]]:
        """Return one entry per unique Litmus experiment template.

        Litmus stores experiments as Argo Workflows in the ``litmus`` namespace.
        Each workflow name ends with a 13-digit timestamp suffix; we deduplicate
        by prefix and keep the most recent run per template so the caller gets
        exactly the set visible in the Litmus UI.
        """
        import re

        target_ns = namespaces[0] if namespaces else "litmus"
        cmd = [
            "kubectl",
            "get",
            "workflows.argoproj.io",
            "-n",
            target_ns,
            "-o",
            "json",
        ]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"kubectl get workflows failed: {proc.stderr.strip()}")

        data = json.loads(proc.stdout)
        # Keep latest workflow per template prefix (strip trailing 13-digit timestamp)
        latest: Dict[str, Any] = {}
        for item in data.get("items", []):
            wf_name = item.get("metadata", {}).get("name", "")
            m = re.match(r"^(.+)-(\d{13})$", wf_name)
            prefix = m.group(1) if m else wf_name
            ts = int(m.group(2)) if m else 0
            prev = latest.get(prefix)
            if prev is None or ts > prev["_ts"]:
                item["_ts"] = ts
                item["_prefix"] = prefix
                latest[prefix] = item

        results: List[Dict[str, Any]] = []
        for prefix, item in sorted(latest.items()):
            templates = item.get("spec", {}).get("templates", [])
            engine_info: Dict[str, Any] = {
                "engine_name": None,
                "engine_namespace": target_ns,
                "workflow_name": prefix,
                "app_namespace": None,
                "app_label": None,
                "experiment_types": [],
                "engine_state": item.get("status", {}).get("phase", "Unknown"),
            }
            # Scan template artifacts for embedded ChaosEngine YAML
            for tmpl in templates:
                for art in tmpl.get("inputs", {}).get("artifacts", []):
                    raw = art.get("raw", {}).get("data", "")
                    if "ChaosEngine" not in raw:
                        continue
                    for line in raw.splitlines():
                        line = line.strip()
                        if line.startswith("workflow_name:"):
                            engine_info["workflow_name"] = (
                                line.split(":", 1)[-1].strip().strip("'\"")
                            )
                        elif line.startswith("appns:"):
                            engine_info["app_namespace"] = line.split(":", 1)[
                                -1
                            ].strip()
                        elif line.startswith("applabel:"):
                            engine_info["app_label"] = line.split(":", 1)[-1].strip()
                        elif line.startswith("generateName:"):
                            engine_info["engine_name"] = line.split(":", 1)[-1].strip()
                    # Extract experiment names from the embedded spec
                    try:
                        # Attempt YAML parse if pyyaml available, else regex
                        import yaml  # type: ignore[import]

                        eng = yaml.safe_load(raw)
                        exps = [
                            e.get("name")
                            for e in (eng or {}).get("spec", {}).get("experiments", [])
                            if isinstance(e, dict) and e.get("name")
                        ]
                        engine_info["experiment_types"] = exps
                    except Exception:
                        exp_matches = re.findall(
                            r"^\s*-\s+name:\s+(.+)$", raw, re.MULTILINE
                        )
                        engine_info["experiment_types"] = exp_matches
                    break
            results.append(engine_info)
        return results
