#!/usr/bin/env python3
"""Host status reporter: collect metrics, report them, answer nothing else."""
import asyncio
import json
import socket
import time
import urllib.request

from config import *
from host import SystemSampler, _is_ip_literal
from upstream import (GrpcClient, H2Error, decode_task, decode_uint_receipt,
                   encode_geoip, encode_host, encode_state, encode_task_result)

# Snapshot for the /api/ping endpoint (main.py reads it from another thread).
STATUS = {"connected": False, "last_report": 0.0, "last_error": "", "started": time.time()}



IPV4_PROBES = (
    "https://ipv4.ip.sb/ip",
    "https://blog.cloudflare.com/cdn-cgi/trace",
    "https://developers.cloudflare.com/cdn-cgi/trace",
)
IPV6_PROBES = (
    "https://ipv6.ip.sb/ip",
    "https://blog.cloudflare.com/cdn-cgi/trace",
    "https://developers.cloudflare.com/cdn-cgi/trace",
)
_bot_geo_state = {"cached_ip": "", "changed": True}


def _fetch_ip_sync(endpoints, family):
    for url in endpoints:
        text = ""
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=RPC_TIMEOUT) as response:
                text = response.read().decode("utf-8", "replace").strip()
        except Exception:
            continue
        candidates = [text] + [line[3:].strip() for line in text.splitlines()
                               if line.startswith("ip=")]
        for candidate in candidates:
            if not candidate or not _is_ip_literal(candidate):
                continue
            try:
                socket.inet_pton(family, candidate)
                return candidate
            except OSError:
                continue
    return ""


async def fetch_bot_geoip():
    ipv4, ipv6 = await asyncio.gather(
        asyncio.to_thread(_fetch_ip_sync, IPV4_PROBES, socket.AF_INET),
        asyncio.to_thread(_fetch_ip_sync, IPV6_PROBES, socket.AF_INET6),
    )
    if not ipv4 and not ipv6:
        return None
    newest = ipv6 or ipv4
    if newest and newest != _bot_geo_state["cached_ip"]:
        _bot_geo_state["cached_ip"] = newest
        _bot_geo_state["changed"] = True
    return {"ipv4": ipv4 or "", "ipv6": ipv6 or ""}


async def execute_task(task):
    """Answer metadata queries only.

    The upstream can still send command / probe tasks; every one of them is
    refused here, so a compromised upstream cannot run anything in this
    container.
    """
    result = {"id": task["id"], "type": task["type"], "delay": 0, "data": "",
              "successful": False}
    started = time.monotonic()
    try:
        if task["type"] == 12:
            result["data"] = json.dumps({
                "server": UPSTREAM_HOST,
                "uuid": UUID,
                "report_delay": REPORT_INTERVAL,
                "disable_command_execute": True,
                "disable_send_query": True,
                "disable_terminal": True,
                "disable_file_manager": True,
            })
            result["successful"] = True
        elif task["type"] == 7:
            result["successful"] = True
        else:
            result["data"] = "disabled"
    except Exception as exc:
        result["data"] = str(exc)
    result["delay"] = time.monotonic() - started
    return result

