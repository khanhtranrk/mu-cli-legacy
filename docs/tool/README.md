# MU CLI Tools

This directory contains documentation for standalone tools provided by the MU CLI.

## Available Tools

- [Transcriptor](transcriptor.md) - Evaluate WhisperX transcripts against lyrics and convert to subtitle formats (SRT, ASS, karaoke)

## Using Tools

All tools are accessed through the `mu tool` command:

```bash
mu tool <tool-name> <command> [options]
```

For help on a specific tool:

```bash
mu tool <tool-name> --help
mu tool <tool-name> <command> --help
```

## Adding New Tools

To add a new tool to MU CLI:

1. Create a new sub-app in `src/mu/cli.py` under the `tool_app` typer instance
2. Implement the tool logic in a new module under `src/mu/`
3. Add documentation in this directory following the existing format
4. Update the main README.md to reference the new tool documentation

See the transcriptor implementation in `src/mu/transcriptor.py` and `src/mu/cli.py` for a reference.
