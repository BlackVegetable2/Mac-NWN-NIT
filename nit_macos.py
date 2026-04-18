#!/usr/bin/env python3
"""
NIT — Neverwinter Nights Installer Tool (macOS Edition)
========================================================
A GUI tool for installing and managing NWN modules, hakpaks, talk tables,
override files, and music on macOS.

URL install flow:
  1. Paste a Neverwinter Vault project page URL
  2. NIT scrapes the page for download links and pulls the archive
  3. If a .mod file is found, its GFF/ERF data is parsed to extract the
     Mod_HakList — the list of hakpak names the module declares it needs
  4. Each hak is searched on the Vault and downloaded automatically
  5. Everything is installed to the correct NWN subdirectories

Dependency resolution is driven by the GFF binary data inside the .mod
file itself, so it is ground-truth rather than relying on human-maintained
metadata (as the original Windows NIT does).

Requirements:
  Python 3.8+  •  tkinter (brew install python-tk if missing)
  All HTTP via stdlib urllib — no third-party packages needed.
"""

import html
import html.parser
import io
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Iterator, Optional

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

APP_NAME    = "NIT — NWN Installer Tool (macOS)"
APP_VERSION = "1.2.0"
CONFIG_FILE = Path.home() / ".config" / "nit_macos" / "config.json"

NWN_DIRS: dict[str, dict] = {
    "modules":  {"label": "Modules",      "exts": {".mod"},                              "icon": "M"},
    "hak":      {"label": "Hakpaks",      "exts": {".hak"},                              "icon": "H"},
    "tlk":      {"label": "Talk Tables",  "exts": {".tlk"},                              "icon": "T"},
    "override": {"label": "Override",     "exts": {".2da", ".tga", ".dds", ".bmp",
                                                    ".nss", ".ncs", ".wav", ".mp3",
                                                    ".ogg"},                              "icon": "O"},
    "music":    {"label": "Music",        "exts": {".bmu", ".mp3", ".wav"},              "icon": "S"},
    "erf":      {"label": "ERF Archives", "exts": {".erf"},                              "icon": "E"},
}

DEFAULT_NWN_PATHS = [
    Path.home() / "Documents" / "Neverwinter Nights",
    Path.home() / "Library" / "Application Support" / "Steam" /
        "steamapps" / "common" / "Neverwinter Nights Enhanced Edition",
    Path("/Applications/Neverwinter Nights Enhanced Edition.app/Contents/Resources/data"),
    Path.home() / ".local" / "share" / "Neverwinter Nights",
]

VAULT_BASE = "https://neverwintervault.org"

# NWN resource type numbers used in ERF key lists
NWN_RT_IFO = 2008  # module.ifo

# Dark palette
BG     = "#1e1e2e"
BG2    = "#313244"
BG3    = "#45475a"
FG     = "#cdd6f4"
ACCENT = "#89b4fa"
GREEN  = "#a6e3a1"
RED    = "#f38ba8"
YELLOW = "#f9e2af"
MAUVE  = "#cba6f7"
TEAL   = "#94e2d5"

# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}

def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

def auto_detect_nwn() -> Optional[Path]:
    for p in DEFAULT_NWN_PATHS:
        if p.is_dir():
            return p
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# ERF Container Parser
# ═══════════════════════════════════════════════════════════════════════════════
# NWN uses ERF (Encapsulated Resource Format) as a container for .mod, .hak,
# and .erf files.  The .mod file is an ERF that contains (among other things)
# module.ifo — a GFF file that holds the module's metadata including its
# required hakpak list.
#
# ERF V1.0 binary layout:
#   [0]   4 bytes  file type  ("ERF ", "MOD ", "HAK ", …)
#   [4]   4 bytes  version    ("V1.0")
#   [8]   4 bytes  LanguageCount
#   [12]  4 bytes  LocalizedStringSize
#   [16]  4 bytes  EntryCount
#   [20]  4 bytes  OffsetToLocalizedString
#   [24]  4 bytes  OffsetToKeyList
#   [28]  4 bytes  OffsetToResourceList
#   … (build year/day, description, 116 reserved bytes) …
#   KeyList:  EntryCount × { ResRef[16], ResID uint32, ResType uint16, pad[2] }
#   ResList:  EntryCount × { OffsetToResource uint32, ResourceSize uint32 }
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ErfEntry:
    name: str       # resref (lowercase, up to 32 chars for NWN:EE)
    res_type: int   # numeric resource type
    offset: int     # byte offset in ERF file
    size: int       # resource size in bytes

class ErfReader:
    """Parse NWN ERF/MOD/HAK container files and extract resources by name."""

    # Map common resource type numbers → file extension strings
    _EXT = {
        1: "bmp", 3: "tga", 4: "wav", 6: "plt", 7: "ini", 10: "bmu",
        12: "txt", 2002: "ncs", 2003: "are", 2004: "itp", 2005: "trn",
        2006: "utc", 2007: "dlg", 2008: "ifo", 2009: "fac", 2010: "bic",
        2011: "ute", 2012: "utd", 2013: "uts", 2014: "ltt", 2015: "gff",
        2016: "uti", 2017: "utm", 2018: "utp", 2019: "dtf", 2020: "gic",
        2021: "gui", 2022: "css", 2023: "ccs", 2024: "nss", 2025: "hak",
        2026: "nwm", 2027: "bik", 2029: "erf", 2030: "bif", 2031: "key",
    }

    def __init__(self, path: Path):
        self.path = Path(path)
        self._entries: list[ErfEntry] = []
        self._data: bytes = self.path.read_bytes()
        self._parse()

    def _parse(self) -> None:
        d = self._data
        # Minimal sanity check
        if len(d) < 160:
            raise ValueError(f"File too small to be an ERF: {self.path}")
        file_type = d[0:4].decode("ascii", errors="replace").strip()
        version   = d[4:8].decode("ascii", errors="replace").strip()
        if version not in ("V1.0", "V1.1", "V2.0"):
            raise ValueError(f"Unsupported ERF version {version!r} in {self.path}")

        entry_count         = struct.unpack_from("<I", d, 16)[0]
        offset_to_key_list  = struct.unpack_from("<I", d, 24)[0]
        offset_to_res_list  = struct.unpack_from("<I", d, 28)[0]

        # Key list: each entry is 24 bytes  (resref[16] + resid[4] + restype[2] + pad[2])
        for i in range(entry_count):
            k_off = offset_to_key_list + i * 24
            raw_name = d[k_off : k_off + 16]
            name     = raw_name.rstrip(b"\x00").decode("latin-1").lower()
            res_id   = struct.unpack_from("<I", d, k_off + 16)[0]
            res_type = struct.unpack_from("<H", d, k_off + 20)[0]

            r_off   = offset_to_res_list + i * 8
            res_off = struct.unpack_from("<I", d, r_off)[0]
            res_sz  = struct.unpack_from("<I", d, r_off + 4)[0]

            self._entries.append(ErfEntry(name, res_type, res_off, res_sz))

    def entries(self) -> list[ErfEntry]:
        return list(self._entries)

    def ext_for(self, res_type: int) -> str:
        return self._EXT.get(res_type, f"res{res_type}")

    def read(self, name: str, res_type: Optional[int] = None) -> Optional[bytes]:
        """Return raw bytes for the named resource, or None if not found."""
        name = name.lower().strip()
        for e in self._entries:
            if e.name == name and (res_type is None or e.res_type == res_type):
                return self._data[e.offset : e.offset + e.size]
        return None

    def read_ifo(self) -> Optional[bytes]:
        """
        Return module.ifo bytes.
        Searches by name only — the resource type number for IFO varies between
        NWN 1.69 (often 2014 or differs by build) and NWN:EE, so requiring an
        exact type match causes false negatives.  The name "module" is unique
        within any valid .mod container, so name-only matching is unambiguous.
        """
        return self.read("module")

# ═══════════════════════════════════════════════════════════════════════════════
# GFF Binary Parser
# ═══════════════════════════════════════════════════════════════════════════════
# GFF V3.2 layout (all little-endian):
#   Header (56 bytes):
#     FileType[4], Version[4]="V3.2",
#     StructOffset, StructCount,
#     FieldOffset,  FieldCount,
#     LabelOffset,  LabelCount,
#     FieldDataOffset, FieldDataCount,
#     FieldIndicesOffset, FieldIndicesCount,
#     ListIndicesOffset,  ListIndicesCount
#   Struct array:  StructCount × { Type uint32, DataOrDataOffset uint32, FieldCount uint32 }
#   Field array:   FieldCount  × { Type uint32, LabelIndex uint32, DataOrDataOffset uint32 }
#   Label array:   LabelCount  × char[16]  (null-padded)
#   FieldData block (raw bytes referenced by complex field types)
#   FieldIndices block (uint32 array; struct with >1 field stores byte-offset here)
#   ListIndices block (uint32 count + uint32 struct-indices for each List field)
#
# Field types 0-8 store their value directly in DataOrDataOffset (≤4 bytes).
# Types 9-13 store a byte offset into FieldData.
# Type 14 (Struct) stores a struct index.
# Type 15 (List)   stores a byte offset into ListIndices.
# ═══════════════════════════════════════════════════════════════════════════════

