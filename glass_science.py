"""
glass_science.py
================
Educational glass-kiln data and small calculators for the Learn page.
Pure module: no Streamlit, no I/O, no hardware. Everything here is importable
from tests and from any page.

UNITS: every temperature in this module is °F (the app currently assumes °F
throughout; the controllers in the studio are configured for °F).

IMPORTANT — the annealing table below is a STARTING POINT, not gospel.
The numbers follow the widely published Bullseye-style "Annealing Thick Slabs"
guidance for COE-90 glass (Bullseye Glass TechNotes and common community
schedules), transcribed from memory of that guidance rather than copied from a
specific document. Verify them against the current chart for YOUR glass,
your kiln, and your mold before trusting a firing — and remember that two
identical programs in two kilns give two different pieces of glass.

Contents
--------
* ANNEAL_TABLE / anneal_schedule() / anneal_segments() — thickness-based
  annealing schedules and their translation into ramp & soak segments.
* PROCESSES — what happens to COE-90 glass at each temperature range.
* PID_TERMS / RS_TERMS — plain-English explanations of controller settings,
  with the Novus N20K48 register numbers.
* HEAT_WORK_* text constants and effective_rate().
* program_svg() — a self-contained inline SVG of a stepped program profile
  (no JavaScript, no external references), styled like notebook.svg_chart.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from novus_protocol import (
    REG_TUNE_AUTO, REG_TUNE_PB, REG_TUNE_IR, REG_TUNE_DT, REG_TUNE_CT,
    REG_HYST, REG_SOFT_START, REG_OULL, REG_OUHL,
)

# COE-90 reference points (°F). Bullseye publishes ~900°F as the annealing
# temperature it recommends for its glass, and ~700°F as the strain point
# below which the glass is rigid enough that it can no longer relieve stress.
ANNEAL_POINT_F = 900.0
STRAIN_POINT_F = 700.0
ROOM_TEMP_F = 70.0

# The controller's ramp & soak engine stores segment time in a 16-bit
# register; we keep a conservative cap so every segment is safely writable.
MAX_SEGMENT_MINUTES = 9999
MAX_SEGMENTS = 9


# ---------------------------------------------------------------------------
# Annealing table — COE-90 (Bullseye-style), by glass thickness
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AnnealRow:
    thickness_mm: int
    thickness_label: str      # the familiar inch label
    soak_temp: float          # °F — anneal soak (900 for COE-90)
    soak_minutes: int         # hold at soak_temp
    rate_to_800: float        # °F/hr, first (slowest) cool  900 -> 800
    rate_to_700: float        # °F/hr, second cool          800 -> 700
    rate_to_70: float         # °F/hr, final cool           700 -> room


# Starting points from published Bullseye-style "Annealing Thick Slabs"
# guidance for COE-90. VERIFY for your glass. For 6mm the common two-layer
# schedule (1h soak, 150°F/hr through the strain zone, then 300°F/hr) is used,
# so the "to 800" and "to 700" rates are both 150 — that row comes from a
# different (thin-work) schedule than the thick-slab rows, which is why its
# later-stage rates are slower than 12mm's 180/600.
ANNEAL_TABLE: list[AnnealRow] = [
    AnnealRow(6,   '1/4"',   900, 60,   150,  150,  300),
    AnnealRow(12,  '1/2"',   900, 120,  100,  180,  600),
    AnnealRow(19,  '3/4"',   900, 180,  45,   81,   270),
    AnnealRow(25,  '1"',     900, 240,  27,   49,   162),
    AnnealRow(38,  '1-1/2"', 900, 360,  12,   22,   72),
    AnnealRow(50,  '2"',     900, 480,  6.8,  12.3, 41),
    AnnealRow(75,  '3"',     900, 720,  3,    5.5,  18),
    AnnealRow(100, '4"',     900, 960,  1.7,  3.1,  10.3),
]


def anneal_schedule(thickness_mm: float) -> dict:
    """Pick the table row for a piece of the given thickness.

    The rule is "nearest-or-next-thicker": a 25mm piece gets the 25mm row, a
    30mm piece gets the 38mm row (never a thinner, faster schedule — under-
    annealing is how glass cracks on the shelf a week later). Thicker than
    the table's last row returns the last row with ``extrapolated=True`` so
    the UI can warn that the chart has run out.
    """
    t = float(thickness_mm)
    for row in ANNEAL_TABLE:
        if row.thickness_mm >= t - 1e-9:
            return _row_dict(row, extrapolated=False)
    return _row_dict(ANNEAL_TABLE[-1], extrapolated=True)


def _row_dict(row: AnnealRow, extrapolated: bool) -> dict:
    return {
        "thickness_mm": row.thickness_mm,
        "thickness_label": row.thickness_label,
        "soak_temp": row.soak_temp,
        "soak_minutes": row.soak_minutes,
        "rate_to_800": row.rate_to_800,
        "rate_to_700": row.rate_to_700,
        "rate_to_70": row.rate_to_70,
        "extrapolated": extrapolated,
    }


def ramp_minutes(from_t: float, to_t: float, rate_per_hr: float) -> int:
    """Minutes to travel from from_t to to_t at rate_per_hr (°F/hour), as an
    int >= 1 — the controller's time unit for a segment."""
    if rate_per_hr <= 0:
        return 1
    return max(1, int(round(abs(to_t - from_t) / rate_per_hr * 60)))


