# MU CLI

**Run reproducible pipelines across CPU and GPU workers.**

MU CLI is the execution engine of [MU](https://github.com/khanhtranrk/mu). Define workloads and dependency-aware pipelines in YAML or JSON, then run them locally or connect a worker to [MU MasterFlow](https://github.com/khanhtranrk/mu-masterflow). The repository is named **mu-cli**, but the executable command is simply **`mu`**.

## What it does

- Runs reusable subprocess workloads as nodes in dependency graphs.
- Resolves parameters, templates, conditions, and lifecycle actions.
- Schedules CPU work and GPU workloads using device capacity units.
- Connects to MasterFlow with scoped worker tokens to claim requests, report progress, and transfer artifacts directly through signed storage URLs.

## Get started

**Requirements:** Python 3.13+ and [uv](https://docs.astral.sh/uv/). GPU tooling is optional for supported devices.

```bash
git clone https://github.com/khanhtranrk/mu-cli.git
cd mu-cli
uv sync
uv run mu --help
```

Run a local pipeline with an existing spec and request file:

```bash
uv run mu run --spec ./spec.yaml --working_dir ./work \\
  --pipeline song-with-cover,./examples/song-with-cover/input.yaml
```

The repository includes a [song-with-cover example](examples/song-with-cover/spec.yaml) to study, but **do not execute the full media workflow as a routine smoke test**: it can download large models and consume substantial GPU, disk, and network resources.

For a remote worker, see the [master-flow CLI guide](docs/spec-format/07-master-flow-and-cli.md) for required server configuration and commands.

## Documentation

| Topic | Where to read |
| --- | --- |
| Spec format, contexts, actions, conditions, pipelines | [Spec format guide](docs/spec-format/README.md) |
| Local and remote execution, CLI flags, lifecycle | [Master-flow and CLI](docs/spec-format/07-master-flow-and-cli.md) |
| Spec-defined lifecycle hooks | [Events](docs/spec-format/10-events.md) |
| Reusable routines and file metadata | [Routines](docs/spec-format/09-routines.md) |
| WhisperX transcript evaluation and subtitle conversion | [Transcriptor tool](docs/tool/transcriptor.md) |
| Complete YAML example | [Example spec](examples/song-with-cover/spec.yaml) |
| Cross-project workspace | [MU workspace](https://github.com/khanhtranrk/mu) |

The detailed schema, operator reference, scheduling semantics, and integration contracts live in `docs/`, not in this overview.

## For coding agents and contributors

Start with **[AGENTS.md](AGENTS.md)**. It explains the runtime architecture, current vs. legacy spec paths, invariants, and safe validation. Read the relevant docs and source before changing behavior. Work on the CLI here; cross-project changes may also require updates to MasterFlow and the parent workspace's submodule pointer.

For a quick non-destructive check:

```bash
uv run mu --help
```

**MU CLI executes the work. [MasterFlow](https://github.com/khanhtranrk/mu-masterflow) manages it.**
