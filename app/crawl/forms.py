"""Source config forms generated from an add-on's Pydantic JSON schema.

Supported field kinds: string, integer, number, boolean, enum (select), list of strings
(textarea, one per line) and secret file (upload). Anything else is edited as JSON.
Form inputs are named cfg.<field>; a secret file's removal checkbox is cfg.<field>.remove.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from app.crawl.base import SECRET_FILE_FORMAT

FieldKind = Literal["string", "integer", "number", "boolean", "enum", "list", "secret", "json"]
PREFIX = "cfg."
MAX_SECRET_BYTES = 1024 * 1024


@dataclass
class FormField:
    name: str
    label: str
    kind: FieldKind
    required: bool = False
    default: Any = None
    description: str | None = None
    options: list[str] = field(default_factory=list)
    minimum: float | None = None
    maximum: float | None = None
    value: Any = None  # current value (config or default)
    secret_present: bool = False

    @property
    def input_name(self) -> str:
        return PREFIX + self.name

    @property
    def text_value(self) -> str:
        """The value as the form shows it."""
        value = self.value
        if value is None:
            return ""
        if self.kind == "list":
            return "\n".join(str(v) for v in value)
        if self.kind == "json":
            return json.dumps(value, ensure_ascii=False)
        return str(value)


def _resolve(prop: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Follow $ref / single allOf, and unwrap Optional (anyOf with null)."""
    schema = dict(prop)
    if "allOf" in schema and len(schema["allOf"]) == 1:
        schema = {**schema.pop("allOf")[0], **schema}
    if "$ref" in schema:
        target = defs.get(schema.pop("$ref").rsplit("/", 1)[-1], {})
        schema = {**target, **schema}
    if "anyOf" in schema:
        variants = [v for v in schema["anyOf"] if v.get("type") != "null"]
        if len(variants) == 1:
            rest = {k: v for k, v in schema.items() if k != "anyOf"}
            schema = {**_resolve(variants[0], defs), **rest}
    return schema


def _kind(schema: dict[str, Any], defs: dict[str, Any]) -> FieldKind:
    if schema.get("format") == SECRET_FILE_FORMAT:
        return "secret"
    if "enum" in schema:
        return "enum"
    kind = schema.get("type")
    if kind in ("string", "integer", "number", "boolean"):
        return kind
    if kind == "array":
        items = _resolve(schema.get("items", {}), defs)
        if items.get("type") == "string" and "enum" not in items:
            return "list"
    return "json"


def form_fields(
    model: type[BaseModel],
    config: Mapping[str, Any] | None = None,
    secrets_present: set[str] | None = None,
) -> list[FormField]:
    """One FormField per config field, in declaration order, with the current values."""
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    required = set(schema.get("required", []))
    config = config or {}
    secrets_present = secrets_present or set()
    fields = []
    for name, prop in schema.get("properties", {}).items():
        resolved = _resolve(prop, defs)
        kind = _kind(resolved, defs)
        default = resolved.get("default")
        fields.append(
            FormField(
                name=name,
                label=resolved.get("title") or name,
                kind=kind,
                required=name in required and kind != "secret",
                default=default,
                description=resolved.get("description"),
                options=[str(v) for v in resolved.get("enum", [])],
                minimum=resolved.get("minimum", resolved.get("exclusiveMinimum")),
                maximum=resolved.get("maximum", resolved.get("exclusiveMaximum")),
                value=config.get(name, default),
                secret_present=name in secrets_present,
            )
        )
    return fields


class FormError(ValueError):
    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in fields.items()))
        self.fields = fields


@dataclass
class ParsedForm:
    config: dict[str, Any]
    secrets: dict[str, bytes | None]  # bytes = new file, None = remove; absent = keep


def _text(form: Mapping[str, Any], key: str) -> str | None:
    value = form.get(key)
    return value if isinstance(value, str) else None


async def parse_config_form(model: type[BaseModel], form: Mapping[str, Any]) -> ParsedForm:
    """Turn submitted form values into a raw config dict (validated later by the model).

    Empty text inputs are left out, so the model's default applies (a required field then
    fails validation with a clear message). Values stay strings; Pydantic converts them.
    """
    config: dict[str, Any] = {}
    secrets: dict[str, bytes | None] = {}
    errors: dict[str, str] = {}
    for f in form_fields(model):
        key = f.input_name
        if f.kind == "boolean":
            config[f.name] = form.get(key) in ("on", "true", "1")
        elif f.kind == "secret":
            upload = form.get(key)
            data = await upload.read() if hasattr(upload, "read") else b""
            if data:
                if len(data) > MAX_SECRET_BYTES:
                    errors[f.name] = "file is larger than 1 MiB"
                else:
                    secrets[f.name] = data
            elif form.get(key + ".remove") in ("on", "true", "1"):
                secrets[f.name] = None
        elif f.kind == "list":
            raw = _text(form, key) or ""
            config[f.name] = [line.strip() for line in raw.splitlines() if line.strip()]
        elif f.kind == "json":
            raw = (_text(form, key) or "").strip()
            if raw:
                try:
                    config[f.name] = json.loads(raw)
                except json.JSONDecodeError as exc:
                    errors[f.name] = f"invalid JSON: {exc.msg}"
        else:
            raw = (_text(form, key) or "").strip()
            if raw:
                config[f.name] = raw
    if errors:
        raise FormError(errors)
    return ParsedForm(config, secrets)
