#!/usr/bin/env python3
"""Entry point: an HTTP status endpoint plus the background reporter loop.

The HTTP server exists so the platform has something to ping and so a human
hitting the URL sees a working app. It never proxies, scans or executes
anything.
"""
import asyncio
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import reporter
from config import UPSTREAM_HOST, UPSTREAM_KEY, VERSION, PORT, log

STARTED = time.time()
REPORT_STALE = 180  # seconds without an upstream report before /api/ping says degraded


class StatusHandler(BaseHTTPRequestHandler):
    server_version = "nginx"
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
        if path in ("/api/ping", "/status"):
            state = reporter.STATUS
            report_age = (time.time() - state["last_report"]) if state["last_report"] else None
            healthy = bool(state["connected"]) and report_age is not None and report_age < REPORT_STALE
            # Always 200: a dead upstream must not make the platform restart us.
            self._json(200, {
                "status": "ok" if healthy else "degraded",
                "uptime": int(time.time() - STARTED),
                "upstream": {
                    "version": VERSION,
                    "ok": bool(state["connected"]),
                    "age": int(report_age) if report_age is not None else None,
                },
            })
            return
        if path == "/":
            body = (
                "<!doctype html><html><head><title>App</title></head>"
                "<body><h1>It works!</h1>"
                "<p>This container is running. Static content placeholder.</p>"
                "</body></html>"
            ).encode()
            self._send(200, body, "text/html; charset=utf-8")
            return
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        log("debug", "http:", fmt % args)


def start_http(port):
    server = ThreadingHTTPServer(("0.0.0.0", port), StatusHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    thread.start()
    log("info", f"status endpoint on :{port}")
    return server


async def run_reporter():
    while True:
        await reporter.Reporter().run_forever()


def main():
    port = int(PORT)
    server = start_http(port)

    if not UPSTREAM_HOST or not UPSTREAM_KEY:
        log("info", "upstream not configured, serving /api/ping only")
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
    task = loop.create_task(run_reporter())

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
