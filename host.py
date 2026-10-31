#!/usr/bin/env python3
"""Deployxa app: host status reporter.

Readable, single-purpose: collect host metrics and report them to the upstream
over the upstream's gRPC/HTTP-2 protocol. No remote shell, no file manager, no
runtime downloads - the container only speaks to the upstream and serves a status
endpoint.
"""
import asyncio
import base64
import gzip
import ipaddress
import json
import os
import platform
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from config import *

def _read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return default




class SystemSampler:
    EXCLUDE_IFACES = ("lo", "tun", "docker", "veth", "br-", "vmbr", "vnet",
                      "kube", "Meta", "tailscale", "fw", "tap")

    def __init__(self):
        self.last_cpu = self._cpu_sample()
        self.last_net = self._net_counters()
        self.last_time = time.monotonic()

    @staticmethod
    def _cpu_sample():
        first = _read_text("/proc/stat").splitlines()
        if first and first[0].startswith("cpu "):
            values = [int(value) for value in first[0].split()[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            return sum(values), idle
        return 0, 0

    @classmethod
    def _net_counters(cls):
        received = sent = 0
        for line in _read_text("/proc/net/dev").splitlines()[2:]:
            if ":" not in line:
                continue
            name, values = line.split(":", 1)
            if any(token in name for token in cls.EXCLUDE_IFACES):
                continue
            fields = values.split()
            if len(fields) >= 9:
                received += int(fields[0])
                sent += int(fields[8])
        return received, sent

    @staticmethod
    def _meminfo():
        values = {}
        for line in _read_text("/proc/meminfo").splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            try:
                values[key] = int(raw.strip().split()[0]) * 1024
            except (ValueError, IndexError):
                pass
        return values

    @staticmethod
    def _boot_time():
        try:
            uptime = float(_read_text("/proc/uptime").split()[0])
        except (ValueError, IndexError):
            return 0
        return int(time.time() - uptime)

    @staticmethod
    def _physical_cores():
        pairs = set()
        processors = 0
        physical = core = None
        for line in _read_text("/proc/cpuinfo").splitlines():
            if line.startswith("processor"):
                processors += 1
            elif line.lower().startswith("physical id"):
                physical = line.split(":", 1)[1].strip()
            elif line.lower().startswith("core id"):
                core = line.split(":", 1)[1].strip()
                if physical is not None and core is not None:
                    pairs.add((physical, core))
        if pairs:
            return len(pairs)
        return processors or (os.cpu_count() or 0)

    @staticmethod
    def _conn_count(proto):
        return max(0, len(_read_text("/proc/net/" + proto).splitlines()) - 1)

    @staticmethod
    def _process_count():
        try:
            return sum(1 for name in os.listdir("/proc") if name.isdigit())
        except OSError:
            return 0

    def host(self):
        values = self._meminfo()
        try:
            disk_total = shutil.disk_usage(os.path.abspath(os.sep)).total
        except OSError:
            disk_total = 0
        cpu_brand = platform.processor() or "Unknown CPU"
        for line in _read_text("/proc/cpuinfo").splitlines():
            if line.lower().startswith(("model name", "hardware")) and ":" in line:
                cpu_brand = line.split(":", 1)[1].strip()
                break
        return {
            "platform": platform.system(),
            "platform_version": platform.release(),
            "cpu": [f"{cpu_brand} {self._physical_cores()} Physical Core"],
            "mem_total": values.get("MemTotal", 0),
            "disk_total": disk_total,
            "swap_total": values.get("SwapTotal", 0),
            "arch": platform.machine(),
            "virtualization": "",
            "boot_time": self._boot_time(),
            "version": VERSION,
        }

    def state(self):
        current = self._cpu_sample()
        total_delta = current[0] - self.last_cpu[0]
        idle_delta = current[1] - self.last_cpu[1]
        cpu = max(0.0, min(100.0, (total_delta - idle_delta) * 100 / total_delta)) if total_delta > 0 else 0.0
        self.last_cpu = current

        values = self._meminfo()
        mem_total = values.get("MemTotal", 0)
        mem_available = values.get("MemAvailable")
        if mem_available is None:
            mem_available = (values.get("MemFree", 0) + values.get("Buffers", 0)
                             + values.get("Cached", 0))
        swap_total = values.get("SwapTotal", 0)

        try:
            disk_used = shutil.disk_usage(os.path.abspath(os.sep)).used
        except OSError:
            disk_used = 0

        current_net = self._net_counters()
        now = time.monotonic()
        elapsed = max(0.001, now - self.last_time)
        in_speed = max(0, int((current_net[0] - self.last_net[0]) / elapsed))
        out_speed = max(0, int((current_net[1] - self.last_net[1]) / elapsed))
        self.last_net = current_net
        self.last_time = now

        load1 = load5 = load15 = 0.0
        loadavg = _read_text("/proc/loadavg").split()
        if len(loadavg) >= 3:
            try:
                load1, load5, load15 = float(loadavg[0]), float(loadavg[1]), float(loadavg[2])
            except ValueError:
                pass

        boot_time = self._boot_time()
        return {
            "cpu": cpu,
            "mem_used": max(0, mem_total - mem_available),
            "swap_used": max(0, swap_total - values.get("SwapFree", 0)),
            "disk_used": disk_used,
            "net_in_transfer": current_net[0],
            "net_out_transfer": current_net[1],
            "net_in_speed": in_speed,
            "net_out_speed": out_speed,
            "uptime": max(0, int(time.time() - boot_time)) if boot_time else 0,
            "load1": load1, "load5": load5, "load15": load15,
            "tcp_conn_count": self._conn_count("tcp") + self._conn_count("tcp6"),
            "udp_conn_count": self._conn_count("udp") + self._conn_count("udp6"),
            "process_count": self._process_count(),
        }




def _dns_labels(host):
    out = b""
    for part in host.rstrip(".").split("."):
        raw = part.encode("idna")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _build_dns_query(host, record_type):
    transaction = os.urandom(2)
    qtype = 1 if record_type == "A" else 28
    header = transaction + struct.pack("!HHHHH", 0x0100, 1, 0, 0, 0)
    return header + _dns_labels(host) + struct.pack("!HH", qtype, 1)


def _parse_dns_answer(data, transaction, answer_type):
    if len(data) < 12 or data[:2] != transaction:
        return None
    _, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", data[:12])
    if flags & 0x000F:
        return None
    offset = 12
    for _ in range(questions):
        offset = skip_dns_name(data, offset) + 4
    for _ in range(answers):
        offset = skip_dns_name(data, offset)
        if offset + 10 > len(data):
            return None
        rtype, rclass, _, length = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        value = data[offset:offset + length]
        offset += length
        if rclass == 1 and rtype == answer_type and length in (4, 16):
            family = socket.AF_INET if answer_type == 1 else socket.AF_INET6
            return socket.inet_ntop(family, value)
    return None


def _parse_dns_answers(data, transaction, answer_type):
    """返回应答里的全部匹配地址（_parse_dns_answer 只保留第一条）。

    被劫持的解析器常把 0.0.0.0 放在第一条，只取第一条就会白白丢掉后面的真实地址。
    """
    if len(data) < 12 or data[:2] != transaction:
        return []
    _, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", data[:12])
    if flags & 0x000F:
        return []
    offset = 12
    for _ in range(questions):
        try:
            offset = skip_dns_name(data, offset) + 4
        except ValueError:
            return []
    found = []
    for _ in range(answers):
        try:
            offset = skip_dns_name(data, offset)
        except ValueError:
            return found
        if offset + 10 > len(data):
            return found
        rtype, rclass, _, length = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        value = data[offset:offset + length]
        offset += length
        if rclass == 1 and rtype == answer_type and length in (4, 16):
            family = socket.AF_INET if answer_type == 1 else socket.AF_INET6
            try:
                found.append(socket.inet_ntop(family, value))
            except ValueError:
                continue
    return found


def is_unusable_address(address):
    """被劫持/污染应答的典型特征（0.0.0.0 / ::，或根本不是 IP）返回 True。

    这类地址永远不可能是真实的连接目标，必须当成“解析失败”交给 DoH 兜底，
    否则劫持者“成功”返回 0.0.0.0 时 DoH 永远走不到。
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True
    if parsed.is_unspecified:
        return True
    if DNS_POISON_LOOPBACK and parsed.is_loopback:
        return True
    return False


_doh_state = threading.local()


def resolve_via_doh(host, timeout=RPC_TIMEOUT, want_ipv6=True):
    """依次尝试 DNS_FALLBACK_URL 里的每个端点（支持 dns-query 线格式与 dns-json）。"""
    if getattr(_doh_state, "busy", False):
        return ""
    _doh_state.busy = True
    try:
        endpoints = [item.strip() for item in (DNS_FALLBACK_URL).split(",") if item.strip()]
        record_types = (("A", 1), ("AAAA", 28)) if want_ipv6 else (("A", 1),)
        for record_type, answer_type in record_types:
            for endpoint in endpoints:
                try:
                    if endpoint.rstrip("/").endswith("/dns-query"):
                        query = _build_dns_query(host, record_type)
                        request = urllib.request.Request(
                            endpoint, data=query, method="POST",
                            headers={"Accept": "application/dns-message",
                                     "Content-Type": "application/dns-message",
                                     "User-Agent": "Mozilla/5.0"},
                        )
                        with urllib.request.urlopen(request, timeout=timeout) as response:
                            body = response.read()
                        addresses = _parse_dns_answers(body, query[:2], answer_type)
                    else:
                        separator = "&" if "?" in endpoint else "?"
                        url = f"{endpoint}{separator}name={_urlquote(host)}&type={record_type}"
                        request = urllib.request.Request(
                            url, headers={"Accept": "application/dns-json", "User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(request, timeout=timeout) as response:
                            payload = json.loads(response.read().decode("utf-8", "replace"))
                        addresses = [item.get("data", "").rstrip(".") for item in payload.get("Answer") or ()
                                     if item.get("type") == answer_type and item.get("data")]
                    address = next((item for item in addresses
                                    if item and not is_unusable_address(item)), "")
                    if address:
                        log("debug", f"DoH resolved {host} to {address}")
                        return address
                except Exception as exc:
                    log("debug", f"DoH query failed via {endpoint}: {exc}")
        return ""
    finally:
        _doh_state.busy = False


_original_getaddrinfo = socket.getaddrinfo


def _has_usable_address(results):
    """所有应答都是被劫持/不可用地址（如 0.0.0.0）时返回 False。"""
    for family, _, _, _, sockaddr in results:
        if family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(sockaddr, tuple):
            return True
        if not is_unusable_address(sockaddr[0]):
            return True
    return False


def _doh_getaddrinfo(name, port, args, kwargs):
    """先用 DoH 解析 name，再把地址交给真正的 getaddrinfo。"""
    address = resolve_via_doh(name)
    if not address:
        return None
    try:
        return _original_getaddrinfo(address, port, *args, **kwargs)
    except socket.gaierror as error:
        log("debug", f"getaddrinfo failed for DoH address {address}: {error}")
        return None


def _lookup_name(host):
    """返回需要解析的主机名；host 本身就是 IP 时返回 None。"""
    name = host
    if isinstance(name, (bytes, bytearray)):
        try:
            name = name.decode("idna")
        except (UnicodeError, LookupError):
            return None
    if not name:
        return None
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return str(name)
    return None


def getaddrinfo_with_doh_fallback(host, port, *args, **kwargs):
    """系统解析优先，出问题才用 DoH。

    DNS_MODE 决定策略：
      auto（默认）- 系统 DNS 优先，只有系统应答缺失或被劫持成不可用地址时才用 DoH
      doh         - DoH 优先，系统 DNS 只作最后兜底（本机解析器坏了/被过滤时用）
      off         - 只用系统 DNS，从不使用 DoH

    健康的系统应答原样返回，行为与延迟都不变；DoH 拿到的地址会交回真正的
    getaddrinfo，socket 层不变，TLS SNI 仍用原主机名（证书保持有效）。
    """
    name = _lookup_name(host)

    if name and DNS_MODE == "doh":
        result = _doh_getaddrinfo(name, port, args, kwargs)
        if result is not None:
            return result

    first_error = None
    try:
        results = _original_getaddrinfo(host, port, *args, **kwargs)
    except socket.gaierror as error:
        first_error = error
        results = None
    else:
        if _has_usable_address(results):
            return results
        log("debug", f"system DNS returned an unusable address for {host}")

    if name and DNS_MODE in ("auto", "doh"):
        result = _doh_getaddrinfo(name, port, args, kwargs)
        if result is not None:
            return result

    if first_error is not None:
        raise first_error
    return results


socket.getaddrinfo = getaddrinfo_with_doh_fallback


def _is_ip_literal(value):
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, value)
            return True
        except OSError:
            continue
    return False


async def resolve_bot_gateway(host):
    """网关主机解析：系统 DNS 优先，只有结果不可用时才走 DoH。

    被劫持的解析器会“成功”返回 0.0.0.0 而不是抛错，所以采信前必须校验，
    否则 DoH 永远走不到。
    """
    if _is_ip_literal(host):
        return host
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        for family, _, _, _, sockaddr in infos:
            if family in (socket.AF_INET, socket.AF_INET6):
                address = sockaddr[0]
                if not is_unusable_address(address):
                    return address
    except OSError:
        pass
    address = await asyncio.to_thread(resolve_via_doh, host)
    if address and not is_unusable_address(address):
        log("debug", f"DoH resolved {host} -> {address}")
        return address
    return host




