"""Simulated Coffee-Tech FZ94 as configured on this roaster: a MODBUS TCP server with the same
slave ids and registers as the real machine, playing a typical roast curve.

  slave 25  input reg 1000 (fc4)   BT          (MODBUS input 1; temperature controllers report whole degrees C)
  slave 21  input reg 1000 (fc4)   ET          (MODBUS input 2)
            holding reg 50 (fc3)   Drum Heat Limit setpoint of the ET controller (writable, caps ET)
  slave 24  input reg 1000 (fc4)   Exhaust
  slave 22  holding reg 8192       drum drive control word (2 = run, 1 = stop)
            holding reg 8193       drum drive frequency setpoint (x10)
            holding reg 2          drum drive output frequency readback (ramps to the setpoint while running)
  slave 23  same as 22 for the fan drive
  slave 99  holding reg 0          simulator control, not on the real machine: write 1 = charge beans, 2 = drop beans

Roast curve after charge follows the medians of the 154 logged FZ94 roasts (TP 68 s at 81 C, DRY 5:29 at 149 C,
FCs 9:00 at 188 C, DROP 11:20 at 204 C), with fan speed slightly cooling ET.

The MODBUS TCP server is implemented here directly (functions 3, 4, 6 and 16) to stay independent of
pymodbus datastore API changes.

Usage:  python dev/fz94_sim.py [--port 5020] [--speed 1.0]
"""

import argparse
import asyncio
import logging
import random
import struct
import time

import numpy as np

# roast time (s) -> BT / ET (C) after charge, from the logged medians
BT_CURVE = ([0, 30, 68, 150, 329, 450, 540, 680], [200, 110, 81, 105, 149, 172, 188, 204])
ET_CURVE = ([0, 30, 68, 150, 329, 540, 680], [190, 165, 160, 165, 168, 172, 175])
PREHEAT_BT, PREHEAT_ET = 200.0, 190.0
RAMP_PER_S = 300.0 # drive frequency ramp in raw units (0.1 Hz) per second, ~4 s to full speed

ILLEGAL_FUNCTION, ILLEGAL_ADDRESS, GATEWAY_TARGET_FAILED = 1, 2, 0x0B

log = logging.getLogger('fz94_sim')


