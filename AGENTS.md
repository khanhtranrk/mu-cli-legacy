# Instructions for coding agents

## Project overview

Mu is a Python 3.13+ CLI that loads YAML or JSON specs and executes parameterized, dependency-aware subprocess pipelines. Use `uv` for project commands and dependency management. The runtime is the source of truth for spec behavior; the maintained format guide is under [`docs/spec-format/`](docs/spec-format/README.md).

## Architecture

The current runtime and legacy compatibility paths serve different spec formats. Read the relevant module before changing behavior:

- `src/mu/runtime.py`: workload/pipeline spec execution, context rendering, conditions, actions, node lifecycle, request scheduling, and remote master-flow support.
- `src/mu/cli.py`: Typer commands, local and remote request loading, GPU option parsing, and user-facing reports.
- `src/mu/hardware.py`: NVIDIA/AMD detection and 4 GiB scheduling-unit capacity.
- `src/mu/pipelines.py`: legacy processor/pipeline expansion, model discovery, and template rendering.
- `src/mu/tasks.py`: legacy processor task graph and scheduler.
- `src/mu/spec.py`: legacy spec loading and repair validation.
- `src/mu/actions.py`: legacy pluggable action registry.
- `src/mu/validators.py`: legacy pluggable validation registry.
- `src/mu/transcriptor.py`: WhisperX transcript evaluation and subtitle conversion (SRT, ASS, karaoke).

Do not assume that fields in the legacy `processors`/`pipelines`/`steps` format match fields in the workload/pipeline/node format. Add format behavior to the owning module rather than introducing CLI-only branches.

## Runtime behavior to preserve

- The workload/pipeline format uses `workloads`, singular `pipeline`, and node action lists with `type: execute_workload`; the media example uses this format.
- Entry lifecycle and lazy pipeline preparation follow [the lifecycle guide](docs/spec-format/07-master-flow-and-cli.md).
- For the complete request → pipeline preparation → node-job construction → GPU admission → node execution → reporting path, read [the request-to-node scheduling workflow](docs/request-to-node-scheduling.md) before changing the scheduler or reporting behavior.
- Every request gets an isolated pipeline context. Pipeline context is merged once, parameter values are validated, and node contexts are copied before node-local additions.
- The base context contains `sys` and `session`. Pipelines add `params`; nodes add `node`; scheduled GPU workloads add `schedule`.
- Workload `params` are a declared schema. Missing required values and unknown supplied values are errors; string and number types are not implicitly coerced.
- Workload args are an argv list. Target and arguments are rendered and passed directly to subprocesses; Mu does not invoke an undocumented shell command.
- Node state is canonical `running`, `success`, or `failed`. `node_status` also accepts `succeeded` as an alias for `success`.
- Node execution order is: merge context, `skip_if`, `precondition`, `before_run`, workload, `after_run`, `postcondition`, then `finally`.
- `skip_if` is a successful skip. A failed `precondition` fails the node. A required action or postcondition failure fails the node unless `ignore_error` is true.
- `finally` runs after success, skip, or failure and is the correct place for failure notifications or cleanup.
- Dependency condition failures block dependent nodes. Failed nodes prevent their dependents from running.
- The scheduler runs ready nodes together, with at most 32 concurrent nodes. GPU units are reserved for the full node lifetime and released afterward.
- `units: 0` is CPU-only. GPU units are direct workload requests in 4 GiB bands; device argument syntax belongs in `device_args`.
- A node with no postcondition succeeds based on process/action success. Declared postconditions take precedence over the implicit result.
- Successful subprocess output is kept out of normal CLI output. Progress and error detail should remain concise and actionable.
- `mu master-flow run` uses the worker API and batch lifecycle in [the CLI guide](docs/spec-format/07-master-flow-and-cli.md).

## Development workflow

- Use the repository skill [`.github/skills/create-mu-spec/SKILL.md`](.github/skills/create-mu-spec/SKILL.md) when creating or modifying a Mu spec. It provides the required format workflow and safe validation checklist.
- Read [`README.md`](README.md), the relevant module, and [`examples/song-with-cover/spec.yaml`](examples/song-with-cover/spec.yaml) before schema or runtime changes.
- Use `uv run ...` for project commands and `uv run mu --help` for a safe CLI smoke test.
- Prefer the smallest relevant test selection. Run broad tests only for cross-module changes or when explicitly requested.
- Do not execute the media example through `mu run` or `mu master-flow run` as a routine check. It may download models, install tools, use GPU resources, or write substantial generated media.
- Avoid network, GPU, and large-file operations unless the task explicitly requires them. Use a temporary minimal spec for side-effect validation.
- Keep generated output, caches, virtual environments, downloaded models, credentials, and machine-specific paths out of commits.
- Update [`README.md`](README.md), [`docs/spec-format/`](docs/spec-format/README.md), and the example YAML together when user-facing commands or schemas change.
- Link to existing documentation instead of duplicating it into agent instructions.

## Adding spec features

- New condition operators belong in the condition registry or the node runtime condition dispatcher, depending on the format being changed.
- New action types belong in `run_action`/the action registry, with explicit validation and action-specific context handling.
- Keep schema errors actionable: report the declaration, field, expected type or value, and offending key.
- Preserve context isolation. Render from a copy, merge declared context deliberately, and never mutate a sibling request or node context.
- Preserve ordering when the spec promises it. Independent nodes may run concurrently, but `finally` always remains at the end of the node lifecycle.
- Update both the technical guide and at least one executable example when introducing a public field.

## Adding standalone tools

Standalone tools (accessed via `mu tool <name>`) are separate from spec-based execution and implement specialized functionality:

- Create a new module under `src/mu/` with the tool's core logic (e.g., `src/mu/transcriptor.py`).
- Add a new typer sub-app under `tool_app` in `src/mu/cli.py` for the tool's commands.
- Follow the pattern of `transcriptor_app` for command structure: import the module, define command functions with proper argument validation, and handle errors consistently.
- Add documentation in `docs/tool/` following the existing format (see `docs/tool/transcriptor.md` for a complete example).
- Update `docs/tool/README.md` to include the new tool in the available tools list.
- Update the main `README.md` documentation table to reference the new tool's documentation.
- Tools should not depend on spec formats or runtime context. They are independent utilities that operate on input files directly.
- Validate file paths and arguments before processing; use typer's built-in validation (exists=True, dir_okay=False, etc.) where appropriate.
- Provide clear error messages that help users understand what went wrong and how to fix it.

## Validation and testing

Use the smallest command that covers the change:

```sh
uv sync
uv run mu --help
```

There is currently no committed test suite or configured linter. For runtime behavior, prefer focused temporary specs and targeted Python checks rather than inventing a broad default suite. If tests are necessary, add a focused test beside the relevant existing coverage and state exactly what was run.

Do not add tests or run a broad test suite unless the user asks for verification or the change needs protection through a non-obvious regression test.
