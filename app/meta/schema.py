"""Check model JSON against the JSON Schema subset used in our prompts.

Model output is untrusted (reporters' text goes into the prompt), so every reply is checked
before it is stored, whichever backend produced it. Supported: object (properties, required,
additionalProperties: false), array (items), string (enum), integer, number (minimum,
maximum), boolean, null, and a list of types such as ["integer", "null"].
"""

from typing import Any

from app.meta.base import ModelOutputError

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def validate(schema: dict[str, Any], value: Any, path: str = "$") -> None:
    """Raise ModelOutputError naming the first field that does not match the schema."""
    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        if not any(_is(value, name) for name in names):
            raise ModelOutputError(f"{path}: expected {'/'.join(names)}, got {value!r:.80}")
    if value is None:
        return
    if "enum" in schema and value not in schema["enum"]:
        raise ModelOutputError(f"{path}: {value!r:.80} is not one of {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ModelOutputError(f"{path}: {value} is below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ModelOutputError(f"{path}: {value} is above {schema['maximum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            raise ModelOutputError(f"{path}: missing {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise ModelOutputError(f"{path}: unexpected {', '.join(extra)}")
        for key, sub in properties.items():
            if key in value:
                validate(sub, value[key], f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            validate(schema["items"], item, f"{path}[{i}]")


def _is(value: Any, name: str) -> bool:
    if name in ("integer", "number") and isinstance(value, bool):
        return False  # bool is an int subclass in Python, but not in JSON
    if name == "integer" and isinstance(value, float):
        return value.is_integer()
    return isinstance(value, _TYPES[name])
