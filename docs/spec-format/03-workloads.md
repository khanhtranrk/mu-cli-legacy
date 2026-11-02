# 03. Workloads

A workload is a reusable subprocess declaration. It does not run when it is defined. A workload runs only when a node invokes it:

```yaml
run:
  - type: execute_workload
    workload: transcribe
    args:
      input: "${{ params.input }}"
      output: "${{ session.output_dir }}/transcript"
```

## Minimum schema

```yaml
workloads:
  transcode:
    params:
      input: {type: string}
      output: {type: string}
    target: ffmpeg
    args:
      - -i
      - "${{ params.input }}"
      - "${{ params.output }}"
    env: {}
    device_args: []
    units: 0
```

## `name` / workload key

The outer key is a unique name:

```yaml
workloads:
  transcode: {}
```

Mu uses `workload: transcode` to look up the declaration. There is no separate `name` field in the workload runtime; the name comes from the mapping key.

## `params`

`params` describes the workload's required parameters. It is a name-to-declaration mapping:

```yaml
params:
  input:
    type: string
  quality:
    type: string
    default: medium
```

Before the process starts:

1. Mu renders `execute_workload.args` using the node context.
2. Mu evaluates missing defaults in the caller context.
3. Mu validates the resulting values against the workload schema.
4. Mu creates `workload_context.params`; supplied values are never rendered again.

Each node/workload pair has its own parameter values. Think of `params` as a contract: the executable does not need to know the global pipeline request.

Parameter declarations do not impose types on the shell. Every value in the final argument list is converted to a string:

```python
[str(target), *map(str, args)]
```

## `env`

`env` is a mapping of additional environment variables:

```yaml
env:
  HF_HUB_ENABLE_HF_TRANSFER: "1"
  MODEL: "${{ params.model }}"
```

Mu builds a child environment from:

```text
Mu's current environment + the declaration environment
```

A key in `env` overrides an environment variable with the same name. `env` is rendered before the subprocess is invoked.

## `target`

`target` is the executable or the first command of the argv:

```yaml
target: ffmpeg
target: "${{ sys.cache_dir }}/bin/yue-synth"
```

Mu calls `subprocess.run([target, *args])`, not a shell. Therefore, `target: "python -m demo"` does not behave like a shell command; use:

```yaml
target: python
args: [-m, demo]
```

If the executable cannot be found, the subprocess raises an error during execution. Use `precondition.operator: exit_code` to report the problem earlier if desired.

## `args`

`args` is the argument list and must be a list:

```yaml
args:
  - --model
  - '${{ sys.models["YuE2-3B-BF16"] }}'
  - --request
  - "${{ params.request }}"
```

No `shlex` quoting is needed in YAML. Mu constructs the argv directly:

```yaml
args: [--name, "hello world"]
```

The result is:

```text
["--name", "hello world"]
```

No additional shell quoting is added.

If a single argument placeholder renders to a list or dictionary, its declaration is unstructured; each final argument is still converted to a string.

## `device_args`

`device_args` is appended to argv only when the workload receives a GPU:

```yaml
device_args:
  - --device
  - "${{ schedule.device_family }}:${{ schedule.device_index }}"
```

The argv assembly order is:

```text
target
+ args
+ device_args
```

If `device_args` is omitted, it defaults to `[]`. It is not appended when the workload runs CPU-only, even if declared.

Device names:

- NVIDIA backends render as `cuda`;
- AMD backends render as `rocm`;
- device indices remain strings.

## `units`

`units` is the VRAM requirement measured in 4 GiB bands:

| Estimated VRAM | units |
|---:|---:|
| 1–4 GiB | 1 |
| 5–8 GiB | 2 |
| 9–12 GiB | 3 |
| 13–16 GiB | 4 |
| 17–20 GiB | 5 |

The default is `0` when omitted.

```yaml
units: 0  # CPU-only
units: 3  # requires a GPU with capacity >= 3
```

Scheduler behavior:

1. Find a GPU with sufficient capacity.
2. Select the GPU with the lowest remaining free units after allocating this workload.
3. Hold the allocation until the node workload and actions finish.
4. If no GPU has enough capacity, run with the CPU instead of attempting to use a GPU that lacks enough VRAM.

Mu does not combine multiple requests to split VRAM. A node requesting 3 units must have a GPU with at least 3 free units.

## Workload result

- Exit code `0`: workload succeeds.
- Any other exit code: the node fails.
- Error `stderr`/`stdout` is included in the node message when available.
- stdout and stderr are not normally printed after success.

## Complete YAML example

```yaml
workloads:
  yue2:
    params:
      request: {type: string}
      output: {type: string}
      score: {type: string}
      model:
        type: string
        default: '${{ sys.models["YuE2-3B-BF16"] }}'
      vae:
        type: string
        default: '${{ sys.models["YuE2-Vae-F32"] }}'
    env:
      HF_HUB_ENABLE_HF_TRANSFER: "1"
    target: "${{ sys.cache_dir }}/bin/yue-synth"
    args:
      - --model
      - "${{ params.model }}"
      - --vae
      - "${{ params.vae }}"
      - --request
      - "${{ params.request }}"
      - --out
      - "${{ params.output }}"
      - --score
      - "${{ params.score }}"
    device_args:
      - --device
      - "${{ schedule.device_family }}:${{ schedule.device_index }}"
    units: 3
```

Node invocation:

```yaml
- name: generate-song
  run:
    - type: execute_workload
      workload: yue2
      args:
        request: "${{ session.cache_dir }}/requests/song.json"
        output: "${{ session.output_dir }}/song/song.mp3"
        score: "${{ session.output_dir }}/song/song.abc"
```
