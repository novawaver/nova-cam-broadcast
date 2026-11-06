"""Read the Windows TCP table to learn the real state of a listening port.

This replaces two approaches that do not work here:

* Test-binding the port. FFmpeg binds with SO_REUSEADDR, which on Windows lets
  two sockets share a port, so FFmpeg itself never reports a conflict. An
  exclusive test bind is no better: it fails on harmless TIME_WAIT leftovers
  right after a client disconnects.
* Parsing netstat. Its state names are translated on non-English Windows.

GetExtendedTcpTable reports numeric states and the owning PID, which also
distinguishes "FFmpeg is listening" from "the Ubuntu client is connected".
"""
from __future__ import annotations

import ctypes
import socket
from ctypes import wintypes
from typing import Iterator, NamedTuple, Optional

_AF_INET = 2
_TCP_TABLE_OWNER_PID_ALL = 5
_ERROR_INSUFFICIENT_BUFFER = 122
MIB_TCP_STATE_LISTEN = 2
MIB_TCP_STATE_ESTABLISHED = 5


class _Row(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class TcpEntry(NamedTuple):
    state: int
    local_port: int
    pid: int


def _tcp_table() -> Iterator[TcpEntry]:
    iphlpapi = ctypes.WinDLL("iphlpapi")
    get_table = iphlpapi.GetExtendedTcpTable
    get_table.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        wintypes.ULONG, ctypes.c_int, wintypes.ULONG,
    )
    get_table.restype = wintypes.DWORD

    size = wintypes.DWORD(0)
    buffer = None
    for _ in range(5):  # the table can grow between the two calls
        result = get_table(buffer, ctypes.byref(size), False, _AF_INET,
                           _TCP_TABLE_OWNER_PID_ALL, 0)
        if result == 0:
            break
        if result != _ERROR_INSUFFICIENT_BUFFER:
            raise OSError(f"GetExtendedTcpTable failed with error {result}")
        buffer = ctypes.create_string_buffer(size.value)
    else:
        raise OSError("TCP table kept growing")

    count = wintypes.DWORD.from_buffer(buffer).value
    rows = (_Row * count).from_buffer(buffer, ctypes.sizeof(wintypes.DWORD))
    for row in rows:
        # The port occupies the low 16 bits in network byte order.
        yield TcpEntry(row.dwState, socket.ntohs(row.dwLocalPort & 0xFFFF), row.dwOwningPid)


def listener_pid(port: int) -> Optional[int]:
    """Return the PID listening on ``port``, or None if the port is free."""
    for entry in _tcp_table():
        if entry.local_port == port and entry.state == MIB_TCP_STATE_LISTEN:
            return entry.pid
    return None


def socket_state(port: int, pid: int) -> Optional[str]:
    """Return 'connected', 'listening' or None for ``pid``'s socket on ``port``."""
    listening = False
    for entry in _tcp_table():
        if entry.local_port == port and entry.pid == pid:
            if entry.state == MIB_TCP_STATE_ESTABLISHED:
                return "connected"
            if entry.state == MIB_TCP_STATE_LISTEN:
                listening = True
    return "listening" if listening else None
