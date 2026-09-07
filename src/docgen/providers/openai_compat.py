"""OpenAI-compatible chat-completions adapter.

Works with:
  - Azure OpenAI    base_url = https://<name>.openai.azure.com/openai/v1/
  - vLLM            base_url = http://<host>:8000/v1
  - Ollama          base_url = http://<host>:11434/v1
  - OpenAI          base_url = https://api.openai.com/v1

Contract: given the extracted blank-template text, return structured MappingProposal JSON.
The model never sees runtime data — this is called only from designtime/mapper.py.
"""
from __future__ import annotations

import json

import httpx
from pydantic import ValidationError

from docgen.providers.base import MappingProposal

_SYSTEM_PROMPT = """You are a document-template analyst. You receive the raw text of a blank Word
template that contains placeholders (bracketed like [MEMBER_NAME], angle-quoted like <<name>>,
underscored blanks like ______, or plain-English gaps such as "Dear ,").

Your job is to propose a mapping to Jinja tags for a docxtpl render engine. Return STRICT JSON
matching this schema:

{
  "suggestions": [
    {
      "placeholder": "<exact substring found in the template>",
      "jinja": "<jinja expression that should replace it, e.g. '{{ member_name }}' or a '{% for %}' block>",
      "field": {
        "name": "snake_case_identifier",
        "type": "string | integer | number | boolean | date | list | object",
        "description": "human-readable purpose",
        "required": true,
        "item_type": null,
        "fields": null
      }
    }
  ],
  "notes": "short free-text summary"
}

Rules:
- Prefer scalar fields. Only use "list" when you see a table or repeating block.
- snake_case names, no reserved words, no whitespace.
- Do not invent fields the template doesn't need.
- Do NOT include any text outside the JSON object."""


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    async def propose_mapping(self, template_text: str) -> MappingProposal:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "api-key": self._api_key,  # Azure-style
            "Content-Type": "application/json",
        }
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": template_text},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(f"{self._base_url}/chat/completions", headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Unexpected LLM response shape: {data}") from e

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"LLM returned non-JSON content: {content[:400]}") from e

        try:
            return MappingProposal.model_validate(parsed)
        except ValidationError as e:
            raise RuntimeError(f"LLM JSON did not match MappingProposal schema: {e}") from e
