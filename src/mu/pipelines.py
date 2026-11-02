"""Build fully expanded pipeline descriptions from a spec and input file."""

from copy import deepcopy
from pathlib import Path
import re
from typing import Any

import yaml


class PipelineBuildError(ValueError):
    """The spec or input cannot be expanded into pipelines."""


MODEL_SUFFIXES = {".gguf", ".safetensor", ".safetensors"}


def list_model_files(cache_dir: Path) -> list[Path]:
    """List supported model files under the cache models directory."""
    models_dir = cache_dir / "models"
    if not models_dir.is_dir():
        return []
    return sorted(
        (path.resolve() for path in models_dir.rglob("*")
         if path.is_file() and path.suffix.lower() in MODEL_SUFFIXES),
        key=str,
    )


def _discover_models(cache_dir: Path) -> dict[str, str]:
    """Index supported models by relative path and unambiguous filename aliases."""
    files = list_model_files(cache_dir)
    models_dir = (cache_dir / "models").expanduser().resolve()
    models: dict[str, str] = {}
    by_name: dict[str, list[Path]] = {}
    by_stem: dict[str, list[Path]] = {}
    for path in files:
        relative = path.relative_to(models_dir)
        relative_key = relative.as_posix()
        models[relative_key] = str(path)
        models[relative.with_suffix("").as_posix()] = str(path)
        by_name.setdefault(path.name, []).append(path)
        by_stem.setdefault(path.stem, []).append(path)
    for name, matches in by_name.items():
        if len(matches) == 1:
            models[name] = str(matches[0])
    for stem, matches in by_stem.items():
        if len(matches) == 1:
            models[stem] = str(matches[0])
    return models


def _read_yaml(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            value = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise PipelineBuildError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineBuildError(f"{label} root must be a YAML mapping")
    return value




def _lookup(expression: str, context: dict[str, Any]) -> tuple[bool, Any]:
    match = re.fullmatch(r"([A-Za-z_][\w]*)(?:\[([^\]]+)\])*", expression)
    if not match:
        return False, None
    root = match.group(1)
    if root not in context:
        return False, None
    value: Any = context[root]
    for key in re.findall(r"\[([^\]]+)\]", expression):
        if not isinstance(value, dict) or key not in value:
            return False, None
        value = value[key]
    return True, value


def _render(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _render(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, context) for item in value]
    if not isinstance(value, str):
        return value

    rendered = value
    for _ in range(8):
        whole = re.fullmatch(r"\{([^{}]+)\}", rendered)
        if whole:
            found, replacement = _lookup(whole.group(1), context)
            if found and not isinstance(replacement, str):
                return deepcopy(replacement)

        def replace(match: re.Match[str]) -> str:
            found, replacement = _lookup(match.group(1), context)
            return str(replacement) if found else match.group(0)

        updated = re.sub(r"\{([^{}]+)\}", replace, rendered)
        if updated == rendered:
            break
        rendered = updated
    return rendered


def build_pipelines(
    spec_path: Path,
    input_path: Path,
    pipeline_names: list[str],
    *,
    cache_dir: Path,
    output_dir: Path,
    input_data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Expand pipelines over the collection selected by each pipeline definition."""
    spec = _read_yaml(spec_path, "spec")
    if input_data is None:
        input_data = _read_yaml(input_path, "input")
    processors = spec.get("processors", {})
    definitions = spec.get("pipelines", {})
    if not isinstance(processors, dict):
        raise PipelineBuildError("spec 'processors' must be a mapping")
    if not isinstance(definitions, dict):
        raise PipelineBuildError("spec 'pipelines' must be a mapping")
    result: list[dict[str, Any]] = []
    resolved_cache_dir = cache_dir.expanduser().resolve()
    common = {
        "cache_dir": str(resolved_cache_dir),
        "output_dir": str(output_dir.expanduser().resolve()),
        "working_dir": str(Path.cwd()),
        "temp_dir": str(resolved_cache_dir),
        "models": _discover_models(resolved_cache_dir),
    }
    root_context = {**input_data, **common, "global": common, "input": input_data}
    for pipeline_name in pipeline_names:
        definition = definitions.get(pipeline_name)
        if not isinstance(definition, dict):
            raise PipelineBuildError(f"pipeline {pipeline_name!r} is not defined")
        steps = definition.get("steps", [])
        if not isinstance(steps, list):
            raise PipelineBuildError(f"pipeline {pipeline_name!r}.steps must be a list")
        collection = definition.get("for-each")
        if collection is None:
            collection = "requests" if "requests" in input_data else None
        if collection is not None and not isinstance(collection, str):
            raise PipelineBuildError(f"pipeline {pipeline_name!r}.for-each must be a context key string")
        if collection is None:
            entries = [(None, {})]
        else:
            values = input_data.get(collection)
            if not isinstance(values, list):
                raise PipelineBuildError(
                    f"input context {collection!r} selected by pipeline {pipeline_name!r} must be a list"
                )
            entries = list(enumerate(values))
        alias = definition.get("as", "request" if collection == "requests" else "item")
        if not isinstance(alias, str) or not alias:
            raise PipelineBuildError(f"pipeline {pipeline_name!r}.as must be a non-empty context name")
        for item_index, item in entries:
            if not isinstance(item, dict):
                location = f"{collection}[{item_index}]" if collection is not None else "input context"
                raise PipelineBuildError(f"input {location} must be a mapping")
            context = {**root_context, "item": item, alias: item}
            if alias == "request" or collection == "requests":
                context["request"] = item
            resolved_item = _render(deepcopy(item), context)
            context.update({"item": resolved_item, alias: resolved_item})
            if "request" in context:
                context["request"] = resolved_item
            expanded_steps: list[dict[str, Any]] = []
            for step_index, raw_step in enumerate(steps):
                if not isinstance(raw_step, dict):
                    raise PipelineBuildError(f"pipeline {pipeline_name!r}.steps[{step_index}] must be a mapping")
                processor_name = raw_step.get("processor")
                if not isinstance(processor_name, str):
                    raise PipelineBuildError(f"pipeline {pipeline_name!r}.steps[{step_index}].processor is required")
                processor = processors.get(processor_name)
                if not isinstance(processor, dict):
                    raise PipelineBuildError(f"processor {processor_name!r} is not defined")
                step_values = _render(deepcopy(raw_step), context)
                step_context = {**context, "step": step_values}
                processor_values = _render(deepcopy(processor), step_context)
                expanded_steps.append({
                    **processor_values,
                    **step_values,
                    "processor": processor_name,
                    "processor_config": processor_values,
                    "validation_context": step_context,
                })
            result.append({
                "pipeline": pipeline_name,
                "request": resolved_item,
                "item": resolved_item,
                "collection": collection,
                "item_alias": alias,
                "steps": expanded_steps,
            })
    return result
