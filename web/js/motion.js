/* motion.js — запись акселерометра и гироскопа рядом с журналом касаний.
 *
 *
 * Чего от канала ждать. Safari отдаёт `devicemotion` примерно на 60 Гц, и
 * поднять частоту нечем: Generic Sensor API в Safari нет, у самого события
 * частота не настраивается. В окно 50 мс после касания попадает два-три
 * отсчёта, то есть сам удар (единицы миллисекунд) мы, скорее всего, промахнём
 * и ловить будем затухание корпуса после него. Годится канал или нет - решает
 * `analysis/analyze_motion.py` по снятым данным, а не эта запись.
 */
(function (global) {
  'use strict';

  var r3 = (global.Clock && global.Clock.r3) || function (x) {
    return (typeof x === 'number' && isFinite(x)) ? Math.round(x * 1000) / 1000 : null;
  };

  function numr(v) { return (typeof v === 'number' && isFinite(v)) ? r3(v) : null; }

  /** Есть ли событие в этом браузере вообще. */
  function supported() {
    return typeof global.DeviceMotionEvent !== 'undefined';
  }

  /**
   * Запрос разрешения. На iOS 13+ обязателен и обязан быть вызван ИЗ ЖЕСТА
   * пользователя, иначе iOS молча отказывает; там же он требует https.
   * На остальных платформах метода нет — считаем, что разрешение есть.
   */
  function request() {
    if (!supported()) {
      return Promise.resolve({ granted: false, reason: 'DeviceMotionEvent не поддерживается' });
    }
    var ask = global.DeviceMotionEvent.requestPermission;
    if (typeof ask !== 'function') {
      return Promise.resolve({ granted: true, reason: 'разрешение не требуется' });
    }
    try {
      return ask.call(global.DeviceMotionEvent).then(function (state) {
        return { granted: state === 'granted', reason: 'ответ системы: ' + state };
      }).catch(function (e) {
        return { granted: false, reason: 'запрос отклонён: ' + (e && e.message) };
      });
    } catch (e) {
      return Promise.resolve({ granted: false, reason: 'запрос не выполнен: ' + e.message });
    }
  }

  function Motion(opts) {
    opts = opts || {};
    this.maxSamples = opts.maxSamples || 200000;
    this.samples = [];
    this.running = false;
    this.granted = null;        // null — не спрашивали; true/false — ответ системы
    this.permissionReason = null;
    this.dropped = 0;
    this._handler = null;
  }

  Motion.prototype.reset = function () {
    this.samples = [];
    this.dropped = 0;
    return this;
  };

  /**
   * Подписка на поток. Слушаем всю серию, включая промежутки между тапами:
   * фоновый шум между ударами - это и есть способ отличить телефон в руке от
   * телефона на столе, не спрашивая участника (в руке тремор, на столе почти
   * ноль). Хват тогда становится измеренной величиной, а не заметкой.
   */
  Motion.prototype.start = function () {
    if (this.running || !supported()) return this;
    var self = this;
    this._handler = function (ev) {
      var now = performance.now();
      if (self.samples.length >= self.maxSamples) { self.dropped++; return; }
      var a = ev.acceleration || null;
      var ag = ev.accelerationIncludingGravity || null;
      var rr = ev.rotationRate || null;
      self.samples.push({
        nowMs: r3(now),
        timeStamp: numr(ev.timeStamp),
        interval: numr(ev.interval),
        // Ускорение без гравитации: именно оно описывает удар. На части
        // устройств поле пустое — тогда его заменяет agX/agY/agZ за вычетом
        // постоянной составляющей, но делает это уже анализ, не запись.
        accX: a ? numr(a.x) : null,
        accY: a ? numr(a.y) : null,
        accZ: a ? numr(a.z) : null,
        // С гравитацией: даёт наклон аппарата, по нему определяется хват.
        agX: ag ? numr(ag.x) : null,
        agY: ag ? numr(ag.y) : null,
        agZ: ag ? numr(ag.z) : null,
        // Вращение: тап мимо центра корпуса закручивает телефон, и в руке это
        // может оказаться заметнее линейного ускорения.
        rotA: rr ? numr(rr.alpha) : null,
        rotB: rr ? numr(rr.beta) : null,
        rotG: rr ? numr(rr.gamma) : null
      });
    };
    global.addEventListener('devicemotion', this._handler, false);
    this.running = true;
    return this;
  };

  Motion.prototype.stop = function () {
    if (this._handler) global.removeEventListener('devicemotion', this._handler, false);
    this._handler = null;
    this.running = false;
    return this;
  };

  /** Частота потока по медиане разностей — тем же способом, что период кадра. */
  Motion.prototype.rateHz = function () {
    var n = this.samples.length;
    if (n < 3) return null;
    var d = [];
    for (var i = 1; i < n; i++) d.push(this.samples[i].nowMs - this.samples[i - 1].nowMs);
    d.sort(function (a, b) { return a - b; });
    var med = d[Math.floor(d.length / 2)];
    return (med > 0) ? r3(1000 / med) : null;
  };

  Motion.prototype.dump = function () {
    return {
      granted: this.granted,
      permissionReason: this.permissionReason,
      supported: supported(),
      sampleCount: this.samples.length,
      rateHz: this.rateHz(),
      dropped: this.dropped,
      samples: this.samples
    };
  };

  Motion.supported = supported;
  Motion.request = request;
  global.Motion = Motion;
})(window);
