"""Etapa 5 (parte 1): generador de carga open-loop, calibración del hedge delay y métricas de un escenario."""
from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass

import numpy as np

from .api import VARIANTS, Api
from .sim import SERVICES, SimConfig, make_draws

SLOW_THRESHOLD_MS = 300.0


@dataclass
class Scenario:
    experiment: str = "E1"
    variant: str = "A"
    rate: float = 100.0            # req/s (de modelo)
    seed: int = 1
    n_requests: int = 10000
    n_writes: int = 2              # escrituras por request (las últimas del catálogo)
    budget: float = 0.10           # fichas por llamada primaria (solo C1/C2)
    bucket_cap: float = 20.0       # tope del bucket (absorbe ráfagas: los hedges de una request salen juntos)
    correlation: float = 0.0
    workers: int = 10
    cancel_in_service: bool = True
    deadline_ms: float = 3000.0
    time_scale: float = 1.0
    arrival: str = "poisson"       # poisson | fixed
    hedge_delay_mode: str = "fast_p95"  # fast_p95 | fixed_ms
    fixed_ms: float = 25.0
    hedge_delay_ms: dict | None = None  # {svc: ms}; si es None y hace falta, se calibra
    save_lat: str | None = None    # ruta .npy donde guardar las latencias de requests completadas


def make_arrivals(n: int, rate: float, seed: int, kind: str) -> np.ndarray:
    """Instantes de llegada (s de modelo) independientes de las respuestas: open-loop."""
    if kind == "fixed":
        return np.arange(n) / rate
    rng = np.random.default_rng(np.random.SeedSequence([seed, 2]))
    return np.cumsum(rng.exponential(1.0 / rate, n))


async def _lag_monitor(lags: list[float]) -> None:
    """Duerme 10 ms y mide el desfase: lo que Python no cumple no es culpa del sistema simulado."""
    loop = asyncio.get_running_loop()
    while True:
        t = loop.time()
        await asyncio.sleep(0.010)
        lags.append((loop.time() - t - 0.010) * 1000.0)


async def _run(sc: Scenario, cfg: SimConfig, collect_svc: bool = False):
    variant = VARIANTS[sc.variant]
    draws = make_draws(cfg, sc.n_requests, sc.seed)
    arrivals = make_arrivals(sc.n_requests, sc.rate, sc.seed, sc.arrival)
    api = Api(variant, cfg, draws, sc.time_scale, sc.n_writes, sc.hedge_delay_ms,
              sc.budget, sc.bucket_cap, sc.deadline_ms)
    api.start()
    lags: list[float] = []
    mon = asyncio.create_task(_lag_monitor(lags))
    loop = asyncio.get_running_loop()
    t0 = loop.time() + 0.05
    tasks = []
    for i, a in enumerate(arrivals):
        intended = t0 + a / sc.time_scale
        dt = intended - loop.time()
        if dt > 0:
            await asyncio.sleep(dt)
        elif i % 50 == 0:
            await asyncio.sleep(0)  # vamos atrasados: ceder el loop de vez en cuando
        tasks.append(asyncio.create_task(api.handle(i, intended)))
    results = await asyncio.gather(*tasks)
    elapsed_model_s = (loop.time() - t0) * sc.time_scale
    mon.cancel()
    await asyncio.gather(mon, return_exceptions=True)
    await api.stop()
    return api, results, np.array(lags), elapsed_model_s


def _pct(x, q):
    return float(np.percentile(x, q)) if len(x) else float("nan")


