# NIT — NWN Installer Tool (macOS)

A dark-themed GUI for installing Neverwinter Nights modules, hakpaks, talk tables, and other content directly from the Neverwinter Vault or from local files.

## Requirements

- macOS 12 Monterey or later
- Python 3.8+
- **tkinter** — `brew install python-tk` (if `python3 -c "import tkinter"` fails)
- **unar** — `brew install unar` (needed for .rar / .7z / .exe archives)

## Launch

```bash
# Option A — launcher script (checks dependencies automatically)
bash nit.sh

# Option B — directly
python3 nit_macos.py
```

## Features

- **URL install** — paste a Neverwinter Vault project page URL; NIT fetches the page, downloads the archive, parses the module's GFF binary to extract its declared hak list, follows any "Required projects" links on the page, and installs everything to the right folders automatically
- **Readme heuristic** — if a readme ships in the archive, NIT scans it for additional hak references and prompts you (one at a time, with the exact source text) before downloading anything speculative
- **Local install** — queue up `.mod`, `.hak`, `.tlk`, `.erf`, `.zip`, and other files and install them in one click
- **Smart routing** — files land in the correct NWN subfolder without any manual sorting
- **Library view** — browse and remove installed content by category; filter by name or type
- **Hak verification** — after install, cross-checks the module's declared hak list against what's actually in `hak/` and reports any gaps with actionable instructions

## Supported file types

| Extension | Destination |
|-----------|-------------|
| `.mod` | `modules/` |
| `.hak` | `hak/` |
| `.tlk` | `tlk/` |
| `.erf` | `erf/` |
| `.2da`, `.tga`, `.nss`, `.ncs` | `override/` |
| `.bmu`, `.mp3`, `.wav` | `music/` |
| `.zip`, `.rar`, `.7z`, `.exe` | extracted and auto-routed |

## NWN home directory

NIT checks these paths automatically on startup:

- `~/Documents/Neverwinter Nights` (Beamdog / GOG default)
- `~/Library/Application Support/Steam/steamapps/common/Neverwinter Nights Enhanced Edition`

If neither is found, click **Browse…** or paste the path manually and click **Save**.

## Where to find modules

- [Neverwinter Vault](https://neverwintervault.org) — the largest NWN content archive
- [Beamdog Forums](https://forums.beamdog.com) — community discussion and module recommendations
