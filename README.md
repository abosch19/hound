# Hound

**English** · [Español](README.es.md) · [Català](README.ca.md)

Semantic search for the shared folder on a Windows Server or a Linux (Samba) server, queried from **Claude Code** over **MCP**.

Hound indexes the files in the shared folder so you can find them by meaning ("the contract with the cleaning supplier", "Pérez's March invoice", "the renovation quote"), not just by name. Everything runs on the server:

- **Nothing leaves the server while indexing.** Text extraction and embeddings run locally, on the CPU.
- **Every search respects the file permissions of the person asking** (NTFS ACLs on Windows; owner, group, mode bits and POSIX ACLs on Linux). If someone can't open a file on disk, they don't see its name or any excerpt from it.
- **It never modifies the shared folder.** It only reads it.

```
 Employee's computer (VPN)                        Windows Server / Linux
┌────────────────────────┐   HTTP + token   ┌──────────────────────────────────────────┐
│ Claude Code            │ ───────────────► │ "Hound" service                           │
│  └─ MCP "hound"        │     /mcp         │  ├─ MCP server (search + permissions)     │
└────────────────────────┘                  │  └─ Indexer (low priority)                │
                                            │        │ reads          │ writes           │
                                            │   D:\Shared        SQLite index           │
                                            └──────────────────────────────────────────┘
```

## What it indexes

