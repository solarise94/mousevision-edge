function authHeaders() {
  const token = document.querySelector('meta[name="mousevision-api-token"]')?.content?.trim();
  return token ? { "X-MouseVision-Token": token } : {};
}

function apiFetch(url, options = {}) {
  const headers = { ...authHeaders(), ...(options.headers || {}) };
  return fetch(url, { ...options, headers });
}
