# 06. Pipelines and nodes

## Pipeline

```yaml
pipeline:
  transcribe:
    params:
      input: {type: string}
      output: {type: string}
    context:
      work: "${{ session.cache_dir }}/transcribe"
    nodes:
      - name: prepare
        run:
          - type: execute_workload
            workload: mkdir
            args:
              path: "${{ params.output }}"
```

### `prepare`

Optional action list, executed once on the first valid request for this pipeline
within a flow. No request params are available. See [lifecycle](07-master-flow-and-cli.md).

### `params`

The schema for pipeline requests. It uses the same `type` and `default` fields as workload params.

### `context`

A context mapping that is rendered and merged before `params`. Because params are not constructed at this stage, pipeline context must not depend on `params[...]`.

### `nodes`

A list of nodes. Every node in a pipeline request must have a unique, non-empty `name`.

## Node

```yaml
- name: generate-song
  context:
    label: "${{ params.name }}"
  ignore_error: false
  skip_if: {...}
  precondition: {...}
  before_run: [...]
  run:
    - type: execute_workload
      workload: yue2
      args: {...}
  after_run: [...]
  postcondition: {...}
  finally: [...]
  depends_on: [...]
```

### `name`

A unique identifier within the pipeline:

```yaml
- name: prepare
- name: generate
```

The name is used in:

- logs;
- `depends_on`;
- `node_status`;
- the request state map.

### `context`

Merged into the node's private context:

```yaml
context:
  stage: generation
```

A node receives `deepcopy(context)` from the pipeline, so action/node changes do not propagate to other nodes.

### `skip_if`

If the condition is true:

1. The node does not run precondition/before/run/after/postcondition.
2. If a postcondition is present, it is still checked.
3. The node changes state to `success`.
4. Finally actions run.

This mechanism skips work whose result already exists. If the postcondition fails after skipping, the node fails.

### `precondition`

If the condition is false:

- The node does not run before or after actions or the workload.
- The node changes state to `failed`.
- Finally actions run.

This is an important distinction from `skip_if`: a failed precondition is a node error.

### `before_run`

A sequential list of actions that runs before `run`:

```yaml
before_run:
  - type: create_dir
    path: "${{ params.output_dir }}"
  - type: write_json
    path: "${{ params.request }}"
    content:
      prompt: "${{ params.prompt }}"
```

If an action fails and its error is not ignored:

- the workload does not run;
- the node fails;
- finally actions run.

### `run`

A sequential action list, using `execute_workload` to invoke a workload:

```yaml
run:
  - type: execute_workload
    workload: transcribe
    args:
      input: "${{ params.input }}"
      output: "${{ params.output }}"
```

Rules:

- An `execute_workload` action uses `workload` to name a declaration in `workloads`.
- Its `args` is a mapping that supplies workload params.
- Args are rendered using the node context.
- Omitting `run` or using `[]` is allowed.
- All three phases accept ordinary actions and `execute_workload`.
- The old `run: {workload, args}` mapping is rejected; migrate it to a list with `type: execute_workload`.

### `after_run`

A sequential list of actions that runs after every `run` action succeeds:

```yaml
after_run:
  - type: send_http_request
    url: "https://example.invalid/events"
    method: post
    body:
      status: completed
```

If the workload fails, `after_run` does not execute. Use `finally` for cleanup or failure notifications.

### `postcondition`

The condition must be true after the main section runs:

```yaml
postcondition:
  operator: and
  props:
    - operator: file_exists
      path: output.mp4
    - operator: file_exists
      path: metadata.json
```

If `postcondition` is omitted, successful completion requires:

- a workload exit code of 0, if a workload is present;
- success of every required action;
- no other errors.

### `finally`

Actions that must run last:

```yaml
finally:
  - type: send_http_request
    precondition:
      operator: node_state
      target: [status]
      expected: failed
    ignore_error: true
    url: https://example.invalid/status
    method: post
    body:
      status: failed
```

`finally` runs after:

- success;
- skipping;
- failure;
- a failed precondition;
- an action failure;
- a workload failure;
- a failed postcondition.

