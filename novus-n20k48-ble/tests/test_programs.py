"""
Standard-program sanity checks. Pure data, no hardware.
Run with:  pytest tests/test_programs.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import programs_library as lib


def test_library_nonempty_and_keyed():
    assert lib.LIBRARY
    for key, prog in lib.LIBRARY.items():
        assert prog.key == key


def test_segment_limit_respected():
    # The controller allows at most 9 segments per program.
    for prog in lib.LIBRARY.values():
        assert 1 <= len(prog.segments) <= 9, prog.key


def test_anneal_down_matches_spec():
    # 3h hold @895, 3h->750, 3h->350, 5h->room, all in minutes.
    a = lib.LIBRARY["anneal_down"]
    assert a.start_setpoint == 895
    steps = [(s.minutes, s.setpoint) for s in a.segments]
    assert steps == [(180, 895), (180, 750), (180, 350), (300, 70)]


def test_times_are_positive_minutes():
    for prog in lib.LIBRARY.values():
        for seg in prog.segments:
            assert seg.minutes >= 1, f"{prog.key} has a non-positive segment time"


def test_fuse_anneals_then_cools_through_strain():
    # Full fuse must drop to an anneal soak and then cool (not end hot).
    f = lib.LIBRARY["full_fuse"]
    setpoints = [s.setpoint for s in f.segments]
    assert max(setpoints) >= 1450        # reaches a fuse temperature
    assert setpoints[-1] <= 750          # ends in/through the annealing range