def _split_segment(minutes: int, from_t: float, to_t: float) -> list[tuple[int, float]]:
    """Break a ramp longer than MAX_SEGMENT_MINUTES into equal pieces with
    evenly spaced intermediate setpoints, so no single segment exceeds the cap.
    (The annealing table never needs this for the listed thicknesses, but a
    custom rate could.)"""
    if minutes <= MAX_SEGMENT_MINUTES:
        return [(max(1, int(minutes)), float(to_t))]
    pieces = int(math.ceil(minutes / MAX_SEGMENT_MINUTES))
    out = []
    for k in range(1, pieces + 1):
        sp = from_t + (to_t - from_t) * k / pieces
        m = int(round(minutes / pieces))
        out.append((max(1, min(MAX_SEGMENT_MINUTES, m)), round(sp, 1)))
    return out


def anneal_segments(thickness_mm: float, start_temp: float = ROOM_TEMP_F,
                    room_temp: float = ROOM_TEMP_F) -> list[tuple[int, float]]:
    """Ramp & soak segments [(minutes, setpoint_F), ...] for an annealing
    program at the given thickness.

    ``start_temp`` is where the kiln is when the program begins (the program's
    start setpoint, SP0). If it is below the anneal temperature the first
    segment heats to the soak at the table's *final-cool* rate — the gentlest
    thickness-appropriate rate in the row — because thick glass is just as
    happy to crack from fast heating as from fast cooling. If the kiln is
    already at the anneal temperature (the usual case: this program runs right
    after a fuse or casting has crashed to 900), no heating segment is added.

    Then: hold at the soak temperature for the soak time, cool to 800, to 700,
    and finally to ``room_temp`` at the three table rates.

    Every segment's minutes is an int >= 1 and <= MAX_SEGMENT_MINUTES; a cool
    that would exceed the cap is split into equal pieces (see _split_segment).
    The result never exceeds MAX_SEGMENTS (9) — the controller's limit.
    """
    row = anneal_schedule(thickness_mm)
    soak = float(row["soak_temp"])
    segs: list[tuple[int, float]] = []

    if start_temp < soak - 0.5:
        segs += _split_segment(ramp_minutes(start_temp, soak, row["rate_to_70"]),
                               start_temp, soak)

    segs.append((max(1, min(MAX_SEGMENT_MINUTES, int(row["soak_minutes"]))), soak))

    plan = [(800.0, row["rate_to_800"]), (700.0, row["rate_to_700"]),
            (float(room_temp), row["rate_to_70"])]
    prev = soak
    for target, rate in plan:
        if target >= prev:
            continue   # e.g. a room_temp above 700 — skip the no-op cool
        segs += _split_segment(ramp_minutes(prev, target, rate), prev, target)
        prev = target

    if len(segs) > MAX_SEGMENTS:
        raise ValueError(f"anneal schedule needs {len(segs)} segments; the "
                         f"controller allows {MAX_SEGMENTS}")
    return segs


def anneal_total_minutes(segments: list[tuple[int, float]]) -> int:
    return sum(int(m) for m, _ in segments)


