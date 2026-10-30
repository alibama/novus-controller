"""
diagnose.py — "why won't it write?" and "what state is this controller in?"

Reads the handful of registers that explain almost every confusing moment
with a Novus controller — protection/password, unit, scaling, auto/manual,
run flag, setpoint and output limits, which program is selected, PID terms —
and turns them into plain-language findings. Read-only; safe at any time.

Used three ways, all sharing this one implementation:
  * Devices page "Diagnose" button,
  * the API's `diagnose` command (result lands in the command file),
  * tools/ from the shell.
"""
from __future__ import annotations

from typing import Optional

import novus_protocol as P

# (register, key, label) — read in one go where contiguous.
REGISTERS = [
    (P.REG_PROTECTION,  "protection",   "Protection level (51)"),
    (P.REG_UNIT,        "unit",         "Temperature unit (77)"),
    (P.REG_SETPOINT,    "sp",           "Setpoint (200)"),
    (P.REG_PV,          "pv",           "Process value (201)"),
    (P.REG_OUTPUT_PCT,  "output_raw",   "Output 0..1000 (202)"),
    (P.REG_CTRL_AUTO,   "auto",         "Auto/manual (213)"),
    (P.REG_CTRL_RUN,    "run",          "Run flag (214)"),
    (P.REG_HYST,        "hysteresis",   "Hysteresis (219)"),
    (P.REG_SPLL,        "sp_low_limit", "Setpoint lower limit (220)"),
    (P.REG_SPHL,        "sp_high_limit","Setpoint upper limit (221)"),
    (P.REG_OULL,        "out_low_limit","Output lower limit (224)"),
    (P.REG_OUHL,        "out_high_limit","Output upper limit (225)"),
    (P.REG_SOFT_START,  "soft_start_s", "Soft start (226)"),
    (P.REG_RS_PRN_EXEC, "program_exec", "Program selected to execute (247)"),
    (P.REG_RS_PRN_EDIT, "program_edit", "Program selected to edit (248)"),
    (P.REG_RS_SEG,      "segment",      "Current segment (249)"),
    (P.REG_RS_SEG_TIME, "segment_time_s","Elapsed in segment (250)"),
    (P.REG_RS_TBASE,    "time_base",    "R&S time base (252)"),
    (P.REG_RS_PROG_TYPE,"program_type", "R&S program type (254)"),
    (P.REG_TUNE_AUTO,   "autotune",     "Autotune (257)"),
    (P.REG_TUNE_PB,     "pb",           "Proportional band (258)"),
    (P.REG_TUNE_IR,     "ir",           "Integral rate (259)"),
    (P.REG_TUNE_DT,     "dt",           "Derivative time (260)"),
    (P.REG_TUNE_CT,     "ct",           "PWM cycle (261)"),
    (P.REG_DPPO,        "decimals",     "Decimal point (279)"),
]

# Contiguous blocks to minimise round trips: (start, count)
_BLOCKS = [(51, 1), (77, 1), (200, 3), (213, 2), (219, 3), (224, 3),
           (247, 4), (252, 1), (254, 1), (257, 5), (279, 1)]


async def read_registers(client) -> dict[str, Optional[int]]:
    """Read every diagnostic register from a CONNECTED client. A block that
    fails is recorded as None values rather than aborting the whole read."""
    raw: dict[int, Optional[int]] = {}
    for start, count in _BLOCKS:
        try:
            vals = await client.read_registers(start, count)
            for i, v in enumerate(vals):
                raw[start + i] = v
        except Exception:
            for i in range(count):
                raw[start + i] = None
    out: dict[str, Optional[int]] = {}
    for reg, key, _ in REGISTERS:
        v = raw.get(reg)
        out[key] = P.to_signed(v) if (v is not None and key in ("sp", "pv",
                                      "sp_low_limit", "sp_high_limit")) else v
    return out


