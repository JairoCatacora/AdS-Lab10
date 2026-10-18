# PoC: Hedged requests + hedge budget (The Tail at Scale)

Simulación in-process (Python/asyncio, sin HTTP) de una API que hace fan-out a 10 servicios con 2 réplicas cada uno.
Compara **A (baseline)** contra **C2 (propuesta)**: hedged requests solo para lecturas, con hedge budget (token bucket 10 %)
y copias con prioridad baja. Dependencias: numpy, pandas, matplotlib (pytest para los tests).

## Cómo correr

```bash
python -m venv .venv && .venv/bin/pip install numpy pandas matplotlib pytest
export PYTHONPATH=.
.venv/bin/python -m hedging.experiments --profile smoke --out results_smoke
.venv/bin/python -m hedging.experiments --profile full  --out results        # 5.000 req x escenario, 3 semillas, ~2 min
.venv/bin/python -m pytest -q hedging/tests.py                               # validaciones
```

Genera `results/results.csv` (una fila por experimento, variante, carga, semilla), `results/summary.md` (tabla Baseline vs
Propuesta por carga, E3, validaciones, conclusión) y 3 PNG: `p99_vs_carga`, `hedge_rate_vs_carga`, `cdf_A_vs_C2`.
Otros parámetros: `--requests --seeds --seed-base --time-scale --rates --workers --budget --deadline-ms
--cancel-in-service/--no-cancel-in-service --hedge-delay-mode {fast_p95,fixed_ms}`.

## Archivos

`hedging/sim.py` réplicas (N workers, PriorityQueue, cancelación) y latencias · `api.py` catálogo, budget, hedge controller ·
`load.py` generador open-loop, calibración, métricas · `experiments.py` E1/E3 en paralelo · `report.py` tablas/gráficos · `tests.py`.

## Modelo

* Réplica: 10 workers + `asyncio.PriorityQueue` (0 = primaria, 1 = copia). No es un `sleep` suelto: hay cola y saturación.
* Servicio: rápido lognormal (mediana 12 ms); lento con p_slow (svc_2, 5, 8 = 10 %; resto 1 %) uniforme 300–1500 ms; 0,1 % de errores.
  Lentitud independiente por réplica (`correlation` existe como parámetro, no se barre en este PoC).
* Common random numbers: mismos sorteos y llegadas para A y C2 con la misma semilla.
* Catálogo servicio → {op, hedgeable}, consultado en cada llamada: svc_1–8 lecturas hedgeables, svc_9–10 escrituras (nunca se copian).
  La request solo puede hacer opt-out; nunca activar hedging.
* Flujo por llamada: primaria (prio 0) → si no responde en el hedge delay y hay ficha, copia a la otra réplica (prio 1) →
  gana la primera respuesta válida y se cancela la otra. Si la primaria falla, la copia sale de inmediato.
* Open-loop (llegadas Poisson), latencia medida desde la llegada PREVISTA. Deadline global 3 s (timeouts se reportan aparte).

**Calibración de `workers`:** servicio medio del propenso ≈ 101 ms ⇒ ρ = λ·0,101/(2·workers). Con 10 workers: ρ = 0,51 a 100 req/s,
1,01 a 200 req/s y 1,27 a 250 req/s (saturación en el rango 150–250 pedido). Con 4 workers saturaría a ~80 req/s.

**Hedge delay = p95 de la distribución RÁPIDA** (≈22 ms), medido en una calibración sin lentitud y luego fijo. Con p_slow ≥ 5 %
el p95 de la distribución completa cae dentro de la cola lenta (300–1500 ms) y la copia saldría tarde.

## Supuestos

1. workers = 10 por réplica (no 4) para que la saturación caiga en 150–250 req/s.
2. `cancel_in_service=True` por defecto (cancelar libera el worker). Es optimista; ver sensibilidad abajo.
3. Un error responde rápido; sin reintentos salvo la copia por falla (no consume ficha).
4. Bucket global, vacío al inicio, tope 20 fichas (con tope 5 las ráfagas de hedges de una misma request lo vaciaban).
   Hedge rate = copias / todas las llamadas primarias.
5. Percentiles solo sobre requests completadas; `p99_incl_timeouts` cuenta timeouts como el deadline.
6. time-scale > 1 divide todas las duraciones; los resultados son indicativos (el perfil `full` usa 1).
7. Descartado a propósito: circuit breaker, caché, tied requests, micro-particiones.

## Resultados (perfil full, 5.000 req × 3 semillas, mediana [rango])

| Métrica (100 req/s) | Baseline | Propuesta |
|---|---|---|
| p50 | 26 | 25 |
| p95 | 1350 | 57 |
| p99 | 1470 | 1102 |
| Requests ejecutadas | 4949 | 4989 |
| % requests extra (hedge rate) | 0 % | 6,8 % |

Todas las cargas y el detalle están en `results/summary.md`. Validaciones (todas OK): requests lentas en A 32,6 % medido vs
32,05 % teórico; ninguna escritura copiada; hedge rate ≤ budget; prioridad respetada (0 violaciones); misma semilla ⇒ mismos sorteos.

* **p95 baja 96 %, p99 solo 25 %.** El p99 queda en ~1100 ms por las llamadas de escritura (1 % lentas, 2 por request ⇒ ~2 % de
  requests, > 1 %), que no se copian. E3: con 0/2/4 escrituras el p99 de C2 es 851/1102/1412 ms (A: 1470).
* **Carga:** con cancelación en ejecución la propuesta sigue ayudando hasta 250 req/s (p99 de A ≈ 2950 ms con timeouts, C2 ≈ 1100 ms),
  porque cancelar las primarias lentas libera capacidad. Sin esa cancelación (corrida aparte con `--no-cancel-in-service --out <carpeta>`) la ventaja
  se reduce a 150 req/s (p99 1621 → 1474 ms), es marginal a 200 (2990 → 2483) y desaparece a 250 (ambos saturan, p99 = deadline).
  El hedging solo sirve si cancelar libera capacidad y hay holgura de utilización (ρ < ~0,8).
* **Límite:** si las dos réplicas son lentas a la vez, la copia no ayuda (p² de lentitud conjunta); E2 de correlación no se corrió en esta versión reducida.

## Decisión: GO WITH CHANGES

Criterio: GO si el p99 baja > 50 % con < 10 % de llamadas extra. Aquí el p99 baja 25 % (p95 96 %) con 6,8 % extra, así que no llega a GO.
Cambios requeridos: (1) tratar las escrituras lentas (hacerlas idempotentes para poder copiarlas, o acelerarlas en su origen);
(2) exigir cancelación efectiva en las réplicas; (3) operar con ρ < ~0,8 o autoescalar antes de saturar.
