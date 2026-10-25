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


def strict_schema(model: type[BaseModel], usar_defs: bool = False) -> dict[str, Any]:
    """`usar_defs` mantém `$defs`/`$ref` em vez de copiar cada submodelo no ponto de uso. A gramática compilada pela
    API do Claude cresce com as cópias (a nota SOAP, com 9 seções `NoteSection`, passa de "compiled grammar is too
    large" só por isso); provedores compatíveis com OpenAI seguem recebendo o schema sem `$ref`."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            if usar_defs:
                return {"$ref": node["$ref"]}
            return walk(defs[node["$ref"].split("/")[-1]])
        out = {k: walk(v) for k, v in node.items() if k not in _DROP}
        if out.get("type") == "object" or "properties" in out:
            out["type"] = "object"
            out["additionalProperties"] = False
            out["required"] = list(out.get("properties", {}).keys())
        return out

    out = walk(raw)
    if usar_defs and defs:
        out["$defs"] = {k: walk(v) for k, v in defs.items()}
    return out
