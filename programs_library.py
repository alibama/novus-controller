"""
programs_library.py
===================
Standard kiln programs as data, ready to WRITE into a Novus program slot.

UNITS: all temperatures °F. The Novus R&S engine is TIME-based: each segment
is (minutes_to_reach_setpoint, setpoint). A "hold" is a segment whose setpoint
equals the previous one. "AFAP" (as-fast-as-possible) cooling is encoded as a
1-minute ramp to the target — the kiln simply cools as fast as it physically
can, since it can't actually hit that rate.

Each program has up to 9 segments (the controller's limit), a start_setpoint
(SP0), and a tolerance band (guaranteed soak: the segment timer only advances
while |PV - SP| <= tolerance — this is what makes holds happen at the real
temperature instead of on the wall clock).

IMPORTANT — these are STARTING TEMPLATES, not gospel:
  * Tuned for Bullseye/COE-90, ~6mm (two-layer / quarter-inch) work.
  * Fusing/slumping/casting results depend on glass type, thickness, mold,
    and YOUR kilns. Test on small samples and adjust holds/rates.
  * Thicker work needs longer anneal holds and slower cooling through the
    strain zone (~900 -> 700°F). For thick slabs/castings use Bullseye's
    "Annealing Thick Slabs" chart.
Sources cross-checked: Bullseye "Writing Firing Schedules" and "Annealing
Thick Slabs" (°F), plus common COE-90 community schedules.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StdSegment:
    minutes: int        # time to reach `setpoint` from the previous setpoint
    setpoint: float     # °F
    event: int = 0


@dataclass
class StdProgram:
    key: str
    name: str
    description: str
    start_setpoint: float
    tolerance: float
    segments: list[StdSegment] = field(default_factory=list)
    # role hint: which kind of device this is meant for (informational)
    intended: str = "kiln"


def _ramp_minutes(from_t: float, to_t: float, rate_per_hr: float) -> int:
    """Minutes to go from from_t to to_t at rate_per_hr (°F/hour)."""
    return max(1, round(abs(to_t - from_t) / rate_per_hr * 60))


AFAP = 1   # minutes — encodes "as fast as possible" for cooling segments


# --- 1. HOLD 896 — annealer "stay on" -------------------------------------
# Fills all 9 segments at 896 so it holds for ~weeks before the program ends,
# the same pattern your furnace uses to stay hot indefinitely.
HOLD_896 = StdProgram(
    key="hold_896",
    name="Hold 896 (stay on)",
    description="Hold 896°F indefinitely. For an idle annealer kept warm.",
    start_setpoint=896, tolerance=5, intended="kiln",
    segments=[StdSegment(5999, 896) for _ in range(9)],
)


# --- 2. ANNEAL DOWN — your exact spec -------------------------------------
# 3h hold @895, 3h down to 750, 3h down to 350, 5h down to room.
ANNEAL_DOWN = StdProgram(
    key="anneal_down",
    name="Anneal down (shutdown)",
    description="3h hold @895, 3h→750, 3h→350, 5h→room. Controlled cooldown.",
    start_setpoint=895, tolerance=5, intended="kiln",
    segments=[
        StdSegment(180, 895),   # 3h hold at anneal
        StdSegment(180, 750),   # 3h down
        StdSegment(180, 350),   # 3h down
        StdSegment(300, 70),    # 5h to room
    ],
)


# --- 3. FULL FUSE — Bullseye COE-90, ~6mm ---------------------------------
FULL_FUSE = StdProgram(
    key="full_fuse",
    name="Full fuse (6mm, COE90)",
    description="Two-layer / 6mm full fuse, ~1465°F. Template — verify for your glass.",
    start_setpoint=70, tolerance=7, intended="kiln",
    segments=[
        StdSegment(_ramp_minutes(70, 1000, 400), 1000),  # initial heat 400°F/hr
        StdSegment(20, 1000),                            # equalize hold
        StdSegment(_ramp_minutes(1000, 1250, 600), 1250),  # to bubble squeeze
        StdSegment(30, 1250),                            # bubble squeeze hold
        StdSegment(_ramp_minutes(1250, 1465, 600), 1465),  # to full fuse
        StdSegment(10, 1465),                            # fuse hold
        StdSegment(AFAP, 900),                           # crash to anneal
        StdSegment(60, 900),                             # anneal hold (6mm)
        StdSegment(_ramp_minutes(900, 700, 150), 700),   # cool through strain
    ],
)


# --- 4. TACK FUSE — softer bond, edges retained ---------------------------
TACK_FUSE = StdProgram(
    key="tack_fuse",
    name="Tack fuse (6mm, COE90)",
    description="~1350°F tack — pieces bond but keep shape. Template.",
    start_setpoint=70, tolerance=7, intended="kiln",
    segments=[
        StdSegment(_ramp_minutes(70, 1000, 400), 1000),
        StdSegment(20, 1000),
        StdSegment(_ramp_minutes(1000, 1350, 500), 1350),  # to tack temp
        StdSegment(10, 1350),
        StdSegment(AFAP, 900),
        StdSegment(60, 900),
        StdSegment(_ramp_minutes(900, 700, 150), 700),
    ],
)


# --- 5. SLUMP — shaping over/into a mold (after a fuse) --------------------
SLUMP = StdProgram(
    key="slump",
    name="Slump (~1225°F, COE90)",
    description="Slump into a mold, ~1225°F. Temp/hold depend heavily on the mold — adjust.",
    start_setpoint=70, tolerance=7, intended="kiln",
    segments=[
        StdSegment(_ramp_minutes(70, 1000, 400), 1000),
        StdSegment(15, 1000),
        StdSegment(_ramp_minutes(1000, 1225, 300), 1225),  # slow to slump temp
        StdSegment(12, 1225),                              # slump hold (mold-dependent)
        StdSegment(AFAP, 900),
        StdSegment(45, 900),
        StdSegment(_ramp_minutes(900, 700, 150), 700),
    ],
)


# --- 6. CASTING — small casting (~1") -------------------------------------
# Thick work: slow heat, long melt hold, LONG anneal + slow strain-zone cool
# per Bullseye Annealing Thick Slabs (~1"/25mm: 4h anneal, 27°F/hr→800,
# 49°F/hr→700, 162°F/hr→70). Bigger/thicker castings need MUCH longer — use
# the Bullseye chart for the actual thickness.
CASTING = StdProgram(
    key="casting_1in",
    name="Casting ~1in (COE90)",
    description="Small casting up to ~1in. Slow heat, long anneal. Thicker = far longer; use Bullseye thick-slab chart.",
    start_setpoint=70, tolerance=10, intended="kiln",
    segments=[
        StdSegment(_ramp_minutes(70, 1250, 150), 1250),   # slow initial heat
        StdSegment(30, 1250),                             # equalize
        StdSegment(_ramp_minutes(1250, 1500, 200), 1500), # to casting temp
        StdSegment(120, 1500),                            # casting hold (mold fill)
        StdSegment(AFAP, 900),                            # crash to anneal
        StdSegment(240, 900),                             # 4h anneal hold (~1")
        StdSegment(_ramp_minutes(900, 800, 27), 800),     # 1st cool 27°F/hr
        StdSegment(_ramp_minutes(800, 700, 49), 700),     # 2nd cool 49°F/hr
        StdSegment(_ramp_minutes(700, 70, 162), 70),      # final cool 162°F/hr
    ],
)


# --- 7. FIRE POLISH — quick gloss without full melt -----------------------
FIRE_POLISH = StdProgram(
    key="fire_polish",
    name="Fire polish (~1330°F)",
    description="Light surface gloss without reshaping. Template.",
    start_setpoint=70, tolerance=7, intended="kiln",
    segments=[
        StdSegment(_ramp_minutes(70, 1000, 400), 1000),
        StdSegment(15, 1000),
        StdSegment(_ramp_minutes(1000, 1330, 500), 1330),
        StdSegment(8, 1330),
        StdSegment(AFAP, 900),
        StdSegment(45, 900),
        StdSegment(_ramp_minutes(900, 700, 150), 700),
    ],
)


LIBRARY: dict[str, StdProgram] = {
    p.key: p for p in [
        HOLD_896, ANNEAL_DOWN, FULL_FUSE, TACK_FUSE, SLUMP, CASTING, FIRE_POLISH,
    ]
}


def total_minutes(p: StdProgram) -> int:
    return sum(s.minutes for s in p.segments)
