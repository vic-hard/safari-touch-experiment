/* clock.js — калибровка часов, границы кадров, аудио-часы метронома.
 *
 * Калибровка делается ПЕРВОЙ (PLAN §7.1): пока неизвестна гранулярность
 * performance.now(), любой вывод о кадровом квантовании (16.7 мс) несостоятелен.
 * Квантование часов (~1 мс) и квантование по кадрам расходятся в 16 раз, поэтому
 * на гистограмме они различимы — но только если гранулярность измерена, а не
 * предположена.
 */
(function (global) {
  'use strict';

  function r3(x) {
    return (typeof x === 'number' && isFinite(x)) ? Math.round(x * 1000) / 1000 : null;
  }

  /* --- A2: гранулярность performance.now() ------------------------------- */

  /**
   * Busy-loop заданной длительности: собираем различные значения часов и
   * минимальную положительную разность между соседними.
   */
  function calibrate(durationMs) {
    var dur = durationMs || 200;
    var t0 = performance.now();
    var prev = t0;
    var reads = 1;
    var steps = 0;
    var counts = Object.create(null);
    var minDelta = Infinity;
    var now = t0;
    while (now - t0 < dur) {
      now = performance.now();
      reads++;
      if (now > prev) {
        var d = now - prev;
        if (d < minDelta) minDelta = d;
        var key = d.toFixed(6);
        counts[key] = (counts[key] || 0) + 1;
        steps++;
        prev = now;
      }
    }
    // Топ повторяющихся приращений: если часы тикают ровно, тут одно-два значения.
    var top = Object.keys(counts)
      .map(function (k) { return { delta: parseFloat(k), n: counts[k] }; })
      .sort(function (a, b) { return b.n - a.n; })
      .slice(0, 8);

    return {
      granularityMs: isFinite(minDelta) ? r3(minDelta) : null,
      durationMs: r3(now - t0),
      reads: reads,
      distinctSteps: steps,
      topDeltas: top,
      timeOrigin: (typeof performance.timeOrigin === 'number') ? r3(performance.timeOrigin) : null,
      dateNowAtCalibration: Date.now(),
      nowAtCalibration: r3(now)
    };
  }

  /* --- окружение: без него повторный прогон не сопоставить (PLAN §8) ------ */

  /**
   * Настоящий Safari или встроенный браузер приложения.
   *
   * Вопрос эксперимента — что отдаёт WebKit в Safari на iPhone 15. Встроенный
   * WebView мессенджера (WKWebView) — другой процесс с другими ограничениями,
   * и снятая в нём серия отвечает на другой вопрос. У настоящего Safari в
   * userAgent есть и токен Version/, и Safari/; у in-app WebView обычно нет ни
   * того, ни другого. Chrome и Firefox на iOS помечаются отдельно: они тоже
   * WebKit, но это не Safari.
   */
  function browserKind(ua) {
    ua = ua || '';
    var ios = /iPhone|iPad|iPod/.test(ua);
    if (/CriOS|FxiOS|EdgiOS|OPiOS|YaBrowser/.test(ua)) return 'other-ios-browser';
    var looksSafari = /Version\/\d+/.test(ua) && /Safari\//.test(ua);
    if (ios) return looksSafari ? 'safari' : 'in-app-webview';
    return looksSafari ? 'safari' : 'other';
  }

  function env() {
    var s = global.screen || {};
    var nav = global.navigator || {};
    return {
      userAgent: nav.userAgent || null,
      platform: nav.platform || null,
      vendor: nav.vendor || null,
      language: nav.language || null,
      maxTouchPoints: (typeof nav.maxTouchPoints === 'number') ? nav.maxTouchPoints : null,
      hardwareConcurrency: (typeof nav.hardwareConcurrency === 'number') ? nav.hardwareConcurrency : null,
      deviceMemory: (typeof nav.deviceMemory === 'number') ? nav.deviceMemory : null,
      screenWidth: s.width != null ? s.width : null,
      screenHeight: s.height != null ? s.height : null,
      availWidth: s.availWidth != null ? s.availWidth : null,
      availHeight: s.availHeight != null ? s.availHeight : null,
      colorDepth: s.colorDepth != null ? s.colorDepth : null,
      devicePixelRatio: (typeof global.devicePixelRatio === 'number') ? global.devicePixelRatio : null,
      innerWidth: global.innerWidth || null,
      innerHeight: global.innerHeight || null,
      orientation: (s.orientation && s.orientation.type) ? s.orientation.type
        : (typeof global.orientation === 'number' ? String(global.orientation) : null),
      standalone: (nav.standalone != null) ? nav.standalone : null,
      browserKind: browserKind(nav.userAgent),
      hasPointerEvents: typeof global.PointerEvent === 'function',
      hasTouchEvents: 'ontouchstart' in global,
      hasCoalesced: !!(global.PointerEvent && global.PointerEvent.prototype &&
                       'getCoalescedEvents' in global.PointerEvent.prototype),
      hasPredicted: !!(global.PointerEvent && global.PointerEvent.prototype &&
                       'getPredictedEvents' in global.PointerEvent.prototype),
      // Low Power Mode из страницы не читается — заполняется руками в метаданных.
      lowPowerMode: null,
      batteryLevel: null
    };
  }

  /** navigator.getBattery в Safari отсутствует; тогда остаётся явный null. */
  function battery() {
    var nav = global.navigator || {};
    if (typeof nav.getBattery !== 'function') return Promise.resolve(null);
    return nav.getBattery().then(function (b) {
      return { level: b.level, charging: b.charging };
    }).catch(function () { return null; });
  }

  /* --- A4: границы кадров ------------------------------------------------ */

  /**
   * Постоянный цикл requestAnimationFrame, пишущий метку каждого кадра.
   * Метка берётся из аргумента rAF (начало кадра), а не из performance.now()
   * внутри колбэка: нужна именно граница кадра.
   */
  function Frames() {
    this.times = [];
    this.running = false;
    this.stalls = 0;          // сколько раз кадры вставали за серию
    this._watchTimer = null;
    this._id = null;
    this._lastT = null;
    this._lastIndex = -1;
    var self = this;
    this._tick = function (ts) {
      if (!self.running) return;
      var t = (typeof ts === 'number') ? ts : performance.now();
      self.times.push(Math.round(t * 1000) / 1000);
      self._lastT = t;
      self._lastIndex = self.times.length - 1;
      self._id = global.requestAnimationFrame(self._tick);
    };
  }
  Frames.prototype.start = function () {
    if (this.running) return this;
    this.running = true;
    this._id = global.requestAnimationFrame(this._tick);
    return this;
  };
  Frames.prototype.stop = function () {
    this.running = false;
    if (this._id != null) global.cancelAnimationFrame(this._id);
    this._id = null;
    return this;
  };
  Frames.prototype.reset = function () {
    this.times = [];
    this.stalls = 0;
    this._lastT = null;
    this._lastIndex = -1;
    return this;
  };
  /** Последняя записанная граница кадра на момент вызова из обработчика. */
  Frames.prototype.last = function () {
    if (this._lastIndex < 0) return { index: null, t: null };
    return { index: this._lastIndex, t: this._lastT };
  };
  /**
   * Сторож кадров. Если rAF перестал тикать — вкладка ушла в фон, экран погас,
   * система придушила страницу — привязка событий к кадрам ломается молча:
   * offsetFromFrameMs становится null или считается от давно устаревшей метки.
   * Заметить это надо во время съёмки, а не при анализе, когда серия уже снята.
   */
  Frames.prototype.watch = function (onStall, thresholdMs, periodMs) {
    var self = this;
    var threshold = thresholdMs || 500;
    this.unwatch();
    this._seen = this.times.length;
    this._watchTimer = global.setInterval(function () {
      if (!self.running) return;
      if (self.times.length === self._seen) {
        self.stalls = (self.stalls || 0) + 1;
        if (onStall) onStall(threshold, self.stalls);
      }
      self._seen = self.times.length;
    }, periodMs || threshold);
    return this;
  };
  Frames.prototype.unwatch = function () {
    if (this._watchTimer != null) global.clearInterval(this._watchTimer);
    this._watchTimer = null;
    return this;
  };

  /** Оценка периода кадра по медиане разностей — контроль частоты обновления. */
  Frames.prototype.periodMs = function () {
    var n = this.times.length;
    if (n < 3) return null;
    var d = [];
    for (var i = 1; i < n; i++) d.push(this.times[i] - this.times[i - 1]);
    d.sort(function (a, b) { return a - b; });
    return r3(d[Math.floor(d.length / 2)]);
  };

  /* --- A8: аудио-часы метронома (PLAN §7.4) ------------------------------ */

  /**
   * Щелчки планируются по AudioContext.currentTime — собственным часам
   * звукового движка, не зависящим от кадров. Перевод в шкалу performance.now()
   * идёт через getOutputTimestamp(); если его нет — через парный замер, и это
   * помечается как менее точный способ.
   */
  function Metronome(opts) {
    opts = opts || {};
    this.bpm = opts.bpm || 100;
    this.beatMs = 60000 / this.bpm;
    this.lookaheadMs = 100;     // как часто просыпается планировщик
    this.scheduleAheadS = 0.3;  // насколько вперёд ставятся щелчки
    this.beats = [];            // { index, audioTimeMs, perfTimeMs }
    this.ctx = null;
    this.running = false;
    this.mapping = null;
    this.mappingEnd = null;
    this._timer = null;
    this._nextBeat = 0;
    this._nextTime = 0;
  }

  /** contextTime -> performance.now(): смещение шкал и как оно получено. */
  Metronome.prototype.measureMapping = function () {
    var ctx = this.ctx;
    if (!ctx) return null;
    if (typeof ctx.getOutputTimestamp === 'function') {
      var ts = ctx.getOutputTimestamp();
      if (ts && typeof ts.contextTime === 'number' && typeof ts.performanceTime === 'number'
          && ts.contextTime > 0) {
        return {
          method: 'getOutputTimestamp',
          offsetMs: r3(ts.performanceTime - ts.contextTime * 1000),
          contextTimeMs: r3(ts.contextTime * 1000),
          performanceTimeMs: r3(ts.performanceTime),
          baseLatencyMs: (typeof ctx.baseLatency === 'number') ? r3(ctx.baseLatency * 1000) : null,
          outputLatencyMs: (typeof ctx.outputLatency === 'number') ? r3(ctx.outputLatency * 1000) : null
        };
      }
    }
    // Запасной способ: один парный замер, точность хуже — помечается явно.
    var p = performance.now();
    var c = ctx.currentTime * 1000;
    return {
      method: 'currentTime-pair',
      offsetMs: r3(p - c),
      contextTimeMs: r3(c),
      performanceTimeMs: r3(p),
      baseLatencyMs: (typeof ctx.baseLatency === 'number') ? r3(ctx.baseLatency * 1000) : null,
      outputLatencyMs: (typeof ctx.outputLatency === 'number') ? r3(ctx.outputLatency * 1000) : null
    };
  };

  /**
   * Сразу после resume() аудио-часы стоят на нуле, и getOutputTimestamp в этот
   * момент пары не даёт. Померив соответствие шкал тут же, мы зря уходим в
   * запасной способ и портим A8. Поэтому ждём, пока часы поедут.
   */
  Metronome.prototype._waitForClock = function () {
    var ctx = this.ctx;
    var tries = 0;
    return new Promise(function (resolve) {
      (function poll() {
        var ts = (typeof ctx.getOutputTimestamp === 'function') ? ctx.getOutputTimestamp() : null;
        var ready = (ts && typeof ts.contextTime === 'number' && ts.contextTime > 0) ||
                    ctx.currentTime > 0;
        if (ready || tries++ > 20) return resolve(ready);
        global.setTimeout(poll, 50);
      })();
    });
  };

  Metronome.prototype.toPerf = function (audioTimeS) {
    if (!this.mapping) return null;
    return r3(audioTimeS * 1000 + this.mapping.offsetMs);
  };

  Metronome.prototype._click = function (when) {
    var ctx = this.ctx;
    var osc = ctx.createOscillator();
    var gain = ctx.createGain();
    osc.type = 'square';
    osc.frequency.value = 1500;
    gain.gain.setValueAtTime(0.0001, when);
    gain.gain.exponentialRampToValueAtTime(0.5, when + 0.001);
    gain.gain.exponentialRampToValueAtTime(0.0001, when + 0.03);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(when);
    osc.stop(when + 0.04);
  };

  Metronome.prototype._schedule = function () {
    var ctx = this.ctx;
    while (this._nextTime < ctx.currentTime + this.scheduleAheadS) {
      this._click(this._nextTime);
      this.beats.push({
        index: this._nextBeat,
        audioTimeMs: r3(this._nextTime * 1000),
        perfTimeMs: this.toPerf(this._nextTime)
      });
      this._nextBeat++;
      this._nextTime += this.beatMs / 1000;
    }
  };

  /**
   * Разблокировка звука — синхронно, прямо в обработчике жеста. Нужна, когда
   * между жестом и стартом стоит промис: запрос разрешения на датчики движения
   * показывает системный диалог, и к его ответу жест уже истёк — resume(),
   * вызванный оттуда, iOS молча игнорирует, и метроном не звучит.
   */
  Metronome.prototype.unlock = function () {
    var AC = global.AudioContext || global.webkitAudioContext;
    if (!AC) return false;
    if (!this.ctx) this.ctx = new AC();
    this.ctx.resume();
    return true;
  };

  /** Старт из жеста пользователя или после unlock(): иначе iOS звук не даст. */
  Metronome.prototype.start = function () {
    var AC = global.AudioContext || global.webkitAudioContext;
    if (!AC) return Promise.reject(new Error('нет Web Audio'));
    if (!this.ctx) this.ctx = new AC();
    var self = this;
    return this.ctx.resume().then(function () {
      return self._waitForClock();
    }).then(function (ready) {
      if (!ready) {
        // Часы так и не поехали за секунду: перевод шкал будет грубым, и это
        // должно быть видно в отчёте, а не молча испортить A8.
        self.clockNeverStarted = true;
      }
      self.mapping = self.measureMapping();
      self.running = true;
      self.beats = [];
      self._nextBeat = 0;
      self._nextTime = self.ctx.currentTime + 0.5;  // полсекунды на раскачку
      self._schedule();
      self._timer = global.setInterval(function () {
        if (self.running) self._schedule();
      }, self.lookaheadMs);
      return self.mapping;
    });
  };

  Metronome.prototype.stop = function () {
    this.running = false;
    if (this._timer != null) global.clearInterval(this._timer);
    this._timer = null;
    // Соответствие шкал замеряется ещё раз в конце: дрейф аудио-часов
    // относительно performance.now() виден только по двум точкам.
    this.mappingEnd = this.measureMapping();
    return this;
  };

  /** Ближайшая доля к метке DOWN и отклонение от неё (знак сохраняется). */
  Metronome.prototype.nearestBeat = function (perfMs) {
    var best = null;
    for (var i = 0; i < this.beats.length; i++) {
      var b = this.beats[i];
      if (b.perfTimeMs == null) continue;
      var dev = perfMs - b.perfTimeMs;
      if (best === null || Math.abs(dev) < Math.abs(best.deviationMs)) {
        best = { beatIndex: b.index, beatPerfMs: b.perfTimeMs, deviationMs: r3(dev) };
      }
    }
    return best;
  };

  Metronome.prototype.dump = function () {
    return {
      bpm: this.bpm,
      beatMs: r3(this.beatMs),
      mappingStart: this.mapping,
      mappingEnd: this.mappingEnd,
      clockNeverStarted: !!this.clockNeverStarted,
      sampleRate: this.ctx ? this.ctx.sampleRate : null,
      beats: this.beats
    };
  };

  global.Clock = {
    calibrate: calibrate,
    env: env,
    browserKind: browserKind,
    battery: battery,
    Frames: Frames,
    Metronome: Metronome,
    r3: r3
  };
})(window);
