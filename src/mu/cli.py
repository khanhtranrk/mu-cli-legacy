from pathlib import Path
import shlex

import typer

from mu.spec import SpecError, repair_spec, validate_spec
from mu.pipelines import PipelineBuildError, build_pipelines, list_model_files
from mu.tasks import Scheduler, TaskGraphError, pipelines_to_tasks
from mu.hardware import detect_devices

app = typer.Typer(no_args_is_help=True)
spec_app = typer.Typer(no_args_is_help=True)
app.add_typer(spec_app, name="spec")
tool_app = typer.Typer(no_args_is_help=True)
app.add_typer(tool_app, name="tool")




def _validation_context(cache_dir: Path) -> dict[str, str]:
    return {
        "cache_dir": str(cache_dir.expanduser().resolve()),
        "working_dir": str(Path.cwd()),
    }


def _print_plan_report(
    pipeline: str,
    tasks: list[dict],
    validations: list,
    devices: list,
    max_units: float | None,
) -> None:
    typer.echo(f"\nExecution plan: {pipeline}")
    typer.echo("Repair validation (read-only)")
    if not validations:
        typer.echo("  No repair validation checks are declared.")
    for result in validations:
        label = typer.style("PASS" if result.passed else "FAIL", fg="green" if result.passed else "red", bold=True)
        typer.echo(f"  {label}  {result.message}")
    typer.echo("  Run repairs unmet checks before processing tasks.")

    typer.echo("Hardware")
    if devices:
        for device in devices:
            typer.echo(f"  GPU {device.index}: {device.name}, {device.vram_gib:.1f} GiB ({device.capacity_units} units)")
    else:
        typer.echo("  No supported GPU detected; GPU tasks will require CPU fallback confirmation in run.")

    scheduler = Scheduler(tasks, max_units=max_units)
    ready_ids = {task["id"] for task in scheduler.ready_tasks()}
    typer.echo("Planned tasks")
    for task in tasks:
        if float(task["units"]) == 0:
            placement = "CPU (0 VRAM units)"
        else:
            candidates = [device.index for device in devices if device.capacity_units >= float(task["units"])]
            placement = f"GPU candidates: {', '.join(candidates)}" if candidates else "no GPU fits; CPU fallback confirmation needed"
        if task["id"] in ready_ids:
            state, color = "READY", "green"
        elif max_units is not None and float(task["units"]) > max_units:
            state, color = "OVER LIMIT", "red"
        else:
            state, color = "WAITING", "yellow"
        label = typer.style(state, fg=color, bold=True)
        item_name = task.get("item", task["request"]).get("name", "(unnamed)")
        dependencies = f"; after {', '.join(task['waiting_for'])}" if task["waiting_for"] else ""
        typer.echo(
            f"  {label} {task['id']}  {item_name} / {task['name']} → {task['processor']} "
            f"[{task['units']} units; ~{float(task['units']) * 4:g} GiB; {placement}{dependencies}]"
        )
        config = task.get("processor_config", {})
        target = config.get("target")
        params = config.get("params", [])
        device_args = config.get("device_args", [])
        if isinstance(target, str) and isinstance(params, list):
            command = [target, *(str(arg) for arg in params)]
            if isinstance(device_args, list):
                command.extend(str(arg) for arg in device_args)
            typer.echo(f"       command: {shlex.join(command)}")
        step_config = task.get("step_config", {})
        before_actions = step_config.get("before", []) if isinstance(step_config, dict) else []
        for action in (before_actions if isinstance(before_actions, list) else []):
            if isinstance(action, dict):
                details = action.get("path", action.get("output", action.get("target", "")))
                typer.echo(f"       before: {action.get('action', 'unknown')} {details}".rstrip())

    failures = sum(not result.passed for result in validations)
    typer.echo(
        f"Plan summary: {len(tasks)} tasks; "
        f"repair checks {len(validations) - failures} passed, {failures} need attention."
    )