# ---------------------------------------------------------------------------
# Glass processes — COE-90 typical ranges, °F
# ---------------------------------------------------------------------------
# Each entry: name, (low, high) °F, what physically happens, key tips.
PROCESSES: dict[str, dict] = {
    "full_fuse": {
        "name": "Full fuse",
        "range": (1465, 1500),
        "what": "Stacked layers melt completely into one flat, glossy slab with "
                "rounded edges. Two layers settle to about 6mm (1/4\") — glass "
                "wants to be 6mm thick at full fuse, so thinner stacks pull in "
                "and thicker ones spread out.",
        "tips": [
            "A short hold (5–15 min) at the top is usually enough; longer "
            "holds spread the piece and can trap more bubbles.",
            "Use a bubble squeeze (hold ~1150–1250°F) on the way up when the "
            "stack has trapped air between layers.",
            "Crash-cool from the fuse temperature to the anneal soak to skip "
            "through the devitrification window quickly.",
        ],
    },
    "tack_fuse": {
        "name": "Tack fuse",
        "range": (1350, 1400),
        "what": "Pieces stick to each other but keep their shape and edges — "
                "a stacked-paper or textured look instead of a flat slab.",
        "tips": [
            "Lower end of the range keeps sharper edges; higher end softens "
            "them and starts to round corners.",
            "Tack-fused pieces have more surface area for stress to collect in; "
            "anneal them as if they were the full stacked thickness.",
        ],
    },
    "contour_fuse": {
        "name": "Contour fuse (fuse-to-round)",
        "range": (1400, 1450),
        "what": "Between tack and full: layers merge and edges round over, but "
                "the top surface still follows the shape of the pieces "
                "underneath.",
        "tips": [
            "Very sensitive to a few degrees and to hold time — change one "
            "variable at a time and keep notes.",
        ],
    },
    "slump": {
        "name": "Slump",
        "range": (1200, 1250),
        "what": "An already-fused blank softens just enough to sag into (or over) "
                "a mold under its own weight. The glass surface stays as it was.",
        "tips": [
            "Hold times depend heavily on the mold's depth and the glass "
            "thickness — a deep bowl may need a longer hold at a lower "
            "temperature rather than a hotter one.",
            "Watch through the peephole: when it has settled, start cooling.",
            "Kiln-wash or boron-nitride the mold; slumped glass sticks.",
        ],
    },
    "drape": {
        "name": "Drape",
        "range": (1180, 1230),
        "what": "Like slumping but the glass hangs over a form (a cylinder, a "
                "stainless drape mold) and folds under gravity.",
        "tips": [
            "Slightly cooler and slower than a slump: the glass stretches, and "
            "thin spots develop if it moves too fast.",
            "Keep the mold a little below the kiln's hottest zone so the "
            "edges and the centre move at the same time.",
        ],
    },
    "fire_polish": {
        "name": "Fire polish",
        "range": (1300, 1350),
        "what": "A short trip above the softening point gives a cold-worked or "
                "sandblasted surface its gloss back without reshaping the piece.",
        "tips": [
            "Keep the hold short (5–10 min); it is a surface treatment, not a "
            "fuse.",
            "Edges that were ground sharp will round a little — plan for it.",
        ],
    },
    "casting": {
        "name": "Casting (pour / mold fill)",
        "range": (1500, 1550),
        "what": "Glass is hot enough to flow into a refractory mold through a "
                "flowerpot or reservoir and fill every detail. Thick work: slow "
                "heating, a long casting hold, then a very long anneal.",
        "tips": [
            "Heat slowly (100–200°F/hr) so mold and glass warm together.",
            "Hold long enough for the reservoir to empty and bubbles to rise — "
            "often 1–3 hours.",
            "Anneal for the thickest section, not the average. Use the "
            "thickness table on the Annealing tab.",
        ],
    },
    "bubble_squeeze": {
        "name": "Bubble squeeze",
        "range": (1150, 1250),
        "what": "A hold on the way up, when the glass is soft enough to settle "
                "but not yet sealed at the edges, lets air between layers escape "
                "instead of being trapped as bubbles.",
        "tips": [
            "20–30 min at ~1250°F is typical for two layers; a slow ramp "
            "(100–200°F/hr) into the squeeze helps more than a longer hold.",
            "The bubble squeeze also doubles as a safe 'equalize' hold so the "
            "whole stack reaches the same temperature.",
        ],
    },
    "devit": {
        "name": "Devitrification window (danger zone)",
        "range": (1300, 1400),
        "what": "In this band glass would rather be a crystal than a glass. "
                "Linger here — especially while cooling slowly — and the surface "
                "grows a dull, scummy crystalline skin called devitrification.",
        "tips": [
            "Go through this range fast on the way down: the crash-cool "
            "(AFAP) from fuse to anneal exists for this reason.",
            "Clean glass thoroughly before firing; dirt, fingerprints and "
            "kiln-wash dust seed devit.",
            "Some colours and surfaces devit more easily; a devit spray or a "
            "clear cap layer protects them.",
        ],
    },
    "annealing": {
        "name": "Annealing",
        "range": (900, 700),
        "what": "Holding at the anneal point (~900°F for COE-90) lets the whole "
                "piece reach one temperature and relax internal stress; then "
                "cooling slowly to the strain point (~700°F) keeps new stress "
                "from freezing in. Below the strain point the glass is rigid "
                "and only thermal shock can hurt it.",
        "tips": [
            "The soak and the 900→700°F cooling rate are where thickness "
            "matters most. See the Annealing tab.",
            "A guaranteed-soak tolerance on the anneal hold makes the timer "
            "wait until the glass is really at temperature.",
        ],
    },
    "crash_cool": {
        "name": "Rapid cool (crash) from peak to anneal",
        "range": (1465, 900),
        "what": "Right after the process hold the kiln is told to go to the "
                "anneal temperature 'as fast as possible'. It can only cool as "
                "fast as it loses heat, so this is safe for the glass — the "
                "point is to spend as little time as possible in the devit "
                "window.",
        "tips": [
            "Encoded as a 1-minute segment on this controller — the timer "
            "expires immediately and the setpoint drops to 900; the real "
            "temperature follows as the kiln loses heat.",
            "Don't open the lid to speed this up on thick work — the surface "
            "would cool far faster than the core.",
        ],
    },
    "furnace": {
        "name": "Furnace melting & holding (blown glass)",
        "range": (2000, 2150),
        "what": "A glass furnace charges batch or cullet at ~2100°F+, fines "
                "(lets bubbles rise) and then holds at a working temperature "
                "where the glass is honey-like enough to gather on a pipe. This "
                "is a continuous hold, not a program with an end.",
        "tips": [
            "The furnace controller runs an indefinite hold (program 1 on this "
            "app); the watchdog's job is to notice when it falls out of it.",
            "Gathering temperature is set per shop and per glass — the "
            "'expected setpoint' on the Settings page is what the watchdog "
            "watches against.",
        ],
    },
}