| Type | Extensions | What is extracted |
|---|---|---|
| PDF | `.pdf` | Text per page; **OCR** of scanned pages |
| Images | `.jpg .png .tif .bmp .webp` | Text via **OCR** (photographed documents, scans, signs) |
| AutoCAD | `.dxf`, `.dwg` | Text, block attributes (title blocks), dimensions and layers. DWG files need [ODA File Converter](#autocad-dwg-files); without it they are indexed by name |
| Word | `.docx .docm .odt .rtf` | Paragraphs, tables, headers |
| Excel | `.xlsx .xlsm .xls .ods .csv` | Rows per sheet, with the header row as context |
| PowerPoint | `.pptx .pptm .odp` | Text and notes per slide |
| Email | `.msg .eml` | Header, body and **attachments** (PDF, Word, Excel…) |
| Text | `.txt .md .log` | Paragraphs |
| Other | `.doc .ppt .zip .mp4`… | Name and folder only |

**Every** file can also be found by its name and folder, even when its content can't be read.

Each result says **where** the match is: "página 3" (page 3), "hoja Gastos, filas 2–40" (sheet, rows), "diapositiva 5" (slide), "adjunto factura.pdf, página 1" (attachment), "bloque CAJETIN" (block)… Location labels are currently in Spanish.

## Requirements

- **Windows:** Windows Server 2016 or later, 64-bit.
- **Linux:** a 64-bit (x86_64 or aarch64) distribution with systemd: Debian/Ubuntu, RHEL/Rocky/Alma (with EPEL), Fedora, openSUSE or Arch. The shared folder is usually served with Samba.
- Internet access during installation.
- About 2 GB of disk for the program and the model, plus the index: about 4–5 KB per fragment. With 1 million fragments that's about 5 GB.
- RAM for the in-memory vectors: about 3 KB per fragment. With 1 million that's about 3 GB, or half with `memory_dtype = "float16"`.
- No GPU needed.

## Installation

### Windows Server

On the server, open **PowerShell as administrator** and run:

```powershell
irm https://raw.githubusercontent.com/abosch19/hound/main/install.ps1 | iex
```

If the server has a single shared folder, it is detected automatically; if it has several, you are asked which one. To pass parameters, especially the **VPN network** allowed to connect:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/abosch19/hound/main/install.ps1))) -SharePath D:\Shared -AllowFrom 10.8.0.0/24
```

If you'd rather download it first with `curl`:

```powershell
curl.exe -fsSL https://raw.githubusercontent.com/abosch19/hound/main/install.ps1 -o install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1 -SharePath D:\Shared -AllowFrom 10.8.0.0/24
```

| Parameter | Default | Meaning |
|---|---|---|
| `-SharePath` | detected | Local path of the shared folder |
| `-AllowFrom` | local subnet | Network(s) allowed to reach the MCP, e.g. the VPN's. Comma-separated |
| `-Port` | `8765` | MCP port |
| `-ShareName` / `-Unc` | detected | SMB share name and the `\\SERVER\Share` path shown in results |
| `-OcrLanguages` | `spa,eng` | OCR languages (e.g. `spa,eng,cat`) |
| `-Version` | `main` | GitHub branch or tag |
| `-NoOcr` | | Don't install Tesseract |

The installer does the following:

1. Installs `uv` and its own Python 3.12 in `C:\Program Files\Hound`, without touching the system Python.
2. Installs Hound and its dependencies.
3. Installs Tesseract with the OCR languages.
4. Downloads the embedding model, once.
5. Creates `C:\ProgramData\Hound\config.toml`.
6. Registers the **Hound** service: starts automatically and restarts if it fails.
7. Enables long path support in Windows (`LongPathsEnabled`).
8. Opens the port in the firewall only for `-AllowFrom`.

### Linux

On the server, as root (or with `sudo`):

```bash
curl -fsSL https://raw.githubusercontent.com/abosch19/hound/main/install.sh | sudo bash -s -- --share /srv/shared --allow-from 10.8.0.0/24
```

Without `--share`, the Samba shares are read with `testparm`: if there's only one it's used, otherwise you're asked which one.

| Parameter | Default | Meaning |
|---|---|---|
| `--share` | detected | Local path of the shared folder |
| `--allow-from` | local subnet | Network(s) allowed to reach the MCP, e.g. the VPN's. Comma-separated |
| `--port` | `8765` | MCP port |
| `--share-name` / `--unc` | detected | Samba share name and the `\\SERVER\Share` path shown in results |
| `--ocr-languages` | `spa,eng` | OCR languages (e.g. `spa,eng,cat`) |
| `--version` | `main` | GitHub branch or tag |
| `--no-ocr` / `--no-firewall` / `--no-service` | | Skip Tesseract, the firewall or the systemd service |

The installer does the following:

1. Installs `uv` and its own Python 3.12 in `/opt/hound`, without touching the system Python.
2. Installs Hound and its dependencies.
3. Installs Tesseract and the OCR languages with the system package manager (apt, dnf/yum, zypper or pacman).
4. Downloads the embedding model, once.
5. Creates `/etc/hound/config.toml` (with `permissions.mode = "posix"`); index, tokens and logs go to `/var/lib/hound`, readable only by root.
6. Registers the **hound** systemd service: starts at boot and restarts if it fails. It runs as root to read the whole folder, but with its capabilities reduced to reading files (`CAP_DAC_READ_SEARCH`) and the rest of the system read-only.
7. Raises `fs.inotify.max_user_watches` if needed, so change monitoring can cover large folders.
8. Opens the port only for `--allow-from` with **ufw** or **firewalld**, whichever is active. With neither, it warns you to restrict the port yourself.
9. Installs the `hound` command in `/usr/local/bin` (use it with `sudo`).

### Following the indexing

When installation finishes, the MCP server is already answering and the index builds in the background. The MCP server is the first thing that starts: the indexer only starts once the port is open, so you can follow progress from the very beginning.

- From the server: `hound status` (on Linux, `sudo hound status`).
- From Claude Code: ask *"how is the Hound indexing going?"*. The `index_status` tool returns the phase (scanning, indexing, paused, up to date, stopped), the percentage, pending and failed files, files per hour, the estimated time left and how many files you can see. It only returns counts, never paths.

> **Initial indexing.** With many files (especially scanned PDFs and images that need OCR), the first indexing run can take hours or days. During working hours the indexer runs slowly, with low CPU and disk priority. If you'd rather it only work at night, set `busy_mode = "pause"` in `config.toml`. After that it only processes changes.

### Updating

Run the same installation command again (`install.ps1` or `install.sh`). Configuration, index and tokens are kept.

### Uninstalling

On the server, from an administrator console (on Linux, with `sudo`):

```bash
hound uninstall
```

It asks for confirmation and keeps the index, tokens and configuration; add `--purge` to delete them too, and `-y` to skip the question. On Windows it continues in a new window. You can also use the scripts directly:

On Windows:

```powershell
irm https://raw.githubusercontent.com/abosch19/hound/main/uninstall.ps1 | iex
```

Data in `C:\ProgramData\Hound` is kept. To delete it too:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/abosch19/hound/main/uninstall.ps1))) -Purge
```

