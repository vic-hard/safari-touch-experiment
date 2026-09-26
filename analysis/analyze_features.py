#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Признаки резкости тапа помимо площади: микросдвиг, ускорение и комбинации.

Площадь пятна (SF §5, `analyze_area.py`) на естественном тапе одной частью
пальца даёт мало: 56% отсева на iPhone 15 и 0% на iPhone 16 Pro Max. Здесь
проверяются остальные признаки, которые в записи уже есть, и их объединения.

Признаки считаются по контакту одного потока:

    area        максимум площади пятна за контакт (тот же, что в analyze_area)
    shift       максимальный сдвиг точки контакта от места касания, px
    shiftNN     то же, но видны только отсчёты первых NN мс после DOWN
    areaNN      максимум площади за первые NN мс после DOWN
    accelNN     максимум модуля ускорения корпуса за первые NN мс, м/с²
    accelbgNN   то же, делённое на фон игрока (нижний квартиль |a| за 5 с до
                касания) — от хвата и от человека зависит меньше сырого
    windupbg    замах: максимум |a| за 100 мс ДО касания к тому же фону —
                ответ, известный в самый момент касания

Ускорение считается в `analyze_motion.py` (там же разобрано, почему фон такой);
у серий, снятых без канала ускорения, эти признаки пустые, и наборы с ними
пропускаются.

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
import itertools
import math
import os
import random
import statistics
import sys

import analyze_area as area_mod
import analyze_motion as motion_mod

# Наборы признаков, которые имеет смысл сравнивать между собой. Порядок — от
# одиночных к объединениям, чтобы в выводе было видно, что даёт объединение.
FEATURE_SETS = [
    ("площадь (признак SF §5)", ["area"]),
    ("микросдвиг за контакт", ["shift"]),
    ("площадь ИЛИ микросдвиг", ["area", "shift"]),
    ("микросдвиг в окне", ["shiftW"]),
    ("площадь ИЛИ микросдвиг в окне", ["area", "shiftW"]),
    ("площадь в окне ИЛИ микросдвиг в окне", ["areaW", "shiftW"]),
    ("ускорение в окне", ["accelW"]),
    ("ускорение к фону в окне", ["accelbgW"]),
    ("замах к фону (в момент касания)", ["windupbg"]),
    ("площадь в окне ИЛИ ускорение к фону", ["areaW", "accelbgW"]),
    ("площадь, микросдвиг, ускорение к фону", ["areaW", "shiftW", "accelbgW"]),
]

