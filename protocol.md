# Novus N20K48 BLE Protocol — Decode Notes

> Reverse-engineered from captured QuickTune Mobile sessions plus
> directed read/write probes against a live N20K48 controller. This
> document is the primary reference for anyone — including Novus
> engineers — looking to verify or correct what we found.

**Confidence legend used throughout:**
- ✅ **Verified** — encoder produces byte-identical frames to QuickTune
  Mobile, or live read/write behavior was confirmed against the kiln's
  physical state
- 🟡 **Hypothesized** — fits the observations but not independently
  confirmed
- ❓ **Open question** — listed at the end

---

## 1. BLE service and characteristic

Discovered via standard GATT discovery against the BLE bridge built
into the N20K48.

| | Value |
|---|---|
| **Manufacturer OUI** | `00:26:A4` (Novus Automation) ✅ |
| **Advertised manufacturer ID** | `0x01FF` (511) ✅ |
| **Service UUID** | `0783b03e-8535-b5a0-7140-a304d2495cb7` ✅ |
| **Data characteristic UUID** | `0783b03e-8535-b5a0-7140-a304d2495cba` ✅ |
| **Properties** | `0x18` = Write + Notify ✅ |

All application traffic flows through that one characteristic: writes
carry requests, notifications carry responses.

The BLE advertisement also includes a 7-byte manufacturer-data payload
that looks like `0x60 <ascii dashes>`. We *think* this is a status flag
byte plus six ASCII placeholders for an at-a-glance status display
(possibly alarm letters), but this is 🟡 unconfirmed.

---

## 2. Frame format

Every request and response is wrapped:

```
 ┌─────────────────┬──────┬──────────────┬─────────────┐
 │ 5a 00 00 00     │ LEN  │ <payload>    │ CRC16-LE    │
 │ magic, 4 bytes  │ 1 B  │ LEN bytes    │ 2 bytes     │
 └─────────────────┴──────┴──────────────┴─────────────┘
```

- `LEN` is the **payload byte count only** (excludes magic, `LEN`, and CRC)
- `CRC` is **Modbus CRC-16** (polynomial `0xA001`, initial value `0xFFFF`),
  computed over `magic + LEN + payload`, transmitted little-endian ✅
- Every payload begins with `0x01` (the bridge's internal slave address —
  see §3) followed by a function code

Verified across **all 344 frames** in a 4560-record snoop session: every
frame parses cleanly and CRC checks pass.

### Example: read 5 registers starting at address 200

Request bytes (13 total):
```
5a 00 00 00 06 01 46 00 c8 00 05 89 de
└──magic──┘ ── ── ──fc── ──addr── ──cnt── ──crc──
            LEN slave
```

Response bytes (20 total):
```
5a 00 00 00 0d 01 46 0a 03 7f 03 7f 00 00 ff 12 00 00 ef 72
└──magic──┘ ── ── ── ── ──────────data────────── ──crc──
            LEN slave fc byte_count (= 10 = 5 regs × 2 B)
```

---

## 3. Slave addressing

On the BLE side the slave byte is always `0x01`. The N20K48's actual
RS-485 Modbus address is `255` (0xFF), but the BLE module appears to
NAT or rewrite that internally — clients on BLE always address the
bridge as slave 1 regardless of the controller's underlying Modbus ID.

This matches what we saw when our early probes (sending raw Modbus
RTU frames without the `5a 00 00 00` wrapper) got back error responses
from the bridge rather than reaching the controller.

---

## 4. Function codes

