# 02. Context, templates, and parameters

## What is context?

Context is JSON data exposed to the CEL interpreter through `${{ expression }}`. It is built in stages:

```text
sys
session
params
node
schedule
actions (when save_state is used)
```

In a template, the outermost key is the context root. For example, `${{ params.name }}` reads `context["params"]["name"]`.

```yaml
args:
  - "${{ params.name }}"
  - "${{ session.output_dir }}/${{ params.name }}/cover.png"
```

## Base context

Mu creates this context before running `prepare`:

```yaml
sys:
  cache_dir: /system/cache/path
  models:
    model-file.gguf: /actual/path/to/file.gguf
session:
  working_dir: /working/path
  unit_name: unknown
  output_dir: /working/path/output
  cache_dir: /working/path/cache
```

`output_dir` and `session.cache_dir` are created automatically in `_runtime_base`.

### Master-flow connection in `session`

`mu master-flow run` adds `session.server`, `session.token`, `session.unit_id`
and `session.unit_type` before entry initialization. Server and token are the
resolved CLI/environment values (`--server` / `--token` take precedence).
`session.server` has trailing slashes removed so API paths can be appended.
These values are inherited by entry actions, pipeline prepare and node contexts;
they are not added by local `mu run`.

```yaml
- type: send_http_request
  url: "${{ session.server }}/api/notifications/send"
  method: post
  header:
    Authorization: "Bearer ${{ session.token }}"
    Content-Type: application/json
  body:
    title: Worker ready
    message: Initialization completed
```

The token is a secret; use it in authentication headers and avoid writing or
printing it in action bodies, command arguments, logs or output files.
See the [executable example](../../examples/master-flow-session/spec.yaml).

### `sys.cache_dir`

This is `--sys_cache_dir`, defaulting to `$HOME/.cache`.

### `sys.models`

Mu scans `.gguf`, `.safetensor`, and `.safetensors` files under `sys.cache_dir/models`. Keys can be:

- a relative path such as `vae/qwen_image_2.1_vae_bf16`;
- a unique stem such as `YuE2-3B-BF16`;
- the full filename if the stem is duplicated.

When several files have the same stem or name, aliases may not be created consistently. Use the relative path or full key to avoid ambiguity.

## Pipeline context

Each pipeline can add context:

```yaml
pipeline:
  demo:
    context:
      media:
        image: png
      quality: high
```

These values are rendered and merged separately for each Request after shared prepare. A template can reference context that already exists:

```yaml
context:
  work_name: "render/${{ params.name }}" # params does not exist at this stage!
```

Note: pipeline `context` is rendered **before** pipeline parameters. Referencing `params` at this stage is an error; use node context for parameter-dependent values. The clear approach is to place parameter-dependent values in node context or keep the template in arguments or an action.

## Parameter schema

Pipeline and workload schemas are declared under `params`. Every key is a parameter name; its declaration can include `type`, `default`, and an optional description:

```yaml
params:
  name:
    type: string
  width:
    type: number
    default: 1024
  optional_description:
    type: string
    default: untitled
```

Rules:

- A missing key without a `default` is an error.
- A key with a `default` uses that default.
- `type: string` requires a string value.
- `type: number` requires an `int` or `float` and rejects booleans.
- `type: boolean`, `array`, and `object` validate their corresponding types (shallow validation).
- Parameters outside the schema are rejected.
- After schema processing, `params` becomes a mapping in the context.

YAML `1` is a number, while `"1"` is a string.

### Pipeline parameters

Request data is a mapping:

```yaml
name: demo
width: 1920
```

Or a list of mappings in a request file:

```yaml
- name: demo-a
- name: demo-b
```

If a mapping lacks a required parameter, Mu reports an error before running nodes.

### Workload parameters

Nodes supply arguments:

```yaml
run:
  - type: execute_workload
    workload: resize
    args:
      input: "${{ params.input }}"
      output: "${{ params.name }}.png"
```

The workload defines the schema for these arguments:

```yaml
workloads:
  resize:
    params:
      input: {type: string}
      output: {type: string}
```

The caller renders supplied arguments once. Mu validates them to construct workload `params`, evaluates only missing defaults in the caller context, then renders the workload target and argv. Missing or extra keys cause a workload error.

## Node context

A node can add context:

```yaml
- name: render
  context:
    stage: main
    status_label: "rendering ${{ params.name }}"
```

Mu renders node context using the current pipeline context and merges it. The resulting context also contains:

```yaml
node:
  name: render
  status: running
  total_units: 3
  device:
    family: cuda
    index: "0"
```

