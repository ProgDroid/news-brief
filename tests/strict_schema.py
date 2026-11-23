"""What a `strict: true` tool schema may not contain (news-brief-bcr).

The API compiles a strict schema into a grammar and answers 400 on any feature
outside its subset, so a schema that passes the local tests but carries one of
these breaks every call in production. Limits from the structured-outputs docs,
"JSON Schema limitations", read 2026-11-23.
"""

UNSUPPORTED_KEYS = {
    "maxItems",
    "maxLength",
    "minLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "oneOf",
    "not",
}


def unsupported(schema, path="$") -> list[str]:
    """Every place `schema` breaks the strict subset, as 'path: reason'."""
    found: list[str] = []
    if isinstance(schema, dict):
        for key in sorted(UNSUPPORTED_KEYS & set(schema)):
            found.append(f"{path}: {key}")
        if schema.get("minItems", 0) not in (0, 1):
            found.append(f"{path}: minItems={schema['minItems']}")
        if (
            schema.get("type") == "object"
            and schema.get("additionalProperties") is not False
        ):
            found.append(f"{path}: additionalProperties is not false")
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                # Keys here are field NAMES, not keywords: a field called
                # "not" is legal. Only their schemas are checked.
                for name, sub in value.items():
                    found.extend(unsupported(sub, f"{path}.properties.{name}"))
            elif key in ("enum", "const", "required"):
                continue  # data, never schemas
            else:
                found.extend(unsupported(value, f"{path}.{key}"))
    elif isinstance(schema, list):
        for i, value in enumerate(schema):
            found.extend(unsupported(value, f"{path}[{i}]"))
    return found
