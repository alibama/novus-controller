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
| `GET /firings` | per-firing rows (energy, cost, peak, heat-work…). Filters: `controller`, `since` (UTC ISO), `limit` |
| `GET /firings.csv` | the same data as CSV |
| `GET /summary` | totals plus rollups `by_controller` and `by_month` |
| `GET /devices` | per-device aggregates (name, role, rated kW, firings, kWh, cost) — no addresses |
| `GET /health` | liveness |

## Attribution

The dataset is CC-BY-4.0. Consumers should credit the studio named in the
`studio` field (set it under Settings → Energy & open data).
