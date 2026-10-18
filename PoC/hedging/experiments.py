"""Experimentos: E1 (A vs C2 en varias cargas) y E3 (escrituras por request), en procesos paralelos.

Uso:  python -m hedging.experiments --profile smoke   (~30 s, time-scale 4: indicativo)
      python -m hedging.experiments --profile full    (5.000 req x escenario, 3 semillas, ~3 min)
"""
from __future__ import annotations

import argparse
import dataclasses
import multiprocessing as mp
import os
import time

import pandas as pd

from .load import Scenario, hedge_delays, run_scenario
from .sim import SimConfig

PROFILES = {
    "smoke": dict(n_requests=1500, seeds=1, time_scale=4.0, rates=[50, 100, 200, 250]),
    "full": dict(n_requests=5000, seeds=3, time_scale=1.0, rates=[50, 100, 150, 200, 250]),
}


def build_scenarios(a, delays) -> list[Scenario]:
    base = Scenario(n_requests=a.requests, workers=a.workers, cancel_in_service=a.cancel_in_service,
                    deadline_ms=a.deadline_ms, time_scale=a.time_scale, hedge_delay_ms=delays,
                    budget=a.budget)
    os.makedirs(os.path.join(a.out, "lat"), exist_ok=True)
    sc = []
    for seed in range(a.seed_base, a.seed_base + a.seeds):
        for v in ("A", "C2"):
            for rate in a.rates:  # E1: baseline vs propuesta en todas las cargas
                lat = os.path.join(a.out, "lat", f"E1_{v}_{rate:g}_{seed}.npy") if rate == a.mid_rate else None
                sc.append(dataclasses.replace(base, experiment="E1", variant=v, rate=rate, seed=seed, save_lat=lat))
            for nw in (0, 4):  # E3: piso de p99 según nº de escrituras (E1 ya cubre el default = 2)
                sc.append(dataclasses.replace(base, experiment="E3", variant=v, rate=a.mid_rate, seed=seed,
                                              n_writes=nw))
    return sc


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", choices=PROFILES, default="smoke")
    p.add_argument("--out", default="results")
    p.add_argument("--requests", type=int)
    p.add_argument("--seeds", type=int)
    p.add_argument("--seed-base", type=int, default=1)
    p.add_argument("--time-scale", type=float, help="divide las duraciones; >1 => resultados indicativos")
    p.add_argument("--rates", type=lambda s: [float(x) for x in s.split(",")], help="ej. 50,100,200")
    p.add_argument("--mid-rate", type=float, default=100.0)
    p.add_argument("--workers", type=int, default=10, help="workers por réplica")
    p.add_argument("--cancel-in-service", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--deadline-ms", type=float, default=3000.0)
    p.add_argument("--hedge-delay-mode", choices=["fast_p95", "fixed_ms"], default="fast_p95")
    p.add_argument("--fixed-ms", type=float, default=25.0)
    p.add_argument("--budget", type=float, default=0.10)
    p.add_argument("--procs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    a = p.parse_args(argv)
    prof = PROFILES[a.profile]
    a.requests = a.requests or prof["n_requests"]
    for k in ("seeds", "time_scale", "rates"):
        if getattr(a, k) is None:
            setattr(a, k, prof[k])
    os.makedirs(a.out, exist_ok=True)

    # calibración única: luego el hedge delay queda FIJO (ver README)
    cfg = SimConfig(workers=a.workers, cancel_in_service=a.cancel_in_service)
    delays = hedge_delays(Scenario(time_scale=a.time_scale, hedge_delay_mode=a.hedge_delay_mode,
                                   fixed_ms=a.fixed_ms, workers=a.workers), cfg)
    print(f"Hedge delay ({a.hedge_delay_mode}): {min(delays.values()):.1f}-{max(delays.values()):.1f} ms")
    scs = build_scenarios(a, delays)
    procs = min(a.procs, len(scs))
    durs = [s.n_requests / s.rate / s.time_scale + 3 for s in scs]
    print(f"{len(scs)} escenarios, {procs} procesos. Estimación: ~{max(sum(durs) / procs, max(durs)) / 60:.1f} min")
    if a.time_scale > 1:
        print("AVISO: time-scale > 1 => resultados INDICATIVOS.")
    u = SimConfig(workers=a.workers).mean_service_ms().max() / 1000 / a.workers / 2
    print("Utilización del servicio propenso a 100/200/250 req/s: " + " / ".join(f"{r * u:.2f}" for r in (100, 200, 250)))

    scs.sort(key=lambda s: -s.n_requests / s.rate)
    rows, t0 = [], time.time()
    with mp.get_context("spawn").Pool(procs) as pool:
        for i, row in enumerate(pool.imap_unordered(run_scenario, scs), 1):
            rows.append(row)
            print(f"[{i}/{len(scs)}] {time.time() - t0:4.0f}s {row['experiment']} {row['variant']:>2} "
                  f"rate={row['rate']:.0f} p99={row['p99_ms']:.0f}ms hedge={row['hedge_rate']:.3f}", flush=True)
    pd.DataFrame(rows).to_csv(os.path.join(a.out, "results.csv"), index=False)
    from . import report
    report.make_all(a.out, mid_rate=a.mid_rate)


if __name__ == "__main__":
    main()
