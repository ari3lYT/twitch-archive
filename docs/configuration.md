# Configuration

| Variable | Default | Applies to |
| --- | --- | --- |
| `CHANNEL` | `example_channel` | Worker and controller; set this explicitly |
| `ARCHIVE_ROOT` | `/archive` (worker), `./archive` (controller) | Recording storage |
| `STATE_ROOT` | `/state` (worker), `./state` (controller) | Queue, control and telemetry |
| `DOWNLOAD_START` / `DOWNLOAD_END` | `06:30` / `23:30` | Shared daily guard and ETA |
| `ARCHIVE_TIMEZONE` | `Europe/Moscow` | Window timezone |
| `MIN_FREE_BYTES` | 100 GiB | Worker free-space reserve |
| `PANEL_BASE` | `./panel` | Controller directory; socket is `control/agent.sock` beneath it |
| `CONTROL_UID` / `CONTROL_GID` | Current UID/GID | Allowed dashboard peer and file ownership |
| `DOCKER_BINARY` | `docker` | Controller executable |
| `ARCHIVE_CONTAINER` | `twitch-archive` | Fixed worker container name |
| `PANEL_STATE` | `/panel-state` | Dashboard authentication/probe directory |
| `MEDIA_ROOT` | `/archive` | Dashboard read-only recording mount |
| `CONTROL_SOCKET` | `/control/agent.sock` | Dashboard controller connection |
| `PUBLIC_ORIGIN` | `https://archive.example.com` | Exact TLS origin for CSRF |
| `PANEL_HOST` / `PANEL_PORT` | `127.0.0.1` / `8925` | HTTP bind |

`state/settings.json` holds validated runtime tuning: polling, settle delay, socket timeout,
free-space reserve, retry intervals, rate limiting and worker count. Paths above must refer to
the same storage across the worker/controller, even if container mount names differ.

Runtime state contains viewing metadata, replay messages and authentication material. Keep the
entire runtime directory private. No actual archive, account, configuration or credentials are included.
