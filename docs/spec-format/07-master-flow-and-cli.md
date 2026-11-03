# 07. Entries, CLI and master flow

## Entry lifecycle

```yaml
entries:
  direct_flow:
    initialization: []
    finalization: []
  master_flow:
    initialization: []
    finalization: []
```

`mu run` selects `direct_flow`; `mu master-flow run` selects `master_flow`.
Missing entries and action lists are empty. Initialization runs once before
requests are executed (before claim in master flow). Finalization runs once
on completion, failure, or interruption after initialization begins, including
initialization failure. All finalization actions are attempted. Cleanup errors
are reported without replacing the original error. Forced termination cannot
run cleanup. Actions use the existing action fields and `ignore_error` rules.
`entry.name` identifies the entry; `entry.status` is `running`, then `success`
or `failed` during finalization. Entry actions do not receive request params.

Root `prepare` and the old `master-flow.requests` section are rejected with a
migration error. Move common setup to entry initialization and pipeline-specific
setup to `pipeline.<name>.prepare`.

## Lazy pipeline preparation

```yaml
pipeline:
  demo:
    context:
      work: "${{ session.cache_dir }}/demo"
    prepare:
      - type: create_dir
        path: "${{ session.cache_dir }}/demo"
    params:
      name: {type: string}
    nodes: []
```

Prepare runs once per pipeline per invocation, after a valid request selects it
and before its nodes run. It is reused across batches. Unused pipelines are
never prepared. Prepare receives the entry context with empty Registry maps;
it has no Request-specific `params`, `request`, `node` or `schedule`. Its saved
results are deep-copied into each Request, then captured Registry maps,
request metadata, pipeline context and validated params are added. Pipeline
context is request-local and evaluated before params. Model discovery is
refreshed after preparation. A preparation failure is cached for this invocation;
other pipelines in the batch still execute.

## Local requests

```sh
uv run mu run --spec ./spec.yaml --working_dir ./work \
  --pipeline demo,./input.yaml \
  --pipeline demo,./other.json
```

`--pipeline NAME,FILE` is required and repeatable. Files contain a mapping or a
list of mappings. `--request` was removed and reports the replacement syntax.
Other options: `--working_dir` (current directory), `--sys_cache_dir` (`~/.cache`),
`--unit_name` (`unknown`), `--device FAMILY,INDEX,MAX_UNITS` (repeatable; otherwise
hardware detection). Workload arguments remain argv lists, without a shell.

## Master flow

```sh
uv run mu master-flow run song-generation \
  --batch-size 10 --by-pipeline song-with-cover --by-status waiting \
  --unit-id kaggle-01 --refresh-loop
```

| Option | Behavior |
|---|---|
| `--server` | Convex HTTP site URL; overrides `MU_MASTER_FLOW_SERVER` |
| `--token` | Unit bearer token; overrides `MU_MASTER_FLOW_TOKEN` |
| `--batch-size` | 1–100; default 1 |
| `--by-pipeline` | Repeatable; OR within the pipeline filter |
| `--by-status` | Repeatable; OR within the status filter; default `waiting` |
| `--by-unit-id` | Filter previous assignment, before claim |
| `--unit-id` | Receiving unit; defaults to hostname |
| `--unit-type` | Unit type; defaults to `unknown` |
| `--refresh-loop` | Claim subsequent batches until an empty array |

