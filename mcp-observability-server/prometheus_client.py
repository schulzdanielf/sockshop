"""Prometheus client for querying metrics."""
import httpx
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from config import settings


class PrometheusClient:
    """Client for querying Prometheus."""
    
    def __init__(self, base_url: str = settings.prometheus_url, timeout: int = settings.prometheus_timeout):
        """Initialize Prometheus client.
        
        Args:
            base_url: Prometheus base URL
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip('/')
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
                f"{self.base_url}/api/v1/query",
                params={"query": query}
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
        step: str = "1m"
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
                params={
                    "query": query,
                    "start": start,
                    "end": end,
                    "step": step
                }
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            return {"status": "error", "error": str(e)}
    
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
                f"{self.base_url}/api/v1/series",
                params={"match[]": match}
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            return {"status": "error", "error": str(e)}
    
    def close(self):
        """Close the HTTP client."""
        self.client.close()
