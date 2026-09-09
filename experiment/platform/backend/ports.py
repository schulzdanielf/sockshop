"""Port definitions (Protocols) for the hexagonal architecture.

Declares the abstract contracts the engine depends on — chaos, load,
metrics and trace providers, storage and notifications. Concrete adapters
(under ``plugins/``, ``storage/`` and ``adapters/``) implement these
Protocols, so the application core stays decoupled from infrastructure.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

from .domain import DomainEvent, ExperimentVersion, RunRecord


class ChaosProviderPort(Protocol):
    def validate(self, config: Dict[str, Any]) -> None:
        ...

    def prepare(
        self, context: Dict[str, Any], config: Dict[str, Any]
    ) -> Dict[str, Any]:
        ...

    def inject(self, context: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def stop(self, context: Dict[str, Any], handle: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def status(self, context: Dict[str, Any], handle: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def collect_artifacts(
        self, context: Dict[str, Any], handle: Dict[str, Any]
    ) -> Dict[str, Any]:
        ...


class LoadProviderPort(Protocol):
    def validate(self, config: Dict[str, Any]) -> None:
        ...

    def prepare(
        self, context: Dict[str, Any], config: Dict[str, Any]
    ) -> Dict[str, Any]:
        ...

    def start_load(
        self, context: Dict[str, Any], config: Dict[str, Any]
    ) -> Dict[str, Any]:
        ...

    def stop_load(
        self, context: Dict[str, Any], handle: Dict[str, Any]
    ) -> Dict[str, Any]:
        ...

    def status(self, context: Dict[str, Any], handle: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def collect_summary(
        self, context: Dict[str, Any], handle: Dict[str, Any]
    ) -> Dict[str, Any]:
        ...


class MetricsProviderPort(Protocol):
    def validate(self, config: Dict[str, Any]) -> None:
        ...

    def collect_window(
        self,
        context: Dict[str, Any],
        config: Dict[str, Any],
        start_iso: str,
        end_iso: str,
        step_seconds: int,
    ) -> Dict[str, Any]:
        ...

    def summarize(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        ...


class TraceProviderPort(Protocol):
    def validate(self, config: Dict[str, Any]) -> None:
        ...

    def collect_window(
        self,
        context: Dict[str, Any],
        config: Dict[str, Any],
        start_iso: str,
        end_iso: str,
    ) -> Dict[str, Any]:
        ...

    def summarize(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        ...


class StoragePort(Protocol):
    def create_experiment(
        self, created_by: str, spec: Dict[str, Any]
    ) -> ExperimentVersion:
        ...

    def list_experiments(self) -> List[Dict[str, Any]]:
        ...

    def get_experiment(
        self, experiment_id: str, version: Optional[int] = None
    ) -> ExperimentVersion:
        ...

    def create_run(
        self, run: RunRecord, idempotency_key: Optional[str] = None
    ) -> RunRecord:
        ...

    def get_run(self, run_id: str) -> RunRecord:
        ...

    def list_runs(self, experiment_id: Optional[str] = None) -> List[RunRecord]:
        ...

    def update_run(self, run: RunRecord) -> None:
        ...

    def append_event(self, event: DomainEvent) -> None:
        ...

    def list_events(self, run_id: str) -> List[DomainEvent]:
        ...

    def save_artifact(self, run_id: str, name: str, content: Dict[str, Any]) -> str:
        ...


class NotificationPort(Protocol):
    def notify(self, event_type: str, payload: Dict[str, Any]) -> None:
        ...