The BLE protocol uses **Modbus-like function codes in the user-defined
range 0x41–0x48**, plus standard Modbus FC 0x2B for device identification.
The function-code numbering mirrors a `+0x43` offset from standard Modbus:
`0x46` ≈ FC 3 (read), `0x47` ≈ FC 6 (write single), `0x48` ≈ FC 16
(write multiple). 🟡 Hypothesized that this offset is deliberate (perhaps
so a raw-Modbus probe over RS-485 wouldn't accidentally match these codes).

### 4.1 `0x46` — Read Multiple Registers ✅
Analog of Modbus FC 0x03.

| Direction | Payload |
|---|---|
| Request  | `01 46 <addr:big-endian u16> <count:big-endian u16>` |
| Response | `01 46 <byte_count:u8> <data: byte_count bytes>` |

`data` is `count` consecutive 16-bit registers, each transmitted big-endian.

### 4.2 `0x47` — Write Single Register ✅
Analog of Modbus FC 0x06. Response echoes the request.

| Direction | Payload |
|---|---|
| Request  | `01 47 <addr:be u16> <value:be u16>` |
| Response | `01 47 <addr:be u16> <value:be u16>` |

### 4.3 `0x48` — Write Multiple Registers ✅
Analog of Modbus FC 0x10. Used for bulk-config saves.

| Direction | Payload |
|---|---|
| Request  | `01 48 <addr:be u16> <count:be u16> <byte_count:u8> <data: byte_count bytes>` |
| Response | `01 48 <addr:be u16> <count:be u16>` |

### 4.4 `0x2B 0x0E` — Read Device Identification ✅
Standard Modbus MEI Type 14 (Encapsulated Interface Transport).

| Direction | Payload |
|---|---|
| Request  | `01 2b 0e <read_id_code:u8> <object_id:u8>` |
| Response | Standard MEI Type 14 response with vendor name, product code, firmware version |

Observed objects returned on our test controller:
- `"Novus Automation"` (vendor)
- `"0200 - N20K48 - USB - 1 AI / 1 DO / 1 RL"` (product code with hardware config)
- `"1.02"` (firmware revision)
- `"96"` (purpose unknown — 🟡 hardware revision?)

---

## 5. Register map

Addresses are 1-based decimal; hex shown for cross-reference.

### Operation cycle — addresses 200–215 ✅

| Address | Hex | Field | Notes |
|---|---|---|---|
| 200 | `0x00C8` | Setpoint (SP) | Signed int16, scaled by decimal-place setting |
| 201 | `0x00C9` | Process Variable (PV) | Signed int16, scaled |
| 202 | `0x00CA` | Output power | `0..1000` = `0.0..100.0%` |
| 203 | `0x00CB` | (unknown) | Consistently `-238` (0xFF12) on idle kiln; 🟡 cold-junction temperature or status word |
| 208 | `0x00D0` | User manual setpoint | Preserved across mode changes; the "real" SP that returns when auto mode is exited |
| 213 | `0x00D5` | Program selected for view/edit | Range 0..20; 0 = none |
| **214** | **`0x00D6`** | **Program currently executing** | **Range 0..20. Write 1..20 to start; write 0 to stop. ✅** |
| 215 | `0x00D7` | Current program segment | Range 0..20; 0 = first segment of running program |

**Verification of register 214:**
Before/after probe of the same controller produced this diff when the
operator switched from manual mode to auto-running a program in
QuickTune Mobile:

```
reg 200 (SP):     895 →  886   (controller interpolating from program)
reg 201 (PV):     201 →  205   (kiln warming up)
reg 202 (Output):   0 → 1000   (heater fully on)
reg 214:            0 →    1   (the run flag)
```

Then writing `0` to register 214 from our client stopped the program and
the kiln went idle, confirmed visually on the front panel.

### Commit register — address 53 ✅

| Address | Hex | Field | Notes |
|---|---|---|---|
| 53 | `0x0035` | Commit | Written as `1` after a bulk-config save sequence to persist changes |

The same register was also written with non-trivial values (e.g. `1111`)
in a later trace; the meaning of those values is 🟡 unclear. May be a
session/transaction token or a user identifier.

### PID / control config — addresses 220–225 🟡

| Address | Hex | Field | Observed value |
|---|---|---|---|
| 220 | `0x00DC` | Proportional band? | 70 |
| 221 | `0x00DD` | Integral time? | 2000 |
| 222 | `0x00DE` | ? | 0 |
| 223 | `0x00DF` | ? | 950 |
| 224 | `0x00E0` | ? | 0 |
| 225 | `0x00E1` | ? | 1000 |

Field labels are inferred from the value ranges (typical for PID
parameters); not confirmed.

### Program tables 🟡

| Address range | Layout |
|---|---|
| 500–1099 | **Program segments**, 9 segments per program, 3 registers per segment (`SP`, `duration_minutes`, `event`). Programs appear to be 30 registers wide. |
| 2600–2999 | **Program metadata**, banks of 50 registers each at addresses 2600, 2650, 2700, … 2950. Exact field layout not yet decoded. |

The 3-register-per-segment format `(SP, time, event)` is confirmed by
inspection of a populated program 1 returning `[350, 60, 0, 80, 60, 0,
0, 0, …]` — two segments and then zeros. The program-bank stride of 30
registers is inferred from the read sizes QuickTune uses.

### High config block — addresses 2400+ 🟡

| Address | Observed value | Hypothesis |
|---|---|---|
| 2400 | 1111 | Session/transaction token (same as register 53 when set) |
| 2401 | 9194 | ? |
| 2402 | 1 | ? |
| 2403 | 1025 | Bit-packed flags? |
| 2404 | 256 | Bit-packed flags? |

---

## 6. BLE notification fragmentation

The Novus bridge negotiates MTU at connect time. Large responses
(e.g. the 200+ byte config dump from a `0x46` read of 100 registers)
arrive as multiple ATT notifications and need reassembly into a single
wire-format frame. The reference client uses the magic header `5a 00 00 00`
+ the LEN byte to determine total expected size and assemble fragments.

See `FrameAssembler` in `novus_protocol.py` for the implementation.

---

## 7. ❓ Open questions

These are the things the reference implementation cannot answer
confidently. **If you're on the Novus team or have documentation
that helps with any of these, we would love to engage.**

### 7.1 Password authentication
At least one N20K48 in the wild rejects write operations. Our hypothesis
is that there is a password-protection mode that requires an
authentication step before writes are accepted, but we don't yet know:
- Whether auth is a register write (and to what register)
- Whether the password is the same digit code shown on the front-panel
  config menus
- What error response the controller emits to indicate "password required"
- Whether the lock applies per-write, per-session, or per-connection

### 7.2 Program-number echo discrepancy
When QuickTune Mobile is set to "run program 5" and the operator confirms,
register 214 reads back as `1` rather than `5`. The kiln does start
heating, but no register we read carries the value `5`. Two possibilities:
- The controller silently fell back to program 1 because program 5 was
  empty (we observed all-zero data in the program-5 register range)
- The "selected program number" lives in a register we haven't located
- QuickTune Mobile's UI selection isn't tied to the executing program
  index in the way we expect

### 7.3 Setpoint behavior during program execution
Register 200 (SP) shifts during program execution but to values that
don't match any obvious segment endpoint or interpolated value. The
relationship between segment definitions in 500+ and the live SP in
register 200 needs documenting. Meanwhile, register 208 holds steady
at the user's last manual setpoint — this is probably the intended
behavior, but we'd like to confirm.

### 7.4 Decimal-point register
The N20K48 supports a configurable decimal point that affects how SP,
PV, and other temperature-valued registers are interpreted. We have not
yet identified the register that stores this setting. The reference
client exposes `client.decimal_places` as a manual override.

### 7.5 Register 203
Reg 203 consistently reads `-238` (`0xFF12`) across two different
controllers and across hours. Static value, single byte each side
(`FF`/`12`), unclear semantics. Cold-junction reading? Status word?
Diagnostic timestamp?

### 7.6 The `0x60 ----` advertisement payload
The 7-byte manufacturer-data field in the BLE advertisement appears to
be a status flag byte (`0x60`) plus six placeholders rendered as ASCII
dashes. If those positions can ever be non-`-`, decoding them would let
a client display alarm state without connecting. The advertisement
format is undocumented as far as we can tell.

### 7.7 Function-code mnemonic
The choice of `0x46`/`0x47`/`0x48` as user-defined codes that map onto
Modbus FC 3/6/16 looks intentional. If there's a documented rationale
(perhaps to coexist with raw Modbus RTU on RS-485), it would be useful
to capture for future-proofing.

---

## 8. Tools to verify against your own controller

The repository includes three diagnostic scripts that produce evidence
useful for verifying or correcting any of the above:

- **`kiln_bt_scanner.py`** — BLE scanner that filters for Novus
  manufacturer ID. Use to confirm OUI and advertisement payload.
- **`gatt_enumerate.py`** — Dumps the full GATT tree of a connected
  controller. Use to confirm UUIDs and characteristic properties.
- **`explore_registers.py`** — Read-only probe that prints the contents
  of address ranges 200–209, 210–229, and 2400–2419. Safe to run on a
  live controller during a firing.

Frame-by-frame analysis of a fresh QuickTune Mobile snoop log can be
done by extracting `btsnoop_hci.log` from an Android bug report and
parsing it with the protocol module — every frame in that log should
pass CRC and decode cleanly.
