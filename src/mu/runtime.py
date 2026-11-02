"""Runtime for the workload/pipeline spec format."""
from __future__ import annotations

import hashlib
import re
import stat
import json
from contextlib import ExitStack
import tempfile
import math
import os
from pathlib import Path
import subprocess
import signal
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from copy import deepcopy
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, HTTPRedirectHandler, build_opener
from urllib.parse import urlsplit

import yaml

from mu.spec_yaml import load_spec_yaml
from mu.http_headers import request_headers
from mu.hardware import ComputeDevice
from mu.expressions import ENGINE_KEY, engine_for, json_value as _cel_json, validate_templates, render as _render


def _request_scopes(context, request):
    captured = request.get("registryContext", {})
    if not isinstance(captured, dict): raise RuntimeError("registryContext must be a mapping")
    for scope in ("CreationSpace", "Session", "Request"):
        values = captured.get(scope, {})
        if not isinstance(values, dict): raise RuntimeError(f"registryContext.{scope} must be a mapping")
        context[scope] = {"Registry": deepcopy(values)}


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlsplit(req.full_url)[:2] != urlsplit(newurl)[:2]:
            for name in ("Authorization", "Cookie", "X-mu-assignment-version"):
                redirected.remove_header(name)
        return redirected


class RuntimeError(ValueError):
    pass


_processes: set[subprocess.Popen] = set()
_process_lock = threading.RLock()
_cancelled = threading.Event()


def terminate_processes() -> None:
    _cancelled.set()
    with _process_lock:
        processes = list(_processes)
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def reset_interruption() -> None:
    _cancelled.clear()


def _run_process(command, **kwargs):
    """Track process groups so interruption can stop worker-thread children too."""
    check = kwargs.pop("check", False)
    with _process_lock:
        if _cancelled.is_set():
            raise RuntimeError("flow interrupted")
        process = subprocess.Popen(command, start_new_session=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, **kwargs)
        _processes.add(process)
    try:
        stdout, stderr = process.communicate()
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, command, stdout, stderr)
        return result
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    finally:
        with _process_lock:
            _processes.discard(process)


