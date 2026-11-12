# Deployment

You can use AnaTele in four ways:

| Option | Best for | Command |
|---|---|---|
| Standalone desktop build | Pilots and investigators. Python is not necessary. | Download from [Releases](https://github.com/Avinashistoocool/AnaTele/releases) |
| pip install | Developers, scripts, CLI | `pip install "anatele[desktop] @ git+https://github.com/Avinashistoocool/AnaTele"` |
| Docker (web UI) | A shared lab computer or a private server | `docker compose up -d` |
| Source checkout | Contributions | `pip install -e ".[desktop,dev]"` |

## Standalone desktop build

Each `v*` tag starts [`release.yml`](https://github.com/Avinashistoocool/AnaTele/blob/main/.github/workflows/release.yml). This workflow builds a one-folder PyInstaller bundle for
Windows, macOS and Linux. It attaches the bundle to the GitHub release, together with the wheel and five simulated flights.

1. Download `AnaTele-<os>.zip`.
2. Extract the zip file.
3. Run `AnaTele.exe` (Windows) or `AnaTele` (macOS/Linux).
4. To try the tool, open `simulations/sim_motor_failure.tlog`.

The build has no signature. Windows SmartScreen can ask you to confirm (*More info → Run anyway*). On macOS, right-click → *Open*.

**Verify a download.** Each release contains `SHA256SUMS.txt` and a build-provenance attestation with a Sigstore signature.
The attestation connects each asset to the exact workflow run and commit that made it:

```bash
gh attestation verify AnaTele-windows.zip --repo Avinashistoocool/AnaTele
```

**Supply-chain hardening.** Release builds install only hash-locked dependencies from `requirements/release.txt`
(`--require-hashes`). Each GitHub Action is pinned to a full commit SHA. The build jobs have a read-only token and
no persisted credentials. Only a final publish job can write to the release, and this job runs no third-party build tools.

To update the lock, run this command:

```bash
uv pip compile requirements/release.in --universal --python-version 3.12 --generate-hashes -o requirements/release.txt
```

To build locally, run these commands:

```bash
pip install -e ".[desktop,dev]"
pyinstaller --noconfirm anatele.spec        # → dist/AnaTele/
```

For each analysis, the bundled executable starts itself again with a hidden `--worker-job` argument. Thus, the workers
have the same process isolation as in a source installation.

## pip

```bash
pip install "anatele[desktop] @ git+https://github.com/Avinashistoocool/AnaTele"
anatele --help        # desktop app with no arguments; see USAGE for sub-commands
```

On servers that must not have Qt, use plain `anatele` (without `[desktop]`). You still get `anatele web`, `analyse`, `replay` and `simulate`.

## Docker (web UI)

```bash
docker compose up -d --build
# open http://127.0.0.1:8766
```

The compose file does these steps:

- It publishes **only on the loopback interface of the host** (`127.0.0.1:8766`).
- It runs as a non-root user with a read-only root filesystem, `no-new-privileges` and a memory limit of 1 GB.
- It keeps the data in the named volume `anatele-data` (`/data`).
- It contains the simulated flights in `/app/simulations` (`docker compose exec anatele ls /app/simulations`).

Plain Docker:

```bash
docker build -t anatele .
docker run -d -p 127.0.0.1:8766:8766 -v anatele-data:/data anatele
```

### Exposing beyond localhost

> **WARNING:** Do not publish port 8766 directly to the Internet. AnaTele has **no authentication or multi-tenancy**, and its parser worker is not an OS sandbox.

If other persons must have access, do these steps:

1. Put AnaTele behind a reverse proxy that does authentication (for example Caddy or nginx with OAuth2 Proxy, or Tailscale/Cloudflare Access).
2. Add the public hostname to `ANATELE_ALLOWED_HOSTS` (comma-separated). If you do not add it, the Host-header check rejects the requests.
3. Keep the container on a private network.

Example Caddy snippet:

```caddy
anatele.example.org {
    forward_auth oauth2-proxy:4180 { uri /oauth2/auth }
    reverse_proxy anatele:8766
}
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TELEMETRY_DATA_DIR` | refer to the next section | Database, uploads, Parquet datasets, worker logs |
| `ANATELE_HOST` | `127.0.0.1` | Web bind address (`0.0.0.0` in Docker) |
| `ANATELE_PORT` | `8766` | Web port |
| `ANATELE_ALLOWED_HOSTS` | *(empty)* | Additional Host headers that the web UI accepts |
| `ANATELE_REAL_TLOG` | — | Path to the private regression capture for `test_real_log.py` |

## Data directory

| Install | Default location |
|---|---|
| Source checkout | `./data` |
| Installed / standalone on Windows | `%LOCALAPPDATA%\AnaTele` |
| Installed / standalone on macOS / Linux | `$XDG_DATA_HOME/anatele` or `~/.local/share/anatele` |
| Docker | `/data` volume |

The directory contains:

- `platform.db` (SQLite: profiles, jobs, manifests);
- `uploads/` (the original files, never changed);
- `datasets/<job>/` (Parquet for observations and signals);
- `worker_logs/` (parser output and tracebacks);
- `exports/` (temporary files while the web UI sends or receives a `.ant`; AnaTele removes them after).

An opened `.ant` file becomes a usual entry in `datasets/`. If the `.ant` file contains the original log, it also becomes an entry in `uploads/`.
The desktop app and the web UI can share one directory. But only one web server can run on a directory at a time.

To make a backup, stop AnaTele, then copy the directory. AnaTele deletes nothing automatically. To get disk space back, remove the entries in `uploads/` and
`datasets/` manually.

## Resource limits

| Limit | Value | Where |
|---|---|---|
| Upload size | 100 MiB | `store.MAX_UPLOAD` |
| `.ant` file / per Parquet entry | 1 GiB / 768 MiB | `antfile.MAX_ANT`, `antfile.LIMITS` |
| Decoded records | 1,000,000 | `parsers.MAX_RECORDS` |
| Analysis deadline | 180 s | `worker.TIMEOUT_SECONDS` |
| Events per log | 20,000 | `analysis.Analysis.event` |
| Derived samples in memory | 6,000,000 | `analysis.SERIES_POINT_LIMIT` |
| DuckDB per query | 256 MB, 2 threads | `api.query` |

## Continuous integration

[`ci.yml`](https://github.com/Avinashistoocool/AnaTele/blob/main/.github/workflows/ci.yml) runs pyflakes and the full test suite on Ubuntu and Windows with Python 3.11 and 3.13. The test suite includes the offscreen smoke test of the desktop app.
The workflow also builds the Docker image and examines its health endpoint.

## Releasing

1. Change the version in `pyproject.toml` and `telemetry_platform/__init__.py`.
2. Make the tag and push it:

```bash
# bump version in pyproject.toml and telemetry_platform/__init__.py
git tag v0.3.0 && git push origin v0.3.0
```
