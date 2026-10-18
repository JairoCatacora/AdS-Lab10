"""Etapas 2-4: la API con fan-out de 10 llamadas, catálogo, hedge controller y variantes."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .sim import SERVICES, Draws, Replica, SimConfig


# ---------------------------------------------------------------- variantes
@dataclass(frozen=True)
class Variant:
    code: str
    name: str
    hedge: str        # "none" | "reads" (solo lecturas hedgeables del catálogo) | "all" (incluye escrituras)
    budget: bool      # limitar copias con token bucket
    low_prio: bool    # copias con prioridad baja en la cola de la réplica


VARIANTS = {
    "A": Variant("A", "Baseline", "none", False, False),
    "B": Variant("B", "Hedging solo lecturas", "reads", False, False),
    "C1": Variant("C1", "+ budget", "reads", True, False),
    "C2": Variant("C2", "Propuesta (+ budget + prioridad)", "reads", True, True),
    "R": Variant("R", "Referencia NO segura (hedge de todo)", "all", False, False),
}


# ---------------------------------------------------------------- catálogo
def build_catalog(n_writes: int = 2) -> dict[str, dict]:
    """servicio -> {op, hedgeable}. Los últimos `n_writes` servicios son escrituras (no hedgeables)."""
    catalog = {}
    for i, svc in enumerate(SERVICES):
        is_write = i >= len(SERVICES) - n_writes
        catalog[svc] = {"op": "write" if is_write else "read", "hedgeable": not is_write}
    return catalog


# ---------------------------------------------------------------- hedge budget
class Bucket:
    """Token bucket: cada llamada primaria (lectura) agrega `frac` fichas; cada copia consume 1."""

    def __init__(self, frac: float, cap: float):
        self.frac, self.cap, self.tokens = frac, cap, 0.0

    def add(self) -> None:
        self.tokens = min(self.cap, self.tokens + self.frac)

    def take(self) -> bool:
        if self.tokens >= 1.0 - 1e-9:
            self.tokens -= 1.0
            return True
        return False


# ---------------------------------------------------------------- API
class Api:
    def __init__(self, variant: Variant, cfg: SimConfig, draws: Draws, scale: float, n_writes: int,
                 hedge_delay_ms: dict[str, float] | None, budget: float, bucket_cap: float,
                 deadline_ms: float):
        self.v, self.draws, self.scale = variant, draws, scale
        self.catalog = build_catalog(n_writes)
        self.hedge_delay_ms = hedge_delay_ms or {}
        self.bucket = Bucket(budget, bucket_cap) if variant.budget else None
        self.deadline_s = deadline_ms / 1000.0 / scale
        self.replicas = {s: [Replica(f"{s}-{r}", cfg, scale, check_priority=variant.low_prio)
                             for r in "AB"] for s in SERVICES}
        # contadores
        self.primary_calls = self.read_primary = 0
        self.copies = self.copies_writes = self.copies_on_error = self.blocked = 0
        self.read_lat: list[float] = []
        self.write_lat: list[float] = []
        self.svc_lat: dict[str, list[float]] = {s: [] for s in SERVICES}

    def all_replicas(self):
        return [r for pair in self.replicas.values() for r in pair]

    def start(self) -> None:
        for r in self.all_replicas():
            r.start()

    async def stop(self) -> None:
        await asyncio.gather(*(r.stop() for r in self.all_replicas()))

    # ---- decisión de hedging: la toma SIEMPRE la API con el catálogo, nunca la request
    def _can_hedge(self, svc: str, opt_out: bool) -> bool:
        if opt_out or self.v.hedge == "none":
            return False
        if self.v.hedge == "all":
            return True
        entry = self.catalog[svc]  # se consulta en cada una de las llamadas
        return entry["op"] == "read" and entry["hedgeable"]

    # ---- una llamada a un servicio (hedge controller). Devuelve True si hubo respuesta válida
    async def call(self, rid: int, si: int, opt_out: bool = False) -> bool:
        svc, d = SERVICES[si], self.draws
        prim = int(d.primary[rid, si])
        reps = self.replicas[svc]
        is_read = self.catalog[svc]["op"] == "read"
        self.primary_calls += 1
        self.read_primary += is_read

        jobs = [(reps[prim], reps[prim].submit(d.service_ms[rid, si, prim], bool(d.fail[rid, si, prim]), 0))]
        try:
            if not self._can_hedge(svc, opt_out):
                return await jobs[0][1].fut  # sin copia: ejecución normal (errores no se reintentan)

            if self.bucket is not None and is_read:
                self.bucket.add()
            other = 1 - prim
            copy_sent = False
            pending = {jobs[0][1].fut}
            timeout = self.hedge_delay_ms[svc] / 1000.0 / self.scale

            def send_copy(on_error: bool) -> None:
                nonlocal copy_sent
                copy_sent = True
                self.copies += 1
                self.copies_on_error += on_error
                self.copies_writes += not is_read
                rep = reps[other]
                prio = 1 if self.v.low_prio else 0
                job = rep.submit(d.service_ms[rid, si, other], bool(d.fail[rid, si, other]), prio)
                jobs.append((rep, job))
                pending.add(job.fut)

            while pending:
                done, pending = await asyncio.wait(pending, timeout=timeout,
                                                   return_when=asyncio.FIRST_COMPLETED)
                if not done:  # venció el hedge delay
                    timeout = None
                    if not copy_sent:
                        if self.bucket is None or self.bucket.take():
                            send_copy(False)
                        else:
                            self.blocked += 1  # hedge bloqueado por budget; se sigue esperando a la primaria
                    continue
                if any(f.result() for f in done):
                    return True  # la primera respuesta VÁLIDA gana
                if not copy_sent:  # la primaria falló: copia inmediata (no consume ficha)
                    send_copy(True)
                    timeout = None
            return False
        finally:
            for rep, job in jobs:  # cancela a la perdedora (no hace nada si ya terminó)
                rep.cancel(job)

    async def _timed_call(self, rid: int, si: int, opt_out: bool) -> bool:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        ok = await self.call(rid, si, opt_out)
        if ok:
            lat = (loop.time() - t0) * 1000.0 * self.scale
            svc = SERVICES[si]
            (self.read_lat if self.catalog[svc]["op"] == "read" else self.write_lat).append(lat)
            self.svc_lat[svc].append(lat)
        return ok

    # ---- una request completa: fan-out a las 10 llamadas con deadline global
    async def handle(self, rid: int, intended: float, opt_out: bool = False) -> tuple[str, float]:
        """Devuelve (estado, latencia_ms_modelo) medida desde el instante PREVISTO de llegada."""
        loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(self._timed_call(rid, si, opt_out)) for si in range(len(SERVICES))]
        remaining = self.deadline_s - (loop.time() - intended)
        _, pending = await asyncio.wait(tasks, timeout=max(remaining, 0.0))
        if pending:
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            status = "timeout"
        else:
            status = "ok" if all(t.result() for t in tasks) else "error"
        return status, (loop.time() - intended) * 1000.0 * self.scale