On Linux (data in `/etc/hound` and `/var/lib/hound` is kept unless you add `--purge`):

```bash
curl -fsSL https://raw.githubusercontent.com/abosch19/hound/main/uninstall.sh | sudo bash -s -- --purge
```

Tesseract is not removed on either system, since other programs may use it.

## Adding a user and connecting Claude Code

Each employee uses a **personal token** tied to **their account on the server** (Windows, or Linux/Samba). Searches run with that account's permissions. On the server, as administrator (on Linux, with `sudo`):

```powershell
hound token create maria --host SERVER
```

The account can be `maria`, `COMPUTER\maria` or `DOMAIN\maria`: local if the server is in a workgroup, or a domain account if you use Active Directory. On Linux it's any account the system knows (`getent passwd maria`), including domain accounts through winbind or sssd. The command shows the token **only once**, together with the exact command the person must run on their computer, with the VPN connected:

```bash
claude mcp add --transport http --scope user hound http://SERVER:8765/mcp --header "Authorization: Bearer hound_..."
```

From then on, in Claude Code it's enough to ask things like *"search the shared folder for the contract with the cleaning supplier and tell me when it expires"*.

Hound exposes these tools:

| Tool | What it does |
|---|---|
| `search_files(query, folder?, file_type?, date_from?, date_to?, limit?)` | Hybrid search. Returns UNC path, location, highlighted excerpt, date and score |
| `read_document(path, start?, end?)` | Extracted text of a file, in chunks, without loading all of it |
| `list_folder(path?)` | Subfolders and files visible to the user |
| `recent_files(limit?, folder?)` | Most recently modified files |
| `index_status()` | How the indexing is going: phase, progress, pending files, rate and estimated time left |

With `share.unc` set, results also include `file_url` and `folder_url` (`file://SERVER/Share/...`), and Claude shows them as clickable links: on Windows the first opens the file with its program and the second opens File Explorer in its folder. The user's computer must be able to reach the shared folder (office network or VPN).

Other commands to manage tokens:

```powershell
hound token list
hound token revoke maria   # all of maria's tokens
hound token revoke 7       # only the token with id 7
```

A revoked token stops working within 30 seconds.

> **Why Claude Code and not claude.ai connectors.** Remote connectors on claude.ai are called from Anthropic's servers, which can't reach a server that is only reachable over VPN. Claude Code connects from the user's computer, which is on the VPN. Hound must not be exposed to the internet.

## Permissions

### Windows (NTFS)

- When indexing, Hound stores each file's NTFS security descriptor: owner, group and ACL.
- When searching, it uses the Windows **Authz** API to check whether the token's account can read each file. It accounts for groups, nested groups, inheritance and "Deny" entries, just like Windows itself.
- The check happens **before** results are ranked, so a file the user can't access never appears: not in searches, `read_document`, `list_folder` or `recent_files`.
- If `share.name` is set, the share permissions ("Sharing" tab) are applied as well.
- When in doubt (a file without a descriptor, a Windows error), access is **denied**.
- Permission changes on disk take effect at the next rescan: right away if change monitoring picks them up and, at most, every `rescan_minutes`.

Other things to keep in mind:

- **Service account.** The service runs as `SYSTEM`, which must be able to read the shared folder (it usually can). `hound doctor` checks this.
- **Domain-joined server.** To compute a domain user's groups, the server's computer account must be able to read the `tokenGroupsGlobalAndUniversal` attribute. It usually can; if `hound search --as DOMAIN\user` fails, add the computer to the domain's **Windows Authorization Access Group**.
- **No-permissions mode.** With `permissions.mode = "none"` every user with a token sees the whole index. Only use it if everyone really can see everything.

### Linux (POSIX)

