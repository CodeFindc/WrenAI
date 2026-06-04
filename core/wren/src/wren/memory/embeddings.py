"""Embedding function abstraction for Wren Memory.

Supports local sentence-transformers or any OpenAI-compatible custom endpoint.
"""

from __future__ import annotations

import contextlib
import os
import httpx

_DEFAULT_PROVIDER = os.getenv("WREN_EMBEDDING_PROVIDER", "sentence-transformers")
_DEFAULT_MODEL = os.getenv(
    "WREN_EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"
)
_DEFAULT_DIM = 384


class OpenAICompatibleEmbedding:
    """A lightweight, pure-Python wrapper for any OpenAI-compatible embedding API."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.api_key = os.getenv("EM_OPENAI_API_KEY")
        base_url = os.getenv("EM_OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or "https://api.openai.com/v1"
        self.api_url = base_url.rstrip("/") + "/embeddings"

    def compute_source_embeddings(self, texts: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            
        payload = {
            "input": texts,
            "model": self.model_name
        }
        
        response = httpx.post(self.api_url, json=payload, headers=headers, timeout=60.0)
        response.raise_for_status()
        
        data = response.json()
        sorted_data = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in sorted_data]

    def compute_query_embeddings(self, query: str) -> list[list[float]]:
        return self.compute_source_embeddings([query])


def get_embedding_function(model_name: str = _DEFAULT_MODEL):
    """Return an embedding function based on WREN_EMBEDDING_PROVIDER."""
    provider = os.getenv("WREN_EMBEDDING_PROVIDER", _DEFAULT_PROVIDER).lower()

    if provider in ("openai", "openai-compatible"):
        return OpenAICompatibleEmbedding(model_name=model_name)

    # 默认回退到原生的 sentence-transformers
    import lancedb.embeddings  # noqa: PLC0415
    registry = lancedb.embeddings.get_registry()
    return registry.get("sentence-transformers").create(name=model_name)


@contextlib.contextmanager
def suppress_stderr():
    """Temporarily redirect stderr to /dev/null."""
    old_fd = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    os.close(devnull)
    try:
        yield
    finally:
        os.dup2(old_fd, 2)
        os.close(old_fd)


def warm_up(embed_fn):
    """Trigger model loading silently and return the vector dimension."""
    probe = embed_fn.compute_source_embeddings(["probe"])
    return len(probe[0])


def default_dimension() -> int:
    """Return the vector dimension for the default model."""
    return _DEFAULT_DIM