class Reporter:
    def __init__(self):
        self.sampler = SystemSampler()
        self.client = None
        self.sessions = set()

    async def run_forever(self):
        retry_delay = RETRY_DELAY
        while True:
            started = time.monotonic()
            error = None
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = exc
            uptime = time.monotonic() - started
            if uptime >= 60:
                retry_delay = RETRY_DELAY
                log("debug", "Bot connection recycled, reconnecting")
            else:
                STATUS["last_error"] = f"{type(error).__name__}: {error}" if error else ""
                log("debug", f"Bot reconnect in {retry_delay}s: {error}")
            await asyncio.sleep(retry_delay)
            retry_delay = min(RECONNECT_COOLDOWN, retry_delay * 2)

    async def run_once(self):
        client = GrpcClient(UPSTREAM_HOST)
        self.client = client
        workers = set()
        running_tasks = set()
        try:
            await client.connect()
            STATUS["connected"] = True
            STATUS["last_error"] = ""
            receipt = await client.unary("/proto.NezhaService/ReportSystemInfo2",
                                         encode_host(self.sampler.host()))
            dashboard_boot_time = decode_uint_receipt(receipt)

            geoip = await fetch_bot_geoip()
            if geoip:
                geoip["dashboard_boot_time"] = dashboard_boot_time
                try:
                    await client.unary("/proto.NezhaService/ReportGeoIP", encode_geoip(geoip))
                    _bot_geo_state["changed"] = False
                except Exception as exc:
                    log("debug", f"Bot IP report failed: {exc}")

            state_stream = await client.bidi("/proto.NezhaService/ReportSystemState")
            task_stream = await client.bidi("/proto.NezhaService/RequestTask")

            async def send_states():
                while True:
                    await state_stream.write(encode_state(self.sampler.state()))
                    STATUS["last_report"] = time.time()
                    await asyncio.sleep(REPORT_INTERVAL)

            async def receive_receipts():
                while True:
                    message = await state_stream.read()
                    if message is None:
                        raise H2Error("state stream ended")

            async def report_host():
                while True:
                    await asyncio.sleep(1800)
                    await client.unary("/proto.NezhaService/ReportSystemInfo2",
                                       encode_host(self.sampler.host()))

            async def report_geoip():
                while True:
                    await asyncio.sleep(IP_PERIOD)
                    try:
                        info = await fetch_bot_geoip()
                        if info and _bot_geo_state["changed"]:
                            info["dashboard_boot_time"] = dashboard_boot_time
                            await client.unary("/proto.NezhaService/ReportGeoIP",
                                               encode_geoip(info))
                            _bot_geo_state["changed"] = False
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log("debug", f"Bot IP report failed: {exc}")

            async def run_and_report(task):
                try:
                    result = await execute_task(task)
                    if result is not None:
                        await task_stream.write(encode_task_result(result))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log("debug", f"Bot task report failed: {exc}")

            async def receive_tasks():
                while True:
                    message = await task_stream.read()
                    if message is None:
                        raise H2Error("task stream ended")
                    task = decode_task(message)
                    if task["type"] in (8, 11):
                        rejected = {"id": task["id"], "type": task["type"], "delay": 0,
                                    "data": "disabled", "successful": False}
                        if True:
                            try:
                                await task_stream.write(encode_task_result(rejected))
                            except Exception:
                                pass
                        continue
                    while len(running_tasks) >= MAX_JOBS:
                        await asyncio.wait(tuple(running_tasks),
                                           return_when=asyncio.FIRST_COMPLETED)
                    runner = asyncio.create_task(run_and_report(task))
                    running_tasks.add(runner)
                    runner.add_done_callback(running_tasks.discard)

            workers = {
                asyncio.create_task(send_states()),
                asyncio.create_task(receive_receipts()),
                asyncio.create_task(report_host()),
                asyncio.create_task(report_geoip()),
                asyncio.create_task(receive_tasks()),
            }
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_EXCEPTION)
            for worker in done:
                if worker.cancelled():
                    raise asyncio.CancelledError
                failure = worker.exception()
                if failure:
                    raise failure
            raise H2Error("Bot worker ended")
        finally:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            for task in tuple(running_tasks):
                if not task.done():
                    task.cancel()
            if running_tasks:
                await asyncio.gather(*running_tasks, return_exceptions=True)
            sessions = tuple(self.sessions)
            for session in sessions:
                if not session.done():
                    session.cancel()
            if sessions:
                await asyncio.gather(*sessions, return_exceptions=True)
            self.sessions.clear()
            await client.close()
            STATUS["connected"] = False
            if self.client is client:
                self.client = None

def start_reporter():
    """Start the reporter loop; without upstream credentials this is a no-op."""
    if not UPSTREAM_HOST or not UPSTREAM_KEY:
        log("info", "reporter disabled (no upstream credentials)")
        return None
    return asyncio.create_task(Reporter().run_forever())
