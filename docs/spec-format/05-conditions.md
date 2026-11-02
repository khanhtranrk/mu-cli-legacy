# 05. Conditions

Actions and nodes use the same condition system. A condition is a mapping with an `operator`:

```yaml
operator: file_exists
path: "${{ params.output }}"
expected: true
```

## General rules

- `operator` is required and must be one of the supported operators.
- `expected` is the expected result.
- The default `expected` value depends on the operator.
- A missing condition (`null` or not declared) is treated as true.
- A failed condition in `precondition` means “do not run,” not “failure.”
- A failed condition in `postcondition` causes the action/node to fail.
- A failed condition in `skip_if` means normal execution continues.

## `and`

AND all of the child properties:

```yaml
operator: and
props:
  - operator: file_exists
    path: /tmp/result.mp3
  - operator: file_exists
    path: /tmp/result.abc
expected: true
```

`props` must be a list. An empty list evaluates to `true`, matching Python's `all([])` behavior.

If no nested condition is false, an empty list still evaluates to `and=true`.

## `or`

OR at least one child property:

```yaml
operator: or
props:
  - operator: file_exists
    path: /tmp/cache-a.bin
  - operator: file_exists
    path: /tmp/cache-b.bin
```

An empty list evaluates to `false`, matching Python's `any([])` behavior.

## `file_exists`

Returns true only if the path exists and is a file:

```yaml
operator: file_exists
path: "${{ session.output_dir }}/${{ params.name }}/cover.png"
expected: true
```

- Do not use this to check for a directory.
- A symlink to a regular file is usually treated as a file by `is_file()`.
- The default is `expected: true`.

## `folder_exists`

Returns true only if the path exists and is a directory:

```yaml
operator: folder_exists
path: "${{ session.output_dir }}/${{ params.name }}"
```

- A symlink to a directory is usually treated as a directory.
- The default is `expected: true`.

## `path_exists`

Returns true for any existing object: a file, folder, or valid symlink:

```yaml
operator: path_exists
path: "${{ params.output }}"
```

Use `file_exists` or `folder_exists` when the object type must be constrained.

## `exit_code`

Runs a command and compares its exit code:

```yaml
operator: exit_code
target: ["${{ sys.cache_dir }}/bin/yue-synth", --help]
expected: 0
```

- `target` must be a non-empty argument-vector list.
- `expected` must be an integer; booleans are not accepted.
- The default is `expected: 0`.
- A missing executable or an OS error makes the condition false.
- The command runs with `cwd = session.working_dir`.

Example that expects exit code `1` to identify a legacy command:

```yaml
operator: exit_code
target: [demucs, --help]
expected: 1
```

Do not use `exit_code` for expensive work. It is only a probe that re-executes the executable.

## `node_status`

Checks a node's state within the same pipeline request:

```yaml
operator: node_status
node: prepare
expected: success
```

`node` selects a name in the scheduler state table for the same request.
`depends_on` no longer accepts conditions; it is a list of names requiring
success automatically. For checks inside a node, use `node_state`:

```yaml
finally:
  - type: send_http_request
    precondition:
      operator: node_state
      target: [status]
      expected: failed
```

Valid states:

- `success`: the node ran successfully or was skipped successfully;
- `failed`: the node failed;
- An unknown node state evaluates to false.

If `expected: succeeded`, the runtime normalizes the alias to `success`, but new documentation and specs should use `success`.

## `expected: false`

A condition can be inverted:

```yaml
operator: file_exists
path: /tmp/must-not-exist
expected: false
```

This is useful with `skip_if`: skip an action when its output does not exist, although the likely mistake is:

```yaml
skip_if:
  operator: file_exists
  path: output.png
expected: true
```

## Relative paths

Mu resolves relative paths against `session.working_dir`:

```yaml
operator: file_exists
path: output/demo.wav
```

That is equivalent to:

```yaml
path: "${{ session.working_dir }}/output/demo.wav"
```

Use an absolute template when a spec may run in a different environment or be started from an unexpected directory.

## Conditions on actions

```yaml
- type: execute_command
  target: python
  args: [convert.py]
  skip_if:
    operator: file_exists
    path: "${{ params.output }}"
  precondition:
    operator: folder_exists
    path: "${{ params.input_dir }}"
  postcondition:
    operator: file_exists
    path: "${{ params.output }}"
```

The sequence is:

1. `skip_if` is true: the action does not run.
2. `precondition` is false: the action does not run.
3. The action runs.
4. `postcondition` is false: the action fails.

## Conditions on nodes

```yaml
- name: generate
  skip_if:
    operator: file_exists
    path: "${{ params.output }}"
  precondition:
    operator: exit_code
    target: [generator, --help]
  postcondition:
    operator: file_exists
    path: "${{ params.output }}"
```

For details, see [Pipelines and nodes](06-pipelines-and-nodes.md).

## `action_state`

Reads saved action results from the containing context's `actions` mapping.
`target` is a non-empty list of non-empty string keys. The first key is the
`save_state` name; subsequent keys traverse nested mappings. List indexing
is not supported. `expected` is required, including when the expected value
is `null` or `false`. Comparison uses the same equality semantics as other
conditions, without string/number coercion.

```yaml
precondition:
  operator: action_state
  target: [check_command, returncode]
  expected: 1
```

This reads `context["actions"]["check_command"]["returncode"]`. HTTP examples
include `[create_job, status_code]` and `[create_job, body, id]`. Missing state,
missing keys, or encountering a non-mapping before the end returns false,
even with `expected: null`. An explicitly stored null does match null.
Missing `expected` or an invalid `target` is a declaration error.

An action's own result is available in its postcondition. Later actions in
the same node and `finally` can read it. A node precondition only sees state
already inherited into that node; it cannot see results of its own future
actions or another node's actions. The stored process field is `returncode`.

Operator names use snake_case only: `and`, `or`, `file_exists`,
`folder_exists`, `path_exists`, `exit_code`, `node_status`, `node_state`, `action_state`.
Hyphenated operator names are rejected.

## `node_state`

Reads the current node's `context["node"]` directly. Like `action_state`,
`target` must be a non-empty list of non-empty string keys and `expected`
is required. The path starts inside the node mapping, without a node name:

```yaml
precondition:
  operator: node_state
  target: [status]
  expected: failed
```

Available runtime fields are `name`, `status`, `total_units`, and `device`.
Use `target: [device, family]` or `[device, index]` to inspect the assigned
placement; CPU placement has family `cpu` and index `null`.
Status is `running` during the main node lifecycle, including node
preconditions, before/after actions and node postconditions. In `finally`,
it is `success` or `failed`, updated before each action. A non-ignored
finally error changes status to `failed` for subsequent finally actions.
A successful skip enters finally as `success`, unless its postcondition fails.
`total_units` is the maximum workload units across `before_run`, `run`,
and `after_run`, available before any node context or action is evaluated.

Missing node context or a missing key returns false, including when
`expected: null`. Malformed target or absent expected raises a declaration
error. Nested mappings are supported; list indexing is not. Expected values
use normal condition equality without string/number coercion.

Use `node_state` inside nodes. Dependencies require success automatically and
do not evaluate condition operators.
No new node state is stored by this operator, and it cannot select another
node. See [the executable example](../../examples/node-state/spec.yaml).

## CEL

```yaml
operator: cel
expression: '${{ params.width > 0 && params.width <= 4096 }}'
expected: true
```

`expression` must be a whole-value CEL template returning a boolean. `expected`
is an optional boolean, defaulting to true. Missing references and type mismatches
are errors. The same [bounded CEL profile](11-cel.md) applies to other Spec fields.