# ---------------------------------------------------------------------------
# PID terms (Novus N20K48 registers)
# ---------------------------------------------------------------------------
# Each entry: name, register, plain-English, what too-high looks like,
# what too-low looks like.
PID_TERMS: dict[str, dict] = {
    "pb": {
        "name": "Proportional band (Pb)",
        "register": REG_TUNE_PB,
        "unit": "% of span",
        "plain": "How far below the setpoint the controller starts easing off "
                 "the power. Inside the band, output is proportional to the "
                 "error: at the band's edge it's 100%, at the setpoint about "
                 "50% (the integral term shifts this).",
        "too_high": "Sluggish: the kiln creeps up to temperature and sits a "
                    "little under the setpoint (the integral term has to make "
                    "up the difference).",
        "too_low": "Twitchy: output snaps between full-on and off, the "
                   "temperature oscillates around the setpoint, and relays "
                   "chatter.",
    },
    "ir": {
        "name": "Integral rate (Ir)",
        "register": REG_TUNE_IR,
        "unit": "repeats per minute (Novus convention)",
        "plain": "How quickly the controller adds up a lingering error and "
                 "corrects for it. Novus expresses this as a RATE (repeats/min), "
                 "so a bigger number means a stronger, faster integral. It is "
                 "what removes the steady offset a proportional-only controller "
                 "leaves. Zero disables it.",
        "too_high": "Overshoot on every ramp and a slow wobble around holds — "
                    "the integral winds up during the climb and has to unwind.",
        "too_low": "The kiln settles a few degrees below (or above) the "
                   "setpoint and stays there; holds are at the wrong temperature.",
    },
    "dt": {
        "name": "Derivative time (dt)",
        "register": REG_TUNE_DT,
        "unit": "seconds",
        "plain": "How much the controller reacts to the SPEED of change. It "
                 "pulls power early when the temperature is rising fast towards "
                 "the setpoint, which damps overshoot. Zero disables it.",
        "too_high": "Nervous output that jumps around in response to sensor "
                    "noise; the chart looks fuzzy.",
        "too_low": "Overshoot at the top of every ramp as the kiln's thermal "
                   "momentum carries it past the setpoint.",
    },
    "ct": {
        "name": "PWM cycle time (Ct)",
        "register": REG_TUNE_CT,
        "unit": "seconds",
        "plain": "The controller delivers partial power by switching the "
                 "elements on for a fraction of each cycle. A 10-second cycle "
                 "at 30% output = on 3 s, off 7 s.",
        "too_high": "The temperature visibly ripples in time with the cycle, "
                    "and control is coarse.",
        "too_low": "Mechanical relays wear out fast. Short cycles (1–2 s) are "
                   "fine for solid-state relays only.",
    },
    "autotune": {
        "name": "Autotune (Atun)",
        "register": REG_TUNE_AUTO,
        "unit": "0 = off, otherwise a tuning mode",
        "plain": "The controller drives the kiln through a deliberate "
                 "oscillation, measures how it responds, and writes its own "
                 "Pb / Ir / dt. Run it with a typical load at a typical "
                 "temperature, once — not every firing.",
        "too_high": "(n/a) — but leaving a tuning mode active means every "
                    "start re-tunes and the first hours of a firing are wasted.",
        "too_low": "(n/a) — 0 just means it won't tune.",
    },
    "hyst": {
        "name": "Hysteresis (HySt)",
        "register": REG_HYST,
        "unit": "degrees",
        "plain": "Only used in ON/OFF control (proportional band = 0): the dead "
                 "band around the setpoint where the output doesn't switch. "
                 "In PID mode it is ignored.",
        "too_high": "Wide temperature swings — the kiln coasts well past the "
                    "setpoint each way.",
        "too_low": "Relay chatter: the output flips on and off every few "
                   "seconds.",
    },
    "soft_start": {
        "name": "Soft start (SFSt)",
        "register": REG_SOFT_START,
        "unit": "seconds",
        "plain": "A ramp-up limit on OUTPUT at power-up: the controller takes "
                 "this many seconds to be allowed to reach 100%, which goes "
                 "easy on cold elements and relays. Zero disables it.",
        "too_high": "The first ramp of every firing is slower than programmed "
                    "and the guaranteed-soak timer waits for it.",
        "too_low": "Nothing bad for most kilns; 0 is a common setting.",
    },
    "oull": {
        "name": "Output low limit (ouLL)",
        "register": REG_OULL,
        "unit": "%",
        "plain": "The output can never go below this. Normally 0.",
        "too_high": "The kiln cannot cool: even when it wants 0% it is still "
                    "being heated.",
        "too_low": "(n/a) — 0 is the normal value.",
    },
    "ouhl": {
        "name": "Output high limit (ouHL)",
        "register": REG_OUHL,
        "unit": "%",
        "plain": "The output can never go above this. A 70% cap is a cheap way "
                 "to protect elements or a weak circuit — at the cost of "
                 "slower ramps.",
        "too_high": "(n/a) — 100 is the normal value.",
        "too_low": "Ramps are power-limited (the Notebook flags these) and the "
                   "kiln may never reach a high setpoint at all.",
    },
}


