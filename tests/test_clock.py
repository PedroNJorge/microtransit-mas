import asyncio
import time

import pytest

from common import clock


@pytest.fixture(autouse=True)
def _reset():
    clock.reset()
    yield
    clock.reset()


def test_now_scales_real_time():
    clock.start(speed=100)
    t0 = clock.now()
    time.sleep(0.1)
    assert 8 <= clock.now() - t0 <= 14                 # ~10 sim seconds


def test_start_resets_to_zero_and_sets_speed():
    clock.start(speed=100)
    assert clock.now() < 1.0 and clock.speed() == 100


def test_lazy_start_on_first_use():
    assert not clock.is_started()
    assert clock.now() < 1.0
    assert clock.is_started()


def test_speed_change_does_not_move_time():
    clock.start(speed=100)
    time.sleep(0.1)
    before = clock.now()                               # ~10 sim s
    clock.set_simulation_speed(1000)
    after = clock.now()
    assert 0 <= after - before < 3                     # continuous, no jump to ~100
    assert clock.speed() == 1000
    time.sleep(0.05)
    assert clock.now() - after > 30                    # now runs at the new speed (~50 sim s)


def test_now_is_monotonic_across_speed_changes():
    clock.start(speed=50)
    seen = []
    for sp in (50, 5, 500, 10):
        clock.set_simulation_speed(sp)
        seen.append(clock.now())
        time.sleep(0.01)
        seen.append(clock.now())
    assert seen == sorted(seen)


def test_reset_clock_keeps_speed_and_restarts_at_zero():
    clock.start(speed=100)
    time.sleep(0.05)
    clock.reset_clock()
    assert clock.speed() == 100 and clock.now() < 1.0


def test_conversions_and_compat_attribute():
    clock.start(speed=20)
    assert clock.to_real(100) == 5 and clock.to_sim(5) == 100
    assert clock.SIMULATION_SPEED == 20                # old attribute stays in sync
    clock.set_simulation_speed(40)
    assert clock.SIMULATION_SPEED == 40


@pytest.mark.parametrize("bad", [0, -1])
def test_invalid_speed_rejected(bad):
    with pytest.raises(ValueError):
        clock.start(speed=bad)
    with pytest.raises(ValueError):
        clock.set_simulation_speed(bad)
    with pytest.raises(ValueError):
        clock.reset(bad)


@pytest.mark.asyncio
async def test_sim_sleep_takes_scaled_real_time():
    clock.start(speed=100)
    t0 = time.monotonic()
    await clock.sim_sleep(10)                          # 0.1 real seconds
    assert 0.08 <= time.monotonic() - t0 <= 0.25


@pytest.mark.asyncio
async def test_sim_sleep_zero_or_negative_is_noop():
    await asyncio.wait_for(clock.sim_sleep(-5), timeout=0.1)
    await asyncio.wait_for(clock.sim_sleep(0), timeout=0.1)
