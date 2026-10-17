"""Faithful JSON Schema validation behind the existing Pydantic interface."""
from copy import deepcopy
from typing import Any
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, model_validator


class UnsupportedSchema(ValueError):
    pass


# Deliberately bounded v1 subset; no remote refs, formats or combinators with
# ambiguous provider behavior. Unknown validation keywords fail closed.
KEYWORDS = {'type', 'properties', 'required', 'additionalProperties', 'items',
            'enum', 'const', 'anyOf', 'oneOf', 'allOf', '$ref', '$defs',
            'title', 'description', 'default', 'minimum', 'maximum',
            'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf',
            'minLength', 'maxLength', 'pattern', 'minItems', 'maxItems',
            'uniqueItems', 'minProperties', 'maxProperties', '$schema'}


def input_model(schema: dict) -> type[BaseModel]:
    raw = deepcopy(schema)
    if raw.get('type') != 'object':
        raise UnsupportedSchema('Tool input must be an object')

    def children(node):
        for key in ("properties", "$defs"):
            yield from node.get(key, {}).values()
        for key in ("items", "additionalProperties"):
            if key in node:
                yield node[key]
        for key in ("anyOf", "oneOf", "allOf"):
            yield from node.get(key, [])

    def visit(node):
        if isinstance(node, bool):
            return
        if not isinstance(node, dict) or set(node) - KEYWORDS:
            raise UnsupportedSchema('Unsupported JSON Schema keyword')
        if '$schema' in node and node['$schema'] != 'https://json-schema.org/draft/2020-12/schema':
            raise UnsupportedSchema('Only JSON Schema 2020-12 supported')
        if '$ref' in node and not node['$ref'].startswith('#/$defs/'):
            raise UnsupportedSchema('Only local $defs references supported')
        for child in children(node):
            visit(child)
    try:
        visit(raw)
        Draft202012Validator.check_schema(raw)
        validator = Draft202012Validator(raw)
        # Resolve local JSON pointers without using jsonschema private APIs.
        # Reject recursive schemas in v1 rather than risking validator recursion.
        def refs(node, active=frozenset()):
            if isinstance(node, bool):
                return
            if '$ref' in node:
                pointer = node['$ref']
                if pointer in active:
                    raise UnsupportedSchema('Recursive references are unsupported')
                target = raw
                for part in pointer[2:].split('/'):
                    key = part.replace('~1', '/').replace('~0', '~')
                    target = target[int(key)] if isinstance(target, list) else target[key]
                visit(target)
                refs(target, active | {pointer})
            for child in children(node):
                refs(child, active)
        refs(raw)
    except Exception:
        raise UnsupportedSchema('Unsupported or invalid JSON Schema') from None

    class MCPInput(BaseModel):
        model_config = ConfigDict(extra='allow', hide_input_in_errors=True)

        @model_validator(mode='before')
        @classmethod
        def validate_remote(cls, value: Any):
            if not validator.is_valid(value):
                raise ValueError('MCP arguments do not match input schema')
            return value

        @classmethod
        def model_json_schema(cls, *args, **kwargs):
            return deepcopy(raw)

    return MCPInput