class GffReader:
    """Parse NWN GFF V3.2 binary format into nested Python dicts."""

    # Field type constants
    BYTE          = 0
    CHAR          = 1
    WORD          = 2
    SHORT         = 3
    DWORD         = 4
    INT           = 5
    DWORD64       = 6
    INT64         = 7
    FLOAT_T       = 8
    DOUBLE_T      = 9
    CExoString    = 10
    ResRef        = 11
    CExoLocString = 12
    VOID_T        = 13
    Struct        = 14
    List          = 15
    Vector        = 16
    Quaternion    = 17

    def __init__(self, data: bytes):
        self._d = data
        if len(data) < 56:
            raise ValueError("GFF data too short")
        self._parse_header()

    def _u32(self, offset: int) -> int:
        return struct.unpack_from("<I", self._d, offset)[0]

    def _i32(self, offset: int) -> int:
        return struct.unpack_from("<i", self._d, offset)[0]

    def _parse_header(self) -> None:
        d = self._d
        self._struct_off   = self._u32(8)
        self._struct_count = self._u32(12)
        self._field_off    = self._u32(16)
        self._field_count  = self._u32(20)
        self._label_off    = self._u32(24)
        self._label_count  = self._u32(28)
        self._fdata_off    = self._u32(32)
        self._finds_off    = self._u32(40)   # field indices
        self._lind_off     = self._u32(48)   # list indices

    def _label(self, index: int) -> str:
        off = self._label_off + index * 16
        raw = self._d[off : off + 16]
        return raw.rstrip(b"\x00").decode("latin-1")

    def _field_label(self, field_index: int) -> str:
        off = self._field_off + field_index * 12
        label_idx = self._u32(off + 4)
        return self._label(label_idx)

    def _read_struct(self, struct_index: int) -> dict:
        off = self._struct_off + struct_index * 12
        _type           = self._u32(off)
        data_or_offset  = self._u32(off + 4)
        field_count     = self._u32(off + 8)

        result = {}
        if field_count == 1:
            # DataOrDataOffset IS the single field index
            field_indices = [data_or_offset]
        else:
            # DataOrDataOffset is a byte offset into the FieldIndices block
            fi_off = self._finds_off + data_or_offset
            field_indices = [
                self._u32(fi_off + i * 4) for i in range(field_count)
            ]

        for fi in field_indices:
            label, value = self._read_field(fi)
            result[label] = value

        return result

    def _read_field(self, field_index: int) -> tuple[str, object]:
        off = self._field_off + field_index * 12
        ftype      = self._u32(off)
        label_idx  = self._u32(off + 4)
        raw_data   = self._u32(off + 8)   # either inline value or FieldData offset

        label = self._label(label_idx)

        if ftype == self.BYTE:
            value = raw_data & 0xFF
        elif ftype == self.CHAR:
            value = struct.unpack("<b", struct.pack("<I", raw_data & 0xFF))[0]
        elif ftype == self.WORD:
            value = raw_data & 0xFFFF
        elif ftype == self.SHORT:
            value = struct.unpack("<h", struct.pack("<I", raw_data & 0xFFFF))[0]
        elif ftype == self.DWORD:
            value = raw_data
        elif ftype == self.INT:
            value = self._i32(off + 8)
        elif ftype == self.FLOAT_T:
            value = struct.unpack_from("<f", self._d, off + 8)[0]

        # 8-byte types stored in FieldData
        elif ftype == self.DWORD64:
            fd_off = self._fdata_off + raw_data
            value = struct.unpack_from("<Q", self._d, fd_off)[0]
        elif ftype == self.INT64:
            fd_off = self._fdata_off + raw_data
            value = struct.unpack_from("<q", self._d, fd_off)[0]
        elif ftype == self.DOUBLE_T:
            fd_off = self._fdata_off + raw_data
            value = struct.unpack_from("<d", self._d, fd_off)[0]

        elif ftype == self.CExoString:
            fd_off = self._fdata_off + raw_data
            length = self._u32(fd_off)
            value  = self._d[fd_off + 4 : fd_off + 4 + length].decode("latin-1")

        elif ftype == self.ResRef:
            fd_off = self._fdata_off + raw_data
            length = self._d[fd_off]            # single byte length prefix
            value  = self._d[fd_off + 1 : fd_off + 1 + length].decode("latin-1")

        elif ftype == self.CExoLocString:
            # { TotalSize uint32, StringRef uint32, StringCount uint32,
            #   [LanguageID uint32, Length uint32, String chars] × StringCount }
            fd_off     = self._fdata_off + raw_data
            total_size = self._u32(fd_off)
            str_ref    = self._u32(fd_off + 4)
            str_count  = self._u32(fd_off + 8)
            cursor     = fd_off + 12
            strings: dict[int, str] = {}
            for _ in range(str_count):
                lang_id = self._u32(cursor)
                length  = self._u32(cursor + 4)
                s = self._d[cursor + 8 : cursor + 8 + length].decode("latin-1", errors="replace")
                strings[lang_id] = s
                cursor += 8 + length
            # Language 0 = English (masculine), 1 = English (feminine)
            value = strings.get(0) or strings.get(1) or next(iter(strings.values()), "")

        elif ftype == self.VOID_T:
            fd_off = self._fdata_off + raw_data
            length = self._u32(fd_off)
            value  = self._d[fd_off + 4 : fd_off + 4 + length]

        elif ftype == self.Struct:
            value = self._read_struct(raw_data)

        elif ftype == self.List:
            # raw_data = byte offset into ListIndices block
            li_off = self._lind_off + raw_data
            count  = self._u32(li_off)
            value  = [
                self._read_struct(self._u32(li_off + 4 + i * 4))
                for i in range(count)
            ]

        elif ftype == self.Vector:
            fd_off = self._fdata_off + raw_data
            value = struct.unpack_from("<3f", self._d, fd_off)

        elif ftype == self.Quaternion:
            fd_off = self._fdata_off + raw_data
            value = struct.unpack_from("<4f", self._d, fd_off)

        else:
            value = None  # unknown type

        return label, value

    def root(self) -> dict:
        """Return the root struct as a nested Python dict."""
        return self._read_struct(0)

# ═══════════════════════════════════════════════════════════════════════════════
# ModuleInfo — extract metadata from a .mod file
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ModuleInfo:
    path:    Path
    name:    str       = ""
    hak_list: list[str] = field(default_factory=list)   # hak names without .hak
    error:   str       = ""

    @classmethod
    def from_file(cls, path: Path) -> "ModuleInfo":
        info = cls(path=path)
        try:
            erf     = ErfReader(path)
            ifo_raw = erf.read_ifo()
            if ifo_raw is None:
                # Dump the actual resources present so the user can see what's inside
                present = ", ".join(
                    f"{e.name}(type={e.res_type})"
                    for e in erf.entries()[:20]
                ) or "(none)"
                info.error = (
                    f"module.ifo not found inside .mod — "
                    f"resources present: [{present}]"
                )
                return info
            gff  = GffReader(ifo_raw)
            root = gff.root()
            info.name     = root.get("Mod_Name", "") or path.stem
            hak_structs   = root.get("Mod_HakList", [])
            info.hak_list = [
                s.get("Mod_Hak", "").lower().strip()
                for s in hak_structs
                if s.get("Mod_Hak", "").strip()
            ]
        except Exception as exc:
            info.error = str(exc)
        return info

# ═══════════════════════════════════════════════════════════════════════════════
# Vault HTML Scraper
# ═══════════════════════════════════════════════════════════════════════════════
# The Neverwinter Vault (neverwintervault.org) runs Drupal.  It has no public
# REST API, so we parse HTML.  The key patterns that are stable across Drupal
# themes are:
#   • Download links go to  /sites/default/files/projects/...  (FTP mirror)
#   • Or to   /project/download/...  (a tracked redirect)
#   • Search lives at  /search?search_api_fulltext=QUERY
#   • Search result titles are in  <h3 class="..."><a href="/project/...">
# We also handle direct file links (.mod, .hak, .zip, .rar, .7z, .exe).
# ═══════════════════════════════════════════════════════════════════════════════

class _LinkParser(html.parser.HTMLParser):
    """Collect all <a href="..."> links from an HTML document."""

    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []   # (href, text)
        self._cur_text: list[str] = []
        self._in_a = False
        self._cur_href = ""

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            self._cur_href = href
            self._cur_text = []
            self._in_a = True

    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            text = html.unescape("".join(self._cur_text)).strip()
            self.links.append((self._cur_href, text))
            self._in_a = False

    def handle_data(self, data):
        if self._in_a:
            self._cur_text.append(data)


_DOWNLOAD_EXTS = {".zip", ".mod", ".hak", ".tlk", ".rar", ".7z", ".exe", ".erf"}

# Drupal FID-based URLs:  /file/NUMBER  or  /file/NUMBER/download
_FILE_FID_RE = re.compile(r'/file/\d+', re.IGNORECASE)

# Vault download-counter script (confirmed format as of 2024/2025):
#   /sites/all/modules/pubdlcnt/pubdlcnt.php?fid=NUMBER
_PUBDLCNT_RE = re.compile(r'pubdlcnt\.php\?.*fid=\d+', re.IGNORECASE)

def _is_download_link(href: str) -> bool:
    low = href.lower()
    # Neverwinter Vault pubdlcnt download-counter script (current primary format)
    if _PUBDLCNT_RE.search(low):
        return True
    # Known NWN/archive file extensions in the URL path
    if any(low.endswith(ext) for ext in _DOWNLOAD_EXTS):
        return True
    # Drupal FID download URLs:  /file/12345  or  /file/12345/download
    if _FILE_FID_RE.search(low):
        return True
    # Old-style Vault direct file hosting
    if "/sites/default/files/" in low:
        return True
    if "/project/download/" in low:
        return True
    return False


