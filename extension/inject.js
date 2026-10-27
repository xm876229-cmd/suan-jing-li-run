/* 运行在页面主世界（MAIN world）：劫持 fetch 与 XHR，把接口调用吐给 relay。

只在「记录模式」开启时工作，避免平时无谓地占内存。
记录的是接口元信息与响应样本，不是完整数据流 —— 目标是搞清楚"导出按钮背后调了哪个接口"。
*/
(() => {
  const TAG = "__jingli_capture__";
  const MAX_BODY = 4000;
  let enabled = false;

  const emit = (payload) => {
    if (!enabled) return;
    try {
      window.postMessage({ tag: TAG, payload }, "*");
    } catch (e) { /* 忽略：主世界与隔离世界通信失败不影响页面 */ }
  };

  const clip = (v) => {
    if (v == null) return null;
    let s;
    if (typeof v === "string") s = v;
    else {
      try { s = JSON.stringify(v); } catch (e) { s = String(v); }
    }
    return s.length > MAX_BODY ? s.slice(0, MAX_BODY) + "…(截断)" : s;
  };

  const interesting = (url) =>
    /\/api\/|\/v\d+\/|export|report|order|product|finance|settlement|affiliate|creator|ads|download/i.test(url);

  // ---- fetch ----
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const method = (init && init.method) || (input && input.method) || "GET";
    const started = Date.now();
    return origFetch.apply(this, arguments).then(async (resp) => {
      if (enabled && interesting(url)) {
        let sample = null;
        const ct = resp.headers.get("content-type") || "";
        try {
          if (ct.includes("json")) {
            const clone = resp.clone();
            sample = clip(await clone.text());
          }
        } catch (e) { /* 响应体读不出来就算了 */ }
        emit({
          kind: "fetch", url, method, status: resp.status,
          contentType: ct, ms: Date.now() - started,
          reqBody: clip(init && init.body), respSample: sample,
          page: location.href, at: new Date().toISOString(),
        });
      }
      return resp;
    }).catch((err) => { throw err; });
  };

  // ---- XMLHttpRequest ----
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__cap = { method, url, started: Date.now() };
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const cap = this.__cap;
    if (cap) {
      this.addEventListener("loadend", () => {
        if (!enabled || !interesting(cap.url)) return;
        let sample = null;
        try {
          if (typeof this.responseText === "string") sample = clip(this.responseText);
        } catch (e) { /* 二进制响应忽略 */ }
        emit({
          kind: "xhr", url: cap.url, method: cap.method, status: this.status,
          contentType: this.getResponseHeader("content-type"),
          ms: Date.now() - cap.started,
          reqBody: clip(body), respSample: sample,
          page: location.href, at: new Date().toISOString(),
        });
      });
    }
    return origSend.apply(this, arguments);
  };

  // ---- 开关 ----
  window.addEventListener("message", (ev) => {
    if (ev.source !== window) return;
    if (ev.data && ev.data.tag === TAG && ev.data.cmd === "toggle") {
      enabled = !!ev.data.enabled;
    }
  });

  // 默认开启：装了就是为了抓
  enabled = true;
})();
