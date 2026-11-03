"""Litmus/Argo chaos provider adapter.

``LitmusChaosPlugin`` implements :class:`ports.ChaosProviderPort` by
cloning and launching pre-generated Argo Workflows in the ``litmus``
namespace (via ``kubectl``) to inject faults into target services.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class LitmusChaosPlugin:
    _STATUS_POLL_INTERVAL_SECONDS = 2
    _STATUS_WAIT_TIMEOUT_SECONDS = 120

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

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
                "injected_at": self._now(),
                "apply": {
                    "returncode": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                },
            }

        # ── Path 2: Litmus UI workflow template (Argo Workflow resubmit) ─────
        workflow_template = config["chaos_engine"]  # e.g. "delete-user"
        resource_chaos = config.get("resource_chaos")
        created_name = self._submit_workflow(
            workflow_template,
            resource_chaos,
            config.get("fault_duration_seconds"),
            template_snapshot=config.get("template_snapshot"),
        )
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
            "injected_at": self._now(),
            "resource_chaos": resource_chaos,
        }

    @staticmethod
    def _apply_resource_chaos(spec: Dict[str, Any], resource_chaos: Dict[str, Any]) -> None:
        import yaml

        experiment_name = resource_chaos["experiment_name"]
        overrides = resource_chaos["environment"]
        updated = 0
        for template in spec.get("templates", []):
            for artifact in template.get("inputs", {}).get("artifacts", []):
                raw = artifact.get("raw", {}).get("data")
                if not isinstance(raw, str) or "kind: ChaosEngine" not in raw:
                    continue
                engine = yaml.safe_load(raw)
                for experiment in engine.get("spec", {}).get("experiments", []):
                    if experiment.get("name") != experiment_name:
                        continue
                    env = experiment.setdefault("spec", {}).setdefault(
                        "components", {}
                    ).setdefault("env", [])
                    for name, value in overrides.items():
                        setting = next(
                            (item for item in env if item.get("name") == name), None
                        )
                        if setting is None:
                            env.append({"name": name, "value": str(value)})
                        else:
                            setting["value"] = str(value)
                    artifact["raw"]["data"] = yaml.safe_dump(
                        engine, sort_keys=False, default_flow_style=False
                    )
                    updated += 1
        if updated != 1:
            raise RuntimeError(
                f"Expected one embedded {experiment_name!r} ChaosEngine, found {updated}"
            )

    @staticmethod
    def _apply_fault_duration(spec: Dict[str, Any], duration_seconds: int) -> None:
        import yaml

        updated = 0
        for template in spec.get("templates", []):
            for artifact in template.get("inputs", {}).get("artifacts", []):
                raw = artifact.get("raw", {}).get("data")
                if not isinstance(raw, str) or "kind: ChaosEngine" not in raw:
                    continue
                engine = yaml.safe_load(raw)
                for experiment in engine.get("spec", {}).get("experiments", []):
                    env = experiment.setdefault("spec", {}).setdefault(
                        "components", {}
                    ).setdefault("env", [])
                    setting = next(
                        (item for item in env if item.get("name") == "TOTAL_CHAOS_DURATION"),
                        None,
                    )
                    if setting is None:
                        env.append(
                            {
                                "name": "TOTAL_CHAOS_DURATION",
                                "value": str(duration_seconds),
                            }
                        )
                    else:
                        setting["value"] = str(duration_seconds)
                    updated += 1
                artifact["raw"]["data"] = yaml.safe_dump(
                    engine, sort_keys=False, default_flow_style=False
                )
        if updated == 0:
            raise RuntimeError("Expected an embedded ChaosEngine to set its duration")

    def _submit_workflow(
        self,
        workflow_template: str,
        resource_chaos: Optional[Dict[str, Any]] = None,
        fault_duration_seconds: Optional[int] = None,
        *,
        template_snapshot: Optional[Dict[str, Any]] = None,
    ) -> str:
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

        if template_snapshot is not None:
            if template_snapshot.get("metadata", {}).get("name") != workflow_template:
                raise RuntimeError("Reviewed workflow snapshot does not match requested template")
            latest_item = template_snapshot
        workflow_spec = copy.deepcopy(latest_item["spec"])
        if resource_chaos:
            self._apply_resource_chaos(workflow_spec, resource_chaos)
        elif fault_duration_seconds is not None:
            self._apply_fault_duration(workflow_spec, int(fault_duration_seconds))

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
            "spec": workflow_spec,
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
            try:
                final_status = self._wait_for_terminal_status(context, handle)
            except (RuntimeError, json.JSONDecodeError, OSError) as exc:
                final_status = {"state": "unknown", "reason": str(exc)}
            handle["final_status"] = final_status
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
            result = {
                "deleted": proc.returncode == 0,
                "workflow_name": workflow_name,
                "stopped_at": self._now(),
                "cleanup": {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr},
            }
            handle.update(result)
            return result

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
            result = {
                "deleted": proc.returncode == 0,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "stopped_at": self._now(),
                "cleanup": {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr},
            }
            handle.update(result)
            return result

        return {
            "deleted": False,
            "reason": "no workflow_name or manifest_path configured",
        }

    def _wait_for_terminal_status(
        self, context: Dict[str, Any], handle: Dict[str, Any]
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + self._STATUS_WAIT_TIMEOUT_SECONDS
        terminal_workflow_phases = {"Succeeded", "Failed", "Error"}
        history = handle.setdefault("stop_status_history", [])
        while True:
            status = self.status(context, handle)
            history.append({"observed_at": self._now(), **status})
            if status.get("workflow_phase") in terminal_workflow_phases:
                return status
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    **status,
                    "terminal_wait_timed_out": True,
                    "terminal_wait_timeout_seconds": self._STATUS_WAIT_TIMEOUT_SECONDS,
                }
            time.sleep(min(self._STATUS_POLL_INTERVAL_SECONDS, remaining))

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
            handle["workflow_before_delete"] = wf_data
            phase = wf_data.get("status", {}).get("phase")
            wf_uid = wf_data.get("metadata", {}).get("uid", "")
            # Argo step that hosts the Litmus runner: its lifetime brackets the
            # real injection even when the ChaosResult is not yet terminal.
            step_started_at: Optional[str] = None
            step_finished_at: Optional[str] = None
            for node in (wf_data.get("status", {}).get("nodes") or {}).values():
                if node.get("type") == "Pod" and node.get("templateName") == "run-chaos":
                    step_started_at = node.get("startedAt")
                    step_finished_at = node.get("finishedAt")
                    break

            # Try to find the ChaosResult created by this workflow run
            chaos_verdict: Optional[str] = None
            chaos_phase: Optional[str] = None
            chaos_started_at: Optional[str] = None
            chaos_result_created_at: Optional[str] = None
            chaos_window_confirmed = False
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
                        chaos_result_created_at = cr_items[0].get("metadata", {}).get(
                            "creationTimestamp"
                        )
                        exp_st = (
                            cr_items[0].get("status", {}).get("experimentStatus", {})
                        )
                        chaos_verdict = exp_st.get("verdict")
                        chaos_phase = exp_st.get("phase")
                        # The experiment pod creates the ChaosResult when it starts
                        # injecting; Running is enough to trust the start time.
                        chaos_window_confirmed = chaos_phase in {
                            "Running",
                            "Completed",
                            "Completed_With_Probe_Failure",
                        }
                        if chaos_window_confirmed:
                            chaos_started_at = chaos_result_created_at

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
                "chaos_started_at": chaos_started_at,
                "chaos_result_created_at": chaos_result_created_at,
                "chaos_window_confirmed": chaos_window_confirmed,
                "chaos_step_started_at": step_started_at,
                "chaos_step_finished_at": step_finished_at,
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
                    "injected_at": handle.get("injected_at"),
                    "stopped_at": handle.get("stopped_at"),
                    "final_status": handle.get("final_status"),
                    "stop_status_history": handle.get("stop_status_history"),
                    "workflow_before_delete": handle.get("workflow_before_delete"),
                    "cleanup": handle.get("cleanup"),
                }
            )
        else:
            base.update(
                {
                    "chaos_engine": handle.get("chaos_engine"),
                    "chaos_result": handle.get("chaos_result"),
                    "manifest_path": handle.get("manifest_path"),
                    "injected_at": handle.get("injected_at"),
                    "stopped_at": handle.get("stopped_at"),
                    "apply": handle.get("apply"),
                    "cleanup": handle.get("cleanup"),
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
