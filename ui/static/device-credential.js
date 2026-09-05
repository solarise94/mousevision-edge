/* 设备凭证管理（B5，合同 §6.2 / §7 / §15-B5）。
 *
 * 云版（非 local edition）H5 的设备身份：
 *   - 首次使用：扫绑定码（POST /api/control/devices/bind）或子账号登录
 *     （POST /api/control/devices/login）换取设备凭证；
 *   - 凭证 {token, tenant_id, tenant_name, device_id, device_label} 持久化在
 *     localStorage（键 mv.deviceCredential.v1），每次 API 请求经 api-client.js
 *     以 X-MouseVision-Token 头携带（服务端先查设备表）；
 *   - 凭证固定绑定一个工作区（服务端不可改绑）；换工作区 / 凭证被撤销时
 *     clear() 后重新走绑定流程。
 *
 * outbox 联动（§7.3-7.6）由 mobile.js 完成：凭证决定 report-client 的
 * v2 键（mv.reportOutbox.v2.<tenant_id>）或 legacy v1 键（仅绑定
 * legacy-default 时）。零依赖、UMD，风格与 api-client.js 一致。
 */
(function (root) {
  "use strict";

  var STORAGE_KEY = "mv.deviceCredential.v1";

  function load() {
    try {
      var g = root || (typeof window !== "undefined" ? window : null);
      var ls = g && g.localStorage;
      if (!ls || typeof ls.getItem !== "function") return null;
      var raw = ls.getItem(STORAGE_KEY);
      if (!raw) return null;
      var obj = JSON.parse(raw);
      if (obj && typeof obj === "object"
        && typeof obj.token === "string" && obj.token
        && typeof obj.tenant_id === "string" && obj.tenant_id) {
        return obj;
      }
    } catch (_) {}
    return null;
  }

  function save(credential) {
    var g = root || (typeof window !== "undefined" ? window : null);
    var ls = g && g.localStorage;
    if (!ls) throw new Error("localStorage 不可用");
    ls.setItem(STORAGE_KEY, JSON.stringify({
      token: String(credential.token || ""),
      tenant_id: String(credential.tenant_id || ""),
      tenant_name: String(credential.tenant_name || ""),
      device_id: String(credential.device_id || ""),
      device_label: String(credential.device_label || ""),
      bound_at: new Date().toISOString(),
    }));
  }

  function clear() {
    try {
      var g = root || (typeof window !== "undefined" ? window : null);
      var ls = g && g.localStorage;
      if (ls && typeof ls.removeItem === "function") ls.removeItem(STORAGE_KEY);
    } catch (_) {}
  }

  /* 服务端签发响应（bind / login）→ 持久化凭证。返回存储后的对象。 */
  function saveFromResponse(data) {
    if (!data || !data.token || !data.tenant_id) {
      throw new Error("签发响应缺少 token/tenant_id");
    }
    save(data);
    return load();
  }

  /* ---------- 网络（依赖 api-client.js 的 apiFetch） ---------- */
  function postJson(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(async function (res) {
      var data = null;
      try { data = await res.json(); } catch (_) {}
      return { status: res.status, ok: res.ok, data: data };
    });
  }

  /* 绑定码换凭证。成功返回存储后的凭证；失败抛 Error(message)。 */
  function bindWithCode(code, deviceLabel) {
    if (typeof apiFetch === "function") {
      return apiFetch("/api/control/devices/bind", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: (code || "").trim(), device_label: deviceLabel || "" }),
      }).then(function (res) { return handleResponse(res); });
    }
    return postJson("/api/control/devices/bind", { code: code, device_label: deviceLabel })
      .then(function (r) { return handleResponse(r); });
  }

  /* 子账号登录换凭证。多工作区账号（HTTP 400 + detail.tenants）时抛出带
   * tenants 列表的错误（err.code = "pick-tenant"），由 UI 呈现工作区选择。 */
  function login(username, password, tenantId, deviceLabel) {
    var body = {
      username: (username || "").trim(),
      password: password || "",
      device_label: deviceLabel || "",
    };
    if (tenantId) body.tenant_id = tenantId;
    return apiFetch("/api/control/devices/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (res) { return handleResponse(res); });
  }

  function handleResponse(res) {
    return (res.json ? res.json().catch(function () { return null; }) : Promise.resolve(res.data))
      .then(function (data) {
        var status = res.status;
        if (status >= 200 && status < 300 && data && data.token) {
          return saveFromResponse(data);
        }
        if (status === 400 && data && data.detail && typeof data.detail === "object" && Array.isArray(data.detail.tenants)) {
          var err = new Error(data.detail.message || "请选择工作区");
          err.code = "pick-tenant";
          err.tenants = data.detail.tenants;
          throw err;
        }
        if (status === 429) throw new Error("尝试次数过多，请稍后再试");
        if (status === 401) throw new Error((data && data.detail) || "用户名或密码错误");
        if (status === 403) throw new Error((data && typeof data.detail === "string" && data.detail) || "该账号不允许绑定设备");
        throw new Error((data && typeof data.detail === "string" && data.detail) || "绑定失败（" + status + "）");
      });
  }

  var api = {
    STORAGE_KEY: STORAGE_KEY,
    load: load,
    save: save,
    clear: clear,
    saveFromResponse: saveFromResponse,
    bindWithCode: bindWithCode,
    login: login,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root && typeof root === "object") {
    root.MvDeviceCredential = api;
  }
})(typeof window !== "undefined" ? window : this);
