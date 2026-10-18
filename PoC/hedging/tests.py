"""Validaciones. Correr:  pytest -q hedging/tests.py   (~1 min, time-scale alto: indicativo)."""
import asyncio
import math

import numpy as np

from .api import VARIANTS, Api, build_catalog
from .load import Scenario, run_scenario
from .sim import Replica, SimConfig, make_draws


def sc(**kw):
    base = dict(n_requests=2000, time_scale=8.0, hedge_delay_mode="fixed_ms", fixed_ms=22.0, rate=50.0)
    base.update(kw)
    return Scenario(**base)


def test_A_slow_fraction_matches_theory():
    r = run_scenario(sc(variant="A", n_requests=4000))
    theo = r["slow_frac_theory"]
    assert abs(theo - 0.3205) < 1e-3
    assert abs(r["slow_frac"] - theo) < 4 * math.sqrt(theo * (1 - theo) / 4000) + 0.005


def test_no_writes_copied_and_budget_respected():
    r = run_scenario(sc(variant="C2", rate=150.0, budget=0.05))
    assert r["copies_writes"] == 0
    assert r["copies"] - r["copies_on_error"] <= 0.05 * r["read_primary_calls"] + r["bucket_cap"]
    assert r["prio_violations"] == 0


def test_same_seed_same_result():
    d1, d2 = make_draws(SimConfig(), 500, 7), make_draws(SimConfig(), 500, 7)
    assert np.array_equal(d1.service_ms, d2.service_ms) and np.array_equal(d1.primary, d2.primary)
    a, b = run_scenario(sc(variant="C2", seed=7)), run_scenario(sc(variant="C2", seed=7))
    assert a["primary_calls"] == b["primary_calls"]
    assert abs(a["p50_ms"] - b["p50_ms"]) < 0.15 * a["p50_ms"]  # solo jitter del scheduler


def test_priority_queue_order_unit():
    """Con la réplica ocupada, las primarias encoladas después entran antes que las copias."""
    async def go():
        rep = Replica("t", SimConfig(workers=1), 1.0, check_priority=True)
        rep.start()
        order = []
        rep.submit(50, False, 0)
        await asyncio.sleep(0.005)
        for tag, prio in (("copia", 1), ("copia", 1), ("prim", 0), ("prim", 0)):
            rep.submit(5, False, prio).fut.add_done_callback(lambda f, t=tag: order.append(t))
        await asyncio.sleep(0.3)
        await rep.stop()
        return order, rep.prio_violations
    order, viol = asyncio.run(go())
    assert order == ["prim", "prim", "copia", "copia"] and viol == 0


def test_catalog_decides_never_the_client():
    cfg = SimConfig()
    api = Api(VARIANTS["C2"], cfg, make_draws(cfg, 1, 1), 1.0, 2, {}, 0.1, 20, 3000)
    assert api._can_hedge("svc_1", opt_out=False) and not api._can_hedge("svc_1", opt_out=True)
    assert not api._can_hedge("svc_9", opt_out=False) and not api._can_hedge("svc_10", opt_out=False)
    assert [s for s, e in build_catalog(2).items() if e["op"] == "write"] == ["svc_9", "svc_10"]
