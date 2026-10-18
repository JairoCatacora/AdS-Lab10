"""Tabla resumen (markdown), gráficos (PNG), validaciones y conclusión a partir de results.csv.

Uso:  python -m hedging.report results/
"""
from __future__ import annotations

import glob
import math
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

COLORS = {"A": "#2a78d6", "C2": "#6250d6"}
LABELS = {"A": "A baseline", "C2": "C2 propuesta"}
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e1e0d9"


def _style(ax, xlabel, ylabel, title):
    ax.set_xlabel(xlabel, color=MUTED)
    ax.set_ylabel(ylabel, color=MUTED)
    ax.set_title(title, color=INK, loc="left", fontsize=11)
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(colors=MUTED)


def _line(ax, df, v, col, scale=1.0):
    g = df[df.variant == v].groupby("rate")[col]
    med = g.median() * scale
    ax.plot(med.index, med, "-o", lw=2, ms=5, color=COLORS[v], label=LABELS[v])
    ax.fill_between(med.index, g.min() * scale, g.max() * scale, color=COLORS[v], alpha=0.15, lw=0)


def plots(df, out, mid_rate):
    e1 = df[df.experiment == "E1"]
    fig, ax = plt.subplots(figsize=(6.5, 4))
    for v in COLORS:
        _line(ax, e1, v, "p99_incl_timeouts_ms")
    _style(ax, "carga (req/s)", "p99 (ms)", "p99 vs carga (timeouts contados como el deadline)")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "p99_vs_carga.png"), dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.5, 4))
    _line(ax, e1, "C2", "hedge_rate", 100)
    ax.axhline(10, color=INK, lw=1, ls=":")
    ax.text(e1.rate.min(), 10.3, "budget 10 %", color=MUTED, fontsize=8)
    ax.set_ylim(bottom=0)
    _style(ax, "carga (req/s)", "hedge rate (% llamadas extra)", "Costo de la propuesta")
    fig.tight_layout()
    fig.savefig(os.path.join(out, "hedge_rate_vs_carga.png"), dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.5, 4))
    for v in COLORS:
        files = glob.glob(os.path.join(out, "lat", f"E1_{v}_{mid_rate:g}_*.npy"))
        if files:
            x = np.sort(np.concatenate([np.load(f) for f in files]))
            ax.plot(x, np.arange(1, len(x) + 1) / len(x), lw=2, color=COLORS[v], label=LABELS[v])
    ax.set_xscale("log")
    _style(ax, "latencia de la request (ms, log)", "P(latencia ≤ x)", f"CDF a {mid_rate:g} req/s")
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout()
    fig.savefig(os.path.join(out, "cdf_A_vs_C2.png"), dpi=150)
    plt.close(fig)


def _fmt(s, d=0, unit=""):
    """mediana [min–max] entre semillas."""
    med, lo, hi = s.median(), s.min(), s.max()
    txt = f"{med:.{d}f}{unit}"
    return txt if math.isclose(lo, hi, abs_tol=10 ** -(d + 1)) else f"{txt} [{lo:.{d}f}–{hi:.{d}f}]"


def summary_tables(df) -> str:
    e1 = df[df.experiment == "E1"]
    md = ["## Baseline (A) vs Propuesta (C2)", "",
          "Mediana entre semillas y [rango]. Latencias en ms, solo requests completadas.", ""]
    for rate in sorted(e1.rate.unique()):
        a, c = e1[(e1.rate == rate) & (e1.variant == "A")], e1[(e1.rate == rate) & (e1.variant == "C2")]
        md += [f"### Carga {rate:g} req/s", "", "| Métrica | Baseline | Propuesta |", "|---|---|---|"]
        for lab, col in (("p50", "p50_ms"), ("p95", "p95_ms"), ("p99", "p99_ms")):
            md.append(f"| {lab} | {_fmt(a[col])} | {_fmt(c[col])} |")
        md.append(f"| Requests ejecutadas | {_fmt(a.completed)} | {_fmt(c.completed)} |")
        md.append(f"| % requests extra (hedge rate) | {_fmt(100 * a.hedge_rate, 1, ' %')} | {_fmt(100 * c.hedge_rate, 1, ' %')} |")
        md += ["", f"Timeouts: A={_fmt(a.timeouts)}, C2={_fmt(c.timeouts)}. p99 contando timeouts como el deadline: "
               f"A={_fmt(a.p99_incl_timeouts_ms)}, C2={_fmt(c.p99_incl_timeouts_ms)}. "
               f"Hedges bloqueados por budget (C2): {_fmt(c.hedges_blocked)}.", ""]
    e3 = df[df.experiment.isin(["E3"]) | ((df.experiment == "E1") & (df.rate == df[df.experiment == "E3"].rate.max()) & (df.n_writes == 2))]
    if len(e3):
        md += ["## E3: piso del p99 según escrituras por request (carga media)", "",
               "| Escrituras | p99 A | p99 C2 | p95 A | p95 C2 |", "|---|---|---|---|---|"]
        for nw in sorted(e3.n_writes.unique()):
            a, c = e3[(e3.n_writes == nw) & (e3.variant == "A")], e3[(e3.n_writes == nw) & (e3.variant == "C2")]
            md.append(f"| {nw} | {_fmt(a.p99_ms)} | {_fmt(c.p99_ms)} | {_fmt(a.p95_ms)} | {_fmt(c.p95_ms)} |")
    return "\n".join(md)


