#!/usr/bin/env python3
"""Entry point: an HTTP health endpoint plus the monitoring agent loop.

The HTTP server exists so the platform has something to health-check and so a
human hitting the URL sees what the container actually is. It never proxies,
scans or executes anything.
"""
import asyncio
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent
from config import BOT_GATEWAY, BOT_TOKEN_SECRET, BOT_VERSION, PORT, log

STARTED = time.time()
REPORT_STALE = 180  # seconds without a state report before /health says degraded


class HealthHandler(BaseHTTPRequestHandler):
    server_version = "monitor-agent/" + BOT_VERSION
    sys_version = ""

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, code, payload):
        self._send(code, json.dumps(payload).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/healthz", "/api/health"):
            state = agent.HEALTH
            report_age = (time.time() - state["last_report"]) if state["last_report"] else None
            healthy = bool(state["connected"]) and report_age is not None and report_age < REPORT_STALE
            # Always 200: a dead panel must not make the platform restart us.
            self._json(200, {
                "status": "ok" if healthy else "degraded",
                "uptime_seconds": int(time.time() - STARTED),
                "agent": {
                    "version": BOT_VERSION,
                    "connected": bool(state["connected"]),
                    "last_report_age": int(report_age) if report_age is not None else None,
                    "last_error": state["last_error"],
                },
            })
            return
        if path == "/":
            body = (
                "host monitoring agent\n"
                f"version {BOT_VERSION}\n"
                "endpoints: /health\n"
            ).encode()
            self._send(200, body, "text/plain; charset=utf-8")
            return
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        log("debug", "http:", fmt % args)


def start_http(port):
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    thread.start()
    log("info", f"health endpoint on :{port}")
    return server


async def run_agent():
    while True:
        await agent.BotAgent().run_forever()


def main():
    port = int(PORT)
    server = start_http(port)

    if not BOT_GATEWAY or not BOT_TOKEN_SECRET:
        log("info", "panel credentials missing, serving /health only")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(run_agent())

    def stop(signum, _frame):
        log("info", f"signal {signum}, shutting down")
        loop.call_soon_threadsafe(task.cancel)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    finally:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        server.shutdown()


if __name__ == "__main__":
    main()