The resolved connection is available in templates as `${{ session.server }}` and
`${{ session.token }}` during initialization, pipeline prepare, nodes and finalization.
The server has no trailing slash; use `Authorization: "Bearer ${{ session.token }}"`
in HTTP action headers. These fields exist only for master flow. See
[connection context and example](02-context-and-templates.md#master-flow-connection-in-session).

Working directory, system cache and device options match local execution.
Filters combine with AND between groups. At most 100 distinct pipeline/status
combinations are supported. The server selects oldest matches atomically, capped
by batch size. API status `pending` is not the standard waiting status.

Execution order:

1. Resolve connection and devices; heartbeat `online` to `/api/units/heartbeat`.
2. Fetch `/api/specs/{specSlug}` once; validate the spec.
3. Run entry initialization.
4. Claim `/api/requests/claim` with filters and `maxCount`.
5. Convert `pipelineName` and mapping `payload` into a pipeline request.
6. Prepare selected pipelines and execute batch nodes through the shared scheduler.
7. Without refresh, exit after one batch. With refresh, claim again after the
   entire batch finishes; an empty array ends the flow. A failed batch stops refresh.
8. Run finalization, then heartbeat `offline`, retaining unit details.

Remote request templates expose `${{ request.requestId }}` and `${{ request.specSlug }}`.
Payload remains exclusively under `params`. Remote payloads must be mappings even
though the server can store arbitrary JSON. Request metadata is never shared.
The transport uses a 30-second timeout and never retries mutations automatically:
a claim may commit before a connection fails. HTTP errors are fatal, not empty
batches. A request returned twice in one invocation stops the flow to prevent
unbounded execution with broad status filters. The worker does not send automatic
request or node status reports; MasterFlow request status is not updated by this
worker after claim.

## Scheduling

Nodes across the batch share the existing scheduler, capped at 32 concurrent
nodes. Dependencies remain local to each request. GPU units are reserved for the
full node lifecycle; `units: 0` is CPU-only. Successful subprocess output is hidden.


HTTP requests identify themselves with `User-Agent: mu/<version>`, including
HTTP actions. An action can override this through its `header` mapping. This
avoids tunnel/proxy rules that reject urllib's default library user agent.
An empty claim prints `No matching requests` and exits successfully; the default
status filter is `waiting`. A successful public health check alone does not
verify token authentication, the spec slug, or the availability of waiting work.

Node GPU reservations use the maximum workload `units` across the sequential
action lists `before_run`, `run`, and `after_run`. Mu reserves this capacity
before starting any node actions and releases it after `finally`. Conditional
workload actions are included in this maximum. See [actions](04-actions.md) for `execute_workload`.


## Request artifacts

MasterFlow derives request/node artifact metadata arrays by joining current
ownership, Artifact and Physical File records; the arrays are not stored. Their `id` and the HTTP `artifactId` are Request-scoped **output
keys**, not the independent canonical Artifact ID. Entries include name, optional
node/label, MIME type, size and creation time; S3 entries include handler,
connection ID and object key. Convex-native legacy file entries are no longer supported.
Claim responses preserve this shape under `request[artifacts]`. Completing an
upload does not mutate the worker's local request context or execution status.

Call `POST /api/requests/artifacts/upload-url` with requestId, a stable output key
as artifactId, byte size and lowercase SHA-256. Optional connectionId selects a
configured storage connection. Use the returned `method` (`PUT`), `headers` and
uploadUrl to send raw bytes with `body_file`, **without a worker token**. Upload
URLs last 15 minutes. Complete with requestId/uploadId, basename name and optional
nodeName/label; do not submit a Convex storageId. The server verifies staged bytes
and finalizes a separate storage object before registering metadata. Retry
completion with identical details if its response is lost.

Worker API calls require the active assigned unit token and
`X-Mu-Assignment-Version`; the runtime supplies the generation for request-context
actions to its configured server origin. Workers cannot overwrite an output from
the same run. A reset generation may publish the same output key, creating a new
canonical Artifact/File rather than redirecting earlier shared Artifacts. Physical
files are retained after logical removal or replacement.

Both `/api/requests/artifacts/download-url` and the older `/download` route return
JSON with an expiring URL, name, contentType, size and expiresAt. Resolve
that URL with worker authentication, then fetch bytes directly with response_file
**without forwarding the worker token**. S3 GET URLs last five minutes; old
Convex URLs have `expiresAt: null`. Existing issued URLs are bearer capabilities
and cannot be revoked merely by removing an Artifact or revoking a token. Both
API and storage origins must be reachable by the worker.

The 100 logical output-key limit per Request includes deletion tombstones. Single
uploads are limited to 5 GiB. Soft deletion denies new artifact access, while
retaining bytes. The current server ownership projection preserves worker payloads: Copy
and Share into a Request simply register the target's own output-key projection.
Workers do not gain access to arbitrary Space/Session Artifacts or reuse operations.
See the canonical [MasterFlow artifact ownership documentation](https://github.com/khanhtranrk/mu-masterflow/blob/master/docs/architecture/artifact-ownership.md)
for the current Artifact/File model (legacy adapters/backfills are removed), and its
[storage protocol](https://github.com/khanhtranrk/mu-masterflow/blob/master/docs/reference/request-artifacts.md)
for setup and provider behavior.

See [the executable example](../../examples/master-flow-artifacts/spec.yaml), which creates and uploads a small JSON file, downloads it again and compares the bytes. Store the spec in MasterFlow, create a disposable request for its `demo` pipeline, and run `uv run mu master-flow run <spec-slug> --unit-id artifact-demo` with `MU_MASTER_FLOW_SERVER` and `MU_MASTER_FLOW_TOKEN` configured. No GPU or model downloads are required. Only run against requests intended for this test.

Request and node execution statuses are not automatically synchronized back to
MasterFlow. The worker still reports its availability through the unit heartbeat;
the user-defined lifecycle actions remain available for explicit notifications.

Shared request/node reporting can be configured with [lifecycle events](10-events.md),
without repeating report actions in every node. Uploads remain explicit spec actions.

## Scoped worker and S3 storage compatibility

Current MasterFlow tokens are bound by the Clerk owner to one exact --unit-id and an explicit allowed-spec list. Update existing tokens in Settings → Unit tokens. Workers claim waiting requests only; custom claims for processing/completed statuses are refused. Server claim responses must include assignmentVersion; the CLI preserves it in request context, and request HTTP actions automatically include X-Mu-Assignment-Version for the configured server origin. Old in-flight clients without assignment generations must finish before cutover or have their work deliberately requeued. Worker tokens no longer allow global request inspection or creation; use the authenticated desktop.

Artifact uploads use the server-issued method and headers (S3 PUT), not a fixed POST, and completion no longer includes Convex storageId. Resolve a download URL with worker auth, then fetch bytes directly without sending the worker token to storage. Update stored YAML as well as local examples; MasterFlow's saved YAML remains authoritative and is never silently rewritten by this migration. The sibling MasterFlow [storage protocol](https://github.com/khanhtranrk/mu-masterflow/blob/master/docs/reference/request-artifacts.md) owns the full storage configuration/security contract.

### Registry-derived request parameters

MasterFlow resolves Registry/CEL inputs before claim and retains full snapshots
server-side. Workers receive literal typed `payload` plus captured Registry value
maps, exposed as `CreationSpace.Registry`, `Session.Registry`, `Request.Registry`.
They never fetch live ancestor registries or re-evaluate Registry definitions.
See [Registry inputs](02-context-and-templates.md#masterflow-registry-inputs).

### Canonical Artifact storage

The development cleanup removes the former Artifact compatibility tables and
backfill tools. The server stores one Artifact in `artifacts`, its file in
`physicalFiles`, and ownership/output placement in `artifactOwners`. Request and
node `artifacts` arrays are derived views, never stored copies. Workers continue
to use request-scoped output keys in HTTP `artifactId`; these are separate from
canonical Artifact IDs. Download responses always contain an expiring S3 URL and
no legacy-provider flag. An old upload receipt cannot complete after removal or
reset, but a new explicit upload can reuse a removed output key.
