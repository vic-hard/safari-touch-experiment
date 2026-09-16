#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A2, A3 — часы устройства.

A2: фактическая гранулярность performance.now().
A3: несёт ли event.timeStamp собственную информацию или совпадает с моментом
    входа в обработчик.

Пока неизвестна гранулярность часов, вывод о кадровом квантовании
несостоятелен (PLAN §7.1): 1 мс и 16.7 мс расходятся в шестнадцать раз, но
только если первое измерено, а не предположено.

    python analysis/analyze_clock.py data/<session>.json [ещё сессии...]
    python analysis/analyze_clock.py data/*.json --csv out/clock.csv

Только стандартная библиотека.
"""

import argparse
import csv
import json
import math
import os
import statistics
import sys
from collections import defaultdict


# --- общие мелочи (в каждом скрипте свои: общего модуля в структуре нет) ----

def load(path):
    with open(path, "r", encoding="utf-8") as fh:
        session = json.load(fh)
    fmt = session.get("format")
    if fmt != "web-probe-0.1.0":
        print("  [!] формат %r, ожидался web-probe-0.1.0 — читаю как есть" % fmt)
    return session


def describe(values):
    """n / среднее / СКО / min / медиана / max — None-безопасно."""
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return None
    return {
        "n": len(vals),
        "mean": statistics.fmean(vals),
        "sd": statistics.stdev(vals) if len(vals) > 1 else 0.0,
        "min": min(vals),
        "p50": statistics.median(vals),
        "max": max(vals),
    }


def fmt_desc(d):
    if not d:
        return "нет данных"
    return ("n=%d  среднее %.3f  СКО %.3f  [%.3f … %.3f]  медиана %.3f"
            % (d["n"], d["mean"], d["sd"], d["min"], d["max"], d["p50"]))


def hist(values, lo, hi, bins, width=46, label=""):
    """Текстовая гистограмма: единственный вид графика в проекте."""
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return "  (пусто)"
    step = (hi - lo) / bins if hi > lo else 1.0
    counts = [0] * bins
    below = above = 0
    for v in vals:
        if v < lo:
            below += 1
            continue
        idx = int((v - lo) / step)
        if idx >= bins:
            above += 1
            continue
        counts[idx] += 1
    top = max(counts) or 1
    lines = []
    if label:
        lines.append("  " + label)
    for i, c in enumerate(counts):
        left = lo + i * step
        bar = "#" * int(round(c / top * width))
        lines.append("  %8.3f | %-*s %d" % (left, width, bar, c))
    if below or above:
        lines.append("  вне диапазона: ниже %d, выше %d" % (below, above))
    return "\n".join(lines)


def head(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# --- A2 --------------------------------------------------------------------

def a2_granularity(session):
    head("A2 — гранулярность performance.now()")
    clock = session.get("clock") or {}
    calib = clock.get("calibration") or {}
    gran = calib.get("granularityMs", clock.get("granularityMs"))
    print("  измерено на устройстве: %s мс" % gran)
    print("  чтений за %s мс: %s, различных шагов: %s"
          % (calib.get("durationMs"), calib.get("reads"), calib.get("distinctSteps")))
    print("  timeOrigin: %s" % calib.get("timeOrigin"))
    top = calib.get("topDeltas") or []
    if top:
        print("  самые частые приращения часов (мс -> сколько раз):")
        for row in top:
            print("    %-12s %d" % (row.get("delta"), row.get("n")))

    # Контроль по самим данным: минимальная положительная разность между
    # метками, записанными в журнале. Должна согласоваться с калибровкой.
    stamps = sorted({e.get("nowMs") for e in session.get("events", [])
                     if isinstance(e.get("nowMs"), (int, float))})
    diffs = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
    if diffs:
        print("  контроль по журналу: минимальная разность меток событий %.4f мс"
              % min(diffs))
    frames = [t for t in session.get("frames", []) if isinstance(t, (int, float))]
    fdiffs = [b - a for a, b in zip(frames, frames[1:])]
    if fdiffs:
        print("  период кадра по медиане разностей rAF: %.3f мс (n=%d)"
              % (statistics.median(fdiffs), len(fdiffs)))
    if gran:
        print("  вывод: метка события не может быть точнее %s мс; кадровое"
              " квантование (~16.7 мс) отличается от этого в %.0f раз"
              % (gran, 16.7 / gran if gran else float("nan")))
    return gran


# --- A3 --------------------------------------------------------------------

def a3_timestamp(session, gran):
    head("A3 — timeStamp против performance.now() в обработчике")
    events = session.get("events", [])
    by_type = defaultdict(list)
    for e in events:
        lag = e.get("handlerLagMs")
        if isinstance(lag, (int, float)):
            by_type[(e.get("source"), e.get("type"))].append(lag)

    if not by_type:
        print("  handlerLagMs нет ни у одного события — timeStamp не записан")
        return

    print("  задержка входа в обработчик = nowMs - timeStamp, мс:")
    for key in sorted(by_type):
        print("    %-10s %-14s %s" % (key[0], key[1], fmt_desc(describe(by_type[key]))))

    all_lags = [v for vals in by_type.values() for v in vals]
    d = describe(all_lags)
    zero = sum(1 for v in all_lags if abs(v) < 1e-9)
    print()
    print("  всего: %s" % fmt_desc(d))
    print("  строго нулевых задержек: %d из %d" % (zero, len(all_lags)))
    print(hist(all_lags, 0.0, max(4.0, d["p50"] * 3 if d else 4.0), 20,
               label="распределение задержки, мс"))

    # Квантование самого timeStamp: если метка кратна 1 мс, дробных частей
    # почти не будет; собственную информацию такая метка несёт слабо.
    fracs = []
    for e in events:
        ts = e.get("timeStamp")
        if isinstance(ts, (int, float)):
            fracs.append(ts - math.floor(ts))
    if fracs:
        distinct = len(set(round(f, 6) for f in fracs))
        print()
        print("  дробная часть timeStamp: различных значений %d из %d"
              % (distinct, len(fracs)))
        print(hist(fracs, 0.0, 1.0, 20, label="дробная часть timeStamp"))

    print()
    if d and gran and d["sd"] <= (gran or 0) / 2 and abs(d["mean"]) < (gran or 1):
        print("  вывод: timeStamp практически совпадает с моментом входа в"
              " обработчик — собственной информации метка не несёт")
    else:
        print("  вывод: timeStamp отличается от nowMs на величину, превышающую"
              " гранулярность часов — метка снята раньше входа в обработчик"
              " и несёт собственную информацию")


def write_csv(path, session, name):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["session", "seq", "source", "type", "timeStamp", "nowMs",
                        "handlerLagMs", "frameIndex", "offsetFromFrameMs"])
        for e in session.get("events", []):
            w.writerow([name, e.get("seq"), e.get("source"), e.get("type"),
                        e.get("timeStamp"), e.get("nowMs"), e.get("handlerLagMs"),
                        e.get("frameIndex"), e.get("offsetFromFrameMs")])


def main(argv=None):
    ap = argparse.ArgumentParser(description="A2, A3 — часы устройства")
    ap.add_argument("sessions", nargs="+", help="JSON сессии из data/")
    ap.add_argument("--csv", help="дописать подробности в CSV")
    args = ap.parse_args(argv)

    for path in args.sessions:
        name = os.path.basename(path)
        print()
        print("#" * 72)
        print("# %s" % name)
        session = load(path)
        meta = session.get("meta") or {}
        print("#   участник %s, серия %s, страница %s, режим %s"
              % (meta.get("participant"), meta.get("series"), meta.get("page"),
                 meta.get("mode")))
        env = meta.get("env") or {}
        print("#   %s" % (env.get("userAgent") or "userAgent не записан"))
        kind = ((meta.get("env") or {}).get("browserKind"))
        if kind and kind != "safari":
            print("#   [!] СРЕДА НЕ SAFARI (%s): встроенный WebView или сторонний" % kind)
            print("#       браузер — другой процесс и другие ограничения. Серия не")
            print("#       отвечает на вопрос эксперимента, числа в findings не идут.")
        gran = a2_granularity(session)
        a3_timestamp(session, gran)
        if args.csv:
            write_csv(args.csv, session, name)
            print("\n  CSV: %s" % args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
