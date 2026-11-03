# Legacy Python documentation

This repository is a migration reference. New CLI development belongs to
[mu-cli](https://github.com/khanhtranrk/mu-cli/blob/master/docs/README.md).
These guides describe Python behavior; do not assume TypeScript parity.

- [Spec format index](spec-format/README.md): lifecycle, context, workloads,
  actions, conditions, pipelines, CLI forms, routines, events and CEL.
- [Request-to-node execution](request-to-node-scheduling.md): Python scheduling
  and reporting implementation map.
- [Standalone tools](tool/README.md): Transcriptor evaluation and subtitles.
- [Media example](../examples/song-with-cover/README.md): reference only; not a routine smoke test.
- [Scheduler smoke example](../examples/scheduler-smoke/README.md): focused scheduling fixture.
- [Agent instructions](../AGENTS.md): read-only default and migration safeguards.

Server APIs and storage are owned by
[MasterFlow](https://github.com/khanhtranrk/mu-masterflow/blob/master/docs/README.md).
The [MU workspace](https://github.com/khanhtranrk/mu/blob/master/docs/README.md)
owns integration history. Historical notes and TODOs do not authorize new work;
verified source behavior takes precedence over documentation discrepancies.