@spec_app.command("validate")
def spec_validate(
    spec: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    cache_dir: Path = typer.Option(Path.home() / ".cache", "--cache_dir", help="Cache directory used by spec templates."),
) -> None:
    """Validate the checks declared in a spec file."""
    try:
        results = validate_spec(spec, context={"cache_dir": str(cache_dir.expanduser().resolve()), "working_dir": str(Path.cwd())})
    except SpecError as exc:
        typer.echo(f"Spec error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    for result in results:
        typer.echo(f"{'PASS' if result.passed else 'FAIL'} {result.message}")
    failures = sum(not result.passed for result in results)
    if failures:
        typer.echo(f"Validation failed ({failures} check(s)).", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Validation passed ({len(results)} check(s)).")


@spec_app.command("repair")
def spec_repair(
    spec: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    force_validates: bool = typer.Option(False, "--force-validates", help="Run a final validation pass after all repair actions."),
    cache_dir: Path = typer.Option(Path.home() / ".cache", "--cache_dir", help="Cache directory available to action templates."),
) -> None:
    """Run repair actions declared in a spec file."""
    try:
        results = repair_spec(spec, cache_dir=cache_dir, force_validates=force_validates)
    except SpecError as exc:
        typer.echo(f"Repair failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    for result in results:
        typer.echo(f"{'PASS' if result.passed else 'FAIL'} {result.message}")
    failures = sum(not result.passed for result in results)
    if failures:
        typer.echo(f"Repair validation failed ({failures} check(s)).", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Repair completed ({len(results)} check(s)).")


@app.command("models")
def models(
    cache_dir: Path = typer.Option(Path.home() / ".cache", "--cache_dir", help="Cache directory containing models."),
) -> None:
    """List GGUF and safetensors model files found in the cache."""
    files = list_model_files(cache_dir.expanduser())
    if not files:
        typer.echo(f"No models found in {cache_dir.expanduser() / 'models'}.")
        return
    models_dir = (cache_dir.expanduser() / "models").resolve()
    for path in files:
        key = path.relative_to(models_dir).with_suffix("").as_posix()
        typer.echo(f"{key} -> {path}")


@app.command()
def plan(
    pipeline: str = typer.Argument(..., help="Pipeline name from the spec."),
    input_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    cache_dir: Path = typer.Option(Path.home() / ".cache", "--cache_dir", help="Cache directory."),
    output_dir: Path = typer.Option(Path.cwd() / "output", "--output_dir", help="Output directory."),
    spec: Path = typer.Option(Path.cwd() / "spec.yaml", "--spec", exists=True, dir_okay=False, readable=True),
    max_units: float | None = typer.Option(None, "--max-units", min=0.01, help="Optional aggregate processor unit capacity."),
) -> None:
    """Validate repair prerequisites and preview task scheduling without running actions."""
    try:
        validations = validate_spec(spec, context=_validation_context(cache_dir))
        pipelines = build_pipelines(spec, input_path, [pipeline], cache_dir=cache_dir, output_dir=output_dir)
        tasks = pipelines_to_tasks(pipelines)
    except (SpecError, PipelineBuildError, TaskGraphError) as exc:
        typer.echo(f"Plan failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _print_plan_report(pipeline, tasks, validations, detect_devices(), max_units)



transcriptor_app = typer.Typer(no_args_is_help=True)
tool_app.add_typer(transcriptor_app, name="transcriptor")


@transcriptor_app.command("evaluate")
def transcriptor_evaluate(
    transcript_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="WhisperX output JSON file"),
    lyrics_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="Lyrics JSON file"),
    output_type: str = typer.Option("pretty", "--output-type", help="Output format: pretty, plaintext, json, yaml"),
    overall_min: float = typer.Option(0.85, "--overall-min", min=0, max=1, help="Overall similarity threshold"),
    local_min: float = typer.Option(0.60, "--local-min", min=0, max=1, help="Local window coverage threshold"),
    window: int = typer.Option(25, "--window", min=1, help="Window size for local coverage check"),
    step: int = typer.Option(5, "--step", min=1, help="Step size for sliding window"),
    line_min: float = typer.Option(0.50, "--line-min", min=0, max=1, help="Minimum line coverage threshold"),
) -> None:
    """Evaluate WhisperX transcript against lyrics."""
    from mu.transcriptor import evaluate
    valid_types = ["pretty", "plaintext", "json", "yaml"]
    if output_type not in valid_types:
        typer.echo(f"Invalid --output-type: {output_type}. Must be one of: {', '.join(valid_types)}", err=True)
        raise typer.Exit(code=2)
    try:
        evaluate(
            transcript_path=transcript_path,
            lyrics_path=lyrics_path,
            output_type=output_type,
            overall_min=overall_min,
            local_min=local_min,
            window=window,
            step=step,
            line_min=line_min,
        )
    except Exception as exc:
        typer.echo(f"Evaluation failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@transcriptor_app.command("convert")
def transcriptor_convert(
    transcript_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="WhisperX output JSON file"),
    lyrics_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="Lyrics JSON file"),
    target_type: str = typer.Option("k-ass", "--target-type", help="Target format: k-ass (karaoke ASS), ass, srt"),
    output: Path = typer.Option(None, "--output", help="Output file path (default: karaoke.ass, lyrics.ass, or lyrics.srt)"),
    min_timing_coverage: float = typer.Option(0.50, "--min-timing-coverage", min=0, max=1, help="Minimum timing coverage per line"),
) -> None:
    """Convert WhisperX transcript to subtitle format."""
    from mu.transcriptor import convert
    valid_types = ["k-ass", "ass", "srt"]
    if target_type not in valid_types:
        typer.echo(f"Invalid --target-type: {target_type}. Must be one of: {', '.join(valid_types)}", err=True)
        raise typer.Exit(code=2)
    try:
        convert(
            transcript_path=transcript_path,
            lyrics_path=lyrics_path,
            target_type=target_type,
            output=output,
            min_timing_coverage=min_timing_coverage,
        )
    except Exception as exc:
        typer.echo(f"Conversion failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc


flow_app = typer.Typer(no_args_is_help=True)
app.add_typer(flow_app, name="master-flow")


def _runtime_base(working_dir: Path, sys_cache_dir: Path, unit_name: str) -> dict:
    working_dir = working_dir.expanduser().resolve()
    session_cache = working_dir / "cache"
    output_dir = working_dir / "output"
    session_cache.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    from mu.pipelines import _discover_models
    return {"sys": {"cache_dir": str(sys_cache_dir.expanduser().resolve()),
                    "models": _discover_models(sys_cache_dir.expanduser().resolve())},
            "session": {"working_dir": str(working_dir), "unit_name": unit_name,
                        "output_dir": str(output_dir), "cache_dir": str(session_cache)}}


def _load_runtime_spec(path: Path) -> dict:
    from mu.runtime import read_data
    value = read_data(path)
    if not isinstance(value, dict): raise ValueError("spec root must be a mapping")
    return value


def _runtime_devices(values: list[str]):
    if not values:
        return detect_devices()
    from mu.hardware import ComputeDevice
    devices = []
    for value in values:
        family, index, capacity = value.split(",", 2)
        devices.append(ComputeDevice(index, family, float(capacity) * 4, family))
    return devices


def _execute_flow(flow, body) -> None:
    import signal
    import threading
    from mu.runtime import terminate_processes, reset_interruption
    reset_interruption()
    previous = None
    def interrupt(signum, frame):
        terminate_processes()
        raise KeyboardInterrupt
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGINT, interrupt)
    failed = True
    try:
        flow.initialize()
        failed = body()
        if failed:
            raise ValueError("one or more pipeline requests failed")
    finally:
        import sys
        original = sys.exc_info()[1]
        reset_interruption()
        try:
            flow.finalize(failed)
        except Exception as exc:
            if original is None:
                raise
            typer.echo(f"Finalization failed: {exc}", err=True)
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)


@app.command("run")
def run_requests(
    pipeline: list[str] = typer.Option([], "--pipeline", help="PIPELINE,REQUEST.yaml (repeatable)."),
    working_dir: Path = typer.Option(Path.cwd(), "--working_dir"),
    sys_cache_dir: Path = typer.Option(Path.home() / ".cache", "--sys_cache_dir"),
    unit_name: str = typer.Option("unknown", "--unit_name"),
    spec: Path = typer.Option(Path.cwd() / "spec.yaml", "--spec"),
    device: list[str] = typer.Option([], "--device", help="family,index,max-units (repeatable)."),
    request: list[str] = typer.Option([], "--request", hidden=True),
) -> None:
    """Execute local requests using entries.direct_flow."""
    from mu.runtime import Flow, read_data
    if request:
        typer.echo("--request was removed; use --pipeline PIPELINE,FILE", err=True)
        raise typer.Exit(2)
    if not pipeline:
        typer.echo("At least one --pipeline PIPELINE,FILE is required", err=True)
        raise typer.Exit(2)
    try:
        definition = _load_runtime_spec(spec)
        base = _runtime_base(working_dir, sys_cache_dir, unit_name)
        parsed = []
        for item in pipeline:
            name, filename = item.split(",", 1)
            params = read_data(Path(filename))
            if isinstance(params, dict):
                params = [params]
            if not isinstance(params, list) or not all(isinstance(value, dict) for value in params):
                raise ValueError(f"request {filename} must contain a mapping or a list of mappings")
            parsed.extend((name, value, {}) for value in params)
        flow = Flow(definition, "direct_flow", base, _runtime_devices(device), Path(base["session"]["working_dir"]))
        _execute_flow(flow, lambda: flow.execute(parsed))
    except Exception as exc:
        typer.echo(f"Run failed: {exc}", err=True)
        raise typer.Exit(1) from exc


@flow_app.command("run")
def master_flow_run(
    spec_slug: str = typer.Argument(...),
    server: str = typer.Option(None, "--server", envvar="MU_MASTER_FLOW_SERVER"),
    token: str = typer.Option(None, "--token", envvar="MU_MASTER_FLOW_TOKEN"),
    batch_size: int = typer.Option(1, "--batch-size", min=1, max=100),
    by_pipeline: list[str] = typer.Option([], "--by-pipeline"),
    by_status: list[str] = typer.Option([], "--by-status"),
    by_unit_id: str = typer.Option(None, "--by-unit-id"),
    unit_id: str = typer.Option(None, "--unit-id"),
    unit_type: str = typer.Option("unknown", "--unit-type"),
    refresh_loop: bool = typer.Option(False, "--refresh-loop"),
    working_dir: Path = typer.Option(Path.cwd(), "--working_dir"),
    sys_cache_dir: Path = typer.Option(Path.home() / ".cache", "--sys_cache_dir"),
    device: list[str] = typer.Option([], "--device"),
) -> None:
    """Claim and execute server requests using entries.master_flow."""
    import socket
    from urllib.parse import quote
    from mu.runtime import Flow
    from mu.master_flow import MasterFlowClient
    if not server or not token:
        typer.echo("Set --server/--token or MU_MASTER_FLOW_SERVER/MU_MASTER_FLOW_TOKEN", err=True)
        raise typer.Exit(2)
    try:
        unit_id = unit_id or socket.gethostname()
        if not unit_id.strip():
            raise ValueError("--unit-id must be non-empty")
        for label, values in (("--by-pipeline", by_pipeline), ("--by-status", by_status)):
            if any(not value.strip() for value in values) or len(set(values)) > 100:
                raise ValueError(f"{label} requires at most 100 non-empty values")
        if max(1, len(set(by_pipeline))) * max(1, len(set(by_status))) > 100:
            raise ValueError("at most 100 pipeline/status filter combinations are supported")
        if by_unit_id is not None and not by_unit_id.strip():
            raise ValueError("--by-unit-id must be non-empty")
        client = MasterFlowClient(server, token)
        base = _runtime_base(working_dir, sys_cache_dir, unit_id)
        base["session"].update(
            unit_id=unit_id, unit_type=unit_type, server=client.server, token=client.token,
        )
        devices = _runtime_devices(device)
        heartbeat = {"unitId": unit_id, "unitType": unit_type,
                     "workingDir": base["session"]["working_dir"],
                     "devices": [{"family": d.backend, "index": int(d.index)} for d in devices]}
        typer.echo(f"[MASTER-FLOW] Registering unit {unit_id}")
        client.post("/api/units/heartbeat", {**heartbeat, "status": "online"})
        try:
            definition = client.spec("/api/specs/" + quote(spec_slug, safe=""))
            typer.echo(f"[MASTER-FLOW] Spec loaded: {spec_slug}")
            flow = Flow(definition, "master_flow", base, devices, Path(base["session"]["working_dir"]))
            from mu.expressions import engine_for
            selection = {"expressionEngine": engine_for(definition), "unitId": unit_id, "specSlug": spec_slug, "maxCount": batch_size,
                         "byStatus": by_status or ["waiting"]}
            if by_pipeline:
                selection["byPipeline"] = by_pipeline
            if by_unit_id is not None:
                selection["byUnitId"] = by_unit_id
            def batches():
                seen = set()
                while True:
                    typer.echo(f"[MASTER-FLOW] Claiming up to {batch_size} request(s) for {spec_slug}")
                    batch = client.post("/api/requests/claim", selection)
                    if not isinstance(batch, list) or len(batch) > batch_size:
                        raise ValueError("claim response must be an array within batch-size")
                    if not batch:
                        typer.echo("[MASTER-FLOW] No matching requests; exiting (status filter: " + ", ".join(selection["byStatus"]) + ")")
                        return False
                    typer.echo(f"[MASTER-FLOW] Claimed {len(batch)} request(s)")
                    parsed = []
                    for item in batch:
                        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k] for k in ("requestId", "specSlug", "pipelineName")) or "payload" not in item:
                            raise ValueError("invalid claim item: expected requestId, specSlug, pipelineName and payload")
                        if item["requestId"] in seen:
                            raise ValueError("server reclaimed a request already seen in this flow; narrow --by-status")
                        seen.add(item["requestId"])
                        metadata = {"artifacts": item.get("artifacts", []), "registryContext": item.get("registryContext", {}), "requestId": item["requestId"], "specSlug": item["specSlug"], "assignmentVersion": item.get("assignmentVersion")}
                        if not isinstance(metadata["artifacts"], list):
                            raise ValueError("claim artifacts must be an array")
                        if not isinstance(metadata["assignmentVersion"], int) or isinstance(metadata["assignmentVersion"], bool) or metadata["assignmentVersion"] < 1:
                            raise ValueError("claim response requires a positive assignmentVersion; update MasterFlow")
                        if item["specSlug"] != spec_slug:
                            raise ValueError("claim returned a different specSlug")
                        parsed.append((item["pipelineName"], item["payload"], metadata))
                    failed = flow.execute(parsed)
                    if failed or not refresh_loop:
                        return failed
            _execute_flow(flow, batches)
        finally:
            import sys
            original = sys.exc_info()[1]
            try:
                client.post("/api/units/heartbeat", {**heartbeat, "status": "offline"})
                typer.echo(f"[MASTER-FLOW] Unit {unit_id}: offline")
            except Exception as exc:
                if original is None:
                    raise
                typer.echo(f"Offline heartbeat failed: {exc}", err=True)
    except Exception as exc:
        typer.echo(f"Master flow failed: {exc}", err=True)
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    app()
