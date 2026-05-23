/**
 * debug/browser.js — перехватчик логов браузера.
 * Автоматически инжектируется сервером в каждый HTML.
 * Отправляет все console.* и ошибки на http://localhost:8765/api/log
 */
(function () {
  var LOG_URL = (window.location.protocol + "//" + window.location.hostname + ":" +
    (window.location.port || "8765") + "/api/log");

  function send(level, message, extra) {
    try {
      var entry = { level: level, message: String(message), source: "browser" };
      if (extra) Object.assign(entry, extra);
      fetch(LOG_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(entry),
        keepalive: true,
      }).catch(function () {});
    } catch (e) {}
  }

  // Перехват console.*
  ["log", "warn", "error", "info", "debug"].forEach(function (method) {
    var original = console[method].bind(console);
    console[method] = function () {
      var args = Array.prototype.slice.call(arguments);
      var msg = args
        .map(function (a) {
          try {
            return typeof a === "object" ? JSON.stringify(a, null, 2) : String(a);
          } catch (e) {
            return String(a);
          }
        })
        .join(" ");
      send(method === "log" ? "info" : method, msg);
      original.apply(console, arguments);
    };
  });

  // Перехват необработанных ошибок JS
  window.addEventListener("error", function (e) {
    send("error", e.message, {
      stack: e.error ? e.error.stack : null,
      file: e.filename,
      line: e.lineno,
      col: e.colno,
    });
  });

  // Перехват необработанных Promise rejections
  window.addEventListener("unhandledrejection", function (e) {
    var msg = e.reason instanceof Error ? e.reason.message : String(e.reason);
    var stack = e.reason instanceof Error ? e.reason.stack : null;
    send("error", "Unhandled Promise: " + msg, { stack: stack });
  });

  // Перехват сетевых ошибок (fetch)
  var origFetch = window.fetch;
  window.fetch = function (url, opts) {
    return origFetch.apply(this, arguments).then(function (resp) {
      if (!resp.ok) {
        send("warn", "Fetch " + resp.status + ": " + url, { status: resp.status });
      }
      return resp;
    }).catch(function (err) {
      send("error", "Fetch failed: " + url + " — " + err.message);
      throw err;
    });
  };

  // Перехват XHR
  var origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__debugUrl = url;
    return origOpen.apply(this, arguments);
  };
  var origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function () {
    var self = this;
    this.addEventListener("load", function () {
      if (self.status >= 400) {
        send("warn", "XHR " + self.status + ": " + self.__debugUrl, { status: self.status });
      }
    });
    this.addEventListener("error", function () {
      send("error", "XHR error: " + self.__debugUrl);
    });
    return origSend.apply(this, arguments);
  };

  send("info", "🐛 Debug logger connected");
})();
