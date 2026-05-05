"""Loki client for querying logs."""
import httpx
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from config import settings


class LokiClient:
    """Client for querying Loki logs."""
    
    def __init__(self, base_url: str = settings.loki_url, timeout: int = settings.loki_timeout):
        """Initialize Loki client.
        
        Args:
            base_url: Loki base URL
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.client = httpx.Client(timeout=timeout)
    
    def query_range(
        self,
        query: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 1000
    ) -> Dict[str, Any]:
        """Execute a range query against Loki.
        
        Args:
            query: LogQL query string
            start: Start time (Unix timestamp in nanoseconds). Defaults to 1 hour ago
            end: End time (Unix timestamp in nanoseconds). Defaults to now
            limit: Maximum number of log lines
            
        Returns:
            Query result from Loki
            
        Raises:
            httpx.HTTPError: If request fails
        """
        if not start:
            start = str(int((datetime.utcnow() - timedelta(hours=1)).timestamp() * 1e9))
        if not end:
            end = str(int(datetime.utcnow().timestamp() * 1e9))
        
        try:
            response = self.client.get(
                f"{self.base_url}/loki/api/v1/query_range",
                params={
                    "query": query,
                    "start": start,
                    "end": end,
                    "limit": limit
                }
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            return {"status": "error", "error": str(e)}
    
    def query(self, query: str, limit: int = 1000) -> Dict[str, Any]:
        """Execute an instant query against Loki.
        
        Args:
            query: LogQL query string
            limit: Maximum number of log lines
            
        Returns:
            Query result from Loki
            
        Raises:
            httpx.HTTPError: If request fails
        """
        try:
            response = self.client.get(
                f"{self.base_url}/loki/api/v1/query",
                params={
                    "query": query,
                    "limit": limit
                }
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            return {"status": "error", "error": str(e)}
    
    def get_labels(self) -> Dict[str, Any]:
        """Get available label names.
        
        Returns:
            List of label names
            
        Raises:
            httpx.HTTPError: If request fails
        """
        try:
            response = self.client.get(f"{self.base_url}/loki/api/v1/labels")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            return {"status": "error", "error": str(e)}
    
    def get_label_values(self, label: str) -> Dict[str, Any]:
        """Get available values for a label.
        
        Args:
            label: Label name
            
        Returns:
            List of label values
            
        Raises:
            httpx.HTTPError: If request fails
        """
        try:
            response = self.client.get(f"{self.base_url}/loki/api/v1/label/{label}/values")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            return {"status": "error", "error": str(e)}
    
    def close(self):
        """Close the HTTP client."""
        self.client.close()
