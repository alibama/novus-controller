# Open-data API

A small read-only HTTP API that publishes the studio's kiln/furnace **energy
and cost** data plus per-device aggregates, so anyone can consume it for global
insight. It's the machine-readable companion to the home page's Open Data
Studio harvest, and it's shaped to feed dashboards or the glassdatabase.org
Explore app.

**Public-safe by design:** GET-only, CORS open to any origin, and it exposes
**no Bluetooth addresses, no secrets, no raw personal data** — only firing
summaries and rollups. Data is **CC-BY-4.0** (please attribute the studio).

## Run it

```bash
pip install -r requirements-api.txt
uvicorn api:app --host 0.0.0.0 --port 8000
```

Interactive OpenAPI docs at `/docs`. For an always-on service use
`deploy/kiln-api.service`, and expose it read-only via your reverse proxy or a
Tailscale Funnel (a different port from the dashboard).

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /` | index, license, attribution, data dictionary, endpoint list |
| `GET /firings` | per-firing rows (energy, cost, peak, heat-work…). Filters: `controller`, `since`, `until`, `limit` |
| `GET /firings.csv` | the same data as CSV |
| `GET /summary` | totals plus rollups `by_controller` and `by_month`. Filters: `since`, `until` (date `YYYY-MM-DD` or full ISO; a bare `until` date includes the whole day). Echoes the window in `range`, and reports the data's own span in `since`/`until` (`start`/`end`). |
| `GET /devices` | per-device aggregates (name, role, rated kW, firings, kWh, cost) — no addresses |
| `GET /health` | liveness |

## Attribution

The dataset is CC-BY-4.0. Consumers should credit the studio named in the
`studio` field (set it under Settings → Energy & open data).

## Glass Database data contract

`GET /summary` exposes these keys at the **top level** so glassdatabase.org can
read them directly (aliases included, so either name works):

| Meaning | Keys |
|---|---|
| number of firings | `firings`, `count` |
| total energy | `energy_kwh`, `kwh` |
| total cost | `cost_usd`, `cost` |
| earliest date | `since`, `start` |
| currency / attribution | `currency`, `studio`, `license` (CC-BY-4.0) |

The nested `totals`, `by_controller`, and `by_month` breakdowns are also present.
No Bluetooth addresses or secrets appear anywhere in the response.

## Exposing it publicly (Tailscale Funnel)

The Glass Database fetches from the open internet, so it needs **Funnel** (not
Serve), HTTPS, and a valid cert. Funnel's public ports are 443, 8443, 10000. If
your dashboard already uses 8443, put the API on another one, e.g. 10000 → the
API's local port 8000:

```bash
tailscale funnel --bg --https=10000 localhost:8000
tailscale funnel status        # confirm the public URL
```

Your `/summary` is then at e.g. `https://<machine>.<tailnet>.ts.net:10000/summary`.
Give that URL to the Glass Database as this studio's `STUDIO_DATA_URL`.
