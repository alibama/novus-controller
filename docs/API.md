# Open-data API

A small HTTP API that publishes the studio's kiln/furnace data so other apps
— the glassdatabase.org Explore app, a scheduling tool, a phone widget — can
consume it, and lets *authorized* apps write back.

- **Read: no key.** GET-only endpoints, CORS open to any origin, CC-BY-4.0.
- **Write: bearer token.** `POST /notes` and `POST /commands` need
  `Authorization: Bearer <API_WRITE_TOKEN>`.
- **Public-safe by construction.** No Bluetooth addresses, no secrets, no
  personal data appear in any response — the tests assert it.

## Run it

The installer runs it as the `kiln-api` systemd service on port 8000. By hand:

```bash
.venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000
```

Interactive OpenAPI docs at `/docs`. Configuration comes from `api.env` next
to the code (see `api.env.example`), or from the environment:

| Variable | Meaning |
|---|---|
| `API_WRITE_TOKEN` | bearer token for the POST endpoints. Empty = writes disabled (503). |
| `API_ALLOW_CONTROL` | `1` (default) lets token holders queue run/stop/setpoint commands; `0` leaves only `poll` and notes. |
| `API_PUBLIC_URL` | optional, the public https URL — shown on the dashboard's Open Data page. |
| `KILN_DATA_DIR` | where the data lives (default: the repo dir). |

The dashboard's **Open Data & API** page shows the token, regenerates it, and
toggles control commands (restart `kiln-api` after changing them).

## Read endpoints

| Endpoint | Returns |
|---|---|
| `GET /` | index: license, attribution, data dictionary, endpoint list, whether writes are enabled |
| `GET /health` | `{ok, live_age_s, writes_enabled}` |
| `GET /live` | the latest reading per controller — see below |
| `GET /firings` | per-firing rows (energy, cost, peak, heat-work…). Filters: `controller`, `since`, `until`, `limit` |
| `GET /firings.csv` | the same data as CSV |
| `GET /summary` | totals plus rollups `by_controller` and `by_month`. Filters: `since`, `until` (date `YYYY-MM-DD` or ISO; a bare `until` date includes the whole day) |
| `GET /devices` | per-device aggregates: name, role, **model**, verified, rated kW, firings, kWh, cost — no addresses |
| `GET /events` | watchdog / audit trail (detections, restarts, API commands). Filters: `controller`, `limit` |
| `GET /notes` | notes posted via the API. Filters: `controller`, `limit` |
| `GET /commands` | recent queued commands and their status |
| `GET /commands/{id}` | one command |

### `/live`

Written by the dashboard's monitor after every poll (and after every control
action) to `state/live.json`; the API serves it with an `age_s` field so you
can tell how fresh it is.

```json
{
  "schema": "novus-kiln-live/1",
  "updated_utc": "2026-10-27T18:42:10+00:00",
  "last_poll_utc": "2026-10-27T18:42:10+00:00",
  "poll_interval_s": 300,
  "paused": false,
  "age_s": 12.4,
  "devices": {
    "furnace": {"role": "furnace", "model": "N20K48", "unit": "°F",
                "connected": true, "pv": 2098, "sp": 2100, "output_pct": 61.5,
                "running": true, "active_program": 1, "active_segment": 3,
                "watchdog": {"worried": false, "armed": true, "needs_human": false}},
    "annealer": {"role": "kiln", "model": "N20K48", "unit": "°F",
                 "connected": true, "pv": 896, "sp": 896, "output_pct": 18,
                 "running": true, "active_program": 1, "active_segment": 1,
                 "watchdog": {"worried": false, "armed": true, "needs_human": false}}
  }
}
```

## Write endpoints (bearer token)

### `POST /notes` → 201

```bash
curl -X POST http://kilnbox:8000/notes \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"controller":"annealer","text":"6mm clear, two layers","author":"anson",
       "firing_start_utc":"2026-10-27T14:05:00+00:00","tags":["fuse"]}'
```

Appends to `notes/notes.jsonl`. `GET /notes` returns them newest first; the
dashboard's Open Data page lists them.

### `POST /commands` → 202

```bash
curl -X POST http://kilnbox:8000/commands \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"controller":"annealer","action":"run_program","program":3,"source":"scheduler"}'
```

| `action` | arguments | effect |
|---|---|---|
| `run_program` | `program` 1–20 | select and start that Ramp & Soak program |
| `stop_program` | — | clear the run flag (disarms auto-recovery on that device) |
| `set_manual_setpoint` | `setpoint` | hold a fixed setpoint, no program (disarms auto-recovery) |
| `poll` | — | take a fresh reading now (allowed even with `API_ALLOW_CONTROL=0`) |

The API does not touch Bluetooth. It writes `commands/<id>.json` with
`status: pending`; the dashboard's monitor — the only process that owns the
radio — picks it up within ~2 s, runs it through the same `control()` path
as the on-screen buttons (including the read-only guard for unverified
controllers), logs it to the device's `events.log`, and updates the file to
`done`, `failed` or `rejected` with a `result` or `error`. Poll
`GET /commands/{id}` for the outcome. If the dashboard isn't running, commands
simply stay `pending`.

Error codes: `401` no token, `403` wrong token or control disabled, `404`
unknown controller, `422` bad action/arguments, `503` no token configured.

## Glass Database data contract

`GET /summary` exposes these keys at the **top level** so glassdatabase.org
can read them directly (aliases included, so either name works):

| Meaning | Keys |
|---|---|
| number of firings | `firings`, `count` |
| total energy | `energy_kwh`, `kwh` |
| total cost | `cost_usd`, `cost` |
| earliest / latest date | `since`/`start`, `until`/`end` |
| currency / attribution | `currency`, `studio`, `license` (CC-BY-4.0) |

The nested `totals`, `by_controller`, and `by_month` breakdowns are also
present. Set the studio name and electricity rate on the dashboard's
**Open Data & API** page so rows carry attribution and cost.

## Exposing it publicly

The Glass Database fetches from the open internet, so the API needs a public
HTTPS address. Any reverse proxy works; with Tailscale:

```bash
tailscale funnel --bg --https=10000 localhost:8000
tailscale funnel status        # confirm the public URL
```

Your `/summary` is then at `https://<machine>.<tailnet>.ts.net:10000/summary`.
Paste that base URL into the Open Data page's *Public URL* field and give the
`/summary` URL to the Glass Database as this studio's `STUDIO_DATA_URL`.

Only the API (port 8000) needs to be public; keep the dashboard (8501) on your
LAN or tailnet — it is password-gated but it can drive kilns.

## Attribution

The dataset is CC-BY-4.0. Consumers should credit the studio named in the
`studio` field.
