# 08. Complete CPU example

This example uses no network, models or GPU. Save `spec.yaml`:

```yaml
expression_engine: cel-v1
workloads:
  verify:
    params:
      path: {type: string}
    target: python
    args:
      - -c
      - 'import json,sys; from pathlib import Path; assert json.loads(Path(sys.argv[1]).read_text())["width"] == 1200'
      - '${{ params.path }}'
    units: 0
pipeline:
  demo:
    params:
      name: {type: string}
      width: {type: number, default: 1200}
    nodes:
      - name: write
        context:
          result: '${{ session.output_dir + "/" + params.name + ".json" }}'
        precondition:
          operator: cel
          expression: '${{ params.width > 0 }}'
        before_run:
          - type: write_json
            path: '${{ result }}'
            content:
              name: '${{ params.name }}'
              width: '${{ params.width }}'
        run:
          - type: execute_workload
            workload: verify
            args:
              path: '${{ result }}'
        postcondition:
          operator: file_exists
          path: '${{ result }}'
```

Save `request.yaml`:

```yaml
name: first-demo
width: 1200
```

Run from the directory containing both files:

```sh
uv run mu run --spec spec.yaml --pipeline demo,request.yaml --working_dir ./work
```

The node writes `work/output/first-demo.json`, checks its numeric width in a
subprocess, and verifies the file exists. Supplied parameters are literal data;
`width: "1200"` fails type validation. Node context can use params, while pipeline
context and shared prepare run before params exist.

For events, routines, remote uploads and media workflows, inspect the maintained
[examples directory](../../examples/). The song-with-cover examples require
external media tools and models; inspect them without running heavy workloads.
