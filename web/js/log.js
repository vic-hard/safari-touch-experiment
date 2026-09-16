/* log.js — лог прямо на странице вместо Web Inspector (PLAN §6).
 * Web Inspector требует macOS, его не будет, поэтому всё, что хотелось бы
 * напечатать в консоль, печатается в DOM. Сюда же перехватываются ошибки:
 * молча упавший обработчик на телефоне выглядит как «ничего не происходит».
 */
(function (global) {
  'use strict';

  var MAX_LINES = 400;
  var box = null;
  var lines = [];

  function stamp() {
    return (global.performance ? performance.now() : 0).toFixed(1).padStart(9, ' ');
  }

  function render() {
    if (!box) return;
    box.textContent = lines.join('\n');
    box.scrollTop = box.scrollHeight;
  }

  function push(text, kind) {
    lines.push((kind ? kind + ' ' : '') + stamp() + '  ' + text);
    if (lines.length > MAX_LINES) lines.splice(0, lines.length - MAX_LINES);
    render();
  }

  var Log = {
    /** Привязать лог к элементу <pre>. */
    attach: function (el) {
      box = typeof el === 'string' ? document.getElementById(el) : el;
      render();
      return Log;
    },
    line: function (text) { push(String(text)); },
    ok: function (text) { push(String(text), '[ok]'); },
    warn: function (text) { push(String(text), '[!]'); },
    err: function (text) { push(String(text), '[ERR]'); },
    /** Объект как набор строк key = value; null печатается явно (PLAN §9). */
    kv: function (obj, prefix) {
      Object.keys(obj).forEach(function (k) {
        var v = obj[k];
        var shown = (v === null) ? 'null'
          : (typeof v === 'object') ? JSON.stringify(v)
          : String(v);
        push((prefix ? prefix + '.' : '') + k + ' = ' + shown);
      });
    },
    /** Красная плашка поверх страницы: то, что нельзя не заметить перед съёмкой. */
    banner: function (text) {
      var el = document.getElementById('ste-banner');
      if (!el) {
        el = document.createElement('div');
        el.id = 'ste-banner';
        el.style.cssText = 'background:#7a2323;border:1px solid #c04a4a;border-radius:10px;' +
          'padding:10px 12px;margin:0 0 10px;font-size:14px;line-height:1.35;color:#ffe9e9';
        document.body.insertBefore(el, document.body.firstChild);
      }
      el.textContent = text;
      return el;
    },
    clear: function () { lines = []; render(); },
    text: function () { return lines.join('\n'); }
  };

  global.addEventListener('error', function (e) {
    Log.err((e.message || 'error') + ' @ ' + (e.filename || '?') + ':' + (e.lineno || '?'));
  });
  global.addEventListener('unhandledrejection', function (e) {
    Log.err('unhandled rejection: ' + (e.reason && e.reason.message ? e.reason.message : e.reason));
  });

  global.Log = Log;
})(window);
