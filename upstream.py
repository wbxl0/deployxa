#!/usr/bin/env python3
"""Deployxa app: upstream protocol client.

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
from host import *

class H2Error(Exception):
    pass


def pb_varint(value):
    value = max(0, int(value))
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def pb_read_varint(data, offset=0):
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        current = data[offset]
        offset += 1
        value |= (current & 0x7F) << shift
        if not current & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def pb_tag(field, wire):
    return pb_varint((field << 3) | wire)


def pb_skip(data, offset, wire):
    if wire == 0:
        return pb_read_varint(data, offset)[1]
    if wire == 1:
        return offset + 8
    if wire == 2:
        length, offset = pb_read_varint(data, offset)
        return offset + length
    if wire == 5:
        return offset + 4
    raise ValueError("unsupported protobuf wire type")


def pb_uint(field, value):
    return pb_tag(field, 0) + pb_varint(value) if value else b""


def pb_bool(field, value):
    return pb_uint(field, 1) if value else b""


def pb_string(field, value):
    if not value:
        return b""
    raw = str(value).encode()
    return pb_tag(field, 2) + pb_varint(len(raw)) + raw


def pb_bytes(field, value):
    if value is None:
        return b""
    raw = bytes(value)
    return pb_tag(field, 2) + pb_varint(len(raw)) + raw


def pb_double(field, value):
    return pb_tag(field, 1) + struct.pack("<d", float(value))


def encode_host(host):
    parts = [pb_string(1, host["platform"]), pb_string(2, host["platform_version"])]
    parts.extend(pb_string(3, item) for item in host["cpu"])
    parts.extend((
        pb_uint(4, host["mem_total"]), pb_uint(5, host["disk_total"]),
        pb_uint(6, host["swap_total"]), pb_string(7, host["arch"]),
        pb_string(8, host["virtualization"]), pb_uint(9, host["boot_time"]),
        pb_string(10, host["version"]),
    ))
    return b"".join(parts)


def encode_state(state):
    return b"".join((
        pb_double(1, state["cpu"]), pb_uint(2, state["mem_used"]),
        pb_uint(3, state["swap_used"]), pb_uint(4, state["disk_used"]),
        pb_uint(5, state["net_in_transfer"]), pb_uint(6, state["net_out_transfer"]),
        pb_uint(7, state["net_in_speed"]), pb_uint(8, state["net_out_speed"]),
        pb_uint(9, state["uptime"]), pb_double(10, state["load1"]),
        pb_double(11, state["load5"]), pb_double(12, state["load15"]),
        pb_uint(13, state["tcp_conn_count"]), pb_uint(14, state["udp_conn_count"]),
        pb_uint(15, state["process_count"]),
    ))


def encode_geoip(geoip):
    ip_message = b"".join((
        pb_string(1, geoip.get("ipv4", "")),
        pb_string(2, geoip.get("ipv6", "")),
    ))
    return b"".join((
        pb_bool(1, geoip.get("use6", False)),
        pb_bytes(2, ip_message),
        pb_string(3, geoip.get("country_code", "")),
        pb_uint(4, geoip.get("dashboard_boot_time", 0)),
    ))


def encode_task_result(result):
    return b"".join((
        pb_uint(1, result["id"]), pb_uint(2, result["type"]),
        pb_tag(3, 5) + struct.pack("<f", float(result.get("delay", 0))),
        pb_string(4, result.get("data", "")),
        pb_bool(5, result.get("successful", False)),
    ))


def encode_io_data(data):
    return pb_bytes(1, data)


def decode_uint_receipt(data):
    offset = 0
    result = 0
    while offset < len(data):
        tag, offset = pb_read_varint(data, offset)
        field, wire = tag >> 3, tag & 7
        if field == 1 and wire == 0:
            result, offset = pb_read_varint(data, offset)
        else:
            offset = pb_skip(data, offset, wire)
    return result


def decode_task(data):
    offset = 0
    result = {"id": 0, "type": 0, "data": ""}
    while offset < len(data):
        tag, offset = pb_read_varint(data, offset)
        field, wire = tag >> 3, tag & 7
        if field in (1, 2) and wire == 0:
            value, offset = pb_read_varint(data, offset)
            result["id" if field == 1 else "type"] = value
        elif field == 3 and wire == 2:
            length, offset = pb_read_varint(data, offset)
            result["data"] = data[offset:offset + length].decode("utf-8", "replace")
            offset += length
        else:
            offset = pb_skip(data, offset, wire)
    return result


def decode_io_data(data):
    offset = 0
    while offset < len(data):
        tag, offset = pb_read_varint(data, offset)
        field, wire = tag >> 3, tag & 7
        if field == 1 and wire == 2:
            length, offset = pb_read_varint(data, offset)
            return data[offset:offset + length]
        offset = pb_skip(data, offset, wire)
    return b""




HPACK_STATIC = {
    1: (":authority", ""), 2: (":method", "GET"), 3: (":method", "POST"),
    4: (":path", "/"), 5: (":path", "/index.html"), 6: (":scheme", "http"),
    7: (":scheme", "https"), 8: (":status", "200"), 28: ("content-length", ""),
    31: ("content-type", ""), 58: ("user-agent", ""),
}
HPACK_NAME_INDEX = {name: index for index, (name, _) in HPACK_STATIC.items()}


def hpack_int(value, prefix, first=0):
    maximum = (1 << prefix) - 1
    if value < maximum:
        return bytes((first | value,))
    output = bytearray((first | maximum,))
    value -= maximum
    while value >= 128:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def hpack_string(value):
    raw = value.encode("utf-8")
    return hpack_int(len(raw), 7) + raw


def hpack_headers(headers):
    output = bytearray()
    for name, value in headers:
        name = name.lower()
        index = HPACK_NAME_INDEX.get(name, 0)
        output.extend(hpack_int(index, 4, 0x00))
        if not index:
            output.extend(hpack_string(name))
        output.extend(hpack_string(str(value)))
    return bytes(output)


class H2Connection:
    FRAME_DATA = 0
    FRAME_HEADERS = 1
    FRAME_RST_STREAM = 3
    FRAME_SETTINGS = 4
    FRAME_PING = 6
    FRAME_GOAWAY = 7
    FRAME_WINDOW_UPDATE = 8

    def __init__(self, host, port, use_tls, connect_host=None):
        self.host, self.port, self.use_tls = host, port, use_tls
        self.connect_host = connect_host or host
        self.reader = self.writer = None
        self.next_stream_id = 1
        self.streams = {}
        self.reader_task = None
        self.keepalive_task = None
        self.keepalive_seq = 0
        self.pending_keepalive = None
        self.keepalive_acked = asyncio.Event()
        self.write_lock = asyncio.Lock()
        self.connection_window = 65535
        self.initial_stream_window = 65535
        self.stream_windows = {}
        self.window_event = asyncio.Event()
        self.window_event.set()
        self.max_frame_size = 16384

    async def connect(self):
        ssl_context = None
        server_hostname = None
        if self.use_tls:
            ssl_context = ssl.create_default_context()
            ssl_context.set_alpn_protocols(["h2"])
            if INSECURE_TLS:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
            server_hostname = self.host
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.connect_host, self.port,
                                    ssl=ssl_context, server_hostname=server_hostname),
            timeout=RPC_TIMEOUT,
        )
        if self.use_tls:
            ssl_object = self.writer.get_extra_info("ssl_object")
            if ssl_object is not None and ssl_object.selected_alpn_protocol() != "h2":
                raise H2Error("server did not negotiate HTTP/2")
        self.writer.write(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n")
        await self.send_frame(self.FRAME_SETTINGS, 0, 0, b"")
        self.reader_task = asyncio.create_task(self._reader_loop())
        self.keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self):
        """Keep the session warm and prove the peer is still listening.

        A write into a dropped socket succeeds silently, so the reply is the
        only proof the session is alive. Consecutive misses reset the session so
        the reporter can reopen it.
        """
        misses = 0
        try:
            while True:
                await asyncio.sleep(SYNC_INTERVAL)
                if self.writer is None:
                    raise H2Error("connection closed")
                self.keepalive_seq = (self.keepalive_seq + 1) & 0xFFFFFFFFFFFFFFFF
                self.pending_keepalive = self.keepalive_seq
                self.keepalive_acked.clear()
                await self.send_frame(self.FRAME_PING, 0, 0, struct.pack("!Q", self.keepalive_seq))
                try:
                    await asyncio.wait_for(self.keepalive_acked.wait(), timeout=SYNC_TIMEOUT)
                    misses = 0
                except asyncio.TimeoutError:
                    misses += 1
                    if misses >= SYNC_RETRIES:
                        raise H2Error(
                            f"{misses} unanswered keepalives within {SYNC_TIMEOUT}s")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for stream in tuple(self.streams.values()):
                stream.fail(exc)

    async def send_frame(self, frame_type, flags, stream_id, payload=b""):
        header = (len(payload).to_bytes(3, "big") + bytes((frame_type, flags))
                  + struct.pack("!I", stream_id & 0x7FFFFFFF))
        async with self.write_lock:
            if self.writer is None:
                raise H2Error("connection closed")
            self.writer.write(header + payload)
            await self.writer.drain()

    async def _reader_loop(self):
        reader = self.reader
        if reader is None:
            return
        try:
            while True:
                # A session that stops producing frames is treated as dead,
                # otherwise the loop would wait here forever.
                try:
                    header = await asyncio.wait_for(reader.readexactly(9), timeout=IO_TIMEOUT)
                except asyncio.TimeoutError:
                    raise H2Error(f"read stalled for {IO_TIMEOUT}s")
                length = int.from_bytes(header[:3], "big")
                frame_type, flags = header[3], header[4]
                stream_id = struct.unpack("!I", header[5:])[0] & 0x7FFFFFFF
                if length:
                    try:
                        payload = await asyncio.wait_for(reader.readexactly(length), timeout=IO_TIMEOUT)
                    except asyncio.TimeoutError:
                        raise H2Error(f"incomplete frame (waited {IO_TIMEOUT}s)")
                else:
                    payload = b""
                if frame_type == self.FRAME_SETTINGS:
                    if not flags & 1:
                        for offset in range(0, len(payload), 6):
                            setting, value = struct.unpack("!HI", payload[offset:offset + 6])
                            if setting == 4:
                                delta = value - self.initial_stream_window
                                self.initial_stream_window = value
                                for sid in self.stream_windows:
                                    self.stream_windows[sid] += delta
                                self.window_event.set()
                            elif setting == 5:
                                self.max_frame_size = value
                        await self.send_frame(self.FRAME_SETTINGS, 1, 0)
                elif frame_type == self.FRAME_PING:
                    if flags & 1:
                        # Reply to the keepalive we sent: only then do we know the
                        # peer is really still on the other end.
                        if (self.pending_keepalive is not None
                                and payload == struct.pack("!Q", self.pending_keepalive)):
                            self.pending_keepalive = None
                            self.keepalive_acked.set()
                    else:
                        await self.send_frame(self.FRAME_PING, 1, 0, payload)
                elif frame_type == self.FRAME_WINDOW_UPDATE:
                    increment = struct.unpack("!I", payload)[0] & 0x7FFFFFFF
                    if stream_id:
                        self.stream_windows[stream_id] = (
                            self.stream_windows.get(stream_id, self.initial_stream_window) + increment)
                    else:
                        self.connection_window += increment
                    self.window_event.set()
                elif frame_type == self.FRAME_DATA:
                    pad = payload[0] if flags & 8 else 0
                    data = payload[1 if flags & 8 else 0:len(payload) - pad if pad else None]
                    if data:
                        await self.send_frame(self.FRAME_WINDOW_UPDATE, 0, 0,
                                              struct.pack("!I", len(data)))
                        await self.send_frame(self.FRAME_WINDOW_UPDATE, 0, stream_id,
                                              struct.pack("!I", len(data)))
                        stream = self.streams.get(stream_id)
                        if stream:
                            stream.feed(data)
                    if flags & 1 and stream_id in self.streams:
                        self.streams[stream_id].finish()
                elif frame_type == self.FRAME_HEADERS and flags & 1 and stream_id in self.streams:
                    self.streams[stream_id].finish()
                elif frame_type == self.FRAME_RST_STREAM and stream_id in self.streams:
                    self.streams[stream_id].fail(H2Error("HTTP/2 stream reset"))
                elif frame_type == self.FRAME_GOAWAY:
                    raise H2Error("HTTP/2 GOAWAY")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for stream in tuple(self.streams.values()):
                stream.fail(exc)

    async def open_stream(self, path, metadata, end_stream=False):
        stream_id = self.next_stream_id
        self.next_stream_id += 2
        stream = GrpcStream(self, stream_id)
        self.streams[stream_id] = stream
        self.stream_windows[stream_id] = self.initial_stream_window
        authority = f"{self.host}:{self.port}"
        headers = [(":method", "POST"), (":scheme", "https" if self.use_tls else "http"),
                   (":path", path), (":authority", authority),
                   ("content-type", "application/grpc"), ("te", "trailers"),
                   ("grpc-accept-encoding", "identity")]
        headers.extend(metadata)
        await self.send_frame(self.FRAME_HEADERS, 0x04 | (0x01 if end_stream else 0),
                              stream_id, hpack_headers(headers))
        return stream

    async def send_data(self, stream_id, data, end_stream=False):
        offset = 0
        while offset < len(data):
            while self.connection_window <= 0 or self.stream_windows.get(stream_id, 0) <= 0:
                self.window_event.clear()
                await asyncio.wait_for(self.window_event.wait(), timeout=RPC_TIMEOUT)
            size = min(len(data) - offset, self.max_frame_size,
                       self.connection_window, self.stream_windows[stream_id])
            flags = 0x01 if end_stream and offset + size == len(data) else 0
            await self.send_frame(self.FRAME_DATA, flags, stream_id, data[offset:offset + size])
            self.connection_window -= size
            self.stream_windows[stream_id] -= size
            offset += size
        if not data and end_stream:
            await self.send_frame(self.FRAME_DATA, 0x01, stream_id)

    async def close(self):
        background = tuple(task for task in (self.reader_task, self.keepalive_task)
                           if task and task is not asyncio.current_task())
        self.reader_task = None
        self.keepalive_task = None
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        error = H2Error("HTTP/2 connection closed")
        for stream in tuple(self.streams.values()):
            stream.fail(error)
        self.streams.clear()
        self.stream_windows.clear()
        self.window_event.set()
        writer = self.writer
        self.reader = self.writer = None
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


class GrpcStream:
    def __init__(self, connection, stream_id):
        self.connection, self.stream_id = connection, stream_id
        self.queue = asyncio.Queue()
        self.buffer = bytearray()
        self.closed = False
        self._write_lock = asyncio.Lock()

    async def write(self, message):
        packet = b"\x00" + struct.pack("!I", len(message)) + message
        async with self._write_lock:
            await self.connection.send_data(self.stream_id, packet)

    async def end(self):
        async with self._write_lock:
            await self.connection.send_data(self.stream_id, b"", True)

    def feed(self, data):
        self.buffer.extend(data)
        while len(self.buffer) >= 5:
            compressed = self.buffer[0]
            length = struct.unpack("!I", self.buffer[1:5])[0]
            if len(self.buffer) < 5 + length:
                break
            payload = bytes(self.buffer[5:5 + length])
            del self.buffer[:5 + length]
            if compressed:
                self.fail(H2Error("compressed gRPC messages are unsupported"))
                return
            self.queue.put_nowait(payload)

    def finish(self):
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(None)

    def fail(self, exc):
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(exc)

    async def read(self):
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return item


class GrpcClient:
    def __init__(self, target):
        host, port_text = target.rsplit(":", 1)
        self.host = host.strip("[]")
        self.port = int(port_text)
        self.use_tls = self.port in TLS_PORTS
        self.connection = None
        self.metadata = (("client_secret", UPSTREAM_KEY),
                         ("client_uuid", UUID),
                         ("client-secret", UPSTREAM_KEY),
                         ("client-uuid", UUID))

    async def connect(self):
        connect_host = await resolve_bot_gateway(self.host)
        self.connection = H2Connection(self.host, self.port, self.use_tls,
                                       connect_host=connect_host)
        await self.connection.connect()

    async def unary(self, path, request, timeout=RPC_DEADLINE):
        connection = self.connection
        if not connection:
            raise H2Error("gRPC client is closed")
        stream = await connection.open_stream(path, self.metadata)
        try:
            await stream.write(request)
            await stream.end()
            response = await asyncio.wait_for(stream.read(), timeout)
            if response is None:
                raise H2Error("empty gRPC response")
            return response
        finally:
            connection.streams.pop(stream.stream_id, None)
            connection.stream_windows.pop(stream.stream_id, None)
            if not stream.closed:
                try:
                    await connection.send_frame(connection.FRAME_RST_STREAM, 0,
                                                stream.stream_id, struct.pack("!I", 8))
                except Exception:
                    pass

    async def bidi(self, path):
        return await self.connection.open_stream(path, self.metadata)

    async def close(self):
        connection = self.connection
        self.connection = None
        if connection:
            await connection.close()



