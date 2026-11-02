"""Pluggable action handlers used by repair, input prep, and processor steps."""

from pathlib import Path
import json
import subprocess
from typing import Any, Callable

from mu.expressions import render as _render


class ActionError(ValueError):
    """An action declaration is invalid or failed while running."""


ActionHandler = Callable[[dict[str, Any], dict[str, Any]], None]
ACTION_HANDLERS: dict[str, ActionHandler] = {}


def register_action(name: str) -> Callable[[ActionHandler], ActionHandler]:
    """Register an action implementation with action-specific parameters."""
    def decorator(handler: ActionHandler) -> ActionHandler:
        ACTION_HANDLERS[name] = handler
        return handler
    return decorator


def run_action(action: dict[str, Any], context: dict[str, Any] | None = None) -> None:
    if "action" in action:
        raise ActionError("use type, not action")
    name = action.get("type")
    if not isinstance(name, str) or name not in ACTION_HANDLERS:
        raise ActionError(f"unknown action {name!r}")
    ACTION_HANDLERS[name](action, context if context is not None else {})


@register_action("command")
def command_action(action: dict[str, Any], context: dict[str, Any]) -> None:
    steps = action.get("steps", [])
    if not isinstance(steps, list):
        raise ActionError("command.steps must be a list of argument lists")
    for index, command in enumerate(steps):
        if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
            raise ActionError(f"command.steps[{index}] must be a non-empty list of strings")
        rendered = [_render(arg, context) for arg in command]
        working_dir = context.get("action_dir", context.get("working_dir"))
        completed = subprocess.run(rendered, check=False, cwd=working_dir, capture_output=True, text=True)
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            suffix = f": {detail}" if detail else ""
            raise ActionError(
                f"command.steps[{index}] exited with status {completed.returncode}{suffix}"
            )


@register_action("create_dir")
def create_dir_action(action: dict[str, Any], context: dict[str, Any]) -> None:
    """Create a directory path, including any missing parent directories."""
    raw_path = action.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ActionError("create_dir.path must be a non-empty path string")
    path = Path(_render(raw_path, context)).expanduser()
    if not path.is_absolute():
        path = Path(context.get("working_dir", Path.cwd())) / path
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ActionError(f"create_dir could not create {path}: {exc}") from exc


@register_action("write_jsons_from_context")
def write_jsons_from_context(action: dict[str, Any], context: dict[str, Any]) -> None:
    """Write one JSON document per item under context[action.target]."""
    target = action.get("target")
    output = action.get("output")
    content = action.get("content")
    if not isinstance(target, str) or not isinstance(output, str) or not isinstance(content, dict):
        raise ActionError("write_jsons_from_context requires string target/output and mapping content")
    items = context.get(target)
    if not isinstance(items, list):
        raise ActionError(f"context[{target!r}] must be a list")
    for item in items:
        if not isinstance(item, dict):
            raise ActionError("write_jsons_from_context items must be mappings")
        values = {**context, "request": item}
        directory = Path(_render(output, values)).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        rendered = {key: _render(value, values) for key, value in content.items()}
        (directory / f"{item.get('name', 'item')}.json").write_text(
            json.dumps(rendered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


@register_action("write_json_from_context")
def write_json_from_context(action: dict[str, Any], context: dict[str, Any]) -> None:
    """Write one JSON document from the current (usually request) context."""
    output = action.get("output")
    content = action.get("content")
    if not isinstance(output, str) or not output:
        raise ActionError("write_json_from_context.output must be a non-empty path string")
    if not isinstance(content, dict):
        raise ActionError("write_json_from_context.content must be a mapping")
    path = Path(_render(output, context)).expanduser()
    if not path.is_absolute():
        path = Path(context.get("working_dir", Path.cwd())) / path
    rendered = _render(content, context)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rendered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ActionError(f"write_json_from_context could not write {path}: {exc}") from exc
