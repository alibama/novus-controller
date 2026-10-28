"""
glass_science sanity checks. Pure data + calculators, no hardware.
Run with:  pytest tests/test_glass_science.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

import glass_science as gs


def test_anneal_table_monotonic():
    # Thicker glass: longer soak, slower (smaller) cooling rates, in order.
    rows = gs.ANNEAL_TABLE
    assert len(rows) >= 8
    for a, b in zip(rows, rows[1:]):
        assert b.thickness_mm > a.thickness_mm
        assert b.soak_minutes > a.soak_minutes, (a, b)
        assert b.rate_to_800 <= a.rate_to_800, (a, b)
    # The 6mm row is the common two-layer schedule (150/150/300), a different
    # source from the thick-slab chart that starts at 12mm (100/180/600), so
    # the later-stage rates are only strictly monotonic from 12mm onward.
    thick = [r for r in rows if r.thickness_mm >= 12]
    for a, b in zip(thick, thick[1:]):
        assert b.rate_to_700 < a.rate_to_700, (a, b)
        assert b.rate_to_70 < a.rate_to_70, (a, b)
    for r in rows:
        assert r.soak_temp == 900
        # within a row the cools get faster as the glass gets colder
        assert r.rate_to_800 <= r.rate_to_700 <= r.rate_to_70


def test_anneal_schedule_picks_next_thicker():
    s = gs.anneal_schedule(30)
    assert s["thickness_mm"] == 38
    assert s["soak_minutes"] == 360
    assert not s["extrapolated"]
    assert gs.anneal_schedule(25)["thickness_mm"] == 25      # exact hit
    assert gs.anneal_schedule(6)["thickness_mm"] == 6
    assert gs.anneal_schedule(1)["thickness_mm"] == 6        # thinner than table
    big = gs.anneal_schedule(150)
    assert big["thickness_mm"] == 100 and big["extrapolated"]


def test_anneal_segments_25mm():
    segs = gs.anneal_segments(25)
    assert 1 <= len(segs) <= 9
    for minutes, sp in segs:
        assert isinstance(minutes, int)
        assert 1 <= minutes <= gs.MAX_SEGMENT_MINUTES
    assert segs[-1][1] == 70
    # a 4h hold at 900 is in there
    assert (240, 900.0) in segs
    # cools pass through 800 and 700 in order
    sps = [sp for _, sp in segs]
    assert sps.index(800.0) < sps.index(700.0) < sps.index(70.0)
    # expected rates: 27 -> 800, 49 -> 700, 162 -> 70
    i800 = sps.index(800.0)
    assert segs[i800][0] == round(100 / 27 * 60)
    assert segs[i800 + 1][0] == round(100 / 49 * 60)
    assert segs[i800 + 2][0] == round(630 / 162 * 60)


def test_anneal_segments_from_anneal_temp_has_no_heat():
    segs = gs.anneal_segments(12, start_temp=900)
    assert segs[0] == (120, 900.0)
    assert len(segs) == 4
    assert segs[-1][1] == 70


def test_anneal_segments_every_row_fits():
    for r in gs.ANNEAL_TABLE:
        for start in (70, 900):
            segs = gs.anneal_segments(r.thickness_mm, start_temp=start)
            assert len(segs) <= 9
            assert all(isinstance(m, int) and m >= 1 for m, _ in segs)
            assert segs[-1][1] == 70


def test_split_segment_caps_minutes():
    out = gs._split_segment(25000, 900, 700)
    assert len(out) == 3
    assert all(1 <= m <= gs.MAX_SEGMENT_MINUTES for m, _ in out)
    assert out[-1][1] == 700


def test_effective_rate():
    assert gs.effective_rate(70, 1000, 60) == 930
    assert gs.effective_rate(900, 700, 120) == -100
    assert gs.effective_rate(900, 900, 30) == 0
    assert gs.effective_rate(70, 1000, 0) == 0.0


def test_program_svg_is_self_contained():
    segs = [(140, 1000), (20, 1000), (1, 900), (60, 900), (80, 700)]
    svg = gs.program_svg(segs, 70)
    assert isinstance(svg, str)
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    assert "<script" not in svg.lower()
    assert svg.replace("http://www.w3.org/2000/svg", "").count("http://") == 0
    assert "https://" not in svg
    # accepts objects with .minutes/.setpoint too
    from programs_library import LIBRARY
    p = LIBRARY["full_fuse"]
    svg2 = gs.program_svg(p.segments, p.start_setpoint)
    assert svg2.startswith("<svg")


def test_pid_terms_have_registers_and_prose():
    from novus_protocol import (REG_TUNE_PB, REG_TUNE_IR, REG_TUNE_DT,
                                REG_TUNE_CT, REG_TUNE_AUTO, REG_HYST,
                                REG_SOFT_START, REG_OULL, REG_OUHL)
    assert gs.PID_TERMS["pb"]["register"] == REG_TUNE_PB == 258
    assert gs.PID_TERMS["ir"]["register"] == REG_TUNE_IR == 259
    assert gs.PID_TERMS["dt"]["register"] == REG_TUNE_DT == 260
    assert gs.PID_TERMS["ct"]["register"] == REG_TUNE_CT == 261
    assert gs.PID_TERMS["autotune"]["register"] == REG_TUNE_AUTO == 257
    assert gs.PID_TERMS["hyst"]["register"] == REG_HYST == 219
    assert gs.PID_TERMS["soft_start"]["register"] == REG_SOFT_START == 226
    assert gs.PID_TERMS["oull"]["register"] == REG_OULL == 224
    assert gs.PID_TERMS["ouhl"]["register"] == REG_OUHL == 225
    for t in gs.PID_TERMS.values():
        for k in ("name", "plain", "too_high", "too_low"):
            assert t[k]
    for t in gs.RS_TERMS.values():
        assert t["name"] and t["plain"]


def test_processes_have_ranges_and_tips():
    for key, p in gs.PROCESSES.items():
        lo, hi = p["range"]
        assert 60 < lo < 2300 and 60 < hi < 2300, key
        assert p["what"] and p["tips"], key
    assert gs.PROCESSES["full_fuse"]["range"][0] >= 1465
    assert gs.PROCESSES["annealing"]["range"] == (900, 700)
