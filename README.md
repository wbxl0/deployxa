# deployxa-app

A tiny status app: serves a status endpoint and, optionally, reports host
metrics to a compatible upstream.

## Endpoints

| path | what |
| :-- | :-- |
| `/api/ping` | JSON: `status` (`ok` / `degraded`), `uptime`, upstream state |
| `/` | placeholder page |

`/api/ping` always answers `200` so an upstream outage cannot make the platform
restart-loop the container; read the `status` field instead.

## Environment

| variable | default | meaning |
| :-- | :-- | :-- |
| `UPSTREAM_HOST` | `nz.wbxl.dpdns.org:443` | upstream gRPC endpoint |
| `UPSTREAM_KEY` | *(empty)* | upstream credential; empty = status endpoint only |
| `UUID` | built-in | node identity |
| `PORT` / `SERVER_PORT` | `3000` | port for the status endpoint |
| `REPORT_INTERVAL` | `45` | seconds between state reports (min 30) |
| `IP_PERIOD` | `1800` | seconds between public-IP refreshes |
| `DNS_MODE` | `off` | `off` / `auto` / `doh` |
| `INSECURE_TLS` | `false` | skip TLS verification (only for self-signed upstreams) |
| `LOG_LEVEL` | `none` | `debug` / `info` / `warn` / `error` |

## Layout

```
main.py       entry point: HTTP status server + asyncio loop, SIGTERM handling
reporter.py   report loop, task handling (metadata only), status snapshot
upstream.py   upstream protocol: protobuf messages, HPACK, HTTP/2 client
host.py       host metrics (/proc, disk, load) and DNS helpers
config.py     environment + logging
```