# ---------------------------------------------------------------------------
# Ramp & soak terms
# ---------------------------------------------------------------------------
RS_TERMS: dict[str, dict] = {
    "segment": {
        "name": "Segment",
        "plain": "One row of a program: 'take N minutes to reach this setpoint'. "
                 "The setpoint moves in a straight line from the previous "
                 "segment's setpoint to this one over N minutes. A program holds "
                 "up to 9 segments.",
    },
    "sp0": {
        "name": "Start setpoint (SP0)",
        "plain": "Where the setpoint line begins when the program starts. For "
                 "a cold start that's room temperature (70°F); for an anneal "
                 "program that runs after a crash, it's the anneal temperature. "
                 "If SP0 is far from the kiln's real temperature, the first "
                 "segment's ramp rate is not what you think it is.",
    },
    "hold": {
        "name": "Hold (soak)",
        "plain": "A segment whose setpoint equals the previous one. The "
                 "controller sits at that temperature for the segment's minutes.",
    },
    "tolerance": {
        "name": "Tolerance / guaranteed soak",
        "plain": "A band around the setpoint. The segment timer only counts "
                 "while the real temperature (PV) is within ±tolerance of the "
                 "setpoint. With tolerance 0 the timer runs on the wall clock "
                 "and a 10-minute hold may end before the glass ever got there. "
                 "With ±5°F, the hold is ten minutes AT temperature. This is the "
                 "single highest-leverage setting for repeatable glass.",
    },
    "time_base": {
        "name": "Time base",
        "plain": "Whether segment times are in seconds or minutes (register "
                 "252). This app assumes minutes; a controller set to seconds "
                 "would run every program sixty times too fast.",
    },
    "link": {
        "name": "Link / chain",
        "plain": "A program can name the program to run next when it finishes, "
                 "so two 9-segment programs chain into one 18-segment firing. "
                 "0 means stop at the end.",
    },
    "events": {
        "name": "Event outputs",
        "plain": "Each segment can switch auxiliary outputs (an alarm, a vent, "
                 "a lamp) on or off. The standard programs here leave them at 0.",
    },
    "afap": {
        "name": "'As fast as possible' (AFAP)",
        "plain": "The controller has no 'full speed' instruction, so a crash "
                 "cool is written as a 1-minute segment to the target. The "
                 "setpoint drops immediately; the kiln follows as fast as it "
                 "physically can. With guaranteed soak on, the next segment "
                 "waits until the temperature actually arrives.",
    },
}


