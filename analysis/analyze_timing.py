#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A4–A8 — тайминг DOWN.

A4: смещение pointerdown от границы кадра (с обязательным контролем по move).
A5: интервал DOWN→UP и его квантование.
A6: доля потерянных и отменённых контактов.
A7: расхождение потоков Touch и Pointer.
A8: СКО попадания по метроному против величины кадрового квантования.

    python analysis/analyze_timing.py data/<session>.json [ещё сессии...]
    python analysis/analyze_timing.py data/*.json --csv out/timing.csv

Только стандартная библиотека.
"""

import argparse
import bisect
import csv
import json
import math
import os
import statistics
import sys
from collections import Counter

FRAME_60HZ_MS = 1000.0 / 60.0


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
    return ("n=%d  среднее %.2f  СКО %.2f  [%.2f … %.2f]  медиана %.2f"
            % (d["n"], d["mean"], d["sd"], d["min"], d["max"], d["p50"]))


def hist(values, lo, hi, bins, width=44, label=""):
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
        lines.append("  %7.2f | %-*s %d" % (lo + i * step, width, "#" * int(round(c / top * width)), c))
    if below or above:
        lines.append("  вне диапазона: ниже %d, выше %d" % (below, above))
    return "\n".join(lines)


def rayleigh(values, period):
    """Сбиты ли значения в полосу внутри периода.

    Критерий Рэлея по фазе x mod period: R около нуля — равномерно по
    интервалу, R близко к единице — узкая полоса. p = exp(-n R^2).
    Это и есть формальная замена взгляду на гистограмму.
    """
    vals = [v for v in values if isinstance(v, (int, float))]
    n = len(vals)
    if n < 3 or not period:
        return None
    c = s = 0.0
    for v in vals:
        theta = 2 * math.pi * ((v % period) / period)
        c += math.cos(theta)
        s += math.sin(theta)
    r = math.hypot(c, s) / n
    p = math.exp(-n * r * r)
    # Обратный перевод R в «ширину полосы»: СКО фазы при большом R.
    width = period * math.sqrt(-2 * math.log(r)) / (2 * math.pi) if 0 < r < 1 else None
    return {"n": n, "R": r, "p": p, "bandWidthMs": width}


def fmt_rayleigh(ra):
    if not ra:
        return "мало данных"
    band = "полоса" if ra["p"] < 0.01 and ra["R"] > 0.3 else "равномерно"
    w = ("~%.2f мс" % ra["bandWidthMs"]) if ra["bandWidthMs"] else "—"
    return ("R=%.3f  p=%.2g  ширина %s  ->  %s" % (ra["R"], ra["p"], w, band))


def is_band(ra):
    return bool(ra and ra["p"] < 0.01 and ra["R"] > 0.3)


def head(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def frame_period(session):
    frames = [t for t in session.get("frames", []) if isinstance(t, (int, float))]
    diffs = [b - a for a, b in zip(frames, frames[1:]) if b > a]
    if len(diffs) < 3:
        return FRAME_60HZ_MS, None
    return statistics.median(diffs), describe(diffs)


def events_of(session, source=None, types=None):
    out = []
    for e in session.get("events", []):
        if source and e.get("source") != source:
            continue
        if types and e.get("type") not in types:
            continue
        out.append(e)
    return out


def frames_sanity(session, period):
    """Живы ли кадры вообще.

    rAF останавливается, когда страница уходит в фон или гаснет экран. Тогда
    привязка события к кадру либо пустая, либо считается от давно устаревшей
    метки — и A4 молча превращается в бессмыслицу. Поэтому кадры проверяются
    до любых выводов.
    """
    head("кадры: пригодны ли данные для A4")
    frames = [t for t in session.get("frames", []) if isinstance(t, (int, float))]
    meta = session.get("meta") or {}
    stalls = meta.get("frameStalls")
    print("  кадров записано: %d" % len(frames))
    if stalls:
        print("  [!] сторож на странице зафиксировал простоев кадров: %s" % stalls)
    if len(frames) < 3:
        print("  [!] КАДРОВ НЕТ. Смещение от границы кадра посчитать не из чего:")
        print("      A4 по этой серии не считается, серию надо переснять с")
        print("      страницей на переднем плане.")
        return False

    span = frames[-1] - frames[0]
    expected = span / period if period else 0
    ratio = len(frames) / expected if expected else 0
    print("  интервал записи: %.1f с, ожидалось кадров ~%.0f, доля записанных %.2f"
          % (span / 1000.0, expected, ratio))
    # Крупные разрывы: пропуск в несколько кадров подряд.
    gaps = [b - a for a, b in zip(frames, frames[1:]) if (b - a) > period * 3]
    if gaps:
        print("  [!] разрывов длиннее трёх кадров: %d, самый долгий %.0f мс"
              % (len(gaps), max(gaps)))
        print("      события, попавшие в разрыв, привязаны к устаревшей границе кадра")
    if ratio < 0.8:
        print("  [!] кадры шли с перебоями — выводы по A4 делать осторожно")
        return True
    print("  кадры идут ровно — данные для A4 пригодны")
    return True


# --- A4 --------------------------------------------------------------------

def offsets_from_frames(frames, times):
    """Смещение метки от ближайшей предыдущей границы кадра.

    Граница ищется по журналу кадров, а не берётся из записи события: в записи
    лежит граница на момент входа в обработчик, а собственная метка события
    относится к более раннему кадру.
    """
    out = []
    for t in times:
        i = bisect.bisect_right(frames, t) - 1
        if i < 0:
            continue
        out.append(t - frames[i])
    return out


def _a4_verdict(res, scale_name):
    """Контроль метода по move (PLAN §7.2) на одной шкале."""
    down = res.get("pointerdown") or res.get("touchstart")
    move = res.get("pointermove") or res.get("touchmove")
    if down is None or move is None:
        print("  [%s] КОНТРОЛЬ НЕВОЗМОЖЕН: нет либо down, либо move." % scale_name)
        return "no-control"
    if is_band(move) and not is_band(down):
        print("  [%s] контроль пройден: move даёт полосу, down — нет." % scale_name)
        print("        DOWN на этой шкале лежит вне кадровой сетки.")
        return "down-free"
    if is_band(move) and is_band(down):
        # Ровно тот случай, ради которого план требует контроль: полоса у обоих
        # сама по себе ответа не даёт — она может означать, что так устроена
        # точка замера, а не событие.
        print("  [%s] полоса у обоих: сама по себе эта шкала ответа не даёт." % scale_name)
        print("        Либо DOWN квантуется по кадрам, либо по кадрам разнесена")
        print("        точка замера. Решает сравнение шкал ниже.")
        return "both"
    if not is_band(move) and not is_band(down):
        print("  [%s] КОНТРОЛЬ НЕ ПРОЙДЕН: полосы нет ни у move, ни у down." % scale_name)
        print("        Move обязан быть склеен с кадрами — чиним метод, не выводы.")
        return "broken"
    print("  [%s] КОНТРОЛЬ НЕ ПРОЙДЕН: полоса у down, но не у move — так не бывает."
          % scale_name)
    return "broken"


def a4_frame_offsets(session, period):
    head("A4 — смещение события от границы кадра")
    print("  период кадра: %.3f мс" % period)
    frames = sorted(t for t in session.get("frames", []) if isinstance(t, (int, float)))
    groups = [
        ("pointerdown", "pointer", "pointerdown"),
        ("pointermove", "pointer", "pointermove"),
        ("touchstart", "touch", "touchstart"),
        ("touchmove", "touch", "touchmove"),
    ]

    # Две шкалы, и это не избыточность (PLAN §7.2 — контроль метода):
    #   nowMs     — момент входа в обработчик. WebKit разносит доставку ввода по
    #               кадрам, поэтому тут полоса получится у чего угодно: шкала
    #               меряет диспетчеризацию, а не событие.
    #   timeStamp — собственная метка события. По A3 она отстоит от входа в
    #               обработчик заметно дальше гранулярности часов, то есть несёт
    #               собственную информацию; именно она значима для ритм-игры.
    result = {"now": {}, "ts": {}}
    for scale, field, title in (("now", "nowMs", "по nowMs (вход в обработчик)"),
                                ("ts", "timeStamp", "по timeStamp (метка события)")):
        print()
        print("  --- %s ---" % title)
        for name, source, typ in groups:
            evs = events_of(session, source, [typ])
            times = [e.get(field) for e in evs]
            times = [t for t in times if isinstance(t, (int, float))]
            offs = offsets_from_frames(frames, times)
            if not offs:
                continue
            ra = rayleigh(offs, period)
            result[scale][name] = ra
            print("  %-12s %s" % (name, fmt_desc(describe(offs))))
            print("  %-12s %s" % ("", fmt_rayleigh(ra)))
            print(hist(offs, 0.0, period * 2, 20))

    print()
    verdict_now = _a4_verdict(result["now"], "nowMs")
    print()
    verdict_ts = _a4_verdict(result["ts"], "timeStamp")
    print()
    if verdict_now == "both" and verdict_ts == "both":
        print("  ИТОГ: полоса на обеих шкалах — DOWN действительно привязан к кадрам.")
    elif verdict_now == "both" and verdict_ts == "down-free":
        print("  ИТОГ: по входу в обработчик полоса у всего, по собственной метке")
        print("  события — нет. По кадрам разнесена ДОСТАВКА события, а сам момент")
        print("  касания известен точнее кадра — для ритм-игры значим именно он.")
    elif verdict_ts == "down-free":
        print("  ИТОГ: DOWN приходит вне кадровой сетки, разрешение лучше кадра.")
    else:
        print("  ИТОГ: метод не даёт состоятельного ответа — см. замечания выше.")
    return result


# --- контакты: пары DOWN→UP ------------------------------------------------

def contacts(session):
    """Пары DOWN→UP по обоим потокам. Возвращает список словарей."""
    DOWN = {"pointerdown": "pointer", "touchstart": "touch"}
    UP = {"pointerup": "pointer", "touchend": "touch"}
    CANCEL = {"pointercancel": "pointer", "touchcancel": "touch"}
    open_by_key = {}
    out = []
    cancels = []
    for e in session.get("events", []):
        typ = e.get("type")
        src = e.get("source")
        key = (src, e.get("pointerId") if src == "pointer" else e.get("identifier"))
        if typ in DOWN:
            open_by_key[key] = e
        elif typ in UP:
            down = open_by_key.pop(key, None)
            if down is not None:
                out.append({
                    "source": src,
                    "down": down,
                    "up": e,
                    "durationMs": (e.get("nowMs") - down.get("nowMs"))
                    if isinstance(e.get("nowMs"), (int, float))
                    and isinstance(down.get("nowMs"), (int, float)) else None,
                    "durationTsMs": (e.get("timeStamp") - down.get("timeStamp"))
                    if isinstance(e.get("timeStamp"), (int, float))
                    and isinstance(down.get("timeStamp"), (int, float)) else None,
                    "label": down.get("label"),
                    "tapIndex": down.get("tapIndex"),
                })
        elif typ in CANCEL:
            cancels.append(e)
            open_by_key.pop(key, None)
    unpaired = list(open_by_key.values())
    return out, cancels, unpaired


# --- A5, A6 ----------------------------------------------------------------

def a5_duration(session, period, pairs):
    head("A5 — интервал DOWN→UP")
    # Две шкалы по той же причине, что в A4: вход в обработчик разнесён по
    # кадрам, поэтому разность двух входов почти обязана оказаться кратной
    # кадру. Настоящую длительность контакта даёт собственная метка события.
    for field, title in (("durationTsMs", "по timeStamp (метка события)"),
                         ("durationMs", "по nowMs (вход в обработчик)")):
        print()
        print("  --- %s ---" % title)
        for source in ("pointer", "touch"):
            durs = [p.get(field) for p in pairs if p["source"] == source]
            durs = [d for d in durs if isinstance(d, (int, float))]
            if not durs:
                continue
            d = describe(durs)
            print("  %-8s %s" % (source, fmt_desc(d)))
            print(hist(durs, 0.0, max(d["max"], 1.0), 20, label="длительность контакта, мс"))
            ra = rayleigh(durs, period)
            print("  кратность периоду кадра: %s" % fmt_rayleigh(ra))
            if is_band(ra):
                if field == "durationMs":
                    print("  на этой шкале полоса ожидаема и ничего не доказывает.")
                else:
                    print("  вывод: длительность контакта действительно квантуется по кадрам.")
            else:
                print("  вывод: следов кадрового квантования в длительности нет.")


def a6_losses(session, pairs, cancels, unpaired):
    head("A6 — потери и отмены контактов")
    counts = Counter(e.get("type") for e in session.get("events", []))
    for source, down_t, up_t, cancel_t in (("pointer", "pointerdown", "pointerup", "pointercancel"),
                                           ("touch", "touchstart", "touchend", "touchcancel")):
        downs = counts.get(down_t, 0)
        if not downs:
            continue
        ups = counts.get(up_t, 0)
        canc = counts.get(cancel_t, 0)
        lost = sum(1 for e in unpaired if e.get("source") == source)
        print("  %-8s DOWN %d, UP %d, cancel %d, без пары %d  ->  потеряно %.1f%%,"
              " отменено %.1f%%"
              % (source, downs, ups, canc, lost,
                 100.0 * lost / downs, 100.0 * canc / downs))
    if cancels:
        print("  отмены на тапах: %s"
              % ", ".join(str(e.get("tapIndex")) for e in cancels[:20]))


# --- A7 --------------------------------------------------------------------

FIELDS_TOUCH = ["radiusX", "radiusY", "rotationAngle", "force"]
FIELDS_POINTER = ["width", "height", "pressure", "tangentialPressure", "tiltX",
                  "tiltY", "twist", "coalescedCount", "predictedCount"]


def a7_streams(session):
    head("A7 — расхождение потоков Touch и Pointer")
    touch_down = events_of(session, "touch", ["touchstart"])
    pointer_down = events_of(session, "pointer", ["pointerdown"])
    print("  touchstart %d, pointerdown %d" % (len(touch_down), len(pointer_down)))

    if touch_down and pointer_down:
        # Пары ищутся по ближайшей метке nowMs: у одного физического касания
        # два события расходятся на доли миллисекунды.
        deltas_now, deltas_ts, order = [], [], Counter()
        used = set()
        for t in touch_down:
            best, best_d = None, None
            for i, p in enumerate(pointer_down):
                if i in used:
                    continue
                if not isinstance(p.get("nowMs"), (int, float)) or not isinstance(t.get("nowMs"), (int, float)):
                    continue
                d = abs(p["nowMs"] - t["nowMs"])
                if best_d is None or d < best_d:
                    best, best_d = i, d
            if best is None or best_d > 50.0:
                continue
            used.add(best)
            p = pointer_down[best]
            deltas_now.append(p["nowMs"] - t["nowMs"])
            if isinstance(p.get("timeStamp"), (int, float)) and isinstance(t.get("timeStamp"), (int, float)):
                deltas_ts.append(p["timeStamp"] - t["timeStamp"])
            order["pointer раньше" if p["nowMs"] < t["nowMs"] else
                  ("touch раньше" if p["nowMs"] > t["nowMs"] else "одновременно")] += 1
        print("  сопоставлено пар: %d" % len(deltas_now))
        print("  pointerdown - touchstart по nowMs, мс:    %s" % fmt_desc(describe(deltas_now)))
        print("  pointerdown - touchstart по timeStamp, мс: %s" % fmt_desc(describe(deltas_ts)))
        print("  порядок: %s" % dict(order))
    else:
        print("  один из потоков не записан — сравнение по меткам невозможно"
              " (это нормально для контрольной серии).")

    print()
    print("  набор полей: доля непустых и разброс по DOWN-событиям")
    for source, fields, typ in (("touch", FIELDS_TOUCH, "touchstart"),
                                ("pointer", FIELDS_POINTER, "pointerdown")):
        evs = events_of(session, source, [typ])
        if not evs:
            continue
        for f in fields:
            vals = [e.get(f) for e in evs]
            nonnull = [v for v in vals if isinstance(v, (int, float))]
            if not vals:
                continue
            if not nonnull:
                state = "все null"
            elif len(set(nonnull)) == 1:
                state = "константа %s" % nonnull[0]
            else:
                state = "живой: %s … %s (различных %d)" % (min(nonnull), max(nonnull), len(set(nonnull)))
            print("    %-8s %-18s непустых %3d/%-3d  %s"
                  % (source, f, len(nonnull), len(vals), state))


# --- A8 --------------------------------------------------------------------

def _phase_series(downs, beat0, beat_ms):
    """Фаза тапа внутри доли, развёрнутая по ходу серии.

    Привязывать тап к конкретной доле бессмысленно: человек держит темп чуть
    быстрее или медленнее метронома, фаза уезжает, и у границы полудоли любая
    привязка начинает перескакивать на соседнюю долю. Пропущенная или лишняя
    доля ломает привязку по порядку точно так же.

    Поэтому считается фаза по модулю доли и разворачивается по непрерывности:
    уход темпа становится наклоном, пропущенная доля — не разрывом, а шагом,
    который разворачивание поглощает. Разброс попадания — остаток после снятия
    наклона.
    """
    phases = []
    prev = None
    for t in downs:
        ph = (t - beat0) % beat_ms
        if ph > beat_ms / 2:
            ph -= beat_ms
        if prev is not None:
            while ph - prev > beat_ms / 2:
                ph -= beat_ms
            while ph - prev < -beat_ms / 2:
                ph += beat_ms
        phases.append(ph)
        prev = ph
    return phases


def _mad(values):
    """Медианное абсолютное отклонение: разброс, не съедаемый одним выбросом."""
    if not values:
        return None
    med = statistics.median(values)
    return statistics.median([abs(v - med) for v in values])


def _linfit(xs, ys):
    """Наименьшие квадраты: смещение и наклон."""
    n = len(xs)
    mx = statistics.fmean(xs)
    my = statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return my, 0.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return my - b * mx, b


def a8_metronome(session, period, warmup=10):
    head("A8 — попадание по метроному")
    metro = session.get("metronome") or {}
    beats = [b for b in (metro.get("beats") or []) if isinstance(b.get("perfTimeMs"), (int, float))]
    if not beats:
        print("  метронома в сессии нет (или перевод шкал не получился) — критерий"
              " A8 по этой серии не считается.")
        return
    mapping = metro.get("mappingStart") or {}
    print("  BPM %s, доля %s мс, перевод шкал: %s, sampleRate %s"
          % (metro.get("bpm"), metro.get("beatMs"), mapping.get("method"), metro.get("sampleRate")))
    if mapping.get("method") != "getOutputTimestamp":
        print("  [!] перевод сделан парным замером — точность хуже, это учитывается в отчёте")
    if metro.get("clockNeverStarted"):
        print("  [!] аудио-часы не пошли за секунду после resume: перевод шкал грубый,")
        print("      число A8 по этой серии считать нельзя — серию надо переснять")
    end = metro.get("mappingEnd") or {}
    if mapping.get("offsetMs") is not None and end.get("offsetMs") is not None:
        print("  дрейф смещения шкал за серию: %.2f мс"
              % (end["offsetMs"] - mapping["offsetMs"]))
    print("  задержка вывода: baseLatency %s мс, outputLatency %s мс"
          % (mapping.get("baseLatencyMs"), mapping.get("outputLatencyMs")))

    beat_times = sorted(b["perfTimeMs"] for b in beats)
    # Метка события, а не вход в обработчик: по A3 они расходятся почти на два
    # кадра, и эта разница ушла бы прямо в результат.
    downs = [e.get("timeStamp") for e in events_of(session, "pointer", ["pointerdown"])]
    downs = [t for t in downs if isinstance(t, (int, float))]
    if not downs:
        downs = [e.get("timeStamp") for e in events_of(session, "touch", ["touchstart"])]
        downs = [t for t in downs if isinstance(t, (int, float))]
    if not downs:
        print("  нет DOWN-событий — считать нечего")
        return

    beat_ms = metro.get("beatMs") or (beat_times[1] - beat_times[0] if len(beat_times) > 1 else None)
    if not beat_ms:
        print("  длительность доли неизвестна — считать нечего")
        return
    downs = sorted(downs)
    devs = _phase_series(downs, beat_times[0], beat_ms)
    if len(devs) < 3:
        print("  тапов не хватает")
        return
    # Врабатывание. Протокол (docs/protocol.md) предупреждает, что первые
    # щелчки уходят на то, чтобы поймать долю: там фаза ещё едет, и её движение
    # — не разброс попадания, а поиск темпа. Отсечка фиксируется заранее, а не
    # подбирается по результату, и ниже печатается её влияние на число.
    full = devs
    if len(devs) > warmup + 5:
        devs = devs[warmup:]
    else:
        print("  [!] тапов меньше, чем отсечка врабатывания — считаю по всей серии")
        warmup = 0
    idx = list(range(len(devs)))
    a, slope = _linfit(idx, devs)
    resid = [d - (a + slope * i) for i, d in zip(idx, devs)]

    print()
    print("  тапов: %d (отброшено на врабатывание: %d), долей: %d, доля %.1f мс"
          % (len(devs), warmup, len(beat_times), beat_ms))
    print("  фаза тапа внутри доли (развёрнутая), мс: %s" % fmt_desc(describe(devs)))
    print(hist(resid, min(resid), max(resid) + 1e-9, 20,
               label="остаток после снятия тренда, мс"))

    # Темп человека: если он держит долю, наклон около нуля.
    ivs = [b - a2 for a2, b in zip(sorted(downs), sorted(downs)[1:])]
    print()
    print("  интервал между тапами, мс:   %s" % fmt_desc(describe(ivs)))
    print("  уход темпа: %+.2f мс за долю (%.1f мс за серию)"
          % (slope, slope * len(devs)))
    if abs(slope) * len(devs) > period:
        print("  [!] за серию фаза уехала больше чем на кадр: человек держал темп")
        print("      чуть быстрее или медленнее метронома. Это сдвиг, а не разброс,")
        print("      поэтому СКО считается по остатку после снятия тренда.")

    d_raw = describe(devs)
    d_res = describe(resid)
    mad = _mad(resid)
    # Один сбой (двойной тап, пропуск) раздувает СКО; MAD показывает, разброс
    # это или выброс.
    robust_sd = mad * 1.4826 if mad is not None else None
    outliers = [i for i, r in enumerate(resid)
                if robust_sd and abs(r - statistics.median(resid)) > 3 * robust_sd]
    quant = period / math.sqrt(12)
    print()
    print("  СКО отклонения без снятия тренда: %.2f мс" % d_raw["sd"])
    print("  СКО отклонения по остатку:        %.2f мс" % d_res["sd"])
    if robust_sd is not None:
        print("  устойчивая оценка (1.4826*MAD):   %.2f мс  <- рабочее число A8" % robust_sd)
    if outliers:
        print("  выбросов (|остаток| > 3 устойчивых СКО): %d — тапы %s"
              % (len(outliers), ", ".join(str(i) for i in outliers[:10])))
        print("  это сбои исполнения (двойной тап, пропуск доли), а не свойство системы")
    # Среднее развёрнутой фазы накапливает обороты и само по себе смысла не
    # имеет; складывать его с задержкой вывода нельзя. Складываем по модулю
    # доли — и всё равно это сумма задержки звука, задержки ввода и привычки
    # человека бить с опережением. Для вывода значим разброс, не сдвиг.
    off = d_raw["mean"] % beat_ms
    if off > beat_ms / 2:
        off -= beat_ms
    print("  постоянное смещение по фазе:      %+.2f мс — смесь задержки вывода" % off)
    print("                                    и привычки бить с опережением; на разброс не влияет")
    print("  вклад кадрового квантования:      %.2f мс (период/sqrt(12))" % quant)
    # Чувствительность к отсечке: если вывод держится только на одном её
    # значении, он ничего не стоит.
    print()
    print("  зависимость от отсечки врабатывания:")
    print("    отброшено  n   СКО остатка  устойчивая  наклон, мс/долю")
    for cut in (0, 5, 10, 15, 20):
        part = full[cut:]
        if len(part) < 10:
            continue
        ii = list(range(len(part)))
        aa, bb = _linfit(ii, part)
        rr = [v - (aa + bb * i) for i, v in enumerate(part)]
        mm = _mad(rr)
        print("    %8d  %3d  %10.1f  %10.1f  %+10.2f"
              % (cut, len(part), describe(rr)["sd"], (mm * 1.4826) if mm else float("nan"), bb))

    sd_work = robust_sd if robust_sd else d_res["sd"]
    ratio = sd_work / quant if quant else float("nan")
    print("  отношение СКО / вклад кадра:      %.2f" % ratio)
    residual = math.sqrt(max(sd_work ** 2 - quant ** 2, 0.0))
    print("  разброс без вклада кадра:         %.2f мс" % residual)
    if ratio > 2.0:
        print("  вывод: вклад кадрового квантования теряется на фоне человеческого")
        print("  разброса — для ритм-игры кадровая сетка не является ограничивающим")
        print("  фактором.")
    elif ratio > 1.2:
        print("  вывод: вклад квантования заметен, но не доминирует.")
    else:
        print("  вывод: разброс сопоставим с вкладом квантования — кадровая сетка")
        print("  ограничивает точность.")


def write_csv(path, session, name, pairs):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["session", "source", "tapIndex", "label", "downNowMs",
                        "downOffsetFromFrameMs", "durationMs", "durationTsMs", "beatDeviationMs"])
        for p in pairs:
            dur = p["durationMs"]
            w.writerow([name, p["source"], p["tapIndex"], p["label"],
                        p["down"].get("nowMs"), p["down"].get("offsetFromFrameMs"),
                        round(dur, 3) if isinstance(dur, float) else dur,
                        p.get("durationTsMs"),
                        p["down"].get("beatDeviationMs")])


def main(argv=None):
    ap = argparse.ArgumentParser(description="A4–A8 — тайминг DOWN")
    ap.add_argument("sessions", nargs="+", help="JSON сессии из data/")
    ap.add_argument("--csv", help="дописать пары DOWN→UP в CSV")
    ap.add_argument("--warmup", type=int, default=10,
                    help="сколько первых тапов серии по метроному отбросить"
                         " на врабатывание (A8), по умолчанию 10")
    args = ap.parse_args(argv)

    for path in args.sessions:
        name = os.path.basename(path)
        print()
        print("#" * 72)
        print("# %s" % name)
        session = load(path)
        meta = session.get("meta") or {}
        print("#   участник %s, серия %s, протокол %s, режим %s, тапов %s"
              % (meta.get("participant"), meta.get("series"), meta.get("protocol"),
                 meta.get("mode"), meta.get("actualTaps")))
        kind = ((meta.get("env") or {}).get("browserKind"))
        if kind and kind != "safari":
            print("#   [!] СРЕДА НЕ SAFARI (%s): встроенный WebView или сторонний" % kind)
            print("#       браузер — другой процесс и другие ограничения. Серия не")
            print("#       отвечает на вопрос эксперимента, числа в findings не идут.")
        period, fdesc = frame_period(session)
        if fdesc:
            print("#   кадры: %s" % fmt_desc(fdesc))
        pairs, cancels, unpaired = contacts(session)
        if frames_sanity(session, period):
            a4_frame_offsets(session, period)
        else:
            print()
            print("  A4 пропущен: кадров в сессии нет.")
        a5_duration(session, period, pairs)
        a6_losses(session, pairs, cancels, unpaired)
        a7_streams(session)
        a8_metronome(session, period, args.warmup)
        if args.csv:
            write_csv(args.csv, session, name, pairs)
            print("\n  CSV: %s" % args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
