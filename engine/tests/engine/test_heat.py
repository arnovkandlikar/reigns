"""FR-B7 / §9 heat algorithm."""
from app.heat import HeatState, level_for


def test_levels_boundaries():
    assert [level_for(h) for h in (0, 10, 11, 30, 31, 55, 56, 80, 81, 100)] == [
        0, 0, 1, 1, 2, 2, 3, 3, 4, 4]


def test_adds_and_clamp():
    h = HeatState()
    h.add_claims([("a", "red", False), ("b", "amber", False), ("c", "red", True)], now=0)
    assert h.heat == 25 + 8 + 15
    h.add_claims([(str(i), "red", False) for i in range(10)], now=1)
    assert h.heat == 100


def test_clean_reply_subtracts_and_floor():
    h = HeatState()
    h.add_claims([("a", "green", False)], now=0)
    assert h.heat == 0
    h.add_claims([("a", "red", False)], now=0)
    h.add_claims([("b", "green", False), ("c", "skipped", False)], now=1)
    assert h.heat == 15


def test_disagree_removes_contribution():
    h = HeatState()
    h.add_claims([("a", "red", False), ("b", "amber", False)], now=0)
    assert h.disagree("a", now=1)
    assert h.heat == 8
    assert not h.disagree("a", now=1)


def test_verified_fix_and_recovered_window():
    h = HeatState()
    h.add_claims([("a", "red", False), ("b", "red", False)], now=0)
    h.verified_fix(now=5)
    assert h.heat == 30
    assert h.recovered(6) and not h.recovered(8.1)


def test_decay_one_per_30s_since_last_increase():
    h = HeatState()
    h.add_claims([("a", "red", False)], now=0)
    assert h.target_level(now=29) == 1 and h.heat == 25
    h.target_level(now=90)
    assert h.heat == 22
    h.add_claims([("b", "amber", False)], now=100)  # increase resets the decay clock
    assert h.heat == 22 + 8
    h.target_level(now=129)
    assert h.heat == 30
    h.target_level(now=130)
    assert h.heat == 29


def test_hysteresis_two_seconds():
    h = HeatState()
    h.add_claims([("a", "red", False), ("b", "red", False), ("c", "red", False)], now=0)
    assert h.tick(0) == 0
    assert h.tick(1.9) == 0
    assert h.tick(2.0) == 3