# Признаки канала ускорения: наборы с ними считаются, только если канал есть у
# всех серий расчёта, иначе серия без канала потеряла бы все мягкие тапы.
MOTION_KEYS = ("accelW", "accelbgW", "windupbg")


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
    contacts = area_mod.contacts(session, source)
    motion = motion_mod.tap_features(session, contacts, window_ms)
    rows = []
    for i, c in enumerate(contacts):
        label = c.get("label")
        if label not in ("soft", "sharp"):
            continue
        m = motion[i] if motion else {}
        down = c["events"][0]
        up = c["events"][-1]
        t0, t1 = down.get("timeStamp"), up.get("timeStamp")
        rows.append({
            "label": label,
            "tapIndex": c.get("tapIndex"),
            "beatDeviationMs": beat_deviation(down.get("timeStamp"), grid),
            "area": area_mod.feature(c, source),
            "areaW": area_of(c, source, window_ms),
            "shift": shift_of(c),
            "shiftW": shift_of(c, window_ms),
            # Длительность в наборы признаков не входит: она известна только на
            # отпускании, то есть на 60–200 мс позже, чем игре нужен ответ.
            # Считается ради проверки разделимости — чтобы видеть, не разделял
            # ли участник градации тем, чего фильтр не видит.
            "dur": (t1 - t0) if all(isinstance(v, (int, float)) for v in (t0, t1)) else None,
            "accelW": m.get("accel"),
            "accelbgW": m.get("accelbg"),
            "windupbg": m.get("windupbg"),
            "bg": m.get("bg"),
            "hasMotion": motion is not None,
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


# Признаки с окном: имя в пороге — основа плюс окно в мс (area50, accelbg50).
WINDOWED = ("area", "shift", "accel", "accelbg")


def key_name(key, window_ms):
    """accelbgW → accelbg50: имя признака так, как оно пишется в пороге."""
    return key[:-1] + str(window_ms) if key.endswith("W") else key


def fmt_thr(value):
    """Порог для печати: шесть знаков, округление только вверх.

    Выписанный порог потом вставляют в --thresholds. Округлённый вниз, он
    оказывается чуть ниже того мягкого тапа, по которому снят, и отсеивает его:
    у непрерывного ускорения так теряется мягкий тап на каждом переносе порога.
    """
    text = "%.6f" % value
    if float(text) < value:
        text = "%.6f" % (float(text) + 1e-6)
    return text


def parse_thresholds(text, window_ms):
    """«area50=2336.369,shift50=0» → {'areaW': 2336.369, 'shiftW': 0.0}.

    Имена те же, что в docs/protocol.md: area, shift — по всему контакту,
    areaNN, shiftNN, accelNN, accelbgNN — по окну NN мс, windupbg — замах, у
    него окно своё. Окно в имени обязано совпасть с --window, иначе правило
    считалось бы не то, которое зафиксировано.
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
        if name in ("area", "shift", "windupbg"):
            out[name] = value
            continue
        for base in WINDOWED:
            if name.startswith(base) and name[len(base):].isdigit():
                if int(name[len(base):]) != window_ms:
                    raise SystemExit(
                        "порог %s не сходится с окном --window %d: правило зафиксировано"
                        " на другом окне" % (name, window_ms))
                out[base + "W"] = value
                break
        else:
            raise SystemExit("непонятное имя порога %r (ожидалось area, shift, windupbg,"
                             " area<мс>, shift<мс>, accel<мс>, accelbg<мс>)" % name)
    if not out:
        raise SystemExit("--thresholds пуст")
    return out


def warn_near_miss(rows_by_name, thr, window_ms):
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
            print("  [!] порог %s = %.6f лежит на %.2g ниже значения %.9f, которое"
                  % (key_name(key, window_ms), limit, min(near) - limit, min(near)))
            print("      встречается в данных %d раз. Похоже на потерю точности при"
                  % len(near))
            print("      выписывании порога: проверь, не округлён ли он вниз.")


def frozen_check(rows_by_name, thr, window_ms):
    """Правило с заранее зафиксированными порогами: обучения нет вовсе."""
    head("проверка зафиксированным правилом (обучение не проводится)")
    print("  пороги: %s" % ", ".join(
        "%s ≤ %s" % (key_name(k, window_ms), fmt_thr(v)) for k, v in sorted(thr.items())))
    warn_near_miss(rows_by_name, thr, window_ms)
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


def auc(sharp, soft):
    """Доля пар «резкий, мягкий», где у резкого признак больше; совпадения — половина.

    0.5 означает, что признак и градация не связаны никак. Величина не зависит
    ни от порога, ни от того, сколько ступеней занял канал, поэтому ею и
    проверяется главная предпосылка всего фильтра: что разделять вообще есть чем.
    """
    if not sharp or not soft:
        return None
    hits = 0.0
    for a in sharp:
        for b in soft:
            hits += 1.0 if a > b else (0.5 if a == b else 0.0)
    return hits / (len(sharp) * len(soft))


def auc_p_value(values, labels, observed, iters=1500):
    """Перестановочный тест: как часто случайная разметка даёт такое же AUC.

    Аналитической формулы для квантованного канала с массой совпадений нет, а
    перестановки её не требуют. Порядок перестановок фиксирован семенем, чтобы
    число в отчёте воспроизводилось.
    """
    if observed is None:
        return None
    rng = random.Random(20260922)
    pool = list(values)
    extreme = 0
    for _ in range(iters):
        rng.shuffle(pool)
        sharp = [v for v, lab in zip(pool, labels) if lab == "sharp"]
        soft = [v for v, lab in zip(pool, labels) if lab == "soft"]
        a = auc(sharp, soft)
        if a is not None and abs(a - 0.5) >= abs(observed - 0.5):
            extreme += 1
    return (extreme + 1.0) / (iters + 1.0)


def separability_report(rows_by_name, window_ms):
    """Есть ли в канале что разделять — до всякого порога.

    Порог, подобранный на данных, где градации неразличимы, выглядит как
    работающий: мягкие сохраняются все, отсев близок к нулю, ни одно правило не
    нарушено. Отличить этот случай от рабочего можно только так — мерой, которая
    от порога не зависит. У участников, снятых 21.09.2026, ровно это и вышло:
    AUC 0.47–0.62 при 0.93–0.99 у тех, кто калибровался.
    """
    rows = [r for rs in rows_by_name.values() for r in rs]
    head("разделимость градаций в канале (до выбора порога)")
    print("  AUC — доля пар «резкий, мягкий», где у резкого признак больше. 0.5 —")
    print("  связи нет; p — перестановочный тест, 1500 перестановок, семя фиксировано.")
    print()
    print("  %-28s %5s %8s %8s" % ("признак", "n", "AUC", "p"))
    verdicts = []
    features = [("areaW", "площадь за %d мс" % window_ms),
                ("shiftW", "микросдвиг за %d мс" % window_ms)]
    if all(r["hasMotion"] for r in rows):
        features += [("accelW", "ускорение за %d мс" % window_ms),
                     ("accelbgW", "ускорение к фону за %d мс" % window_ms),
                     ("windupbg", "замах к фону, -100…0 мс")]
        no_bg = sum(1 for r in rows if r["accelW"] is not None and r["bg"] is None)
        if no_bg:
            print("  [!] у %d контактов из %d нет фона: меньше %d отсчётов за %d с до касания."
                  % (no_bg, len(rows), motion_mod.BG_MIN_SAMPLES, -motion_mod.BG_FROM_MS // 1000))
            print("      Ускорение к фону у них не считается, и фильтр отсеет их как резкие.")
            print()
    features.append(("dur", "длительность контакта"))
    for key, title in features:
        vals = [r[key] for r in rows if r[key] is not None]
        labels = [r["label"] for r in rows if r[key] is not None]
        if not vals:
            print("  %-28s %5d %8s %8s" % (title, 0, "—", "—"))
            print("      признак не измерен ни у одного контакта")
            verdicts.append(False)
            continue
        if len(set(vals)) < 2:
            print("  %-28s %5d %8s %8s" % (title, len(vals), "—", "—"))
            print("      канал на этих сериях константа — разделять нечем")
            verdicts.append(False)
            continue
        a = auc([v for v, lab in zip(vals, labels) if lab == "sharp"],
                [v for v, lab in zip(vals, labels) if lab == "soft"])
        pv = auc_p_value(vals, labels, a)
        print("  %-28s %5d %8.2f %8.3f" % (title, len(vals), a, pv))
        if key != "dur":
            verdicts.append(pv is not None and pv < 0.05 and a > 0.5)
        elif pv is not None and pv < 0.05:
            note = "короче" if a < 0.5 else "длиннее"
            print("      градации различаются длительностью (у резких контакт %s)." % note)
            print("      В набор признаков она не входит: известна только на отпускании.")
    if not any(verdicts):
        print()
        print("  [!] ни один признак фильтра градации не различает. Порог, снятый с таких")
        print("      данных, пройдёт по всем правилам и при этом не будет отсеивать ничего:")
        print("      сохранится 100% мягких и около 0% резких. Это не настройка порога,")
        print("      а отсутствующий сигнал — сначала разбираться с жестом и инструкцией")
        print("      (docs/protocol.md, «Проверка разделимости»), а не с порогом.")


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


SINGLE_KEEP = 0.99   # доля мягких, которую единый порог обязан сохранить


def single_threshold(rows, keys, keep=SINGLE_KEEP):
    """Единый порог набора: наибольший отсев резких при ≥ keep мягких вместе.

    Правило docs/protocol.md, «Единый порог для всех темпов», — теперь не вручную,
    а перебором. Оптимальный порог по признаку всегда совпадает с каким-то
    значением мягкого тапа (между ними отсев не меняется), а ниже своего
    (1 − keep)-выброса признак не может опуститься, даже будь он в наборе один:
    сам потеряет больше мягких, чем разрешено. Поэтому кандидатов по признаку —
    единицы, и перебор их сочетаний точный, без жадного выбора по одному.
    Из равных по отсеву берётся сохраняющий больше мягких.
    """
    soft = [r for r in rows if r["label"] == "soft"]
    sharp = [r for r in rows if r["label"] == "sharp"]
    if not soft or not sharp:
        return None
    need = math.ceil(keep * len(soft))
    cands = []
    for k in keys:
        vals = sorted({r[k] for r in soft if r[k] is not None})
        full = sorted(r[k] for r in soft if r[k] is not None)
        if len(full) < need:
            return None          # признак не измерен у слишком многих мягких
        floor = full[need - 1]
        cands.append([v for v in vals if v >= floor])
    combos = 1
    for c in cands:
        combos *= len(c)
    if combos > 200000:
        raise SystemExit("единый порог: %d сочетаний кандидатов — слишком много" % combos)
    best = None
    for combo in itertools.product(*cands):
        thr = dict(zip(keys, combo))
        kept = sum(1 for r in soft if passes(r, thr))
        if kept < need:
            continue
        rejected = sum(1 for r in sharp if not passes(r, thr))
        score = (rejected, kept)
        if best is None or score > best[0]:
            best = (score, thr)
    if best is None:
        return None
    return best[1]


def single_report(rows_by_name, sets, window_ms):
    """Единый порог на всех загруженных сериях вместе, с разбивкой по сериям."""
    head("единый порог на все серии вместе (мягких сохранено ≥ %.0f%%)" % (100 * SINGLE_KEEP))
    print("  Порог один на все загруженные серии — все темпы и режимы сразу, как в")
    print("  продукте. Подобран на этих же данных: число обучающее, не подтверждённое.")
    everything = [r for rs in rows_by_name.values() for r in rs]
    for title, keys in sets:
        thr = single_threshold(everything, keys)
        print()
        print("  %s" % title.replace("в окне", "за %d мс" % window_ms))
        if thr is None:
            print("    порога нет: признак не измерен у слишком многих мягких")
            continue
        print("    пороги: %s" % ", ".join("%s ≤ %s" % (key_name(k, window_ms), fmt_thr(v))
                                         for k, v in thr.items()))
        soft_ok = soft_n = sharp_ok = sharp_n = 0
        for name, rows in rows_by_name.items():
            soft = [r for r in rows if r["label"] == "soft"]
            sharp = [r for r in rows if r["label"] == "sharp"]
            sp = sum(1 for r in soft if passes(r, thr))
            rj = sum(1 for r in sharp if not passes(r, thr))
            soft_ok += sp; soft_n += len(soft); sharp_ok += rj; sharp_n += len(sharp)
            print("    %-46s мягких %-9s резких отсеяно %s"
                  % (name, "%d/%d" % (sp, len(soft)), "%d/%d" % (rj, len(sharp))))
        print("    %-46s мягких %-9s резких отсеяно %s"
              % ("ВСЕ ВМЕСТЕ", "%d/%d" % (soft_ok, soft_n),
                 "%d/%d (%.0f%%)" % (sharp_ok, sharp_n, 100.0 * sharp_ok / sharp_n)))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Признаки резкости тапа помимо площади: микросдвиг, ускорение и комбинации")
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

    no_motion = sorted(n for n, rs in rows.items() if rs and not rs[0]["hasMotion"])
    sets = FEATURE_SETS
    if no_motion:
        sets = [(t, ks) for t, ks in FEATURE_SETS if not any(k in MOTION_KEYS for k in ks)]
        print("  канала ускорения нет у %d серий из %d (сняты до этапа 2 или без"
              % (len(no_motion), len(rows)))
        print("  разрешения) — наборы с ускорением не считаются.")

    beat_report(rows)
    separability_report(rows, args.window)

    if args.thresholds:
        thr = parse_thresholds(args.thresholds, args.window)
        if no_motion and any(k in MOTION_KEYS for k in thr):
            print("  [!] в правиле есть порог по ускорению, а у серий %s канала нет:"
                  % ", ".join(no_motion))
            print("      их мягкие тапы отсеялись бы все. Считай их без порога по ускорению.")
            return 2
        frozen_check(rows, thr, args.window)
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
    for title, keys in sets:
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
                "%s ≤ %s" % (key_name(k, args.window), fmt_thr(v))
                for k, v in res["thr"].items() if v is not None))
        for i, line in enumerate(shown):
            print("  %-38s пороги%s: %s"
                  % ("", " (расклад %d)" % (i + 1) if len(shown) > 1 else "", line))
        if soft_n and soft_sum < soft_n:
            print("  %-38s из мягких потеряно %d — правило требует пропускать все"
                  % ("", soft_n - soft_sum))

    single_report(rows, sets, args.window)

    print()
    print("  Эталон Android (SF §4): 60/60 мягких пропущено, 27/30 резких отсеяно.")
    print()
    print("  Наборы перебраны по этим же данным: отложенная серия проверяет порог,")
    print("  но не выбор набора. Настоящее число даёт только серия, снятая после")
    print("  фиксации правила — см. docs/protocol.md, «Подтверждающая серия».")
    return 0


if __name__ == "__main__":
    sys.exit(main())
