# ZeeRef

A whiteboard for images and markdown notes on an infinite canvas. Use
it to track a project over time, or as a mood board for reference
images and artist assets.

ZeeRef is a personal fork of [BeeRef](https://github.com/rbreu/beeref)
by rbreu. Three directions pulled it off the original path:

- **Large images** — pans and zooms multi-gigapixel images smoothly
  via on-disk tile pyramids, originally for a scientific microscopy
  use case.
- **Markdown text items** — text items render markdown, so notes,
  captions, and links live next to the images they're about.
- **CLI for scripting and AI agents** — `zeeref-cli` lets external
  processes (including agents like Claude Code) add images, drop
  notes, edit items, and inspect the scene over a local socket.

## Installation

### Prebuilt binaries (recommended)

Download from the [latest release](https://github.com/zackgomez/zeeref/releases/latest):

- **Windows** — `ZeeRef-x.y.z-windows-setup.exe`
- **macOS (Apple Silicon)** — `ZeeRef-x.y.z-macos-arm64.zip`
- **Linux (x86_64)** — `ZeeRef-x.y.z-linux-x86_64.tar.gz`

### From source

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/zackgomez/zeeref.git
cd zeeref
uv sync
uv run zeeref
```

## CLI

`zeeref-cli` controls a running ZeeRef session over a local socket —
useful for shell scripting and for AI agents that need to drop images,
write notes, or read what's currently on the board. See
`zeeref-cli --help` for the full subcommand list.

## License

GPLv3 — see [LICENSE](LICENSE).

Copyright (C) 2025-2026 Zack Gomez.
Original BeeRef copyright (C) 2021-2024 Rebecca Breu.