def _extract_required_project_hrefs(html_text: str) -> list[str]:
    """
    Return /project/... hrefs listed under the 'Required projects' field on a
    Vault project page.

    The Vault (Drupal) renders this as a labeled section.  We locate the
    'required projects' text in the HTML, then collect every /project/ href
    in the window before the next distinct section label.  This is resilient
    to theme changes because it operates on text content rather than class names.
    """
    start_m = re.search(r'required[\s_-]*projects', html_text, re.IGNORECASE)
    if not start_m:
        return []

    # Slice from the label onwards; skip the first 20 chars so we don't
    # accidentally match "Required projects" itself if it contains a self-link
    tail = html_text[start_m.start():]
    end_m = re.search(
        r'(?:related[\s_-]*projects|patreon|permissions[\s_-]*&|files\b)',
        tail[20:],
        re.IGNORECASE,
    )
    window = tail[: end_m.start() + 20] if end_m else tail[:4000]

    # Collect distinct /project/ hrefs (excludes download links, nav links, etc.)
    seen: set[str] = set()
    hrefs: list[str] = []
    for h in re.findall(r'href=["\']?(/project/[^"\'>\s]+)', window, re.IGNORECASE):
        if h not in seen:
            seen.add(h)
            hrefs.append(h)
    return hrefs


def _absolute(href: str, base: str = VAULT_BASE) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return urllib.parse.urljoin(base, href)


def _http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"NIT-macOS/{APP_VERSION} (compatible; +https://github.com/)",
            "Accept":     "text/html,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class VaultClient:
    """Scrape Neverwinter Vault for project pages and file downloads."""

    # ── Page scraping ─────────────────────────────────────────────────────────

    def fetch_download_urls(
        self, page_url: str
    ) -> tuple[list[str], list[str], list[str]]:
        """
        Given a Vault project page URL, return:
          (download_urls, all_hrefs, required_project_urls)

        download_urls          — pubdlcnt / direct file download links
        all_hrefs              — every distinct href (for diagnostics)
        required_project_urls  — absolute URLs from the 'Required projects'
                                 field; these pages host the haks this module
                                 depends on and should be visited first
        Raises urllib.error.URLError / ValueError on failure.
        """
        html_bytes = _http_get(page_url)
        html_text  = html_bytes.decode("utf-8", errors="replace")

        parser = _LinkParser()
        parser.feed(html_text)

        urls: list[str] = []
        all_hrefs: list[str] = []
        seen: set[str] = set()
        seen_all: set[str] = set()

        for href, _text in parser.links:
            if not href or href.startswith("#") or href.startswith("mailto:"):
                continue
            abs_url = _absolute(href, page_url)
            if abs_url not in seen_all:
                seen_all.add(abs_url)
                all_hrefs.append(abs_url)
            if _is_download_link(href) and abs_url not in seen:
                seen.add(abs_url)
                urls.append(abs_url)

        required_project_urls = [
            _absolute(h, page_url)
            for h in _extract_required_project_hrefs(html_text)
        ]

        return urls, all_hrefs, required_project_urls

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, query: str, max_results: int = 10) -> list[tuple[str, str]]:
        """
        Search the Vault.  Returns [(title, url), …] for the top results.
        Tries the full-text search endpoint.
        """
        enc = urllib.parse.quote_plus(query)
        search_url = f"{VAULT_BASE}/search?search_api_fulltext={enc}"
        try:
            html_bytes = _http_get(search_url)
        except Exception:
            return []

        html_text = html_bytes.decode("utf-8", errors="replace")
        parser    = _LinkParser()
        parser.feed(html_text)

        results: list[tuple[str, str]] = []
        seen: set[str] = set()
        for href, text in parser.links:
            if not text:
                continue
            # Vault project links have the form /project/nwnX/...
            if "/project/" in href and href not in seen:
                abs_url = _absolute(href)
                seen.add(href)
                results.append((text.strip(), abs_url))
                if len(results) >= max_results:
                    break
        return results

    def find_hak_page(self, hak_name: str) -> Optional[str]:
        """
        Search the Vault for a hak by name.  Returns the best-guess project
        page URL, or None if nothing plausible is found.

        Search strategy (tried in order until a confident match appears):
          1. Exact name:         "ctp_common"
          2. Underscores→spaces: "ctp common"   (better Drupal tokenisation)
          3. First segment only: "ctp"           (catches package-style names)

        Matching heuristic (applied to results from each query):
          prefer any result whose title or URL contains the leading name token.
          If no confident match is found across all queries, return the first
          result from the last successful search as a last-ditch attempt.
        """
        lower      = hak_name.lower().replace("-", " ").replace("_", " ")
        first_word = lower.split()[0]   # e.g. "ctp" from "ctp_common"

        queries: list[str] = [hak_name]
        if "_" in hak_name or "-" in hak_name:
            queries.append(lower)           # "ctp common"
        if " " in lower:                    # multi-word → also try first word alone
            queries.append(first_word)      # "ctp"

        last_results: list[tuple[str, str]] = []
        for query in queries:
            results = self.search(query)
            if not results:
                continue
            last_results = results
            for title, url in results:
                t = title.lower().replace("-", " ").replace("_", " ")
                if lower in t or first_word in url.lower():
                    return url

        # Exhausted all queries; return first result from last successful search
        return last_results[0][1] if last_results else None

# ═══════════════════════════════════════════════════════════════════════════════
# Download Manager
# ═══════════════════════════════════════════════════════════════════════════════

ProgressCB = Callable[[int, int, str], None]   # (bytes_done, total_bytes, filename)


