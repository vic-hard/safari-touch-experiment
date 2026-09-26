#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1–M6 — проба канала акселерометра (этап 1).

Зачем канал понадобился: площадь пятна меряет геометрию контакта и поэтому
молчит, когда человек меняет не часть пальца, а силу удара — у троих участников
из пяти отсев резких вышел 0–3% (`docs/findings.md`, «Три чужих iPhone»).
Ускорение меряет сам удар, то есть ровно то, что сказано в инструкции.

Разделы:

    M1  канал: разрешение, частота потока, ровность интервалов
    M2  шкала: сопоставимы ли метки отсчётов со шкалой pointerdown
    M3  покрытие: у скольких тапов в окне 50 мс есть хоть один отсчёт
    M4  хват: телефон в руке или на столе — определяется из данных
    M5  accel50: максимум модуля ускорения в первые 50 мс после DOWN
    M6  контроль метода: то же окно ДО касания и нормировка на фон тапа

Проба считается отдельным скриптом. Признак для правила фильтра (этап 2) —
`tap_features` в конце файла; его вызывает `analyze_features.py`, там же порог и
наборы вместе с площадью и микросдвигом.

    python analysis/analyze_motion.py data/<серии accel-probe>.json

Порядок разделов — не украшение, а зависимость по смыслу. M2 выбирает шкалу, по
которой режется окно: при неверной шкале M5 померяет соседний тап. M3 отвечает,
есть ли в окне отсчёты вообще; пока на это нет ответа, число из M5 описывает
везение попадания в выборку, а не удар.

M6 — обязательный контроль, без него M5 не читается. Градации снимаются блоками
по 10, поэтому «резкий» означает не только удар, но и участок серии: если рука
в резком блоке движется крупнее и между тапами тоже, разделение получится и у
окна, снятого ДО касания, где удара ещё не было. Так и вышло на первой пробе
(23.09.2026): фон в резких блоках поднят вчетверо за 400 мс до тапа. Серия с
чередованием градации по одному тапу этот вопрос закрыла — там дальнее окно
градации не различает, и разделение даёт сам удар.

Нормировка на фон тапа (rise50) печатается, но вывод на ней не строится: при
чередовании в базу мягкого тапа попадает хвост предыдущего резкого, знаменатель
оказывается связан с градацией в обратную сторону, и контраст завышается. Вывод
этапа 1 делается по сырому accel50; знаменатель для правила фильтра выбран на
этапе 2 — фон игрока за 5 с до касания (BG_* ниже).

