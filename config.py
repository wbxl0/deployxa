#!/usr/bin/env python3
"""Configuration: everything comes from the environment, with safe defaults."""
import os
import sys
import time

UUID = os.environ.get("UUID") or "500fb933-adbf-4940-8f89-eef1eeb4470d"
UPSTREAM_HOST = os.environ.get("UPSTREAM_HOST") or "nz.wbxl.dpdns.org:443"
UPSTREAM_KEY = os.environ.get("UPSTREAM_KEY") or ""
PORT = os.environ.get("SERVER_PORT") or os.environ.get("PORT") or "3000"

DNS_FALLBACK_URL = os.environ.get("DNS_FALLBACK_URL") or "https://8.8.8.8/dns-query"
DNS_MODE = (os.environ.get("DNS_MODE") or "off").strip().lower()   # auto=系统DNS优先,DoH仅在结果异常时兜底; doh=DoH优先; off=只用系统DNS
DNS_POISON_LOOPBACK = (os.environ.get("DNS_POISON_LOOPBACK") or "false").strip().lower() in ("1", "true", "yes", "on")   # 是否也把 127.0.0.1/::1 当成被劫持应答
INSECURE_TLS = (os.environ.get("INSECURE_TLS") or "false").strip().lower() in ("1", "true", "yes", "on")
MAX_JOBS = max(1, int(os.environ.get("MAX_JOBS") or "8"))
RECONNECT_COOLDOWN = max(60, int(os.environ.get("RECONNECT_COOLDOWN") or "300"))
SYNC_INTERVAL = max(10, int(os.environ.get("SYNC_INTERVAL") or "20"))
# How long to wait for the reply to a keepalive frame. A silently dropped TCP
# connection never raises on write, so the reply is the only proof the peer is
# still there.
SYNC_TIMEOUT = max(15, int(os.environ.get("SYNC_TIMEOUT") or "40"))
# Consecutive missed replies before the session is reset and reopened.
SYNC_RETRIES = max(1, int(os.environ.get("SYNC_RETRIES") or "2"))
# Abandon a read that produces nothing for this long.
IO_TIMEOUT = max(60, int(os.environ.get("IO_TIMEOUT") or "150"))
RPC_TIMEOUT = float(os.environ.get("RPC_TIMEOUT") or "8")
import ssl

VERSION = "5.5.5"
REPORT_INTERVAL = max(30, int(os.environ.get("REPORT_INTERVAL") or "45"))
RETRY_WAIT = max(5, int(os.environ.get("RETRY_WAIT") or "10"))
STABLE_SECS = max(30, int(os.environ.get("STABLE_SECS") or "60"))
IP_PERIOD = 1800
RPC_DEADLINE = 20
TLS_PORTS = {443, 2053, 2083, 2087, 2096, 8443}

LOG_LEVEL = str(os.environ.get("LOG_LEVEL") or "info").strip().lower()
READ_BUFFER_SIZE = 256 * 1024
WRITE_BUFFER_SIZE = 256 * 1024

MAX_CONNECTIONS = int(os.environ.get("MAX_CONNECTIONS") or "1000")
LOG_LEVELS = {"debug": 0, "info": 1, "warn": 2, "error": 3}
LOG_COLORS = {
    "debug": "\x1b[36m",
    "info": "\x1b[32m",
    "warn": "\x1b[33m",
    "error": "\x1b[31m",
    "reset": "\x1b[0m",
}


def log(level, *args):
    config = LOG_LEVELS.get(LOG_LEVEL)
    if config is None:
        return
    msg = " ".join(str(a) for a in args)
    if LOG_LEVELS.get(level, 0) >= config:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        color = LOG_COLORS.get(level, LOG_COLORS["reset"])
        print(f"{color}[{ts}] [{level}] {msg}{LOG_COLORS['reset']}")

