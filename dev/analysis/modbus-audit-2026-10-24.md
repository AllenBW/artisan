# Modbus audit: customized FZ94 (2026-10-24)

Covers the installed Artisan 3.1.0 settings and this repo's code (4.2.1, pymodbus 3.15). Nothing on the roaster was changed or contacted.

Each claim is marked:
- **[code]** checked in this repo's code
- **[trace]** seen on the wire in the simulator test
- **[manual]** taken from a manufacturer manual or driver source
- **[inference]** reasoned, not yet confirmed

## The commands

| What | Slave | Function | Register | Scaling | Status |
|---|---|---|---|---|---|
| BT (bean probe), input 1 | 25 | 4 (input register) | 1000 / 0x03E8 | whole °C | OK [code][trace] |
| ET, input 2 | 21 | 4 | 1000 / 0x03E8 | whole °C | OK [code][trace] |
| Exhaust | 24 | 4 | 1000 / 0x03E8 | whole °C | OK |
| ET heat limit (setpoint of the ET controller) | 21 | 3 (holding) | 50 / 0x0032 | raw | Reads 200, which is plausible. Controller model unknown. |
| Drum / fan drive readback | 22 / 23 | 3 | 2 / 0x0002 | ÷10 | Meaning unknown (see "Drives") |
| Fan / Drum On, Off | 23 / 22 | 6 (write single) | 8192 / 0x2000 | 2 = run, 1 = stop | Matches Delta's convention [manual] |
| Fan / Drum speed | 23 / 22 | 6 | 8193 / 0x2001 | slider × 10 | **Unit unconfirmed** |
| ET heat limit slider | 21 | 6 | 50 / 0x0032 | × 1 | Writes the controller setpoint, probably to EEPROM |

The ET heat limit is the setpoint of the ET controller (slave 21). The controller holds back the burner when ET reaches it.

### Temperature controllers
- Reading PV at FC4 register 1000 matches Autonics TK controllers [manual] and also Fuji PXR/PXG ones (`src/artisanlib/pid_control.py`).
- The setpoint at holding register 50 does **not** match a TK: on a TK, register 50 is Run/Stop and the setpoint is at register 0 [manual]. These are therefore not TKs with that map, or not TKs at all. **Model to be identified.**
- Most controllers save a setpoint written over Modbus to EEPROM, which has a limited number of write cycles. The heat slider should stay locked, as it is in the mock-up.

### Drives
- 0x2000 run/stop with 2/1 and 0x2001 frequency is Delta's convention [manual].
- On Delta, 0x2001 is in **0.01 Hz** units. Artisan's stock FZ94 preset reads Delta output frequency at 0x2103 ÷ 100.
- Your setup differs from the stock FZ94: slaves 22/23 instead of 1/2, readback at 0x0002, 8E1 instead of 8N2. The readback at 0x0002 is not Delta's output-frequency register, so these are a retrofit or a Delta-compatible clone.
- **Whether slider 120 means 120.0 Hz or 12.00 Hz is open.** A fan at 12 Hz would be implausibly slow, so 0.1 Hz units are more likely [inference]. But 120 Hz may be over the motor's rating.

## How Artisan sends and reads

**Writes are fire-and-forget [code]** (`modbusport.py`, `writeSingleRegister`)
- The write is scheduled with `call_soon_threadsafe(asyncio.ensure_future, …)` and never awaited.
- Timeouts, exception responses (e.g. a value out of range) and wrong echoes are dropped silently.
- With read retries at 0, each write is sent exactly once.
- The UI updates and the event is logged **before** the write goes out.
- The label "1200Hz" comes from the logged event being *value × factor + unit*.

**One missed reply blanks every channel [code]**
- In `read_active_registers_async`, a failed request raises out of the whole pass, so the remaining reads are skipped.
- With `fail_on_cache_miss = True` (`modbusport.py:158`), every unread channel then returns −1.
- Slave 25 (BT) is read first, so a single miss there drops all six channels at once. This matches the dropouts in 41 of 50 roasts recorded with 3.1.0.

**Timing at 19200 baud, 8E1**
- One character takes 0.573 ms. A read cycle is about 55 ms on the wire plus device response time, roughly 150–250 ms per 2 s. Bus load is well under 15%.
- The optimizer groups nothing: six separate requests.
- The 0.3 s timeout is generous next to typical 10–40 ms replies.
- The inter-frame delay is effectively 0 (`sleepBetween()` is a no-op), and Artisan's own comment says delays "seem to be beneficial on … the FZ-94".

**Unused PID fields** (registers 10, 240 and 49, multipliers 2) do nothing while the PID slave ID is 0. If it were ever set to 21, they would write ×100 values to unknown registers. **Clear them.**

## Safety
- A lost **Fan On** or **Drum On** while the burner runs is the dangerous case. Make sure there is a **hardware interlock** (burner only enabled while the fan and drum run). Artisan must not be that interlock.
- A lost **Off** or setpoint write leaves the old state with no warning. The touch panel inherits this.
- RS-485 practice:
  - one daisy chain, 120 Ω termination at both ends only, biasing applied once
  - shielded twisted pair, routed away from the motor cables
  - the drives' communication-loss action set to *warn and continue*, so they don't stop on a lost link

## Recommendations, ranked
1. **Settings:** read retries 1, keep the timeout at 0.3 s for now, and clear the unused PID registers.
2. **Fork, read isolation:** catch errors per request, so one missed reply only affects its own channel. Optionally hold the last value for one sample.
3. **Fork, confirmed writes:** await the write result, retry once, and show failures in the UI. Show the panel's setpoint as *pending* until the echo or readback confirms it.
4. **Fork, inter-frame gap:** a configurable 10–30 ms pause before each request.
5. **Fork, correct labels:** once the unit is known, log "120 Hz" rather than "1200Hz".
6. **Fork, trace fix:** reassemble RTU frames before decoding (see the review finding). Otherwise the serial trace logs fragments as CRC errors.

## To settle in the dry run (with `ARTISAN_MODBUS_TRACE=1`)
- Reply latency per slave, which slave misses first, and whether misses cluster while the drives run.
- On a fan step, whether register 2 **ramps** (output frequency) or **jumps** (a mirror of the setpoint).
- **Frequency unit:** set the fan slider to 30 and read the drive's keypad. 30.0 means 0.1 Hz units; 3.00 means 0.01 Hz units.
- Read-only identification probes: drives FC3 0x2100–0x2104 (exception 02 means not Delta), and the controllers' model registers.
- From you:
  - the make and model labels of the drives and controllers
  - the motors' maximum frequency
  - the USB–RS-485 adapter type
  - where the termination resistors are
  - whether a hardware burner interlock exists