`node.status` is `running` throughout the main lifecycle and updated to
`success` or `failed` before each finally action. `node.total_units` is the maximum workload units across `before_run`, `run`,
and `after_run`, available from the start of node execution.
Use [node_state](05-conditions.md#node_state) with `target: [status]`, `[name]`,
or `[total_units]` and a required `expected` value to inspect these fields.

`node.device` describes the scheduler-assigned workload device from the start
of node execution, including context rendering, conditions, actions and `finally`.
`family` is `cuda` for NVIDIA, `rocm` for AMD, or the backend name for other
backends. GPU `index` is a string. CPU-only nodes and CPU fallback use
`{family: cpu, index: null}`. Skipped nodes retain their assigned placement;
this does not prove that a workload ran or that an action uses that GPU.
Read `${{ node.device.family }}` / `${{ node.device.index }}`, or use `node_state`
with `target: [device, family]` / `[device, index]` and an explicit `expected`.

## Action context

An action can merge its own context:

```yaml
- type: write_json
  context:
    payload:
      name: "${{ params.name }}"
  path: "${{ session.cache_dir }}/request.json"
  content: "${{ payload }}"
```

Action context is rendered and merged directly into the containing context
before skip/precondition checks. There is no isolated action context copy;
later actions in the same node see these additions. Merge replaces top-level
keys rather than recursively merging mappings. A whole-value placeholder preserves the referenced type, including a copied
mapping or list; composed strings convert substitutions to text. For example,
`content: "${{ payload }}"` writes a JSON object when `payload` is a mapping.

`save_state: name` on a supported action writes `actions[name]` into this same
containing context. In a node, later actions, workload rendering and finally
can read it; other nodes and requests cannot read that node's changes.
Entry actions share the flow context. Each pipeline prepares an isolated deep
copy of the entry context. Request-specific pipeline context is applied afterward. Saved prepare results are inherited by deep copy
into requests; each request adds its validated `params`. Remote requests also
receive `request: {requestId, specSlug}`. Nodes receive another deep copy.
See [lifecycle context scopes](07-master-flow-and-cli.md).

## Schedule context

This exists only when a workload has `units > 0` and the scheduler assigns a GPU:

```yaml
schedule:
  device_family: cuda | rocm
  device_index: "0"
```

For example:

```yaml
device_args:
  - --device
  - "${{ schedule.device_family }}:${{ schedule.device_index }}"
```

If a workload has `units: 0`, no GPU is assigned, no `schedule` is passed, and `device_args` are not appended.

## Rendering rules

CEL renders each template once. Whole-value expressions preserve JSON types;
scalars in mixed strings become text (`true`, `false`, `null` use CEL spelling).
Maps/lists require a whole-value expression. Missing keys and type mismatches fail.
Rendered strings are data and never interpreted again. Use `$${{ expression }}`
to emit literal `${{ expression }}`. Single braces are ordinary literal text.

```yaml
args:
  - '${{ params.name }}.wav'
content: '${{ payload }}' # whole object
```

Values in mappings and lists are rendered recursively; structural map keys remain
literal. Only `write_json.content` and `send_http_request.body` render payload keys
and reject duplicate or empty keys. Supplied Request parameters are literal JSON;
only missing defaults execute expressions. Subprocess argv elements become strings
when commands are built, after parameter type validation.
See [the CEL profile](11-cel.md) for supported functions, bounds and conditions.

## Full context example

```text
sys:
  cache_dir: "/home/u/.cache"
  models:
    demo-model: "/home/u/.cache/models/demo.gguf"
session:
  working_dir: "/work"
  unit_name: "worker-1"
  output_dir: "/work/output"
  cache_dir: "/work/cache"
params:
  name: "demo"
  quality: 4
node:
  name: upscale
  status: running
  total_units: 2
schedule:
  device_family: cuda
  device_index: "1"
```

Routine calls create an isolated context with their own params and actions; see [routines](09-routines.md). Payload mapping keys in HTTP body and write_json content are rendered recursively with collision checks.

## MasterFlow Registry inputs

MasterFlow resolves submitted Registry expressions before claim and freezes them
in a Request snapshot. Workers receive the resolved payload and captured value
maps exposed as `CreationSpace.Registry`, `Session.Registry`, `Request.Registry`.
These roots cannot be overwritten by user context. Local runs use empty maps.
Lowercase `session` is worker runtime context, independent of a Creation Session.

A Request width `${{ Request.Registry.Width }}` can arrive as number `1024`.
A Spec default can read that captured value directly; locally supplied unresolved
expressions remain strings and fail numeric parameter validation.
See [CEL execution](11-cel.md) and the canonical
[Registry guide](https://github.com/khanhtranrk/mu-masterflow/blob/master/docs/reference/registry-cel.md).
