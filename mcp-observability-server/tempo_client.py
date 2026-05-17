"""Tempo client for searching and retrieving traces."""
import httpx
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
from config import settings


def _now_unix() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _ago_unix(minutes: int = 60) -> int:
    return int((datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp())


class TempoClient:
    """Client for querying Tempo traces."""

    def __init__(self, base_url: str = settings.tempo_url, timeout: int = settings.tempo_timeout):
        """Initialize Tempo client.

        Args:
            base_url: Tempo base URL
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.client = httpx.Client(timeout=timeout)

    def search_traces(
        self,
        query: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 20,
        min_duration_ms: Optional[int] = None,
        max_duration_ms: Optional[int] = None,
        service_name: Optional[str] = None,
        last_minutes: int = 60,
    ) -> Dict[str, Any]:
        """Search traces in Tempo.

        Args:
            query: Optional TraceQL query (e.g., '{ status = error }').
            start: Start time as Unix timestamp (int) or RFC3339 string.
                   Defaults to `last_minutes` ago.
            end: End time as Unix timestamp (int) or RFC3339 string.
                 Defaults to now.
            limit: Maximum number of traces to return.
            min_duration_ms: Optional minimum trace duration in milliseconds.
            max_duration_ms: Optional maximum trace duration in milliseconds.
            service_name: Optional service name filter.
            last_minutes: How many minutes back to search when start/end are
                          not provided (default: 60).

        Returns:
            Search results from Tempo.
        """
        params: Dict[str, Any] = {
            "limit": limit,
            "start": int(start) if start and str(start).isdigit() else (int(datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()) if start else _ago_unix(last_minutes)),
            "end":   int(end)   if end   and str(end).isdigit()   else (int(datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp())   if end   else _now_unix()),
        }

        if min_duration_ms is not None:
            params["minDuration"] = f"{min_duration_ms}ms"
        if max_duration_ms is not None:
            params["maxDuration"] = f"{max_duration_ms}ms"
        if query:
            params["q"] = query
        elif service_name:
            params["q"] = f'{{resource.service.name="{service_name}"}}'

        try:
            response = self.client.get(f"{self.base_url}/api/search", params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            return {"status": "error", "error": str(e)}

    def get_trace(self, trace_id: str) -> Dict[str, Any]:
        """Get a full trace by ID from Tempo.

        Args:
            trace_id: Tempo trace ID.

        Returns:
            Trace payload from Tempo.
        """
        try:
            response = self.client.get(f"{self.base_url}/api/traces/{trace_id}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            return {"status": "error", "error": str(e)}

    def close(self):
        """Close the HTTP client."""
        self.client.close()