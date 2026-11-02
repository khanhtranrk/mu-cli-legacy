# Quick Reference

## Root keys

| Key | Type | Description |
|---|---|---|
| `expression_engine` | `cel-v1` | Only supported engine; also the default when omitted |
| `entries` | mapping | `direct_flow` / `master_flow` lifecycle actions |
| `pipeline.<name>.prepare` | action list | Lazy setup once per used pipeline |
| `workloads` | mapping | Subprocess definitions |
| `pipeline` | mapping | Pipeline definitions |

## Workload keys

| Key | Type | Default | Description |
|---|---|---:|---|
| `params` | mapping | `{}` | Parameter schema |
| `env` | mapping | `{}` | Additional environment variables |
| `target` | string | actually required | Executable |
| `args` | list | `[]` | Argument vector |
| `device_args` | list | `[]` | Additional arguments when a GPU is selected |
| `units` | number | `0` | Scheduling units requested |

## Shared action keys

| Key | Type | Default | Description |
|---|---|---:|---|
| `type` | string | required | Action type |
| `context` | mapping | `{}` | Additional context |
| `skip_if` | condition | absent | Skip the action when true |
| `precondition` | condition | absent | Run only when true |
| `postcondition` | condition | absent | Check after execution |
| `ignore_error` | boolean | `false` | Ignore action errors |

## Action types

| Type | Type-specific fields | Result |
|---|---|---|
| `execute_command` | `target`, `args`, `env`, `save_state` | Run one process |
| `send_http_request` | `url`, `method`, `header`, `body`/`body_file`, `response_file`, `timeout`, `save_state` | Make an HTTP request and optionally save the response |
| `write_json` | `path`, `content` | Write JSON |
| `create_dir` | `path` | Create a directory and its parents |

Action names use snake_case and are declared with `type`. No action aliases are accepted.

## Pipeline keys

| Key | Type | Description |
|---|---|---|
| `params` | mapping | Request schema |
| `context` | mapping | Pipeline context |
| `nodes` | list | Nodes in declaration order |

## Node keys

| Key | Type | Description |
|---|---|---|
| `name` | string | Unique identifier |
| `context` | mapping | Node context |
| `ignore_error` | boolean | Default false; report node failure as scheduler success after finally |
| `skip_if` | condition | Skip the node when true |
| `precondition` | condition | Required precondition |
| `before_run` | action list | Run before the run actions |
| `run` | action list | Sequential actions, including `execute_workload` |
| `after_run` | action list | Run after the run actions |
| `postcondition` | condition | Check after the main section |
| `finally` | action list | Always run last |
| `depends_on` | list | List of prerequisite node names; all must succeed |

## Condition operators

Operator names use snake_case without aliases.

| Operator | Fields | Default `expected` | True when |
|---|---|---:|---|
| `cel` | `expression`, optional boolean `expected` | true | Whole-value CEL returns the expected boolean |
| `and` | `props` | true | All props are true |
| `or` | `props` | true | At least one prop is true |
| `file_exists` | `path` | true | The path is an existing file |
| `folder_exists` | `path` | true | The path is an existing directory |
| `path_exists` | `path` | true | The path exists |
| `action_state` | `target`, `expected` | required | Compare a saved action value at the target key path |
| `exit_code` | `target`, `expected` | 0 | The command exits with `expected` |
| `node_state` | `target`, `expected` | required | Compare a field of the current node context |
| `node_status` | `node`, `expected` | `success` | The selected scheduler node has the expected status |

## Context keys

| Key | Example | Layer |
|---|---|---|
| `sys.cache_dir` | `${{ sys.cache_dir }}` | Base |
| `sys.models` | `${{ sys.models["model.gguf"] }}` | Base |
| `session.working_dir` | `${{ session.working_dir }}` | Base |
| `session.unit_name` | `${{ session.unit_name }}` | Base |
| `session.output_dir` | `${{ session.output_dir }}` | Base |
| `session.cache_dir` | `${{ session.cache_dir }}` | Base |
| `params` | `${{ params.name }}` | Pipeline/workload |
| `node.name/status/total_units` | `${{ node.status }}` | Node |
| `node.device.family/index` | `${{ node.device.family }}` | Node; CPU uses `cpu` / `null` |
| `schedule.device_family` | `${{ schedule.device_family }}` | GPU node |
| `schedule.device_index` | `${{ schedule.device_index }}` | GPU node |

## Node status

| Status | Meaning |
|---|---|
| `running` | Currently running |
| `success` | Completed successfully or skipped successfully |
| `failed` | Error or failed precondition/postcondition |

## Parameter schema

```yaml
params:
  required:
    type: string
  number:
    type: number
  optional:
    type: string
    default: value
```

- Missing required params cause an error.
- Extra keys cause an error.
- `number` does not accept booleans.
- `string` does not coerce numbers to strings.

## Units and VRAM

| Device VRAM | Units |
|---|---:|
| 0–4 GiB | 1 |
| 5–8 GiB | 2 |
| 9–12 GiB | 3 |
| 13–16 GiB | 4 |
| 17–20 GiB | 5 |

`units: 0` means CPU and does not add `device_args`.

## CLI

```bash
# Run locally
uv run mu run --spec ./spec.yaml --pipeline pipeline,request.yaml

# Configure a GPU manually
uv run mu run --pipeline pipeline,request.yaml --device cuda,0,4

# Run a remote master flow
uv run mu master-flow run song-generation

# Help
uv run mu --help
uv run mu run --help
uv run mu master-flow run --help
```

## Pre-run checklist

- The root spec is a mapping.
- Every workload key has a target; args is a list.
- Workload params match `execute_workload.args`.
- Pipeline params match the request.
- Node names are unique within the pipeline.
- Dependency nodes exist within the same pipeline.
- Paths/output use `session` instead of a hard-coded working directory.
- `postcondition` checks the correct artifact.
- `finally` is used for cleanup/failure notification and has `ignore_error` if notification must not make the pipeline fail.
- `units: 0` for workloads that do not use a GPU.
- Do not commit an API token in `header`.

CEL renders once. Request inputs are literal JSON; missing defaults execute in the caller context. Captured Registry values are under `CreationSpace.Registry`, `Session.Registry`, `Request.Registry`. See [CEL expressions](11-cel.md).
