// API 客户端：同源（服务器托管 H5）与跨源（打包进 APK 的独立 app）两种运行模式。
//
// 打包 app 模式：构建期生成的 config.js 注入 window.MV_CONFIG
//   { apiBase: API 服务器地址, token: 同步令牌, appOrigin: 合成域 }。
//   所有相对 /api/* 路径经 apiUrl() 前置 apiBase，token 走 X-MouseVision-Token 头。
// 服务器托管模式：无 MV_CONFIG，同源请求，token 由服务端注入 meta（现状不变）。

function mvConfig() {
  return window.MV_CONFIG && typeof window.MV_CONFIG === "object" ? window.MV_CONFIG : null;
}

function authHeaders() {
  // 优先级：打包 app 的同步令牌 → 服务器注入 meta（托管 H5）→ 无令牌。
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
