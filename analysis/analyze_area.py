#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A1, A10 — площадь контактного пятна.

A1: живая ли геометрия (radiusX/radiusY, width/height) или константа.
A10: результат в той же мере, что SF §4 — сколько мягких прошло и сколько
     резких отсеяно на отложенной серии.

Методика взята из softness-filter без изменений, чтобы числа были сопоставимы
(PLAN §7.5): признак — максимум площади за контакт (не первый отсчёт, фильтр
живёт на краю распределения); обучение на двух сериях, проверка на третьей
отложенной; порог — максимум признака по мягким тапам обучающих серий;
градации только мягко/резко. Абсолютные пороги с Android несопоставимы —
другой сенсор, другие единицы.

    python analysis/analyze_area.py data/s1.json data/s2.json data/s3.json
    python analysis/analyze_area.py data/*.json --holdout data/s3.json --csv out/area.csv

Только стандартная библиотека.
"""

import argparse
import csv
import json
import math
import os
import statistics
import sys
from collections import Counter

GEOMETRY_TOUCH = ["radiusX", "radiusY", "rotationAngle", "force"]
GEOMETRY_POINTER = ["width", "height", "pressure"]


# --- общие мелочи ----------------------------------------------------------

def load(path):
    with open(path, "r", encoding="utf-8") as fh:
        session = json.load(fh)
    if session.get("format") != "web-probe-0.1.0":
        print("  [!] формат %r, ожидался web-probe-0.1.0 — читаю как есть"
              % session.get("format"))
    return session


def describe(values):
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


def hist(values, lo, hi, bins, width=44, label=""):
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return "  (пусто)"
    step = (hi - lo) / bins if hi > lo else 1.0
    counts = [0] * bins
    for v in vals:
        idx = int((v - lo) / step)
        idx = 0 if idx < 0 else (bins - 1 if idx >= bins else idx)
        counts[idx] += 1
    top = max(counts) or 1
    lines = ["  " + label] if label else []
    for i, c in enumerate(counts):
        lines.append("  %9.3f | %-*s %d" % (lo + i * step, width, "#" * int(round(c / top * width)), c))
    return "\n".join(lines)


def head(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# --- контакты и признак ----------------------------------------------------

def area_touch(e):
    """Площадь эллипса пятна по Touch Events."""
    rx, ry = e.get("radiusX"), e.get("radiusY")
    if not isinstance(rx, (int, float)):
        return None
    if not isinstance(ry, (int, float)):
        ry = rx
    return math.pi * rx * ry


def area_pointer(e):
    """Площадь прямоугольника пятна по Pointer Events."""
    w, h = e.get("width"), e.get("height")
    if not isinstance(w, (int, float)) or not isinstance(h, (int, float)):
        return None
    return w * h


AREA_FN = {"touch": area_touch, "pointer": area_pointer}


def contacts(session, source):
    """Контакты одного потока: все события между DOWN и UP одного id."""
    if source == "pointer":
        down_t, up_t, cancel_t, id_key = "pointerdown", "pointerup", "pointercancel", "pointerId"
        move_t = "pointermove"
    else:
        down_t, up_t, cancel_t, id_key = "touchstart", "touchend", "touchcancel", "identifier"
        move_t = "touchmove"

    open_by_id, out = {}, []
    for e in session.get("events", []):
        if e.get("source") != source:
            continue
        typ, key = e.get("type"), e.get(id_key)
        if typ == down_t:
            open_by_id[key] = {"events": [e], "label": e.get("label"),
                               "tapIndex": e.get("tapIndex")}
        elif typ in (move_t, up_t, cancel_t):
            cur = open_by_id.get(key)
            if cur is None:
                continue
            cur["events"].append(e)
            if typ in (up_t, cancel_t):
                cur["closed"] = (typ == up_t)
                out.append(open_by_id.pop(key))
    # Незакрытые контакты тоже учитываем: терять тап из-за пропавшего UP нельзя.
    for cur in open_by_id.values():
        cur["closed"] = False
        out.append(cur)
    return out


def feature(contact, source):
    """Признак SF §5: максимум площади за контакт, а не первый отсчёт."""
    fn = AREA_FN[source]
    areas = [fn(e) for e in contact["events"]]
    areas = [a for a in areas if isinstance(a, (int, float))]
    if not areas:
        return None
    return max(areas)


# --- A1 --------------------------------------------------------------------

def a1_channels(sessions):
    head("A1 — живая геометрия или константа")
    live = {}
    for source, fields in (("touch", GEOMETRY_TOUCH), ("pointer", GEOMETRY_POINTER)):
        for f in fields:
            vals, nonnull = [], []
            for _, session in sessions:
                for e in session.get("events", []):
                    if e.get("source") != source:
                        continue
                    if e.get("type") not in ("touchstart", "touchmove", "pointerdown", "pointermove"):
                        continue
                    v = e.get(f)
                    vals.append(v)
                    if isinstance(v, (int, float)):
                        nonnull.append(v)
            if not vals:
                continue
            distinct = sorted(set(nonnull))
            if not nonnull:
                state, is_live = "все null — канал отсутствует", False
            elif len(distinct) == 1:
                state, is_live = "КОНСТАНТА %s" % distinct[0], False
            elif len(distinct) == 2:
                # Два значения на весь канал — это не градация, а ступенька
                # (например разные значения на down и на move). Для признака
                # площади такой канал непригоден, живым не считается.
                state, is_live = ("почти константа: два значения %s и %s"
                                  % (distinct[0], distinct[1])), False
            else:
                state = ("живой: %s … %s, различных значений %d"
                         % (distinct[0], distinct[-1], len(distinct)))
                is_live = True
            live["%s.%s" % (source, f)] = is_live
            print("  %-8s %-16s непустых %4d/%-4d  %s"
                  % (source, f, len(nonnull), len(vals), state))
            if is_live and len(distinct) <= 12:
                print("           значения: %s" % ", ".join(str(x) for x in distinct))

    print()
    touch_live = live.get("touch.radiusX") or live.get("touch.radiusY")
    pointer_live = live.get("pointer.width") or live.get("pointer.height")
    if touch_live or pointer_live:
        print("  вывод A1: геометрия живая (%s%s%s) — площадная половина имеет смысл."
              % ("Touch" if touch_live else "",
                 " и " if touch_live and pointer_live else "",
                 "Pointer" if pointer_live else ""))
    else:
        print("  вывод A1: геометрия константа — это отрицательный результат, а не")
        print("  брак записи (PLAN §10). Площадная половина закрывается, тайминговая")
        print("  от этого не зависит.")
    return touch_live, pointer_live


def collect_features(sessions, source):
    """(имя сессии, метка, признак) по всем контактам."""
    rows = []
    for name, session in sessions:
        for c in contacts(session, source):
            f = feature(c, source)
            if f is None:
                continue
            rows.append({"session": name, "label": c["label"], "feature": f,
                         "tapIndex": c["tapIndex"], "closed": c.get("closed")})
    return rows


def a10_filter(train_rows, test_rows, source):
    print("  поток: %s, признак: максимум площади за контакт" % source)

    train_soft = [r["feature"] for r in train_rows if r["label"] == "soft"]
    train_sharp = [r["feature"] for r in train_rows if r["label"] == "sharp"]
    if not train_soft:
        print("  в обучающих сериях нет тапов с меткой soft — считать нечего.")
        print("  Проверь, что серии сняты режимом «площадь» (collect.html).")
        return
    print("  обучение: мягких %d, резких %d" % (len(train_soft), len(train_sharp)))
    print("    мягкие: %s" % fmt_desc(describe(train_soft)))
    print("    резкие: %s" % fmt_desc(describe(train_sharp)))

    # Направление признака. План (§7.5) берёт порог как максимум признака по
    # мягким: подразумевается, что резкий тап даёт большую площадь. Если в
    # данных наоборот, правило пропустит всё подряд и даст молча бессмысленный
    # ответ, поэтому направление проверяется.
    inverted = False
    if train_sharp:
        inverted = statistics.median(train_sharp) <= statistics.median(train_soft)
        if inverted:
            print()
            print("  [!] НАПРАВЛЕНИЕ ПРИЗНАКА ОБРАТНОЕ: в обучающих сериях резкие тапы")
            print("      дают МЕНЬШУЮ площадь, чем мягкие (медианы %.3f против %.3f)."
                  % (statistics.median(train_sharp), statistics.median(train_soft)))
            print("      Правило «порог = максимум по мягким» рассчитано на обратный знак.")
            print("      Ниже считаются оба варианта.")

    # Порог — максимум признака по мягким тапам обучающих серий (SF §7.5):
    # фильтр обязан пропускать все мягкие, цена — часть резких проходит тоже.
    threshold = max(train_soft)
    print("  порог (максимум признака по мягким обучающим): %.3f" % threshold)
    print("  абсолютное значение с Android несопоставимо — другой сенсор,"
          " другие единицы (PLAN §7.5)")

    test_soft = [r["feature"] for r in test_rows if r["label"] == "soft"]
    test_sharp = [r["feature"] for r in test_rows if r["label"] == "sharp"]
    if not test_soft and not test_sharp:
        print("  в отложенной серии нет размеченных тапов — проверка невозможна.")
        return

    lo = min(train_soft + train_sharp + test_soft + test_sharp)
    hi = max(train_soft + train_sharp + test_soft + test_sharp)
    if hi > lo:
        print()
        print(hist(test_soft, lo, hi, 18, label="отложенная серия: мягкие"))
        print(hist(test_sharp, lo, hi, 18, label="отложенная серия: резкие"))

    passed = sum(1 for v in test_soft if v <= threshold)
    rejected = sum(1 for v in test_sharp if v > threshold)
    print()
    print("  РЕЗУЛЬТАТ в мере SF §4:")
    print("    мягких прошло:  %d / %d" % (passed, len(test_soft)))
    print("    резких отсеяно: %d / %d" % (rejected, len(test_sharp)))
    if len(test_soft):
        print("    доля пропущенных мягких: %.0f%%" % (100.0 * passed / len(test_soft)))
    if len(test_sharp):
        print("    доля отсеянных резких:   %.0f%%" % (100.0 * rejected / len(test_sharp)))

    if inverted:
        # Зеркальное правило: порог — минимум по мягким, мягкое сверху.
        mirror = min(train_soft)
        m_passed = sum(1 for v in test_soft if v >= mirror)
        m_rejected = sum(1 for v in test_sharp if v < mirror)
        print()
        print("  зеркальный вариант (порог = минимум по мягким, %.3f):" % mirror)
        print("    мягких прошло:  %d / %d" % (m_passed, len(test_soft)))
        print("    резких отсеяно: %d / %d" % (m_rejected, len(test_sharp)))
    print()
    print("  Эталон Android (SF §4): 60/60 мягких пропущено, 27/30 резких отсеяно.")
    print("  Сравнивается доля, не порог.")


def write_csv(path, rows, source):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["session", "source", "tapIndex", "label", "featureMaxArea", "closed"])
        for r in rows:
            w.writerow([r["session"], source, r["tapIndex"], r["label"],
                        "%.4f" % r["feature"], r["closed"]])

def within_contact_dynamics(sessions):
    """Меняется ли размер пятна в течение контакта — по каждому потоку.

    От этого зависит судьба признака SF §5 («максимум площади за контакт»):
    если значение заморожено на момент касания, максимум брать не из чего и
    признак вырождается в «значение при касании». Потоки на iPhone ведут себя
    по-разному, поэтому проверяются оба.
    """
    head("динамика пятна внутри контакта")
    for source in ("touch", "pointer"):
        total = moving = grew = 0
        for _, session in sessions:
            for c in contacts(session, source):
                vals = [AREA_FN[source](e) for e in c["events"]]
                vals = [v for v in vals if isinstance(v, (int, float)) and v > 0]
                if len(vals) < 2:
                    continue
                total += 1
                if len(set(vals)) > 1:
                    moving += 1
                if max(vals) > vals[0]:
                    grew += 1
        if not total:
            print("  %-8s контактов с двумя и более отсчётами нет" % source)
            continue
        print("  %-8s контактов с >=2 отсчётами: %3d, значение менялось: %3d,"
              " максимум выше начального: %3d" % (source, total, moving, grew))
        if moving == 0:
            print("           значение заморожено на момент касания: признак «максимум за")
            print("           контакт» здесь вырождается в «значение при касании»")


def run_for_source(loaded, source, args, holdout):
    """Полный расчёт A10 по одному потоку."""
    rows = collect_features(loaded, source)
    if args.csv:
        write_csv(args.csv, rows, source)
        print("  CSV: %s" % args.csv)

    labels = Counter(r["label"] for r in rows)
    print("  контактов с признаком: %d, метки: %s" % (len(rows), dict(labels)))

    if len(loaded) < 2:
        print("  A10 требует минимум двух серий: обучение на двух, проверка на"
              " отложенной третьей (SF §2). Сейчас серия одна — считается только A1.")
        return
    if len(loaded) < 3:
        print("  [!] серий меньше трёх: обучение и проверка разведены, но запаса нет."
              " По SF §3.1 отложенная серия обязательна — досними третью.")

    train_rows = [r for r in rows if r["session"] != holdout]
    test_rows = [r for r in rows if r["session"] == holdout]
    print("  отложенная серия: %s (контактов %d), обучающих контактов %d"
          % (holdout, len(test_rows), len(train_rows)))
    if not test_rows:
        print("  [!] отложенная серия не найдена среди загруженных — проверь --holdout")
        return
    a10_filter(train_rows, test_rows, source)


def main(argv=None):
    ap = argparse.ArgumentParser(description="A1, A10 — площадь контактного пятна")
    ap.add_argument("sessions", nargs="+", help="JSON сессии из data/")
    ap.add_argument("--holdout", help="отложенная серия (по умолчанию — последняя из списка)")
    ap.add_argument("--source", choices=["auto", "touch", "pointer"], default="auto",
                    help="поток геометрии; auto считает оба, потому что они отличаются")
    ap.add_argument("--csv", help="выгрузить признаки по контактам в CSV")
    ap.add_argument("--include-partial", action="store_true",
                    help="не исключать прерванные серии (по умолчанию исключаются)")
    args = ap.parse_args(argv)

    loaded, partial = [], []
    for path in args.sessions:
        name = os.path.basename(path)
        session = load(path)
        meta = session.get("meta") or {}
        planned, actual = meta.get("plannedTaps"), meta.get("actualTaps")
        # Прерванная серия — это серия с оборванным блоком градаций: доли по
        # градациям перекошены, в обучение и проверку она идти не должна.
        if isinstance(planned, int) and isinstance(actual, int) and actual < planned:
            partial.append((name, actual, planned))
            if not args.include_partial:
                continue
        loaded.append((name, session))
    if partial:
        print("неполные серии: %d" % len(partial))
        for name, actual, planned in partial:
            print("  %-44s снято %d из %d тапов — %s"
                  % (name, actual, planned,
                     "включена по --include-partial" if args.include_partial else "ИСКЛЮЧЕНА"))
    if not loaded:
        print("не осталось ни одной пригодной серии")
        return 2

    # Серии разных протоколов нельзя молча смешивать: условия исполнения разные,
    # и порог, снятый по их смеси, не описывает ни одну из съёмок.
    protocols = {}
    for name, session in loaded:
        protocols.setdefault((session.get("meta") or {}).get("protocol"), []).append(name)
    if len(protocols) > 1:
        print()
        print("  [!] В ОДНОМ РАСЧЁТЕ СМЕШАНЫ РАЗНЫЕ ПРОТОКОЛЫ:")
        for proto, names in protocols.items():
            print("      %-28s %d серия(й)" % (proto, len(names)))
        print("      Обучение и проверка должны идти по сериям одного протокола.")
        print("      Считаю как просили, но число A10 при такой смеси не сопоставимо")
        print("      ни с эталоном, ни с другими прогонами.")

    print("сессий загружено: %d" % len(loaded))
    for name, session in loaded:
        meta = session.get("meta") or {}
        print("  %-44s участник %s, серия %s, протокол %s"
              % (name, meta.get("participant"), meta.get("series"), meta.get("protocol")))
        kind = ((meta.get("env") or {}).get("browserKind"))
        if kind and kind != "safari":
            print("      [!] СРЕДА НЕ SAFARI (%s) — серия не является измерением" % kind)

    touch_live, pointer_live = a1_channels(loaded)
    within_contact_dynamics(loaded)

    holdout = os.path.basename(args.holdout) if args.holdout else loaded[-1][0]
    if args.source == "auto":
        sources = [src for src, live in (("touch", touch_live), ("pointer", pointer_live)) if live]
        if not sources:
            print()
            print("  A10 не считается: живого канала геометрии нет.")
            return 0
    else:
        sources = [args.source]

    for src in sources:
        head("A10 по потоку %s" % src)
        run_for_source(loaded, src, args, holdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
