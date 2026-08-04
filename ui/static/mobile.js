/* 小鼠称重记录 — 手机 Web SPA
 * 单文件实现：路由 / 状态 / API / 相机 / 扫码 / 视图 (design §4, §6, §7)
 * 依赖 api-client.js 提供的全局 apiFetch()。
 */
(function () {
  "use strict";

  const BASE = "/mobile";
  const app = document.getElementById("app");

  /* dev 模式：Android dev 版会在 H5 URL 后追加 ?dev=1。
   * SPA pushState 会丢 query，所以同时落 sessionStorage 保持会话级持久。
   * dev 模式下每次录制会话采集完整天平读数时间序列，随记录上报（供训练识别模型）。
   * 非 dev 模式行为与现状完全一致（零开销：不采集、不附字段）。 */
  var DEV_MODE = /[?&]dev=1\b/.test(location.search)
    || (function () { try { return sessionStorage.getItem("mv.devMode") === "1"; } catch (_) { return false; } })();
  if (DEV_MODE) { try { sessionStorage.setItem("mv.devMode", "1"); } catch (_) {} }

  /* ------------------------------------------------------------------ *
   * 状态 (design §7.5)
   * ------------------------------------------------------------------ */
  const state = {
    projectId: localStorage.getItem("mv.projectId") || "default",
    currentBox: null, // { cageId, strain, mouseNoPad }
    activeJobId: null,
  };
  function loadCurrentBox() {
    try {
      const raw = sessionStorage.getItem("mv.currentBox");
      state.currentBox = raw ? JSON.parse(raw) : null;
    } catch (_) {
      state.currentBox = null;
    }
  }
  function setCurrentBox(box) {
    state.currentBox = box;
    sessionStorage.setItem("mv.currentBox", JSON.stringify(box));
    if (box) localStorage.setItem("mv.lastCageId", box.cageId);
  }
  loadCurrentBox();

  /* ------------------------------------------------------------------ *
   * 全局天平连接管理（首页连接一次，整 app 共享；record 复用）。
   * K797 不可连接广播秤必须持续被动扫描，所以连接 = 持续扫描 + 监听状态。
   * ------------------------------------------------------------------ */
  const scaleConn = {
    channel: null,            // ScaleBridge.createScaleChannel() 实例
    state: "disconnected",    // disconnected | connecting | connected | stale | error
    lastGrams: null,          // 最近读数克数（显示用）
    lastReading: null,        // 最近读数 detail
    errorMsg: "",
    devices: [],              // 扫描发现的天平设备（{deviceId,name,rssi,grams}）
    selectedDeviceId: null,   // 当前选定设备 deviceId
    selectedDeviceName: null, // 当前选定设备名（显示用）
    _subs: [],                // 状态变更订阅者（首页卡片等）
  };
  function notifyScaleConn() {
    scaleConn._subs.forEach((fn) => { try { fn(); } catch (_) {} });
  }

  /* 选定设备持久化：localStorage mv.scaleDevice = JSON {deviceId,name}。
   * 断开连接时清除内存引用，但保留 localStorage（下次进入可自动重选）。
   * 仅在选择确认时写入。 */
  function loadSavedScaleDevice() {
    try {
      const raw = localStorage.getItem("mv.scaleDevice");
      if (!raw) return null;
      const obj = JSON.parse(raw);
      if (obj && typeof obj.deviceId === "string" && obj.deviceId) {
        return { deviceId: obj.deviceId, name: typeof obj.name === "string" ? obj.name : "" };
      }
    } catch (_) {}
    return null;
  }
  function saveScaleDevice(deviceId, name) {
    try {
      localStorage.setItem("mv.scaleDevice", JSON.stringify({ deviceId: deviceId, name: name || "" }));
    } catch (_) {}
  }
  function clearSavedScaleDevice() {
    try { localStorage.removeItem("mv.scaleDevice"); } catch (_) {}
  }
  // 启动时把持久化的选定设备载入内存（供 viewRecord 直连用）
  (function restoreScaleDevice() {
    const saved = loadSavedScaleDevice();
    if (saved) {
      scaleConn.selectedDeviceId = saved.deviceId;
      scaleConn.selectedDeviceName = saved.name;
    }
  })();

  /* ------------------------------------------------------------------ *
   * 离线记录上报队列（app 级单例）。纯 app 化后称重结果由本地控制器
   * 入队，outbox 负责联网补传 POST /api/records/report。整个 app 生命周期
   * 复用同一个实例（localStorage 持久化 + online 事件自动 flush）。
   * ------------------------------------------------------------------ */
  const reportOutbox = ReportClient.createOutbox({ storage: localStorage });
  reportOutbox.start();

  /* 构造并启动一个天平通道，挂载标准读数/状态/stale 回调。
   * deviceId 可选：支持设备选择 API 时传入以锁定设备；否则走旧直连。 */
  function startScaleConnChannel(deviceId) {
    const ch = ScaleBridge.createScaleChannel(deviceId ? { deviceId: deviceId } : {});
    ch.onReading(function (reading) {
      scaleConn.lastReading = reading;
      scaleConn.lastGrams = reading.grams;
      scaleConn.state = "connected";
      // 每条读数都通知：卡片要实时刷新克数；仅在状态跃迁时通知会导致
      // staleCbs 先触发的渲染把克数定格在 null（"已连接 · —"）。
      notifyScaleConn();
    });
    ch.onStaleChange(function (isStale) {
      scaleConn.state = isStale ? "stale" : "connected";
      // 短暂 stale 时**保留**上一个有效克数：状态点已表达"广播中断"，
      // 数字不应清空，否则读数间隙（卓易通容器扫描投递比原生更稀疏/突发）
      // 会让卡片在 26.3 与 -- g 之间闪烁。彻底清空只发生在 disconnect/error。
      notifyScaleConn();
    });
    ch.onStatus(function (detail) {
      const bad = detail.state === "unauthorized" || detail.state === "bluetooth_off" ||
        detail.state === "off" || detail.state === "error";
      if (bad) {
        scaleConn.state = "error";
        scaleConn.errorMsg = detail.message || "天平异常";
        notifyScaleConn();
      }
    });
    ch.start();
    return ch;
  }

  /* 选择漂移自愈：选定的 deviceId 连续多次不在发现表、且表中恰好只有一台设备时，
   * 自动改选这台（应对秤/模拟器重启后 BLE 地址漂移——真机验收实测会发生）。
   * 连续 3 次（devices 事件 ≥500ms 间隔，约 1.5s）确认，避免广播抖动误切。
   * 返回 true 表示发生了切换。 */
  function reconcileScaleSelection(ch, norm) {
    if (!scaleConn.selectedDeviceId) { scaleConn._mismatch = 0; return false; }
    const present = norm.devices.some((x) => x.deviceId === scaleConn.selectedDeviceId);
    if (present || norm.devices.length !== 1) { scaleConn._mismatch = 0; return false; }
    scaleConn._mismatch = (scaleConn._mismatch || 0) + 1;
    if (scaleConn._mismatch < 3) return false;
    const only = norm.devices[0];
    scaleConn.selectedDeviceId = only.deviceId;
    scaleConn.selectedDeviceName = only.name;
    saveScaleDevice(only.deviceId, only.name);
    ch.selectDevice(only.deviceId);
    scaleConn._mismatch = 0;
    return true;
  }

  /* connectScale 入口：
   * - 支持 device API → 开始扫描 + 打开"选择天平"sheet（用户挑选设备）
   * - 不支持（legacy app）→ 直接启动通道并自动连（旧行为） */
  function connectScale() {
    if (!ScaleBridge.detectNativeBridge()) {
      scaleConn.state = "error";
      scaleConn.errorMsg = "未检测到天平桥（请在原生外壳中打开）";
      notifyScaleConn();
      return;
    }
    if (scaleConn.channel) return; // 已在连接
    if (ScaleBridge.detectDeviceSupport()) {
      openDevicePickSheet();
      return;
    }
    // legacy 直连
    scaleConn.state = "connecting";
    scaleConn.errorMsg = "";
    notifyScaleConn();
    scaleConn.channel = startScaleConnChannel(null);
  }
  function disconnectScale() {
    if (scaleConn.channel) {
      try { scaleConn.channel.stop(); } catch (_) {}
      scaleConn.channel = null;
    }
    scaleConn.state = "disconnected";
    scaleConn.lastGrams = null;
    scaleConn.lastReading = null;
    scaleConn.errorMsg = "";
    scaleConn.devices = [];
    notifyScaleConn();
  }

  /* ------------------------------------------------------------------ *
   * "选择天平"底部弹出层（仅 device API 可用时由 connectScale 调用）。
   * - 开始扫描 → 订阅 onDevices 实时刷新列表
   * - 点行 → channel.selectDevice → 选定设备的读数到达（state=connected）后
   *   0.8s 自动关 sheet；也可点"完成"关 sheet
   * - 关 sheet 时未选任何设备 → 停止扫描回 disconnected
   * ------------------------------------------------------------------ */
  function openDevicePickSheet() {
    // 进入发现模式即启动扫描通道（不发读数，仅发现设备）
    scaleConn.state = "connecting";
    scaleConn.errorMsg = "";
    scaleConn.devices = [];
    notifyScaleConn();
    const ch = startScaleConnChannel(null);
    scaleConn.channel = ch;
    // 已有历史选定设备（localStorage 恢复）→ 立即通知原生锁定该设备自动重连；
    // 读数到达后 state=connected，sheet 0.8s 自动关闭（用户仍可改选其他设备）。
    if (scaleConn.selectedDeviceId) {
      ch.selectDevice(scaleConn.selectedDeviceId);
    } else {
      // 发现模式：显式 clear 一次。关键作用是让原生侧确认"页面在用新 API"，
      // 取消 4s 旧版兜底抢选（getScaleDevices 纯查询不会标记，只有
      // select/clear 才标记；无选择时 clear 是幂等 no-op，无副作用）。
      ch.clearDevice();
    }

    // 构造 sheet DOM
    const overlay = h("div", { class: "sheet-overlay device-pick-overlay" });
    const sheet = h("div", { class: "sheet device-pick-sheet" }, [
      h("div", { class: "sheet-grabber" }),
      h("div", { class: "sheet-header" }, [
        h("div", { class: "sheet-title" }, "选择天平"),
        h("button", { class: "sheet-done-btn", type: "button" }, "完成"),
      ]),
    ]);
    const listWrap = h("div", { class: "device-list" });
    const searchingHint = h("div", { class: "device-searching" }, [
      h("div", { class: "device-spinner" }),
      h("div", {}, "正在搜索附近的天平…"),
    ]);
    sheet.appendChild(listWrap);
    overlay.appendChild(sheet);
    document.body.appendChild(overlay);
    // 触发上滑动画
    requestAnimationFrame(() => overlay.classList.add("visible"));

    let closed = false;
    let autoCloseTimer = null;

    function renderList() {
      listWrap.innerHTML = "";
      const devs = scaleConn.devices;
      if (!devs.length) {
        listWrap.appendChild(searchingHint);
        return;
      }
      devs.forEach((d) => {
        const isSelected = d.deviceId === scaleConn.selectedDeviceId;
        const gramsText = (typeof d.grams === "number" && isFinite(d.grams))
          ? Number(d.grams).toFixed(1) + " g"
          : "—";
        const row = h("div", {
          class: "device-row" + (isSelected ? " selected" : ""),
          onClick: () => {
            if (closed) return;
            scaleConn.selectedDeviceId = d.deviceId;
            scaleConn.selectedDeviceName = d.name;
            saveScaleDevice(d.deviceId, d.name);
            ch.selectDevice(d.deviceId);
            renderList();
          },
        }, [
          h("div", { class: "device-row-main" }, [
            h("div", { class: "device-row-name" }, d.name),
            h("div", { class: "device-row-sub" }, gramsText),
          ]),
          rssiIndicator(d.rssi),
          isSelected ? h("div", { class: "device-row-check" }, "✓") : null,
        ]);
        listWrap.appendChild(row);
      });
    }

    // 实时设备刷新
    const onDevicesCb = function (norm) {
      scaleConn.devices = norm.devices;
      // 地址漂移自愈：选定设备不在表中且仅一台候选 → 自动改选（见函数注释）
      reconcileScaleSelection(ch, norm);
      // 若本地无选择而原生侧已有（如兜底自动选）→ 同步并持久化；
      // 本地已有选择时不覆盖（刚自愈改选的新 id 可能被原生旧事件回写）。
      if (norm.selectedDeviceId && !scaleConn.selectedDeviceId) {
        scaleConn.selectedDeviceId = norm.selectedDeviceId;
        const m = norm.devices.filter((x) => x.deviceId === norm.selectedDeviceId)[0];
        if (m) scaleConn.selectedDeviceName = m.name;
        saveScaleDevice(norm.selectedDeviceId, m ? m.name : "");
      }
      renderList();
      // 选定设备的读数到达（state 变 connected）→ 0.8s 后自动关 sheet
      if (scaleConn.state === "connected" && scaleConn.selectedDeviceId && !autoCloseTimer && !closed) {
        autoCloseTimer = setTimeout(closeSheet, 800);
      }
    };
    ch.onDevices(onDevicesCb);
    // 立即主动拉一次发现表：既填充初始列表，又让原生侧知道页面在用新 API，
    // 取消 4s 旧版兜底自动选择（否则用户还在看列表就被原生抢选最强设备）。
    ch.refreshDevices();
    // 已选定设备：状态变 connected 也触发自动关闭（设备读数来源）
    const connSub = function () {
      if (scaleConn.state === "connected" && scaleConn.selectedDeviceId && !autoCloseTimer && !closed) {
        autoCloseTimer = setTimeout(closeSheet, 800);
      }
    };
    scaleConn._subs.push(connSub);

    renderList();

    function closeSheet() {
      if (closed) return;
      closed = true;
      if (autoCloseTimer) { clearTimeout(autoCloseTimer); autoCloseTimer = null; }
      const i = scaleConn._subs.indexOf(connSub);
      if (i >= 0) scaleConn._subs.splice(i, 1);
      // 未选任何设备 → 停止扫描回 disconnected
      if (!scaleConn.selectedDeviceId) {
        if (scaleConn.channel) {
          try { scaleConn.channel.stop(); } catch (_) {}
          scaleConn.channel = null;
        }
        scaleConn.state = "disconnected";
        scaleConn.devices = [];
        notifyScaleConn();
      }
      overlay.classList.remove("visible");
      setTimeout(() => { if (overlay.parentNode) overlay.remove(); }, 280);
    }

    // 点遮罩空白 = 取消（不选定）
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeSheet(); });
    sheet.querySelector(".sheet-done-btn").addEventListener("click", closeSheet);
  }

  /* RSSI 信号强度指示：4 根递增高度竖条，按 rssi 分 4 档（CSS 画，不用 emoji）。
   * -50 以上 4 格；-60 以上 3 格；-70 以上 2 格；其余 1 格。 */
  function rssiIndicator(rssi) {
    if (typeof rssi !== "number" || !isFinite(rssi)) rssi = -100;
    let level;
    if (rssi >= -50) level = 4;
    else if (rssi >= -60) level = 3;
    else if (rssi >= -70) level = 2;
    else level = 1;
    const bars = [];
    for (let i = 1; i <= 4; i++) {
      bars.push(h("span", { class: "rssi-bar" + (i <= level ? " on" : "") }));
    }
    return h("div", { class: "rssi-indicator", "aria-label": `信号 ${level}/4` }, bars);
  }

  /* ------------------------------------------------------------------ *
   * API
   * ------------------------------------------------------------------ */
  const api = {
    async json(url, opts) {
      const res = await apiFetch(url, opts);
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const body = await res.json();
          detail = body.detail || detail;
        } catch (_) {}
        const err = new Error(detail);
        err.status = res.status;
        throw err;
      }
      return res.json();
    },
    recentBoxes: () => api.json("/api/boxes/recent?limit=6"),
    boxes: (strain) =>
      api.json("/api/boxes" + (strain ? `?strain=${encodeURIComponent(strain)}` : "")),
    box: (cage) => api.json(`/api/boxes/${encodeURIComponent(cage)}`),
    boxRecords: (cage) => api.json(`/api/boxes/${encodeURIComponent(cage)}/records`),
    createBox: (payload) =>
      api.json("/api/boxes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    record: (id) => api.json(`/api/records/${encodeURIComponent(id)}`),
    job: (id) => api.json(`/api/jobs/${encodeURIComponent(id)}`),
    jobWait: (id) => api.json(`/api/jobs/${encodeURIComponent(id)}/wait`),
    jobReport: (id) => api.json(`/api/jobs/${encodeURIComponent(id)}/report`),
  };

  /* ------------------------------------------------------------------ *
   * DOM 助手
   * ------------------------------------------------------------------ */
  function h(tag, attrs, children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "html") el.innerHTML = v;
        else if (k.startsWith("on") && typeof v === "function")
          el.addEventListener(k.slice(2).toLowerCase(), v);
        else if (k === "hidden") el.hidden = !!v;
        else el.setAttribute(k, v);
      }
    }
    for (const c of [].concat(children || [])) {
      if (c == null || c === false) continue;
      el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return el;
  }
  const STATUS_LABEL = {
    uploading: "上传中",
    queued: "等待分析",
    processing: "分析中",
    completed: "已分析",
    failed: "分析失败",
    canceled: "已取消",
  };
  function badge(status) {
    return h("span", { class: `badge ${status}` }, STATUS_LABEL[status] || status);
  }
  function pad(n, width) {
    return String(n == null ? "" : n).padStart(width || 2, "0");
  }
  function fmtTime(iso) {
    if (!iso) return "";
    return iso.replace("T", " ").slice(0, 19);
  }
  function fmtWait(sec) {
    if (sec == null) return "--:--";
    const s = Math.max(0, Math.round(sec));
    return `${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
  }
  function fmtBytes(b) {
    b = Number(b || 0);
    return b < 1048576 ? `${(b / 1024).toFixed(0)} KB` : `${(b / 1048576).toFixed(1)} MB`;
  }

  let toastTimer = null;
  function toast(msg) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.hidden = true), 2600);
  }

  function showQr(cage) {
    const overlay = h(
      "div",
      {
        style:
          "position:fixed;inset:0;z-index:200;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;padding:24px",
        onClick: () => overlay.remove(),
      },
      [
        h("div", { class: "card", style: "text-align:center;max-width:320px;width:100%" }, [
          h("div", { class: "card-title" }, cage),
          h("img", {
            src: `/api/boxes/${encodeURIComponent(cage)}/qr.svg`,
            alt: "二维码",
            style: "width:220px;height:220px",
          }),
          h("p", { class: "li-sub" }, "扫此码选箱录制 · 点击空白处关闭"),
        ]),
      ]
    );
    document.body.appendChild(overlay);
  }

  function appbar(title, opts) {
    opts = opts || {};
    const left = opts.back
      ? h("button", { class: "iconbtn", onClick: () => go(opts.back === true ? -1 : opts.back) }, "‹")
      : opts.leftIcon
      ? h("button", { class: "iconbtn", onClick: opts.onLeft }, opts.leftIcon)
      : h("span", { class: "iconbtn" }, "");
    const right = opts.right || h("span", { class: "slot right" }, "");
    // Accept either a string title or a pre-built node (so callers can mutate
    // the title text in place, e.g. the record screen switching between
    // 准备 / 录制中 / 上传中).
    const titleChild = opts.titleNode || title;
    return h("header", { class: "appbar" + (opts.transparent ? " transparent" : "") }, [
      h("span", { class: "slot" }, [left]),
      typeof titleChild === "string" ? h("h1", {}, titleChild) : titleChild,
      h("span", { class: "slot right" }, [right]),
    ]);
  }

  /* ------------------------------------------------------------------ *
   * 路由
   * ------------------------------------------------------------------ */
  const routes = [];
  function route(pattern, view) {
    const keys = [];
    const rx = new RegExp(
      "^" +
        pattern.replace(/:[^/]+/g, (m) => {
          keys.push(m.slice(1));
          return "([^/]+)";
        }) +
        "$"
    );
    routes.push({ rx, keys, view });
  }
  function go(to) {
    if (to === -1) {
      history.back();
      return;
    }
    const path = to.startsWith("/") ? BASE + to : to;
    history.pushState({}, "", path);
    render();
  }
  window.addEventListener("popstate", render);

  let cleanup = null;
  async function render() {
    if (cleanup) {
      try { cleanup(); } catch (_) {}
      cleanup = null;
    }
    let rel = location.pathname.slice(BASE.length) || "/";
    if (rel === "") rel = "/";
    let matched = null;
    for (const r of routes) {
      const m = rel.match(r.rx);
      if (m) {
        const params = {};
        r.keys.forEach((k, i) => (params[k] = decodeURIComponent(m[i + 1])));
        matched = { view: r.view, params };
        break;
      }
    }
    if (!matched) matched = { view: viewHome, params: {} };
    app.innerHTML = "";
    try {
      cleanup = (await matched.view(matched.params)) || null;
    } catch (err) {
      app.appendChild(errorScreen(err));
    }
  }

  function errorScreen(err) {
    return h("div", { class: "screen" }, [
      appbar("出错了", { back: "/" }),
      h("div", { class: "content" }, [
        h("div", { class: "empty" }, (err && err.message) || "加载失败"),
      ]),
    ]);
  }

  function mount(node) {
    app.appendChild(node);
  }

  /* ------------------------------------------------------------------ *
   * 相机助手 — Canvas 720×1280 所见即所得 (design §6.2/§6.3)
   * 中心裁切算法与 mousevision/capture_geom.py 对齐。
   * ------------------------------------------------------------------ */
  const CLIENT_VERSION = "2026.07.14-canvas";
  const CANVAS_W = 720;
  const CANVAS_H = 1280;

  function supportsLiveCanvasCapture() {
    const canvas = document.createElement("canvas");
    return !!(
      navigator.mediaDevices &&
      navigator.mediaDevices.getUserMedia &&
      window.MediaRecorder &&
      canvas.captureStream
    );
  }

  function centerCropSourceRect(srcW, srcH, dstW, dstH) {
    // Mirror of mousevision.capture_geom.center_crop_source_rect
    dstW = dstW || CANVAS_W;
    dstH = dstH || CANVAS_H;
    if (!srcW || !srcH || !dstW || !dstH) return null;
    const srcAspect = srcW / srcH;
    const dstAspect = dstW / dstH;
    let sx, sy, sw, sh;
    if (srcAspect > dstAspect) {
      sh = srcH;
      sw = srcH * dstAspect;
      sx = (srcW - sw) / 2;
      sy = 0;
    } else {
      sw = srcW;
      sh = srcW / dstAspect;
      sx = 0;
      sy = (srcH - sh) / 2;
    }
    return { sx, sy, sw, sh };
  }

  function trackSettings(stream) {
    try {
      const track = stream && stream.getVideoTracks && stream.getVideoTracks()[0];
      return track && track.getSettings ? track.getSettings() : {};
    } catch (_) {
      return {};
    }
  }

  function videoSourceSize(videoEl, stream) {
    let w = videoEl && videoEl.videoWidth;
    let h = videoEl && videoEl.videoHeight;
    if (w && h) return { width: w, height: h };
    const s = trackSettings(stream);
    w = s.width || 0;
    h = s.height || 0;
    if (w && h) return { width: w, height: h };
    return null;
  }

  async function openCameraStream(constraints) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("insecure");
    }
    return navigator.mediaDevices.getUserMedia({
      audio: false,
      video: constraints,
    });
  }

  async function openBackCamera(videoEl, deviceId) {
    // Prefer a moderate landscape capture; Canvas then center-crops to 720x1280.
    const base = {
      width: { ideal: 1280 },
      height: { ideal: 720 },
      frameRate: { ideal: 15, max: 30 },
    };
    let stream;
    if (deviceId) {
      stream = await openCameraStream({ ...base, deviceId: { exact: deviceId } });
    } else {
      try {
        stream = await openCameraStream({
          ...base,
          facingMode: { exact: "environment" },
        });
      } catch (err) {
        const constraintFailure = [
          "OverconstrainedError",
          "ConstraintNotSatisfiedError",
          "NotFoundError",
        ].includes(err && err.name);
        if (!constraintFailure) throw err;
        stream = await openCameraStream({
          ...base,
          facingMode: { ideal: "environment" },
        });
      }
    }
    videoEl.srcObject = stream;
    videoEl.muted = true;
    videoEl.playsInline = true;
    await videoEl.play();
    return stream;
  }

  async function listVideoInputs() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
      return [];
    }
    const devices = await navigator.mediaDevices.enumerateDevices();
    return devices.filter((d) => d.kind === "videoinput");
  }

  function stopStream(stream) {
    if (stream) stream.getTracks().forEach((t) => t.stop());
  }

  function computePreviewCrop(videoEl, stageEl, stream) {
    // Legacy CSS object-fit:cover crop (kept for debugging / old path).
    const size = videoSourceSize(videoEl, stream);
    const sw = stageEl.clientWidth;
    const sh = stageEl.clientHeight;
    if (!size || !sw || !sh) return null;
    const vw = size.width;
    const vh = size.height;
    const videoRatio = vw / vh;
    const stageRatio = sw / sh;
    let cropW, cropH;
    if (videoRatio > stageRatio) {
      cropH = 1;
      cropW = stageRatio / videoRatio;
    } else {
      cropW = 1;
      cropH = videoRatio / stageRatio;
    }
    const x = (1 - cropW) / 2;
    const y = (1 - cropH) / 2;
    const r = (n) => Math.round(n * 10000) / 10000;
    return { x: r(x), y: r(y), w: r(cropW), h: r(cropH) };
  }

  function pickMime() {
    // 首选 MP4/H.264（iOS Safari 唯一支持、分析管线首选容器）；
    // 卓易通等 Android WebView 的 MediaRecorder 不支持 MP4 → 回退 WebM
    // （后端 _upload_suffix 接受 .webm，OpenCV/ffmpeg 可正常解码）。
    if (!window.MediaRecorder) return "";
    const list = [
      "video/mp4;codecs=avc1.42E01E",
      "video/mp4",
      "video/webm;codecs=vp9",
      "video/webm;codecs=vp8",
      "video/webm",
    ];
    return list.find((t) => MediaRecorder.isTypeSupported(t)) || "";
  }

  /* 按实际录制的 mime 决定上传扩展名（与后端 _upload_suffix 对齐）。 */
  function extForMime(mime) {
    return mime && mime.indexOf("webm") >= 0 ? "webm" : "mp4";
  }

  /* ------------------------------------------------------------------ *
   * 上传
   * ------------------------------------------------------------------ */
  function uploadVideo(blob, filename, box, onProgress, durationSec, opts) {
    opts = opts || {};
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      fd.append("cage_id", box.cageId);
      fd.append("project_id", state.projectId);
      fd.append("expected_single", "true");
      if (durationSec != null && durationSec > 0) {
        fd.append("recorded_duration_sec", String(durationSec));
      }
      if (opts.previewCrop) {
        fd.append("preview_crop", JSON.stringify(opts.previewCrop));
      }
      if (opts.captureMode) {
        fd.append("capture_mode", opts.captureMode);
      }
      if (opts.captureMeta) {
        fd.append(
          "capture_meta",
          typeof opts.captureMeta === "string"
            ? opts.captureMeta
            : JSON.stringify(opts.captureMeta)
        );
      }
      fd.append("video", blob, filename);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/jobs");
      const token = document
        .querySelector('meta[name="mousevision-api-token"]')
        ?.content?.trim();
      if (token) xhr.setRequestHeader("X-MouseVision-Token", token);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try { resolve(JSON.parse(xhr.responseText)); }
          catch (_) { reject(new Error("响应解析失败")); }
        } else {
          let detail = `上传失败 (${xhr.status})`;
          try { detail = JSON.parse(xhr.responseText).detail || detail; } catch (_) {}
          reject(new Error(detail));
        }
      };
      xhr.onerror = () => reject(new Error("网络错误"));
      xhr.send(fd);
    });
  }

  /* ================================================================== *
   * 视图：首页 (屏 1)
   * ================================================================== */
  async function viewHome() {
    const screen = h("div", { class: "screen" });
    screen.appendChild(
      appbar("", {
        right: h("button", { class: "appbar-btn", onClick: () => go("/settings") }, "设置"),
      })
    );
    const content = h("div", { class: "content" });

    // 大标题 + 日期副标题
    const today = new Date();
    const dateStr = `${today.getFullYear()}年${today.getMonth() + 1}月${today.getDate()}日`;
    content.appendChild(h("div", { class: "page-title" }, "小鼠称重"));
    content.appendChild(h("div", { class: "page-subtitle" }, dateStr));

    // 天平连接卡片（全局连接入口，iOS 风格）
    const connectCard = renderScaleConnectCard();
    content.appendChild(connectCard);

    // 操作分组列表（开始录制 ›、箱子管理 ›）
    content.appendChild(h("div", { class: "group-title" }, "操作"));
    const opGroup = h("div", { class: "group" }, [
      // 开始录制：未连接时 label3 置灰但可点（进手动模式）
      groupNavRow("开始录制", null, () => startRecording(), "home-start-rec"),
      groupNavRow("箱子管理", null, () => go("/manage")),
    ]);
    content.appendChild(opGroup);

    // 最近记录分组
    content.appendChild(h("div", { class: "group-title" }, "最近记录"));
    const listWrap = h("div", { class: "group recent-group" }, [h("div", { class: "empty group-empty" }, "加载中…")]);
    content.appendChild(listWrap);
    content.appendChild(
      h("button", { class: "group-footer-btn", onClick: () => go("/manage") }, "查看全部 ›")
    );
    screen.appendChild(content);
    mount(screen);

    // 连接卡片订阅全局连接状态实时刷新；同时刷新开始录制行的置灰态
    const startRow = opGroup.querySelector(".home-start-rec");
    const updater = renderScaleConnectCard._updater(connectCard, startRow);
    scaleConn._subs.push(updater);
    return () => {
      const i = scaleConn._subs.indexOf(updater);
      if (i >= 0) scaleConn._subs.splice(i, 1);
    };

    try {
      const data = await api.recentBoxes();
      listWrap.innerHTML = "";
      if (!data.items.length) {
        listWrap.appendChild(h("div", { class: "empty group-empty" }, "还没有记录，去录制第一只吧"));
      } else {
        data.items.forEach((b) => listWrap.appendChild(recentRow(b)));
      }
    } catch (err) {
      listWrap.innerHTML = "";
      listWrap.appendChild(h("div", { class: "empty group-empty" }, err.message));
    }
  }

  /* iOS 风格分组导航行：左标题 / 副标题 + 右侧 "›"。extraClass 用于钩子。 */
  function groupNavRow(title, subtitle, onClick, extraClass) {
    const main = h("div", { class: "group-row-main" }, [
      h("div", { class: "group-row-title" }, title),
      subtitle ? h("div", { class: "group-row-sub" }, subtitle) : null,
    ]);
    return h("div", { class: "group-row nav-row" + (extraClass ? " " + extraClass : ""), onClick: onClick }, [
      main,
      h("span", { class: "group-chevron" }, "›"),
    ]);
  }

  // 天平连接卡片渲染 + 状态→DOM 更新器（返回一个订阅函数）
  function renderScaleConnectCard() {
    const card = h("div", { class: "card scale-connect-card" });
    // 左侧 40px 圆角 9 蓝色方块内白色"⚖"
    const icon = h("div", { class: "connect-icon" }, "⚖");
    const dot = h("span", { class: "connect-dot" });
    const statusText = h("div", { class: "connect-status-text" }, "天平未连接");
    const weightText = h("div", { class: "connect-weight" }, "");
    const title = h("div", { class: "connect-title" }, "天平");
    const info = h("div", { class: "connect-info" }, [dot, statusText, weightText]);
    const infoBlock = h("div", { class: "connect-info-block" }, [title, info]);
    const actionBtn = h("button", { class: "pill connect-action pill-connect" }, "连接");
    actionBtn.addEventListener("click", () => {
      if (scaleConn.state === "disconnected" || scaleConn.state === "error") connectScale();
      else disconnectScale();
    });
    card.appendChild(h("div", { class: "connect-row" }, [icon, infoBlock, actionBtn]));
    // 立即按当前状态渲染一次
    applyConnectState(card, dot, statusText, weightText, actionBtn);
    return card;
  }
  renderScaleConnectCard._updater = function (card, startRow) {
    return function () {
      const dot = card.querySelector(".connect-dot");
      const statusText = card.querySelector(".connect-status-text");
      const weightText = card.querySelector(".connect-weight");
      const actionBtn = card.querySelector(".connect-action");
      applyConnectState(card, dot, statusText, weightText, actionBtn);
      // 开始录制行：未连接时 label3 置灰但仍可点（进手动模式）
      if (startRow) {
        const connected = scaleConn.state === "connected";
        startRow.classList.toggle("dim", !connected);
      }
    };
  };
  function applyConnectState(card, dot, statusText, weightText, actionBtn) {
    const s = scaleConn.state;
    const labels = {
      disconnected: "未连接",
      connecting: "正在搜索天平…",
      connected: "已连接",
      stale: "广播中断",
      error: scaleConn.errorMsg || "天平异常",
    };
    statusText.textContent = labels[s] || s;
    // connected/stale 时副标题显示 设备名 · 实时克数（stale 保留最近一次有效读数，
    // 仅状态点变橙表达"广播中断"，数字不闪烁）。
    if (s === "connected" || s === "stale") {
      const name = scaleConn.selectedDeviceName || "";
      const g = scaleConn.lastGrams !== null ? Number(scaleConn.lastGrams).toFixed(1) + " g" : "—";
      weightText.textContent = name ? `${name} · ${g}` : g;
      weightText.hidden = false;
    } else {
      weightText.textContent = "";
      weightText.hidden = true;
    }
    // 状态色点（pill 内的小圆点用 connect-dot class）
    if (dot) dot.className = "connect-dot " + s;
    actionBtn.textContent = (s === "disconnected" || s === "error") ? "连接" : "断开";
    actionBtn.disabled = (s === "connecting");
    // pill 状态色：connected 绿、connecting/stale 橙、error/disconnected 灰
    actionBtn.classList.remove("pill-green", "pill-orange", "pill-red", "pill-gray");
    if (s === "connected") actionBtn.classList.add("pill-green");
    else if (s === "connecting" || s === "stale") actionBtn.classList.add("pill-orange");
    else actionBtn.classList.add("pill-gray");
  }

  function recentRow(b) {
    const count = (b.record_count || 0) + (b.pending_count || 0);
    return h(
      "div",
      { class: "group-row nav-row recent-row", onClick: () => go(`/box/${encodeURIComponent(b.cage_id)}`) },
      [
        h("div", { class: "group-row-main" }, [
          h("div", { class: "group-row-title" }, b.cage_id),
          h("div", { class: "group-row-sub" }, `${b.strain} · ${fmtTime(b.last_activity_at || b.created_at)}`),
        ]),
        h("span", { class: "count-badge" }, `${count} 只`),
        h("span", { class: "group-chevron" }, "›"),
      ]
    );
  }

  function startRecording() {
    if (state.currentBox) go("/mode");
    else go("/scan");
  }

  /* ================================================================== *
   * 视图：录制模式选择（选笼号后、进录制前）
   * 三模式：后匹配 / 即时报数 / 手动。未连接天平时仅允许手动。
   * ================================================================== */
  function viewMode() {
    const screen = h("div", { class: "screen" });
    const box = state.currentBox || { cageId: "-" };
    screen.appendChild(appbar("记录方式", { back: "/scan" }));
    const content = h("div", { class: "content mode-content" });

    content.appendChild(h("div", { class: "page-title" }, "记录方式"));
    content.appendChild(h("div", { class: "page-subtitle" }, `本次录制：箱 ${box.cageId}`));

    const connected = scaleConn.state === "connected";
    const modes = [
      { id: "post_match", label: "后匹配", desc: "连续录像，自动记录每只，事后审核", requireScale: true, icon: "▶" },
      { id: "announce", label: "即时报数", desc: "每只暂停确认重量，语音报数", requireScale: true, icon: "♪" },
      { id: "manual", label: "手动", desc: "人工输入每只克数，无需天平", requireScale: false, icon: "✎" },
    ];
    let selectedMode = currentRecordMode();
    const cardsWrap = h("div", { class: "mode-cards" });
    function renderCards() {
      cardsWrap.innerHTML = "";
      modes.forEach((m) => {
        const disabled = m.requireScale && !connected;
        const selected = selectedMode === m.id;
        const card = h("button", {
          class: "mode-card" + (disabled ? " disabled" : "") + (selected ? " selected" : ""),
          onClick: () => {
            if (disabled) { toast("请先在首页连接天平"); return; }
            selectedMode = m.id;
            renderCards();
          },
        }, [
          h("div", { class: "mode-card-icon" }, m.icon),
          h("div", { class: "mode-card-main" }, [
            h("div", { class: "mode-card-label" }, m.label),
            h("div", { class: "mode-card-desc" }, m.desc),
          ]),
          selected ? h("div", { class: "mode-card-check" }, "✓") : null,
        ]);
        cardsWrap.appendChild(card);
      });
    }
    renderCards();
    content.appendChild(cardsWrap);
    if (!connected) {
      content.appendChild(h("div", { class: "group-footer mode-footer-note" },
        "天平未连接：后匹配 / 即时报数需先连接天平；可选择手动模式。"));
    }
    // 底部固定主按钮
    const startBtn = h("button", { class: "btn btn-p", onClick: () => {
      const m = modes.filter((x) => x.id === selectedMode)[0];
      if (m && m.requireScale && !connected) { toast("请先在首页连接天平"); return; }
      sessionStorage.setItem("mv.recordMode", selectedMode);
      go("/record");
    } }, "开始录制");
    screen.appendChild(content);
    screen.appendChild(h("div", { class: "dock dock-fixed" }, [startBtn]));
    mount(screen);
  }
  function currentRecordMode() {
    return sessionStorage.getItem("mv.recordMode") || "announce";
  }

  /* ================================================================== *
   * 视图：扫码选箱 (屏 2) — 浅色卡片布局
   * ================================================================== */
  async function viewScan() {
    const guideText = h(
      "div",
      { class: "scan-guide" },
      "请将二维码放入框内，系统将自动识别"
    );
    const video = h("video", {
      autoplay: "",
      muted: "",
      playsinline: "",
      "webkit-playsinline": "",
      "x5-playsinline": "",
    });
    const torchIcon = h("span", { class: "fab-icon" }, "💡");
    const torchLabel = document.createTextNode("开灯");
    const torchBtn = h(
      "button",
      { class: "scan-fab", type: "button", onClick: toggleTorch },
      [torchIcon, torchLabel]
    );
    const albumBtn = h(
      "button",
      { class: "scan-fab", type: "button", onClick: pickFromAlbum },
      [h("span", { class: "fab-icon" }, "🖼"), "相册"]
    );
    const resultValue = h("div", { class: "scan-result-value empty" }, "等待识别…");
    const rescanBtn = h(
      "button",
      { class: "rescan", type: "button", onClick: restartScan },
      "↻ 重新扫描"
    );
    const scanCard = h("div", { class: "scan-card" }, [
      video,
      h("div", { class: "scan-corners" }, [h("span")]),
      h("div", { class: "scan-card-actions" }, [torchBtn, albumBtn]),
    ]);
    const resultBlock = h("div", { class: "scan-result-block" }, [
      h("div", { class: "scan-result-head" }, [
        h("span", { class: "label" }, "识别结果"),
        rescanBtn,
      ]),
      resultValue,
    ]);
    const footer = h("div", { class: "scan-footer" }, [
      h(
        "button",
        { class: "scan-footer-btn", type: "button", onClick: showHelp },
        [h("span", { class: "ico" }, "?"), "使用帮助"]
      ),
      h(
        "button",
        { class: "scan-footer-btn", type: "button", onClick: manualInput },
        [h("span", { class: "ico kbd" }, "⌨"), "手动输入"]
      ),
    ]);
    const screen = h("div", { class: "screen scan-screen" }, [
      appbar("扫描箱号二维码", { back: "/" }),
      h("div", { class: "scan-body" }, [guideText, scanCard, resultBlock]),
      footer,
    ]);
    mount(screen);

    let stream = null;
    let scanning = true;
    let detector = null;
    let torchOn = false;
    let torchSupported = false;
    let videoTrack = null;
    let sheetEl = null;

    function setResult(text, ok) {
      if (ok) {
        resultValue.classList.remove("empty");
        resultValue.textContent = text;
      } else {
        resultValue.classList.add("empty");
        resultValue.textContent = text || "等待识别…";
      }
    }

    function closeSheet() {
      if (sheetEl) {
        sheetEl.remove();
        sheetEl = null;
      }
    }

    function onDecoded(text) {
      if (!scanning) return;
      scanning = false;
      if (navigator.vibrate) navigator.vibrate(60);
      const parsed = parseQr(text);
      setResult(parsed.cageId, true);
      // Brief pause so the operator can read the result before navigating.
      setTimeout(() => selectCage(parsed), 350);
    }

    async function loop() {
      if (!scanning || !detector) return;
      try {
        if (video.readyState >= 2) {
          const codes = await detector.detect(video);
          if (codes && codes.length) {
            onDecoded(codes[0].rawValue);
            return;
          }
        }
      } catch (_) {}
      if (scanning) requestAnimationFrame(loop);
    }

    function restartScan() {
      closeSheet();
      scanning = true;
      setResult("等待识别…", false);
      if (detector) requestAnimationFrame(loop);
    }

    async function applyTorch(on) {
      if (!videoTrack || !torchSupported) return false;
      try {
        await videoTrack.applyConstraints({ advanced: [{ torch: !!on }] });
        torchOn = !!on;
        torchBtn.classList.toggle("on", torchOn);
        torchLabel.textContent = torchOn ? "关灯" : "开灯";
        return true;
      } catch (_) {
        return false;
      }
    }

    async function toggleTorch() {
      if (!torchSupported) {
        toast("当前设备不支持手电筒");
        return;
      }
      const ok = await applyTorch(!torchOn);
      if (!ok) toast("无法切换手电筒");
    }

    async function refreshTorchCapability() {
      videoTrack = null;
      torchSupported = false;
      torchOn = false;
      torchBtn.classList.remove("on");
      torchLabel.textContent = "开灯";
      try {
        const track = stream && stream.getVideoTracks && stream.getVideoTracks()[0];
        if (!track) return;
        videoTrack = track;
        const caps =
          typeof track.getCapabilities === "function" ? track.getCapabilities() : null;
        torchSupported = !!(caps && "torch" in caps);
      } catch (_) {
        torchSupported = false;
      }
      torchBtn.disabled = !torchSupported;
    }

    (async () => {
      try {
        stream = await openBackCamera(video);
        await refreshTorchCapability();
        if ("BarcodeDetector" in window) {
          detector = new window.BarcodeDetector({ formats: ["qr_code"] });
          requestAnimationFrame(loop);
        } else {
          guideText.textContent = "此浏览器不支持自动扫码，请用相册选择或手动输入";
          guideText.style.color = "var(--muted)";
        }
      } catch (err) {
        guideText.textContent = "无法打开相机（需 HTTPS）。请用相册选择或手动输入";
        guideText.style.color = "var(--muted)";
        torchBtn.disabled = true;
      }
    })();

    async function pickFromAlbum() {
      const input = h("input", { type: "file", accept: "image/*" });
      input.onchange = async () => {
        const file = input.files && input.files[0];
        if (!file) return;
        if (!("BarcodeDetector" in window)) {
          toast("此浏览器不支持图片解码，请手动输入");
          return;
        }
        try {
          const bitmap = await createImageBitmap(file);
          const d = new window.BarcodeDetector({ formats: ["qr_code"] });
          const codes = await d.detect(bitmap);
          if (codes && codes.length) onDecoded(codes[0].rawValue);
          else toast("未识别到二维码");
        } catch (_) {
          toast("图片解码失败");
        }
      };
      input.click();
    }

    function manualInput() {
      closeSheet();
      const input = h("input", {
        type: "text",
        inputmode: "text",
        autocomplete: "off",
        autocapitalize: "characters",
        placeholder: "例如 C57-023",
        value: "",
      });
      const cancelBtn = h(
        "button",
        { class: "btn ghost", type: "button", onClick: closeSheet },
        "取消"
      );
      const okBtn = h("button", { class: "btn primary", type: "button" }, "确认");
      const panel = h("div", { class: "scan-manual-panel" }, [
        h("h3", {}, "手动输入箱号"),
        h("div", { class: "field", style: "margin-bottom:0" }, [
          h("label", {}, "箱号"),
          input,
        ]),
        h("div", { class: "actions" }, [cancelBtn, okBtn]),
      ]);
      sheetEl = h(
        "div",
        {
          class: "scan-manual-sheet",
          onClick: (e) => {
            if (e.target === sheetEl) closeSheet();
          },
        },
        [panel]
      );
      okBtn.addEventListener("click", () => {
        const value = (input.value || "").trim();
        if (!value) {
          toast("请输入箱号");
          return;
        }
        closeSheet();
        scanning = false;
        setResult(value, true);
        selectCage({ cageId: value, projectId: state.projectId });
      });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") okBtn.click();
      });
      document.body.appendChild(sheetEl);
      setTimeout(() => input.focus(), 50);
    }

    function showHelp() {
      closeSheet();
      const panel = h("div", { class: "scan-help-panel" }, [
        h("h3", {}, "使用帮助"),
        h("ol", {}, [
          h("li", {}, "将箱号二维码对准绿色取景框"),
          h("li", {}, "保持稳定，系统会自动识别"),
          h("li", {}, "光线不足时可点「开灯」"),
          h("li", {}, "也可从相册选择图片，或手动输入箱号"),
        ]),
        h(
          "button",
          { class: "btn primary", type: "button", onClick: closeSheet },
          "知道了"
        ),
      ]);
      sheetEl = h(
        "div",
        {
          class: "scan-help-sheet",
          onClick: (e) => {
            if (e.target === sheetEl) closeSheet();
          },
        },
        [panel]
      );
      document.body.appendChild(sheetEl);
    }

    async function selectCage(parsed) {
      const cage = parsed.cageId;
      if (!/^[A-Za-z0-9._-]{1,64}$/.test(cage)) {
        toast("箱号格式不合法");
        setResult("等待识别…", false);
        scanning = true;
        if (detector) requestAnimationFrame(loop);
        return;
      }
      if (parsed.projectId) state.projectId = parsed.projectId;
      let box = null;
      try {
        box = await api.box(cage);
      } catch (err) {
        if (err.status === 404) {
          if (confirm(`箱号 ${cage} 尚未建立，是否新建？`)) {
            go(`/manage/new?cage=${encodeURIComponent(cage)}`);
            return;
          }
          // 允许临时使用（上传时后端会自动建箱）
        } else {
          toast(err.message);
          setResult("等待识别…", false);
          scanning = true;
          if (detector) requestAnimationFrame(loop);
          return;
        }
      }
      setCurrentBox({
        cageId: cage,
        strain: box ? box.strain : "其他",
        mouseNoPad: box ? box.mouse_no_pad : 2,
      });
      go("/mode");
    }

    return () => {
      scanning = false;
      closeSheet();
      applyTorch(false).catch(() => {});
      stopStream(stream);
    };
  }

  function parseQr(text) {
    try {
      const obj = JSON.parse(text);
      if (obj && obj.cage_id) return { cageId: String(obj.cage_id), projectId: obj.project_id };
    } catch (_) {}
    return { cageId: String(text).trim(), projectId: null };
  }

  /* ================================================================== *
   * 视图：录制中 (屏 3) — Canvas 720×1280 所见即所得
   * ================================================================== */
  async function viewRecord() {
    if (!state.currentBox) {
      go("/scan");
      return;
    }
    document.documentElement.classList.add("camera-mode", "record-light");
    const box = state.currentBox;
    // 录制模式与重量来源：必须在所有引用 recordMode/weightSource 的 DOM 构造之前计算
    // （manualPanel、weightSource 判断等都在下方同步执行，TDZ 要求先声明）。
    // 纯 app 化：无天平桥时只允许 manual（OCR 视频判定已彻底移除）。
    const scaleAvailable = ScaleBridge.detectNativeBridge();
    const recordMode = currentRecordMode();
    const weightSource = recordMode === "manual" ? "manual" : (scaleAvailable ? "native_ble" : "manual");
    const titleEl = h("h1", {}, `实时称重 · ${box.cageId}`);
    function setTitle(text) { titleEl.textContent = text; }
    const switchCamBtn = h(
      "button",
      {
        class: "action-text switch-cam",
        type: "button",
        hidden: true,
        title: "切换摄像头",
      },
      "切换"
    );
    const finishBtn = h(
      "button",
      {
        class: "action-text rt-finish-btn",
        type: "button",
        title: "完成本箱并上传录像",
      },
      "完成本箱"
    );
    const appbarRight = h("span", { class: "rt-appbar-right" }, [switchCamBtn, finishBtn]);

    // Hidden source video (camera decode). Visible canvas is what the user
    // sees and what MediaRecorder captures — same 720×1280 pixels. The video
    // is recorded as evidence (随 report 批次上报)；判定在本地 BLE 引擎完成。
    const video = h("video", {
      class: "camera-source",
      autoplay: "",
      muted: "",
      playsinline: "",
      "webkit-playsinline": "",
      "x5-playsinline": "",
    });
    const canvas = h("canvas", {
      class: "camera-canvas",
      width: String(CANVAS_W),
      height: String(CANVAS_H),
    });
    const ctx = canvas.getContext("2d", { alpha: false });

    const guides = h("div", { class: "weighing-guides", "aria-hidden": "true" }, [
      h("div", { class: "capture-guide mouse-guide" }, [h("span", {}, "小鼠称重区（秤盘）")]),
      h("div", { class: "framing-hint" }, "调整手机使两个区域都清晰"),
      h("div", { class: "capture-guide weight-guide" }, [h("span", {}, "体重读数区（显示屏）")]),
    ]);
    const viewport = h("div", { class: "capture-viewport" }, [video, canvas, guides]);
    // dev 模式角标：提示操作员正在采集读数（复用 pill-orange 风格）
    const viewportHostChildren = [viewport];
    if (DEV_MODE) {
      viewportHostChildren.push(
        h("div", {
          class: "pill pill-orange",
          style: "position:absolute;top:8px;right:8px;z-index:20;font-size:12px;padding:3px 8px;pointer-events:none",
        }, "DEV·采集中")
      );
    }
    const viewportHost = h("div", { class: "record-viewport-host" }, viewportHostChildren);

    // --- Realtime dock ---
    const stateDot = h("span", { class: "rt-state-dot" });
    const stateText = h("span", { class: "rt-state-text" }, "正在连接…");
    // 状态 pill：毛玻璃 + 状态色点 + 文字。CSS 提供样式，setState 切 class。
    const stateIndicator = h(
      "div",
      { class: "rt-state-pill" },
      [stateDot, stateText]
    );

    const weightValue = h("span", { class: "rt-weight-value" }, "--");
    const weightUnit = h("span", { class: "rt-weight-unit" }, "g");
    // 蓝牙天平源标签 + RSSI/广播间隔（仅 native_ble 模式可见）
    const scaleSourceLabel = h("div", { class: "rt-scale-source", hidden: true }, "蓝牙天平 K797");
    const scaleMeta = h("div", { class: "rt-scale-meta", hidden: true });
    const weightDisplay = h(
      "div",
      { class: "rt-weight-display", style: "text-align:center;margin:2px 0" },
      [weightValue, weightUnit, scaleSourceLabel, scaleMeta]
    );

    const qualityHints = h("div", {
      class: "rt-quality-hints",
      style: "text-align:center;color:var(--muted,#5f6368);font-size:13px;min-height:18px",
    });

    const retryBtn = h(
      "button",
      { class: "btn rt-btn-retry", type: "button", hidden: true },
      "重测"
    );
    const acceptBtn = h(
      "button",
      { class: "btn btn-p rt-btn-accept", type: "button", hidden: true },
      "确认"
    );
    const actionButtons = h(
      "div",
      { class: "rt-actions" },
      [retryBtn, acceptBtn]
    );

    const mouseCount = h(
      "div",
      { class: "rt-mouse-count" },
      "已记录 0 只"
    );

    // 手动模式：克数输入 + 确认本只（替代 BLE 自动读数）。iOS 输入框风格。
    const manualInput = h("input", {
      class: "ios-input manual-weight-input", type: "number", inputmode: "decimal",
      step: "0.1", min: "0", max: "6553.5", placeholder: "输入克数，如 25.4",
    });
    const manualSubmit = h("button", { class: "btn btn-p manual-submit-btn", type: "button" }, "确认本只");
    const manualPanel = h("div", { class: "manual-panel", hidden: recordMode !== "manual" },
      [manualInput, manualSubmit]);
    manualSubmit.addEventListener("click", () => {
      const val = parseFloat(manualInput.value);
      if (!isFinite(val) || val < 0 || val > 6553.5) {
        toast("请输入有效克数（0 ~ 6553.5）");
        return;
      }
      // 纯 app 化：交给本地控制器校验并生成记录（控制器内部发 'accepted' 事件
      // 驱动 mouseCount/weight 显示）。控制器校验失败（非 manual 模式 / 越界）→ toast。
      const rec = ctrl && ctrl.submitManual(val);
      if (rec) {
        manualInput.value = "";
      } else {
        toast("记录失败，请重试");
      }
    });
    manualInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); manualSubmit.click(); }
    });

    const dockChildren = [stateIndicator, weightDisplay, qualityHints, actionButtons, mouseCount];
    if (recordMode === "manual") dockChildren.push(manualPanel);
    const dock = h(
      "div",
      { class: "realtime-dock", style: "padding:8px 16px 16px" },
      dockChildren
    );

    const stage = h("div", { class: "camera-stage record-stage realtime-stage" }, [
      viewportHost,
      dock,
    ]);

    const reconnectOverlay = h(
      "div",
      {
        class: "rt-reconnect-overlay",
        hidden: true,
        style:
          "position:fixed;inset:0;z-index:200;background:rgba(0,0,0,.72);display:flex;align-items:center;justify-content:center;color:#fff;font-size:17px",
      },
      "连接断开，正在重连…"
    );

    const screen = h("div", { class: "screen camera-screen record-screen realtime-screen" }, [
      appbar("", {
        back: "/",
        titleNode: titleEl,
        right: appbarRight,
      }),
      stage,
      reconnectOverlay,
    ]);
    mount(screen);

    let stream = null;
    let canvasStream = null;
    let recorder = null;
    let chunks = [];
    let recording = false;
    let clockTimer = null;
    let startedAt = 0;
    let drawing = true;
    let drawHandle = null;
    let useCanvas = supportsLiveCanvasCapture();
    let lastSourceSize = null;
    let videoInputs = [];
    let currentDeviceId = null;
    let paintedReady = false;
    let viewportObserver = null;

    // Local-weigh controller state（纯 app 化：判定/记录/上报全部本地完成）
    let ctrl = null;                 // LocalWeigh.createController() 实例
    let scaleChannel = null;          // ScaleBridge.createScaleChannel() 实例（announce/post_match）
    let rtState = "connecting";       // 当前 UI 状态（沿用 STATE_LABELS）
    let announcedWeight = null;       // 当前候选确认克数（announced 态）
    let finished = false;             // 完成本箱已触发
    let abandoned = false;            // 离开页面（非完成）→ 不上报

    // Pixel-exact 9:16 layout within the host (excludes bottom dock chrome).
    function layoutViewport() {
      const sw = viewportHost.clientWidth;
      const sh = viewportHost.clientHeight;
      if (!sw || !sh) return;
      // Host padding is already inside client box; keep a small safety inset.
      const padX = 8;
      const padY = 8;
      const availW = Math.max(1, sw - padX * 2);
      const availH = Math.max(1, sh - padY * 2);
      const target = CANVAS_W / CANVAS_H;
      let w;
      let h;
      if (availW / availH > target) {
        h = availH;
        w = availH * target;
      } else {
        w = availW;
        h = availW / target;
      }
      viewport.style.width = Math.max(1, Math.floor(w)) + "px";
      viewport.style.height = Math.max(1, Math.floor(h)) + "px";
    }
    layoutViewport();
    if (typeof ResizeObserver === "function") {
      viewportObserver = new ResizeObserver(() => layoutViewport());
      viewportObserver.observe(viewportHost);
    } else {
      window.addEventListener("resize", layoutViewport);
    }

    function paintFrame() {
      if (!ctx) return false;
      try {
        const size = videoSourceSize(video, stream);
        if (!size) return false;
        // HAVE_CURRENT_DATA — a decoded frame is available to draw.
        if (video.readyState < 2) return false;
        lastSourceSize = size;
        const rect = centerCropSourceRect(size.width, size.height, CANVAS_W, CANVAS_H);
        if (!rect) return false;
        ctx.drawImage(
          video,
          rect.sx, rect.sy, rect.sw, rect.sh,
          0, 0, CANVAS_W, CANVAS_H
        );
        paintedReady = true;
        return true;
      } catch (_) {
        // Transient WebView draw failures: keep the loop alive.
        return false;
      }
    }

    function drawFrame() {
      if (!drawing) return;
      paintFrame();
      scheduleDraw();
    }

    function scheduleDraw() {
      if (!drawing) return;
      if (typeof video.requestVideoFrameCallback === "function") {
        drawHandle = video.requestVideoFrameCallback(() => drawFrame());
      } else {
        drawHandle = requestAnimationFrame(() => drawFrame());
      }
    }

    function stopDraw() {
      drawing = false;
      if (drawHandle != null) {
        if (typeof video.cancelVideoFrameCallback === "function") {
          try { video.cancelVideoFrameCallback(drawHandle); } catch (_) {}
        } else {
          cancelAnimationFrame(drawHandle);
        }
        drawHandle = null;
      }
    }

    function buildCaptureMeta(mode) {
      const settings = trackSettings(stream);
      return {
        client_version: CLIENT_VERSION,
        capture_mode: mode,
        source_width: (lastSourceSize && lastSourceSize.width) || settings.width || null,
        source_height: (lastSourceSize && lastSourceSize.height) || settings.height || null,
        canvas_width: CANVAS_W,
        canvas_height: CANVAS_H,
        viewport_width: viewport.clientWidth || null,
        viewport_height: viewport.clientHeight || null,
        stage_width: stage.clientWidth || null,
        stage_height: stage.clientHeight || null,
        facing_mode: settings.facingMode || null,
        frame_rate: settings.frameRate || null,
        user_agent: (navigator.userAgent || "").slice(0, 240),
      };
    }

    async function startCamera(deviceId) {
      paintedReady = false;
      stopStream(stream);
      stream = await openBackCamera(video, deviceId || undefined);
      const settings = trackSettings(stream);
      currentDeviceId = settings.deviceId || deviceId || null;
      lastSourceSize = videoSourceSize(video, stream);
      const facing = (settings.facingMode || "").toLowerCase();
      if (facing && facing !== "environment") {
        switchCamBtn.hidden = false;
        toast("未检测到后置摄像头，请切换");
      }
      try {
        videoInputs = await listVideoInputs();
        if (videoInputs.length > 1) switchCamBtn.hidden = false;
      } catch (_) {}
    }

    switchCamBtn.addEventListener("click", async () => {
      if (switchCamBtn.disabled) return;
      if (!videoInputs.length) {
        try { videoInputs = await listVideoInputs(); } catch (_) {}
      }
      const ids = videoInputs.map((d) => d.deviceId).filter(Boolean);
      if (!ids.length) {
        toast("未找到可切换的摄像头");
        return;
      }
      let idx = ids.indexOf(currentDeviceId);
      idx = (idx + 1) % ids.length;
      try {
        await startCamera(ids[idx]);
        toast("已切换摄像头");
      } catch (err) {
        toast("切换摄像头失败");
      }
    });

    // --- Background MediaRecorder (starts immediately, uploads on finish) ---
    function startBackgroundRecorder() {
      if (!useCanvas || !stream || !window.MediaRecorder || typeof canvas.captureStream !== "function") {
        toast("当前浏览器不支持网页录像，请更换浏览器后重试");
        return false;
      }
      // Require a successful paint — never record black/frozen frames.
      if (!paintFrame() || !paintedReady) {
        toast("画面未就绪，请稍候");
        return false;
      }
      chunks = [];
      try {
        canvasStream = canvas.captureStream(15);
      } catch (err) {
        toast("无法录制当前画面，请稍后重试");
        return false;
      }
      const mime = pickMime();
      if (!mime) {
        toast("当前浏览器不支持录像（MP4/WebM 均不可用），请更换浏览器后重试");
        return false;
      }
      const opts = { videoBitsPerSecond: 1500000, mimeType: mime };
      try {
        recorder = new MediaRecorder(canvasStream, opts);
      } catch (err2) {
        toast("无法启动录像，请更换浏览器后重试");
        return false;
      }
      recorder.addEventListener("dataavailable", (e) => {
        if (e.data && e.data.size) chunks.push(e.data);
      });
      recorder.addEventListener("stop", () => {
        clearInterval(clockTimer);
        // If the user navigated away without finishing, drop the recording.
        if (abandoned) return;
        const type = recorder.mimeType || mime || "video/mp4";
        const blob = new Blob(chunks, { type });
        const durationSec = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
        // Freeze meta before stopping the camera track (never refresh geometry
        // after stopStream zeros videoWidth).
        const meta = buildCaptureMeta("realtime");
        stopStream(stream);
        stream = null;
        if (canvasStream) {
          canvasStream.getTracks().forEach((t) => t.stop());
          canvasStream = null;
        }
        // 纯 app 化：视频证据随本地称重批次上报（不再 POST /api/jobs 做 OCR）。
        finishBoxFlow(blob, `mv-${Date.now()}.${extForMime(type)}`, durationSec, {
          captureMode: "realtime",
          captureMeta: meta,
        });
      });
      // No timeslice: one complete container on stop() (Android fMP4 pitfall).
      recorder.start();
      recording = true;
      startedAt = Date.now();
      return true;
    }

    // --- UI 辅助（沿用既有 DOM 钩子与样式） ---

    function speakWeight(weight) {
      try {
        if (!("speechSynthesis" in window)) return;
        const u = new SpeechSynthesisUtterance(`${Number(weight).toFixed(2)}克`);
        u.lang = "zh-CN";
        u.rate = 1.0;
        window.speechSynthesis.cancel();
        window.speechSynthesis.speak(u);
      } catch (_) {}
    }

    function setWeightValue(value, confirmed) {
      if (value == null) {
        weightValue.textContent = "--";
        weightValue.style.color = "var(--label2)";
        weightUnit.style.color = "var(--label2)";
      } else {
        weightValue.textContent = Number(value).toFixed(2);
        // Confirmed (announced) = green; live/unconfirmed = label (dark).
        const c = confirmed ? "var(--green)" : "var(--label)";
        weightValue.style.color = c;
        weightUnit.style.color = c;
      }
    }

    function setQualityHints(lines) {
      qualityHints.innerHTML = "";
      if (!lines || !lines.length) return;
      lines.forEach((txt) => {
        qualityHints.appendChild(h("div", { class: "rt-hint" }, String(txt)));
      });
    }

    const STATE_LABELS = {
      connecting: "正在连接…",
      calibrating: "校准中",
      armed: "待称重",
      weighing: "称重中",
      announced: "请确认",
      wait_clear: "等待清场",
      accepted: "已记录",
      manual: "手动录入",
      retry_requested: "正在重测…",
    };
    const STATE_COLORS = {
      connecting: "var(--gray)",
      calibrating: "var(--orange)",
      armed: "var(--blue)",
      weighing: "var(--blue)",
      announced: "var(--green)",
      wait_clear: "var(--orange)",
      accepted: "var(--green)",
      manual: "var(--blue)",
      retry_requested: "var(--orange)",
    };

    function setState(newState, msg) {
      msg = msg || {};
      rtState = newState;
      stateText.textContent = STATE_LABELS[newState] || newState;
      stateDot.style.background = STATE_COLORS[newState] || "#9aa0a6";

      const showGuides = newState === "connecting" || newState === "calibrating";
      guides.style.display = showGuides ? "" : "none";

      // announce 模式才显示确认/重测按钮；post_match 自动 accept、manual 无按钮。
      const showActions = newState === "announced";
      retryBtn.hidden = !showActions;
      acceptBtn.hidden = !showActions;

      switch (newState) {
        case "calibrating":
          setQualityHints(["请确保秤盘空载，等待校准"]);
          break;
        case "armed":
          setQualityHints(["请将小鼠放上秤盘"]);
          break;
        case "weighing":
          setQualityHints([]);
          break;
        case "announced":
          setQualityHints([]);
          break;
        case "wait_clear":
          setQualityHints(["请取走小鼠"]);
          break;
        case "accepted":
          setQualityHints([]);
          if (announcedWeight != null) {
            weightDisplay.style.transition = "transform .15s ease";
            weightDisplay.style.transform = "scale(1.15)";
            setTimeout(() => { weightDisplay.style.transform = "scale(1)"; }, 200);
          }
          break;
        case "manual":
          setQualityHints([]);
          break;
      }
    }

    /* --- BLE 读数显示（一位小数）：raw=0 → "0.0"。
     * 控制器 'weight' 事件驱动；announced 态由引擎确认值（2 位小数）覆盖。 */
    function setBleWeightDisplay(grams) {
      if (grams == null || typeof grams !== "number" || !isFinite(grams)) {
        weightValue.textContent = "--";
        weightValue.style.color = "var(--label2)";
        weightUnit.style.color = "var(--label2)";
        return;
      }
      weightValue.textContent = grams.toFixed(1);
      weightValue.style.color = "var(--label)";
      weightUnit.style.color = "var(--label)";
    }

    function updateScaleMeta(reading) {
      if (!reading) return;
      const parts = [];
      if (typeof reading.rssi === "number") parts.push(`${reading.rssi} dBm`);
      const st = scaleChannel && scaleChannel.getState();
      if (st && st.lastReadingAtMs) {
        const ageSec = Math.max(0, Math.round((performance.now() - st.lastReadingAtMs) / 1000));
        parts.push(`${ageSec}s 前`);
      }
      scaleMeta.textContent = parts.join(" · ");
      scaleMeta.hidden = false;
    }

    function showScaleStaleHint(stale) {
      if (!stale) return;
      setQualityHints(["天平广播中断"]);
    }

    /* ================================================================== *
     * 本地称重控制器事件 → UI 对接
     * 控制器（LocalWeigh.createController）封装了 WeighEngine + 记录 + 草稿 +
     * outbox；这里只把控制器事件映射到既有 DOM 更新函数。
     * ================================================================== */
    function handleLocalWeighEvent(type, payload) {
      payload = payload || {};
      if (type === "state") {
        // calibrating/armed/weighing/announced/wait_clear/manual
        setState(payload.state || rtState, {});
        return;
      }
      if (type === "weight") {
        // BLE 直读（每次有效读数）。announced 态保留引擎确认值，不被直读覆盖。
        if (rtState !== "announced" && rtState !== "accepted") {
          setBleWeightDisplay(payload.grams);
        }
        return;
      }
      if (type === "announce") {
        // 引擎判定稳定 → 候选重量。announce 模式：弹确认/重测 + 语音；
        // post_match 模式：控制器内部已自动 accept，这里只刷新数字。
        announcedWeight = payload.weight_g;
        setWeightValue(announcedWeight, true);
        setState("announced", {});
        return;
      }
      if (type === "accepted") {
        // 已确认一只（announce 人工 / post_match 自动 / manual 录入）
        const count = payload.count != null ? payload.count : 0;
        mouseCount.textContent = `已记录 ${count} 只`;
        // 数字闪一下 + 切到 accepted 瞬态（ready_next 会再切回 armed/calibrating）
        if (announcedWeight != null) {
          setWeightValue(announcedWeight, true);
        }
        setState("accepted", {});
        return;
      }
      if (type === "ready_next") {
        // 清秤完成，准备下一只：隐藏按钮，候选清空
        announcedWeight = null;
        retryBtn.hidden = true;
        acceptBtn.hidden = true;
        return;
      }
      if (type === "stale") {
        showScaleStaleHint(payload.stale);
        return;
      }
      if (type === "draft_resumed") {
        const count = payload.count != null ? payload.count : 0;
        if (count > 0) {
          mouseCount.textContent = `已记录 ${count} 只`;
          toast(`已恢复 ${count} 只未完成记录`);
        }
        return;
      }
      // 'recorded' 等其它事件此处不需要额外 UI（accepted 已覆盖）
    }

    /* --- 按钮事件：直接转发给本地控制器（控制器内部守卫状态）--- */
    retryBtn.addEventListener("click", () => {
      if (rtState !== "announced") return;
      if (!ctrl) return;
      retryBtn.disabled = true;
      retryBtn.textContent = "正在重测…";
      const r = ctrl.retry();
      // retry 后控制器会经 engine 事件回到 weighing；这里恢复按钮可用
      setTimeout(() => {
        retryBtn.disabled = false;
        retryBtn.textContent = "重测";
      }, 300);
      if (!r || !r.applied) {
        toast("当前无法重测，请稍后再试");
      } else {
        announcedWeight = null;
      }
    });
    acceptBtn.addEventListener("click", () => {
      if (rtState !== "announced") return;
      if (!ctrl) return;
      const acc = ctrl.accept();
      if (!acc) {
        toast("确认失败，请重试");
      }
    });

    /* ================================================================== *
     * 蓝牙天平通道（纯 app 化：仅本地读取，不再转发 WS）。
     * announce/post_match 模式才创建；manual 模式不连天平。
     * 控制器内部通过 scaleChannel.onReading 订阅读数 → engine.ingestReading，
     * 因此这里只负责：直读显示（announced 态除外）、元信息刷新、stale 提示、
     * 设备选择自愈、原生状态同步。读数喂给控制器由 createController 时注入的
     * scaleChannel 自动完成（控制器 start() 内部订阅）。
     * ================================================================== */
    function startScaleChannel() {
      if (scaleChannel || weightSource !== "native_ble") return;
      // 录制期间由本视图独占 BLE 扫描；暂停首页的全局连接通道，避免双扫描冲突。
      if (scaleConn.channel) disconnectScale();
      const saved = loadSavedScaleDevice();
      const channelOpts = saved ? { deviceId: saved.deviceId } : {};
      if (!saved && ScaleBridge.detectDeviceSupport()) {
        setQualityHints(["未选择天平，请回首页连接并选定设备"]);
      }
      scaleChannel = ScaleBridge.createScaleChannel(channelOpts);
      // 新读数 → 直显（announced/accepted 态除外，由引擎确认值驱动）+ 元信息。
      // 注意：控制器在 createController 时已拿到同一个 scaleChannel 引用，并在
      // start() 内部订阅 onReading 做 engine.ingestReading + 发 'weight' 事件。
      // 这里再订阅一次仅用于"非 announced 态的直读大数字显示 + RSSI 元信息"，
      // 不会造成双引擎（引擎由控制器独占创建）。
      scaleChannel.onReading(function (reading) {
        if (rtState !== "announced" && rtState !== "accepted") {
          const fmt = ScaleBridge.formatScaleDisplay(scaleChannel.getState());
          if (!fmt.stale) setBleWeightDisplay(reading.grams);
        }
        updateScaleMeta(reading);
      });
      scaleChannel.onStaleChange(function (isStale) {
        if (isStale && rtState !== "announced" && rtState !== "accepted") {
          showScaleStaleHint(true);
        }
      });
      scaleChannel.onStatus(function (detail) {
        const bad = detail.state === "unauthorized" || detail.state === "bluetooth_off" ||
          detail.state === "error";
        if (bad && detail.message) {
          stateText.textContent = detail.message;
        }
      });
      scaleChannel.onDevices(function (norm) {
        reconcileScaleSelection(scaleChannel, norm);
      });
      scaleSourceLabel.hidden = false;
      scaleChannel.start();
      // 立即同步一次原生状态
      try {
        const native = window.MiceAutomaticScale;
        if (native && typeof native.getScaleStatus === "function") {
          const raw = native.getScaleStatus();
          if (raw) {
            let parsed; try { parsed = JSON.parse(raw); } catch (_) { parsed = null; }
            if (parsed) {
              window.dispatchEvent(new CustomEvent("miceautomatic:scale-status", { detail: parsed }));
            }
          }
        }
      } catch (_) {}
    }

    function stopScaleChannel() {
      if (scaleChannel) {
        try { scaleChannel.stop(); } catch (_) {}
        scaleChannel = null;
      }
    }

    /* --- 构造本地称重控制器（判定/记录/草稿/outbox 全本地）--- */
    function createLocalController() {
      // manual 模式无天平通道；announce/post_match 用 startScaleChannel 创建的通道。
      const channelForCtrl = recordMode === "manual" ? null : scaleChannel;
      return LocalWeigh.createController({
        mode: recordMode,
        weighEngine: WeighEngine,
        // 真秤实测：放鼠有稳定瞬态，需稳定判定持续满 ~0.8s 真实时间才播报，
        // 否则重量还在爬升/抖动就确认（最小稳定时长门槛，仅 App 运行时设置，
        // 引擎默认 stable_min_span_ms=0 不变、不影响单测）。
        engineConfig: { stable_min_span_ms: 800 },
        scaleChannel: channelForCtrl,
        outbox: reportOutbox,
        box: { cageId: box.cageId, strain: box.strain },
        deviceId: scaleConn.selectedDeviceId || "scale01",
        projectId: state.projectId,
        weightSource: weightSource === "native_ble" ? "ble_k797" : weightSource,
        storage: localStorage,
        buildRecord: ReportClient.buildRecord,
        // 当前录像相对毫秒（用于 accept 时记 clip_start_ms，供服务端抽帧）
        videoTimeMs: function () { return startedAt > 0 ? Math.max(0, Date.now() - startedAt) : 0; },
        speak: recordMode === "announce" ? speakWeight : null,
        onEvent: handleLocalWeighEvent,
        // dev 模式：采集完整天平读数时间序列，随记录上报
        collectReadings: DEV_MODE,
      });
    }

    /* --- 完成本箱：停止录像 → ctrl.finishBox(videoBlob) → outbox 入队 → 上报 UI ---
     * 视频作为证据随 report 批次走（不再 POST /api/jobs 做 OCR，避免重复计数）。
     * 上报状态：已上报 N 只 / 待补传 M 批（离线）。 */
    function finishBoxFlow(blob, filename, durationSec, uploadOpts) {
      stopDraw();
      stopStream(stream);
      stream = null;
      setTitle("上报中");

      // 1) 控制器停止订阅读数（避免 finishBox 后再有 accepted 事件）
      if (ctrl) { try { ctrl.stop(); } catch (_) {} }
      // 2) 累积批次入队 outbox（含视频证据 Blob）→ 返回 {count, batchId}
      //    dev 模式：若控制器采集了读数，附进上报（可序列化、可离线补传）
      let result = null;
      let enqueueErr = null;
      try {
        let readingsPayload = null;
        if (DEV_MODE && ctrl && typeof ctrl.getReadingsPayload === "function") {
          try { readingsPayload = ctrl.getReadingsPayload(); } catch (_) {}
        }
        result = ctrl ? ctrl.finishBox(blob, readingsPayload) : { count: 0, batchId: null };
      } catch (e) {
        enqueueErr = e;
      }
      // 3) 触发立即补传（在线即发；离线由 outbox 退避重试 / online 事件补传）
      const flushP = reportOutbox.flush().catch(function () {});

      renderReportUploading(box, result, reportOutbox.pending(), durationSec, uploadOpts || {});

      flushP.then(function (fr) {
        const sent = (fr && typeof fr.sent === "number") ? fr.sent : 0;
        const remaining = reportOutbox.pending();
        updateReportUploadingDone(box, result, sent, remaining, durationSec, enqueueErr);
      });
    }

    finishBtn.addEventListener("click", function () {
      if (finished) return;
      finished = true;
      finishBtn.disabled = true;
      // 停止控制器订阅；停止 BLE 通道（录像停止由 recorder.stop 触发 finishBoxFlow）
      if (ctrl) { try { ctrl.stop(); } catch (_) {} }
      stopScaleChannel();
      showReconnectOverlay(false);
      if (recorder && recording) {
        recording = false;
        try { recorder.stop(); } catch (_) {} // → finishBoxFlow
      } else {
        // 无录像（不应发生，但兜底）：直接 finishBox（无视频证据）
        finishBoxFlow(null, `mv-${Date.now()}.mp4`, 0, {});
      }
    });

    function disableRealtime(reason) {
      useCanvas = false;
      paintedReady = false;
      stopDraw();
      stopScaleChannel();
      if (ctrl) { try { ctrl.stop(); } catch (_) {} ctrl = null; }
      stopStream(stream);
      stream = null;
      if (canvasStream) {
        canvasStream.getTracks().forEach((t) => t.stop());
        canvasStream = null;
      }
      switchCamBtn.hidden = true;
      finishBtn.disabled = true;
      stateText.textContent = "不可用";
      stateDot.style.background = "var(--red)";
      setQualityHints([reason || "当前浏览器无法进行实时称重，请更换浏览器后重试"]);
    }

    // --- 上报遮罩控制（复用 reconnectOverlay 节点，纯本地无重连语义）---
    function showReconnectOverlay(show) {
      reconnectOverlay.hidden = !show;
    }

    // --- Boot: 相机 → 预览绘制循环 → 后台录像 → 本地控制器 ---
    (async () => {
      if (!useCanvas) {
        disableRealtime("浏览器不支持网页录像，请更换浏览器后重试");
        return;
      }
      try {
        await startCamera();
        drawing = true;
        scheduleDraw();
        if (window.screen && window.screen.orientation && window.screen.orientation.lock) {
          window.screen.orientation.lock("portrait").catch(() => {});
        }
        // 后台录像是证据来源；无法启动则不进控制器（操作员会误以为在记录）。
        const recOk = startBackgroundRecorder();
        if (!recOk) {
          disableRealtime("无法启动后台录像，称重不可用。请更换浏览器或重试。");
          return;
        }
        // native_ble：先建通道再建控制器（控制器构造时需要 scaleChannel 引用）
        if (weightSource === "native_ble") startScaleChannel();
        ctrl = createLocalController();
        ctrl.start();
        // 初始状态：manual → manual；ble → calibrating（控制器 start 内会发 'state'）
        if (recordMode === "manual") {
          setState("manual", {});
        } else {
          setState("calibrating", {});
        }
      } catch (err) {
        disableRealtime("无法打开实时相机，请确认 HTTPS 与摄像头权限");
      }
    })();

    return () => {
      // 区分"离开页面"与"完成本箱"：仅前者丢弃录像（abandoned 抑制 recorder.stop 上报）
      if (!finished) abandoned = true;
      finished = true;
      document.documentElement.classList.remove("camera-mode", "record-light");
      clearInterval(clockTimer);
      stopDraw();
      if (ctrl) { try { ctrl.stop(); } catch (_) {} }
      stopScaleChannel();
      if (viewportObserver) {
        try { viewportObserver.disconnect(); } catch (_) {}
        viewportObserver = null;
      } else {
        window.removeEventListener("resize", layoutViewport);
      }
      if (window.screen && window.screen.orientation && window.screen.orientation.unlock) {
        try { window.screen.orientation.unlock(); } catch (_) {}
      }
      try { if ("speechSynthesis" in window) window.speechSynthesis.cancel(); } catch (_) {}
      if (recorder && recording) try { recorder.stop(); } catch (_) {}
      if (canvasStream) canvasStream.getTracks().forEach((t) => t.stop());
      stopStream(stream);
      // 录制期间为避免双扫描暂停了首页全局天平连接；BLE 模式下录制结束自动恢复，
      // 连续录多箱时回到首页无需重新点"连接天平"。自动恢复时若已有持久化选定设备，
      // 直接重连该设备（带 deviceId），不弹"选择天平"sheet。
      if (weightSource === "native_ble" && ScaleBridge.detectNativeBridge()) {
        const saved = loadSavedScaleDevice();
        if (saved) {
          scaleConn.state = "connecting";
          scaleConn.errorMsg = "";
          notifyScaleConn();
          scaleConn.channel = startScaleConnChannel(saved.deviceId);
        } else {
          connectScale();
        }
      }
    };
  }

  /* ================================================================== *
   * 视图：本箱上报（纯 app 化：本地记录 + outbox 离线补传，无 OCR 视频分析）
   * finishBoxFlow 在 ctrl.finishBox(videoBlob) 后调用：
   *   - renderReportUploading：展示"上报中"（含已记录只数 + 待补传批数）
   *   - updateReportUploadingDone：flush 完成后展示"已上报 N 只 / 待补传 M 批"
   * ================================================================== */
  function reportDoneCard(box, recordedCount, sent, remaining, enqueueErr) {
    const hasPending = remaining > 0;
    const titleText = enqueueErr
      ? "本地记录已保存（入队失败）"
      : (hasPending ? "已记录，等待联网补传" : "本箱已上报");
    const icon = hasPending || enqueueErr ? "↑" : "✓";
    const iconBg = hasPending || enqueueErr ? "var(--orange)" : "var(--green)";
    const subParts = [`${box.cageId} · 共 ${recordedCount} 只`];
    if (sent > 0) subParts.push(`已上报 ${sent} 批`);
    if (remaining > 0) subParts.push(`待补传 ${remaining} 批（离线）`);
    const card = h("div", { class: "card" }, [
      h("div", { class: "center-status" }, [
        h("div", { class: "check-circle", style: `background:${iconBg}` }, icon),
        h("strong", {}, titleText),
        h("p", { class: "li-sub" }, subParts.join(" · ")),
      ]),
    ]);
    if (enqueueErr) {
      card.appendChild(h("p", { class: "li-sub", style: "color:var(--red)" },
        `入队异常：${enqueueErr.message || enqueueErr}`));
    }
    if (hasPending) {
      card.appendChild(h("p", { class: "li-sub" },
        "联网后自动补传，可在本箱记录中查看结果。"));
    }
    return card;
  }

  function renderReportUploading(box, finishResult, pendingCount, durationSec, uploadOpts) {
    uploadOpts = uploadOpts || {};
    const recordedCount = (finishResult && typeof finishResult.count === "number")
      ? finishResult.count : 0;
    const screen = h("div", { class: "screen" });
    screen.appendChild(appbar("上报中", {}));
    const content = h("div", { class: "content" }, [
      h("div", { class: "card" }, [
        h("div", { class: "center-status" }, [
          h("div", { class: "spinner" }),
          h("strong", {}, "正在上报"),
          h("p", { class: "li-sub" }, `${box.cageId} · 共 ${recordedCount} 只`),
        ]),
      ]),
    ]);
    screen.appendChild(content);
    app.innerHTML = "";
    mount(screen);
  }

  function updateReportUploadingDone(box, finishResult, sent, remaining, durationSec, enqueueErr) {
    const recordedCount = (finishResult && typeof finishResult.count === "number")
      ? finishResult.count : 0;
    const screen = h("div", { class: "screen" });
    screen.appendChild(appbar("本箱完成", {}));
    const card = reportDoneCard(box, recordedCount, sent, remaining, enqueueErr);
    const content = h("div", { class: "content" }, [
      card,
      h("button", {
        class: "btn ghost",
        onClick: () => go(`/box/${encodeURIComponent(box.cageId)}`),
      }, "查看本箱记录"),
      h("button", { class: "btn btn-p", onClick: () => go("/mode") }, "继续录制下一只"),
    ]);
    screen.appendChild(content);
    app.innerHTML = "";
    mount(screen);
  }

  /* ================================================================== *
   * 视图：上传 + 完成 / 排队 (屏 4) — 旧 OCR 视频分析流程（保留，viewRecord 不再调用）
   * ================================================================== */
  function renderUploading(box, blob, filename, durationSec, uploadOpts) {
    uploadOpts = uploadOpts || {};
    const screen = h("div", { class: "screen" });
    screen.appendChild(appbar("上传视频", {}));
    const bar = h("span");
    const pct = h("b", {}, "0%");
    const text = h("span", {}, "正在上传视频…");
    const content = h("div", { class: "content" }, [
      h("div", { class: "card" }, [
        h("div", { class: "center-status" }, [
          h("div", { class: "spinner" }),
          h("strong", {}, "上传中"),
          h("p", { class: "li-sub" }, `${box.cageId} · ${fmtBytes(blob.size)}`),
        ]),
        h("div", { class: "progress-track" }, [bar]),
        h("div", { class: "progress-copy" }, [text, pct]),
      ]),
    ]);
    screen.appendChild(content);
    app.innerHTML = "";
    mount(screen);

    uploadVideo(blob, filename, box, (p) => {
      const v = Math.round(p * 100);
      bar.style.width = v + "%";
      pct.textContent = v + "%";
      if (v >= 100) text.textContent = "上传完成，正在入队…";
    }, durationSec, uploadOpts)
      .then((job) => {
        state.activeJobId = job.job_id;
        go("/done");
      })
      .catch((err) => {
        toast(err.message);
        content.innerHTML = "";
        content.appendChild(
          h("div", { class: "card" }, [
            h("div", { class: "center-status" }, [
              h("strong", {}, "上传失败"),
              h("p", { class: "li-sub" }, err.message),
            ]),
            h("button", { class: "btn primary", onClick: () => renderUploading(box, blob, filename, durationSec, uploadOpts) }, "重试上传"),
            h("button", { class: "btn ghost", onClick: () => go("/record") }, "重新录制"),
          ])
        );
      });
  }

  async function viewDone() {
    if (!state.activeJobId) {
      go("/");
      return;
    }
    const jobId = state.activeJobId;
    const box = state.currentBox || { cageId: "-" };
    const screen = h("div", { class: "screen" });
    screen.appendChild(appbar("录制完成", {}));

    const statusIcon = h("div", { class: "spinner" });
    const statusTitle = h("strong", {}, "视频已上传，正在排队…");
    const statusSub = h("p", { class: "li-sub" }, box.cageId);
    const posEl = h("strong", {}, "--");
    const waitEl = h("strong", {}, "--:--");
    const queueBox = h("div", { class: "queue-box" }, [
      h("div", {}, [h("small", {}, "当前排队"), posEl]),
      h("div", {}, [h("small", {}, "预计等待"), waitEl]),
    ]);
    const card = h("div", { class: "card" }, [
      h("div", { class: "center-status" }, [statusIcon, statusTitle, statusSub]),
      queueBox,
    ]);
    const content = h("div", { class: "content" }, [
      card,
      h("button", { class: "btn ghost", onClick: () => go(`/box/${encodeURIComponent(box.cageId)}`) }, "查看本箱记录"),
      h("button", { class: "btn primary", onClick: () => go("/record") }, "继续录制下一只"),
    ]);
    screen.appendChild(content);
    mount(screen);

    let alive = true;
    async function poll() {
      if (!alive) return;
      try {
        const job = await api.job(jobId);
        if (job.status === "completed") {
          const n = job.record_count || 0;
          statusIcon.replaceWith(h("div", { class: "check-circle" }, "✓"));
          statusTitle.textContent = n > 0
            ? `分析完成，共检出 ${n} 只`
            : "未检出小鼠";
          statusSub.textContent = n > 0 ? box.cageId : `${box.cageId} · 可重新录制`;
          queueBox.hidden = true;
          // Zero-detect: show the backend analysis frame so the operator can
          // verify framing (mice / LCD) against what was analysed.
          if (n === 0 && job.analysis_preview_url) {
            const img = h("img", {
              class: "analysis-preview",
              alt: "分析预览",
            });
            const previewWrap = h("div", {}, [
              h("p", { class: "li-sub" }, "后端实际分析画面："),
              img,
            ]);
            card.appendChild(previewWrap);
            // Fetch with API token — <img src> cannot send the header.
            apiFetch(job.analysis_preview_url)
              .then((res) => (res.ok ? res.blob() : Promise.reject()))
              .then((blob) => {
                img.src = URL.createObjectURL(blob);
              })
              .catch(() => {
                previewWrap.appendChild(
                  h("p", { class: "li-sub" }, "分析预览加载失败")
                );
              });
          }
          return;
        }
        if (job.status === "failed") {
          const isFormatErr = !!(job.message || "").includes("视频格式异常");
          statusIcon.replaceWith(h("div", { class: "check-circle", style: "background:#dc3545" }, "!"));
          statusTitle.textContent = isFormatErr ? "录像可能损坏" : "分析失败";
          statusSub.textContent = isFormatErr
            ? "录像可能损坏，请用本页重录一次"
            : (job.error || "");
          queueBox.hidden = true;
          return;
        }
        const wait = await api.jobWait(jobId);
        if (job.status === "processing") {
          statusTitle.textContent = "正在分析…";
          posEl.textContent = "分析中";
        } else {
          statusTitle.textContent = "视频已上传，正在排队…";
          posEl.textContent = wait.position ? `第 ${wait.position} 位` : "-";
        }
        waitEl.textContent = fmtWait(wait.estimated_wait_sec);
      } catch (_) {}
      if (alive) setTimeout(poll, 2000);
    }
    poll();
    return () => { alive = false; };
  }

  /* ================================================================== *
   * 视图：本箱记录 (屏 5)
   * ================================================================== */
  async function viewBoxRecords(params) {
    const cage = params.cageId;
    const screen = h("div", { class: "screen" });
    let strain = "";
    let box = null;

    screen.appendChild(
      appbar(cage, {
        back: "/manage",
        right: h("button", { class: "iconbtn", onClick: () => showQr(cage) }, "▦"),
      })
    );
    const countEl = h("span", { class: "count-pill" }, "");
    // strain 用引用保留，await 拿到箱信息后再更新文本（先挂骨架再取数，避免白屏）。
    const strainEl = h("div", { class: "strain-sub" }, "其他");
    const subHead = h("div", { class: "content", style: "padding-bottom:0" }, [
      h("div", { class: "section-head" }, [strainEl, countEl]),
    ]);
    screen.appendChild(subHead);
    const listWrap = h("div", { class: "list" }, [h("div", { class: "empty" }, "加载中…")]);
    const content = h("div", { class: "content with-dock" }, [h("div", { class: "card" }, [listWrap])]);
    screen.appendChild(content);
    screen.appendChild(
      h("div", { class: "dock" }, [
        h("button", {
          class: "btn primary",
          onClick: () => {
            setCurrentBox({ cageId: cage, strain: strain || "其他", mouseNoPad: box ? box.mouse_no_pad : 2 });
            // 与首页「开始录制」一致：先选记录模式（后匹配/即时报数/手动）再进录制。
            go("/mode");
          },
        }, "继续录制"),
      ])
    );
    // 先挂载骨架，再 await 网络——否则 render() 已清空屏幕，FRP 慢/挂起时整页白屏
    // （真机「查看本箱记录」白屏卡死的根因）。
    mount(screen);

    try {
      box = await api.box(cage);
      strain = box.strain || "";
      strainEl.textContent = strain || "其他";
    } catch (_) {}

    try {
      const data = await api.boxRecords(cage);
      const pad2 = box ? box.mouse_no_pad : 2;
      listWrap.innerHTML = "";
      const done = data.items.filter((i) => i.status === "completed" && i.record_id).length;
      countEl.textContent = `共 ${done} 只`;
      if (!data.items.length) {
        listWrap.appendChild(h("div", { class: "empty" }, "本箱还没有记录"));
      } else {
        data.items.forEach((it) => listWrap.appendChild(recordRow(it, pad2)));
      }
    } catch (err) {
      listWrap.innerHTML = "";
      listWrap.appendChild(h("div", { class: "empty" }, err.message));
    }
  }

  function recordRow(it, pad2) {
    const ordinal = it.actual_ordinal || it.requested_ordinal;
    const clickable = it.status === "completed" && it.record_id;
    const thumb = it.photo_url
      ? h("img", { class: "thumb", src: it.photo_url + "?size=thumb", loading: "lazy" })
      : h("div", { class: "thumb placeholder" }, "🐭");
    const title =
      it.status === "completed" && it.weight != null
        ? h("div", { class: "li-weight" }, `${Number(it.weight).toFixed(2)} g`)
        : h("div", { class: "li-title" }, `第 ${pad(ordinal, pad2)} 只`);
    const subParts = [];
    if (it.status === "completed" && it.record_id)
      subParts.push(`第 ${pad(ordinal, pad2)} 只 · ${fmtTime(it.created_at)}`);
    else subParts.push(fmtTime(it.created_at));
    const main = h("div", { class: "li-main" }, [
      title,
      h("div", { class: "li-sub" }, subParts.join("")),
      it.warning === "no_detection"
        ? h("div", { class: "warn-note" }, "未检出小鼠，可重录")
        : it.warning === "multi_detected"
        ? h("div", { class: "warn-note" }, "同段检出多只")
        : it.warning === "format_error"
        ? h("div", { class: "warn-note" }, "录像可能损坏，请重录")
        : it.warning === "analysis_failed"
        ? h("div", { class: "warn-note" }, "分析失败，可重试")
        : null,
    ]);
    return h(
      "div",
      {
        class: "list-item",
        onClick: clickable ? () => go(`/mouse/${encodeURIComponent(it.record_id)}`) : null,
      },
      [thumb, main, badge(it.status)]
    );
  }

  /* ================================================================== *
   * 视图：小鼠详情 (屏 6)
   * ================================================================== */
  async function viewMouse(params) {
    const id = params.recordId;
    const screen = h("div", { class: "screen" });
    screen.appendChild(appbar("小鼠详情", { back: true }));
    const content = h("div", { class: "content" }, [h("div", { class: "empty" }, "加载中…")]);
    screen.appendChild(content);
    mount(screen);

    try {
      const m = await api.record(id);
      content.innerHTML = "";
      content.appendChild(
        h("img", { class: "detail-media", src: m.photo_url, alt: "稳定帧" })
      );
      const card = h("div", { class: "card" }, [
        kv("箱号", m.cage_id),
        kv("小鼠编号", `第 ${pad(m.actual_ordinal || m.ordinal, 2)} 只`),
        kv("体重", m.weight != null ? `${Number(m.weight).toFixed(2)} g` : "-"),
        kv("称重时间", fmtTime(m.timestamp)),
        kv("分析状态", "已完成"),
        kv("置信度", m.confidence != null ? Number(m.confidence).toFixed(3) : "-"),
      ]);
      content.appendChild(card);
      // 删除按钮：Phase 2 默认隐藏（角色权限见 design §6.6）
    } catch (err) {
      content.innerHTML = "";
      content.appendChild(h("div", { class: "empty" }, err.message));
    }
  }
  function kv(k, v) {
    return h("div", { class: "kv" }, [h("span", { class: "k" }, k), h("span", { class: "v" }, String(v))]);
  }

  /* ================================================================== *
   * 视图：箱子管理 (屏 7)
   * ================================================================== */
  async function viewManage() {
    const screen = h("div", { class: "screen" });
    screen.appendChild(
      appbar("箱子管理", {
        back: "/",
        right: h("button", { class: "action-text", onClick: () => go("/manage/new") }, "+ 新建"),
      })
    );
    const tabsWrap = h("div", { class: "tabs" });
    const listWrap = h("div", { class: "list" }, [h("div", { class: "empty" }, "加载中…")]);
    const content = h("div", { class: "content" }, [tabsWrap, h("div", { class: "card" }, [listWrap])]);
    screen.appendChild(content);
    mount(screen);

    const tabs = [
      { key: "", label: "全部" },
      { key: "C57BL/6", label: "C57BL/6" },
      { key: "BALB/c", label: "BALB/c" },
      { key: "其他", label: "其他" },
    ];
    let active = "";
    function renderTabs() {
      tabsWrap.innerHTML = "";
      tabs.forEach((t) => {
        tabsWrap.appendChild(
          h("button", {
            class: "tab" + (t.key === active ? " active" : ""),
            onClick: () => { active = t.key; renderTabs(); load(); },
          }, t.label)
        );
      });
    }
    async function load() {
      listWrap.innerHTML = "";
      listWrap.appendChild(h("div", { class: "empty" }, "加载中…"));
      try {
        const data = await api.boxes(active || undefined);
        listWrap.innerHTML = "";
        if (!data.items.length) {
          listWrap.appendChild(h("div", { class: "empty" }, "没有箱子，点击右上角新建"));
        } else {
          data.items.forEach((b) => listWrap.appendChild(boxRow(b)));
        }
      } catch (err) {
        listWrap.innerHTML = "";
        listWrap.appendChild(h("div", { class: "empty" }, err.message));
      }
    }
    renderTabs();
    load();
  }

  function boxRow(b) {
    const count = (b.record_count || 0) + (b.pending_count || 0);
    return h(
      "div",
      { class: "list-item", onClick: () => go(`/box/${encodeURIComponent(b.cage_id)}`) },
      [
        h("div", { class: "li-main" }, [
          h("div", { class: "li-title" }, b.cage_id),
          h("div", { class: "li-sub" }, `${b.strain} · ${fmtTime(b.created_at)}`),
        ]),
        h("span", { class: "count-pill" }, `${count} 只`),
      ]
    );
  }

  /* ================================================================== *
   * 视图：新建箱子 (屏 8)
   * ================================================================== */
  async function viewBoxNew() {
    const q = new URLSearchParams(location.search);
    const prefill = q.get("cage") || "";
    const screen = h("div", { class: "screen" });

    const cageInput = h("input", { value: prefill, maxlength: "64", placeholder: "请输入箱号", autocomplete: "off" });
    const strainSel = h("select", {}, [
      h("option", { value: "C57BL/6" }, "C57BL/6"),
      h("option", { value: "BALB/c" }, "BALB/c"),
      h("option", { value: "其他" }, "其他"),
    ]);
    const notesInput = h("textarea", { placeholder: "可选备注信息", maxlength: "500" });

    let pad = 2;
    const chipDefs = [
      { pad: 2, label: "01" },
      { pad: 3, label: "001" },
      { pad: 0, label: "自定义" },
    ];
    const chips = h("div", { class: "chips" });
    const customInput = h("input", { type: "number", min: "1", placeholder: "起始值", hidden: true });
    function renderChips() {
      chips.innerHTML = "";
      chipDefs.forEach((c) => {
        chips.appendChild(
          h("button", {
            class: "chip" + ((c.pad === pad || (c.pad === 0 && pad === 0)) ? " active" : ""),
            onClick: () => { pad = c.pad; customInput.hidden = c.pad !== 0; renderChips(); },
          }, c.label)
        );
      });
    }
    renderChips();

    const saveBtn = h("button", { class: "action-text", onClick: save }, "保存");
    screen.appendChild(appbar("新建箱子", { back: "/manage", right: saveBtn }));
    screen.appendChild(
      h("div", { class: "content" }, [
        h("div", { class: "card" }, [
          h("div", { class: "field" }, [h("label", {}, "箱号"), cageInput]),
          h("div", { class: "field" }, [h("label", {}, "品系"), strainSel]),
          h("div", { class: "field" }, [h("label", {}, "备注"), notesInput]),
          h("div", { class: "field" }, [
            h("label", {}, "默认小鼠编号起始格式"),
            chips,
            h("div", { style: "margin-top:10px" }, [customInput]),
          ]),
        ]),
      ])
    );
    mount(screen);

    async function save() {
      const cage = cageInput.value.trim();
      if (!/^[A-Za-z0-9._-]{1,64}$/.test(cage)) {
        toast("箱号仅支持字母数字点横线下划线");
        return;
      }
      let mouse_no_pad = pad || 2;
      let mouse_no_start = 1;
      if (pad === 0) {
        mouse_no_start = Math.max(1, parseInt(customInput.value || "1", 10));
        mouse_no_pad = String(customInput.value || "1").length || 1;
      }
      saveBtn.disabled = true;
      try {
        await api.createBox({
          cage_id: cage,
          strain: strainSel.value,
          notes: notesInput.value.trim(),
          project_id: state.projectId,
          mouse_no_start,
          mouse_no_pad,
        });
        toast("已创建");
        renderBoxCreated(cage);
      } catch (err) {
        toast(err.message);
        saveBtn.disabled = false;
      }
    }
  }

  function renderBoxCreated(cage) {
    const screen = h("div", { class: "screen" });
    screen.appendChild(appbar("箱子已创建", {}));
    screen.appendChild(
      h("div", { class: "content" }, [
        h("div", { class: "card" }, [
          h("div", { class: "center-status" }, [
            h("div", { class: "check-circle" }, "✓"),
            h("strong", {}, cage),
            h("p", { class: "li-sub" }, "扫此码即可选箱录制，可打印贴在箱上"),
          ]),
          h("div", { style: "text-align:center;padding:10px 0" }, [
            h("img", {
              src: `/api/boxes/${encodeURIComponent(cage)}/qr.svg`,
              alt: "二维码",
              style: "width:200px;height:200px",
            }),
          ]),
        ]),
        h("button", {
          class: "btn primary",
          onClick: () => {
            setCurrentBox({ cageId: cage, strain: "其他", mouseNoPad: 2 });
            go("/record");
          },
        }, "立即录制这一箱"),
        h("button", { class: "btn ghost", onClick: () => go("/manage") }, "返回箱子管理"),
      ])
    );
    app.innerHTML = "";
    mount(screen);
  }

  /* ================================================================== *
   * 视图：设置
   * ================================================================== */
  async function viewSettings() {
    const screen = h("div", { class: "screen" });
    screen.appendChild(appbar("设置", { back: "/" }));
    const projInput = h("input", { value: state.projectId, maxlength: "64" });
    screen.appendChild(
      h("div", { class: "content" }, [
        h("div", { class: "card" }, [
          h("div", { class: "field" }, [h("label", {}, "项目号（任务标签）"), projInput]),
          h("button", {
            class: "btn primary",
            onClick: () => {
              const v = projInput.value.trim() || "default";
              state.projectId = v;
              localStorage.setItem("mv.projectId", v);
              toast("已保存");
            },
          }, "保存"),
        ]),
        h("div", { class: "card" }, [
          h("div", { class: "li-sub" }, "管理端"),
          h("button", { class: "btn ghost", onClick: () => (location.href = "/?intent=manage") }, "打开管理端"),
        ]),
      ])
    );
    mount(screen);
  }

  /* ------------------------------------------------------------------ *
   * 路由注册
   * ------------------------------------------------------------------ */
  route("/", viewHome);
  route("/scan", viewScan);
  route("/mode", viewMode);
  route("/record", viewRecord);
  route("/done", viewDone);
  route("/box/:cageId", viewBoxRecords);
  route("/mouse/:recordId", viewMouse);
  route("/manage", viewManage);
  route("/manage/new", viewBoxNew);
  route("/settings", viewSettings);

  render();
})();
