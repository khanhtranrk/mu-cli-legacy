"""Spec loading and validation orchestration."""

from pathlib import Path
import subprocess
from typing import Any

import yaml

from mu.expressions import engine_for, validate_templates

from mu.validators import ValidationError, ValidationResult, validate_action_checks
from mu.actions import ACTION_HANDLERS, ActionError, run_action


class SpecError(ValueError):
    """A spec file is unreadable or structurally invalid."""


def load_spec(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise SpecError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError("spec root must be a YAML mapping")
    try:
        engine_for(data)
        validate_templates(data)
    except ValueError as exc:
        raise SpecError(str(exc)) from exc
    return data


def validate_spec(path: Path, *, context: dict[str, Any] | None = None) -> list[ValidationResult]:
    """Run all action-level validators declared under ``repair``."""
    spec = load_spec(path)
    actions = spec.get("repair", [])
    if not isinstance(actions, list):
        raise SpecError("'repair' must be a list")
    results: list[ValidationResult] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise SpecError(f"repair[{index}] must be a mapping")
        if "action" in action:
            raise SpecError(f"repair[{index}]: use type, not action")
        action_type = action.get("type")
        if not isinstance(action_type, str):
            raise SpecError(f"repair[{index}].type must be a string")
        if action_type not in ACTION_HANDLERS:
            raise SpecError(f"repair[{index}].type: unknown action {action_type!r}")
        checks = action.get("validate", [])
        if checks is None:
            continue
        if not isinstance(checks, list):
            raise SpecError(f"repair[{index}].validate must be a list")
        try:
            results.extend(validate_action_checks(checks, base_dir=path.resolve().parent, context=context))
        except ValidationError as exc:
            raise SpecError(f"repair[{index}]: {exc}") from exc
    return results


def repair_spec(path: Path, *, cache_dir: Path, force_validates: bool = False) -> list[ValidationResult]:
    """Run repair actions, checking each action's declared postconditions."""
    spec = load_spec(path)
    actions = spec.get("repair", [])
    if not isinstance(actions, list):
        raise SpecError("'repair' must be a list")
    base_dir = path.resolve().parent
    context = {
        "cache_dir": str(cache_dir.expanduser().resolve()),
        "working_dir": str(Path.cwd()),
        "action_dir": str(base_dir),
    }
    results: list[ValidationResult] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise SpecError(f"repair[{index}] must be a mapping")
        try:
            run_action(action, context)
        except (ActionError, OSError, subprocess.CalledProcessError) as exc:
            raise SpecError(f"repair[{index}] action failed: {exc}") from exc
        checks = action.get("validate", [])
        if checks is None:
            checks = []
        if not isinstance(checks, list):
            raise SpecError(f"repair[{index}].validate must be a list")
        if not force_validates:
            try:
                action_results = validate_action_checks(checks, base_dir=base_dir, context=context)
            except ValidationError as exc:
                raise SpecError(f"repair[{index}]: {exc}") from exc
            results.extend(action_results)
            failures = [result for result in action_results if not result.passed]
            if failures:
                raise SpecError(f"repair[{index}] validation failed: {failures[0].message}")

    # This option defers checks until every repair action has run.
    if force_validates:
        results = validate_spec(path, context=context)
    return results
