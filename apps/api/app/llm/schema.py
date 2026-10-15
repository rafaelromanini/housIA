"""Converte modelos Pydantic no subconjunto de JSON Schema aceito pelas saídas estruturadas do Claude."""

from typing import Any

from pydantic import BaseModel

_DROP = {
    "title",
    "default",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minItems",
    "maxItems",
    "pattern",
    "examples",
}


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(defs[node["$ref"].split("/")[-1]])
        out = {k: walk(v) for k, v in node.items() if k not in _DROP}
        if out.get("type") == "object" or "properties" in out:
            out["type"] = "object"
            out["additionalProperties"] = False
            out["required"] = list(out.get("properties", {}).keys())
        return out

    return walk(raw)
