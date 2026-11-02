# 01. Overall structure and spec lifecycle

## Root structure and lifecycle

A spec contains `entries`, `workloads`, and singular `pipeline`.
See [entry lifecycle and lazy pipeline prepare](07-master-flow-and-cli.md) for
complete rules and migration from root `prepare` / `master-flow.requests`.

```yaml
entries:
  direct_flow:
    initialization: []
    finalization: []
  master_flow:
    initialization: []
    finalization: []
workloads: {}
pipeline:
  demo:
    context: {}
    prepare: []
    params: {}
    nodes: []
```

Entry initialization runs once, pipeline prepare runs once when its first valid
request arrives, request nodes execute, then entry finalization runs on exit.
Unused pipelines are not prepared. Pipeline prepare has no request params.

## `workloads`

`workloads` is a mapping, not a list:

```yaml
workloads:
  yue2:
    target: ...
  qwen-image:
    target: ...
```

The outer key is the workload's unique name within the spec. An `execute_workload` action references that name in its `workload` field. Its properties are described in [Workloads](03-workloads.md).

## `pipeline`

`pipeline` is also a mapping. A pipeline name is an outer key:

```yaml
pipeline:
  song-with-cover:
    params: {}
    context: {}
    nodes: []
```

A pipeline does not run directly. The CLI accepts a request and combines `(pipeline_name, request_params)` into an executable job.

## Runtime pipeline structure

For one request:

```text
prepared entry context
  + pipeline.context + pipeline.prepare results
  + parameters from pipeline.params
  -> shared pipeline context

shared pipeline context
  -> create one deep copy per node

node
  + node.context
  + node:{name,status,total_units}
  -> node context

node
  -> workload/action context
```

Every request creates its own pipeline context. Changes made inside a node do not affect the pipeline context of another request.

## Node execution order

When dependencies have succeeded:

```text
1. Create node context by deep-copying and merging node.context
2. Set node status to running
3. Check skip_if
4. If skip_if matches:
   - check postcondition, if present
   - run finally
   - end with status success
5. Check precondition, if present
6. Run each action in before_run
7. Run the actions in run sequentially
8. Run each action in after_run
9. Check postcondition, if present
10. Run each action in finally
```

If any required step raises an error:

```text
status = failed
immediately jump to finally
```

`finally` runs after success, a skip, or failure.

## Example with two prepare actions and a node

```yaml
entries:
  direct_flow:
    initialization:
      - type: create_dir
        path: "${{ session.working_dir }}/cache/models"

    finalization: []
workloads:
  copy:
    params:
      source: {type: string}
      destination: {type: string}
    target: cp
    args: ["${{ params.source }}", "${{ params.destination }}"]

pipeline:
  demo:
    params:
      source: {type: string}
      destination: {type: string}
    nodes:
      - name: copy-file
        run:
          - type: execute_workload
            workload: copy
            args:
              source: "${{ params.source }}"
              destination: "${{ params.destination }}"
        finally:
          - type: execute_command
            target: python
            args: [-c, "print('done')"]
```

The example contains a syntax error: the final workload `args` entry must close both quoted strings and the array. The corrected form is:

```yaml
args:
  - "${{ params.source }}"
  - "${{ params.destination }}"
```

## How spec-level validation differs from a node postcondition

- `prepare` has no `validate` schema in the current runtime.
- `prepare` uses `skip_if` and `postcondition` in the same way as actions.
- Nodes use `skip_if`, `precondition`, `postcondition`, and `finally`.
- `depends_on` lists prerequisite node names in the same request; all must report success.

The repository can contain two formats in different code paths (the older processor format and the newer workload format). The primary format documented here is the `workloads` + `pipeline` + `nodes` structure in `runtime.py`.
