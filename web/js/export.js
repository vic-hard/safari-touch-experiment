/* export.js — выгрузка сессии двумя путями сразу (PLAN §6).
 *
 * Основной путь: POST на свой же сервер через тот же туннель — один origin,
 * никакого mixed content, JSON падает в data/.
 * Страховка: скачивание файлом в Files app на случай обрыва сети посреди серии.
 * Терять снятую серию из-за Wi-Fi нельзя: повторить её в тех же условиях
 * невозможно.
 */
(function (global) {
  'use strict';

  function log(kind, text) {
    if (global.Log && Log[kind]) Log[kind](text);
  }

  function filename(session) {
    var meta = (session && session.meta) || {};
    function part(v, d) {
      return String(v == null ? d : v).replace(/[^A-Za-z0-9._-]+/g, '-').slice(0, 40) || d;
    }
    var iso = (meta.startedAtIso || new Date().toISOString()).replace(/[:.]/g, '-').slice(0, 19);
    return [iso, part(meta.page, 'page'), part(meta.participant, 'p'),
            's' + part(meta.series, '0'), part(meta.mode, 'both')].join('-') + '.json';
  }

  function toJson(session) {
    return JSON.stringify(session);
  }

  /** POST /upload на свой сервер. Таймаут обязателен: висящий запрос
   *  задерживает второй путь выгрузки, а серию терять нельзя. */
  function post(session, timeoutMs) {
    var body = toJson(session);
    var ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, timeoutMs || 20000) : null;
    return fetch('/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body,
      cache: 'no-store',
      signal: ctrl ? ctrl.signal : undefined
    }).then(function (res) {
      if (timer) clearTimeout(timer);
      return res;
    }, function (e) {
      if (timer) clearTimeout(timer);
      throw e;
    }).then(function (res) {
      return res.json().catch(function () { return { ok: res.ok }; })
        .then(function (data) {
          if (!res.ok || !data.ok) throw new Error('сервер: ' + res.status + ' ' + JSON.stringify(data));
          return data;
        });
    });
  }

  /** Скачивание файлом: в Files app через Blob + <a download>. */
  function download(session) {
    var name = filename(session);
    var blob = new Blob([toJson(session)], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.rel = 'noopener';
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    global.setTimeout(function () {
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }, 4000);
    return name;
  }

  /**
   * Оба пути, строго по очереди: сначала POST, потом скачивание файлом.
   *
   * Порядок важен. В iOS Safari старт скачивания blob-а обрывает незавершённые
   * запросы страницы, и POST падает с «Load failed» — ровно это и случилось на
   * первой съёмке. Поэтому сеть отрабатывает первой, а файл сохраняется в любом
   * случае, прошёл POST или нет: серию терять нельзя.
   */
  function save(session) {
    function saveFile() {
      try {
        var name = download(session);
        log('ok', 'файл: ' + name);
        return name;
      } catch (e) {
        log('err', 'скачивание не удалось: ' + e.message);
        return null;
      }
    }
    return post(session).then(function (data) {
      log('ok', 'POST: ' + data.file + ' (' + data.bytes + ' B, events=' + data.events + ')');
      return { file: saveFile(), server: data };
    }).catch(function (e) {
      var reason = (e && e.name === 'AbortError') ? 'таймаут' : (e.message || e);
      log('err', 'POST не прошёл: ' + reason);
      var name = saveFile();
      log('warn', 'серия сохранена файлом — перенеси её в data/ руками');
      return { file: name, server: null, error: String(reason) };
    });
  }

  global.Export = {
    save: save,
    post: post,
    download: download,
    filename: filename
  };
})(window);
