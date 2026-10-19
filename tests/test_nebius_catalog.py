"""Name-based capability classification for the Token Factory catalog.

A vision model filed as chat-only is missing from the Extract dropdown and
offered as a Reason model instead, so the matcher is tested on real ids.
"""
from __future__ import annotations

import pytest

from docgen.providers.nebius_catalog import CatalogEntry, NebiusCatalog, _classify


@pytest.mark.parametrize("model_id", [
    "openbmb/MiniCPM-V-4_5",
    "nvidia/Nemotron-3.5-Super-VL",          # "VL" ends the name
    "Qwen/Qwen2.5-VL-72B-Instruct",          # "VL" mid-name
    "google/gemma-3-27b-it",
    "meta-llama/Llama-3.2-11B-Vision-Instruct",
])
def test_vision_models_are_classified_as_vision(model_id: str) -> None:
    assert _classify(model_id) == frozenset({"chat", "vision"})


@pytest.mark.parametrize("model_id", [
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "nvidia/Nemotron-3_5-Lightning",
    "deepseek-ai/DeepSeek-V3",               # "V3" is a version, not vision
])
def test_text_models_stay_chat_only(model_id: str) -> None:
    assert _classify(model_id) == frozenset({"chat"})


def test_embedding_models_are_not_chat() -> None:
    assert _classify("Qwen/Qwen3-Embedding-8B") == frozenset({"embed"})


def test_vision_model_is_offered_for_extract_not_reason() -> None:
    cat = NebiusCatalog(base_url="http://unused", api_key="")
    cat.entries = [CatalogEntry(id=m, capabilities=_classify(m)) for m in
                   ("nvidia/Nemotron-3.5-Super-VL", "nvidia/nemotron-3-super-120b-a12b")]
    assert cat.vision_models() == ["nvidia/Nemotron-3.5-Super-VL"]
    assert cat.chat_models() == ["nvidia/nemotron-3-super-120b-a12b"]
