/* recorder.js — единая запись событий обоих потоков в один журнал.
 * Недоступное поле пишется явным null, не опускается.
 */
(function (global) {
  'use strict';

  var r3 = (global.Clock && global.Clock.r3) || function (x) {
    return (typeof x === 'number' && isFinite(x)) ? Math.round(x * 1000) / 1000 : null;
  };

  function num(v) { return (typeof v === 'number' && isFinite(v)) ? v : null; }
  function numr(v) { return (typeof v === 'number' && isFinite(v)) ? r3(v) : null; }

  var POINTER_TYPES = ['pointerdown', 'pointerup', 'pointermove', 'pointercancel',
                       'pointerover', 'pointerout'];
  var TOUCH_TYPES = ['touchstart', 'touchend', 'touchmove', 'touchcancel'];

  function Recorder(opts) {
    opts = opts || {};
    this.element = opts.element;
    this.mode = opts.mode || 'both';            // both | touch | pointer
    this.frames = opts.frames || null;          // Clock.Frames
    this.metronome = opts.metronome || null;    // Clock.Metronome
    this.recordMove = opts.recordMove !== false; // move нужен как контроль метода
    this.recordOverOut = !!opts.recordOverOut;
    this.preventDefault = !!opts.preventDefault;
    this.maxEvents = opts.maxEvents || 200000;
    this.onEvent = opts.onEvent || null;

    this.events = [];
    this.running = false;
    this.label = null;      // 'soft' | 'sharp' | null — градация для следующего тапа
    this.tapLabel = null;   // градация тапа, идущего прямо сейчас
    this.tapIndex = -1;     // номер физического касания внутри серии
    this.onTapStart = opts.onTapStart || null;
    this.onTapEnd = opts.onTapEnd || null;
    // Одно касание в совмещённом режиме даёт DOWN в обоих потоках, и порядок
    // между ними зависит от движка: Safari шлёт pointerdown РАНЬШЕ touchstart.
    // Поэтому тап считается не по конкретному типу события, а по тому, есть ли
    // сейчас открытый контакт: первый DOWN при нуле открытых начинает тап,
    // последний UP закрывает. Для протокола одиночных тапов этого достаточно;
    // одновременные пальцы такой счётчик объединит в один тап.
    this._openDowns = 0;
    this.dropped = 0;
    this._bound = [];
  }

  /** Смещение события от предыдущей границы кадра. */
  Recorder.prototype._frameInfo = function (now) {
    if (!this.frames) return { frameIndex: null, frameTimeMs: null, offsetFromFrameMs: null };
    var f = this.frames.last();
    if (f.index === null) return { frameIndex: null, frameTimeMs: null, offsetFromFrameMs: null };
    return {
      frameIndex: f.index,
      frameTimeMs: r3(f.t),
      offsetFromFrameMs: r3(now - f.t)
    };
  };

  Recorder.prototype._base = function (ev, source, now) {
    var fi = this._frameInfo(now);
    var rec = {
      seq: this.events.length,
      source: source,
      type: ev.type,
      // собственная метка события против времени входа в обработчик.
      timeStamp: numr(ev.timeStamp),
      nowMs: r3(now),
      handlerLagMs: (typeof ev.timeStamp === 'number' && isFinite(ev.timeStamp))
        ? r3(now - ev.timeStamp) : null,
      frameIndex: fi.frameIndex,
      frameTimeMs: fi.frameTimeMs,
      offsetFromFrameMs: fi.offsetFromFrameMs,
      tapIndex: this.tapIndex,
      // Защёлкнутая метка: смена градации посреди контакта переразметила бы
      // тап, который уже идёт.
      label: this.tapLabel,
      isTrusted: (typeof ev.isTrusted === 'boolean') ? ev.isTrusted : null
    };
    if (this.metronome && this.metronome.beats.length && /down|start/.test(ev.type)) {
      var b = this.metronome.nearestBeat(now);
      rec.beatIndex = b ? b.beatIndex : null;
      rec.beatPerfMs = b ? b.beatPerfMs : null;
      rec.beatDeviationMs = b ? b.deviationMs : null;
    } else {
      rec.beatIndex = null;
      rec.beatPerfMs = null;
      rec.beatDeviationMs = null;
    }
    return rec;
  };

  var DOWN_TYPES = { pointerdown: 1, touchstart: 1 };
  var UP_TYPES = { pointerup: 1, touchend: 1, pointercancel: 1, touchcancel: 1 };

  /** Начало физического касания: первый DOWN при нуле открытых контактов. */
  Recorder.prototype._noteDown = function () {
    if (this._openDowns === 0) {
      this.tapIndex++;
      this.tapLabel = this.label;
      if (this.onTapStart) {
        try { this.onTapStart(this.tapIndex, this.tapLabel); }
        catch (e) { if (global.Log) Log.err('onTapStart: ' + e.message); }
      }
    }
    this._openDowns++;
  };

  /** Конец касания: закрылись оба потока, а не только тот, что пришёл первым. */
  Recorder.prototype._noteUp = function () {
    if (this._openDowns > 0) this._openDowns--;
    if (this._openDowns === 0 && this.onTapEnd) {
      try { this.onTapEnd(this.tapIndex, this.tapLabel); }
      catch (e) { if (global.Log) Log.err('onTapEnd: ' + e.message); }
    }
  };

  Recorder.prototype._push = function (rec) {
    if (this.events.length >= this.maxEvents) { this.dropped++; return; }
    this.events.push(rec);
    if (this.onEvent) {
      try { this.onEvent(rec); } catch (e) { if (global.Log) Log.err('onEvent: ' + e.message); }
    }
  };

  Recorder.prototype._onPointer = function (ev) {
    var now = performance.now();
    if (DOWN_TYPES[ev.type]) this._noteDown();
    if (this.preventDefault && ev.cancelable) ev.preventDefault();

    var rec = this._base(ev, 'pointer', now);
    rec.pointerId = num(ev.pointerId);
    rec.pointerType = ev.pointerType || null;
    rec.isPrimary = (typeof ev.isPrimary === 'boolean') ? ev.isPrimary : null;
    rec.clientX = numr(ev.clientX);
    rec.clientY = numr(ev.clientY);
    rec.pageX = numr(ev.pageX);
    rec.pageY = numr(ev.pageY);
    rec.screenX = numr(ev.screenX);
    rec.screenY = numr(ev.screenY);
    // В1: геометрия пятна на стороне Pointer Events.
    rec.width = numr(ev.width);
    rec.height = numr(ev.height);
    rec.pressure = numr(ev.pressure);
    rec.tangentialPressure = numr(ev.tangentialPressure);
    rec.tiltX = num(ev.tiltX);
    rec.tiltY = num(ev.tiltY);
    rec.twist = num(ev.twist);
    rec.altitudeAngle = num(ev.altitudeAngle);
    rec.azimuthAngle = num(ev.azimuthAngle);
    rec.buttons = num(ev.buttons);
    // Склейка: сколько отсчётов WebKit накопил к кадру.
    var co = null, pr = null, coTimes = null;
    if (typeof ev.getCoalescedEvents === 'function') {
      try {
        var list = ev.getCoalescedEvents();
        co = list.length;
        if (list.length > 1) {
          coTimes = [];
          for (var i = 0; i < list.length; i++) coTimes.push(numr(list[i].timeStamp));
        }
      } catch (e) { co = null; }
    }
    if (typeof ev.getPredictedEvents === 'function') {
      try { pr = ev.getPredictedEvents().length; } catch (e2) { pr = null; }
    }
    rec.coalescedCount = co;
    rec.coalescedTimeStamps = coTimes;
    rec.predictedCount = pr;
    rec.openContacts = this._openDowns;
    // Полей Touch у этого события нет — пишем явные null, чтобы записи обоих
    // потоков имели одинаковую форму.
    rec.identifier = null;
    rec.radiusX = null;
    rec.radiusY = null;
    rec.rotationAngle = null;
    rec.force = null;
    rec.touchesCount = null;
    rec.targetTouchesCount = null;
    rec.changedTouchesCount = null;
    this._push(rec);
    if (UP_TYPES[ev.type]) this._noteUp();
  };

  Recorder.prototype._onTouch = function (ev) {
    var now = performance.now();
    if (this.preventDefault && ev.cancelable) ev.preventDefault();

    var changed = ev.changedTouches || [];
    if (DOWN_TYPES[ev.type]) this._noteDown();
    for (var i = 0; i < changed.length; i++) {
      var t = changed[i];
      var rec = this._base(ev, 'touch', now);
      rec.identifier = num(t.identifier);
      rec.clientX = numr(t.clientX);
      rec.clientY = numr(t.clientY);
      rec.pageX = numr(t.pageX);
      rec.pageY = numr(t.pageY);
      rec.screenX = numr(t.screenX);
      rec.screenY = numr(t.screenY);
      // В1: геометрия пятна на стороне Touch Events.
      rec.radiusX = numr(t.radiusX);
      rec.radiusY = numr(t.radiusY);
      rec.rotationAngle = numr(t.rotationAngle);
      // force на iPhone 15 заведомо 0 (3D Touch убран) — пишем как есть.
      rec.force = numr(t.force);
      rec.touchesCount = ev.touches ? ev.touches.length : null;
      rec.targetTouchesCount = ev.targetTouches ? ev.targetTouches.length : null;
      rec.changedTouchesCount = changed.length;
      rec.touchIndexInChanged = i;
      rec.openContacts = this._openDowns;
      // Полей Pointer у этого события нет.
      rec.pointerId = null;
      rec.pointerType = null;
      rec.isPrimary = null;
      rec.width = null;
      rec.height = null;
      rec.pressure = null;
      rec.tangentialPressure = null;
      rec.tiltX = null;
      rec.tiltY = null;
      rec.twist = null;
      rec.altitudeAngle = null;
      rec.azimuthAngle = null;
      rec.buttons = null;
      rec.coalescedCount = null;
      rec.coalescedTimeStamps = null;
      rec.predictedCount = null;
      this._push(rec);
    }
    if (UP_TYPES[ev.type]) this._noteUp();
  };

  Recorder.prototype._types = function () {
    var pointer = [], touch = [];
    if (this.mode === 'both' || this.mode === 'pointer') {
      pointer = POINTER_TYPES.filter(function (t) {
        if (!this.recordMove && t === 'pointermove') return false;
        if (!this.recordOverOut && (t === 'pointerover' || t === 'pointerout')) return false;
        return true;
      }, this);
    }
    if (this.mode === 'both' || this.mode === 'touch') {
      touch = TOUCH_TYPES.filter(function (t) {
        return this.recordMove || t !== 'touchmove';
      }, this);
    }
    return { pointer: pointer, touch: touch };
  };

  Recorder.prototype.start = function () {
    if (this.running) return this;
    var el = this.element;
    var types = this._types();
    var self = this;
    // passive: false — иначе preventDefault недоступен, а слушатели должны
    // иметь возможность удержать жест (PLAN §10).
    var opts = { passive: false, capture: false };
    types.pointer.forEach(function (type) {
      var fn = function (ev) { self._onPointer(ev); };
      el.addEventListener(type, fn, opts);
      self._bound.push([type, fn]);
    });
    types.touch.forEach(function (type) {
      var fn = function (ev) { self._onTouch(ev); };
      el.addEventListener(type, fn, opts);
      self._bound.push([type, fn]);
    });
    this.running = true;
    this.startedAt = { nowMs: r3(performance.now()), epochMs: Date.now() };
    return this;
  };

  Recorder.prototype.stop = function () {
    var el = this.element;
    this._bound.forEach(function (pair) {
      el.removeEventListener(pair[0], pair[1], { capture: false });
    });
    this._bound = [];
    this.running = false;
    this.stoppedAt = { nowMs: r3(performance.now()), epochMs: Date.now() };
    return this;
  };

  Recorder.prototype.reset = function () {
    this.events = [];
    this.tapIndex = -1;
    this.tapLabel = null;
    this._openDowns = 0;
    this.dropped = 0;
    return this;
  };

  /** Градация для СЛЕДУЮЩЕГО тапа: идущий сейчас контакт не переразмечается. */
  Recorder.prototype.setLabel = function (label) { this.label = label || null; return this; };

  /** Быстрый счётчик для экрана: сколько чего записано. */
  Recorder.prototype.counts = function () {
    var by = Object.create(null);
    this.events.forEach(function (e) { by[e.type] = (by[e.type] || 0) + 1; });
    return by;
  };

  /**
   * Сборка сессии в формате web-probe-0.1.0 (PLAN §9).
   * meta дополняется снаружи (участник, серия, заряд, ориентация).
   */
  Recorder.prototype.session = function (meta, clock) {
    var m = {};
    Object.keys(meta || {}).forEach(function (k) { m[k] = meta[k]; });
    m.mode = this.mode;
    m.preventDefault = this.preventDefault;
    m.recordMove = this.recordMove;
    m.touchAction = this.element ? (global.getComputedStyle(this.element).touchAction || null) : null;
    m.startedAt = this.startedAt || null;
    m.stoppedAt = this.stoppedAt || null;
    m.startedAtIso = new Date((this.startedAt && this.startedAt.epochMs) || Date.now()).toISOString();
    m.droppedEvents = this.dropped;
    m.frameCount = this.frames ? this.frames.times.length : null;
    m.framePeriodMs = this.frames ? this.frames.periodMs() : null;
    // Простои кадров: при них привязка к кадру недостоверна (A4).
    m.frameStalls = this.frames ? (this.frames.stalls || 0) : null;

    return {
      format: 'web-probe-0.1.0',
      meta: m,
      clock: clock || null,
      frames: this.frames ? this.frames.times : [],
      events: this.events,
      metronome: this.metronome ? this.metronome.dump() : null
    };
  };

  global.Recorder = Recorder;
})(window);
