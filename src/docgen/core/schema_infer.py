"""Schema utilities — build/validate JSON Schemas for template fields.

Per §7, the frozen schema drives three surfaces: runtime validation, REST OpenAPI,
and the MCP tool signature. Keep the field-type set small on purpose.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, create_model

FieldType = Literal["string", "integer", "number", "boolean", "date", "list", "object"]


class FieldSpec(BaseModel):
    name: str
    type: FieldType = "string"
    description: str | None = None
    required: bool = True
    item_type: FieldType | None = None  # for list
    fields: list["FieldSpec"] | None = None  # for object / list-of-object


FieldSpec.model_rebuild()


class TemplateSchema(BaseModel):
    fields: list[FieldSpec]

    def to_json_schema(self) -> dict[str, Any]:
        props: dict[str, Any] = {}
        required: list[str] = []
        for f in self.fields:
            props[f.name] = _field_to_json(f)
            if f.required:
                required.append(f.name)
        schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": True}
        if required:
            schema["required"] = required
        return schema


def _field_to_json(f: FieldSpec) -> dict[str, Any]:
    if f.type == "string":
        return {"type": "string", "description": f.description or ""}
    if f.type == "integer":
        return {"type": "integer", "description": f.description or ""}
    if f.type == "number":
        return {"type": "number", "description": f.description or ""}
    if f.type == "boolean":
        return {"type": "boolean", "description": f.description or ""}
    if f.type == "date":
        return {"type": "string", "format": "date", "description": f.description or ""}
    if f.type == "list":
        item_schema: dict[str, Any]
        if f.fields:
            item_schema = TemplateSchema(fields=f.fields).to_json_schema()
        elif f.item_type:
            item_schema = _field_to_json(FieldSpec(name="item", type=f.item_type, required=True))
        else:
            item_schema = {"type": "string"}
        return {"type": "array", "items": item_schema, "description": f.description or ""}
    if f.type == "object":
        nested = TemplateSchema(fields=f.fields or []).to_json_schema()
        nested["description"] = f.description or ""
        return nested
    return {"type": "string"}


_TYPE_MAP: dict[FieldType, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "date": str,
    "list": list,
    "object": dict,
}


def build_pydantic_model(schema: TemplateSchema, name: str = "TemplatePayload") -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for f in schema.fields:
        py_type: Any
        if f.type == "list":
            py_type = list
        elif f.type == "object":
            py_type = dict
        else:
            py_type = _TYPE_MAP[f.type]
        default = ... if f.required else None
        if not f.required:
            py_type = py_type | None  # type: ignore[operator]
        fields[f.name] = (py_type, Field(default, description=f.description))
    return create_model(name, **fields)  # type: ignore[call-overload]


def validate_payload(schema_dict: dict, payload: dict) -> tuple[bool, list[str]]:
    try:
        schema = TemplateSchema.model_validate(schema_dict)
    except ValidationError as e:
        return False, [f"schema-invalid: {e}"]
    Model = build_pydantic_model(schema)
    try:
        Model.model_validate(payload)
        return True, []
    except ValidationError as e:
        return False, [f"{'/'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]


def coerce_payload(schema_dict: dict, payload: dict) -> dict:
    """Return payload with types coerced per the schema (e.g. "false"->False, "12"->12).

    Jinja treats every non-empty string as truthy, so a boolean field arriving as the
    string "false" renders as True in {% if %} — every checkbox ticked. This helper
    runs the same pydantic model as validate_payload but returns the coerced dict so
    the renderer sees real bools/ints/floats. Unknown keys pass through unchanged.
    """
    try:
        schema = TemplateSchema.model_validate(schema_dict)
    except ValidationError:
        return dict(payload)
    Model = build_pydantic_model(schema)
    try:
        coerced = Model.model_validate(payload).model_dump()
    except ValidationError:
        return dict(payload)
    # Preserve extras (schema uses additionalProperties: True).
    known = {f.name for f in schema.fields}
    for k, v in payload.items():
        if k not in known:
            coerced[k] = v
    return coerced
