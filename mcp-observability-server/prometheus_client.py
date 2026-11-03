"""Prometheus client for querying metrics."""
from datetime import datetime, timedelta
import math
from typing import Any, Dict, Optional

import httpx
from config import settings


class PrometheusClient:
    """Client for querying Prometheus."""

    def __init__(
        self,
        base_url: str = settings.prometheus_url,
        timeout: int = settings.prometheus_timeout,
    ):
        """Initialize Prometheus client.

        Args:
            base_url: Prometheus base URL
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.client = httpx.Client(timeout=timeout)

    def query(self, query: str) -> Dict[str, Any]:
        """Execute an instant query against Prometheus.

        Args:
            query: PromQL query string

        Returns:
            Query result from Prometheus

        Raises:
            httpx.HTTPError: If request fails
        """
        try:
            response = self.client.get(
                f"{self.base_url}/api/v1/query", params={"query": query}
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            return {"status": "error", "error": str(e)}

    def query_range(
        self,
        query: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        step: str = "1m",
    ) -> Dict[str, Any]:
        """Execute a range query against Prometheus.

        Args:
            query: PromQL query string
            start: Start time (ISO format or duration). Defaults to 1 hour ago
            end: End time (ISO format or duration). Defaults to now
            step: Query resolution step

        Returns:
            Query result from Prometheus

        Raises:
            httpx.HTTPError: If request fails
        """
        if not start:
            start = (datetime.utcnow() - timedelta(hours=1)).isoformat() + "Z"
        if not end:
            end = datetime.utcnow().isoformat() + "Z"

        try:
            response = self.client.get(
                f"{self.base_url}/api/v1/query_range",
                params={"query": query, "start": start, "end": end, "step": step},
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            return {"status": "error", "error": str(e)}

    def get_scrape_targets(self) -> Dict[str, Any]:
        try:
            response = self.client.get(
                f"{self.base_url}/api/v1/targets", params={"state": "active"}
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            return {"status": "error", "error": str(exc)}

    def query_raw_range(self, selector: str, start: str, end: str) -> Dict[str, Any]:
        """Fetch actual samples in ``(start, end]``, without evaluation lookback.

        Unlike query_range, an instant range-vector selector preserves scrape
        timestamps. This internal API accepts a metric selector, not arbitrary
        PromQL expressions, and evaluates it at the requested historical end.
        """
        start_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_time = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if start_time.utcoffset() is None or end_time.utcoffset() is None:
            raise ValueError("raw range timestamps must include a timezone")
        duration_ms = math.ceil((end_time - start_time).total_seconds() * 1000)
        if duration_ms <= 0:
            raise ValueError("raw range end must be after start")
        try:
            response = self.client.get(
                f"{self.base_url}/api/v1/query",
                params={"query": f"{selector}[{duration_ms}ms]", "time": end},
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            return {"status": "error", "error": str(exc)}

    def get_metrics(self) -> Dict[str, Any]:
        """Get list of available metrics.

        Returns:
            List of metric names

        Raises:
            httpx.HTTPError: If request fails
        """
        try:
            response = self.client.get(f"{self.base_url}/api/v1/label/__name__/values")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            return {"status": "error", "error": str(e)}

    def get_series(self, match: str) -> Dict[str, Any]:
        """Get time series matching a pattern.

        Args:
            match: Series matcher pattern (e.g., '{job="prometheus"}')

        Returns:
            List of series

        Raises:
            httpx.HTTPError: If request fails
        """
        try:
            response = self.client.get(
                f"{self.base_url}/api/v1/series", params={"match[]": match}
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            return {"status": "error", "error": str(e)}

    def close(self):
        """Close the HTTP client."""
        self.client.close()
