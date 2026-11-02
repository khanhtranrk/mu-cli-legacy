# Versioned CEL Specs (phase 5)

Specs opt in at the root:

```yaml
expression_engine: cel-v1
pipeline:
  demo:
    params:
      count: {type: number, default: '${{ Request.Registry.Count }}'}
    nodes:
      - name: check
        precondition:
          operator: cel
          expression: '${{ params.count > 0 }}'
        run:
          - type: write_json
            path: '${{ session.output_dir + "/result.json" }}'
            content:
              count: '${{ params.count }}'
```

Without this declaration, or with `expression_engine: python-format-v1`, the
existing `{params[name]}` renderer remains unchanged. Unknown engine versions
fail before lifecycle execution. The legacy `processors` runtime is unchanged.
There is no automatic migration of saved Specs or queued Requests.

## Expression contract

Use the same `${{ expression }}` wrapper as MasterFlow Registry. A whole-value
expression returns its JSON type; surrounding whitespace is discarded. Mixed
string interpolation accepts scalars only (boolean/null text is `true`, `false`,
`null`). Maps/lists must occupy the whole value. `$${{ ... }}` emits a literal
`${{ ... }}`. Results are never interpreted as a second template. Unwrapped
`{...}` is literal in CEL Specs. Missing values and type mismatches are errors.

Values are evaluated at the existing execution stages: action/node contexts,
conditions, args, workload defaults/targets/argv/env/device args, routine returns,
events and lifecycle actions. Structural map keys/names remain literal. JSON/HTTP
payload map keys can use templates and must resolve to unique nonempty strings.

Pipeline Request payloads are **literal JSON**, including strings resembling CEL;
they are not executable Spec code. MasterFlow evaluates Registry templates before
claiming. Local request files also contain literal JSON. Workload/routine args
are rendered by their caller once; only missing declared defaults execute within
`params_for`. Default expressions use the caller's context, not sibling defaults.

Supported roots include runtime `sys`, lowercase `session`, `params`, `node`,
`actions`, `request`, `event`, `entry`, `pipeline_name`, GPU `schedule` where
available, and user context roots. They are JSON data, without host functions.
Uppercase `CreationSpace.Registry`, `Session.Registry`, `Request.Registry` contain
**captured resolved values** from the Request snapshot, not current database
values or Registry definitions. Standalone/local runs have empty Registry maps.
These three roots and the internal engine marker cannot be overwritten by context.
Creation Spaces remain organizational and add no permission boundary.

CEL pipeline `context` is request-local and merged **after shared prepare**. It
may reference captured Registry values. Entry initialization and once-per-pipeline
`prepare` have no Request, params or Registry data; put shared setup context in
entry initialization. Invalid parameters/context are checked before preparation.
Preparation results remain cached within a Flow; Registry values and request-local
context are reapplied independently for every Request, including later batches.

`operator: cel` requires a whole expression returning boolean; `expected` is an
optional boolean (default true). Existing condition operators remain available.
GPU schedule context is available for device arguments/env after device admission;
units and graph identifiers remain static.

## Bounded profile, not every CEL extension

Both implementations use pinned interpreters: `cel-python==0.5.0` (celpy's
interpreted runner, no Python eval/transpilation) and `@marcbachmann/cel-js@8.0.0`.
The supported global calls are `has`, `size`, `string`, `int`, `double`, `bool`;
receiver methods are `size`, `contains`, `startsWith`, `endsWith`. Collection
comprehensions/macros (`map`, `filter`, `all`, `exists`), regex, timestamps, duration,
bytes and object construction are outside v1. Output is JSON only: finite numbers,
safe integers (±9007199254740991), string map keys, no prototype keys.

Each rendering operation allows 192 expressions of at most 2048 characters,
128-element result collections, depth 16 and 32 KiB per expression result.
Spec syntax validation also caps total active wrappers at 192. Python profile
adapters prevent Python boolean/integer coercion and normalize supported casts,
scalar text and aggregate comparisons to the TypeScript profile. Interpreters also bound parsed work: TypeScript has 256 semantic AST nodes per
expression, 8192 per operation; celpy's more verbose grammar trees allow 2048
nodes per expression, 65536 per operation. Limits are implementation guards,
not a promise that every expression at the boundary is portable.

The identical `tests/fixtures/cel-v1.json` files in both independent repositories
are the shared conformance vectors; update both together and run both suites.
They cover the supported profile's common behavior, not the full CEL standard.

## Explicit migration

```sh
uv run mu spec migrate-cel spec.yaml                    # report only
uv run mu spec migrate-cel spec.yaml --output spec-cel.yaml
```

The source is untouched; existing output files are never overwritten. Recognized
`{root[key][nested]}` placeholders become `${{ root["key"]["nested"] }}`. Literal
braces/JSON strings, preexisting CEL, dynamic structural names/map keys, and
pipeline prepare depending on pipeline context require manual work; these block
candidate writing. Scalar interpolation changes, strict missing-key behavior,
removal of recursive rendering, types and YAML formatting/comments need review.

MasterFlow's **Preview CEL migration** creates a YAML draft and report. Review and
smoke-test it before **Save changes**; saves with stale source YAML are rejected.
The phase does not convert existing media examples automatically. New workers
advertise `expressionEngine` when claiming; the server rejects an engine mismatch
before any Request changes status. Bootstrap also requires the
`X-Mu-Expression-Engines: python-format-v1,cel-v1` capability header for CEL
Specs, so old workers cannot run their entry initialization. Upgrade/restart workers when changing engines.

### Artifact Registry references

MasterFlow's `artifact` Registry type is captured as ordinary JSON:
`{"artifactId": "<canonical Artifact ID>"}`. For example,
`${{ Session.Registry.Asset.artifactId }}` resolves the canonical ID, while
`${{ Session.Registry.Asset }}` returns the object for an object parameter.
The worker preserves and isolates this map like other captured Registry values.
This type adds no YAML parameter type, signed URL, automatic download or new
worker endpoint. Source scope validation and capture belong to MasterFlow; see
[Artifact Registry values](../../../mu-masterflow/docs/registry-cel.md#artifact-registry-values).
