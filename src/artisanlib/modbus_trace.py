#
# ABOUT
# Optional wire-level trace of all MODBUS traffic (every frame sent and received, with timestamps
# and a best-effort decode of RTU frames) written as JSON Lines, to document and replay the
# communication with the machine.
#
# Enabled by setting the environment variable ARTISAN_MODBUS_TRACE=1.
# Files are written to ARTISAN_SESSION_DIR (default: ~/artisan-sessions) as modbus-<timestamp>.jsonl.

# LICENSE
# This program or module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import json
import time
import datetime
import logging
import threading
from pathlib import Path
from typing import Final, Any, TextIO

_log: Final[logging.Logger] = logging.getLogger(__name__)

_lock: Final[threading.Lock] = threading.Lock()
_file: TextIO|None = None
_framing: str = 'rtu' # 'rtu' (serial), 'tcp' (TCP/UDP) or 'raw' (ASCII; logged but not decoded)
_last_tx: float = 0 # time of the last frame sent, to compute response latencies


def enabled() -> bool:
    return os.environ.get('ARTISAN_MODBUS_TRACE', '').strip().lower() in {'1', 'true', 'yes', 'on'}


def session_dir() -> Path:
    d = os.environ.get('ARTISAN_SESSION_DIR', '').strip()
    return Path(d) if d else Path.home() / 'artisan-sessions'


def crc16(data:bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


# best-effort decode of a MODBUS RTU frame (direction: 'tx' request, 'rx' response)
def decode_rtu(direction:str, frame:bytes) -> dict[str, Any]:
    if len(frame) < 4:
        return {'partial': True}
    d = decode_pdu(direction, frame[0], frame[1], frame[2:-2])
    d['crc_ok'] = crc16(frame[:-2]) == int.from_bytes(frame[-2:], 'little')
    return d


# best-effort decode of a MODBUS TCP/UDP frame (MBAP header: transaction id, protocol id, length, unit id)
def decode_tcp(direction:str, frame:bytes) -> dict[str, Any]:
    if len(frame) < 8:
        return {'partial': True}
    d = decode_pdu(direction, frame[6], frame[7], frame[8:])
    d['tid'] = int.from_bytes(frame[0:2], 'big')
    return d


def decode_pdu(direction:str, slave:int, function:int, body:bytes) -> dict[str, Any]:
    d:dict[str, Any] = {'slave': slave, 'fc': function & 0x7F}
    if function & 0x80: # exception response
        d['exception'] = body[0] if body else None
    elif direction == 'tx' and len(body) == 4: # read request (fc 1-4) or single write (fc 5, 6)
        d['register'] = int.from_bytes(body[0:2], 'big')
        d['count' if d['fc'] in {1, 2, 3, 4} else 'value'] = int.from_bytes(body[2:4], 'big')
    elif direction == 'tx' and d['fc'] == 16 and len(body) >= 5:
        d['register'] = int.from_bytes(body[0:2], 'big')
        d['values'] = [int.from_bytes(body[5+i:7+i], 'big') for i in range(0, body[4], 2)]
    elif direction == 'rx' and d['fc'] in {3, 4} and body and len(body) == body[0] + 1:
        d['registers'] = [int.from_bytes(body[1+i:3+i], 'big') for i in range(0, body[0], 2)]
    elif direction == 'rx' and d['fc'] in {5, 6} and len(body) == 4: # echo of the single write
        d['register'] = int.from_bytes(body[0:2], 'big')
        d['value'] = int.from_bytes(body[2:4], 'big')
    return d


def _write(record:dict[str, Any]) -> None:
    global _file # pylint: disable=global-statement
    with _lock:
        if _file is None:
            directory = session_dir()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f'modbus-{datetime.datetime.now(datetime.UTC).astimezone():%Y%m%d-%H%M%S}.jsonl'
            _file = path.open('a', encoding='utf-8', buffering=1) # line buffered: nothing lost on a crash
            _log.info('MODBUS trace: %s', path)
        _file.write(json.dumps(record) + '\n')


# expected length of an RTU response frame from its first bytes, or None if not yet known
def rtu_response_length(frame:bytes) -> int|None:
    if len(frame) < 2:
        return None
    fc = frame[1]
    if fc & 0x80:
        return 5 # slave, function|0x80, exception code, crc
    if fc in {1, 2, 3, 4}:
        return 5 + frame[2] if len(frame) >= 3 else None # slave, function, byte count, data, crc
    if fc in {5, 6, 15, 16}:
        return 8 # echo of address and value/quantity
    return len(frame) # unknown function: log what arrived


# on serial lines pymodbus passes every partial receive buffer (and the growing buffer again); a response is logged
# once, when it is complete, with the number of pieces it arrived in
_rx_pieces:int = 0
_last_rx:bytes = b''
_last_tx_slave:int|None = None


def trace_packet(sending:bool, data:bytes) -> bytes:
    global _last_tx, _rx_pieces, _last_rx, _last_tx_slave # pylint: disable=global-statement
    try:
        now = time.time()
        direction = 'tx' if sending else 'rx'
        record:dict[str, Any] = {'epoch': round(now, 4), 'dir': direction}
        frame = data
        if sending:
            _last_tx = now
            _rx_pieces, _last_rx = 0, b''
            _last_tx_slave = data[0] if data and _framing == 'rtu' else None
        elif _framing == 'rtu':
            _rx_pieces += 1
            if _last_tx_slave is not None and data and data[0] != _last_tx_slave:
                if data != _last_rx: # bytes not from the addressed slave: line noise or a late reply
                    _last_rx = data
                    _write({**record, 'hex': data.hex(' '), 'noise': True})
                return data
            expected = rtu_response_length(data)
            if expected is None or len(data) < expected:
                return data # incomplete, wait for the rest
            frame = data[:expected]
            if frame == _last_rx:
                return data # the same complete frame passed again
            _last_rx = frame
            record['pieces'] = _rx_pieces
            if len(data) > expected:
                record['extra'] = data[expected:].hex(' ')
        record['hex'] = frame.hex(' ')
        if not sending and _last_tx:
            record['ms'] = round((now - _last_tx) * 1000, 1) # time since the request was sent
        if _framing == 'rtu':
            record.update(decode_rtu(direction, frame))
        elif _framing == 'tcp':
            record.update(decode_tcp(direction, frame))
        _write(record)
    except Exception as e: # pylint: disable=broad-except
        _log.debug(e)
    return data # the frame is passed on unchanged


def trace_connect(connected:bool) -> None:
    try:
        _write({'epoch': round(time.time(), 4), 'dir': 'connect' if connected else 'disconnect'})
    except Exception as e: # pylint: disable=broad-except
        _log.debug(e)


# extra keyword arguments for the pymodbus client constructors; empty if tracing is disabled
def client_kwargs(framing:str, settings:dict[str, Any]) -> dict[str, Any]:
    global _framing # pylint: disable=global-statement
    if not enabled():
        return {}
    _framing = framing
    _write({'epoch': round(time.time(), 4), 'dir': 'config', 'framing': framing, **settings})
    return {'trace_packet': trace_packet, 'trace_connect': trace_connect}
