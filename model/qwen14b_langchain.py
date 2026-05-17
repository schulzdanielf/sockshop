import requests

from langchain_core.language_models.llms import LLM
from typing import Optional, List


class Qwen14BAPI(LLM):
    """
    LangChain wrapper para sua API local FastAPI + ExLlamaV2
    """

    api_url: str = "http://localhost:8001/generate"

    @property
    def _llm_type(self) -> str:
        return "qwen14b_api"

    def _call(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager=None,
        **kwargs
    ) -> str:

        max_tokens = kwargs.get("max_new_tokens", 512)

        response = requests.post(
            self.api_url,
            json={
                "prompt": prompt,
                "max_new_tokens": max_tokens
            },
            timeout=300
        )

        response.raise_for_status()

        text = response.json()["response"]

        # LangChain stop handling simples
        if stop:
            for s in stop:
                text = text.split(s)[0]

        return text