Add images to a ZeeRef session, starting one if needed.

Usage: `/zeeref:add <session> <files...>`

For the simple "drop these in" case, pass absolute paths positionally:

```bash
zeeref-cli add <session> /abs/path/img1.png /abs/path/img2.png
```

When per-image metadata matters — especially `caption`, which is often
distinct per image (sample IDs, observations, source notes) — pipe a JSON
array on `--stdin`:

```bash
echo '[
  {"path": "/abs/path/img1.png", "title": "10x Flake 42", "caption": "SF121 Chip 2"},
  {"path": "/abs/path/img2.png", "title": "5x overview"}
]' | zeeref-cli add <session> --stdin
```

For a single value applied uniformly to all positional files, use flags:

```bash
zeeref-cli add <session> --title "10x scan" /abs/path/*.png
```

Per-image optional fields (JSON keys, or `--flags` when applied uniformly):
- `title`, `caption` — metadata shown beside the image
- `x`, `y` — top-left in scene coords (omit to center at view)
- `scale` — multiplier on native size (default 1.0)
- `rotation` — degrees (pivots around local top-left)
- `z` — stack order (higher = on top)
- `flip` — `1` or `-1` (horizontal flip)
- `opacity` — `0.0` to `1.0`

If no session name is provided, use "default".
If no files are provided, ask the user.
Resolve all file paths to absolute before passing to the CLI.

For the full CLI surface (list/get/edit/delete/add-text/...), see `/zeeref:help`.