def checks(df, mid_rate) -> list[tuple[str, bool, str]]:
    res = []
    a = df[(df.experiment == "E1") & (df.variant == "A") & (df.rate <= mid_rate)]
    meas, theo = a.slow_frac.mean(), a.slow_frac_theory.iloc[0]
    tol = 4 * math.sqrt(theo * (1 - theo) / a.completed.mean()) + 0.005
    res.append(("A: % requests lentas (>300 ms) vs teórico", abs(meas - theo) <= tol,
                f"teórico={100 * theo:.2f} %, medido={100 * meas:.2f} %"))
    c2 = df[df.variant == "C2"]
    res.append(("C2: ninguna escritura copiada", bool((c2.copies_writes == 0).all()), f"máx={c2.copies_writes.max():.0f}"))
    over = (c2.copies - c2.copies_on_error) - c2.budget * c2.read_primary_calls - c2.bucket_cap
    res.append(("C2: hedge rate ≤ budget (+ tope del bucket)", bool((over <= 0).all()),
                f"máx copias/lecturas={100 * c2.hedge_rate_reads.max():.2f} % (budget {100 * c2.budget.iloc[0]:.0f} %)"))
    res.append(("C2: prioridad respetada", bool((c2.prio_violations == 0).all()),
                f"violaciones={c2.prio_violations.sum():.0f}"))
    return res


def conclusion(df, mid_rate) -> str:
    e1 = df[df.experiment == "E1"]
    med = lambda v, r, col: e1[(e1.variant == v) & (e1.rate == r)][col].median()  # noqa: E731
    red = lambda r, col: 1 - med("C2", r, col) / med("A", r, col)  # noqa: E731
    red99, red95, cost = red(mid_rate, "p99_ms"), red(mid_rate, "p95_ms"), med("C2", mid_rate, "hedge_rate")
    stops = next((r for r in sorted(e1.rate.unique()) if red(r, "p99_incl_timeouts_ms") < 0.10), None)
    if red99 > 0.5 and cost < 0.10:
        verdict = "GO"
    elif (red95 > 0.5 or red99 > 0.2) and cost < 0.10:
        verdict = "GO WITH CHANGES"
    else:
        verdict = "NO-GO"
    e3 = df[(df.experiment == "E3") & (df.variant == "C2")]
    floor = ""
    if len(e3):
        g = e3.groupby("n_writes").p99_ms.median()
        floor = f" Sin escrituras (E3) el p99 de C2 baja a {g[0]:.0f} ms: las escrituras no copiables son el piso." if 0 in g else ""
    sat = f"desde ~{stops:g} req/s el p99 baja menos de 10 %" if stops else "en las cargas probadas el p99 siempre baja ≥ 10 %"
    return (f"## Conclusión\n\nA {mid_rate:g} req/s la propuesta reduce el p95 un {100 * red95:.0f} % y el p99 un "
            f"{100 * red99:.0f} % (A={med('A', mid_rate, 'p99_ms'):.0f} ms, C2={med('C2', mid_rate, 'p99_ms'):.0f} ms) con "
            f"{100 * cost:.1f} % de llamadas extra. Deja de ayudar: {sat}.{floor} "
            f"**Decisión: {verdict}.** Criterio: GO si el p99 baja > 50 % con < 10 % de extra; GO WITH CHANGES si el p99 "
            f"baja > 20 % o el p95 > 50 % con < 10 % de extra; NO-GO en otro caso.\n")


def make_all(out, mid_rate=100.0):
    df = pd.read_csv(os.path.join(out, "results.csv"))
    plots(df, out, mid_rate)
    ck = checks(df, mid_rate)
    md = ["# Resultados del PoC", "",
          f"time_scale={df.time_scale.iloc[0]:g}, requests/escenario={df.requests.iloc[0]}, semillas={df.seed.nunique()}"
          + (" — **indicativo (time-scale > 1)**" if df.time_scale.iloc[0] > 1 else ""), "",
          conclusion(df, mid_rate), summary_tables(df), "", "## Validaciones", "", "| Validación | Resultado | Detalle |", "|---|---|---|"]
    md += [f"| {n} | {'OK' if ok else 'FALLA'} | {d} |" for n, ok, d in ck]
    with open(os.path.join(out, "summary.md"), "w") as f:
        f.write("\n".join(md) + "\n")
    print("\n".join(f"[{'OK' if ok else 'FALLA'}] {n}: {d}" for n, ok, d in ck))
    print(conclusion(df, mid_rate))
    print(f"-> {out}/summary.md y PNGs")


if __name__ == "__main__":
    make_all(sys.argv[1] if len(sys.argv) > 1 else "results")