class FZ94:
    def __init__(self, speed: float, drops: dict[int, tuple[int, int]] | None = None, rejects: set[tuple[int, int, int]] | None = None) -> None:
        self.speed = speed
        # fault injection: slave -> (every, run): of every EVERY requests to it, RUN in a row get no answer
        # (timeouts, like a burst of noise on RS-485 that a single retry does not get through);
        # (slave, register, value) writes answered with exception 3 (illegal data value, like a drive refusing a value)
        self.drops = drops or {}
        self.rejects = rejects or set()
        self.request_counts: dict[int, int] = {}
        self.charged_at: float | None = None
        self.dropped_at: float | None = None
        drive = lambda: {'hr': {2: 0, 8192: 1, 8193: 300}} # noqa: E731
        # slave id -> register table ('ir' input registers, 'hr' holding registers) -> address -> value
        self.slaves: dict[int, dict[str, dict[int, int]]] = {
            25: {'ir': {1000: int(PREHEAT_BT)}},
            21: {'ir': {1000: int(PREHEAT_ET)}, 'hr': {50: 200}},
            24: {'ir': {1000: int(PREHEAT_ET) - 25}},
            22: drive(),
            23: drive(),
            99: {'hr': {0: 0}},
        }
        self.requests = 0

    def hr(self, slave: int, register: int) -> int:
        return self.slaves[slave]['hr'][register]

    def roast_time(self) -> float | None:
        return None if self.charged_at is None else (time.monotonic() - self.charged_at) * self.speed

    def step(self, dt: float) -> None:
        command = self.hr(99, 0)
        if command == 1 and self.charged_at is None:
            self.charged_at = time.monotonic()
            log.info('charge')
        elif command == 2 and self.charged_at is not None and self.dropped_at is None:
            self.dropped_at = time.monotonic()
            log.info('drop at roast time %.0f s', self.roast_time() or 0)
        self.slaves[99]['hr'][0] = 0

        # drives: output frequency ramps towards the setpoint while running, towards 0 when stopped
        fan_hz = 0.0
        for slave in (22, 23):
            regs = self.slaves[slave]['hr']
            target = regs[8193] if regs[8192] == 2 else 0
            step = RAMP_PER_S * dt
            current = regs[2]
            regs[2] = int(round(min(target, current + step) if current < target else max(target, current - step)))
            if slave == 23:
                fan_hz = regs[2] / 10

        # temperatures
        t = self.roast_time()
        if t is None:
            bt, et = PREHEAT_BT, PREHEAT_ET
        elif self.dropped_at is None:
            bt, et = float(np.interp(t, *BT_CURVE)), float(np.interp(t, *ET_CURVE))
        else: # beans out: the BT probe sees the empty drum and falls off quickly
            since = (time.monotonic() - self.dropped_at) * self.speed
            bt_drop = float(np.interp((self.dropped_at - (self.charged_at or 0)) * self.speed, *BT_CURVE))
            bt, et = bt_drop - min(60.0, since * 2.5), float(np.interp(since, [0, 60], [175, 185]))
        et = min(et - max(0.0, fan_hz - 30) * 0.08, self.hr(21, 50)) # more air cools ET, the heat limit caps it
        noise = lambda: random.uniform(-0.4, 0.4) # noqa: E731
        self.slaves[25]['ir'][1000] = int(round(bt + noise()))
        self.slaves[21]['ir'][1000] = int(round(et + noise()))
        self.slaves[24]['ir'][1000] = int(round(et - 25 + noise()))

    async def run(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            self.step(now - last)
            last = now

    # returns the response PDU for a request PDU
    def handle(self, slave: int, pdu: bytes) -> bytes:
        self.requests += 1
        fc = pdu[0]
        def exception(code: int) -> bytes:
            return bytes([fc | 0x80, code])
        if slave not in self.slaves:
            return exception(GATEWAY_TARGET_FAILED)
        if fc in {3, 4} and len(pdu) == 5:
            start, count = struct.unpack('>HH', pdu[1:5])
            table = self.slaves[slave].get('hr' if fc == 3 else 'ir', {})
            if any(start + i not in table for i in range(count)):
                return exception(ILLEGAL_ADDRESS)
            values = [table[start + i] & 0xFFFF for i in range(count)]
            return bytes([fc, 2 * count]) + struct.pack(f'>{count}H', *values)
        if fc == 6 and len(pdu) == 5:
            register, value = struct.unpack('>HH', pdu[1:5])
            if (slave, register, value) in self.rejects:
                log.info('fault: rejected write slave %d register %d value %d', slave, register, value)
                return exception(3)
            table = self.slaves[slave].get('hr', {})
            if register not in table:
                return exception(ILLEGAL_ADDRESS)
            table[register] = value
            return pdu # echo
        if fc == 16 and len(pdu) >= 6:
            start, count, nbytes = struct.unpack('>HHB', pdu[1:6])
            values = struct.unpack(f'>{count}H', pdu[6:6 + nbytes])
            table = self.slaves[slave].get('hr', {})
            if any(start + i not in table for i in range(count)):
                return exception(ILLEGAL_ADDRESS)
            for i, v in enumerate(values):
                table[start + i] = v
            return pdu[:5]
        return exception(ILLEGAL_FUNCTION)

    async def serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info('peername')
        log.info('client connected %s', peer)
        try:
            while True:
                header = await reader.readexactly(7) # MBAP: transaction id, protocol id, length, unit id
                tid, pid, length, unit = struct.unpack('>HHHB', header)
                pdu = await reader.readexactly(length - 1)
                self.request_counts[unit] = self.request_counts.get(unit, 0) + 1
                every, run = self.drops.get(unit, (0, 0))
                if every and self.request_counts[unit] % every < run:
                    log.info('fault: dropped request %d to slave %d', self.request_counts[unit], unit)
                    continue
                response = self.handle(unit, pdu)
                writer.write(struct.pack('>HHHB', tid, pid, len(response) + 1, unit) + response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            log.info('client disconnected %s after %d requests', peer, self.requests)
            writer.close()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', type=int, default=5020)
    ap.add_argument('--speed', type=float, default=1.0, help='roast time runs this many times faster than real time')
    ap.add_argument('--drop', action='append', default=[], metavar='SLAVE:EVERY:RUN', help='of every EVERY requests to SLAVE, leave RUN in a row unanswered')
    ap.add_argument('--reject', action='append', default=[], metavar='SLAVE:REGISTER:VALUE', help='answer this write with exception 3')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    drops = {int(s): (int(n), int(r)) for s, n, r in (d.split(':') for d in args.drop)}
    rejects = {tuple(int(x) for x in r.split(':')) for r in args.reject}
    sim = FZ94(args.speed, drops, rejects) # type: ignore[arg-type]
    server = await asyncio.start_server(sim.serve_client, '127.0.0.1', args.port)
    log.info('FZ94 simulator on 127.0.0.1:%d (speed x%g)', args.port, args.speed)
    async with server:
        await asyncio.gather(server.serve_forever(), sim.run())


if __name__ == '__main__':
    asyncio.run(main())
