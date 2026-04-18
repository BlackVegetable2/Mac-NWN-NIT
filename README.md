# macOS Dev Environment + NIT (NWN Installer Tool)

A complete macOS development shell setup paired with a native GUI tool for managing Neverwinter Nights modules.

---

## Shell Setup (Oh My Zsh)

### Quick Start

```bash
# 1. Make the script executable
chmod +x setup_shell.sh

# 2. Run it (installs Homebrew tools, Oh My Zsh, plugins, and copies .zshrc)
bash setup_shell.sh

# 3. Open a new terminal — Powerlevel10k will run its config wizard
exec zsh
```

### What gets installed

**Homebrew packages**
- `eza` — modern `ls` with colours and icons
- `bat` — `cat` with syntax highlighting
- `fzf` — fuzzy finder (Ctrl+R history, Ctrl+T file search)
- `ripgrep` (`rg`) — blazing-fast grep
- `fd` — fast `find` replacement
- `zoxide` — smarter `cd` (type `z dirname`)
- `tree`, `jq`, `wget`, `htop`, `tldr`

**Oh My Zsh plugins**
- `zsh-syntax-highlighting` — colour commands as you type
- `zsh-autosuggestions` — ghost-text completions from history
- `zsh-completions` — extra tab-completion scripts
- `git`, `macos`, `fzf`, `colored-man-pages`, and more

**Theme: Powerlevel10k**
Run `p10k configure` at any time to re-run the wizard.

> **Tip:** Install a [Nerd Font](https://www.nerdfonts.com) in your terminal (e.g. MesloLGS NF) for full icon support.

### Key aliases

| Alias | What it does |
|-------|-------------|
| `ls` / `ll` / `la` | eza with colours and icons |
| `cat` | bat with syntax highlighting |
| `gs` | `git status -sb` |
| `gl` | pretty git log graph |
| `gco` / `gcb` | git checkout / checkout -b |
| `z dirname` | jump to a recently visited dir |
| `fv` | fuzzy-pick a file and open in vim |
| `serve` | `python3 -m http.server 8080` |
| `reload` | re-source `~/.zshrc` |
| `nit` | launch the NWN Installer Tool |

---

## NIT — NWN Installer Tool (macOS)

A dark-themed GUI application for installing Neverwinter Nights modules, hakpaks, talk tables, and other content.

### Launch

```bash
# Option A — via launcher script (auto-installs dependencies if missing)
bash nit.sh

# Option B — directly
python3 nit_macos.py
```

### Features

- **Configure** your NWN home directory (auto-detects common locations)
- **Install** `.mod`, `.hak`, `.tlk`, `.erf`, `.2da`, and `.zip` archives
- **Smart routing** — files go to the correct NWN subfolder automatically
- **Library view** — browse and remove installed content by category
- **Filter** installed files by name or type

### Supported file types

| Extension | Category | Destination |
|-----------|----------|-------------|
| `.mod` | Module | `modules/` |
| `.hak` | Hakpak | `hak/` |
| `.tlk` | Talk Table | `tlk/` |
| `.erf` | ERF Archive | `erf/` |
| `.2da`, `.tga`, `.nss`, `.ncs` | Override | `override/` |
| `.bmu`, `.mp3`, `.wav` | Music | `music/` |
| `.zip` | Archive | auto-routed |

### NWN home locations

NIT checks these paths automatically:

- `~/Documents/Neverwinter Nights` (Beamdog/GOG default)
- `~/Library/Application Support/Steam/steamapps/common/Neverwinter Nights Enhanced Edition`
- If neither exists, click **Browse…** to set it manually.

### Where to find modules

- [Neverwinter Vault](https://neverwintervault.org) — the largest NWN content archive
- [Beamdog Forums](https://forums.beamdog.com) — community and official modules

---

## File structure

```
setup_shell.sh    ← run this first (installs everything)
.zshrc            ← copy to ~/.zshrc (setup_shell.sh does this for you)
nit_macos.py      ← NWN Installer Tool GUI (Python 3 + tkinter)
nit.sh            ← launcher with auto-dependency check
README.md         ← this file
```

## Requirements

- macOS 12 Monterey or later
- Internet connection (for Homebrew installs)
- Python 3.8+ (installed by setup_shell.sh if missing)