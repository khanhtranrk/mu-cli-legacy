# CEL Spec expressions

CEL is the only Spec expression engine. Declare it explicitly in examples:

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

Omitting `expression_engine` also selects `cel-v1`. Any other declared engine,
including `python-format-v1`, fails before lifecycle execution. The old formatter
and migration command/UI have been removed. All bundled examples use CEL.

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
Whole-Spec syntax validation has a separate budget: up to 4096 wrappers,
64 levels of document nesting, and total parsed-work caps of 262144 TypeScript
semantic nodes / 2097152 celpy grammar nodes. Per-string syntax checks retain the
normal expression limits; runtime render budgets remain unchanged. Python profile
adapters prevent Python boolean/integer coercion and normalize supported casts,
scalar text and aggregate comparisons to the TypeScript profile. Interpreters also bound parsed work: TypeScript has 256 semantic AST nodes per
expression, 8192 per operation; celpy's more verbose grammar trees allow 2048
nodes per expression, 65536 per operation. Limits are implementation guards,
not a promise that every expression at the boundary is portable.

The identical `tests/fixtures/cel-v1.json` files in both independent repositories
are the shared conformance vectors; update both together and run both suites.
They cover the supported profile's common behavior, not the full CEL standard.

## Worker contract

Workers advertise `X-Mu-Expression-Engines: cel-v1` on Spec download and send
`expressionEngine: cel-v1` when claiming. MasterFlow rejects missing or unsupported
capabilities before assignment. Update and restart workers together with the server.
Saved Spec YAML must use `${{ expression }}`; old single-brace placeholders are
literal text and are never evaluated. Existing Request payloads and Registry
snapshots remain literal captured data.

### Artifact Registry references

MasterFlow's `artifact` Registry type is captured as ordinary JSON:
`{"artifactId": "<canonical Artifact ID>"}`. For example,
`${{ Session.Registry.Asset.artifactId }}` resolves the canonical ID, while
`${{ Session.Registry.Asset }}` returns the object for an object parameter.
The worker preserves and isolates this map like other captured Registry values.
This type adds no YAML parameter type, signed URL, automatic download or new
worker endpoint. Source scope validation and capture belong to MasterFlow; see
[Artifact Registry values](../../../mu-masterflow/docs/reference/registry-cel.md#artifact-registry-values).
