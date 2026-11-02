---
name: create-mu-spec
description: 'Create or repair Mu YAML/JSON specs. Use when a user asks to write, design, extend, or troubleshoot a Mu spec, workload, pipeline, node, action, condition, master-flow, request file, or spec.md/spec.yaml.'
argument-hint: '[describe the desired pipeline or attach an existing spec]'
user-invocable: true
disable-model-invocation: false
---

# Create a Mu Spec

Use this skill whenever the requested output is a Mu spec or a spec fragment. The project guide in [`docs/spec-format`](../../../docs/spec-format/README.md) is the format authority. Read the relevant guide sections before implementing a field that is not already clear.

## Procedure

1. **Clarify only the missing decisions**
   - Identify desired pipeline behavior, inputs, outputs, external executables, side effects, dependencies, and failure handling.
   - Identify whether the spec should be YAML or JSON.
   - Ask only when a missing detail materially changes the spec. Do not interview for values that a safe default can express.
   - If the user says `spec.md`, ask whether they mean a design document or the executable YAML/JSON spec. Usually the deliverable is `spec.yaml`; Markdown may be used for explanatory context only.

2. **Inspect the repository contract**
   - Read [`docs/spec-format/README.md`](../../../docs/spec-format/README.md).
   - Read the relevant detailed sections:
     - [`workloads`](../../../docs/spec-format/03-workloads.md)
     - [`actions`](../../../docs/spec-format/04-actions.md)
     - [`conditions`](../../../docs/spec-format/05-conditions.md)
     - [`pipelines and nodes`](../../../docs/spec-format/06-pipelines-and-nodes.md)
     - [`master-flow and CLI`](../../../docs/spec-format/07-master-flow-and-cli.md)
   - Inspect the existing example [`examples/song-with-cover/spec.yaml`](../../../examples/song-with-cover/spec.yaml) when patterns should match the repository.

3. **Choose the smallest stable design**
   - Separate reusable subprocess declarations into `workloads`.
   - Put request parameters and request-specific values in `pipeline.params` and request files.
   - Put independent executable steps in separate nodes.
   - Put ordering constraints in `depends_on`.
   - Put shared flow setup in `entries.<entry>.initialization`; put lazy pipeline setup in `pipeline.<name>.prepare`.
   - Put setup around a workload in `before_run` and `after_run`.
   - Put cleanup or guaranteed notifications in `finally`.
   - Use `skip_if` for already-complete output; use `postcondition` to prove completion.
   - Use `units: 0` unless a workload genuinely requires GPU scheduling.

4. **Draft the spec**
   - Start from [`assets/minimal-spec.yaml`](./assets/minimal-spec.yaml) when creating a new spec.
   - Keep workload names and node names stable and unique.
   - Declare every pipeline/workload parameter explicitly.
   - Use CEL only: `${{ params.input }}`, `${{ session.output_dir }}`, `${{ sys.cache_dir }}`. Add `expression_engine: cel-v1`; supplied Request values are literal and rendering is once-only. Read [CEL](../../../docs/spec-format/11-cel.md) for typed values and captured Registry scopes.
   - Use argv lists in `target`/`args`; never require an undocumented shell.
   - Use `depends_on: [node_name]`; prerequisites must report success and dependency conditions are not supported.
   - Inside a node, use `operator: node_state`, `target: [status]`, and an explicit `expected` (for example `failed` in `finally`).
   - Prefer `ignore_error: true` only for telemetry or best-effort actions. Do not hide core command or output failures.
   - Avoid committing secrets, absolute machine paths, downloaded assets, or generated media.

5. **Model the execution graph**
   - Give each node a unique name.
   - List every real prerequisite by name in `depends_on`.
   - Ensure `depends_on` references only nodes from the same pipeline request.
   - Verify there is no dependency cycle.
   - Put GPU `device_args` templates behind `units > 0`.
   - Confirm CPU-only workloads do not refer to `schedule[...]`.
   - Use conditions at the right stage:
     - `skip_if`: skip and succeed when satisfied.
     - `precondition`: fail the node when unsatisfied.
     - `postcondition`: prove action/node output after execution.

6. **Validate before returning**
   - Parse the spec as YAML/JSON.
   - Validate required fields, enum-like values, parameter schemas, unique names, and dependency references.
   - Run a safe, small CLI smoke check against a temporary spec/request. Prefer `--help`, a schema-only check, or a harmless command. Never run `mu run` against the media example when it could download models or generate media.
   - If executable behavior is being tested, use a temporary command that has no network or GPU side effects.
   - Review all expected template keys and ensure every key exists by the stage where it is referenced.
   - For JSON output, ensure the structure is genuinely JSON rather than YAML with a `.json` extension.

7. **Deliver the result**
   - Save executable YAML/JSON under the requested path.
   - Include a short request example when one is needed to execute the spec.
   - Summarize the pipeline graph, assumptions, and validation performed.
   - Do not present an untested heavy workload as fully validated.
   - Point to the exact spec fields that control important behavior.

## Default spec skeleton

Use this shape for a new local pipeline:

```yaml
expression_engine: cel-v1
entries: {}

workloads:
  example:
    params:
      input: {type: string}
      output: {type: string}
    target: python
    args: [example.py, "${{ params.input }}", "${{ params.output }}"]
    units: 0

pipeline:
  example-pipeline:
    params:
      input: {type: string}
      output: {type: string}
    nodes:
      - name: execute
        run:
          - type: execute_workload
            workload: example
            args:
              input: "${{ params.input }}"
              output: "${{ params.output }}"
        postcondition:
          operator: file_exists
          path: "${{ params.output }}"
```

Note: quote every template string as needed and ensure a real existing executable replaces `example.py`.

## Repairing an existing spec

When modifying a spec:

1. Preserve unrelated fields and behavior.
2. Compare the old and new YAML semantically, not only textually.
3. Inspect whether a field belongs to the current runtime format or a legacy format.
4. Fix only the requested behavior plus tightly coupled validation or documentation.
5. Keep the same pipeline/request parameter contract unless migration is explicitly requested.
6. Update a neighboring example or README only when the public format changed.
7. Never silently rename a workload, pipeline, node, or parameter used by dependents.

## Output checklist

Before finishing, verify:

- [ ] Spec root is a mapping.
- [ ] `entries`, pipeline-local `prepare`, `workloads`, and `pipeline` use the implemented keys.
- [ ] Every workload has a valid `target` and list `args`.
- [ ] Every workload call supplies exactly the declared params.
- [ ] Every pipeline request supplies exactly the declared params.
- [ ] Every node name is unique within its pipeline.
- [ ] Every dependency exists in the same pipeline/request.
- [ ] `skip_if`, `precondition`, and `postcondition` use a supported `operator`.
- [ ] Relative paths are resolved intentionally.
- [ ] GPU nodes declare units and use device arguments correctly.
- [ ] `finally` is used for guaranteed cleanup/notification.
- [ ] The spec parses and a safe validation succeeds.
- [ ] No secrets or machine-specific absolute paths are embedded.
