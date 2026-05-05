"""Configuration for observability services."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)
    
    # Prometheus settings
    prometheus_url: str = "http://localhost:9090"
    prometheus_timeout: int = 30
    
    # Loki settings
    loki_url: str = "http://localhost:3100"
    loki_timeout: int = 30
    
    # Server settings
    host: str = "0.0.0.0"
    port: int = 8000


settings = Settings()
