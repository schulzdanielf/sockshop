"""Opt-in, bounded log snapshot capture and snapshot-first replay support."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import httpx
from exceptiongroup import ExceptionGroup
from mcp.shared.exceptions import McpError

from experiment.platform.backend.analysis.system_card import (
    SystemCardError,
    load_system_card,
)
from experiment.platform.backend.plugins.mcp_client import MCPToolClient

def _reference_windows(observation_window: Dict[str, Any]) -> tuple[Dict[str, Dict[str, str]], str]:
    from experiment.platform.backend.agent_console import rca_reference_phases

    phases = observation_window.get("phases") or {}
    selected = {
        name: phase
        for name, phase in phases.items()
        if name in {"baseline", "fault", "post"} and isinstance(phase, dict)
    }
    return rca_reference_phases(
        selected, baseline_source=(observation_window.get("phase_sources") or {}).get("baseline"),
    )


class MCPRcaEvidenceSnapshotPlugin:
    def __init__(self, client_factory: Callable[..., Any] = MCPToolClient):
        self._client_factory = client_factory

    def capture(
        self,
        context: Dict[str, Any],
        observability_config: Dict[str, Any],
        namespace: str,
        observation_window: Dict[str, Any],
        capture_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        metrics_config = observability_config.get("metrics") or {}
        traces_config = observability_config.get("traces") or {}
        mcp_url = metrics_config.get("mcp_sse_url") or traces_config.get("mcp_sse_url")
        try:
            services = sorted(
                (load_system_card(namespace).get("services") or {}).keys()
            )
        except SystemCardError:
            services = []
        windows, reference_source = _reference_windows(observation_window)
        if not mcp_url or not services or not {"baseline", "fault"}.issubset(windows):
            return {
                "schema_version": 1,
                "status": "tool_unavailable",
                "source": "mcp_loki",
                "run_id": context.get("run_id"),
                "namespace": namespace,
                "phase_windows": windows,
                "reference_source": reference_source,
            }

        max_total_lines = capture_config.get("max_total_lines", 1200)
        if (
            isinstance(max_total_lines, bool)
            or not isinstance(max_total_lines, int)
            or not 1 <= max_total_lines <= 1200
        ):
            return {
                "schema_version": 1,
                "status": "invalid_config",
                "source": "mcp_loki",
                "run_id": context.get("run_id"),
                "namespace": namespace,
                "phase_windows": windows,
                "reference_source": reference_source,
            }

        timeout_seconds = capture_config.get("timeout_seconds", 10)
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
            timeout_seconds = 10
        timeout_seconds = min(max(timeout_seconds, 1), 15)
        arguments = {
            "services": services,
            "namespace": namespace,
            "baseline_start": windows["baseline"]["start"],
            "baseline_end": windows["baseline"]["end"],
            "fault_start": windows["fault"]["start"],
            "fault_end": windows["fault"]["end"],
            "max_total_lines": max_total_lines,
        }
        if isinstance(windows.get("post"), dict):
            arguments["post_start"] = windows["post"]["start"]
            arguments["post_end"] = windows["post"]["end"]

        try:
            client = self._client_factory(
                sse_url=str(mcp_url), timeout_seconds=timeout_seconds
            )
            result = client.call_tool_json(
                "capture_run_log_snapshot",
                arguments,
                timeout_seconds=timeout_seconds,
            )
        except (
            asyncio.TimeoutError,
            ExceptionGroup,
            httpx.HTTPError,
            McpError,
            OSError,
            RuntimeError,
            SystemCardError,
            TypeError,
            ValueError,
        ) as exc:
            return {
                "schema_version": 1,
                "status": "tool_error",
                "source": "mcp_loki",
                "run_id": context.get("run_id"),
                "namespace": namespace,
                "phase_windows": windows,
                "reference_source": reference_source,
                "error_type": type(exc).__name__,
            }

        if not isinstance(result, dict) or result.get("isError"):
            return {
                "schema_version": 1,
                "status": "tool_error",
                "source": "mcp_loki",
                "run_id": context.get("run_id"),
                "namespace": namespace,
                "phase_windows": windows,
                "reference_source": reference_source,
                "error_type": "invalid_tool_response",
            }

        return {
            "schema_version": 1,
            "status": result.get("status", "unknown"),
            "source": "mcp_loki",
            "run_id": context.get("run_id"),
            "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "namespace": namespace,
            "phase_windows": windows,
            "reference_source": reference_source,
            "budget": result.get("budget", {}),
            "services": result.get("services", {}),
        }


class SnapshotPreferredMCPClient:
    """Serve matching log evidence from a snapshot and delegate all else live."""

    def __init__(self, live_client: Any, snapshot: Optional[Dict[str, Any]]):
        self._live_client = live_client
        self._snapshot = snapshot or {}

    def list_tools(self):
        return self._live_client.list_tools()

    def call_tool_json(self, name: str, arguments: Optional[Dict[str, Any]] = None, **kwargs):
        if name == "check_service_logs" and isinstance(arguments, dict):
            snapshot_result = self._matching_snapshot_result(arguments)
            if snapshot_result is not None:
                return {**snapshot_result, "evidence_source": "snapshot"}
        return self._live_client.call_tool_json(name, arguments, **kwargs)

    def _matching_snapshot_result(self, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if self._snapshot.get("schema_version") != 1:
            return None
        if self._snapshot.get("namespace") != arguments.get("namespace"):
            return None
        service = arguments.get("service")
        result = (self._snapshot.get("services") or {}).get(service)
        if not isinstance(result, dict):
            return None
        if result.get("status") not in {"ok", "partial", "no_data"}:
            return None
        windows = self._snapshot.get("phase_windows") or {}
        expected_phases = ["baseline", "fault"]
        if arguments.get("post_start") and arguments.get("post_end"):
            expected_phases.append("post")
        for phase in expected_phases:
            window = windows.get(phase) or {}
            if (
                window.get("start") != arguments.get(f"{phase}_start")
                or window.get("end") != arguments.get(f"{phase}_end")
                or phase not in (result.get("phases") or {})
            ):
                return None
        return result