def download_file(
    url: str,
    dest_dir: Path,
    progress_cb: Optional[ProgressCB] = None,
    cancel_flag: Optional[threading.Event] = None,
) -> Path:
    """
    Download *url* into *dest_dir* and return the local Path.
    Single HTTP request: filename is derived from Content-Disposition, the
    post-redirect URL, or the original URL path — in that priority order.
    Downloads to a temp file first, then renames to the final name.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"NIT-macOS/{APP_VERSION}",
            "Referer":    VAULT_BASE,
        },
    )

    tmp_path = dest_dir / f"_nit_{threading.get_ident()}.tmp"

    with urllib.request.urlopen(req, timeout=60) as resp:
        # 1. Content-Disposition header (most reliable for redirect-based scripts)
        cd = resp.headers.get("Content-Disposition", "")
        cd_match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)',
                             cd, re.IGNORECASE)
        if cd_match:
            filename = urllib.parse.unquote(cd_match.group(1).strip().strip('"\''))
        else:
            filename = ""

        # 2. Final URL path after redirects
        if not filename or filename in ("download", "pubdlcnt.php"):
            final_path = urllib.parse.urlparse(resp.url).path
            filename = urllib.parse.unquote(Path(final_path).name or "")

        # 3. Original URL path
        if not filename or filename in ("download", "pubdlcnt.php"):
            orig_path = urllib.parse.urlparse(url).path
            filename = urllib.parse.unquote(Path(orig_path).name or "nit_download.bin")

        total = int(resp.headers.get("Content-Length", 0))
        done  = 0
        chunk = 65536

        with open(tmp_path, "wb") as out:
            while True:
                if cancel_flag and cancel_flag.is_set():
                    tmp_path.unlink(missing_ok=True)
                    raise InterruptedError("Download cancelled by user")
                buf = resp.read(chunk)
                if not buf:
                    break
                out.write(buf)
                done += len(buf)
                if progress_cb:
                    progress_cb(done, total, filename)

    dest = dest_dir / filename
    # If a file with this name already exists in tmp, remove it before rename
    dest.unlink(missing_ok=True)
    tmp_path.rename(dest)
    return dest

# ═══════════════════════════════════════════════════════════════════════════════
# Installation logic (local files)
# ═══════════════════════════════════════════════════════════════════════════════

def route_file(filename: str) -> Optional[str]:
    ext = Path(filename).suffix.lower()
    for subdir, info in NWN_DIRS.items():
        if ext in info["exts"]:
            return subdir
    return None


def install_file(src: Path, nwn_home: Path) -> tuple[bool, str]:
    subdir = route_file(src.name)
    if subdir is None:
        return False, f"Unknown file type: {src.suffix}"
    dest_dir = nwn_home / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        return False, f"Already installed: {src.name}"
    shutil.copy2(src, dest)
    return True, f"Installed {src.name} → {subdir}/"


def install_zip(zip_path: Path, nwn_home: Path) -> list[tuple[bool, str]]:
    results: list[tuple[bool, str]] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                name = Path(member.filename).name
                if not name or member.is_dir():
                    continue
                subdir = route_file(name)
                if subdir is None:
                    results.append((False, f"Skipped (unknown type): {name}"))
                    continue
                dest_dir = nwn_home / subdir
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / name
                if dest.exists():
                    results.append((False, f"Already installed: {name}"))
                    continue
                with zf.open(member) as src_f, open(dest, "wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)
                results.append((True, f"Installed {name} → {subdir}/"))
    except zipfile.BadZipFile:
        results.append((False, f"Not a valid ZIP: {zip_path.name}"))
    return results


def list_installed(nwn_home: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for subdir, info in NWN_DIRS.items():
        d = nwn_home / subdir
        if d.is_dir():
            files = sorted(
                f.name for f in d.iterdir()
                if f.is_file() and f.suffix.lower() in info["exts"]
            )
            if files:
                result[subdir] = files
    return result


def already_installed_haks(nwn_home: Path) -> set[str]:
    """Return set of hak names (lowercase, without .hak) already in hak/."""
    hak_dir = nwn_home / "hak"
    if not hak_dir.is_dir():
        return set()
    return {f.stem.lower() for f in hak_dir.iterdir() if f.suffix.lower() == ".hak"}

# ═══════════════════════════════════════════════════════════════════════════════
# Readme-based Hak Suggester  (NLP-lite)
# ═══════════════════════════════════════════════════════════════════════════════
# NWN mod archives frequently ship with a readme.txt that lists hakpak
# requirements in free-form prose ("You will need sf_haks.hak from the Vault").
# The GFF Mod_HakList is the authoritative source, but some older modules omit
# entries there while documenting them only in a readme.
#
# Strategy:
#   1. Collect any readme-like file found in a downloaded archive.
#   2. Regex-scan for "<name>.hak" occurrences.
#   3. Score the surrounding ±220 chars for "need" vs "included" keyword phrases.
#   4. Surface names that look needed but aren't already covered by GFF or the
#      currently installed hak set.
#   5. Ask the user before acting — these are *speculative* suggestions.
# ═══════════════════════════════════════════════════════════════════════════════

class ReadmeParser:
    """
    Extract hak names that appear to be externally required (not bundled) in a
    readme/notes file.  Returns results as (hak_stem, context_snippet) pairs.
    """

    # Match bare hak stems:  sf_haks  "sf_haks"  sf_haks.hak  'CEP2_hak'
    _HAK_RE = re.compile(
        r"""
        ["\']?                          # optional open quote
        ([\w][\w\-\.]{1,31})            # hak stem: word chars / hyphens / dots
        \.hak                           # literal .hak extension
        ["\']?                          # optional close quote
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Phrases that suggest the hak must be separately obtained
    _NEED_PHRASES: list[str] = [
        "download", "install", "place in", "copy to", "put in", "put into",
        "required", "requirement", "must have", "need", "needs", "you will need",
        "you'll need", "available at", "from the vault", "from neverwinter vault",
        "get from", "separately", "not included", "not bundled", "find at",
        "obtain", "grab", "also need", "also require",
    ]

    # Phrases that suggest the hak ships inside the same archive
    _INCLUDED_PHRASES: list[str] = [
        "included", "provided", "bundled", "comes with", "packed with",
        "in this download", "in this archive", "in this zip", "in this package",
        "part of this", "already included", "comes bundled", "ships with",
    ]

    # Characters of context to examine on each side of a match
    _CTX: int = 220

    @classmethod
    def extract_needed_haks(
        cls,
        text: str,
        already_known: set[str],
    ) -> list[tuple[str, str]]:
        """
        Return [(hak_stem, context_snippet), …] for haks that:
          • appear in *text* with a .hak extension
          • are not in *already_known*  (already GFF-declared or installed)
          • score as "needed" (need phrases ≥ included phrases in surrounding text)
        Results are de-duplicated; order is preserved by first occurrence.
        """
        results: list[tuple[str, str]] = []
        seen: set[str] = set()

        for m in cls._HAK_RE.finditer(text):
            stem = m.group(1).lower()
            if stem in already_known or stem in seen:
                continue

            # Extract surrounding context
            start = max(0, m.start() - cls._CTX)
            end   = min(len(text), m.end() + cls._CTX)
            ctx   = text[start:end].lower()

            need_score     = sum(1 for p in cls._NEED_PHRASES     if p in ctx)
            included_score = sum(1 for p in cls._INCLUDED_PHRASES if p in ctx)

            # If "included" wins unambiguously, skip; otherwise surface it
            # (user will be asked before anything installs)
            if included_score > need_score:
                continue

            # Build a readable snippet
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()[:180]
            seen.add(stem)
            results.append((stem, snippet))

        return results


# ═══════════════════════════════════════════════════════════════════════════════
# URL Installer — orchestrates the full fetch → parse → resolve → install flow
# ═══════════════════════════════════════════════════════════════════════════════

class UrlInstaller:
    """
    Coordinates downloading a module from a Vault URL, parsing its GFF to
    find required haks, searching the Vault for each missing hak, downloading
    and installing everything.

    Designed to run on a background thread.  All progress is reported via the
    *log_cb* and *progress_cb* callbacks (which must be thread-safe — on the
    tkinter side, wrap them with widget.after()).
    """

    def __init__(
        self,
        url:          str,
        nwn_home:     Path,
        log_cb:       Callable[[str, str], None],          # (message, level)
        progress_cb:  Callable[[int, int, str], None],      # (done, total, filename)
        cancel_flag:  threading.Event,
        prompt_cb:    Optional[Callable[[str, str], bool]] = None,  # (title, msg) → yes/no
    ):
        self.url         = url
        self.nwn_home    = nwn_home
        self.log         = log_cb
        self.on_progress = progress_cb
        self.cancel      = cancel_flag
        self._prompt     = prompt_cb   # may be None in headless / test use
        self._vault      = VaultClient()
        self._tmp        = Path(tempfile.mkdtemp(prefix="nit_"))
        # Readme files collected during archive unpacking; fed to ReadmeParser
        self._readme_files: list[Path] = []
        # Hak names (lowercase) for which find_hak_page() returned None
        self._hak_search_failed: set[str] = set()

    def run(self) -> None:
        try:
            self._run()
        finally:
            shutil.rmtree(self._tmp, ignore_errors=True)

    def _cancelled(self) -> bool:
        return self.cancel.is_set()

    def _run(self) -> None:
        # ── Step 1: fetch the project page ────────────────────────────────────
        self.log(f"Fetching project page: {self.url}", "info")
        try:
            dl_urls, all_hrefs, required_project_urls = \
                self._vault.fetch_download_urls(self.url)
        except Exception as exc:
            self.log(f"Could not fetch page: {exc}", "error")
            return

        if not dl_urls:
            self.log("No download links recognised on that page.", "warn")
            if all_hrefs:
                # Show links that contain keywords likely to be file/download related
                _keywords = ("fid=", "download", "pubdlcnt", "/file/", "files/",
                             ".zip", ".mod", ".hak", ".tlk", ".php?")
                suspects = [h for h in all_hrefs
                            if any(k in h.lower() for k in _keywords)]
                if suspects:
                    self.log(f"  Candidate links found on page ({len(suspects)}) — "
                             f"update NIT if a new format appears here:", "warn")
                    for h in suspects:
                        self.log(f"    {h}", "detail")
                else:
                    self.log(f"  Page had {len(all_hrefs)} links but none look like "
                             f"file downloads. First 20:", "warn")
                    for h in all_hrefs[:20]:
                        self.log(f"    {h}", "detail")
            else:
                self.log("  Page contained no links — may be JavaScript-rendered.", "warn")
            return

        self.log(f"Found {len(dl_urls)} download link(s):", "info")
        for u in dl_urls:
            self.log(f"  {u}", "detail")

        # ── Step 2: download the primary archive ──────────────────────────────
        mod_files:  list[Path] = []
        hak_files:  list[Path] = []
        other_files: list[Path] = []

        for dl_url in dl_urls:
            if self._cancelled():
                self.log("Cancelled.", "warn")
                return
            try:
                local = self._download(dl_url)
            except Exception as exc:
                self.log(f"Download failed ({Path(dl_url).name}): {exc}", "error")
                continue
            self._unpack_and_sort(local, mod_files, hak_files, other_files)

        # ── Step 3: parse each .mod for its hak list ──────────────────────────
        declared_haks: list[str] = []
        for mod_path in mod_files:
            info = ModuleInfo.from_file(mod_path)
            if info.error:
                self.log(f"Warning parsing {mod_path.name}: {info.error}", "warn")
            else:
                self.log(f"Module '{info.name or mod_path.stem}' declares "
                         f"{len(info.hak_list)} hak(s):", "info")
                for h in info.hak_list:
                    self.log(f"  hak: {h}", "detail")
                declared_haks.extend(info.hak_list)

        # ── Step 3b: follow 'Required projects' links ─────────────────────────
        # The Vault page itself lists dependency pages in a 'Required projects'
        # field. Following those links directly is more reliable than searching
        # by hak name, and works even when readmes don't list exact filenames.
        if required_project_urls:
            self.log(
                f"Module page links {len(required_project_urls)} required project(s):",
                "info",
            )
            for rp in required_project_urls:
                self.log(f"  {rp}", "detail")
            self._follow_required_projects(required_project_urls, hak_files, other_files)

        # ── Step 4: resolve remaining missing haks via Vault search ───────────
        installed_haks = already_installed_haks(self.nwn_home)
        # Includes haks downloaded via required-project pages in step 3b
        downloaded_hak_names = {f.stem.lower() for f in hak_files}

        missing = [
            h for h in declared_haks
            if h not in installed_haks and h not in downloaded_hak_names
        ]

        if missing:
            self.log(
                f"{len(missing)} hak(s) still not resolved — searching Vault…",
                "info",
            )
            for hak_name in missing:
                if self._cancelled():
                    self.log("Cancelled.", "warn")
                    return
                self._resolve_hak(hak_name, hak_files)
        elif declared_haks:
            self.log("All required haks are already installed.", "ok")

        # ── Step 4b: parse readme files for speculative hak deps ──────────────
        if self._readme_files:
            self.log(
                f"Found {len(self._readme_files)} readme file(s) — scanning for "
                f"additional hak references…",
                "info",
            )
            for rf in self._readme_files:
                self.log(f"  {rf.name}", "detail")
            self._resolve_readme_haks(
                declared_haks  = declared_haks,
                installed_haks = installed_haks,
                hak_files      = hak_files,
            )

        # ── Step 5: install everything ────────────────────────────────────────
        self.log("Installing files…", "info")
        all_files = mod_files + hak_files + other_files
        ok = skip = err = 0
        for fp in all_files:
            if fp.suffix.lower() == ".zip":
                for success, msg in install_zip(fp, self.nwn_home):
                    self.log(f"  {msg}", "ok" if success else "warn")
                    if success: ok += 1
                    else: skip += 1
            else:
                success, msg = install_file(fp, self.nwn_home)
                self.log(f"  {msg}", "ok" if success else "warn")
                if success: ok += 1
                else: skip += 1

        self.log(
            f"Done — {ok} installed, {skip} skipped/already-present, {err} errors.",
            "ok" if ok > 0 else "warn",
        )

        # ── Step 6: verify GFF-declared haks are present by exact name ────────
        if declared_haks:
            self._verify_haks(declared_haks)

    def _download(self, url: str) -> Path:
        # Build a readable display name — pubdlcnt.php URLs have no useful path segment
        parsed = urllib.parse.urlparse(url)
        display = Path(parsed.path).name or parsed.query or url
        self.log(f"Downloading {display}…", "info")
        return download_file(url, self._tmp, self.on_progress, self.cancel)

    def _unpack_and_sort(
        self,
        local: Path,
        mod_files:   list[Path],
        hak_files:   list[Path],
        other_files: list[Path],
    ) -> None:
        """Unpack archive (zip / rar / 7z / exe) then sort files by NWN type."""
        ext = local.suffix.lower()
        if ext == ".zip":
            unpack_dir = self._tmp / local.stem
            unpack_dir.mkdir(exist_ok=True)
            try:
                with zipfile.ZipFile(local, "r") as zf:
                    zf.extractall(unpack_dir)
                for f in unpack_dir.rglob("*"):
                    if f.is_file():
                        self._sort_file(f, mod_files, hak_files, other_files)
            except zipfile.BadZipFile:
                # Server sometimes returns a non-zip despite the .zip name
                self.log(f"  {local.name} is not a valid ZIP — trying unar…", "warn")
                self._unpack_with_unar(local, mod_files, hak_files, other_files)
        elif ext in {".rar", ".7z", ".exe", ".bin"}:
            self._unpack_with_unar(local, mod_files, hak_files, other_files)
        else:
            self._sort_file(local, mod_files, hak_files, other_files)

    def _unpack_with_unar(
        self,
        archive: Path,
        mod_files:   list[Path],
        hak_files:   list[Path],
        other_files: list[Path],
    ) -> None:
        """
        Extract any archive format using the `unar` CLI (brew install unar).
        unar handles .rar, .7z, .zip, .exe self-extractors, and more.
        """
        unar = shutil.which("unar")
        if not unar:
            self.log(f"  Cannot extract {archive.name}: `unar` not installed.", "error")
            self.log(f"    Fix: brew install unar   (then retry)", "error")
            return

        out_dir = self._tmp / f"unar_{archive.stem}"
        out_dir.mkdir(exist_ok=True)
        try:
            result = subprocess.run(
                [unar, "-output-directory", str(out_dir),
                 "-force-overwrite", str(archive)],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                self.log(f"  unar failed on {archive.name}: "
                         f"{result.stderr.strip() or result.stdout.strip()}", "error")
                return
            extracted = list(out_dir.rglob("*"))
            nwn_found = 0
            for f in extracted:
                if f.is_file():
                    before = len(mod_files) + len(hak_files) + len(other_files)
                    self._sort_file(f, mod_files, hak_files, other_files)
                    if len(mod_files) + len(hak_files) + len(other_files) > before:
                        nwn_found += 1
            if nwn_found == 0 and extracted:
                self.log(f"  unar extracted {len(extracted)} file(s) from "
                         f"{archive.name} but none are recognised NWN types.", "warn")
                for f in extracted:
                    if f.is_file():
                        self.log(f"    {f.name}", "detail")
        except subprocess.TimeoutExpired:
            self.log(f"  unar timed out extracting {archive.name}", "error")
        except Exception as exc:
            self.log(f"  unar error: {exc}", "error")

    def _sort_file(
        self, f: Path,
        mod_files: list[Path], hak_files: list[Path], other_files: list[Path],
    ) -> None:
        e = f.suffix.lower()
        if e == ".mod":
            mod_files.append(f)
        elif e == ".hak":
            hak_files.append(f)
        elif e in {".tlk", ".erf", ".2da", ".nss", ".ncs", ".bmu", ".tga"}:
            other_files.append(f)
        elif e in {".txt", ".rtf", ".nfo", ".md", ".htm", ".html"}:
            # Collect readme-like text files for speculative dep detection.
            # .nfo is almost always an info/readme in NWN distributions.
            # For other extensions require "read" to appear in the stem.
            stem_low = f.stem.lower()
            if e == ".nfo" or "read" in stem_low:
                self._readme_files.append(f)
        elif e in {".pdf", ".jpg", ".jpeg", ".png", ".bmp",
                   ".doc", ".docx", ".url", ".lnk"}:
            pass  # documentation / shortcuts — ignore silently
        else:
            self.log(f"  Skipped unrecognised file: {f.name} ({e})", "detail")

    def _follow_required_projects(
        self,
        required_urls: list[str],
        hak_files:     list[Path],
        other_files:   list[Path],
    ) -> None:
        """
        Visit each 'Required projects' page and download its hak archives.

        This is the primary dependency resolution step: the Vault page itself
        links to exactly the pages that host the needed haks, so following
        those links is more reliable than searching by hak name.
        """
        for rp_url in required_urls:
            if self._cancelled():
                return
            self.log(f"  → Required project: {rp_url}", "info")
            try:
                dl_urls, _, _ = self._vault.fetch_download_urls(rp_url)
            except Exception as exc:
                self.log(f"    Could not fetch page: {exc}", "error")
                continue

            if not dl_urls:
                self.log(f"    No download links found on that page.", "warn")
                continue

            hak_count_before = len(hak_files)
            for dl_url in dl_urls:
                if self._cancelled():
                    return
                try:
                    local = self._download(dl_url)
                    dummy_mod: list[Path] = []
                    self._unpack_and_sort(local, dummy_mod, hak_files, other_files)
                except Exception as exc:
                    self.log(f"    Download failed: {exc}", "error")

            new_haks = [f.stem.lower() for f in hak_files[hak_count_before:]]
            if new_haks:
                self.log(
                    f"    Got {len(new_haks)} hak(s): {', '.join(new_haks)}",
                    "ok",
                )
            else:
                self.log(f"    No new haks extracted from that page.", "detail")

    def _resolve_hak(self, hak_name: str, hak_files: list[Path]) -> None:
        """Search Vault for hak_name, download it, add to hak_files."""
        self.log(f"Searching Vault for hak: {hak_name}…", "info")
        try:
            page_url = self._vault.find_hak_page(hak_name)
        except Exception as exc:
            self.log(f"  Search failed for '{hak_name}': {exc}", "error")
            return

        if not page_url:
            self._hak_search_failed.add(hak_name.lower())
            self.log(f"  '{hak_name}' not found on Vault — you will need to download it manually.", "warn")
            return

        self.log(f"  Found page: {page_url}", "detail")

        try:
            dl_urls, _, _ = self._vault.fetch_download_urls(page_url)
        except Exception as exc:
            self.log(f"  Could not fetch hak page: {exc}", "error")
            return

        if not dl_urls:
            self.log(f"  No download links on hak page for '{hak_name}'.", "warn")
            return

        # All Vault links are now pubdlcnt.php?fid=NNN — none end with .hak or .zip,
        # so just take the first recognised download link.
        chosen = dl_urls[0]
        self.log(f"  Downloading from: {chosen}", "detail")

        try:
            local = self._download(chosen)
            # Route through the full unpack-and-sort so .rar/.7z/.exe all work
            dummy_mod: list[Path] = []
            dummy_other: list[Path] = []
            self._unpack_and_sort(local, dummy_mod, hak_files, dummy_other)
            if dummy_mod:
                self.log(f"  Note: hak archive also contained .mod file(s): "
                         f"{[f.name for f in dummy_mod]}", "warn")
        except Exception as exc:
            self.log(f"  Download failed for '{hak_name}': {exc}", "error")

    def _confirm_one_readme_hak(self, stem: str, snippet: str) -> bool:
        """
        Show a per-hak yes/no confirmation dialog quoting the exact readme text
        that triggered the suggestion.

        This is intentionally one-at-a-time so that a malicious or misleading
        readme cannot sneak through multiple downloads under a single bulk approval.
        The user sees the raw context from the file and decides for themselves.

        Safe to call from the background installer thread (schedules on main thread).
        Defaults to False (decline) when running headless — never auto-installs
        speculative deps without explicit human confirmation.
        """
        if self._prompt is None:
            return False   # headless / test mode — decline by default (safe)

        title = f"Readme mentions: {stem}.hak"
        msg   = (
            f"A readme file references  '{stem}.hak'.\n\n"
            f"Exact text from the readme:\n"
            f"────────────────────────────\n"
            f"{snippet}\n"
            f"────────────────────────────\n\n"
            f"⚠  NIT is reading free-form text — this may be inaccurate or\n"
            f"   intentionally misleading.  Only click Yes if you recognise\n"
            f"   this hak and trust the source.\n\n"
            f"Search the Vault for  '{stem}.hak'  and install it?"
        )
        return self._prompt(title, msg)

    def _resolve_readme_haks(
        self,
        declared_haks:        list[str],
        installed_haks:       set[str],
        hak_files:            list[Path],
        _processed_readmes:   Optional[set[str]] = None,
        depth:                int = 0,
        max_depth:            int = 2,
    ) -> None:
        """
        Parse any readme files collected so far and offer to install haks they
        mention that aren't already covered by GFF / existing installs.

        Calls itself recursively (up to *max_depth*) if resolving a speculative
        hak downloads an archive containing its own readme with further deps.
        """
        if depth > max_depth:
            return

        if _processed_readmes is None:
            _processed_readmes = set()

        # Only process readmes we haven't seen yet
        new_readmes = [r for r in self._readme_files
                       if str(r) not in _processed_readmes]
        if not new_readmes:
            return

        for r in new_readmes:
            _processed_readmes.add(str(r))

        # Build the "already known" set fresh so we respect haks fetched by
        # earlier iterations of this recursive call
        already_known: set[str] = (
            set(h.lower() for h in declared_haks)
            | installed_haks
            | {f.stem.lower() for f in hak_files}
        )

        speculative: list[tuple[str, str]] = []
        for readme in new_readmes:
            try:
                text = readme.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                self.log(f"  Could not read {readme.name}: {exc}", "warn")
                continue
            found = ReadmeParser.extract_needed_haks(text, already_known)
            if found:
                for stem, snippet in found:
                    if stem not in {s for s, _ in speculative}:
                        speculative.append((stem, snippet))
            else:
                # Tell the user we read the file but found nothing actionable
                hak_count_in_text = len(re.findall(r'\.hak\b', text, re.IGNORECASE))
                if hak_count_in_text:
                    self.log(
                        f"  {readme.name}: {hak_count_in_text} .hak mention(s) found "
                        f"but all are already installed or scored as 'bundled'.",
                        "detail",
                    )
                else:
                    self.log(
                        f"  {readme.name}: no .hak filenames mentioned — "
                        f"check it manually if haks are still missing.",
                        "detail",
                    )

        if not speculative:
            self.log("  No new speculative hak dependencies found in readme(s).", "detail")
            return

        depth_tag = f"  (depth {depth})" if depth > 0 else ""
        self.log(
            f"── Readme parser{depth_tag}: {len(speculative)} speculative hak(s) found ──",
            "info",
        )
        self.log(
            f"  Each will require individual confirmation before any download begins.",
            "warn",
        )
        for stem, snippet in speculative:
            self.log(f"  readme-suggested: {stem}.hak", "warn")
            self.log(f"    «{snippet}»", "detail")

        hak_count_before = len(hak_files)

        for stem, snippet in speculative:
            if self._cancelled():
                self.log("Cancelled.", "warn")
                return

            # ── Per-hak consent: user sees the exact readme text each time ──────
            if not self._confirm_one_readme_hak(stem, snippet):
                self.log(f"  Skipped (declined by user): {stem}.hak", "warn")
                continue

            self.log(f"  (readme-suggested) Resolving: {stem}…", "info")
            self._resolve_hak(stem, hak_files)

        # If new haks were downloaded they may have shipped with their own
        # readmes (collected by _sort_file during _unpack_and_sort).  Recurse.
        if len(hak_files) > hak_count_before and depth < max_depth:
            self._resolve_readme_haks(
                declared_haks     = declared_haks,
                installed_haks    = installed_haks,
                hak_files         = hak_files,
                _processed_readmes = _processed_readmes,
                depth             = depth + 1,
                max_depth         = max_depth,
            )

    def _verify_haks(self, declared_haks: list[str]) -> None:
        """
        Cross-check the GFF-declared hak list against what is actually present
        in the NWN hak/ directory, matching by exact stem name (case-insensitive).

        NWN requires exact filename matches — a hak downloaded as 'sf_haks_v2.hak'
        will NOT satisfy a module that declares 'sf_haks', so we surface any gaps
        clearly rather than letting the user discover them inside the game.
        """
        hak_dir = self.nwn_home / "hak"
        present = {f.stem.lower() for f in hak_dir.iterdir()
                   if f.is_file() and f.suffix.lower() == ".hak"} if hak_dir.is_dir() else set()

        ok_haks      = [h for h in declared_haks if h.lower() in present]
        missing_haks = [h for h in declared_haks if h.lower() not in present]

        self.log("── Hak verification ──────────────────────────────", "info")
        for h in ok_haks:
            self.log(f"  ✓  {h}.hak", "ok")

        if missing_haks:
            self.log(f"  {len(missing_haks)} hak(s) still missing after install:", "error")
            for h in missing_haks:
                self.log(f"  ✗  {h}.hak  ← NWN will refuse to load the module", "error")
                if h.lower() in self._hak_search_failed:
                    self.log(
                        f"     ↳ Vault search found no results for '{h}'.",
                        "warn",
                    )
                    self.log(
                        f"       Try searching the Vault manually: "
                        f"https://neverwintervault.org/search?search_api_fulltext="
                        f"{urllib.parse.quote_plus(h)}",
                        "warn",
                    )
                else:
                    self.log(
                        f"     ↳ A Vault page was found but the downloaded file may "
                        f"have a different name.",
                        "warn",
                    )
                    self.log(
                        f"       Check your hak/ folder and rename the file to "
                        f"exactly '{h}.hak'.",
                        "warn",
                    )
        else:
            self.log("  All required haks confirmed present ✓", "ok")

# ═══════════════════════════════════════════════════════════════════════════════
# GUI
# ═══════════════════════════════════════════════════════════════════════════════

class NitApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.nwn_home: Optional[Path] = None

        self.title(APP_NAME)
        self.geometry("960x720")
        self.minsize(780, 540)
        self.configure(bg=BG)

        self._apply_style()
        self._build_ui()
        self._load_nwn_path()

    # ── Theming ──────────────────────────────────────────────────────────────

    def _apply_style(self):
        s = ttk.Style(self)
        s.theme_use("default")
        s.configure(".",              background=BG,  foreground=FG, font=("SF Pro Text", 13))
        s.configure("TFrame",         background=BG)
        s.configure("TLabel",         background=BG,  foreground=FG)
        s.configure("TButton",        background=BG2, foreground=FG, padding=(8, 4))
        s.map("TButton",              background=[("active", BG3)])
        s.configure("Accent.TButton", background=ACCENT, foreground=BG,
                    font=("SF Pro Text", 13, "bold"))
        s.map("Accent.TButton",       background=[("active", "#74c7ec")])
        s.configure("TEntry",         fieldbackground=BG2, foreground=FG,
                    insertcolor=FG, bordercolor=BG3)
        s.configure("TNotebook",      background=BG, tabmargins=[2, 4, 2, 0])
        s.configure("TNotebook.Tab",  background=BG2, foreground=FG, padding=[12, 6])
        s.map("TNotebook.Tab",        background=[("selected", BG3)],
              foreground=[("selected", ACCENT)])
        s.configure("Treeview",       background=BG2, foreground=FG,
                    fieldbackground=BG2, rowheight=24)
        s.configure("Treeview.Heading", background=BG3, foreground=ACCENT,
                    font=("SF Pro Text", 12, "bold"))
        s.map("Treeview",             background=[("selected", ACCENT)],
              foreground=[("selected", BG)])
        s.configure("TScrollbar",     background=BG3, troughcolor=BG2, arrowcolor=FG)
        s.configure("TLabelframe",    background=BG, foreground=MAUVE)
        s.configure("TLabelframe.Label", background=BG, foreground=MAUVE,
                    font=("SF Pro Text", 12, "bold"))
        s.configure("TProgressbar",   troughcolor=BG2, background=ACCENT)

    # ── Top-level layout ─────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        header = ttk.Frame(self)
        header.pack(fill="x", padx=16, pady=(14, 0))
        ttk.Label(header, text="NIT", font=("SF Pro Text", 24, "bold"),
                  foreground=ACCENT).pack(side="left")
        ttk.Label(header, text="  Neverwinter Nights Installer Tool",
                  font=("SF Pro Text", 14)).pack(side="left", pady=4)
        ttk.Label(header, text=f"v{APP_VERSION}", foreground=BG3,
                  font=("SF Pro Text", 11)).pack(side="right", pady=6)

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=16, pady=8)

        # NWN path row
        path_frame = ttk.LabelFrame(self, text="  NWN Installation Path", padding=10)
        path_frame.pack(fill="x", padx=16, pady=(0, 8))
        self.path_var = tk.StringVar()
        ttk.Entry(path_frame, textvariable=self.path_var, width=55).pack(
            side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(path_frame, text="Browse…",    command=self._browse_nwn).pack(side="left", padx=(0,6))
        ttk.Button(path_frame, text="Auto-detect", command=self._auto_detect).pack(side="left", padx=(0,6))
        ttk.Button(path_frame, text="Save", style="Accent.TButton",
                   command=self._save_path).pack(side="left")

        # Notebook
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        self._build_url_tab()
        self._build_install_tab()
        self._build_library_tab()
        self._build_about_tab()

        # Status bar
        self.status_var = tk.StringVar(value="Ready.")
        self._status_bar = tk.Label(self, textvariable=self.status_var,
                                    bg=BG2, fg=FG, anchor="w",
                                    font=("SF Pro Text", 11), pady=4, padx=10)
        self._status_bar.pack(fill="x", side="bottom")

    # ── URL Install Tab ───────────────────────────────────────────────────────

    def _build_url_tab(self):
        tab = ttk.Frame(self.nb, padding=12)
        self.nb.add(tab, text="  Install from URL  ")

        # URL entry
        url_frame = ttk.LabelFrame(tab, text="  Neverwinter Vault Project URL", padding=10)
        url_frame.pack(fill="x", pady=(0, 10))

        self.url_var = tk.StringVar()
        ttk.Entry(url_frame, textvariable=self.url_var, width=60).pack(
            side="left", fill="x", expand=True, padx=(0, 8))

        self._retrieve_btn = ttk.Button(url_frame, text="Retrieve & Install",
                                        style="Accent.TButton",
                                        command=self._start_url_install)
        self._retrieve_btn.pack(side="left", padx=(0, 6))

        self._cancel_btn = ttk.Button(url_frame, text="Cancel",
                                      command=self._cancel_url_install,
                                      state="disabled")
        self._cancel_btn.pack(side="left")

        # Progress bar
        self._url_progress = ttk.Progressbar(tab, mode="indeterminate", length=400)
        self._url_progress.pack(fill="x", pady=(0, 8))

        # How it works
        help_frame = ttk.LabelFrame(tab, text="  How it works", padding=8)
        help_frame.pack(fill="x", pady=(0, 8))
        help_text = (
            "1. Paste a Vault project URL (e.g. https://neverwintervault.org/project/nwn1/module/…)\n"
            "2. NIT fetches the page, finds download links, and pulls the archive\n"
            "3. If a .mod is found, its GFF binary is parsed to extract the declared hak list\n"
            "4. Each missing hak is searched on the Vault and downloaded automatically\n"
            "5. Everything is installed to your NWN home directory"
        )
        tk.Label(help_frame, text=help_text, bg=BG, fg=FG, justify="left",
                 font=("SF Pro Text", 11)).pack(anchor="w")

        # Log area
        log_frame = ttk.LabelFrame(tab, text="  Install Log", padding=6)
        log_frame.pack(fill="both", expand=True)

        self._log_text = tk.Text(log_frame, bg=BG2, fg=FG, wrap="word",
                                 font=("SF Mono", 11), state="disabled",
                                 relief="flat", padx=6, pady=6)
        log_vsb = ttk.Scrollbar(log_frame, orient="vertical",
                                 command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=log_vsb.set)
        self._log_text.pack(side="left", fill="both", expand=True)
        log_vsb.pack(side="right", fill="y")

        # Tag colours for log levels
        self._log_text.tag_configure("info",   foreground=FG)
        self._log_text.tag_configure("ok",     foreground=GREEN)
        self._log_text.tag_configure("warn",   foreground=YELLOW)
        self._log_text.tag_configure("error",  foreground=RED)
        self._log_text.tag_configure("detail", foreground=BG3)

        self._url_cancel_flag: Optional[threading.Event] = None

    def _log_append(self, msg: str, level: str = "info") -> None:
        """Append a line to the log widget.  Must be called from the main thread."""
        self._log_text.configure(state="normal")
        self._log_text.insert("end", msg + "\n", level)
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _log_from_thread(self, msg: str, level: str = "info") -> None:
        """Thread-safe log append (schedules on main thread via after)."""
        self.after(0, lambda m=msg, lv=level: self._log_append(m, lv))

    def _progress_from_thread(self, done: int, total: int, filename: str) -> None:
        """Thread-safe progress update."""
        if total:
            pct = done * 100 // total
            self.after(0, lambda: self.status_var.set(
                f"  Downloading {filename}: {pct}% ({self._fmt_size(done)} / {self._fmt_size(total)})"
            ))

    def _start_url_install(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("No URL", "Paste a Neverwinter Vault project URL first.")
            return
        if not self.nwn_home:
            messagebox.showwarning("No NWN Path", "Set and save your NWN path first.")
            return

        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

        self._url_cancel_flag = threading.Event()
        self._retrieve_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        self._url_progress.start(12)

        installer = UrlInstaller(
            url          = url,
            nwn_home     = self.nwn_home,
            log_cb       = self._log_from_thread,
            progress_cb  = self._progress_from_thread,
            cancel_flag  = self._url_cancel_flag,
            prompt_cb    = self._make_prompt_cb(),
        )

        def worker():
            installer.run()
            self.after(0, self._url_install_done)

        threading.Thread(target=worker, daemon=True).start()

    def _url_install_done(self) -> None:
        self._url_progress.stop()
        self._retrieve_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")
        self._set_status("URL install complete.", "ok")
        self._refresh_library()

    def _cancel_url_install(self) -> None:
        if self._url_cancel_flag:
            self._url_cancel_flag.set()
            self._log_append("Cancellation requested…", "warn")
            self._cancel_btn.configure(state="disabled")

    def _make_prompt_cb(self) -> Callable[[str, str], bool]:
        """
        Return a callback that shows a yes/no dialog on the main tkinter thread
        and returns the user's choice.  Safe to call from a background thread.
        """
        root = self   # NitApp IS the tk.Tk root

        def prompt(title: str, message: str) -> bool:
            result_holder: list[bool] = [False]
            done = threading.Event()

            def ask() -> None:
                result_holder[0] = messagebox.askyesno(title, message)
                done.set()

            root.after(0, ask)
            done.wait(timeout=600)   # 10-minute timeout for very slow readers
            return result_holder[0]

        return prompt

    # ── Manual Install Tab ────────────────────────────────────────────────────

    def _build_install_tab(self):
        tab = ttk.Frame(self.nb, padding=12)
        self.nb.add(tab, text="  Install Files  ")

        top = ttk.LabelFrame(tab, text="  Select Files to Install", padding=10)
        top.pack(fill="x", pady=(0, 10))
        btn_row = ttk.Frame(top)
        btn_row.pack(fill="x")

        ttk.Button(btn_row, text="+ Module (.mod)",
                   command=lambda: self._pick_files([("NWN Module", "*.mod")])).pack(side="left", padx=(0,8))
        ttk.Button(btn_row, text="+ Hakpak (.hak)",
                   command=lambda: self._pick_files([("NWN Hakpak", "*.hak")])).pack(side="left", padx=(0,8))
        ttk.Button(btn_row, text="+ ZIP Archive",
                   command=lambda: self._pick_files([("ZIP Archive", "*.zip")])).pack(side="left", padx=(0,8))
        ttk.Button(btn_row, text="+ Any NWN File",
                   command=lambda: self._pick_files([
                       ("All NWN Files", "*.mod *.hak *.tlk *.erf *.zip *.2da *.bmu"),
                       ("All Files", "*"),
                   ])).pack(side="left")

        mid = ttk.LabelFrame(tab, text="  Install Queue", padding=6)
        mid.pack(fill="both", expand=True, pady=(0, 10))

        cols = ("file", "type", "status")
        self.queue_tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        self.queue_tree.heading("file",   text="Filename")
        self.queue_tree.heading("type",   text="Type")
        self.queue_tree.heading("status", text="Status")
        self.queue_tree.column("file",   width=360)
        self.queue_tree.column("type",   width=120)
        self.queue_tree.column("status", width=260)
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.queue_tree.yview)
        self.queue_tree.configure(yscrollcommand=vsb.set)
        self.queue_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        btn_row2 = ttk.Frame(tab)
        btn_row2.pack(fill="x")
        ttk.Button(btn_row2, text="Remove Selected", command=self._remove_from_queue).pack(side="left", padx=(0,8))
        ttk.Button(btn_row2, text="Clear Queue",     command=self._clear_queue).pack(side="left", padx=(0,8))
        ttk.Button(btn_row2, text="Install All", style="Accent.TButton",
                   command=self._install_all).pack(side="right")

    # ── Library Tab ──────────────────────────────────────────────────────────

    def _build_library_tab(self):
        tab = ttk.Frame(self.nb, padding=12)
        self.nb.add(tab, text="  Installed Library  ")

        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Button(top, text="Refresh",         command=self._refresh_library).pack(side="left", padx=(0,8))
        ttk.Button(top, text="Remove Selected", command=self._uninstall_selected).pack(side="left")
        ttk.Label(top, text="Filter:").pack(side="left", padx=(16, 4))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._apply_filter())
        ttk.Entry(top, textvariable=self.filter_var, width=22).pack(side="left")

        tree_frame = ttk.Frame(tab)
        tree_frame.pack(fill="both", expand=True)

        cols2 = ("name", "category", "size")
        self.lib_tree = ttk.Treeview(tree_frame, columns=cols2, show="headings", selectmode="extended")
        self.lib_tree.heading("name",     text="Filename")
        self.lib_tree.heading("category", text="Category")
        self.lib_tree.heading("size",     text="Size")
        self.lib_tree.column("name",     width=440)
        self.lib_tree.column("category", width=140)
        self.lib_tree.column("size",     width=90, anchor="e")
        vsb2 = ttk.Scrollbar(tree_frame, orient="vertical", command=self.lib_tree.yview)
        self.lib_tree.configure(yscrollcommand=vsb2.set)
        self.lib_tree.pack(side="left", fill="both", expand=True)
        vsb2.pack(side="right", fill="y")
        self._lib_all_rows: list[tuple[str, str, str]] = []

    # ── About Tab ────────────────────────────────────────────────────────────

    def _build_about_tab(self):
        tab = ttk.Frame(self.nb, padding=24)
        self.nb.add(tab, text="  About  ")
        ttk.Label(tab, text="NIT — Neverwinter Nights Installer Tool",
                  font=("SF Pro Text", 18, "bold"), foreground=ACCENT).pack(anchor="w")
        ttk.Label(tab, text=f"macOS Edition  •  v{APP_VERSION}", foreground=BG3).pack(anchor="w", pady=(2,14))

        body = (
            "Install modules directly from the Neverwinter Vault by URL.\n\n"
            "GFF dependency resolver: opens the .mod as an ERF container, reads\n"
            "module.ifo, and extracts the Mod_HakList — the ground-truth hakpak list.\n"
            "Each missing hak is searched on the Vault and downloaded automatically.\n\n"
            "Readme heuristic (v1.2): if a readme-like file ships in the archive,\n"
            "NIT scans it for .hak references and offers to install any that look\n"
            "required but aren't already covered by the GFF list.  You'll be asked\n"
            "before any speculative downloads happen.\n\n"
            "Supported file types:  .mod  .hak  .tlk  .erf  .2da  .nss  .bmu  .zip\n"
            "Archive formats:       .zip  .rar  .7z  .exe  (needs: brew install unar)\n\n"
            "To install a module from a URL:\n"
            "  1. Set your NWN path above and click Save\n"
            "  2. Go to 'Install from URL', paste the Vault page URL, click Retrieve\n\n"
            "To install local files:\n"
            "  Use the 'Install Files' tab to build a queue and install in one click."
        )
        text = tk.Text(tab, bg=BG2, fg=FG, wrap="word", relief="flat",
                       font=("SF Pro Text", 12), padx=10, pady=10,
                       height=16, borderwidth=0)
        text.insert("1.0", body)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, pady=(0, 14))

        link_row = ttk.Frame(tab)
        link_row.pack(anchor="w")
        ttk.Label(link_row, text="Resources:", foreground=MAUVE,
                  font=("SF Pro Text", 12, "bold")).pack(side="left", padx=(0, 8))
        for label, url in [
            ("Neverwinter Vault",  "https://neverwintervault.org"),
            ("Beamdog Forums",     "https://forums.beamdog.com/categories/neverwinter-nights-enhanced-edition"),
        ]:
            btn = tk.Label(link_row, text=label, fg=ACCENT, bg=BG, cursor="hand2",
                           font=("SF Pro Text", 12, "underline"))
            btn.pack(side="left", padx=(0, 14))
            btn.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))

    # ── Path helpers ─────────────────────────────────────────────────────────

    def _load_nwn_path(self):
        saved = self.cfg.get("nwn_home")
        if saved and Path(saved).is_dir():
            self.path_var.set(saved)
            self.nwn_home = Path(saved)
            self._set_status(f"NWN home: {saved}", "ok")
            self._refresh_library()
        else:
            detected = auto_detect_nwn()
            if detected:
                self.path_var.set(str(detected))
                self._set_status("Auto-detected NWN path — click Save to confirm.", "warn")
            else:
                self._set_status("NWN installation not found. Set the path above.", "warn")

    def _browse_nwn(self):
        path = filedialog.askdirectory(title="Select Neverwinter Nights folder",
                                       initialdir=str(Path.home() / "Documents"))
        if path:
            self.path_var.set(path)

    def _auto_detect(self):
        d = auto_detect_nwn()
        if d:
            self.path_var.set(str(d))
            self._set_status(f"Found: {d}", "ok")
        else:
            messagebox.showinfo("Not Found",
                "Could not auto-detect NWN.\nCommon locations:\n"
                "  ~/Documents/Neverwinter Nights\n"
                "  ~/Library/Application Support/Steam/…")

    def _save_path(self):
        raw = self.path_var.get().strip()
        if not raw:
            messagebox.showwarning("No Path", "Enter the NWN installation path.")
            return
        p = Path(raw)
        if not p.is_dir():
            if messagebox.askyesno("Create?", f"{p} does not exist. Create it?"):
                p.mkdir(parents=True, exist_ok=True)
            else:
                return
        self.nwn_home = p
        self.cfg["nwn_home"] = str(p)
        save_config(self.cfg)
        self._set_status(f"NWN home saved: {p}", "ok")
        self._refresh_library()

    # ── Manual install queue ──────────────────────────────────────────────────

    def _pick_files(self, filetypes):
        paths = filedialog.askopenfilenames(title="Select NWN files", filetypes=filetypes)
        for p in paths:
            self._add_to_queue(Path(p))

    def _add_to_queue(self, p: Path):
        for iid in self.queue_tree.get_children():
            if self.queue_tree.item(iid, "values")[0] == p.name:
                self._set_status(f"Already in queue: {p.name}", "warn")
                return
        ext = p.suffix.lower()
        file_type = "ZIP Archive" if ext == ".zip" else (
            NWN_DIRS[route_file(p.name)]["label"] if route_file(p.name) else "Unknown"
        )
        self.queue_tree.insert("", "end", iid=str(p),
                               values=(p.name, file_type, "Pending"))

    def _remove_from_queue(self):
        for iid in self.queue_tree.selection():
            self.queue_tree.delete(iid)

    def _clear_queue(self):
        for iid in self.queue_tree.get_children():
            self.queue_tree.delete(iid)

    def _install_all(self):
        if not self.nwn_home:
            messagebox.showwarning("No NWN Path", "Set and save your NWN path first.")
            return
        items = self.queue_tree.get_children()
        if not items:
            messagebox.showinfo("Empty Queue", "No files in the queue.")
            return
        ok = skip = err = 0
        for iid in items:
            src = Path(iid)
            if not src.exists():
                self._update_queue_row(iid, "File not found", RED)
                err += 1
                continue
            if src.suffix.lower() == ".zip":
                results = install_zip(src, self.nwn_home)
                ok   += sum(1 for s, _ in results if s)
                skip += sum(1 for s, _ in results if not s)
                self._update_queue_row(iid,
                    f"Installed {sum(1 for s,_ in results if s)} file(s)", GREEN)
            else:
                success, msg = install_file(src, self.nwn_home)
                self._update_queue_row(iid, msg, GREEN if success else YELLOW)
                if success: ok += 1
                else: skip += 1
        self._set_status(f"Done: {ok} installed, {skip} skipped, {err} errors.",
                         "ok" if not err else "warn")
        self._refresh_library()

    def _update_queue_row(self, iid, status, colour):
        vals = list(self.queue_tree.item(iid, "values"))
        vals[2] = status
        tag = f"c_{colour}"
        self.queue_tree.item(iid, values=vals, tags=(tag,))
        self.queue_tree.tag_configure(tag, foreground=colour)

    # ── Library ───────────────────────────────────────────────────────────────

    def _refresh_library(self):
        self.lib_tree.delete(*self.lib_tree.get_children())
        self._lib_all_rows = []
        if not self.nwn_home or not self.nwn_home.is_dir():
            return
        for subdir, files in list_installed(self.nwn_home).items():
            label = NWN_DIRS[subdir]["label"]
            for fname in files:
                try:
                    size = self._fmt_size((self.nwn_home / subdir / fname).stat().st_size)
                except OSError:
                    size = "?"
                self._lib_all_rows.append((fname, label, size))
        self._apply_filter()

    def _apply_filter(self):
        term = self.filter_var.get().strip().lower()
        self.lib_tree.delete(*self.lib_tree.get_children())
        for fname, label, size in self._lib_all_rows:
            if term and term not in fname.lower() and term not in label.lower():
                continue
            self.lib_tree.insert("", "end", values=(fname, label, size))

    def _uninstall_selected(self):
        sel = self.lib_tree.selection()
        if not sel:
            messagebox.showinfo("No Selection", "Select files to remove.")
            return
        names = [self.lib_tree.item(i, "values")[0] for i in sel]
        if not messagebox.askyesno("Confirm", f"Remove {len(names)} file(s)?"):
            return
        removed = 0
        for iid in sel:
            fname, label = self.lib_tree.item(iid, "values")[:2]
            for subdir, info in NWN_DIRS.items():
                if info["label"] == label:
                    t = self.nwn_home / subdir / fname
                    if t.exists():
                        t.unlink()
                        removed += 1
                    break
        self._set_status(f"Removed {removed} file(s).", "ok")
        self._refresh_library()

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    def _set_status(self, msg: str, level: str = "info") -> None:
        colours = {"ok": GREEN, "warn": YELLOW, "error": RED, "info": FG}
        self.status_var.set(f"  {msg}")
        self._status_bar.configure(fg=colours.get(level, FG))

# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("tkinter is not available.\n"
              "Install it with: brew install python-tk\n"
              "Then re-run: python3 nit_macos.py")
        sys.exit(1)
    NitApp().mainloop()

if __name__ == "__main__":
    main()