Только стандартная библиотека.
"""

import argparse
import bisect
import math
import os
import statistics
import sys

import analyze_area as area_mod
# Модулем, а не именами: analyze_features сам импортирует этот файл ради
# признаков ускорения, и при взаимном импорте имён один из двух запусков
# падал бы на недоопределённом модуле.
import analyze_features as features_mod

WINDOW_MS = 50          # то же окно, что у area50 и shift50
CONTROL_WINDOW_MS = 100  # контроль: если в 50 мс отсчётов не хватает, видно здесь
# Окно замаха: у резкого тапа рука разгоняется к стеклу, и в последние 100 мс
# перед касанием это уже видно (серия с чередованием, 23.09.2026: AUC 0.94 при
# 0.28–0.60 дальше назад). Величина полезная — ответ можно дать раньше касания,
# — но как база для нормировки она не годится: у резких тапов она завышена самим
# замахом.
WINDUP_FROM_MS, WINDUP_TO_MS = -100, 0

# База для нормировки: дальнее окно, где ни удара, ни замаха ещё нет. На серии с
# чередованием оно градации не различает (AUC 0.24) — то есть меряет состояние
# телефона, а не предстоящий тап.
BASE_FROM_MS, BASE_TO_MS = -400, -200
# Хвост предыдущего удара в базу попадать не должен: после резкого тапа звон
# слышен ещё сотню миллисекунд.
BASE_GUARD_MS = 150

# Правило решения зафиксировано до съёмки (docs/protocol.md, «Проверка
# разделимости»). Записано здесь, чтобы его нельзя было подвинуть, посмотрев
# на результат.
AUC_GO = 0.85
AUC_MAYBE = 0.70
COVERAGE_GATE = 0.90


def head(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def describe(values, unit=""):
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return "нет данных"
    return ("n=%d  медиана %.3f%s  среднее %.3f  СКО %.3f  [%.3f … %.3f]"
            % (len(vals), statistics.median(vals), unit, statistics.mean(vals),
               statistics.stdev(vals) if len(vals) > 1 else 0.0, min(vals), max(vals)))


def vlen(x, y, z):
    """Модуль вектора; None, если хоть одна составляющая отсутствует."""
    if not all(isinstance(v, (int, float)) for v in (x, y, z)):
        return None
    return math.sqrt(x * x + y * y + z * z)


def samples_of(session):
    motion = session.get("motion") or {}
    return motion, (motion.get("samples") or [])


def sample_time(s, scale):
    return s.get("timeStamp") if scale == "timestamp" else s.get("nowMs")


def event_time(e, scale):
    return e.get("timeStamp") if scale == "timestamp" else e.get("nowMs")


def time_index(samples, scale):
    """Отсчёты, отсортированные по выбранной шкале, и их метки — для bisect.

    Окно у каждого тапа ищется двоичным поиском, а не перебором всей серии: на
    40 тапах разницы не видно, но на серии в несколько минут перебор становится
    квадратичным. Отсчёты без метки в индекс не попадают — перебор их тоже
    пропускал.
    """
    pairs = [(sample_time(s, scale), s) for s in samples]
    pairs = [p for p in pairs if isinstance(p[0], (int, float))]
    pairs.sort(key=lambda p: p[0])
    return [p[0] for p in pairs], [p[1] for p in pairs]


def in_window(index, t0, lo, hi):
    """Отсчёты с меткой в (t0 + lo, t0 + hi] — те же границы, что у перебора."""
    times, ordered = index
    if not isinstance(t0, (int, float)):
        return []
    return ordered[bisect.bisect_right(times, t0 + lo):bisect.bisect_right(times, t0 + hi)]


# --- M1 --------------------------------------------------------------------

def m1_channel(session):
    """Есть ли канал и ровно ли он идёт. Шкала здесь ещё не нужна."""
    motion, samples = samples_of(session)
    head("M1 — канал: разрешение, частота, ровность потока")
    print("  поддержка в браузере: %s" % motion.get("supported"))
    print("  разрешение: %s (%s)" % (motion.get("granted"), motion.get("permissionReason")))
    print("  отсчётов записано: %s, потеряно: %s" % (motion.get("sampleCount"), motion.get("dropped")))
    if not samples:
        print("  [!] отсчётов нет вовсе — дальше считать нечего.")
        print("      Либо разрешение не дано, либо серия снята страницей без motion.js.")
        return None

    deltas = []
    for a, b in zip(samples, samples[1:]):
        ta, tb = a.get("nowMs"), b.get("nowMs")
        if isinstance(ta, (int, float)) and isinstance(tb, (int, float)):
            deltas.append(tb - ta)
    print("  интервал между отсчётами, мс: %s" % describe(deltas))
    if deltas:
        med = statistics.median(deltas)
        if med > 0:
            print("  частота по медиане: %.1f Гц (поле interval: %s)"
                  % (1000.0 / med, samples[0].get("interval")))
        # Ровность потока важнее средней частоты: на рваном потоке отсчёт может
        # не попасть в окно именно у резких тапов, и разделение получится из
        # пропусков, а не из удара.
        big = sum(1 for d in deltas if d > 1.5 * med)
        print("  интервалов длиннее 1.5 медианы: %d из %d (%.0f%%)"
              % (big, len(deltas), 100.0 * big / len(deltas)))
    return samples


# --- M2 --------------------------------------------------------------------

def delivery_lags(samples):
    lags = []
    for s in samples:
        t, n = s.get("timeStamp"), s.get("nowMs")
        if isinstance(t, (int, float)) and isinstance(n, (int, float)):
            lags.append(n - t)
    return lags


def scale_of(samples):
    """Тот же выбор шкалы, что в M2, но без печати — для правила фильтра."""
    lags = delivery_lags(samples)
    if not lags or abs(statistics.median(lags)) > 1000:
        return "now"
    return "timestamp"


def m2_scale(session, samples):
    """По какой шкале резать окно и что на самом деле значит метка отсчёта.

    У касаний метка события отстоит от входа в обработчик на 19–24 мс (A3): её
    ставит источник, раньше доставки, и она несёт собственную информацию. У
    motion возможны три ответа, и различаются они зазором nowMs − timeStamp:

      * зазор меньше тика часов — метка проставлена при доставке. Шкала та же,
        что у касаний, но момент замера метка не несёт: физический отсчёт снят
        раньше, на неизвестную величину до одного периода. Так отвечает Safari
        на iPhone 15 (23.09.2026);
      * зазор порядка A3 — метка источника, как у касаний;
      * зазор в секунды и больше — другое начало отсчёта, шкала несравнима.
    """
    head("M2 — шкала меток: сопоставима ли со шкалой касаний")
    lags = delivery_lags(samples)
    print("  motion, nowMs − timeStamp, мс: %s" % describe(lags))

    ev_lags = [e.get("handlerLagMs") for e in session.get("events", [])
               if e.get("type") == "pointerdown" and isinstance(e.get("handlerLagMs"), (int, float))]
    print("  pointerdown, тот же зазор, мс: %s" % describe(ev_lags))

    if not lags:
        print("  [!] у отсчётов нет собственных меток — остаётся шкала nowMs.")
        return "now"
    med = statistics.median(lags)
    if abs(med) > 1000:
        print("  [!] зазор в %.0f мс — это не задержка доставки, а другое начало отсчёта."
              % med)
        print("      timeStamp у motion в этой шкале несравним с касаниями; беру nowMs.")
        return "now"
    # Гранулярность часов на iPhone — 1 мс (A2): зазор меньше тика значит, что
    # метка и вход в обработчик — один и тот же момент.
    if abs(med) < 1.0:
        print("  вывод: у motion метка — это время ДОСТАВКИ, а не замера: зазор меньше")
        print("  тика часов. Шкала та же, что у касаний (одно начало отсчёта), поэтому")
        print("  окно режется по timeStamp, но собственной информации метка не несёт.")
        print("  Физический отсчёт снят раньше, на неизвестную величину до одного")
        print("  периода потока. Для окна в 50 мс это терпимо, для привязки точнее — нет.")
        return "timestamp"
    print("  вывод: у motion метка источника, как у касаний (зазор %.1f мс), — окно" % med)
    print("  режется по ней, как area50 и shift50.")
    return "timestamp"


# --- M3 --------------------------------------------------------------------

def m3_coverage(index, contacts, scale):
    """Сколько тапов вообще накрыто отсчётами — гейт для всего остального."""
    head("M3 — покрытие окна отсчётами")
    for window in (WINDOW_MS, CONTROL_WINDOW_MS):
        covered = 0
        counts = []
        for c in contacts:
            t0 = event_time(c["events"][0], scale)
            n = len(in_window(index, t0, 0, window))
            counts.append(n)
            if n > 0:
                covered += 1
        if not contacts:
            continue
        share = covered / float(len(contacts))
        print("  окно %3d мс: отсчёт есть у %d из %d тапов (%.0f%%), отсчётов на тап %s"
              % (window, covered, len(contacts), 100.0 * share,
                 describe(counts)))
        if window == WINDOW_MS:
            if share < COVERAGE_GATE:
                print("  [!] покрытие ниже %.0f%% — вопрос о разделении градаций не ставится."
                      % (100 * COVERAGE_GATE))
                print("      При таком покрытии признак у части тапов не измерен вовсе, и")
                print("      любое AUC будет описывать, кому повезло попасть в выборку.")
            else:
                print("  покрытие достаточное: признак измерен почти у всех тапов.")




# --- M4 --------------------------------------------------------------------

def gravity_baseline(samples):
    """Постоянная составляющая ускорения с гравитацией — покомпонентная медиана."""
    out = []
    for key in ("agX", "agY", "agZ"):
        vals = [s.get(key) for s in samples if isinstance(s.get(key), (int, float))]
        out.append(statistics.median(vals) if vals else None)
    return out


def m4_pose(session, index, contacts, scale):
    """Хват — измеренная величина, а не заметка участника.

    В руке телефон дрожит, на столе почти нет; направление гравитации заодно
    говорит про наклон. Числа печатаются, а не сводятся к вердикту по
    абсолютному порогу: порог тут неоткуда взять, а сравнение двух серий между
    собой отвечает на вопрос прямо.
    """
    head("M4 — хват: что видно в данных")
    times, ordered = index
    gx, gy, gz = gravity_baseline(ordered)
    print("  гравитация (медиана agX/agY/agZ): %s"
          % ", ".join("%.3f" % v if v is not None else "null" for v in (gx, gy, gz)))

    # Занятые участки: от 20 мс до касания до 100 мс после отпускания, обе
    # границы включительно. Отсчёты внутри них помечаются по позиции в индексе.
    busy = set()
    for c in contacts:
        t0 = event_time(c["events"][0], scale)
        t1 = event_time(c["events"][-1], scale)
        if isinstance(t0, (int, float)) and isinstance(t1, (int, float)):
            busy.update(range(bisect.bisect_left(times, t0 - 20.0),
                              bisect.bisect_right(times, t1 + 100.0)))

    quiet = []
    for i, s in enumerate(ordered):
        if i in busy:
            continue
        mag = accel_magnitude(s, (gx, gy, gz))
        if mag is not None:
            quiet.append(mag)
    print("  фоновый |a| между тапами, м/с²: %s" % describe(quiet))
    if quiet:
        print("  СКО фона — это и есть мера хвата: в руке дрожь даёт заметный")
        print("  разброс, на столе он падает почти до нуля. Сравнивать нужно")
        print("  серии между собой, абсолютного порога здесь нет.")
    return quiet


def accel_magnitude(s, baseline):
    """Модуль ускорения удара.

    Основной путь — `acceleration` без гравитации. Если поле пустое, гравитация
    вычитается из `accelerationIncludingGravity` по постоянной составляющей: это
    грубее (наклон телефона во время удара тоже попадёт в остаток), но лучше,
    чем молча потерять канал на устройстве, где WebKit отдаёт только одно поле.
    """
    m = vlen(s.get("accX"), s.get("accY"), s.get("accZ"))
    if m is not None:
        return m
    gx, gy, gz = baseline
    if None in (gx, gy, gz):
        return None
    ax, ay, az = s.get("agX"), s.get("agY"), s.get("agZ")
    if not all(isinstance(v, (int, float)) for v in (ax, ay, az)):
        return None
    return math.sqrt((ax - gx) ** 2 + (ay - gy) ** 2 + (az - gz) ** 2)


def gyro_magnitude(s):
    return vlen(s.get("rotA"), s.get("rotB"), s.get("rotG"))


# --- M5 --------------------------------------------------------------------

def feature_rows(session, index, contacts, scale, baseline):
    """accel50 / gyro50 / accel100 / замах / база по контактам — по образцу area50."""
    ordered = sorted(contacts, key=lambda c: (event_time(c["events"][0], scale) or 0))
    rows = []
    prev_down = None
    for c in ordered:
        label = c.get("label")
        t0 = event_time(c["events"][0], scale)
        if label not in ("soft", "sharp"):
            prev_down = t0
            continue
        row = {"label": label, "tapIndex": c.get("tapIndex")}
        # База обрезается так, чтобы не залезть в хвост предыдущего удара. На
        # быстром темпе от неё может не остаться ничего — это не ошибка, а
        # ограничение признака, и оно печатается отдельной колонкой.
        base_lo = BASE_FROM_MS
        if prev_down is not None and isinstance(t0, (int, float)):
            base_lo = max(base_lo, (prev_down + BASE_GUARD_MS) - t0)
        base_window = (base_lo, BASE_TO_MS) if base_lo < BASE_TO_MS else None
        for key, (lo, hi), fn in (("accel50", (0, WINDOW_MS), accel_magnitude),
                                  ("accel100", (0, CONTROL_WINDOW_MS), accel_magnitude),
                                  ("gyro50", (0, WINDOW_MS), gyro_magnitude),
                                  ("windup", (WINDUP_FROM_MS, WINDUP_TO_MS), accel_magnitude),
                                  ("base", base_window or (0, 0), accel_magnitude)):
            if key == "base" and base_window is None:
                row["base"], row["base_n"] = None, 0
                continue
            best, n = None, 0
            for s in in_window(index, t0, lo, hi):
                v = fn(s, baseline) if fn is accel_magnitude else fn(s)
                if v is None:
                    continue
                n += 1
                if best is None or v > best:
                    best = v
            row[key] = best
            row[key + "_n"] = n
        # Отношение удара к состоянию телефона ДО этого тапа: общий уровень
        # движения делится и сокращается, остаётся только то, что дал удар.
        row["rise50"] = (row["accel50"] / row["base"]
                         if row["accel50"] is not None and row["base"] else None)
        rows.append(row)
        prev_down = t0
    return rows


def m5_feature(rows_by_name):
    """Отвечает ли канал на градацию — и хватает ли ответа для этапа 2."""
    head("M5 — accel50: отклик канала на градацию")
    print("  accel50 — максимум модуля ускорения в первые %d мс после DOWN," % WINDOW_MS)
    print("  ровно по образцу area50. Рядом gyro50 (вращение корпуса) и accel100")
    print("  (то же в окне %d мс) — контроль на случай, если отсчётов в 50 мс мало."
          % CONTROL_WINDOW_MS)
    verdicts = {}
    for name, rows in rows_by_name.items():
        print()
        print("  %s" % name)
        soft = [r for r in rows if r["label"] == "soft"]
        sharp = [r for r in rows if r["label"] == "sharp"]
        print("    контактов: мягких %d, резких %d" % (len(soft), len(sharp)))
        print("    %-10s %10s %10s %8s %8s %s"
              % ("признак", "мягкие", "резкие", "AUC", "p", "без отсчётов"))
        for key in ("accel50", "accel100", "gyro50"):
            sv = [r[key] for r in soft if r[key] is not None]
            hv = [r[key] for r in sharp if r[key] is not None]
            empty = sum(1 for r in rows if r[key + "_n"] == 0)
            if not sv or not hv:
                print("    %-10s %10s %10s %8s %8s %d из %d"
                      % (key, "—", "—", "—", "—", empty, len(rows)))
                continue
            a = features_mod.auc(hv, sv)
            vals = [r[key] for r in rows if r[key] is not None]
            labels = [r["label"] for r in rows if r[key] is not None]
            pv = features_mod.auc_p_value(vals, labels, a)
            print("    %-10s %10.3f %10.3f %8.2f %8.3f %d из %d"
                  % (key, statistics.median(sv), statistics.median(hv), a, pv, empty, len(rows)))
            if key == "accel50":
                verdicts[name] = (a, pv)
    return verdicts


# --- M6 --------------------------------------------------------------------

def m6_control(rows_by_name, metas):
    """Контроль метода: отличать удар от участка серии.

    Прямой аналог контроля по `pointermove` в A4. Там полоса у move означала,
    что метод годен; здесь наоборот — разделение в дальнем окне ДО касания
    (base) означает, что сырой accel50 несёт не только удар. Правило чтения:

      * rise50 не разделяет             -> удара в канале нет, всё разделение
        дало общее движение руки;
      * base перевёрнута (AUC ≤ 0.30)   -> у мягких фон выше: при чередовании в
        их базу попадает хвост соседнего резкого. Удар реален, rise50 завышен;
      * base не разделяет               -> контроль пройден, разделение даёт
        сам тап;
      * base разделяет при чередовании  -> телефон между тапами движется
        по-разному, и это уже не блок;
      * base разделяет при блоках       -> «резкий» означает ещё и участок
        серии; разводит только серия с чередованием.

    `metas` — метаданные серий под теми же именами, что в `rows_by_name`: из
    них берётся размер блока, от которого зависит чтение.
    """
    head("M6 — контроль метода: окно ДО касания и нормировка на фон")
    print("  windup — максимум |a| в окне %d…%d мс: замах, удара ещё нет."
          % (WINDUP_FROM_MS, WINDUP_TO_MS))
    print("  base   — то же в окне %d…%d мс: состояние телефона до тапа, без замаха."
          % (BASE_FROM_MS, BASE_TO_MS))
    print("  rise50 — accel50, делённый на base того же тапа: общий уровень движения")
    print("           сокращается, остаётся вклад самого удара.")
    for name, rows in rows_by_name.items():
        print()
        print("  %s" % name)
        lost = sum(1 for r in rows if r["base"] is None)
        if lost:
            print("    [!] у %d тапов из %d базу взять негде: предыдущий тап ближе %d мс."
                  % (lost, len(rows), -BASE_FROM_MS + BASE_GUARD_MS))
            print("        На быстром темпе признак rise50 в этом виде неприменим.")
        print("    %-10s %10s %10s %8s %8s" % ("признак", "мягкие", "резкие", "AUC", "p"))
        res = {}
        for key in ("accel50", "windup", "base", "rise50"):
            sv = [r[key] for r in rows if r["label"] == "soft" and r[key] is not None]
            hv = [r[key] for r in rows if r["label"] == "sharp" and r[key] is not None]
            if not sv or not hv:
                continue
            a = features_mod.auc(hv, sv)
            vals = [r[key] for r in rows if r[key] is not None]
            labels = [r["label"] for r in rows if r[key] is not None]
            res[key] = a
            print("    %-10s %10.3f %10.3f %8.2f %8.3f"
                  % (key, statistics.median(sv), statistics.median(hv), a,
                     features_mod.auc_p_value(vals, labels, a)))
        base_auc, rise, windup = res.get("base"), res.get("rise50"), res.get("windup")
        if base_auc is None or rise is None:
            continue
        blocks = (metas.get(name) or {}).get("blockSize")
        print()
        if rise < AUC_MAYBE:
            print("    [!] удара в канале нет: нормировка убивает разделение (rise50 AUC %.2f)."
                  % rise)
            print("    Всё, что видел accel50, — общее движение, а не касание.")
        elif base_auc <= 1.0 - AUC_MAYBE:
            # Перевёрнутая база — не «контроль пройден»: связь есть, просто
            # обратная, и знаменатель rise50 оказывается связан с градацией.
            print("    [!] база ПЕРЕВЁРНУТА (AUC %.2f): у мягких тапов фон ВЫШЕ, чем у резких."
                  % base_auc)
            if blocks == 1:
                print("    Так и должно быть при чередовании: мягкий тап идёт следом за резким,")
                print("    и в его базу попадает хвост чужого удара.")
            print("    Разделение самого удара это не отменяет — при чередовании его нельзя")
            print("    объяснить ни блоком, ни дрейфом. Но знаменатель rise50 связан с")
            print("    градацией в обратную сторону, и контраст rise50 завышен: вид")
            print("    нормировки — вопрос этапа 2, знаменатель, видимо, нужен общий по серии.")
        elif base_auc < AUC_MAYBE:
            print("    контроль пройден: за %d мс до касания градации не различаются"
                  % -BASE_FROM_MS)
            print("    (AUC базы %.2f), всё разделение даёт сам тап." % base_auc)
        elif blocks == 1:
            print("    [!] база разделяет (AUC %.2f) даже при чередовании по одному тапу —"
                  % base_auc)
            print("    уровнем блока это объяснить нельзя. Значит телефон и между тапами")
            print("    движется по-разному; нормированный rise50 (AUC %.2f) от этого свободен."
                  % rise)
        else:
            print("    [!] база разделяет (AUC %.2f): градации снимались блоками, и «резкий»"
                  % base_auc)
            print("    означает ещё и участок серии. Удар реален (rise50 AUC %.2f), но сырое"
                  % rise)
            print("    число завышено. Разводит серия с чередованием по одному тапу.")
        if windup is not None and windup >= AUC_GO:
            print("    Замах виден до касания (AUC %.2f): решение можно принимать раньше,"
                  % windup)
            print("    чем через 50 мс, — но это уже вопрос этапа 2.")


def verdict(verdicts):
    head("вывод по зафиксированному правилу")
    print("  Правило записано до съёмки: AUC accel50 ≥ %.2f при p < 0.05 хотя бы в одной"
          % AUC_GO)
    print("  позе — этап 2 оправдан; %.2f…%.2f — нужна ещё серия; ниже %.2f — канал"
          % (AUC_MAYBE, AUC_GO, AUC_MAYBE))
    print("  не годится и этапы 2–4 не делаются.")
    print()
    if not verdicts:
        print("  считать нечего: размеченных контактов с отсчётами нет.")
        return
    best_name, (best_auc, best_p) = max(verdicts.items(), key=lambda kv: kv[1][0])
    for name, (a, pv) in verdicts.items():
        print("  %-46s AUC %.2f, p %.3f" % (name, a, pv))
    print()
    if best_auc >= AUC_GO and best_p < 0.05:
        print("  ИДЁМ ДАЛЬШЕ: лучшая серия %s даёт AUC %.2f (p %.3f)."
              % (best_name, best_auc, best_p))
    elif best_auc >= AUC_MAYBE:
        print("  НЕОДНОЗНАЧНО: лучшее AUC %.2f — между %.2f и %.2f. Нужна ещё одна серия,"
              % (best_auc, AUC_MAYBE, AUC_GO))
        print("  решение принимается после неё, а не подгонкой окна или признака.")
    else:
        print("  КАНАЛ НЕ ГОДИТСЯ: лучшее AUC %.2f ниже %.2f. Этапы 2–4 не делаются."
              % (best_auc, AUC_MAYBE))
        print("  Это результат, а не неудача: отрицательный ответ стоил полдня вместо дня.")


# --- этап 2: признак для правила фильтра -----------------------------------
#
# Фон игрока — знаменатель нормировки. Сырое accel50 зависит от хвата (на столе
# все значения в 8 раз меньше) и от человека (медиана мягких у user6 выше самого
# слабого резкого у user5), поэтому единым порогом на пяти сериях с ускорением
# отсеивается 73 резких из 100, а после деления на фон — 96.
#
# Фон берётся по каждому тапу, но не из окна у самого тапа: такое окно
# (base, −400…−200 мс) на чередовании оказалось связано с градацией в обратную
# сторону — в него попадает хвост соседнего резкого, — а на 150 BPM его нет
# вовсе. Здесь фон — нижний квартиль |a| за 5 с до касания, по ВСЕМ отсчётам,
# тапы не вырезаются. Нижний квартиль переживает, даже если звоном ударов
# занята половина отсчётов, поэтому пауз между тапами не требует и годится для
# плотного чарта. Он причинный — игра знает его в момент касания. Последние
# 100 мс не входят: там уже замах, у резких он завышен самим тапом.
#
# Параметры выбраны по пяти сериям 23–24.09.2026 из плато, а не по максимуму:
# окно 2/5/10 с и квантиль 0.10/0.25/0.50 дают от 91 до 97 отсеянных из 100.
BG_FROM_MS, BG_TO_MS = -5000, -100
BG_QUANTILE = 0.25
# Меньше секунды истории — фону верить нельзя, признак не считается (null). В
# игре это первые тапы после старта.
BG_MIN_SAMPLES = 20


def window_max(index, baseline, t0, lo, hi):
    """Максимум |a| в (t0 + lo, t0 + hi] и число отсчётов в окне."""
    best, n = None, 0
    for s in in_window(index, t0, lo, hi):
        v = accel_magnitude(s, baseline)
        if v is None:
            continue
        n += 1
        if best is None or v > best:
            best = v
    return best, n


def background(index, baseline, t0):
    vals = [v for v in (accel_magnitude(s, baseline)
                        for s in in_window(index, t0, BG_FROM_MS, BG_TO_MS))
            if v is not None]
    if len(vals) < BG_MIN_SAMPLES:
        return None
    return features_mod.quantile(vals, BG_QUANTILE)


def tap_features(session, contacts, window_ms):
    """Признаки ускорения по контактам, в порядке `contacts`; None — канала нет.

        accel      максимум |a| в первые window_ms после DOWN, м/с²
        accelbg    accel, делённый на фон игрока (см. BG_*)
        windupbg   максимум |a| в −100…0 мс, делённый на фон: ответ, известный
                   в самый момент касания, без ожидания окна
        bg         сам фон, м/с²

    Метка отсчёта `devicemotion` — время доставки, физический отсчёт раньше на
    величину до периода потока (M2). Поправка не вносится: игра видит отсчёт в
    тот же момент доставки, и окно, отмеренное по доставке, — ровно то, что ей
    доступно. Сдвиг окна на −17 мс на пяти сериях ничего не дал (89 отсеянных
    из 100 против 96).
    """
    _, samples = samples_of(session)
    if not samples:
        return None
    scale = scale_of(samples)
    index = time_index(samples, scale)
    baseline = gravity_baseline(index[1])
    out = []
    for c in contacts:
        t0 = event_time(c["events"][0], scale)
        accel, n = window_max(index, baseline, t0, 0, window_ms)
        windup, _ = window_max(index, baseline, t0, WINDUP_FROM_MS, WINDUP_TO_MS)
        bg = background(index, baseline, t0) if isinstance(t0, (int, float)) else None
        out.append({
            "accel": accel,
            "accel_n": n,
            "bg": bg,
            "accelbg": accel / bg if accel is not None and bg else None,
            "windupbg": windup / bg if windup is not None and bg else None,
        })
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="M1–M6 — проба канала акселерометра")
    ap.add_argument("sessions", nargs="+", help="JSON сессии из data/")
    ap.add_argument("--source", choices=["pointer", "touch"], default="pointer",
                    help="поток для разметки контактов (по умолчанию pointer)")
    ap.add_argument("--scale", choices=["auto", "timestamp", "now"], default="auto",
                    help="шкала времени для окна; auto — решает M2")
    args = ap.parse_args(argv)

    rows_by_name = {}
    metas = {}
    for path in args.sessions:
        session = area_mod.load(path)
        name = os.path.basename(path)
        meta = session.get("meta") or {}
        print()
        print("#" * 72)
        print("# %s" % name)
        print("#   участник %s, серия %s, протокол %s, заметки: %s"
              % (meta.get("participant"), meta.get("series"), meta.get("protocol"),
                 meta.get("notes")))

        contacts = [c for c in area_mod.contacts(session, args.source)
                    if c.get("label") in ("soft", "sharp")]
        motion, samples = samples_of(session)

        # Порядок вызовов повторяет зависимость по смыслу: M1 отвечает, есть ли
        # канал, M2 — по какой шкале резать окно, и только потом M3 считает
        # покрытие, которому шкала уже нужна.
        if m1_channel(session) is None:
            continue
        scale = args.scale
        chosen = m2_scale(session, samples)
        if scale == "auto":
            scale = chosen
        index = time_index(samples, scale)
        m3_coverage(index, contacts, scale)

        baseline = gravity_baseline(samples)
        m4_pose(session, index, contacts, scale)
        metas[name] = meta
        rows_by_name[name] = feature_rows(session, index, contacts, scale, baseline)

    if rows_by_name:
        verdicts = m5_feature(rows_by_name)
        m6_control(rows_by_name, metas)
        verdict(verdicts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