def interpret(regs: dict, expected_unit: str = "°F", wanted_sp: Optional[float] = None,
              decimals: Optional[int] = None) -> dict:
    """Turn raw register values into {summary, findings:[{level, text}],
    writes_blocked: bool, values: {...}}. Pure; unit-testable."""
    f: list[dict] = []

    def add(level, text):
        f.append({"level": level, "text": text})

    dp = regs.get("decimals") if decimals is None else decimals
    scale = 10 ** dp if isinstance(dp, int) and 0 <= dp <= 3 else 1

    def temp(v):
        return None if v is None else v / scale

    unit = regs.get("unit")
    unit_s = "°F" if unit == 1 else "°C" if unit == 0 else "?"
    sp, pv = temp(regs.get("sp")), temp(regs.get("pv"))
    lo, hi = temp(regs.get("sp_low_limit")), temp(regs.get("sp_high_limit"))
    out = (regs.get("output_raw") or 0) / 10.0 if regs.get("output_raw") is not None else None
    auto, run = regs.get("auto"), regs.get("run")
    prog, ptype = regs.get("program_exec"), regs.get("program_type")
    prot = regs.get("protection")
    blocked = False

    # --- unit & scaling -----------------------------------------------------
    if unit is None:
        add("warn", "Could not read the unit register (77).")
    elif unit_s != expected_unit:
        add("danger", f"Controller is set to **{unit_s}** but the app (and its "
                      f"program templates) assume {expected_unit}. A setpoint of 896 "
                      f"here means 896{unit_s}. Change the unit on the front panel "
                      f"(or treat every number as {unit_s}).")
    if isinstance(dp, int) and dp > 0:
        add("info", f"Decimal point is {dp} place(s): raw register values are ×{scale}. "
                    "The app scales reads and writes accordingly.")

    # --- protection ---------------------------------------------------------
    if prot is None:
        add("warn", "Could not read the protection level (51).")
    elif prot and prot > 1:
        add("warn", f"Protection level is {prot}. Writes over Bluetooth are likely "
                    "rejected until the controller password is sent (register 53). "
                    "Set the password on the Devices page (the factory default is "
                    "1111; the panel shows it as PAS) or lower the protection level "
                    "on the front panel.")
        blocked = True

    # --- mode ---------------------------------------------------------------
    if auto == 0:
        add("danger", f"Controller is in **MANUAL** mode (213 = 0): it drives a fixed "
                      f"output ({out:g}%) and ignores the setpoint entirely. Switch to "
                      "automatic (the app's RUN / override do this) before trusting it "
                      "to hold a temperature.")
    if run == 0:
        add("info", "Run flag is 0: the controller is stopped (output off).")
    if prog and prog > 0 and run == 1:
        add("info", f"Program {prog} is selected and running"
                    + (f", segment {regs.get('segment')}" if regs.get("segment") else "")
                    + ". While a program runs, the program sets the setpoint — a manual "
                      "setpoint write is overwritten. Stop it (or use the app's "
                      "override, which clears the program) to hold a fixed temperature.")
    elif prog and prog > 0:
        add("info", f"Program {prog} is selected (not running). Pressing RUN will run it.")

    # --- limits -------------------------------------------------------------
    if lo is not None and hi is not None:
        add("info", f"Setpoint limits: {lo:g} … {hi:g} {unit_s}. Anything outside is "
                    "clamped silently.")
        if wanted_sp is not None and not (lo <= wanted_sp <= hi):
            add("danger", f"Your target {wanted_sp:g} is outside the setpoint limits "
                          f"{lo:g}…{hi:g}; the controller clamps it. Raise the upper "
                          "limit (register 221) on the panel or with the register console.")
        if sp is not None and (sp >= hi or sp <= lo) and wanted_sp is None:
            add("warn", f"Setpoint {sp:g} sits exactly on a limit — a write was probably "
                        "clamped.")
    ol, oh = regs.get("out_low_limit"), regs.get("out_high_limit")
    if oh is not None and oh < 1000:
        add("warn", f"Output upper limit is {oh/10:g}% (225): the controller can never "
                    "deliver full power. Intentional for some elements; otherwise raise it.")
    if ol is not None and ol > 0:
        add("danger", f"Output LOWER limit is {ol/10:g}% (224): the element is never fully "
                      "off, so the kiln can drift above setpoint. Set it to 0 unless you "
                      "know why it isn't.")

    # --- plausibility -------------------------------------------------------
    if pv is not None and sp is not None and run == 1 and out is not None:
        if pv > sp + 100 and out <= 5:
            add("danger", f"PV {pv:g} is well above SP {sp:g} with output {out:g}%: "
                          "the output stage is delivering power the controller is not "
                          "asking for. Suspect a shorted SSR / welded contactor. Cut "
                          "element power at the breaker and check.")
        elif pv > sp + 100 and out > 5:
            add("danger", f"PV {pv:g} is well above SP {sp:g} and output is still "
                          f"{out:g}%: tuning, a wrong thermocouple type, or manual mode. "
                          "Stop it and check the input type and PID terms.")

    # --- PID glance ---------------------------------------------------------
    if regs.get("pb") is not None:
        add("info", f"PID: band {regs.get('pb')}, integral {regs.get('ir')}, derivative "
                    f"{regs.get('dt')}, cycle {regs.get('ct')} s, autotune {regs.get('autotune')}.")
        if regs.get("pb") == 0:
            add("warn", "Proportional band is 0 → ON/OFF control with hysteresis "
                        f"{regs.get('hysteresis')}. Expect overshoot; run autotune for PID.")

    summary = (f"{unit_s} · {'manual' if auto == 0 else 'auto'} · "
               f"{'running' if run == 1 else 'stopped'}"
               + (f" program {prog}" if prog else "")
               + (f" · PV {pv:g} SP {sp:g}" if pv is not None and sp is not None else "")
               + (f" · out {out:g}%" if out is not None else "")
               + (" · WRITES BLOCKED (password)" if blocked else ""))
    return {"summary": summary, "findings": f, "writes_blocked": blocked,
            "unit": unit_s, "values": {k: regs.get(k) for _, k, _ in REGISTERS}}


async def diagnose(client, expected_unit: str = "°F",
                   wanted_sp: Optional[float] = None) -> dict:
    """Read + interpret on a connected client."""
    regs = await read_registers(client)
    return interpret(regs, expected_unit=expected_unit, wanted_sp=wanted_sp,
                     decimals=getattr(client, "decimal_places", None))


def to_text(report: dict) -> str:
    icons = {"danger": "!!", "warn": "! ", "info": "  "}
    lines = [report["summary"], ""]
    for x in report["findings"]:
        lines.append(f"{icons.get(x['level'], '  ')} {x['text']}")
    lines.append("")
    for _, key, label in REGISTERS:
        lines.append(f"  {label:<36} = {report['values'].get(key)}")
    return "\n".join(lines)