- When indexing, Hound stores each file's owner, group, mode bits and POSIX ACL (`setfacl`), plus those of every folder from the root of the share down to it.
- When searching, it applies the same check as the kernel when Samba opens a file on the user's behalf: read permission on the file and traverse permission (`x`) on every folder on the way. Groups are resolved with NSS, so local, winbind and sssd accounts all work, including named-user and named-group ACL entries and the ACL mask.
- Everything else works as on Windows: checked before ranking, denied when in doubt, changes picked up at the next rescan.
- Not covered: Samba-only rules in `smb.conf` (`valid users`, `read list`, `force user`…) and Windows ACLs stored by `vfs_acl_xattr` that aren't reflected in the POSIX ACLs. If you rely on those, use exclusions or check with `hound search --as`.

To check what a specific user would see:

```powershell
hound search "March payslips" --as maria
```

## Excluded folders

Whatever Claude reads through these tools is sent to Anthropic to generate the answer. That's why folders that must not leave the server (payroll, HR, health data…) are excluded from the index in `config.toml`:

```toml
[index]
exclude = ["HR", "Accounting/Payroll"]
```

Excluded folders are neither indexed nor searchable, and anything already indexed under them is removed at the next scan. Paths are relative to the shared folder and case-insensitive.

## AutoCAD DWG files

DWG is a closed format. To read its content (text, title blocks, layers):

