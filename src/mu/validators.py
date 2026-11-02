"""Extensible validators for action-level ``validate`` sections."""

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any, Callable

from mu.expressions import render as _render


class ValidationError(ValueError):
    """A validation declaration is malformed or uses an unknown validator."""


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    message: str


CheckHandler = Callable[[Any, Path, Any], ValidationResult]
CHECKS: dict[str, CheckHandler] = {}


def register_check(name: str) -> Callable[[CheckHandler], CheckHandler]:
    """Register a check handler; adding validators requires no dispatcher edits."""
    def decorator(handler: CheckHandler) -> CheckHandler:
        CHECKS[name] = handler
        return handler
    return decorator


@register_check("file_existed")
def file_existed(target: Any, base_dir: Path, expected: Any = True) -> ValidationResult:
    if not isinstance(target, str):
        raise ValidationError("file_existed target must be a path string")
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    exists = path.exists()
    passed = exists == expected
    return ValidationResult(passed, f"file_existed: {path} {'exists' if exists else 'does not exist'}")


@register_check("exit_zero")
def exit_zero(target: Any, base_dir: Path, expected: Any = 0) -> ValidationResult:
    if not isinstance(target, list) or not target or not all(isinstance(arg, str) for arg in target):
        raise ValidationError("exit_zero target must be a non-empty list of command arguments")
    try:
        completed = subprocess.run(target, cwd=base_dir, check=False, capture_output=True, text=True)
    except OSError as exc:
        return ValidationResult(False, f"exit_zero: could not run {target[0]}: {exc}")
    status = "returned 0" if completed.returncode == 0 else f"returned {completed.returncode}"
    detail = completed.stderr.strip() or completed.stdout.strip()
    suffix = f" ({detail})" if detail else ""
    return ValidationResult(completed.returncode == 0, f"exit_zero: {' '.join(target)} {status}{suffix}")


@register_check("exit_one")
def exit_one(target: Any, base_dir: Path, expected: Any = 1) -> ValidationResult:
    if not isinstance(target, list) or not target or not all(isinstance(arg, str) for arg in target):
        raise ValidationError("exit_one target must be a non-empty list of command arguments")
    try:
        completed = subprocess.run(target, cwd=base_dir, check=False, capture_output=True, text=True)
    except OSError as exc:
        return ValidationResult(False, f"exit_one: could not run {target[0]}: {exc}")
    status = f"returned {completed.returncode}"
    detail = completed.stderr.strip() or completed.stdout.strip()
    suffix = f" ({detail})" if detail else ""
    return ValidationResult(completed.returncode == 1, f"exit_one: {' '.join(target)} {status}{suffix}")


@register_check("exit_code")
def exit_code(target: Any, base_dir: Path, expected: Any = 0) -> ValidationResult:
    if not isinstance(target, list) or not target or not all(isinstance(arg, str) for arg in target):
        raise ValidationError("exit_code target must be a non-empty list of command arguments")
    if not isinstance(expected, int) or isinstance(expected, bool):
        raise ValidationError("exit_code expected must be an integer")
    try:
        completed = subprocess.run(target, cwd=base_dir, check=False, capture_output=True, text=True)
    except OSError as exc:
        return ValidationResult(False, f"exit_code: could not run {target[0]}: {exc}")
    detail = completed.stderr.strip() or completed.stdout.strip()
    suffix = f" ({detail})" if detail else ""
    passed = completed.returncode == expected
    return ValidationResult(passed, f"exit_code: {' '.join(target)} returned {completed.returncode}; expected {expected}{suffix}")


def validate_action_checks(
    checks: list[Any], *, base_dir: Path, context: dict[str, Any] | None = None
) -> list[ValidationResult]:
    results: list[ValidationResult] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise ValidationError(f"validate[{index}] must be a mapping")
        kind = check.get("condition")
        if not isinstance(kind, str) or kind not in CHECKS:
            raise ValidationError(f"validate[{index}] has unknown condition {kind!r}")
        if "target" not in check:
            raise ValidationError(f"validate[{index}].target is required")
        try:
            target = _render(check["target"], context or {})
            results.append(CHECKS[kind](target, base_dir, check.get("expected", 0 if kind == "exit_code" else True)))
        except ValidationError as exc:
            raise ValidationError(f"validate[{index}]: {exc}") from exc
    return results
