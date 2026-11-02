# Routines and file inspection

`routines` is an optional root mapping of reusable sequential action lists.
Unlike a workload, a routine does not launch a scheduled subprocess template.

```yaml
routines:
  inspect_artifact:
    params:
      path: {type: string}
    actions:
      - type: inspect_file
        target: "${{ params.path }}"
        save_state: file_info
    returns: "${{ actions.file_info }}"
```

Call it from any action list (entry initialization/finalization, pipeline prepare,
node before_run/run/after_run/finally):

```yaml
- type: execute_routine
  routine: inspect_artifact
  args:
    path: "${{ params.artifact_path }}"
  save_state: inspected
```

The routine name is literal, not a template. `args` is a mapping, rendered in the
caller before parameter validation. Params use a mapping schema; missing required
and unknown arguments fail. Types include string, number, boolean, array and object;
validation is shallow (array elements and object properties have no nested schema).
Defaults are copied/rendered for each invocation. No implicit type coercion occurs.

Each invocation deep-copies caller context, replaces `params` with routine params,
and starts with an empty `actions` mapping. Session/request/node remain readable;
local context and action results do not leak into the caller. Pass caller action
results explicitly through args. Nested routines work the same way. Calls run in
order and stop at the first unignored failure. Routine cycles are rejected, including
unused cyclic declarations during flow initialization. `execute_workload` is not
allowed inside routines: GPU admission only examines direct scheduled node calls.

`returns` is an optional value/template rendered after successful actions. The call
result saved to `actions.<save_state>` is `{success: true, error: null, output: ...}`;
without returns, output is null. On failure it contains success false and the usual
error object, without output. Nested error labels include routine/action paths.
Common action skip/pre/postconditions and ignore_error still apply. Put ignore_error
on the routine call for best-effort notifications; ignoring errors inside the routine
allows it to continue and can produce a successful call result.

## inspect_file

Requires a non-empty `target` path. Relative paths resolve from the worker working
directory; `~` is expanded and symlinks resolve to their target. Only readable regular
files are accepted. The saved result includes success/error and, on success:

- `path`: resolved absolute path.
- `name`: basename of that resolved path.
- `size`: integer byte count.
- `sha256`: lowercase 64-character digest, calculated in 1 MiB chunks.

Missing files, directories, read errors and detected changes during inspection fail.
Metadata describes the inspected file, not a locked snapshot. Keep it unchanged until
upload completion; MasterFlow verifies its declared digest and size on completion.

## Dynamic payload keys

`send_http_request.body` and `write_json.content` render mapping keys recursively:

```yaml
body:
  id: "${{ request.requestId }}"
  node_states:
    "${{ node.name }}":
      status: "${{ params.status }}"
      logs: "${{ params.logs }}"
```

Keys must resolve to non-empty strings; unresolved key templates and duplicate keys
after rendering fail before writing/sending. Whole-value templates retain lists,
objects and numbers. Structural spec keys and mapping keys in other fields retain
their existing behavior. Mu does not report request/node lifecycle automatically. Configure shared
[lifecycle event hooks](10-events.md) to call reporting routines once per transition.

See the [safe local executable demo](../../examples/routines/spec.yaml). Download
and upload routines are user-defined compositions of HTTP/file actions, not built-ins.