def run_scenario(sc: Scenario) -> dict:
    """Ejecuta un escenario completo y devuelve una fila de métricas."""
    cfg = SimConfig(workers=sc.workers, correlation=sc.correlation, cancel_in_service=sc.cancel_in_service)
    variant = VARIANTS[sc.variant]
    if variant.hedge != "none" and sc.hedge_delay_ms is None:
        sc = dataclasses.replace(sc, hedge_delay_ms=hedge_delays(sc, cfg))
    api, results, lags, elapsed = asyncio.run(_run(sc, cfg))

    status = np.array([r[0] for r in results])
    lat = np.array([r[1] for r in results])
    ok = lat[status == "ok"]
    n_to, n_err = int((status == "timeout").sum()), int((status == "error").sum())
    lat_incl = np.where(status == "timeout", sc.deadline_ms, lat)[status != "error"]
    if sc.save_lat:
        np.save(sc.save_lat, ok)
    reps = api.all_replicas()
    prob_slow = 1 - np.prod(1 - cfg.p_slow())
    row = {
        **{k: v for k, v in dataclasses.asdict(sc).items() if k not in ("hedge_delay_ms", "save_lat")},
        "variant_name": variant.name,
        "requests": sc.n_requests, "completed": len(ok), "timeouts": n_to, "errors": n_err,
        "throughput_rps": len(ok) / elapsed,
        "p50_ms": _pct(ok, 50), "p95_ms": _pct(ok, 95), "p99_ms": _pct(ok, 99),
        "p99_incl_timeouts_ms": _pct(lat_incl, 99),  # timeouts contados como el deadline
        "read_call_p50_ms": _pct(api.read_lat, 50), "read_call_p95_ms": _pct(api.read_lat, 95),
        "read_call_p99_ms": _pct(api.read_lat, 99),
        "write_call_p50_ms": _pct(api.write_lat, 50), "write_call_p95_ms": _pct(api.write_lat, 95),
        "write_call_p99_ms": _pct(api.write_lat, 99),
        "primary_calls": api.primary_calls, "read_primary_calls": api.read_primary,
        "copies": api.copies, "copies_writes": api.copies_writes, "copies_on_error": api.copies_on_error,
        "hedge_rate": api.copies / api.primary_calls,
        "hedge_rate_reads": api.copies / max(api.read_primary, 1),
        "hedges_blocked": api.blocked,
        "cancel_queue": sum(r.cancel_queue for r in reps),
        "cancel_service": sum(r.cancel_service for r in reps),
        "wasted_ms": sum(r.wasted_ms for r in reps),
        "max_active": max(r.max_active for r in reps),
        "max_queue": max(r.max_queue for r in reps),
        "prio_violations": sum(r.prio_violations for r in reps),
        "lag_p99_ms": _pct(lags, 99), "lag_max_ms": float(lags.max()) if len(lags) else float("nan"),
        "slow_frac": float((ok > SLOW_THRESHOLD_MS).mean()) if len(ok) else float("nan"),
        "slow_frac_theory": float(prob_slow),
        "elapsed_model_s": elapsed,
    }
    return row


def hedge_delays(sc: Scenario, cfg: SimConfig) -> dict[str, float]:
    """Hedge delay por servicio, FIJO durante el experimento (ver README: por qué p95 de la dist. RÁPIDA)."""
    if sc.hedge_delay_mode == "fixed_ms":
        return {s: sc.fixed_ms for s in SERVICES}
    return calibrate(cfg, sc.time_scale, sc.n_writes)


def calibrate(cfg: SimConfig, scale: float, n_writes: int = 2, n_requests: int = 1500,
              rate: float = 150.0, seed: int = 987654) -> dict[str, float]:
    """Fase de calibración: se corre el sistema SIN lentitud ni errores y se mide el p95 por servicio."""
    fast_cfg = dataclasses.replace(cfg, p_slow_prone=0.0, p_slow_other=0.0, p_error=0.0)
    sc = Scenario(experiment="calib", variant="A", rate=rate, seed=seed, n_requests=n_requests,
                  n_writes=n_writes, time_scale=scale, workers=cfg.workers)
    api, _, _, _ = asyncio.run(_run(sc, fast_cfg))
    return {s: float(np.percentile(api.svc_lat[s], 95)) for s in SERVICES}


def utilization(cfg: SimConfig, rate: float) -> np.ndarray:
    """Utilización teórica por réplica: rho_s = (rate/2) * E[S_s] / workers (sin copias)."""
    return (rate / 2.0) * cfg.mean_service_ms() / 1000.0 / cfg.workers
