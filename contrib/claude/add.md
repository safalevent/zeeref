Add images to a ZeeRef session, starting one if needed.

Usage: `/zeeref:add <session> <files...>`

Build a JSON array with one entry per image, each with `path` (absolute) plus optional metadata and transform fields. Pipe it to `zeeref-cli add`:

```bash
echo '[
  {"path": "/abs/path/img1.png", "title": "10x Flake 42", "caption": "SF121 Chip 2"},
  {"path": "/abs/path/img2.png", "title": "5x overview"}
]' | zeeref-cli add <session> --stdin
```

Per-image optional fields:
- `title`, `caption` — metadata shown beside the image
- `x`, `y` — top-left in scene coords (omit to center at view)
- `scale` — multiplier on native size (default 1.0)
- `rotation` — degrees (pivots around local top-left)
- `z` — stack order (higher = on top)
- `flip` — `1` or `-1` (horizontal flip)
- `opacity` — `0.0` to `1.0`

If no session name is provided, use "default".
If no files are provided, ask the user.
Resolve all file paths to absolute before including in the JSON.

For the full CLI surface (list/get/edit/delete/add-text/...), see `/zeeref:help`.