# ---------------------------------------------------------------------------
# Heat-work
# ---------------------------------------------------------------------------
HEAT_WORK_INTRO = (
    "Glass does not care what the program said; it responds to the heat it "
    "actually received — how hot, for how long, all added up. That running "
    "total is called heat-work. A slow kiln that takes forty minutes to crawl "
    "the last fifty degrees gives the piece more heat-work than a fast one "
    "that snaps to the setpoint and starts the timer, even if both report the "
    "same hold."
)

HEAT_WORK_WHY_DIFFERENT = (
    "The same program gives different glass when the kiln is fuller or "
    "emptier (more mass to heat), when the elements have aged (slower ramps, "
    "more time near the top), when the shelf or mold is thicker, when the "
    "room is colder, or when a Bluetooth dropout hides part of the curve. The "
    "program is the request; the logged temperature is what happened."
)

HEAT_WORK_NOTEBOOK = (
    "The Firing Notebook page reads the logged curve for a run and reports a "
    "coarse heat-work proxy: the time–temperature integral in °F·hours, and "
    "minutes spent near the peak (within the tolerance band of the highest "
    "target). It then compares each segment to what the program asked for — "
    "didn't reach target, power-limited ramp, short soak, dropouts — and "
    "suggests adjustments. Comparing two runs side by side shows why two "
    "pieces from 'the same program' came out different."
)

WATCHDOG_PLAIN = (
    "The furnace watchdog is the night-shift operator. It watches each "
    "furnace's temperature; if the furnace stays below a trigger temperature "
    "you set, the watchdog does exactly what you would do by hand — stop "
    "program 1 and start it again — and then waits to confirm the "
    "temperature climbs back before it relaxes. It will never drive the "
    "furnace on an implausible reading (a thermocouple fault reads very low, "
    "and heating 'to fix' that would be dangerous), it limits how many times "
    "it will retry, and when it gives up it alerts you and waits for a human."
)


def effective_rate(from_t: float, to_t: float, minutes: float) -> float:
    """Degrees per hour implied by travelling from from_t to to_t in
    `minutes`. Signed: positive heating, negative cooling. Zero or negative
    minutes returns 0.0 (an AFAP 1-minute segment gives a huge number, which
    the UI formats as 'AFAP' instead)."""
    if minutes <= 0:
        return 0.0
    return (to_t - from_t) / (minutes / 60.0)


# ---------------------------------------------------------------------------
# Program profile SVG
# ---------------------------------------------------------------------------