1. Download and install **ODA File Converter**, free, from https://www.opendesign.com/guestfiles/oda_file_converter. The installer doesn't include it because its license must be accepted manually.
2. Restart the service with `Restart-Service Hound` (Linux: `sudo systemctl restart hound`). Hound finds it under `C:\Program Files\ODA\` on Windows, or as `ODAFileConverter` in the `PATH` on Linux (the `.deb`/`.rpm` package installs it in `/usr/bin`), and automatically reprocesses the DWG files that were indexed by name only.

If it's installed elsewhere, set the path in `[cad] oda_converter`.

## Operations

| Command | Purpose |
|---|---|
| `hound status` | Indexer phase, progress, pending and failed files, rate and estimated time left (same as the `index_status` tool) |
| `hound errors` | Files that couldn't be read and why (password-protected PDF, damaged files…) |
| `hound log --user maria` | Query log: who searched for what and when |
| `hound doctor` | Checks folder, model, Tesseract, ODA and permissions |
| `hound reindex --errors` | Retries failed files |
| `hound reindex --path "Projects\2024"` | Reindexes a folder or a file |
| `hound reindex --all` | Rebuilds the whole index (e.g. after changing the model). Keeps tokens and the query log |
| `hound backup D:\Backups` | Hot backup of the index |
| `Restart-Service Hound` / `sudo systemctl restart hound` | Applies `config.toml` changes |

Logs are in `C:\ProgramData\Hound\logs` on Windows, and in `/var/lib/hound/logs` and `journalctl -u hound` on Linux. On Linux, run these commands with `sudo`.

### Backup and restore

The index (`C:\ProgramData\Hound\index.sqlite`, or `/var/lib/hound/index.sqlite` on Linux) includes users, tokens and the query log. To back it up without stopping the service:

```powershell
hound backup D:\Backups
```

You can schedule it with Task Scheduler (on Linux, `sudo hound backup /backups` from cron or a systemd timer). To restore:

1. `Stop-Service Hound` (Linux: `sudo systemctl stop hound`).
2. Copy the file over `index.sqlite` and delete `index.sqlite-wal` and `index.sqlite-shm` if they exist.
3. `Start-Service Hound` (Linux: `sudo systemctl start hound`).

If the index is lost, nothing in the shared folder is lost: just run `hound reindex --all` and create the tokens again.

## Configuration

The file is `C:\ProgramData\Hound\config.toml` on Windows and `/etc/hound/config.toml` on Linux. After changing it, run `Restart-Service Hound` or `sudo systemctl restart hound`.

| Section | Option | Default | Description |
|---|---|---|---|
| `share` | `path` | | Local path of the shared folder |
| | `unc` | | UNC path shown in results |
| | `name` | | SMB share whose permissions are applied as well |
| `server` | `host`, `port` | `0.0.0.0`, `8765` | Where the MCP listens |
| | `tls_cert`, `tls_key` | | PEM paths to serve HTTPS |
| `index` | `exclude` | `[]` | Excluded folders |
| | `exclude_names` | recycle bin, `~$*`, `Thumbs.db`… | Ignored names (glob) |
| | `extensions` | see above | Extensions to index |
| | `max_file_mb` | `300` | Above this size, name only |
| | `rescan_minutes` | `360` | Periodic full rescan |
| | `watch` | `true` | Real-time change monitoring |
| | `extract_workers` | `2` | Parallel extraction processes |
| `index.schedule` | `busy_hours`, `busy_days` | `08:00-19:00`, `mon-fri` | Working hours |
| | `busy_mode` | `slow` | `slow`, `pause` or `normal` |
| `ocr` | `enabled`, `languages`, `dpi` | `true`, `spa+eng`, `300` | OCR with Tesseract |
| `cad` | `oda_converter` | detected | Path to `ODAFileConverter.exe` |
| `model` | `repo`, `file`, `dim` | EmbeddingGemma-300M int8, 768 | ONNX embedding model |
| | `threads` | half the cores | CPU threads for embeddings |
| `search` | `memory_dtype` | `float32` | `float16` uses half the RAM and is somewhat slower |
| `permissions` | `mode` | `ntfs` | `ntfs` or `none` |

About HTTPS: inside the VPN traffic is already encrypted. If you still want HTTPS with your own certificate, users must set `NODE_EXTRA_CA_CERTS` to the CA so Claude Code accepts it.

## How it works

- **Index:** a single SQLite database in WAL mode, in the style of [DigUp](https://github.com/ARahim3/DigUp). The indexer is the only writer; the server only reads. Tables:
  - `files`: path, type, size, date, status and security descriptor;
  - `segments`: fragments with their location, text and float16 vector;
  - `segments_fts`: FTS5 keyword index, accent-insensitive;
  - `security_descriptors`: deduplicated, because most files inherit their folder's permissions;
  - `users`, `tokens` and `query_log`.
- **Model:** [EmbeddingGemma-300M](https://huggingface.co/onnx-community/embeddinggemma-300m-ONNX), int8-quantized, with ONNX Runtime on the CPU. It's multilingual, with good quality in Spanish, and half the size of bge-m3. It's downloaded once and then works offline.
- **Hybrid search:**
  1. Cosine similarity against all vectors in memory (numpy, in blocks).
  2. In parallel, FTS5 with BM25 for keywords.
  3. Both lists are combined with *Reciprocal Rank Fusion* and grouped by file.

  With 1 million fragments, the vector part takes about 120 ms.
- **Incremental indexing:**
  - a periodic rescan compares each file's size, date and permissions;
  - change monitoring (`ReadDirectoryChangesW` on Windows, inotify on Linux) reacts immediately;
  - extraction runs in separate processes with a per-file time limit, and a failing file is logged without stopping anything.

## Known limitations

- Old `.doc` and `.ppt` files (binary format) are indexed by name only.
- Images are searched by the text they contain (OCR), not by what they show. Photo search by content and audio are planned for a later phase.
- Location labels and server messages are in Spanish.
- Paths are compared case-insensitively, as on Windows. On Linux, two files in the same folder whose names differ only in case are indexed as one.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
python -m pytest
```

- The real-ACL permission tests (`tests/test_windows_acl.py`) create local users: they only run on Windows with `HOUND_WIN_ACL_TESTS=1`. GitHub CI runs them on `windows-latest`, together with a full installation using `install.ps1`.
- The POSIX permission tests (`tests/test_posix_acl.py`) run on Linux and macOS; the one with real ACLs needs `setfacl`. CI also does a full installation on `ubuntu-latest` with `install.sh`, checks that a folder without permission stays hidden, and uninstalls.
- `HOUND_BENCH=1` enables the performance test with 1 million fragments.

The original brief (in Spanish) is in [docs/PROMPT.md](docs/PROMPT.md).

## License

MIT
