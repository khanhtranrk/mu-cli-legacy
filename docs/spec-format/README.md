# Mu Spec Creation Guide

This directory explains the YAML/JSON structure understood by Mu, using the spec in [`examples/song-with-cover`](../../examples/song-with-cover/spec.yaml). It is designed so you can read an existing spec and create a new one without having to guess the meaning of every field.

> This documentation describes the **format that the runtime currently executes**. Verify field names and semantics against `src/mu/runtime.py`, `src/mu/expressions.py` and their tests when documentation disagrees. This guide describes Python behavior, not automatic TypeScript feature parity.

## Read in this order

1. [01 – Overall structure and spec lifecycle](01-overview.md)
2. [02 – Context, templates, and parameters](02-context-and-templates.md)
3. [03 – Workloads](03-workloads.md)
4. [04 – Actions](04-actions.md)
5. [05 – Conditions](05-conditions.md)
6. [06 – Pipelines and nodes](06-pipelines-and-nodes.md)
7. [07 – Master-flow, CLI, and scheduling](07-master-flow-and-cli.md)
8. [08 – Complete examples, from small to large](08-complete-example.md)
9. [Routines and file inspection](09-routines.md)
10. [Lifecycle events](10-events.md)
11. [CEL Spec expressions](11-cel.md)
12. [Quick-reference table](quick-reference.md)

## A complete spec at a glance

```yaml
expression_engine: cel-v1
entries:
  direct_flow:
    initialization:
      - type: execute_command
        target: python
        args: [--version]

    finalization: []
workloads:
  make-copy:
    params:
      source:
        type: string
      destination:
        type: string
    target: cp
    args:
      - "${{ params.source }}"
      - "${{ params.destination }}"
    units: 0

pipeline:
  copy-file:
    params:
      source:
        type: string
      destination:
        type: string
    nodes:
      - name: copy
        before_run:
          - type: create_dir
            path: "${{ session.working_dir }}/output"
        run:
          - type: execute_workload
            workload: make-copy
            args:
              source: "${{ params.source }}"
              destination: "${{ params.destination }}"
        postcondition:
          operator: file_exists
          path: "${{ params.destination }}"
```

The data tree above means:

- `entries`: initialization and finalization for each command entry.
- `pipeline.<name>.prepare`: runs once when that pipeline receives valid work.
- `workloads`: reusable subprocess templates.
- `pipeline`: workflows made up of tasks.
- `pipeline.<name>.nodes`: the steps in a workflow.
- `node.run`: a sequential list of actions, including `execute_workload`.
- `node.before_run`, `after_run`, and `finally`: separate action lists around `run`.
- `skip_if`, `precondition`, and `postcondition`: conditions that control nodes and actions.
- `depends_on`: dependencies and ordering between nodes.

## Important rules

### 1. A spec is data, not a shell script

The `target` and `args` properties create a subprocess argv. Mu does not join command components into a string and then execute the result through a shell. Arguments must be declared as a list. This avoids errors caused by embedded spaces and shell characters.

### 2. JSON is the equivalent format

The spec root is a mapping/object. YAML can be read as JSON when `safe_load` produces the corresponding string, number, boolean, list, and mapping values. The keys shown below are valid in JSON as well.

### 3. Templates use CEL

For example:

```yaml
args:
  - "${{ params.input }}"
  - "${{ session.output_dir }}"
```

Mu evaluates expressions in the current JSON context using CEL. A string can contain multiple placeholders, but a placeholder that constitutes the entire string can return a number or boolean during rendering; the system does not convert it to a string first. See [Context and templates](02-context-and-templates.md).

### 4. Missing parameters are configuration errors

The workload or pipeline schema identifies required parameters, optional parameters, and default values. Unexpected values not present in the schema are rejected.

### 5. Nodes participate in scheduling

Mu schedules all nodes from every supplied request. Declare `depends_on: [node_a, node_b]` to wait for successful prerequisites. A failed prerequisite blocks execution, including finally. Ignored node failures report success and allow dependent nodes to proceed. `units: 0` means CPU-only and consumes no GPU units.

### 6. Canonical status values

A runtime node uses only three execution states: `running`, `success`, and `failed`. Dependencies automatically require scheduler success. Inside a node, use `node_state` with `target: [status]`: the value is `running` during the main lifecycle, then `success` or `failed` in `finally`. See [Conditions](05-conditions.md#node_state). The runtime accepts the alias `succeeded` and converts it to `success`, but the canonical name `success` should be used.

## The two CLI forms

- `mu run --pipeline PIPELINE,REQUEST.yaml ...`: runs a local spec and local requests.
- `mu master-flow run SPEC_SLUG`: registers a unit, retrieves the spec, and claims request batches from MasterFlow.

See [Master-flow and CLI](07-master-flow-and-cli.md) for details.
