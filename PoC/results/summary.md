# Resultados del PoC

time_scale=1, requests/escenario=5000, semillas=3

## Conclusión

A 100 req/s la propuesta reduce el p95 un 96 % y el p99 un 25 % (A=1470 ms, C2=1102 ms) con 6.8 % de llamadas extra. Deja de ayudar: en las cargas probadas el p99 siempre baja ≥ 10 %. Sin escrituras (E3) el p99 de C2 baja a 851 ms: las escrituras no copiables son el piso. **Decisión: GO WITH CHANGES.** Criterio: GO si el p99 baja > 50 % con < 10 % de extra; GO WITH CHANGES si el p99 baja > 20 % o el p95 > 50 % con < 10 % de extra; NO-GO en otro caso.

## Baseline (A) vs Propuesta (C2)

Mediana entre semillas y [rango]. Latencias en ms, solo requests completadas.

### Carga 50 req/s

| Métrica | Baseline | Propuesta |
|---|---|---|
| p50 | 25 [25–25] | 25 [25–25] |
| p95 | 1337 [1327–1347] | 58 [52–324] |
| p99 | 1471 [1466–1471] | 1103 [1068–1172] |
| Requests ejecutadas | 4949 [4947–4955] | 4989 [4987–4995] |
| % requests extra (hedge rate) | 0.0 % | 6.8 % [6.6–6.8] |

Timeouts: A=0, C2=0. p99 contando timeouts como el deadline: A=1471 [1466–1471], C2=1103 [1068–1172]. Hedges bloqueados por budget (C2): 0 [0–1].

### Carga 100 req/s

| Métrica | Baseline | Propuesta |
|---|---|---|
| p50 | 26 [25–27] | 25 [25–25] |
| p95 | 1350 [1327–1350] | 57 [52–314] |
| p99 | 1470 [1468–1477] | 1102 [1068–1161] |
| Requests ejecutadas | 4949 [4947–4955] | 4989 [4987–4995] |
| % requests extra (hedge rate) | 0.0 % | 6.8 % [6.6–6.8] |

Timeouts: A=0, C2=0. p99 contando timeouts como el deadline: A=1470 [1468–1477], C2=1102 [1068–1161]. Hedges bloqueados por budget (C2): 0.

### Carga 150 req/s

| Métrica | Baseline | Propuesta |
|---|---|---|
| p50 | 235 [152–472] | 25 [25–25] |
| p95 | 1389 [1362–1552] | 59 [52–312] |
| p99 | 1624 [1493–1966] | 1101 [1069–1161] |
| Requests ejecutadas | 4948 [4947–4955] | 4989 [4987–4995] |
| % requests extra (hedge rate) | 0.0 % | 6.8 % [6.6–6.9] |

Timeouts: A=0 [0–1], C2=0. p99 contando timeouts como el deadline: A=1624 [1493–1973], C2=1101 [1069–1161]. Hedges bloqueados por budget (C2): 0.

### Carga 200 req/s

| Métrica | Baseline | Propuesta |
|---|---|---|
| p50 | 1412 [1054–1816] | 25 [25–25] |
| p95 | 2368 [2118–2542] | 58 [58–313] |
| p99 | 2810 [2663–2896] | 1102 [1069–1162] |
| Requests ejecutadas | 4875 [4795–4923] | 4989 [4987–4995] |
| % requests extra (hedge rate) | 0.0 % | 6.9 % [6.7–6.9] |

Timeouts: A=83 [24–155], C2=0. p99 contando timeouts como el deadline: A=3000 [2780–3000], C2=1102 [1069–1162]. Hedges bloqueados por budget (C2): 0 [0–3].

### Carga 250 req/s

| Métrica | Baseline | Propuesta |
|---|---|---|
| p50 | 2268 [2199–2387] | 25 [25–25] |
| p95 | 2715 [2680–2728] | 62 [59–328] |
| p99 | 2948 [2917–2953] | 1102 [1068–1185] |
| Requests ejecutadas | 4488 [4305–4587] | 4989 [4987–4995] |
| % requests extra (hedge rate) | 0.0 % | 6.9 % [6.7–6.9] |

Timeouts: A=466 [373–647], C2=0. p99 contando timeouts como el deadline: A=3000, C2=1102 [1068–1185]. Hedges bloqueados por budget (C2): 0 [0–16].

## E3: piso del p99 según escrituras por request (carga media)

| Escrituras | p99 A | p99 C2 | p95 A | p95 C2 |
|---|---|---|---|---|
| 0 | 1470 [1468–1477] | 851 [828–864] | 1349 [1327–1350] | 45 [45–46] |
| 2 | 1470 [1468–1477] | 1102 [1068–1161] | 1350 [1327–1350] | 57 [52–314] |
| 4 | 1470 [1467–1477] | 1412 [1408–1426] | 1349 [1327–1350] | 1082 [1023–1097] |

## Validaciones

| Validación | Resultado | Detalle |
|---|---|---|
| A: % requests lentas (>300 ms) vs teórico | OK | teórico=32.05 %, medido=32.60 % |
| C2: ninguna escritura copiada | OK | máx=0 |
| C2: hedge rate ≤ budget (+ tope del bucket) | OK | máx copias/lecturas=8.62 % (budget 10 %) |
| C2: prioridad respetada | OK | violaciones=0 |
