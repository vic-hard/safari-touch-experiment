#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Признаки резкости тапа помимо площади: микросдвиг и комбинации.

Площадь пятна (SF §5, `analyze_area.py`) на естественном тапе одной частью
пальца даёт мало: 56% отсева на iPhone 15 и 0% на iPhone 16 Pro Max. Здесь
проверяются остальные признаки, которые в записи уже есть, и их объединения.

Признаки считаются по контакту одного потока:

    area        максимум площади пятна за контакт (тот же, что в analyze_area)
    shift       максимальный сдвиг точки контакта от места касания, px
    shiftNN     то же, но видны только отсчёты первых NN мс после DOWN
    areaNN      максимум площади за первые NN мс после DOWN

Окно нужно из-за задержки: полный сдвиг известен только на отпускании, а
ритм-игре ответ нужен ближе к касанию, поэтому вариант с окном — не украшение,
а единственный, который можно вживить в игру.

Правило порога — то же, что у площади (SF §2, §7.5): порог по каждому признаку
равен максимуму этого признака по мягким тапам обучающих серий, контакт
считается мягким, только если он ниже порога по всем признакам набора. Обучение
на всех сериях, кроме отложенной; отложенная перебирается по очереди, потому что
на трёх сериях один расклад ещё ничего не значит.

    python analysis/analyze_features.py data/<три серии одного протокола>.json
    python analysis/analyze_features.py data/*.json --source touch --window 50

ВАЖНО про честность числа: скрипт перебирает наборы признаков и печатает их
рядом. Отложенная серия защищает от подгонки порога, но не от выбора самого
набора — выбор делается по этим же данным. Число становится настоящим только
на серии, снятой после того, как набор зафиксирован; зафиксированный набор и
условия подтверждения — `docs/protocol.md`, раздел «Подтверждающая серия».

Только стандартная библиотека.
"""

import argparse
import bisect
import math
import os
import statistics
import sys

import analyze_area as area_mod

# Наборы признаков, которые имеет смысл сравнивать между собой. Порядок — от
# одиночных к объединениям, чтобы в выводе было видно, что даёт объединение.
FEATURE_SETS = [
    ("площадь (признак SF §5)", ["area"]),
    ("микросдвиг за контакт", ["shift"]),
    ("площадь ИЛИ микросдвиг", ["area", "shift"]),
    ("микросдвиг в окне", ["shiftW"]),
    ("площадь ИЛИ микросдвиг в окне", ["area", "shiftW"]),
    ("площадь в окне ИЛИ микросдвиг в окне", ["areaW", "shiftW"]),
]


def head(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def shift_of(contact, window_ms=None):
    """Максимальный сдвиг от точки касания, px; window_ms ограничивает видимое."""
    events = contact["events"]
    x0, y0 = events[0].get("clientX"), events[0].get("clientY")
    t0 = events[0].get("timeStamp")
    if not all(isinstance(v, (int, float)) for v in (x0, y0)):
        return None
    best = 0.0
    for e in events[1:]:
        x, y, t = e.get("clientX"), e.get("clientY"), e.get("timeStamp")
        if not all(isinstance(v, (int, float)) for v in (x, y)):
            continue
        if window_ms is not None:
            if not isinstance(t, (int, float)) or not isinstance(t0, (int, float)):
                continue
            if t - t0 > window_ms:
                continue
        best = max(best, math.hypot(x - x0, y - y0))
    return best


def area_of(contact, source, window_ms=None):
    """Максимум площади за контакт; window_ms ограничивает видимое."""
    events = contact["events"]
    t0 = events[0].get("timeStamp")
    vals = []
    for e in events:
        if window_ms is not None:
            t = e.get("timeStamp")
            if not isinstance(t, (int, float)) or not isinstance(t0, (int, float)):
                continue
            if t - t0 > window_ms:
                continue
        a = area_mod.AREA_FN[source](e)
        if isinstance(a, (int, float)):
            vals.append(a)
    return max(vals) if vals else None


def beat_grid(session):
    """Полная сетка долей метронома из сессии, мс по шкале performance.now()."""
    metro = session.get("metronome") or {}
    times = [b.get("perfTimeMs") for b in (metro.get("beats") or [])]
    return sorted(t for t in times if isinstance(t, (int, float)))


def beat_deviation(ts, grid):
    """Отклонение тапа от ближайшей доли, мс; минус — раньше щелчка.

    Поле beatDeviationMs из записи для этого не годится: страница считает его в
    момент тапа, а планировщик метронома ставит щелчки лишь на 300 мс вперёд,
    поэтому тап, сделанный раньше этого горизонта, привязывается к предыдущей
    доле и даёт отклонение почти в целую долю. По полной сетке из сессии такой
    ошибки нет — так же считается A8 в analyze_timing.
    """
    if not grid or not isinstance(ts, (int, float)):
        return None
    i = bisect.bisect_left(grid, ts)
    best = None
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(grid):
            d = ts - grid[j]
            if best is None or abs(d) < abs(best):
                best = d
    return best


def contact_rows(session, source, window_ms):
    grid = beat_grid(session)
    rows = []
    for c in area_mod.contacts(session, source):
        label = c.get("label")
        if label not in ("soft", "sharp"):
            continue
        down = c["events"][0]
        rows.append({
            "label": label,
            "tapIndex": c.get("tapIndex"),
            "beatDeviationMs": beat_deviation(down.get("timeStamp"), grid),
            "area": area_mod.feature(c, source),
            "areaW": area_of(c, source, window_ms),
            "shift": shift_of(c),
            "shiftW": shift_of(c, window_ms),
        })
    return rows


def quantile(values, q):
    """Квантиль по возрастанию, линейная интерполяция; q=1.0 — максимум."""
    vals = sorted(values)
    if not vals:
        return None
    if q >= 1.0:
        return vals[-1]
    pos = (len(vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def thresholds(train, keys, soft_quantile=1.0):
    """Порог по каждому признаку — квантиль по мягким обучающим.

    По умолчанию квантиль единичный, то есть максимум: так задано в SF §7.5 и
    так фильтр пропускает все обучающие мягкие тапы. У этого правила есть цена,
    которая на квантованном канале становится решающей: один случайный мягкий
    тап, попавший ступенью выше, поднимает порог над всем диапазоном, и признак
    перестаёт отсеивать что-либо вообще (на сериях под метроном ровно это и
    произошло с площадью). Квантиль меньше единицы отдаёт такие выбросы в
    обмен на работающий порог — но это уже другое правило, и проверять его надо
    на серии, снятой после того, как оно зафиксировано.
    """
    out = {}
    for k in keys:
        vals = [r[k] for r in train if r["label"] == "soft" and r[k] is not None]
        out[k] = quantile(vals, soft_quantile) if vals else None
    return out


def passes(row, thr):
    """Мягким считается контакт, который ниже порога по всем признакам набора."""
    for k, limit in thr.items():
        if limit is None:
            continue
        v = row[k]
        if v is None or v > limit:
            return False
    return True


def evaluate(train, test, keys, soft_quantile=1.0):
    thr = thresholds(train, keys, soft_quantile)
    soft = [r for r in test if r["label"] == "soft"]
    sharp = [r for r in test if r["label"] == "sharp"]
    return {
        "thr": thr,
        "soft_passed": sum(1 for r in soft if passes(r, thr)),
        "soft_total": len(soft),
        "sharp_rejected": sum(1 for r in sharp if not passes(r, thr)),
        "sharp_total": len(sharp),
    }


def parse_thresholds(text, window_ms):
    """«area50=2336.369,shift50=0» → {'areaW': 2336.369, 'shiftW': 0.0}.

    Имена те же, что в docs/protocol.md: area, shift — по всему контакту,
    areaNN, shiftNN — по окну NN мс. Окно в имени обязано совпасть с --window,
    иначе правило считалось бы не то, которое зафиксировано.
    """
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        try:
            value = float(value)
        except ValueError:
            raise SystemExit("порог %r не число" % part)
        if name in ("area", "shift"):
            out[name] = value
            continue
        for base in ("area", "shift"):
            if name.startswith(base) and name[len(base):].isdigit():
                if int(name[len(base):]) != window_ms:
                    raise SystemExit(
                        "порог %s не сходится с окном --window %d: правило зафиксировано"
                        " на другом окне" % (name, window_ms))
                out[base + "W"] = value
                break
        else:
            raise SystemExit("непонятное имя порога %r (ожидалось area, shift,"
                             " area<мс>, shift<мс>)" % name)
    if not out:
        raise SystemExit("--thresholds пуст")
    return out


def warn_near_miss(rows_by_name, thr):
    """Порог, лежащий чуть ниже реального значения признака, — частая ошибка.

    Значения канала квантованы, и порог, выписанный с округлением вниз (5254.945
    вместо 5254.945081), отсекает целую ступень: тапы, сидящие ровно на ней,
    становятся «резкими». Разница в тысячных, а ответ меняется на десятки тапов.
    """
    rows = [r for rs in rows_by_name.values() for r in rs]
    for key, limit in sorted(thr.items()):
        if limit is None:
            continue
        near = [r[key] for r in rows
                if isinstance(r.get(key), (int, float)) and 0 < r[key] - limit <= 0.01]
        if near:
            print("  [!] порог %s = %.6f лежит на %.6f ниже значения %.6f, которое"
                  % (key, limit, min(near) - limit, min(near)))
            print("      встречается в данных %d раз. Похоже на потерю точности при"
                  % len(near))
            print("      выписывании порога: проверь, не округлён ли он вниз.")


def frozen_check(rows_by_name, thr, window_ms):
    """Правило с заранее зафиксированными порогами: обучения нет вовсе."""
    head("проверка зафиксированным правилом (обучение не проводится)")
    print("  пороги: %s" % ", ".join(
        "%s ≤ %.6f" % (k.replace("W", str(window_ms)), v) for k, v in sorted(thr.items())))
    warn_near_miss(rows_by_name, thr)
    print()
    print("  %-46s %-16s %s" % ("серия", "мягких прошло", "резких отсеяно"))
    soft_ok = soft_n = sharp_ok = sharp_n = 0
    for name, rows in rows_by_name.items():
        soft = [r for r in rows if r["label"] == "soft"]
        sharp = [r for r in rows if r["label"] == "sharp"]
        sp = sum(1 for r in soft if passes(r, thr))
        rj = sum(1 for r in sharp if not passes(r, thr))
        soft_ok += sp; soft_n += len(soft); sharp_ok += rj; sharp_n += len(sharp)
        print("  %-46s %-16s %s"
              % (name, "%d из %d" % (sp, len(soft)), "%d из %d" % (rj, len(sharp))))
    if len(rows_by_name) > 1 and sharp_n:
        print("  %-46s %-16s %s"
              % ("ВСЕ ВМЕСТЕ", "%d из %d" % (soft_ok, soft_n),
                 "%d из %d (%.0f%%)" % (sharp_ok, sharp_n, 100.0 * sharp_ok / sharp_n)))
    if soft_n and soft_ok < soft_n:
        print()
        print("  потеряно мягких: %d из %d (%.0f%%). Правило SF (порог = максимум по мягким)"
              % (soft_n - soft_ok, soft_n, 100.0 * (soft_n - soft_ok) / soft_n))
        print("      требует пропускать все, и тогда это уже незачёт; процентильное правило")
        print("      единичные потери допускает — сверься с критерием в docs/protocol.md.")


def beat_report(rows_by_name):
    """Попадание по щелчку метронома по градациям.

    Нужно, чтобы отличить «фильтр работает» от «фильтр работает, но мягкий тап
    выбивает игрока из темпа»: в игре это два разных исхода.
    """
    rows = [r for rs in rows_by_name.values() for r in rs
            if isinstance(r.get("beatDeviationMs"), (int, float))]
    if not rows:
        return
    head("попадание по щелчку метронома, по градациям")
    print("  %-8s %5s %14s %14s" % ("градация", "n", "|отклонение|", "СКО отклонения"))
    for label in ("soft", "sharp"):
        vals = [r["beatDeviationMs"] for r in rows if r["label"] == label]
        if not vals:
            continue
        print("  %-8s %5d %11.1f мс %11.1f мс"
              % (label, len(vals), statistics.median([abs(v) for v in vals]),
                 statistics.stdev(vals) if len(vals) > 1 else 0.0))
    print("  Медиана модуля отклонения и СКО; постоянная задержка звука сдвигает обе")
    print("  градации одинаково и на сравнение между ними не влияет (PLAN §7.4).")
    print("  Считается по полной сетке долей из сессии, а не по полю beatDeviationMs.")


def margin_report(rows, key, title):
    """Запас прочности порога: где сидят мягкие и где резкие."""
    soft = sorted(r[key] for r in rows if r["label"] == "soft" and r[key] is not None)
    sharp = sorted(r[key] for r in rows if r["label"] == "sharp" and r[key] is not None)
    if not soft or not sharp:
        return
    print("  %s" % title)
    print("    мягкие: n=%d, медиана %.1f, 90-й процентиль %.1f, максимум %.1f"
          % (len(soft), statistics.median(soft), soft[int(0.9 * (len(soft) - 1))], soft[-1]))
    print("    резкие: n=%d, медиана %.1f, минимум %.1f"
          % (len(sharp), statistics.median(sharp), sharp[0]))
    zero_soft = sum(1 for v in soft if v <= 0)
    zero_sharp = sum(1 for v in sharp if v <= 0)
    if key.startswith("shift"):
        print("    нулевой сдвиг: у мягких %d из %d, у резких %d из %d"
              % (zero_soft, len(soft), zero_sharp, len(sharp)))
        if zero_soft == len(soft):
            print("    [!] у мягких сдвиг нулевой во всех контактах: признак вырождается в")
            print("        «пришёл ли хоть один move за контакт», и запаса у порога нет.")
            print("        Один дрогнувший палец игрока — одно ложное срабатывание.")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Признаки резкости тапа помимо площади: микросдвиг и комбинации")
    ap.add_argument("sessions", nargs="+", help="JSON сессии из data/, минимум две")
    ap.add_argument("--source", choices=["pointer", "touch"], default="pointer",
                    help="поток: pointer (по умолчанию — у Touch размер заморожен)")
    ap.add_argument("--window", type=int, default=50,
                    help="окно признаков с окном, мс после DOWN (по умолчанию 50)")
    ap.add_argument("--holdout", help="считать только этот расклад, а не все по очереди")
    ap.add_argument("--soft-quantile", type=float, default=1.0,
                    help="квантиль по мягким обучающим для порога: 1.0 — максимум"
                         " (правило SF, по умолчанию), 0.95 — отдать верхний выброс")
    ap.add_argument("--thresholds",
                    help="применить готовые пороги без обучения, например"
                         " \"area50=2336.369,shift50=0\" — так проверяется правило,"
                         " зафиксированное до съёмки (docs/protocol.md)")
    args = ap.parse_args(argv)

    loaded = []
    for path in args.sessions:
        session = area_mod.load(path)
        meta = session.get("meta") or {}
        planned, actual = meta.get("plannedTaps"), meta.get("actualTaps")
        if isinstance(planned, int) and isinstance(actual, int) and actual < planned:
            print("  [!] %s: снято %d из %d тапов — серия неполная, пропускаю"
                  % (os.path.basename(path), actual, planned))
            continue
        loaded.append((os.path.basename(path), session))

    if len(loaded) < 2 and not args.thresholds:
        print("нужны минимум две серии: обучение и проверка должны быть разведены")
        print("(с готовыми порогами — ключ --thresholds — хватает и одной)")
        return 2

    # Разные протоколы, участники и устройства в одном расчёте не смешиваются —
    # по тем же причинам, что в analyze_area.
    for field, title in (("protocol", "протоколы"), ("participant", "участники")):
        values = {(s.get("meta") or {}).get(field) for _, s in loaded}
        if len(values) > 1:
            print("  [!] в одном расчёте разные %s: %s" % (title, ", ".join(map(str, values))))
            print("      порог снимается с руки участника и с сенсора модели —")
            print("      считай каждого отдельным прогоном.")
    devices = {((s.get("meta") or {}).get("env") or {}).get("userAgent") for _, s in loaded}
    if len(devices) > 1:
        print("  [!] в одном расчёте серии с разных устройств: шаг квантования канала")
        print("      у каждой модели свой, абсолютные пороги не переносятся.")

    print("сессий загружено: %d, поток %s, окно %d мс"
          % (len(loaded), args.source, args.window))
    for name, session in loaded:
        meta = session.get("meta") or {}
        print("  %-46s участник %s, серия %s, протокол %s"
              % (name, meta.get("participant"), meta.get("series"), meta.get("protocol")))

    rows = {name: contact_rows(session, args.source, args.window)
            for name, session in loaded}
    everything = [r for rs in rows.values() for r in rs]
    if not everything:
        print("размеченных контактов нет: нужны серии площадного протокола")
        return 2

    beat_report(rows)

    if args.thresholds:
        frozen_check(rows, parse_thresholds(args.thresholds, args.window), args.window)
        return 0

    head("запас прочности признаков (все серии вместе)")
    margin_report(everything, "shift", "микросдвиг за контакт, px")
    margin_report(everything, "shiftW", "микросдвиг за первые %d мс, px" % args.window)

    names = [n for n, _ in loaded]
    holdouts = [os.path.basename(args.holdout)] if args.holdout else names
    head("отсев резких по наборам признаков (обучение — остальные серии)")
    if args.soft_quantile >= 1.0:
        print("  порог: максимум признака по мягким обучающим; мягким считается контакт,")
        print("  который ниже порога по всем признакам набора")
    else:
        print("  порог: %.2f-квантиль признака по мягким обучающим (не максимум —"
              % args.soft_quantile)
        print("  часть мягких отдаётся сознательно); мягким считается контакт, который")
        print("  ниже порога по всем признакам набора")
    print()
    print("  %-38s %-18s %s" % ("набор признаков", "мягких прошло", "резких отсеяно"))
    for title, keys in FEATURE_SETS:
        if args.window is None and any(k.endswith("W") for k in keys):
            continue
        per_hold, soft_sum, soft_n, sharp_sum, sharp_n = [], 0, 0, 0, 0
        for hold in holdouts:
            if hold not in rows:
                print("  [!] отложенная серия %s не найдена" % hold)
                return 2
            train = [r for n, rs in rows.items() if n != hold for r in rs]
            res = evaluate(train, rows[hold], keys, args.soft_quantile)
            per_hold.append(res)
            soft_sum += res["soft_passed"]; soft_n += res["soft_total"]
            sharp_sum += res["sharp_rejected"]; sharp_n += res["sharp_total"]
        label = title.replace("в окне", "за %d мс" % args.window)
        print("  %-38s %-18s %s"
              % (label,
                 "/".join(str(r["soft_passed"]) for r in per_hold)
                 + " из %d" % per_hold[0]["soft_total"],
                 "/".join(str(r["sharp_rejected"]) for r in per_hold)
                 + " из %d  (итого %.0f%%)" % (per_hold[0]["sharp_total"],
                                               100.0 * sharp_sum / sharp_n if sharp_n else 0)))
        # Пороги печатаются всегда: подтверждающий прогон опознаётся по ним —
        # если они разошлись с записанными в протоколе, переданы не те серии.
        shown = []
        for res in per_hold:
            shown.append(", ".join(
                "%s ≤ %.6f" % (k.replace("W", str(args.window)), v)
                for k, v in res["thr"].items() if v is not None))
        for i, line in enumerate(shown):
            print("  %-38s пороги%s: %s"
                  % ("", " (расклад %d)" % (i + 1) if len(shown) > 1 else "", line))
        if soft_n and soft_sum < soft_n:
            print("  %-38s из мягких потеряно %d — правило требует пропускать все"
                  % ("", soft_n - soft_sum))

    print()
    print("  Эталон Android (SF §4): 60/60 мягких пропущено, 27/30 резких отсеяно.")
    print()
    print("  Наборы перебраны по этим же данным: отложенная серия проверяет порог,")
    print("  но не выбор набора. Настоящее число даёт только серия, снятая после")
    print("  фиксации правила — см. docs/protocol.md, «Подтверждающая серия».")
    return 0


if __name__ == "__main__":
    sys.exit(main())
