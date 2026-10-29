# Deployment

AnaTele can be used in four ways:

| Option | Best for | Command |
|---|---|---|
| Standalone desktop build | Pilots and investigators; no Python needed | Download from [Releases](https://github.com/Avinashistoocool/AnaTele/releases) |
| pip install | Developers, scripting, CLI | `pip install "anatele[desktop] @ git+https://github.com/Avinashistoocool/AnaTele"` |
| Docker (web UI) | A shared lab machine or private server | `docker compose up -d` |
| Source checkout | Contributing | `pip install -e ".[desktop,dev]"` |

## Standalone desktop build

Each `v*` tag triggers [`release.yml`](../.github/workflows/release.yml). It builds a one-folder PyInstaller bundle for
Windows, macOS and Linux and attaches it to the GitHub release together with the wheel and five simulated flights.

1. Download `AnaTele-<os>.zip` and extract it.
2. Run `AnaTele.exe` (Windows) or `AnaTele` (macOS/Linux).
3. Open `simulations/sim_motor_failure.tlog` to try it.

The build is unsigned. Windows SmartScreen may ask you to confirm (*More info → Run anyway*); on macOS, right-click → *Open*.

**Verifying a download.** Each release includes `SHA256SUMS.txt` and a Sigstore-signed build-provenance attestation
that ties every asset to the exact workflow run and commit that produced it:

```bash
gh attestation verify AnaTele-windows.zip --repo Avinashistoocool/AnaTele
```

**Supply-chain hardening.** Release builds install only hash-locked dependencies from `requirements/release.txt`
(`--require-hashes`). Every GitHub Action is pinned to a full commit SHA, and the build jobs have a read-only token with
no persisted credentials. Only a final publish job, which runs no third-party build tooling, can write to the release.
To update the lock:

```bash
uv pip compile requirements/release.in --universal --python-version 3.12 --generate-hashes -o requirements/release.txt
```

Build locally:

```bash
pip install -e ".[desktop,dev]"
pyinstaller --noconfirm anatele.spec        # → dist/AnaTele/
```

The bundled executable re-invokes itself with a hidden `--worker-job` argument for each analysis, so workers
keep the same process isolation as a source install.

## pip

```bash
pip install "anatele[desktop] @ git+https://github.com/Avinashistoocool/AnaTele"
anatele --help        # desktop app with no arguments; see USAGE for sub-commands
```

Use plain `anatele` (without `[desktop]`) on servers where Qt isn't wanted. You still get `anatele web`, `analyse`, `replay` and `simulate`.

## Docker (web UI)

```bash
docker compose up -d --build
# open http://127.0.0.1:8766
```

The compose file:

- Publishes **only on the host's loopback** (`127.0.0.1:8766`).
- Runs as a non-root user with a read-only root filesystem, `no-new-privileges` and a 1 GB memory limit.
- Stores data in the named volume `anatele-data` (`/data`).
- Includes the simulated flights in `/app/simulations` (`docker compose exec anatele ls /app/simulations`).

Plain Docker:

```bash
docker build -t anatele .
docker run -d -p 127.0.0.1:8766:8766 -v anatele-data:/data anatele
```

### Exposing beyond localhost

AnaTele has **no authentication or multi-tenancy**, and its parser worker is not an OS sandbox. If other people need access:

1. Put it behind an authenticating reverse proxy (e.g. Caddy or nginx with OAuth2 Proxy, or Tailscale/Cloudflare Access).
2. Add the public hostname to `ANATELE_ALLOWED_HOSTS` (comma-separated). Otherwise the Host-header check rejects requests.
3. Keep the container on a private network. Don't publish port 8766 directly to the Internet.

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
| `TELEMETRY_DATA_DIR` | see below | Database, uploads, Parquet datasets, worker logs |
| `ANATELE_HOST` | `127.0.0.1` | Web bind address (`0.0.0.0` inside Docker) |
| `ANATELE_PORT` | `8766` | Web port |
| `ANATELE_ALLOWED_HOSTS` | *(empty)* | Extra Host headers accepted by the web UI |
| `ANATELE_REAL_TLOG` | — | Path to the private regression capture for `test_real_log.py` |

## Data directory

| Install | Default location |
|---|---|
| Source checkout | `./data` |
| Installed / standalone on Windows | `%LOCALAPPDATA%\AnaTele` |
| Installed / standalone on macOS / Linux | `$XDG_DATA_HOME/anatele` or `~/.local/share/anatele` |
| Docker | `/data` volume |

Contents: `platform.db` (SQLite: profiles, jobs, manifests), `uploads/` (original files, never modified),
`datasets/<job>/` (observations and signals Parquet), `worker_logs/` (parser output and tracebacks),
`exports/` (temporary files while the web UI streams or receives a `.ant`; removed afterwards).
Opened `.ant` files become ordinary entries in `datasets/`, plus `uploads/` when the original log was included.
The desktop app and web UI can share one directory, but only one web server per directory can run at a time.

Back up by copying the directory while AnaTele is stopped. Nothing is deleted automatically; remove `uploads/` and
`datasets/` entries yourself to reclaim space.

## Resource limits

| Limit | Value | Where |
|---|---|---|
| Upload size | 100 MiB | `store.MAX_UPLOAD` |
| `.ant` file / per Parquet entry | 1 GiB / 768 MiB | `antfile.MAX_ANT`, `antfile.LIMITS` |
| Decoded records | 1,000,000 | `parsers.MAX_RECORDS` |
| Analysis deadline | 180 s | `worker.TIMEOUT_SECONDS` |
| Events per log | 20,000 | `analysis.Analysis.event` |
| In-memory derived samples | 6,000,000 | `analysis.SERIES_POINT_LIMIT` |
| DuckDB per query | 256 MB, 2 threads | `api.query` |

## Continuous integration

[`ci.yml`](../.github/workflows/ci.yml) runs pyflakes and the full test suite (including the offscreen desktop smoke
test) on Ubuntu and Windows with Python 3.11 and 3.13. It also builds the Docker image and checks its health endpoint.

## Releasing

```bash
# bump version in pyproject.toml and telemetry_platform/__init__.py
git tag v0.3.0 && git push origin v0.3.0
```
