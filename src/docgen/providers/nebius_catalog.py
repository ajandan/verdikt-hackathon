"""Nebius Token Factory model-catalog helper.

Hits ``/v1/models`` on the configured Nebius endpoint, caches the result, and
classifies each model by capability so the admin UI dropdowns can populate
from a live catalog instead of a hard-coded list.

Classification is heuristic and endpoint-verified: a model is treated as
vision-capable if a probe request with ``image_url`` content returns 200. The
alternative (regex on model name) is fragile — ``google/gemma-3-27b-it`` is
vision-capable on Nebius but its name contains no obvious hint. We keep the
name-based classification as a fast path and fall back to the probe only when
the operator refreshes.

Cache is in-process. A restart re-fetches; there is intentionally no on-disk
persistence — the source of truth is Nebius, not us.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

import httpx

Capability = Literal["chat", "vision", "embed"]


@dataclass(frozen=True)
class CatalogEntry:
    """One model entry from ``/v1/models`` with its inferred capabilities."""

    id: str
    capabilities: frozenset[Capability]

    def supports(self, cap: Capability) -> bool:
        return cap in self.capabilities


# Name patterns that reliably indicate a capability. Kept small and specific;
# ambiguous names fall through to "chat only" and the operator can flip them
# via the admin UI's manual-verify probe.
_VISION_PATTERNS = (
    re.compile(r"MiniCPM-V", re.IGNORECASE),
    re.compile(r"gemma-3-\d+b", re.IGNORECASE),  # Gemma 3 is multimodal
    # Qwen2.5-VL-72B, Nemotron-3.5-Super-VL, Llama-Vision. "VL" may end the
    # name, so a trailing dash can't be required.
    re.compile(r"-VL(?:-|$)|Vision", re.IGNORECASE),
    re.compile(r"NVLM", re.IGNORECASE),
    re.compile(r"Pixtral", re.IGNORECASE),
)

_EMBED_PATTERNS = (
    re.compile(r"[Ee]mbed"),
    re.compile(r"BGE|bge"),
)


def _classify(model_id: str) -> frozenset[Capability]:
    caps: set[Capability] = set()
    if any(p.search(model_id) for p in _EMBED_PATTERNS):
        caps.add("embed")
    else:
        # An embed model doesn't do chat; a chat model does chat.
        caps.add("chat")
        if any(p.search(model_id) for p in _VISION_PATTERNS):
            caps.add("vision")
    return frozenset(caps)


@dataclass
class NebiusCatalog:
    """In-process cache of Nebius Token Factory's model catalog."""

    base_url: str
    api_key: str
    timeout: float = 30.0
    entries: list[CatalogEntry] = field(default_factory=list)

    def refresh(self) -> None:
        """Re-fetch ``/v1/models`` and rebuild the cached classification."""
        url = f"{self.base_url.rstrip('/')}/models"
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
        self.entries = [
            CatalogEntry(id=item["id"], capabilities=_classify(item["id"]))
            for item in data
            if item.get("id")
        ]

    def by_capability(self, cap: Capability) -> list[CatalogEntry]:
        return [e for e in self.entries if e.supports(cap)]

    def vision_models(self) -> list[str]:
        return [e.id for e in self.by_capability("vision")]

    def chat_models(self) -> list[str]:
        # Chat but not vision — pure reasoning models suitable for Reason stage.
        return [e.id for e in self.entries if e.supports("chat") and not e.supports("vision")]

    def embed_models(self) -> list[str]:
        return [e.id for e in self.by_capability("embed")]


_CACHED: NebiusCatalog | None = None


def get_cached_catalog(
    base_url: str, api_key: str, *, force_refresh: bool = False
) -> NebiusCatalog:
    """Return a lazily-populated, process-wide catalog cache.

    First call fetches ``/v1/models`` and classifies. Subsequent calls return
    the cached instance. ``force_refresh=True`` re-fetches. Empty api_key
    yields an empty catalog rather than raising — the admin UI degrades to
    a "no models discovered" state instead of erroring.
    """
    global _CACHED
    if _CACHED is None or force_refresh:
        cat = NebiusCatalog(base_url=base_url, api_key=api_key)
        if api_key:
            try:
                cat.refresh()
            except Exception:  # never crash the admin page on a catalog error
                cat.entries = []
        _CACHED = cat
    return _CACHED


__all__ = ["Capability", "CatalogEntry", "NebiusCatalog", "get_cached_catalog"]