def read_data(path: Path) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw) if path.suffix.lower() == ".json" else load_spec_yaml(raw)
    except (OSError, yaml.YAMLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc
    return value


def params_for(schema: dict[str, Any], supplied: dict[str, Any], context: dict[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(schema, dict) or not isinstance(supplied, dict):
        raise RuntimeError(f"{label}: params and args must be mappings")
    result = {}
    for name, declaration in schema.items():
        declaration = declaration if isinstance(declaration, dict) else {}
        if name in supplied:
            value = supplied[name]
        elif "default" in declaration:
            value = declaration["default"]
        else:
            raise RuntimeError(f"{label} requires parameter {name!r}")
        value = _cel_json(value) if name in supplied else _render(value, context)
        kind = declaration.get("type")
        if kind in {"array", "object", "boolean"} and not isinstance(value, {"array": list, "object": dict, "boolean": bool}[kind]):
            raise RuntimeError(f"{label}.{name} must be {kind}")
        if declaration.get("type") == "string" and not isinstance(value, str):
            raise RuntimeError(f"{label}.{name} must be a string")
        if declaration.get("type") == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise RuntimeError(f"{label}.{name} must be a number")
        result[name] = value
    unknown = set(supplied) - set(schema)
    if unknown:
        raise RuntimeError(f"{label} has unknown parameter(s): {', '.join(sorted(unknown))}")
    return result


def condition(spec: Any, context: dict[str, Any], states: dict[str, str]) -> bool:
    if spec is None:
        return True
    if not isinstance(spec, dict) or not isinstance(spec.get("operator"), str):
        raise RuntimeError("condition must be a mapping with operator")
    op = spec["operator"]
    expected = spec.get("expected", True)
    if op == "cel":
        value = _render(spec.get("expression"), context)
        if not isinstance(value, bool) or not isinstance(expected, bool):
            raise RuntimeError("CEL condition expression and expected must be booleans")
    elif op in {"and", "or"}:
        props = spec.get("props", [])
        if not isinstance(props, list): raise RuntimeError(f"{op}.props must be a list")
        value = all(condition(x, context, states) for x in props) if op == "and" else any(condition(x, context, states) for x in props)
    elif op in {"file_exists", "folder_exists", "path_exists"}:
        path = Path(str(_render(spec.get("path", ""), context))).expanduser()
        if not path.is_absolute(): path = Path(context["session"]["working_dir"]) / path
        value = path.is_file() if op == "file_exists" else path.is_dir() if op == "folder_exists" else path.exists()
    elif op == "exit_code":
        target = _render(spec.get("target"), context)
        if not isinstance(target, list) or not target:
            raise RuntimeError("exit_code.target must be a non-empty command list")
        expected = spec.get("expected", 0)
        if not isinstance(expected, int) or isinstance(expected, bool):
            raise RuntimeError("exit_code.expected must be an integer")
        try:
            result = _run_process(list(map(str, target)), cwd=context["session"]["working_dir"], text=True)
            value = result.returncode
        except OSError:
            value = None
    elif op in {"action_state", "node_state"}:
        if "expected" not in spec:
            raise RuntimeError(f"{op}.expected is required")
        target = _render(spec.get("target"), context)
        if not isinstance(target, list) or not target or not all(isinstance(key, str) and key for key in target):
            raise RuntimeError(f"{op}.target must be a non-empty list of non-empty string keys")
        value = context.get("actions" if op == "action_state" else "node", {})
        for key in target:
            if not isinstance(value, dict) or key not in value:
                return False
            value = value[key]
    elif op == "node_status":
        value = states.get(str(spec.get("node", context.get("node", {}).get("name", ""))))
        expected = spec.get("expected", "success")
        if expected == "succeeded": expected = "success"
    else:
        raise RuntimeError(f"unknown condition operator {op!r}")
    return value == expected


def _apply_context(target: dict[str, Any], values: Any) -> None:
    if values:
        if not isinstance(values, dict): raise RuntimeError("context must be a mapping")
        if ENGINE_KEY in values or any(k in values for k in ("CreationSpace", "Session", "Request")):
            raise RuntimeError("context cannot override engine or captured Registry scopes")
        target.update(values)


def render_payload(value: Any, context: dict[str, Any]) -> Any:
    """Render data keys without changing structural spec keys."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key = _render(key, context)
            if not isinstance(key, str) or not key.strip():
                raise RuntimeError("payload key must resolve to a non-empty string")
            if key in result:
                raise RuntimeError(f"duplicate rendered payload key {key!r}")
            result[key] = render_payload(item, context)
        return result
    if isinstance(value, list):
        return [render_payload(item, context) for item in value]
    rendered = _render(value, context)
    return rendered


def validate_routines(spec: dict) -> None:
    routines = spec.get("routines", {})
    if not isinstance(routines, dict):
        raise RuntimeError("routines must be a mapping")
    visited, visiting = set(), set()
    def visit(name):
        if name in visiting:
            raise RuntimeError(f"routines.{name}: routine call cycle")
        if name in visited:
            return
        declaration = routines.get(name)
        if not isinstance(name, str) or not name.strip() or not isinstance(declaration, dict):
            raise RuntimeError(f"routines.{name}: undefined or invalid routine")
        if not isinstance(declaration.get("params", {}), dict):
            raise RuntimeError(f"routines.{name}.params must be a mapping")
        for param, field in declaration.get("params", {}).items():
            if not isinstance(field, dict) or field.get("type") not in {None, "string", "number", "boolean", "array", "object"}:
                raise RuntimeError(f"routines.{name}.params.{param}: invalid parameter declaration")
        actions = declaration.get("actions")
        if not isinstance(actions, list) or not all(isinstance(a, dict) for a in actions):
            raise RuntimeError(f"routines.{name}.actions must be a list of action mappings")
        visiting.add(name)
        for action in actions:
            if action.get("type") == "execute_workload":
                raise RuntimeError(f"routines.{name}: execute_workload is not supported inside routines")
            if action.get("type") == "execute_routine":
                target = action.get("routine")
                if not isinstance(target, str) or not target or re.search(r"[{}]", target):
                    raise RuntimeError(f"routines.{name}: routine must be a literal name")
                visit(target)
        visiting.remove(name)
        visited.add(name)
    for name in routines:
        visit(name)


EVENT_NAMES = {"node_started", "node_finished", "pipeline_started", "pipeline_finished"}


def validate_events(spec: dict) -> None:
    """Validate hooks before executing any side effects."""
    scopes = [("events", spec, EVENT_NAMES)]
    pipelines = spec.get("pipeline", {})
    if not isinstance(pipelines, dict):
        raise RuntimeError("pipeline must be a mapping")
    for name, pipeline in pipelines.items():
        if not isinstance(pipeline, dict):
            continue
        scopes.append((f"pipeline.{name}.events", pipeline, EVENT_NAMES))
        for node in pipeline.get("nodes", []) if isinstance(pipeline.get("nodes", []), list) else []:
            if isinstance(node, dict):
                scopes.append((f"pipeline.{name}.nodes.{node.get('name')}.events", node,
                               {"node_started", "node_finished"}))
    for path, scope, allowed in scopes:
        hooks = scope.get("events", [])
        if not isinstance(hooks, list):
            raise RuntimeError(f"{path}: expected a list of hooks")
        for index, hook in enumerate(hooks):
            label = f"{path}[{index}]"
            if not isinstance(hook, dict) or not isinstance(hook.get("on"), str) or hook["on"] not in allowed:
                raise RuntimeError(f"{label}.on: expected one of {sorted(allowed)}")
            actions = hook.get("do")
            if not isinstance(actions, list) or not all(isinstance(a, dict) for a in actions):
                raise RuntimeError(f"{label}.do: expected a list of actions")
            for action in actions:
                if action.get("type") not in {"execute_command", "create_dir", "write_json", "send_http_request", "inspect_file", "execute_routine"}:
                    raise RuntimeError(f"{label}.do: unsupported hook action {action.get('type')!r}")
                if action.get("type") == "execute_routine" and (not isinstance(action.get("routine"), str) or action["routine"] not in spec.get("routines", {})):
                    raise RuntimeError(f"{label}.do: undefined routine {action.get('routine')!r}")


def emit_event(spec, name, pipeline_name, context, states, workdir, *, node=None,
               status="running", error=None, skipped=False, blocked=False, ignored_error=None):
    """Run isolated, best-effort hooks; hook actions cannot emit lifecycle events."""
    pipeline = spec.get("pipeline", {}).get(pipeline_name, {})
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    event = {"name": name, "pipeline": {"name": pipeline_name, "status": status},
             "error": deepcopy(error)}
    if node is not None:
        event["pipeline"].pop("status")
        event["node"] = {**context.get("node", {}), "name": node["name"], "status": status,
                         "skipped": skipped, "blocked": blocked, "ignored_error": ignored_error}
    for scope in (spec, pipeline, *((node,) if node is not None else ())):
        for index, hook in enumerate(scope.get("events", [])):
            if hook["on"] != name:
                continue
            try:
                local = deepcopy(context)
                local["event"] = deepcopy(event)
                local["actions"] = {}
                if node is not None:
                    local["node"] = deepcopy(event["node"])
                for action_index, action in enumerate(hook["do"]):
                    run_action(action, local, dict(states), workdir, spec=spec, allow_workload=False,
                               label=f"event {name} hook[{index}].do[{action_index}]")
            except Exception as exc:
                print(f"[WARN] event {name} hook[{index}] failed: {exc}", flush=True)


def run_action(
    action: dict[str, Any], context: dict[str, Any], states: dict[str, str], workdir: Path,
    *, label: str = "action", spec: dict[str, Any] | None = None,
    device: ComputeDevice | None = None, routine_stack: tuple[str, ...] = (),
    allow_workload: bool = True,
) -> None:
    typ = action.get("type")
    if "action" in action:
        raise RuntimeError(f"{label}: use type, not action")
    if not isinstance(typ, str) or typ not in {"execute_command", "execute_workload", "create_dir", "write_json", "send_http_request", "inspect_file", "execute_routine"}:
        raise RuntimeError(f"{label}: unknown action type {typ!r}; use snake_case names")
    if typ == "execute_workload" and (spec is None or not allow_workload):
        raise RuntimeError(f"{label}: execute_workload is only supported in node before_run, run and after_run")
    if "save_response_to_context" in action:
        raise RuntimeError(f"{label}: save_response_to_context was removed; use save_state")
    if "save_state" in action and typ not in {"execute_command", "send_http_request", "inspect_file", "execute_routine"}:
        raise RuntimeError(f"{label}: save_state requires a command, HTTP, inspect_file or execute_routine action")
    _apply_context(context, _render(action.get("context", {}), context))
    if action.get("skip_if") and condition(action["skip_if"], context, states):
        print(f"[SKIP] {label} ({typ}): skip_if matched", flush=True)
        return
    if not condition(action.get("precondition"), context, states):
        print(f"[SKIP] {label} ({typ}): precondition did not match", flush=True)
        return
    values = _render({k: v for k, v in action.items() if k not in {"context", "skip_if", "precondition", "postcondition", "body", "content"}}, context)
    key = values.get("save_state")
    if "save_state" in values:
        if not isinstance(key, str) or not key.strip():
            raise RuntimeError(f"{label}.save_state must be a non-empty string")
        if "actions" in context and not isinstance(context["actions"], dict):
            raise RuntimeError(f"{label}: context.actions must be a mapping")
    result = {"success": False, "error": None}
    if typ == "execute_command":
        result.update(returncode=None, stdout="", stderr="")
    elif typ == "send_http_request":
        result.update(status_code=None, headers={}, body=None)
    started = time.perf_counter()
    print(f"[ACTION] {label}: {typ} started", flush=True)
    try:
        for field in ({"send_http_request": ["body"], "write_json": ["content"]}.get(typ, [])):
            if field in action:
                values[field] = render_payload(action[field], context)
        if typ == "execute_routine":
            name = action.get("routine")
            if not isinstance(name, str) or not name or re.search(r"[{}]", name):
                raise RuntimeError("execute_routine.routine must be a literal name")
            if name in routine_stack:
                raise RuntimeError(f"routine call cycle: {' -> '.join((*routine_stack, name))}")
            declaration = (spec or {}).get("routines", {}).get(name)
            if not isinstance(declaration, dict):
                raise RuntimeError(f"routine {name!r} is not defined")
            local = deepcopy(context)
            local["params"] = params_for(declaration.get("params", {}), values.get("args", {}), context, f"routine {name}")
            local["actions"] = {}
            actions = declaration.get("actions")
            if not isinstance(actions, list) or not all(isinstance(a, dict) for a in actions):
                raise RuntimeError(f"routine {name}.actions must be a list of action mappings")
            for index, child in enumerate(actions):
                run_action(child, local, states, workdir, label=f"{label}/{name}.actions[{index}]",
                           spec=spec, routine_stack=(*routine_stack, name), allow_workload=False)
            result["output"] = _render(declaration.get("returns"), local)
        elif typ == "inspect_file":
            target = values.get("target")
            if not isinstance(target, str) or not target.strip():
                raise RuntimeError("inspect_file.target must be a non-empty path")
            path = Path(target).expanduser()
            path = (path if path.is_absolute() else workdir / path).resolve()
            if not path.is_file():
                raise RuntimeError("inspect_file.target must be a regular file")
            with path.open("rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise RuntimeError("inspect_file.target must be a regular file")
                digest = hashlib.sha256()
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                after = os.fstat(source.fileno())
            current = path.stat()
            signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if signature(before) != signature(after) or signature(after) != signature(current):
                raise RuntimeError("file changed during inspection")
            result.update(path=str(path), name=path.name, size=after.st_size, sha256=digest.hexdigest())
        elif typ == "execute_command":
            target = values.get("target")
            args = values.get("args", [])
            if not target or not isinstance(args, list):
                raise RuntimeError("execute_command requires target and args")
            completed = _run_process(
                [str(target), *map(str, args)], cwd=workdir,
                env={**os.environ, **values.get("env", {})}, check=False,
                text=True, errors="replace",
            )
            result.update(returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
            if completed.returncode:
                raise subprocess.CalledProcessError(completed.returncode, completed.args, completed.stdout, completed.stderr)
        elif typ == "execute_workload":
            declaration = spec["workloads"][action["workload"]]
            execute_workload(spec, action["workload"], values.get("args", {}), context,
                             device if declaration.get("units", 0) > 0 else None, workdir)
        elif typ == "create_dir":
            Path(values["path"]).expanduser().mkdir(parents=True, exist_ok=True)
        elif typ == "write_json":
            path = Path(values["path"]).expanduser(); path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(values.get("content", {}), ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        elif typ == "send_http_request":
            if "body" in values and "body_file" in values:
                raise RuntimeError("send_http_request: body and body_file are mutually exclusive")
            timeout = values.get("timeout", 30)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                raise RuntimeError("send_http_request.timeout must be a positive finite number of seconds")
            def file_path(field):
                value = values[field]
                if not isinstance(value, str) or not value.strip():
                    raise RuntimeError(f"send_http_request.{field} must be a non-empty path")
                path = Path(value).expanduser()
                return (path if path.is_absolute() else workdir / path).resolve()
            destination = file_path("response_file") if "response_file" in values else None
            headers = request_headers(values.get("header", {}))
            server = context.get("session", {}).get("server")
            assignment = context.get("request", {}).get("assignmentVersion")
            if server and assignment is not None and urlsplit(values["url"])[:2] == urlsplit(server)[:2]:
                headers["X-Mu-Assignment-Version"] = str(assignment)
            with ExitStack() as resources:
                if "body_file" in values:
                    source = file_path("body_file")
                    payload = resources.enter_context(source.open("rb"))
                    headers = {k: v for k, v in headers.items() if k.lower() not in {"content-length", "transfer-encoding"}}
                    headers["Content-Length"] = str(os.fstat(payload.fileno()).st_size)
                    if not any(k.lower() == "content-type" for k in headers):
                        headers["Content-Type"] = "application/octet-stream"
                else:
                    data = values.get("body")
                    payload = json.dumps(data).encode() if data is not None else None
                req = Request(values["url"], data=payload, headers=headers, method=str(values.get("method", "get")).upper())
                http_error = None
                try:
                    response = build_opener(_SafeRedirect()).open(req, timeout=timeout)
                except HTTPError as exc:
                    response = exc
                    http_error = exc
                with response:
                    result.update(status_code=response.code, headers={k.lower(): v for k, v in response.headers.items()})
                    if destination is not None and http_error is None:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        temporary = None
                        try:
                            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".mu-download-", delete=False) as output:
                                temporary = Path(output.name)
                                size = 0
                                while chunk := response.read(1024 * 1024):
                                    output.write(chunk)
                                    size += len(chunk)
                            expected = response.headers.get("Content-Length")
                            if expected is not None and size != int(expected):
                                raise RuntimeError("Incomplete HTTP download: Content-Length does not match bytes received")
                            temporary.replace(destination)
                            result.update(response_file=str(destination), size=size)
                        finally:
                            if temporary is not None:
                                temporary.unlink(missing_ok=True)
                    else:
                        raw = response.read()
                        if raw:
                            text = raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
                            try:
                                result["body"] = json.loads(text)
                            except ValueError:
                                result["body"] = text
                if http_error is not None:
                    raise http_error
        result["success"] = True
        if key is not None:
            context.setdefault("actions", {})[key] = result
        if action.get("postcondition"):
            if not condition(action["postcondition"], context, states):
                raise RuntimeError("postcondition failed")
            print(f"[CHECK] {label}: postcondition passed", flush=True)
    except Exception as exc:
        result.pop("output", None)
        result.update(success=False, error={"type": type(exc).__name__, "message": str(exc)})
        if key is not None:
            context.setdefault("actions", {})[key] = result
        if action.get("ignore_error"):
            print(f"[WARN] {label}: ignored action error: {exc}", flush=True)
            return
        raise RuntimeError(f"{label} ({typ}) failed: {exc}") from exc
    elapsed = time.perf_counter() - started
    print(f"[ACTION] {label}: {typ} completed in {elapsed:.1f}s", flush=True)


def execute_workload(spec: dict[str, Any], name: str, supplied: dict[str, Any], context: dict[str, Any], device: ComputeDevice | None, workdir: Path) -> None:
    declaration = spec.get("workloads", {}).get(name)
    if not isinstance(declaration, dict): raise RuntimeError(f"workload {name!r} is not defined")
    workload_context = dict(context)
    workload_context["params"] = params_for(declaration.get("params", {}), supplied, context, f"workload {name}")
    target = _render(declaration.get("target", ""), workload_context)
    args = _render(declaration.get("args", []), workload_context)
    if not isinstance(args, list): raise RuntimeError(f"workload {name!r}.args must be a list")
    command = [str(target), *map(str, args)]
    if device:
        family = {"nvidia": "cuda", "rocm": "rocm"}.get(device.backend, device.backend)
        workload_context["schedule"] = {"device_family": family, "device_index": device.index}
        command += list(map(str, _render(declaration.get("device_args", []), workload_context)))
    env = {**os.environ, **_render(declaration.get("env", {}), workload_context)}
    units = declaration.get("units", 0)
    placement = f"{device.backend}:{device.index}" if device else "cpu"
    print(f"[WORKLOAD] {name}: starting {target} ({units} units; device {placement})", flush=True)
    started = time.perf_counter()
    result = _run_process(command, cwd=workdir, env=env, text=True)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"{target} exited with status {result.returncode}" + (f": {detail}" if detail else ""))
    print(f"[WORKLOAD] {name}: completed in {time.perf_counter() - started:.1f}s", flush=True)


def _node_result(node: dict[str, Any], state: str, error: str | None, label: str) -> tuple[str, str | None]:
    if state == "failed" and node.get("ignore_error") is True:
        print(f"[WARN] {label}: ignored node error: {error or 'node failed'}", flush=True)
        return "success", None
    return state, error


def _execute_node(
    spec: dict[str, Any], pipeline_name: str, node: dict[str, Any], context: dict[str, Any],
    states: dict[str, str], device: ComputeDevice | None, workdir: Path,
) -> tuple[str, str | None]:
    name = node["name"]
    node_context = deepcopy(context)
    node_context["node"] = {
        "name": name, "status": "running", "total_units": node_units(spec, node),
        "device": {
            "family": {"nvidia": "cuda", "rocm": "rocm"}.get(device.backend, device.backend) if device else "cpu",
            "index": device.index if device else None,
        },
    }
    emit_event(spec, "node_started", context.get("pipeline_name", pipeline_name), node_context, states, workdir, node=node)
    state, error = "success", None
    skipped = False
    started = time.perf_counter()
    print(f"[NODE] {pipeline_name}/{name}: starting", flush=True)
    try:
        _apply_context(node_context, _render(node.get("context", {}), node_context))
        if node.get("skip_if") and condition(node["skip_if"], node_context, states):
            skipped = True
            print(f"[SKIP] {pipeline_name}/{name}: skip_if matched", flush=True)
            if node.get("postcondition") and not condition(node["postcondition"], node_context, states):
                raise RuntimeError(f"node {name!r} postcondition failed after skip")
        else:
            if node.get("precondition"):
                if not condition(node["precondition"], node_context, states):
                    print(f"[CHECK] {pipeline_name}/{name}: precondition failed", flush=True)
                    raise RuntimeError(f"node {name!r} precondition failed")
                print(f"[CHECK] {pipeline_name}/{name}: precondition passed", flush=True)
            for phase in ("before_run", "run", "after_run"):
                for index, action in enumerate(node.get(phase, []), start=1):
                    run_action(action, node_context, states, workdir,
                               label=f"{pipeline_name}/{name} {phase}[{index}]", spec=spec, device=device)
            if node.get("postcondition"):
                if not condition(node["postcondition"], node_context, states):
                    raise RuntimeError(f"node {name!r} postcondition failed")
                print(f"[CHECK] {pipeline_name}/{name}: postcondition passed", flush=True)
    except Exception as exc:
        state, error = "failed", str(exc)
    final_states = {**states, name: state}
    for index, action in enumerate(node.get("finally", []), start=1):
        node_context["node"]["status"] = state
        try:
            run_action(action, node_context, final_states, workdir, label=f"{pipeline_name}/{name} finally[{index}]", spec=spec, allow_workload=False)
        except Exception as exc:
            state, error = "failed", str(exc)
            final_states[name] = state
    original_error = error
    state, error = _node_result(node, state, error, f"{pipeline_name}/{name}")
    node_context["node"]["status"] = state
    emit_event(spec, "node_finished", context.get("pipeline_name", pipeline_name), node_context,
               {**states, name: state}, workdir, node=node, status=state, skipped=skipped,
               error={"type": "RuntimeError", "message": error} if error else None,
               ignored_error=original_error if original_error and not error else None)
    elapsed = time.perf_counter() - started
    if state == "failed": print(f"[FAIL] {pipeline_name}/{name} after {elapsed:.1f}s: {error}", flush=True)
    else: print(f"[OK] {pipeline_name}/{name} {state} in {elapsed:.1f}s", flush=True)
    return state, error


def node_units(spec: dict[str, Any], node: dict[str, Any]) -> float:
    """Reserve the peak sequential workload demand before any node action starts."""
    peak = 0.0
    for phase in ("before_run", "run", "after_run"):
        for index, action in enumerate(node.get(phase, []), start=1):
            if action.get("type") != "execute_workload":
                continue
            label = f"node {node['name']!r}.{phase}[{index}]"
            name = action.get("workload")
            if not isinstance(name, str) or not name or "{" in name or "}" in name:
                raise RuntimeError(f"{label}.workload must be a literal non-empty workload name")
            declaration = spec.get("workloads", {}).get(name)
            if not isinstance(declaration, dict):
                raise RuntimeError(f"{label}.workload references undefined workload {name!r}")
            if not isinstance(action.get("args", {}), dict):
                raise RuntimeError(f"{label}.args must be a mapping of workload parameters")
            units = declaration.get("units", 0)
            if isinstance(units, bool) or not isinstance(units, (int, float)) or not math.isfinite(units) or units < 0:
                raise RuntimeError(f"workloads.{name}.units must be a finite non-negative number; got {units!r}")
            peak = max(peak, units)
    return peak


def validate_pipeline(definition: dict, pipeline_name: str) -> list[dict]:
    nodes = definition.get("nodes", [])
    if not isinstance(nodes, list): raise RuntimeError(f"pipeline {pipeline_name!r}.nodes must be a list")
    for node in nodes:
        if not isinstance(node, dict):
            raise RuntimeError(f"pipeline {pipeline_name!r}.nodes must contain mappings")
        for phase in ("before_run", "run", "after_run", "finally"):
            actions = node.get(phase, [])
            if not isinstance(actions, list) or not all(isinstance(action, dict) for action in actions):
                raise RuntimeError(f"node {node.get('name')!r}.{phase} must be a list of action mappings; use type: execute_workload to invoke a workload")
        if not isinstance(node.get("ignore_error", False), bool):
            raise RuntimeError(f"node {node.get('name')!r}.ignore_error must be a boolean")
    names = [node.get("name") for node in nodes]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise RuntimeError(f"pipeline {pipeline_name!r} node names must be unique non-empty strings")
    for node in nodes:
        dependencies = node.get("depends_on", [])
        if not isinstance(dependencies, list) or not all(isinstance(name, str) and name for name in dependencies):
            raise RuntimeError(f"node {node['name']!r}.depends_on must be a list of non-empty node name strings")
        for dependency in dependencies:
            if dependency not in names:
                raise RuntimeError(f"node {node['name']!r} depends on unknown node {dependency!r}")
    edges = {node["name"]: node.get("depends_on", []) for node in nodes}
    visiting, visited = set(), set()
    def visit(name):
        if name in visiting:
            raise RuntimeError(f"pipeline {pipeline_name!r}: dependency cycle at {name!r}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in edges[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
    for name in edges:
        visit(name)
    return nodes


def execute_pipelines(
    spec: dict[str, Any], requests: list[tuple[str, dict[str, Any]]], base: dict[str, Any],
    devices: list[ComputeDevice], workdir: Path,
    *, request_contexts: list[dict[str, Any]] | None = None,
    events_started: bool = False,
) -> list[dict[str, str]]:
    """Schedule ready nodes across all requests, respecting dependencies and GPU units."""
    validate_events(spec)
    jobs: dict[str, dict[str, Any]] = {}
    results: list[dict[str, str]] = []
    job_by_request_name: dict[tuple[int, str], str] = {}
    order: list[str] = []
    remaining = {}
    execution_contexts = []
    for request_index, (pipeline_name, request) in enumerate(requests):
        definition = spec.get("pipeline", {}).get(pipeline_name)
        if not isinstance(definition, dict): raise RuntimeError(f"pipeline {pipeline_name!r} is not defined")
        context = deepcopy(request_contexts[request_index] if request_contexts is not None else base)
        context[ENGINE_KEY] = engine_for(spec)
        if request_contexts is None:
            _apply_context(context, _render(definition.get("context", {}), context))
        context["pipeline_name"] = pipeline_name
        context["params"] = params_for(definition.get("params", {}), request, context, f"pipeline {pipeline_name}")
        if not events_started:
            emit_event(spec, "pipeline_started", pipeline_name, context, {}, workdir)
        nodes = validate_pipeline(definition, pipeline_name)
        print(f"[PIPELINE] {pipeline_name} request {request_index + 1}: queued ({len(nodes)} nodes)", flush=True)
        remaining[request_index] = len(nodes)
        execution_contexts.append(context)
        results.append({})
        for node in nodes:
            job_id = f"p{request_index}:{node['name']}"
            job_by_request_name[(request_index, node["name"])] = job_id
            order.append(job_id)
            jobs[job_id] = {
                "request_index": request_index, "pipeline": pipeline_name,
                "context": context, "node": node, "status": "pending", "units": node_units(spec, node),
            }

    for index, (pipeline_name, _) in enumerate(requests):
        if remaining[index] == 0:
            emit_event(spec, "pipeline_finished", pipeline_name,
                       execution_contexts[index], {}, workdir, status="success")

    def finish(job_id, state):
        job = jobs[job_id]
        index = job["request_index"]
        job["status"] = state
        results[index][job["node"]["name"]] = state
        remaining[index] -= 1
        if remaining[index] == 0:
            status = "failed" if "failed" in results[index].values() else "success"
            emit_event(spec, "pipeline_finished", job["pipeline"], job["context"], results[index], workdir,
                       status=status, error={"type": "RuntimeError", "message": "one or more nodes failed"} if status == "failed" else None)

    device_free = {device.index: float(device.capacity_units) for device in devices}
    futures: dict[Future[tuple[str, str | None]], tuple[str, ComputeDevice | None, float]] = {}
    max_workers = max(1, min(32, len(jobs)))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mu-node") as pool:
        while any(job["status"] in {"pending", "running"} for job in jobs.values()):
            launched = False
            for job_id in order:
                job = jobs[job_id]
                if job["status"] != "pending": continue
                request_index = job["request_index"]
                node = job["node"]
                dependencies = node.get("depends_on", [])
                dependency_jobs = [jobs[job_by_request_name[(request_index, name)]] for name in dependencies]
                if any(dependency["status"] in {"pending", "running"} for dependency in dependency_jobs):
                    continue
                pipeline_states = results[request_index]
                failed_dependency = next((dependency for dependency in dependency_jobs if dependency["status"] != "success"), None)
                if failed_dependency is not None:
                    dependency_name = failed_dependency["node"]["name"]
                    detail = f"dependency {dependency_name} failed"
                    print(f"[BLOCKED] {job['pipeline']}/{node['name']}: {detail}", flush=True)
                    state, _ = _node_result(node, "failed", detail, f"{job['pipeline']}/{node['name']}")
                    emit_event(spec, "node_finished", job["pipeline"], job["context"],
                               {**pipeline_states, node["name"]: state}, workdir, node=node, status=state,
                               blocked=True, error={"type": "DependencyError", "message": detail} if state == "failed" else None,
                               ignored_error=detail if state == "success" else None)
                    finish(job_id, state)
                    continue

                units = job["units"]
                device: ComputeDevice | None = None
                if units > 0:
                    fitting = [candidate for candidate in devices if candidate.capacity_units >= units]
                    if fitting:
                        available = [candidate for candidate in fitting if device_free[candidate.index] >= units]
                        if not available:
                            continue
                        device = min(available, key=lambda candidate: device_free[candidate.index] - units)
                    else:
                        print(f"[SCHEDULE] {job['pipeline']}/{node['name']}: no GPU fits {units:g} units; using CPU", flush=True)
                if len(futures) >= max_workers:
                    continue
                job["status"] = "running"
                if device:
                    device_free[device.index] -= units
                print(f"[SCHEDULE] request {request_index + 1} {job['pipeline']}/{node['name']}: dispatched ({units:g} units; {device.backend + ':' + device.index if device else 'cpu'})", flush=True)
                future = pool.submit(
                    _execute_node, spec, f"{job['pipeline']} request {request_index + 1}", node, job["context"],
                    dict(pipeline_states), device, workdir,
                )
                futures[future] = (job_id, device, units)
                launched = True

            if not futures:
                if any(job["status"] == "pending" for job in jobs.values()):
                    raise RuntimeError("scheduler deadlock: pending nodes cannot be assigned")
                break
            if not launched or len(futures) >= max_workers:
                completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            else:
                continue
            for future in completed:
                job_id, device, units = futures.pop(future)
                if device: device_free[device.index] += units
                runner_error = None
                try:
                    state, error = future.result()
                except Exception as exc:
                    state, error = "failed", f"unexpected node runner error: {exc}"
                    runner_error = error
                job = jobs[job_id]
                state, error = _node_result(job["node"], state, error, f"{job['pipeline']}/{job['node']['name']}")
                if runner_error:
                    emit_event(spec, "node_finished", job["pipeline"], job["context"],
                               {**results[job["request_index"]], job["node"]["name"]: state}, workdir,
                               node=job["node"], status=state,
                               error={"type": "RuntimeError", "message": error} if error else None,
                               ignored_error=runner_error if not error else None)
                finish(job_id, state)
                if state == "failed" and error:
                    print(f"[NODE-ERROR] {job['pipeline']}/{job['node']['name']}: {error}", flush=True)

    for index, (pipeline_name, _) in enumerate(requests):
        states = results[index]
        succeeded = sum(value == "success" for value in states.values())
        failed = sum(value == "failed" for value in states.values())
        print(f"[PIPELINE] {pipeline_name} request {index + 1}: finished ({succeeded} succeeded, {failed} failed)", flush=True)
    return results


def execute_pipeline(spec: dict[str, Any], pipeline_name: str, request: dict[str, Any], base: dict[str, Any], devices: list[ComputeDevice], workdir: Path) -> dict[str, str]:
    """Compatibility wrapper for scheduling one pipeline request."""
    return execute_pipelines(spec, [(pipeline_name, request)], base, devices, workdir)[0]


class Flow:
    """One entry invocation; preparation caches never survive into another flow."""

    def __init__(self, spec: dict, entry: str, base: dict, devices: list[ComputeDevice], workdir: Path):
        if "prepare" in spec:
            raise RuntimeError("spec.prepare was removed; move actions to pipeline.<name>.prepare or entries.<entry>.initialization")
        if "master-flow" in spec:
            raise RuntimeError("spec.master-flow was removed; use entries.master_flow and server request claiming")
        engine_for(spec)
        validate_templates(spec)
        validate_routines(spec)
        validate_events(spec)
        entries = spec.get("entries", {})
        if not isinstance(entries, dict):
            raise RuntimeError("entries must be a mapping")
        for name, value in entries.items():
            if name not in {"direct_flow", "master_flow"} or not isinstance(value, dict):
                raise RuntimeError(f"entries.{name}: expected direct_flow/master_flow mapping")
            for phase in ("initialization", "finalization"):
                self.validate_actions(value.get(phase, []), f"entries.{name}.{phase}")
        pipelines = spec.get("pipeline", {})
        if not isinstance(pipelines, dict):
            raise RuntimeError("pipeline must be a mapping")
        for name, value in pipelines.items():
            if not isinstance(value, dict):
                raise RuntimeError(f"pipeline.{name} must be a mapping")
            self.validate_actions(value.get("prepare", []), f"pipeline.{name}.prepare")
        self.spec, self.devices, self.workdir = spec, devices, workdir
        self.context = deepcopy(base)
        self.context[ENGINE_KEY] = engine_for(spec)
        _request_scopes(self.context, {})
        self.context["entry"] = {"name": entry, "status": "running"}
        self.lifecycle = entries.get(entry, {})
        self.prepared: dict[str, dict] = {}
        self.prepare_errors: dict[str, Exception] = {}

    @staticmethod
    def validate_actions(actions: Any, label: str) -> None:
        if not isinstance(actions, list) or not all(isinstance(a, dict) for a in actions):
            raise RuntimeError(f"{label} must be a list of action mappings")

    def initialize(self) -> None:
        for index, action in enumerate(self.lifecycle.get("initialization", [])):
            run_action(action, self.context, {}, self.workdir, label=f"entry initialization[{index}]", spec=self.spec, allow_workload=False)

    def finalize(self, failed: bool) -> None:
        self.context["entry"]["status"] = "failed" if failed else "success"
        errors = []
        for index, action in enumerate(self.lifecycle.get("finalization", [])):
            try:
                run_action(action, self.context, {}, self.workdir, label=f"entry finalization[{index}]", spec=self.spec, allow_workload=False)
            except Exception as exc:
                errors.append(str(exc))
                self.context["entry"]["status"] = "failed"
        if errors:
            raise RuntimeError("entry finalization: " + "; ".join(errors))

    def execute(self, requests: list[tuple[str, dict, dict]]) -> bool:
        queued, contexts = [], []
        failed = False
        for name, payload, request in requests:
            context = deepcopy(self.context)
            context["request"] = deepcopy(request)
            _request_scopes(context, request)
            context["pipeline_name"] = name
            emit_event(self.spec, "pipeline_started", name, context, {}, self.workdir)
            try:
                definition = self.spec.get("pipeline", {}).get(name)
                if not isinstance(definition, dict):
                    raise RuntimeError(f"pipeline {name!r} is not defined")
                if not isinstance(payload, dict):
                    raise RuntimeError(f"pipeline {name!r}: request payload must be a mapping")
                for node in validate_pipeline(definition, name):
                    node_units(self.spec, node)
                context = deepcopy(self.prepared.get(name, self.context))
                validation_context = deepcopy(context)
                _request_scopes(validation_context, request)
                validation_context["request"] = deepcopy(request)
                validation_context["pipeline_name"] = name
                _apply_context(validation_context, _render(definition.get("context", {}), validation_context))
                params_for(definition.get("params", {}), payload, validation_context, f"pipeline {name}")
                if name in self.prepare_errors:
                    raise self.prepare_errors[name]
                if name not in self.prepared:
                    try:
                        for index, action in enumerate(definition.get("prepare", [])):
                            run_action(action, context, {}, self.workdir, label=f"pipeline {name} prepare[{index}]", spec=self.spec, allow_workload=False)
                        from mu.pipelines import _discover_models
                        context["sys"]["models"] = _discover_models(Path(context["sys"]["cache_dir"]))
                        self.prepared[name] = context
                    except Exception as exc:
                        self.prepare_errors[name] = exc
                        raise
                context = deepcopy(self.prepared[name])
                _request_scopes(context, request)
                context["request"] = deepcopy(request)
                context["pipeline_name"] = name
                _apply_context(context, _render(definition.get("context", {}), context))
                params_for(definition.get("params", {}), payload, context, f"pipeline {name}")
                context["pipeline_name"] = name
                if request:
                    context["request"] = deepcopy(request)
                queued.append((name, payload))
                contexts.append(context)
            except Exception as exc:
                context["request"] = deepcopy(request)
                emit_event(self.spec, "pipeline_finished", name, context, {}, self.workdir, status="failed",
                           error={"type": type(exc).__name__, "message": str(exc)})
                print(f"[REQUEST] {name}: failed: {exc}", flush=True)
                failed = True
        if queued:
            results = execute_pipelines(
                self.spec, queued, self.context, self.devices, self.workdir, request_contexts=contexts, events_started=True,
            )
            failed |= any(state == "failed" for result in results for state in result.values())
        return failed
