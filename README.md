<p align="center">
  <img src="https://img.shields.io/github/license/ChristopherDond/service-watchdog" alt="License" />
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python" />
  <img src="https://img.shields.io/badge/dependencies-none-brightgreen" alt="Dependencies" />
</p>

<div align="right">

[Português (PT-BR)](README-pt-br.md)

</div>

# service-watchdog

**service-watchdog** keeps local services alive: probes HTTP/TCP endpoints on a schedule, restarts the failed service after N consecutive failures, logs every incident to `incidents.jsonl`, and alerts on Telegram the moment something goes down. Built for homelabs, self-hosted dashboards, game servers and local AI gateways — anything that crashes when nobody is watching.

## What it does

- Probes each service in `config.json` (`http` GET or raw `tcp` connect).
- Counts consecutive failures per service.
- Runs the service `restart_command` after `failure_threshold` hits.
- Respects per-service `cooldown` so a flapping service is not restarted in a loop.
- Appends every restart to `incidents.jsonl`.
- Sends a Telegram `sendMessage` alert when enabled.

## How it works

Probe logic:

- `probe=http` → `GET {url}{health_path}` with timeout. Healthy = status 2xx–3xx.
- `probe=tcp` → raw `socket.create_connection(host, port)` with timeout. Healthy = connect succeeds. Useful for databases, game servers or any TCP service that doesn't speak HTTP.

Restart logic:

- Failures tracked per service in `.watchdog_state.json` (`failures`, `last_restart`).
- Success resets the counter to 0.
- When `failures >= failure_threshold` and `cooldown` expired → `subprocess.run(restart_command, shell=True)`, counter reset, `last_restart` updated.

Incident log:

- One JSON object per line in `incidents.jsonl`: `timestamp`, `service`, `url`, `failures`, `restart_command`, `returncode`, `restarted`.

## Installation

```bash
cd service-watchdog
pip install -r requirements.txt
cp config.json.example config.json
```

Python 3.11+, stdlib only. `requirements.txt` holds just `pytest` for tests.

## Configuration

Copy `config.json.example` to `config.json` and edit. Never commit `config.json` with real tokens (gitignored).

| Field | Level | Description |
|---|---|---|
| `check_interval` | root | Seconds between probes in `daemon` mode |
| `failure_threshold` | root | Default consecutive failures before restart |
| `incidents_file` | root | Path for `incidents.jsonl` |
| `telegram.enabled` | root | `true` to send Telegram alerts |
| `telegram.bot_token` | root | BotFather token (keep in `config.json`, never commit) |
| `telegram.chat_id` | root | Target chat/channel id |
| `telegram.parse_mode` | root | `HTML` (default) |
| `name` | service | Unique service name |
| `url` | service | Base URL (`http://host:port`) or `host:port` for TCP |
| `health_path` | service | Appended to `url` for HTTP probes (e.g. `/health`) |
| `probe` | service | `http` or `tcp` |
| `timeout` | service | Probe timeout in seconds |
| `restart_command` | service | Shell command run on failure (e.g. `docker restart x`) |
| `cooldown` | service | Min seconds between restarts for this service |
| `failure_threshold` | service | Optional per-service override |

## Usage

```bash
python watchdog.py check --config config.json.example --dry-run
python watchdog.py check --config config.json
python watchdog.py run --config config.json --state .watchdog_state.json
python watchdog.py daemon --config config.json --interval 30
python watchdog.py daemon --config config.json --iterations 5 --interval 1
```

Subcommands:

- `check` — probe once, print `OK`/`FAIL`, never restarts. Exit 1 if any service unhealthy.
- `run` — probe once, restart services over threshold, save state, log incidents.
- `daemon` — loop `run` forever (`--iterations N` caps loops for cron/testing). `Ctrl+C` exits cleanly.

## Tests

```bash
python -m pytest
```
