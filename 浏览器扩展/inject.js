/* 运行在页面主世界（MAIN world）：劫持 fetch 与 XHR，把接口调用吐给 relay。

要点：
1. 有些请求用 `fetch(new Request(url, {...}))` 发起，body 不在 init 里，
   必须 clone Request 再读 text()，否则拿到的请求体永远是空的。
2. FormData / URLSearchParams / Blob 都要单独序列化，直接 JSON.stringify 会丢内容。
3. 只记录「看起来是数据接口」的请求，避免把图片、静态资源也灌进来。
*/
(() => {
  const TAG = "__jingli_capture__";
  const MAX_BODY = 20000;
  // 响应样本原来只留 6000 字符，很多接口的返回（尤其是带分页的列表）
  // 正好在这一截断，导致字段看不全，只能靠猜。放宽到 30K。
  const MAX_RESP = 30000;
  let enabled = true;

  const emit = (payload) => {
    if (!enabled) return;
    try {
      window.postMessage({ tag: TAG, payload }, "*");
    } catch (e) { /* 主世界与隔离世界通信失败不影响页面 */ }
  };

  const clip = (v, max = MAX_BODY) => {
    if (v == null) return null;
    let s;
    if (typeof v === "string") s = v;
    else {
      try { s = JSON.stringify(v); } catch (e) { s = String(v); }
    }
    return s.length > max ? s.slice(0, max) + "…(截断)" : s;
  };

  // 把各种 body 类型统一变成可读字符串
  const serializeBody = (body) => {
    if (body == null) return null;
    try {
      if (typeof body === "string") return clip(body);
      if (body instanceof URLSearchParams) return clip(body.toString());
      if (body instanceof FormData) {
        const parts = [];
        for (const [k, v] of body.entries()) {
          parts.push(`${k}=${typeof v === "string" ? v : "[file]"}`);
        }
        return clip(parts.join("&"));
      }
      if (body instanceof Blob) return `[Blob ${body.size}B ${body.type}]`;
      if (body instanceof ArrayBuffer) return `[ArrayBuffer ${body.byteLength}B]`;
      return clip(body);
    } catch (e) {
      return "[unserializable]";
    }
  };

  const interesting = (url) =>
    /\/api\/|\/v\d+\/|export|report|order|product|finance|settlement|affiliate|creator|ads|download|insight/i.test(url);

  /** 只留可能影响接口判定的头：x- 开头的自定义头（风控、追踪、会话）和 content-type。
   *  Cookie / Authorization 这类敏感的不要——它们由浏览器按域自动带上，不需要重放。 */
  const KEEP_HEADER = /^x-|^content-type$/i;
  function pickHeaders(h) {
    const out = {};
    try {
      if (!h) return out;
      if (typeof h.forEach === "function" && !Array.isArray(h)) {
        h.forEach((v, k) => { if (KEEP_HEADER.test(k)) out[k.toLowerCase()] = String(v).slice(0, 300); });
      } else if (Array.isArray(h)) {
        for (const [k, v] of h) if (KEEP_HEADER.test(k)) out[k.toLowerCase()] = String(v).slice(0, 300);
      } else if (typeof h === "object") {
        for (const k of Object.keys(h)) if (KEEP_HEADER.test(k)) out[k.toLowerCase()] = String(h[k]).slice(0, 300);
      }
    } catch (e) { /* 跨域限制等，忽略 */ }
    return out;
  }

  // ---- fetch ----
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    const isReq = typeof Request !== "undefined" && input instanceof Request;
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const method = (init && init.method) || (isReq ? input.method : "GET") || "GET";
    const started = Date.now();
    let reqHeaders = pickHeaders(init && init.headers);
    if (isReq && Object.keys(reqHeaders).length === 0) reqHeaders = pickHeaders(input.headers);

    // 同步抓一份 body：Request 对象要 clone 后异步读，不能阻塞原请求
    let bodyPromise = Promise.resolve(serializeBody(init && init.body));
    if (isReq && !init?.body && method !== "GET" && method !== "HEAD") {
      try {
        bodyPromise = input.clone().text().then((t) => clip(t)).catch(() => null);
      } catch (e) { /* 已被消费的 Request 无法 clone */ }
    }

    return origFetch.apply(this, arguments).then(async (resp) => {
      if (enabled && interesting(url)) {
        const reqBody = await bodyPromise;
        let sample = null;
        const ct = resp.headers.get("content-type") || "";
        try {
          if (ct.includes("json") || ct.includes("text")) {
            sample = clip(await resp.clone().text(), MAX_RESP);
          }
        } catch (e) { /* 响应体读不出来就算了 */ }
        emit({
          kind: "fetch", url, method, status: resp.status,
          contentType: ct, ms: Date.now() - started,
          reqBody, respSample: sample, reqHeaders,
          page: location.href, at: new Date().toISOString(),
        });
      }
      return resp;
    }).catch((err) => { throw err; });
  };

  // ---- XMLHttpRequest ----
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__cap = { method, url, started: Date.now(), headers: {} };
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    try {
      if (this.__cap && KEEP_HEADER.test(k)) this.__cap.headers[String(k).toLowerCase()] = String(v).slice(0, 300);
    } catch (e) { /* 忽略 */ }
    return origSetHeader.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const cap = this.__cap;
    const reqBody = serializeBody(body);
    if (cap) {
      this.addEventListener("loadend", () => {
        if (!enabled || !interesting(cap.url)) return;
        let sample = null;
        try {
          if (typeof this.responseText === "string") sample = clip(this.responseText, MAX_RESP);
        } catch (e) { /* 二进制响应忽略 */ }
        emit({
          kind: "xhr", url: cap.url, method: cap.method, status: this.status,
          contentType: this.getResponseHeader("content-type"),
          ms: Date.now() - cap.started,
          reqBody, respSample: sample, reqHeaders: cap.headers || {},
          page: location.href, at: new Date().toISOString(),
        });
      });
    }
    return origSend.apply(this, arguments);
  };

  window.addEventListener("message", (ev) => {
    if (ev.source !== window) return;
    if (ev.data && ev.data.tag === TAG && ev.data.cmd === "toggle") {
      enabled = !!ev.data.enabled;
    }
  });
})();