Mu updates `node.status` before each finally action. Use `node_state` with `target: [status]` to read it. An unignored finally error changes the node to `failed`; subsequent finally actions see that state and are still considered for execution. Ignored errors do not change node status. A node blocked by failed dependencies never enters execution and does not run finally.

### `depends_on`

A list of prerequisite node names in the same pipeline request:

```yaml
depends_on: [prepare, validate]
```

The node waits for all prerequisites to complete, including finally, and
runs only if every scheduler status is `success`. A failed prerequisite
blocks execution; the blocked node's actions and finally do not run.
Omitting `depends_on` or using `[]` means no prerequisites.
Mapping entries, dependency conditions, and names from another pipeline or
request are not supported. Unknown names and malformed lists are errors.
A prerequisite with `ignore_error: true` reports success even after an error,
so its dependents may run normally.

## Node scheduler

The scheduler scans every pending node in declaration order and:

- skips a node whose dependencies have not finished;
- marks the node failed if a dependency fails;
- skips a node if enough GPU units are unavailable;
- runs at most 32 nodes concurrently;
- releases GPU units when a node finishes.

Mu does not rely on `wait_timeout` or YAML order to run nodes concurrently. Dependencies provide guaranteed ordering.

## Dependencies and error propagation

```text
prepare success
  -> generate success
  -> transcribe success

prepare failed
  -> generate BLOCKED/failed
  -> transcribe BLOCKED/failed
```

Every dependency requires success. A failed prerequisite blocks its dependents,
which are marked failed as well, unless their own `ignore_error` converts that
outcome to success. Blocked nodes do not execute actions or finally.

## Complete pipeline example

```yaml
pipeline:
  image-job:
    params:
      name: {type: string}
      prompt: {type: string}
      width: {type: number, default: 1024}
      height: {type: number, default: 1024}
    context:
      work: "${{ session.output_dir }}/${{ params.name }}"
    nodes:
      - name: output-dir
        run:
          - type: execute_workload
            workload: mkdir
            args:
              path: "${{ session.output_dir }}/${{ params.name }}"

      - name: render
        depends_on:
          - output-dir
        skip_if:
          operator: file_exists
          path: "${{ session.output_dir }}/${{ params.name }}/cover.png"
        before_run:
          - type: write_json
            path: "${{ session.cache_dir }}/${{ params.name }}.json"
            content:
              prompt: "${{ params.prompt }}"
        run:
          - type: execute_workload
            workload: render-image
            args:
              request: "${{ session.cache_dir }}/${{ params.name }}.json"
              output: "${{ session.output_dir }}/${{ params.name }}/cover.png"
              width: "${{ params.width }}"
              height: "${{ params.height }}"
        postcondition:
          operator: file_exists
          path: "${{ session.output_dir }}/${{ params.name }}/cover.png"
        finally:
          - type: send_http_request
            ignore_error: true
            url: "https://example.invalid/status"
            method: post
            header: {}
            body:
              status: "${{ node.status }}"
```

## Node `ignore_error`

Optional boolean, default `false`. With `ignore_error: true`, a failed node
is reported to the scheduler as `success`, allowing downstream dependencies
to pass. A warning retains the error
detail. This covers precondition, action, workload, postcondition and finally
failures, as well as a node blocked by a failed dependency.

Execution still stops at the first main-section failure and proceeds to
finally; after_run and remaining main actions are not resumed. Finally sees
the actual `success`/`failed` status through `node_state`. Only after finally
finishes is failure converted to scheduler success. Action `save_state`
results retain their actual outcome. Action-level `ignore_error` instead
suppresses that individual action error and permits subsequent actions.

A blocked node with this flag is marked success without entering execution;
its actions and finally do not run. Invalid flag types and invalid pipeline
declarations remain errors, rather than ignored execution outcomes.

```yaml
- name: optional_step
  ignore_error: true
  before_run:
    - type: execute_command
      target: python
      args: [-c, "raise SystemExit(1)"]
- name: continue_pipeline
  depends_on:
    - optional_step
```

Node GPU reservations use the maximum workload `units` across the sequential
action lists `before_run`, `run`, and `after_run`. Mu reserves this capacity
before starting any node actions and releases it after `finally`. Conditional
workload actions are included in this maximum. See [actions](04-actions.md) for `execute_workload`.
