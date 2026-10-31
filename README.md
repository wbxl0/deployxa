# deployxa-monitor

A small, single-purpose **host monitoring agent** for [Nezha](https://github.com/nezhahq/nezha)-compatible panels.
It reports CPU / memory / disk / network / load / process counts to the panel and answers metadata queries.
That is all it does.

## Endpoints

| path | what |
| :-- | :-- |
| `/health` | JSON: `status` (`ok` / `degraded`), uptime, panel connection state, last report age, last error |
| `/` | plain text: what this container is |

`/health` always answers `200` so a panel outage cannot make the platform restart-loop the
container; read the `status` field instead.

## What it deliberately does NOT do

- no remote shell, no terminal, no file manager, no command execution — those task types are
  answered with `disabled` and the code paths are gone, not just switched off
- no runtime downloads, no `curl | sh`, no writing executables to `/tmp`
- no DNS-over-HTTPS unless `BOT_DNS_MODE` says so (default `off` = system resolver)
- no TLS verification bypass by default (`BOT_INSECURE_TLS=false`)
- no `/etc/hosts` edits, no decoy pages

## Environment

| variable | default | meaning |
| :-- | :-- | :-- |
| `BOT_GATEWAY` | `nz.wbxl.dpdns.org:443` | panel gRPC endpoint |
| `BOT_TOKEN_SECRET` | *(empty)* | panel agent secret; empty = health endpoint only |
| `UUID` | built-in | node identity on the panel |
| `PORT` / `SERVER_PORT` | `3000` | port for the health endpoint |
| `BOT_HEARTBEAT` | `45` | seconds between state reports (min 30) |
| `BOT_IP_PERIOD` | `1800` | seconds between public-IP refreshes |
| `BOT_DNS_MODE` | `off` | `off` / `auto` / `doh` |
| `BOT_INSECURE_TLS` | `false` | skip TLS verification (only for self-signed panels) |
| `LOG_LEVEL` | `none` | `debug` / `info` / `warn` / `error` |

## Layout

```
main.py     entry point: HTTP health server + asyncio agent loop, SIGTERM handling
agent.py    report loop, task handling (metadata only), health snapshot
panel.py    panel protocol: protobuf messages, HPACK, HTTP/2 client
host.py     host metrics (/proc, disk, load) and DNS helpers
config.py   environment + logging
```
