"""Etapa 1: servicios simulados con capacidad limitada, cola con prioridad y cancelación.

Unidades: todo el modelo se expresa en milisegundos "de modelo". Con --time-scale = k,
las esperas reales son k veces más cortas (ms_modelo / 1000 / k segundos reales).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import numpy as np

SERVICES = [f"svc_{i}" for i in range(1, 11)]
PRONE = {"svc_2", "svc_5", "svc_8"}  # servicios "propensos" a lentitud

# Estados de un job dentro de la réplica
QUEUED, RUNNING, DONE, CANCELLED = range(4)


@dataclass
class SimConfig:
    workers: int = 10              # workers por réplica (calibrado: ver README, saturación ~200 req/s)
    p_slow_prone: float = 0.10     # p_slow de svc_2, svc_5, svc_8
    p_slow_other: float = 0.01     # p_slow del resto
    p_error: float = 0.001         # probabilidad de falla
    median_ms: float = 12.0        # mediana lognormal (modo rápido)
    sigma: float = 0.35            # sigma lognormal: 5%-95% ~ 6.7-21 ms
    slow_lo_ms: float = 300.0
    slow_hi_ms: float = 1500.0
    correlation: float = 0.0       # P(réplica B hereda el modo lento de A)
    cancel_in_service: bool = True # True: cancelar libera el worker; False: termina y desperdicia

    def p_slow(self) -> np.ndarray:
        return np.array([self.p_slow_prone if s in PRONE else self.p_slow_other for s in SERVICES])

    def mean_service_ms(self) -> np.ndarray:
        """Tiempo medio de servicio por servicio (para calcular utilización)."""
        fast = self.median_ms * np.exp(self.sigma ** 2 / 2)
        slow = (self.slow_lo_ms + self.slow_hi_ms) / 2
        p = self.p_slow()
        return (1 - p) * fast + p * slow


@dataclass
class Draws:
    """Sorteos pre-generados por (request, servicio, réplica): common random numbers."""
    service_ms: np.ndarray  # (N, S, 2) tiempo de servicio (ms de modelo)
    fail: np.ndarray        # (N, S, 2) bool: la réplica responde con error
    primary: np.ndarray     # (N, S)    réplica primaria (0=A, 1=B)
    is_slow: np.ndarray     # (N, S, 2) bool: modo lento (solo para diagnóstico)


def make_draws(cfg: SimConfig, n_requests: int, seed: int) -> Draws:
    """Todas las variantes con la misma semilla enfrentan exactamente las mismas desgracias."""
    rng = np.random.default_rng(np.random.SeedSequence([seed, 1]))
    n, s = n_requests, len(SERVICES)
    u_slow = rng.random((n, s, 2))
    fast = rng.lognormal(np.log(cfg.median_ms), cfg.sigma, (n, s, 2))
    slow = rng.uniform(cfg.slow_lo_ms, cfg.slow_hi_ms, (n, s, 2))
    u_err = rng.random((n, s, 2))
    u_corr = rng.random((n, s))
    primary = rng.integers(0, 2, (n, s)).astype(np.int8)

    is_slow = u_slow < cfg.p_slow()[None, :, None]  # independiente por réplica
    # Correlación: con prob. `correlation`, B hereda el modo lento de A
    inherit = u_corr < cfg.correlation
    is_slow[:, :, 1] = np.where(inherit, is_slow[:, :, 0], is_slow[:, :, 1])

    fail = u_err < cfg.p_error
    service = np.where(is_slow, slow, fast)
    service = np.where(fail, fast, service)  # un error responde rápido (falla "visible")
    return Draws(service, fail, primary, is_slow)


class Job:
    __slots__ = ("svc_ms", "svc_s", "will_fail", "prio", "state", "t_start", "work", "fut", "seq")

    def __init__(self, svc_ms: float, scale: float, will_fail: bool, prio: int):
        self.svc_ms = svc_ms
        self.svc_s = svc_ms / 1000.0 / scale
        self.will_fail = will_fail
        self.prio = prio
        self.state = QUEUED
        self.t_start = 0.0
        self.work: asyncio.Future | None = None  # termina cuando acaba el servicio (o se cancela)
        self.fut: asyncio.Future | None = None   # resultado hacia quien llamó: True=ok, False=error
        self.seq = 0


class Replica:
    """Una réplica: N workers que consumen de una PriorityQueue (0=primaria, 1=copia)."""

    def __init__(self, name: str, cfg: SimConfig, scale: float, check_priority: bool = False):
        self.name = name
        self.workers = cfg.workers
        self.cancel_in_service = cfg.cancel_in_service
        self.scale = scale
        self.check_priority = check_priority  # solo se valida cuando las copias van con prioridad baja
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = 0
        self._tasks: list[asyncio.Task] = []
        self.waiting = [0, 0]  # jobs vivos en cola por prioridad
        self.active = 0
        # métricas
        self.max_active = 0
        self.max_queue = 0
        self.cancel_queue = 0
        self.cancel_service = 0
        self.wasted_ms = 0.0
        self.prio_violations = 0

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._worker()) for _ in range(self.workers)]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def submit(self, svc_ms: float, will_fail: bool, prio: int) -> Job:
        job = Job(svc_ms, self.scale, will_fail, prio)
        job.fut = asyncio.get_running_loop().create_future()
        self._seq += 1
        job.seq = self._seq
        self.waiting[prio] += 1
        self.max_queue = max(self.max_queue, self.waiting[0] + self.waiting[1])
        self.queue.put_nowait((prio, self._seq, job))  # (prio, seq): FIFO dentro de cada prioridad
        return job

    def cancel(self, job: Job) -> None:
        if job.state == QUEUED:
            # Sigue en cola: se marca; el worker la salta y no consume capacidad
            job.state = CANCELLED
            self.waiting[job.prio] -= 1
            self.cancel_queue += 1
        elif job.state == RUNNING:
            job.state = CANCELLED
            self.cancel_service += 1
            if self.cancel_in_service:
                # libera el worker de inmediato; lo ya trabajado es desperdicio
                loop = asyncio.get_running_loop()
                self.wasted_ms += (loop.time() - job.t_start) * 1000.0 * self.scale
                if not job.work.done():
                    job.work.set_result(None)
            # si no: el worker termina el servicio y suma el desperdicio completo (ver _worker)

    async def _worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            prio, _, job = await self.queue.get()
            if job.state == CANCELLED:
                continue  # cancelada en cola
            self.waiting[prio] -= 1
            if self.check_priority and prio == 1 and self.waiting[0] > 0:
                self.prio_violations += 1  # una copia entró antes que una primaria que esperaba
            job.state = RUNNING
            job.t_start = loop.time()
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            job.work = loop.create_future()
            handle = loop.call_later(job.svc_s, _finish, job.work)
            await job.work
            handle.cancel()
            self.active -= 1
            if job.state == RUNNING:
                job.state = DONE
                job.fut.set_result(not job.will_fail)
            elif not self.cancel_in_service:
                self.wasted_ms += job.svc_ms  # cancelada en ejecución pero terminó: capacidad perdida


def _finish(fut: asyncio.Future) -> None:
    if not fut.done():
        fut.set_result(None)
