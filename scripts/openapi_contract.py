import argparse
from importlib import metadata as importlib_metadata
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SNAPSHOT_PATH = ROOT / "docs" / "api" / "openapi-contract.json"
SCHEMA_DEPENDENCY_PACKAGES = ("fastapi", "starlette", "sqlmodel")


def _required_schema_dependency_versions() -> dict[str, str]:
    requirements_path = ROOT / "server" / "requirements.txt"
    pinned: dict[str, str] = {}
    for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        for package in SCHEMA_DEPENDENCY_PACKAGES:
            prefix = f"{package}=="
            if line.lower().startswith(prefix):
                pinned[package] = line[len(prefix) :].strip()
    missing = [package for package in SCHEMA_DEPENDENCY_PACKAGES if package not in pinned]
    if missing:
        raise RuntimeError(
            "OpenAPI schema dependencies must be exactly pinned: "
            + ", ".join(missing)
        )
    return pinned


def require_contract_generator_environment() -> None:
    required = _required_schema_dependency_versions()
    mismatches = [
        f"{package}=={expected} (installed {importlib_metadata.version(package)})"
        for package, expected in required.items()
        if importlib_metadata.version(package) != expected
    ]
    if mismatches:
        raise RuntimeError(
            "OpenAPI contract generation requires pinned dependencies: "
            + ", ".join(mismatches)
            + ". Run: python -m pip install -r server/requirements.txt"
        )


def _sorted_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in sorted(value)}


def _schema_fingerprint(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return None
    out: dict[str, Any] = {}
    for key in ["$ref", "type", "format", "nullable"]:
        if key in schema:
            out[key] = schema[key]
    if "items" in schema:
        out["items"] = _schema_fingerprint(schema.get("items"))
    if "properties" in schema and isinstance(schema.get("properties"), dict):
        out["properties"] = _sorted_dict(
            {
                str(name): _schema_fingerprint(child)
                for name, child in schema["properties"].items()
                if isinstance(child, dict)
            }
        )
    if "required" in schema and isinstance(schema.get("required"), list):
        out["required"] = sorted(str(item) for item in schema["required"])
    if "anyOf" in schema and isinstance(schema.get("anyOf"), list):
        out["anyOf"] = [_schema_fingerprint(item) for item in schema["anyOf"] if isinstance(item, dict)]
    if "allOf" in schema and isinstance(schema.get("allOf"), list):
        out["allOf"] = [_schema_fingerprint(item) for item in schema["allOf"] if isinstance(item, dict)]
    if "oneOf" in schema and isinstance(schema.get("oneOf"), list):
        out["oneOf"] = [_schema_fingerprint(item) for item in schema["oneOf"] if isinstance(item, dict)]
    if "enum" in schema and isinstance(schema.get("enum"), list):
        out["enum"] = sorted(str(item) for item in schema["enum"])
    return out


def _content_schema(content: Any) -> dict[str, Any]:
    if not isinstance(content, dict):
        return {}
    out: dict[str, Any] = {}
    for content_type, detail in content.items():
        schema = detail.get("schema") if isinstance(detail, dict) else None
        out[str(content_type)] = _schema_fingerprint(schema)
    return _sorted_dict(out)


def _operation_contract(operation: dict[str, Any]) -> dict[str, Any]:
    parameters = []
    for param in operation.get("parameters") or []:
        if not isinstance(param, dict):
            continue
        parameters.append(
            {
                "name": str(param.get("name") or ""),
                "in": str(param.get("in") or ""),
                "required": bool(param.get("required")),
                "schema": _schema_fingerprint(param.get("schema")),
            }
        )
    parameters.sort(key=lambda item: (item["in"], item["name"]))

    request_body = operation.get("requestBody") if isinstance(operation.get("requestBody"), dict) else {}
    responses: dict[str, Any] = {}
    for status, response in (operation.get("responses") or {}).items():
        if not isinstance(response, dict):
            continue
        responses[str(status)] = {
            "content": _content_schema(response.get("content")),
        }

    return {
        "operation_id": str(operation.get("operationId") or ""),
        "tags": sorted(str(tag) for tag in (operation.get("tags") or [])),
        "parameters": parameters,
        "request_body": {
            "required": bool(request_body.get("required")),
            "content": _content_schema(request_body.get("content")),
        },
        "responses": _sorted_dict(responses),
    }


def build_openapi_contract(openapi_schema: dict[str, Any]) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for path, path_item in (openapi_schema.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        methods = {}
        for method, operation in path_item.items():
            method_name = str(method).lower()
            if method_name not in {"get", "post", "put", "patch", "delete"}:
                continue
            if isinstance(operation, dict):
                methods[method_name] = _operation_contract(operation)
        if methods:
            paths[str(path)] = _sorted_dict(methods)

    schemas: dict[str, Any] = {}
    for name, schema in ((openapi_schema.get("components") or {}).get("schemas") or {}).items():
        schemas[str(name)] = _schema_fingerprint(schema)

    return {
        "version": 1,
        "title": str((openapi_schema.get("info") or {}).get("title") or ""),
        "paths": _sorted_dict(paths),
        "schemas": _sorted_dict(schemas),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or check the stable OpenAPI contract snapshot.")
    parser.add_argument("--write", action="store_true", help="Write docs/api/openapi-contract.json")
    args = parser.parse_args()

    require_contract_generator_environment()
    from server.main import app

    contract = build_openapi_contract(app.openapi())
    if args.write:
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(
            json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0

    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    if contract != expected:
        raise SystemExit("OpenAPI contract snapshot is stale. Run: python scripts/openapi_contract.py --write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
