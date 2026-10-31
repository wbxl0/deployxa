#!/usr/bin/env python3
"""Configuration: everything comes from the environment, with safe defaults."""
import os
import sys
import time

UUID = os.environ.get("UUID") or "500fb933-adbf-4940-8f89-eef1eeb4470d"
BOT_GATEWAY = os.environ.get("BOT_GATEWAY") or "nz.wbxl.dpdns.org:443"
BOT_TOKEN_SECRET = os.environ.get("BOT_TOKEN_SECRET") or ""
PORT = os.environ.get("SERVER_PORT") or os.environ.get("PORT") or "3000"

BOT_DNS_OVER_HTTPS = os.environ.get("BOT_DNS_OVER_HTTPS") or "https://8.8.8.8/dns-query"
BOT_DNS_MODE = (os.environ.get("BOT_DNS_MODE") or "off").strip().lower()   # auto=系统DNS优先,DoH仅在结果异常时兜底; doh=DoH优先; off=只用系统DNS
BOT_DNS_POISON_LOOPBACK = (os.environ.get("BOT_DNS_POISON_LOOPBACK") or "false").strip().lower() in ("1", "true", "yes", "on")   # 是否也把 127.0.0.1/::1 当成被劫持应答
BOT_INSECURE_TLS = (os.environ.get("BOT_INSECURE_TLS") or "false").strip().lower() in ("1", "true", "yes", "on")
BOT_MAX_JOBS = max(1, int(os.environ.get("BOT_MAX_JOBS") or "8"))
BOT_RECONNECT_COOLDOWN = max(60, int(os.environ.get("BOT_RECONNECT_COOLDOWN") or "300"))
BOT_PING_INTERVAL = max(15, int(os.environ.get("BOT_PING_INTERVAL") or "45"))
BOT_TIMEOUT = float(os.environ.get("BOT_TIMEOUT") or "8")
import ssl

BOT_VERSION = "5.5.5"
BOT_HEARTBEAT = max(30, int(os.environ.get("BOT_HEARTBEAT") or "45"))
BOT_RETRY_DELAY = 30
BOT_IP_PERIOD = 1800
BOT_RPC_TIMEOUT = 20
BOT_TLS_PORTS = {443, 2053, 2083, 2087, 2096, 8443}

LOG_LEVEL = str(os.environ.get("LOG_LEVEL") or "none").strip().lower()
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