def _nice_step(span: float, target_ticks: int = 5) -> float:
    """A round gridline step (1/2/5 × 10^n) giving about target_ticks lines."""
    if span <= 0:
        return 1.0
    raw = span / max(1, target_ticks)
    mag = 10 ** math.floor(math.log10(raw))
    for mult in (1, 2, 5, 10):
        if mult * mag >= raw:
            return mult * mag
    return 10 * mag


def program_svg(segments: list, start_setpoint: float,
                width: int = 760, height: int = 300) -> str:
    """Inline SVG of a program's setpoint profile — time on the x axis
    (hours), temperature on the y axis (°F), drawn as a stepped/ramped line
    through each segment's endpoint. `segments` is [(minutes, setpoint), ...]
    (tuples, or anything with .minutes/.setpoint). No JavaScript and no
    external references, so it can be dropped into st.components.html or a
    saved HTML file. Styled like notebook.svg_chart: system-ui font and CSS
    variables with fallbacks."""
    pts: list[tuple[float, float]] = [(0.0, float(start_setpoint))]
    acc = 0.0
    for seg in segments:
        if isinstance(seg, (tuple, list)):
            minutes, sp = seg[0], seg[1]
        else:
            minutes, sp = getattr(seg, "minutes"), getattr(seg, "setpoint")
        acc += max(0.0, float(minutes)) / 60.0
        pts.append((acc, float(sp)))

    tmax = pts[-1][0] or 1.0
    vals = [v for _, v in pts]
    vmin, vmax = min(vals), max(vals)
    if vmax - vmin < 50:
        vmax = vmin + 50
    pad = (vmax - vmin) * 0.08
    vlo, vhi = vmin - pad, vmax + pad

    L, R, T, B = 54, 14, 14, 34

    def X(t: float) -> float:
        return L + (t / tmax) * (width - L - R)

    def Y(v: float) -> float:
        return T + (1 - (v - vlo) / (vhi - vlo or 1)) * (height - T - B)

    grid = ""
    ystep = _nice_step(vhi - vlo)
    v = math.ceil(vlo / ystep) * ystep
    while v <= vhi:
        y = Y(v)
        grid += (f"<line x1='{L}' y1='{y:.1f}' x2='{width - R}' y2='{y:.1f}' "
                 f"stroke='var(--grid,#e2e2e2)' stroke-width='1'/>"
                 f"<text x='{L - 6}' y='{y + 3:.1f}' text-anchor='end' font-size='10' "
                 f"fill='var(--muted,#888)'>{v:.0f}</text>")
        v += ystep
    xstep = _nice_step(tmax, 6)
    t = 0.0
    while t <= tmax + 1e-9:
        x = X(t)
        grid += (f"<line x1='{x:.1f}' y1='{T}' x2='{x:.1f}' y2='{height - B}' "
                 f"stroke='var(--grid,#eeeeee)' stroke-width='1'/>"
                 f"<text x='{x:.1f}' y='{height - B + 14}' text-anchor='middle' "
                 f"font-size='10' fill='var(--muted,#888)'>{t:g}h</text>")
        t += xstep

    path = "M" + " L".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in pts)
    dots = "".join(
        f"<circle cx='{X(t):.1f}' cy='{Y(v):.1f}' r='2.5' fill='var(--target,#c9a227)'/>"
        for t, v in pts[1:])

    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" font-family="system-ui,sans-serif">\n'
        f"{grid}\n"
        f"<line x1='{L}' y1='{height - B}' x2='{width - R}' y2='{height - B}' "
        f"stroke='var(--muted,#999)' stroke-width='1'/>\n"
        f"<line x1='{L}' y1='{T}' x2='{L}' y2='{height - B}' "
        f"stroke='var(--muted,#999)' stroke-width='1'/>\n"
        f'<path d="{path}" fill="none" stroke="var(--target,#c9a227)" stroke-width="2.2"/>\n'
        f"{dots}\n"
        f"<text x='{width - R}' y='{height - 4}' text-anchor='end' font-size='10' "
        f"fill='var(--muted,#888)'>hours (sum of segment times — holds may extend)</text>\n"
        f"<text x='{L + 4}' y='{T + 10}' font-size='10' fill='var(--muted,#888)'>°F</text>\n"
        f"</svg>"
    )
