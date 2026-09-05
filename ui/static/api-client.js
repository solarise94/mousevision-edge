// API 客户端：同源（服务器托管 H5）与跨源（打包进 APK 的独立 app）两种运行模式。
//
// 打包 app 模式：构建期生成的 config.js 注入 window.MV_CONFIG
//   { apiBase: API 服务器地址, token: 同步令牌, shareToken, edition, appOrigin }。
//   所有相对 /api/* 路径经 apiUrl() 前置 apiBase，token 走 X-MouseVision-Token 头。
// 服务器托管模式：无 MV_CONFIG，同源请求。
//
// B5（合同 §6.2/§7）鉴权头优先级：
//   1. 设备凭证（localStorage mv.deviceCredential.v1，经绑定码/子账号登录签发，
//      绑定固定工作区）——云版新链路的正式身份；
//   2. MV_CONFIG.token（历史 cloud APK 注入的共享同步令牌，过渡期兼容；新包
//      不再注入）；
//   3. 服务器注入 meta（历史托管 H5；B5 起服务端已停止注入，读到空值即无头）。
// 头名沿用 X-MouseVision-Token（服务端 ContextResolver 先查设备表再查 legacy
// 令牌；CORS allowlist 也只放行该头）。

const MV_DEVICE_CREDENTIAL_KEY = "mv.deviceCredential.v1";

function mvDeviceCredential() {
  try {
    const raw = localStorage.getItem(MV_DEVICE_CREDENTIAL_KEY);
    if (!raw) return null;
    const obj = JSON.parse(raw);
    if (obj && typeof obj === "object" && typeof obj.token === "string" && obj.token) return obj;
  } catch (_) {}
  return null;
}

function mvConfig() {
  return window.MV_CONFIG && typeof window.MV_CONFIG === "object" ? window.MV_CONFIG : null;
}

function authHeaders() {
  // 优先级：设备凭证 → 打包 app 的同步令牌 → 服务器注入 meta → 无令牌。
  const cred = mvDeviceCredential();
  if (cred && cred.token) return { "X-MouseVision-Token": cred.token };
  const cfg = mvConfig();
  if (cfg && cfg.token) return { "X-MouseVision-Token": cfg.token };
  const token = document.querySelector('meta[name="mousevision-api-token"]')?.content?.trim();
  return token ? { "X-MouseVision-Token": token } : {};
}

/** 相对 API 路径拼上打包 app 的 apiBase；绝对 URL 原样；无配置时同源原样。 */
function apiUrl(path) {
  const cfg = mvConfig();
  if (!cfg || !cfg.apiBase) return path;
  // 绝对 URL（含协议相对 //host）原样放行，如远端 CDN 或跨源资源。
  if (/^[a-z][a-z0-9+.-]*:/i.test(path) || path.startsWith("//")) return path;
  return cfg.apiBase.replace(/\/+$/, "") + path;
}

function apiFetch(url, options = {}) {
  const headers = { ...authHeaders(), ...(options.headers || {}) };
  return fetch(apiUrl(url), { ...options, headers });
}
