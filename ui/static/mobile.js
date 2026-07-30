/* 小鼠称重记录 — 手机 Web SPA
 * 单文件实现：路由 / 状态 / API / 相机 / 扫码 / 视图 (design §4, §6, §7)
 * 依赖 api-client.js 提供的全局 apiFetch()。
 */
(function () {
  "use strict";

  const BASE = "/mobile";
  const app = document.getElementById("app");

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
    // P1-d: force MP4/H.264 only — no webm fallback.
    if (!window.MediaRecorder) return "";
    const list = [
      "video/mp4;codecs=avc1.42E01E",
    ];
    return list.find((t) => MediaRecorder.isTypeSupported(t)) || "";
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
      appbar("小鼠称重记录", {
        right: h("button", { class: "iconbtn", onClick: () => go("/settings") }, "⚙"),
      })
    );
    const content = h("div", { class: "content" });
    content.appendChild(h("div", { class: "hero-illus", html: HERO_SVG }));
    content.appendChild(
      h("button", { class: "btn primary", onClick: startRecording }, "📷  开始录制")
    );
    content.appendChild(
      h("button", { class: "btn outline", onClick: () => go("/manage") }, "🗂  开始管理")
    );
    content.appendChild(
      h("button", { class: "btn ghost", onClick: () => go("/scale-sync") }, "⏱  天平校时（测试）")
    );

    const recentCard = h("div", { class: "card", style: "margin-top:18px" });
    recentCard.appendChild(
      h("div", { class: "section-head" }, [
        h("h2", {}, "最近记录"),
        h("button", { class: "link", onClick: () => go("/manage") }, "查看全部 ›"),
      ])
    );
    const listWrap = h("div", { class: "list" }, [h("div", { class: "empty" }, "加载中…")]);
    recentCard.appendChild(listWrap);
    content.appendChild(recentCard);
    screen.appendChild(content);
    mount(screen);

    try {
      const data = await api.recentBoxes();
      listWrap.innerHTML = "";
      if (!data.items.length) {
        listWrap.appendChild(h("div", { class: "empty" }, "还没有记录，去录制第一只吧"));
      } else {
        data.items.forEach((b) => listWrap.appendChild(recentRow(b)));
      }
    } catch (err) {
      listWrap.innerHTML = "";
      listWrap.appendChild(h("div", { class: "empty" }, err.message));
    }
  }

  function recentRow(b) {
    const count = (b.record_count || 0) + (b.pending_count || 0);
    return h(
      "div",
      { class: "list-item", onClick: () => go(`/box/${encodeURIComponent(b.cage_id)}`) },
      [
        h("div", { class: "li-main" }, [
          h("div", { class: "li-title" }, b.cage_id),
          h("div", { class: "li-sub" }, `${b.strain} · ${fmtTime(b.last_activity_at || b.created_at)}`),
        ]),
        h("span", { class: "count-pill" }, `${count} 只`),
      ]
    );
  }

  function startRecording() {
    if (state.currentBox) go("/record");
    else go("/scan");
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
      go("/record");
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
    // sees, what MediaRecorder captures, and what we JPEG-encode for the
    // realtime WebSocket stream — same 720×1280 pixels.
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
    const viewportHost = h("div", { class: "record-viewport-host" }, [viewport]);

    // --- Realtime dock ---
    const stateDot = h("span", {
      class: "rt-state-dot",
      style:
        "display:inline-block;width:10px;height:10px;border-radius:50%;background:#9aa0a6;margin-right:6px;vertical-align:middle",
    });
    const stateText = h("span", { class: "rt-state-text" }, "正在连接…");
    const stateIndicator = h(
      "div",
      { class: "rt-state-indicator", style: "text-align:center;padding:6px 0;font-size:15px" },
      [stateDot, stateText]
    );

    const weightValue = h(
      "span",
      { class: "rt-weight-value", style: "font-size:56px;font-weight:700;line-height:1;color:#9aa0a6" },
      "--"
    );
    const weightUnit = h(
      "span",
      { class: "rt-weight-unit", style: "font-size:20px;margin-left:6px;color:#9aa0a6" },
      "g"
    );
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
      { class: "btn primary rt-btn-accept", type: "button", hidden: true },
      "确认"
    );
    const actionButtons = h(
      "div",
      { class: "rt-actions", style: "display:flex;gap:12px;justify-content:center" },
      [retryBtn, acceptBtn]
    );

    const mouseCount = h(
      "div",
      { class: "rt-mouse-count", style: "text-align:center;color:var(--muted,#5f6368);font-size:13px" },
      "已记录 0 只"
    );

    const dock = h(
      "div",
      { class: "realtime-dock", style: "padding:8px 16px 16px" },
      [stateIndicator, weightDisplay, qualityHints, actionButtons, mouseCount]
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

    // Realtime-specific state
    let rtSession = null; // { session_id, ... }
    let ws = null;
    let wsClosedByUs = false;
    let reconnectHandle = null;
    let nextFrameTimer = null;
    let frameSeq = 0;
    let recordingT0 = 0;
    // Strict single in-flight frame: only the matching frame_seq ACK releases.
    let pendingFrameSeq = null;
    let pendingFrameSentAt = 0;
    let frameAckTimer = null;
    let nextAllowedSendAt = 0;
    let frameLoopActive = false;
    let retryInFlight = false;
    let rtState = "connecting";
    let rtMouseCount = 0;
    let announcedWeight = null;
    let finished = false;
    let abandoned = false;

    // 蓝牙天平桥接：原生外壳注入 window.MiceAutomaticScale 时启用 BLE 源，
    // 否则全程走 OCR（行为与改造前逐字节一致）。
    const scaleAvailable = ScaleBridge.detectNativeBridge();
    const weightSource = scaleAvailable ? "native_ble" : "ocr";
    let scaleChannel = null;          // ScaleBridge.createScaleChannel() 实例
    let scaleSender = null;           // createLatestOnlySender：断线仅缓存最新一条
    let scaleReadingInvalidCount = 0; // 后端拒绝计数（scale_reading_invalid）

    // Adaptive JPEG encode profiles (realtime path only; archive video untouched).
    const ENCODE_PROFILES = {
      high: { name: "high", w: 720, h: 1280, quality: 0.55 },
      medium: { name: "medium", w: 540, h: 960, quality: 0.50 },
      low: { name: "low", w: 480, h: 854, quality: 0.40 },
    };
    let encodeProfile = ENCODE_PROFILES.high;
    let recentAckMs = [];
    // These defaults are overridden by the server's client_config on session
    // create (P2); declared with let so they can be clamped from YAML.
    let MIN_FRAME_INTERVAL_MS = 200; // hard 5fps ceiling
    let FRAME_ACK_TIMEOUT_MS = 3000;
    // Client-side timing telemetry: per-ACK {frame_seq, encode_ms, rtt_ms,
    // jpeg_bytes}. Flushed to the server in batches (P1-3) so the session
    // finish summary can report client encode + RTT P50/P95.
    let clientTimingSamples = [];
    let clientTimingFlushTimer = null;
    const CLIENT_TIMING_FLUSH_INTERVAL_MS = 2000;
    const CLIENT_TIMING_FLUSH_BATCH = 10;
    // Per-frame encode bookkeeping (set in sendFrame, consumed on ACK).
    let lastEncodeStartedAt = 0;
    let lastEncodeMs = 0;
    let lastJpegBytes = 0;

    // Offscreen canvas for downscaled realtime JPEG (archive stays 720×1280).
    const encodeCanvas = document.createElement("canvas");
    const encodeCtx = encodeCanvas.getContext("2d", { alpha: false });

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
        toast("当前浏览器不支持 MP4/H.264 录像，请更换浏览器后重试");
        return false;
      }
      const opts = { videoBitsPerSecond: 1500000, mimeType: mime };
      try {
        recorder = new MediaRecorder(canvasStream, opts);
      } catch (err2) {
        toast("无法启动 MP4 录像，请更换浏览器后重试");
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
        doUpload(blob, `mv-${Date.now()}.mp4`, durationSec, {
          captureMode: "realtime",
          captureMeta: meta,
        });
      });
      // No timeslice: one complete container on stop() (Android fMP4 pitfall).
      recorder.start();
      recording = true;
      startedAt = Date.now();
      recordingT0 = startedAt;
      return true;
    }

    function doUpload(blob, filename, durationSec, uploadOpts) {
      stopDraw();
      stopStream(stream);
      stream = null;
      setTitle("上传中");
      renderUploading(box, blob, filename, durationSec, uploadOpts || {});
    }

    // --- Realtime session + WebSocket ---
    function getToken() {
      try {
        const meta = document.querySelector('meta[name="mousevision-api-token"]');
        return meta && meta.content ? meta.content.trim() : "";
      } catch (_) {
        return "";
      }
    }

    function rtSend(obj) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify(obj)); return true; } catch (_) {}
      }
      return false;
    }

    function showReconnect(show) {
      reconnectOverlay.hidden = !show;
    }

    function connectWs() {
      if (!rtSession || !rtSession.session_id) return;
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const token = getToken();
      const qs = `session_id=${encodeURIComponent(rtSession.session_id)}&token=${encodeURIComponent(token)}`;
      const url = `${proto}//${location.host}/api/realtime/ws?${qs}`;
      try {
        ws = new WebSocket(url);
        ws.binaryType = "arraybuffer";
      } catch (err) {
        showReconnect(true);
        scheduleReconnect();
        return;
      }
      ws.addEventListener("open", () => {
        showReconnect(false);
        if (rtState === "connecting") setState("calibrating", {});
        // 蓝牙天平：重连后 flush 最新缓存的读数（仅一条），不补发过期队列
        if (weightSource === "native_ble" && scaleSender) {
          scaleSender.flush();
        }
      });
      ws.addEventListener("message", (ev) => {
        if (typeof ev.data !== "string") return;
        let msg;
        try { msg = JSON.parse(ev.data); } catch (_) { return; }
        handleServerMessage(msg);
      });
      ws.addEventListener("close", () => {
        releasePendingFrame();
        if (wsClosedByUs || finished) return;
        showReconnect(true);
        scheduleReconnect();
      });
      ws.addEventListener("error", () => {
        releasePendingFrame();
        // The close handler will fire and trigger reconnect.
      });
    }

    function scheduleReconnect() {
      if (reconnectHandle || finished) return;
      reconnectHandle = setTimeout(() => {
        reconnectHandle = null;
        connectWs();
      }, 2000);
    }

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
        weightValue.style.color = "#9aa0a6";
        weightUnit.style.color = "#9aa0a6";
      } else {
        weightValue.textContent = Number(value).toFixed(2);
        // Confirmed (announced) = green; live/unconfirmed = gray.
        const c = confirmed ? "#1e8e3e" : "#5f6368";
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
      retry_requested: "正在重测…",
    };
    const STATE_COLORS = {
      connecting: "#9aa0a6",
      calibrating: "#f59e0b",
      armed: "#1a73e8",
      weighing: "#1a73e8",
      announced: "#1e8e3e",
      wait_clear: "#f59e0b",
      accepted: "#1e8e3e",
      retry_requested: "#f59e0b",
    };

    function setState(newState, msg) {
      msg = msg || {};
      rtState = newState;
      stateText.textContent = STATE_LABELS[newState] || newState;
      stateDot.style.background = STATE_COLORS[newState] || "#9aa0a6";

      const showGuides = newState === "connecting" || newState === "calibrating";
      guides.style.display = showGuides ? "" : "none";

      const showActions = newState === "announced";
      retryBtn.hidden = !showActions;
      acceptBtn.hidden = !showActions;

      switch (newState) {
        case "calibrating":
          setQualityHints(
            msg.hints && msg.hints.length ? msg.hints : ["请调整手机，使显示屏位于画面内"]
          );
          setWeightValue(msg.weight != null ? msg.weight : null, false);
          break;
        case "armed":
          setQualityHints(["请将小鼠放上秤盘"]);
          setWeightValue(msg.weight != null ? msg.weight : null, false);
          break;
        case "weighing":
          setQualityHints([]);
          setWeightValue(msg.weight != null ? msg.weight : null, false);
          break;
        case "announced":
          setQualityHints([]);
          if (typeof msg.weight === "number") announcedWeight = msg.weight;
          if (announcedWeight != null) setWeightValue(announcedWeight, true);
          break;
        case "wait_clear":
          setQualityHints(["请取走小鼠"]);
          setWeightValue(null, false);
          break;
        case "accepted":
          setQualityHints([]);
          if (announcedWeight != null) {
            weightDisplay.style.transition = "transform .15s ease";
            weightDisplay.style.transform = "scale(1.15)";
            setTimeout(() => { weightDisplay.style.transform = "scale(1)"; }, 200);
          }
          break;
      }
    }

    function maybeReleasePendingFrame(msg) {
      // Only state/error that carries the matching frame_seq releases the lock.
      // hello / retry ACK / accept ACK must never release a pending frame.
      if (pendingFrameSeq == null) return;
      if (msg.type !== "state" && msg.type !== "error") return;
      if (Number(msg.frame_seq) !== pendingFrameSeq) return;
      if (pendingFrameSentAt > 0) {
        noteAckLatency(performance.now() - pendingFrameSentAt);
      }
      releasePendingFrame();
      if (frameLoopActive && !retryInFlight) scheduleNextFrame();
    }

    function noteAckLatency(ms) {
      recentAckMs.push(ms);
      if (recentAckMs.length > 8) recentAckMs.shift();
      const avg = recentAckMs.reduce((a, b) => a + b, 0) / recentAckMs.length;
      // Soft adaptive: slow ACK → low; recover → medium. Stay off high by default.
      if (avg > 1200 && encodeProfile.name !== "low") {
        encodeProfile = ENCODE_PROFILES.low;
      } else if (avg < 500 && encodeProfile.name === "low") {
        encodeProfile = ENCODE_PROFILES.medium;
      }
      // Record a client-side timing sample tied to the just-ACKed frame.
      if (pendingFrameSeq != null || lastEncodeMs > 0) {
        clientTimingSamples.push({
          frame_seq: frameSeq > 0 ? frameSeq - 1 : 0,
          encode_ms: Math.round(lastEncodeMs * 10) / 10,
          rtt_ms: Math.round(ms * 10) / 10,
          jpeg_bytes: lastJpegBytes,
        });
        if (clientTimingSamples.length >= CLIENT_TIMING_FLUSH_BATCH) {
          flushClientTiming();
        } else if (!clientTimingFlushTimer) {
          clientTimingFlushTimer = setTimeout(flushClientTiming, CLIENT_TIMING_FLUSH_INTERVAL_MS);
        }
      }
    }

    function flushClientTiming() {
      clientTimingFlushTimer = null;
      if (!clientTimingSamples.length) return;
      const batch = clientTimingSamples.splice(0, clientTimingSamples.length);
      rtSend({ type: "client_timing", samples: batch });
    }

    function releasePendingFrame() {
      pendingFrameSeq = null;
      pendingFrameSentAt = 0;
      if (frameAckTimer) {
        clearTimeout(frameAckTimer);
        frameAckTimer = null;
      }
    }

    function onFrameAckTimeout() {
      frameAckTimer = null;
      pendingFrameSeq = null;
      pendingFrameSentAt = 0;
      // P1 fix: a single ACK timeout means the old frame may still be queued
      // in the network or server. Rather than send another frame on the same
      // connection (which could re-introduce multi-frame queuing on weak
      // networks), close the socket and let the reconnect path deliver only
      // the freshest frame.
      showReconnect(true);
      stateText.textContent = "网络较慢，正在重连";
      try {
        if (ws) ws.close();
      } catch (_) {}
    }

    function handleServerMessage(msg) {
      if (!msg || typeof msg !== "object") return;
      const t = msg.type;

      // Release in-flight frame ONLY when frame_seq matches (state or error).
      maybeReleasePendingFrame(msg);

      if (t === "hello") {
        // Initial state snapshot on WS connect — does not release frame lock.
        if (msg.state) setState(msg.state, msg);
        if (msg.accepted_count != null) {
          rtMouseCount = msg.accepted_count;
          mouseCount.textContent = `已记录 ${rtMouseCount} 只`;
        }
        // P1 fix: full state recovery on (re)connect. A dropped retry ACK
        // would otherwise leave retryInFlight stuck; and a reconnect during
        // weighing/armed/wait_clear must resume the frame loop or the phone
        // stops sending frames forever.
        retryInFlight = false;
        retryBtn.disabled = false;
        retryBtn.textContent = "重测";
        const helloState = msg.state;
        if (
          helloState === "weighing" ||
          helloState === "armed" ||
          helloState === "calibrating" ||
          helloState === "wait_clear"
        ) {
          // These states need frames to progress. announced is handled by
          // setState (shows the accept/retry buttons) and waits for user
          // input, so it does NOT need the frame loop.
          startFrameLoop();
        }
      } else if (t === "state") {
        // Primary per-frame state update from the engine.
        const newState = msg.state || rtState;

        // Weight display: use weight_candidate (backend field name).
        if (typeof msg.weight_candidate === "number") {
          const confirmed = newState === "announced";
          setWeightValue(msg.weight_candidate, confirmed);
          if (confirmed) announcedWeight = msg.weight_candidate;
        }

        // Quality hints: array of {code, message}.
        if (msg.quality_hints && msg.quality_hints.length) {
          setQualityHints(msg.quality_hints.map(function (h) { return h.message || h.code; }));
        } else if (newState === "calibrating") {
          setQualityHints(["请调整手机，使显示屏位于画面内"]);
        } else if (newState === "armed") {
          setQualityHints(["请将小鼠放上秤盘"]);
        } else if (newState === "wait_clear") {
          setQualityHints(["请取走小鼠"]);
        } else {
          setQualityHints([]);
        }

        // New attempt announced → speak + show action buttons.
        if (msg.attempt && newState === "announced") {
          announcedWeight = msg.attempt.weight_g;
          setWeightValue(announcedWeight, true);
          speakWeight(announcedWeight);
        }

        // Weight accepted (auto or explicit) → update count.
        if (msg.accepted_weight != null) {
          rtMouseCount += 1;
          mouseCount.textContent = `已记录 ${rtMouseCount} 只`;
          announcedWeight = null;
        }

        setState(newState, msg);
      } else if (t === "ack") {
        // Response to a retry/accept command — never releases a frame lock.
        if (msg.cmd === "accept") {
          if (msg.accepted) {
            rtMouseCount += 1;
            mouseCount.textContent = `已记录 ${rtMouseCount} 只`;
            announcedWeight = null;
            if (msg.state) setState(msg.state, msg);
            // P0 fix: backend now sits in WAIT_CLEAR and needs more frames to
            // see the weight return to zero for the next mouse. We must resume
            // the frame loop on success — otherwise the flow stalls forever
            // after the first mouse.
            startFrameLoop();
          } else {
            // accept rejected (e.g. already accepted / stale state): restore
            // the loop so the operator can try again.
            if (msg.state) setState(msg.state, msg);
            startFrameLoop();
          }
        } else if (msg.cmd === "retry") {
          retryBtn.disabled = false;
          retryBtn.textContent = "重测";
          retryInFlight = false;
          if (msg.applied) {
            announcedWeight = null;
            setState(msg.state || "weighing", msg);
            startFrameLoop();
          } else {
            toast("当前无法重测，请稍后再试");
            if (msg.state) setState(msg.state, msg);
            startFrameLoop();
          }
        } else if (msg.state) {
          setState(msg.state, msg);
          // Any other ack must not leave the loop stopped, or the phone
          // would freeze in weighing/announced/wait_clear.
          if (!retryInFlight) startFrameLoop();
        }
      } else if (t === "error") {
        // 蓝牙读数被后端判为无效：仅记录计数 + 控制台告警，不重试循环
        if (msg.code === "scale_reading_invalid" || msg.code === "scale_reading_rejected") {
          scaleReadingInvalidCount += 1;
          console.warn("scale_reading rejected:", msg.code, msg.message || "");
          return;
        }
        toast(msg.message || "实时分析出错");
      }
    }

    async function waitForPendingFrameClear(timeoutMs) {
      const deadline = performance.now() + (timeoutMs || FRAME_ACK_TIMEOUT_MS);
      while (pendingFrameSeq != null && performance.now() < deadline) {
        await new Promise((r) => setTimeout(r, 40));
      }
      if (pendingFrameSeq != null) releasePendingFrame();
    }

    retryBtn.addEventListener("click", async () => {
      if (retryInFlight || rtState !== "announced") return;
      retryInFlight = true;
      retryBtn.disabled = true;
      retryBtn.textContent = "正在重测…";
      stopFrameLoop();
      // Wait for the single in-flight frame ACK (or timeout) so retry is not
      // queued behind a stale JPEG. Same-connection FIFO then puts retry next.
      await waitForPendingFrameClear(FRAME_ACK_TIMEOUT_MS);
      const sent = rtSend({ type: "retry" });
      if (!sent) {
        retryInFlight = false;
        retryBtn.disabled = false;
        retryBtn.textContent = "重测";
        toast("发送失败，请检查网络");
        startFrameLoop();
      }
      // Do not optimistic-switch to armed; wait for applied ACK.
    });
    acceptBtn.addEventListener("click", async () => {
      stopFrameLoop();
      await waitForPendingFrameClear(FRAME_ACK_TIMEOUT_MS);
      const sent = rtSend({ type: "accept" });
      if (!sent) {
        toast("发送失败，请检查网络");
        startFrameLoop();
      }
    });

    // --- Frame sending: strict single in-flight + 5fps ceiling ---
    function encodeRealtimeBlob(cb) {
      const profile = encodeProfile;
      try {
        if (profile.w === CANVAS_W && profile.h === CANVAS_H) {
          canvas.toBlob(cb, "image/jpeg", profile.quality);
          return;
        }
        encodeCanvas.width = profile.w;
        encodeCanvas.height = profile.h;
        encodeCtx.drawImage(canvas, 0, 0, profile.w, profile.h);
        encodeCanvas.toBlob(cb, "image/jpeg", profile.quality);
      } catch (_) {
        cb(null);
      }
    }

    function sendFrame() {
      if (!frameLoopActive || retryInFlight) return;
      if (pendingFrameSeq != null) return;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      if (!paintedReady) {
        scheduleNextFrame();
        return;
      }
      // Encode first; allocate frame_seq only immediately before ws.send().
      lastEncodeStartedAt = performance.now();
      encodeRealtimeBlob((blob) => {
        if (!blob) {
          scheduleNextFrame();
          return;
        }
        // Capture encode duration (drawImage + JPEG encode) for telemetry.
        lastEncodeMs = performance.now() - lastEncodeStartedAt;
        lastJpegBytes = blob.size;
        if (!frameLoopActive || retryInFlight) return;
        if (pendingFrameSeq != null) return;
        if (!ws || ws.readyState !== WebSocket.OPEN) {
          scheduleNextFrame();
          return;
        }
        const reader = new FileReader();
        reader.onload = () => {
          if (!frameLoopActive || retryInFlight) return;
          if (pendingFrameSeq != null) return;
          if (!ws || ws.readyState !== WebSocket.OPEN) {
            scheduleNextFrame();
            return;
          }
          const jpegBytes = new Uint8Array(reader.result);
          const seq = frameSeq;
          const buf = new ArrayBuffer(8 + jpegBytes.length);
          const dv = new DataView(buf);
          dv.setUint32(0, seq, true);
          dv.setUint32(4, Date.now() - recordingT0, true);
          new Uint8Array(buf, 8).set(jpegBytes);
          try {
            ws.send(buf);
          } catch (_) {
            scheduleNextFrame();
            return;
          }
          frameSeq = seq + 1;
          pendingFrameSeq = seq;
          pendingFrameSentAt = performance.now();
          nextAllowedSendAt = performance.now() + MIN_FRAME_INTERVAL_MS;
          if (frameAckTimer) clearTimeout(frameAckTimer);
          frameAckTimer = setTimeout(onFrameAckTimeout, FRAME_ACK_TIMEOUT_MS);
          // Do NOT schedule next frame here — wait for matching ACK.
        };
        reader.onerror = () => {
          scheduleNextFrame();
        };
        reader.readAsArrayBuffer(blob);
      });
    }

    function scheduleNextFrame() {
      if (!frameLoopActive || retryInFlight) return;
      const delay = Math.max(0, nextAllowedSendAt - performance.now());
      if (nextFrameTimer) clearTimeout(nextFrameTimer);
      nextFrameTimer = setTimeout(sendFrame, delay);
    }

    function startFrameLoop() {
      frameLoopActive = true;
      scheduleNextFrame();
    }

    function stopFrameLoop() {
      frameLoopActive = false;
      if (nextFrameTimer) {
        clearTimeout(nextFrameTimer);
        nextFrameTimer = null;
      }
      // Keep pendingFrameSeq until ACK/timeout so retry can wait on it;
      // full cleanup happens on WS close / session teardown.
    }

    function teardownFrameProtocol() {
      // Flush any remaining client timing before the socket goes away so the
      // session summary reflects the tail frames too.
      if (clientTimingFlushTimer) {
        clearTimeout(clientTimingFlushTimer);
        clientTimingFlushTimer = null;
      }
      flushClientTiming();
      stopFrameLoop();
      releasePendingFrame();
    }

    // --- Create realtime session and open WS ---
    function applyClientConfig(cc) {
      // Apply server-provided tuning knobs with client-side clamping so a bad
      // or stale YAML value cannot break the frame loop (P2).
      if (!cc || typeof cc !== "object") return;
      if (typeof cc.max_fps === "number" && cc.max_fps > 0) {
        // Clamp to [1, 10] fps; floor of 100ms/frame.
        const fps = Math.max(1, Math.min(10, Math.floor(cc.max_fps)));
        MIN_FRAME_INTERVAL_MS = Math.max(100, Math.round(1000 / fps));
      }
      if (typeof cc.frame_ack_timeout_ms === "number" && cc.frame_ack_timeout_ms > 0) {
        // Clamp to [1000, 10000] ms.
        FRAME_ACK_TIMEOUT_MS = Math.max(1000, Math.min(10000, Math.round(cc.frame_ack_timeout_ms)));
      }
      if (
        typeof cc.encode_profile === "string" &&
        Object.prototype.hasOwnProperty.call(ENCODE_PROFILES, cc.encode_profile)
      ) {
        encodeProfile = ENCODE_PROFILES[cc.encode_profile];
      }
    }

    async function startRealtime() {
      try {
        // 原生 BLE 桥存在时，向会话声明 weight_source=ble_k797，后端据此
        // 接收 scale_reading 文本消息；OCR 模式不带该字段，行为不变。
        const body = { cage_id: box.cageId, project_id: state.projectId };
        if (weightSource === "native_ble") body.weight_source = "ble_k797";
        const res = await api.json("/api/realtime/session", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        rtSession = res;
        // Apply server-side client_config BEFORE starting the frame loop so
        // the first frame uses the configured fps / profile / ACK timeout.
        applyClientConfig(res && res.client_config);
        connectWs();
        startFrameLoop();
        // native_ble：WS 打开后由 connectWs 的 open 回调 flush 最新读数。
        if (weightSource === "native_ble") startScaleChannel();
      } catch (err) {
        toast(err && err.message ? err.message : "无法启动实时称重");
        showReconnect(true);
        scheduleReconnect();
      }
    }

    /* --- 蓝牙天平通道：仅在 weightSource === 'native_ble' 时生效 --- */
    function bleClientTsMs() {
      // 复用帧时序基准（recordingT0 = 开始录像的 Date.now()），与帧 client_ts 同源
      if (!recordingT0) return 0;
      return Date.now() - recordingT0;
    }

    function updateScaleMeta(reading) {
      if (!reading) return;
      // RSSI + 广播年龄（秒），信息行
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
      // 复用 qualityHints 展示广播中断提示；真实 0g 由读数直接渲染
      setQualityHints(["天平广播中断"]);
    }

    // BLE 读数显示（一位小数）：raw=0 → "0.0"；不改动 OCR 的 setWeightValue
    function setBleWeightDisplay(grams) {
      if (grams == null || typeof grams !== "number" || !isFinite(grams)) {
        weightValue.textContent = "--";
        weightValue.style.color = "#9aa0a6";
        weightUnit.style.color = "#9aa0a6";
        return;
      }
      weightValue.textContent = grams.toFixed(1);
      weightValue.style.color = "#5f6368";
      weightUnit.style.color = "#5f6368";
    }

    function sendScaleReadingToWs(reading) {
      if (!rtSession || rtSession.weight_source !== "ble_k797") return;
      const msg = ScaleBridge.buildScaleReadingMessage(reading, bleClientTsMs());
      // WS 打开则直发；断开期间由 sender 仅缓存最新一条，重连 flush。
      if (ws && ws.readyState === WebSocket.OPEN) {
        rtSend(msg);
      } else {
        scaleSender.offer(msg);
      }
    }

    function startScaleChannel() {
      if (scaleChannel || weightSource !== "native_ble") return;
      scaleSender = ScaleBridge.createLatestOnlySender(function (m) { rtSend(m); });
      scaleChannel = ScaleBridge.createScaleChannel();
      // 新读数 → 直显（一位小数）+ 转发 WS；announced 态不改写（由引擎驱动）
      scaleChannel.onReading(function (reading) {
        if (rtState !== "announced" && rtState !== "accepted") {
          const fmt = ScaleBridge.formatScaleDisplay(scaleChannel.getState());
          if (!fmt.stale) setBleWeightDisplay(reading.grams);
        }
        updateScaleMeta(reading);
        sendScaleReadingToWs(reading);
      });
      // stale 切换 → 显示 "--" + 中断提示
      scaleChannel.onStaleChange(function (isStale) {
        if (isStale && rtState !== "announced" && rtState !== "accepted") {
          setWeightValue(null, false);
          showScaleStaleHint(true);
        }
      });
      // 状态 → 反映未授权/蓝牙关闭/异常到状态文本
      scaleChannel.onStatus(function (detail) {
        const bad = detail.state === "unauthorized" || detail.state === "bluetooth_off" ||
          detail.state === "error";
        if (bad && detail.message) {
          stateText.textContent = detail.message;
        }
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
      scaleSender = null;
    }

    // --- Finish: stop recording → upload video → finalize session with job_id ---
    // Order matters: the uploaded video gets a job_id from /api/jobs, and
    // we pass that id to /api/realtime/session/<id>/finish so the finalized
    // run dir can link back to the source video for clip extraction.
    async function finishSession() {
      if (finished) return;
      finished = true;
      finishBtn.disabled = true;
      teardownFrameProtocol();
      stopScaleChannel();
      wsClosedByUs = true;
      if (ws) {
        try { ws.close(); } catch (_) {}
        ws = null;
      }
      if (reconnectHandle) {
        clearTimeout(reconnectHandle);
        reconnectHandle = null;
      }
      showReconnect(false);

      // 1. Stop the background recorder → its stop handler builds the blob
      //    and calls doUpload → renderUploading → POST /api/jobs.
      //    We intercept doUpload to capture the returned job_id before it
      //    navigates away, so we can pass it to /finish.
      let uploadedJobId = null;
      const origDoUpload = doUpload;
      function doUploadForFinish(blob, filename, durationSec, uploadOpts) {
        // Override: upload, capture job_id, then finalize, then render done.
        stopDraw();
        stopStream(stream);
        stream = null;
        setTitle("上传中");
        uploadVideo(blob, filename, box, null, durationSec, uploadOpts || {})
          .then(async (job) => {
            uploadedJobId = job.job_id;
            // 2. Finalize the realtime session with the video job id.
            if (rtSession && rtSession.session_id) {
              try {
                await api.json(
                  `/api/realtime/session/${encodeURIComponent(rtSession.session_id)}/finish`,
                  {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                      video_upload_job_id: uploadedJobId,
                      capture_meta: (uploadOpts && uploadOpts.captureMeta) || null,
                    }),
                  }
                );
              } catch (_) {}
            }
            state.activeJobId = uploadedJobId;
            go("/done");
          })
          .catch((err) => {
            toast(err.message);
            // Even if upload fails, try to finalize so accepted attempts
            // are still persisted (without a linked source video).
            if (rtSession && rtSession.session_id) {
              api.json(
                `/api/realtime/session/${encodeURIComponent(rtSession.session_id)}/finish`,
                { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }
              ).catch(() => {});
            }
            go("/");
          });
      }
      doUpload = doUploadForFinish; // type: ignore - local override

      if (recorder && recording) {
        recording = false;
        try { recorder.stop(); } catch (_) {} // triggers doUploadForFinish
      } else {
        // No recording (shouldn't happen post-P1 fix, but handle gracefully):
        // finalize without a source video.
        if (rtSession && rtSession.session_id) {
          try {
            await api.json(
              `/api/realtime/session/${encodeURIComponent(rtSession.session_id)}/finish`,
              { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }
            );
          } catch (_) {}
        }
        go("/");
      }
    }
    finishBtn.addEventListener("click", finishSession);

    function disableRealtime(reason) {
      useCanvas = false;
      paintedReady = false;
      stopDraw();
      teardownFrameProtocol();
      stopScaleChannel();
      wsClosedByUs = true;
      if (ws) { try { ws.close(); } catch (_) {} ws = null; }
      if (reconnectHandle) { clearTimeout(reconnectHandle); reconnectHandle = null; }
      stopStream(stream);
      stream = null;
      if (canvasStream) {
        canvasStream.getTracks().forEach((t) => t.stop());
        canvasStream = null;
      }
      switchCamBtn.hidden = true;
      finishBtn.disabled = true;
      stateText.textContent = "不可用";
      stateDot.style.background = "#dc3545";
      setQualityHints([reason || "当前浏览器无法进行实时称重，请更换浏览器后重试"]);
    }

    // --- Boot: camera → draw loop → background recorder → realtime session ---
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
        // P1: the background recorder is the only source of the uploaded
        // video. If it cannot start, we MUST NOT begin a realtime session,
        // because accepted attempts would otherwise have no audit video and
        // the operator would believe a session was recorded when it wasn't.
        const recOk = startBackgroundRecorder();
        if (!recOk) {
          disableRealtime("无法启动后台录像，实时称重不可用。请更换浏览器或重试。");
          return;
        }
        startRealtime();
      } catch (err) {
        disableRealtime("无法打开实时相机，请确认 HTTPS 与摄像头权限");
      }
    })();

    return () => {
      // Distinguish navigation-away from an explicit finish: only the former
      // should suppress the recorder's upload handler.
      if (!finished) abandoned = true;
      finished = true;
      document.documentElement.classList.remove("camera-mode", "record-light");
      clearInterval(clockTimer);
      stopDraw();
      teardownFrameProtocol();
      stopScaleChannel();
      wsClosedByUs = true;
      if (ws) { try { ws.close(); } catch (_) {} ws = null; }
      if (reconnectHandle) { clearTimeout(reconnectHandle); reconnectHandle = null; }
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
    };
  }

  /* ================================================================== *
   * 视图：上传 + 完成 / 排队 (屏 4)
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
    try { box = await api.box(cage); strain = box.strain; } catch (_) {}

    screen.appendChild(
      appbar(cage, {
        back: "/manage",
        right: h("button", { class: "iconbtn", onClick: () => showQr(cage) }, "▦"),
      })
    );
    const countEl = h("span", { class: "count-pill" }, "");
    const subHead = h("div", { class: "content", style: "padding-bottom:0" }, [
      h("div", { class: "section-head" }, [
        h("div", { class: "strain-sub" }, strain || "其他"),
        countEl,
      ]),
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
            go("/record");
          },
        }, "继续录制"),
      ])
    );
    mount(screen);

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

  /* ================================================================== *
   * 视图：天平校时（测试）  docs/SCALE_TIME_SYNC_MVP.md
   *   新建会话 → 记开始锚点 → 记结束锚点 → 上传 CSV → 选两行 → 计算结果
   * ================================================================== */

  /* 手机时间格式化：精确到毫秒 */
  function fmtPhoneTime(ms) {
    const d = new Date(ms);
    const p = (n, w) => String(n).padStart(w || 2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
  }

  /* UTC 偏移分钟 -> +08:00 */
  function fmtUtcOffset(min) {
    if (min == null) return "";
    const sign = min <= 0 ? "+" : "-";
    const abs = Math.abs(min);
    return `UTC${sign}${String(Math.floor(abs / 60)).padStart(2, "0")}:${String(abs % 60).padStart(2, "0")}`;
  }

  function phoneTzInfo() {
    let tz = "";
    let offset = null;
    try {
      tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
      offset = -new Date().getTimezoneOffset();
    } catch (_) {}
    return { tz, offset };
  }

  /* 顶部手机时间条（100ms 刷新）。返回 { stop } 以便视图 cleanup。 */
  function mountPhoneClock(parent) {
    const timeEl = h("div", { class: "sclk-time" }, "--");
    const tzEl = h("div", { class: "sclk-tz" }, "");
    const card = h("div", { class: "card sclk-card" }, [
      h("div", { class: "li-sub" }, "手机时间（用于匹配视频）"),
      timeEl,
      tzEl,
    ]);
    parent.appendChild(card);
    const tick = () => {
      timeEl.textContent = fmtPhoneTime(Date.now());
      const { tz, offset } = phoneTzInfo();
      tzEl.textContent = `${tz || "未知时区"}  ${fmtUtcOffset(offset)}`;
    };
    tick();
    const timer = setInterval(tick, 100);
    return { node: card, stop: () => clearInterval(timer) };
  }

  const SCALE_SYNC_API = {
    createSession: (body) =>
      api.json("/api/scale-sync/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    getSession: (sid) => api.json(`/api/scale-sync/sessions/${sid}`),
    putAnchor: (sid, kind, body) =>
      api.json(`/api/scale-sync/sessions/${sid}/anchors/${kind}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    deleteAnchor: (sid, kind) =>
      api.json(`/api/scale-sync/sessions/${sid}/anchors/${kind}`, { method: "DELETE" }),
    importCsv: (sid, file) => {
      const fd = new FormData();
      fd.append("file", file);
      return api.json(`/api/scale-sync/sessions/${sid}/imports`, { method: "POST", body: fd });
    },
    readings: (sid, iid, q) =>
      api.json(`/api/scale-sync/sessions/${sid}/imports/${iid}/readings` + (q ? `?query=${encodeURIComponent(q)}` : "")),
    matchAnchor: (sid, kind, body) =>
      api.json(`/api/scale-sync/sessions/${sid}/anchors/${kind}/match`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    calculate: (sid) => api.json(`/api/scale-sync/sessions/${sid}/calculate`, { method: "POST" }),
  };

  function fmtOffsetSec(ms) {
    const s = (ms || 0) / 1000;
    const sign = s >= 0 ? "慢" : "快";
    return `天平比手机${sign} ${Math.abs(s).toFixed(1)} s`;
  }

  function fmtDrift(ppm) {
    const perHour = ((ppm || 0) / 1_000_000) * 3600;
    const sign = perHour >= 0 ? "快" : "慢";
    return `天平每小时再${sign} ${Math.abs(perHour).toFixed(2)} s（${(ppm || 0).toFixed(0)} ppm）`;
  }

  async function viewScaleSync() {
    const screen = h("div", { class: "screen" });
    screen.appendChild(appbar("天平校时（测试）", { back: "/" }));
    const content = h("div", { class: "content" });
    screen.appendChild(content);
    mount(screen);

    const clockCtl = mountPhoneClock(content);

    // 会话恢复：URL ?session_id= 优先，其次 sessionStorage
    const urlSid = new URLSearchParams(location.search).get("session_id");
    let sessionId = urlSid || sessionStorage.getItem("mv.scaleSync.sid") || "";

    const statusEl = h("div", { class: "li-sub" }, "正在加载…");
    content.appendChild(statusEl);
    const bodyEl = h("div", {});
    content.appendChild(bodyEl);

    function persistSid(sid) {
      sessionId = sid;
      if (sid) {
        sessionStorage.setItem("mv.scaleSync.sid", sid);
        if (!urlSid) {
          const u = new URL(location.href);
          u.searchParams.set("session_id", sid);
          history.replaceState({}, "", u.toString());
        }
      } else {
        sessionStorage.removeItem("mv.scaleSync.sid");
      }
    }

    async function refresh() {
      if (!sessionId) {
        statusEl.textContent = "";
        renderNew();
        return;
      }
      try {
        const sess = await SCALE_SYNC_API.getSession(sessionId);
        statusEl.textContent = "";
        renderSession(sess);
      } catch (err) {
        statusEl.textContent = "";
        bodyEl.innerHTML = "";
        bodyEl.appendChild(sessionErrBox(err));
      }
    }

    function sessionErrBox(err) {
      const box = h("div", { class: "card" }, [
        h("div", { class: "empty" }, err.message || "会话加载失败"),
        h("button", { class: "btn outline", onClick: () => { persistSid(""); refresh(); } }, "新建校时"),
      ]);
      return box;
    }

    function renderNew() {
      bodyEl.innerHTML = "";
      bodyEl.appendChild(
        h("div", { class: "card" }, [
          h("div", { class: "li-sub" },
            "为本次称量建立天平时钟与手机时间的换算关系。流程：放物体→记锚点→上传 CSV→选对应行→计算。"),
          h("button", {
            class: "btn primary",
            onClick: async () => {
              try {
                const { tz } = phoneTzInfo();
                const sess = await SCALE_SYNC_API.createSession({
                  project_id: state.projectId || "default",
                  scale_timezone: "Asia/Shanghai",
                });
                persistSid(sess.session_id);
                void tz;
                refresh();
              } catch (err) { toast(err.message); }
            },
          }, "新建本次校时"),
        ])
      );
    }

    function progressBar(state) {
      // state ∈ created / has-start / has-end / has-import / matched / calculated
      const steps = ["新建会话", "记开始锚点", "记结束锚点", "上传 CSV", "选两条行", "计算结果"];
      let idx = 0;
      if (state === "calculated") idx = 5;
      else if (state === "matched") idx = 4;
      else if (state === "has-import") idx = 3;
      else if (state === "has-end") idx = 2;
      else if (state === "has-start") idx = 1;
      return h("div", { class: "ssync-progress" }, [
        h("div", { class: "ssync-steps" },
          steps.map((s, i) =>
            h("div", { class: "ssync-step" + (i <= idx ? " done" : "") + (i === idx ? " current" : "") }, [
              h("span", { class: "ssync-dot" }, String(i + 1)),
              h("span", { class: "ssync-label" }, s),
            ])
          )
        ),
      ]);
    }

    function sessionPhase(sess) {
      const a = {};
      (sess.anchors || []).forEach((x) => (a[x.kind] = x));
      const hasImport = (sess.imports || []).length > 0;
      if (sess.state === "calculated" || sess.calculated_model) return "calculated";
      if (a.start && a.start.matched_row && a.end && a.end.matched_row) return "matched";
      if (hasImport && a.start && a.end) return "has-import";
      if (a.start && a.end) return "has-end";
      if (a.start) return "has-start";
      return "created";
    }

    function renderSession(sess) {
      bodyEl.innerHTML = "";
      bodyEl.appendChild(progressBar(sessionPhase(sess)));
      const a = {};
      (sess.anchors || []).forEach((x) => (a[x.kind] = x));
      bodyEl.appendChild(anchorCard(sess, "start", a.start));
      bodyEl.appendChild(anchorCard(sess, "end", a.end));
      if (a.start && a.end) {
        bodyEl.appendChild(importCard(sess));
      }
      if (a.start && a.start.matched_row && a.end && a.end.matched_row) {
        bodyEl.appendChild(calculateCard(sess));
      }
      if (sess.calculated_model) {
        bodyEl.appendChild(resultCard(sess));
      }
    }

    function anchorCard(sess, kind, anchor) {
      const title = kind === "start" ? "开始锚点" : "结束锚点";
      const card = h("div", { class: "card ssync-card" });
      card.appendChild(h("div", { class: "section-head" }, [h("h2", {}, title)]));
      if (anchor) {
        card.appendChild(h("div", { class: "li-sub" }, `手机时间：${fmtPhoneTime(anchor.client_epoch_ms)}`));
        if (anchor.observed_weight_g != null)
          card.appendChild(h("div", { class: "li-sub" }, `观察重量：${anchor.observed_weight_g} g`));
        if (anchor.matched_row) {
          const m = anchor.matched_row;
          const phoneDelta = (anchor.client_epoch_ms - m.scale_epoch_ms) / 1000;
          card.appendChild(
            h("div", { class: "li-sub" },
              `已绑定行 #${m.source_line_no}：天平 ${new Date(m.scale_epoch_ms).toLocaleString("zh-CN")} · ${m.weight_g} ${m.unit}（按钮与行相差 ${phoneDelta.toFixed(1)} s）`)
          );
        } else {
          card.appendChild(h("div", { class: "li-sub ssync-pending" }, "尚未绑定 CSV 行"));
        }
        card.appendChild(
          h("button", {
            class: "btn ghost ssync-mini",
            onClick: async () => {
              if (!confirm(`确认删除${title}并重记？此操作会写审计记录。`)) return;
              try {
                await SCALE_SYNC_API.deleteAnchor(sess.session_id, kind);
                refresh();
              } catch (err) { toast(err.message); }
            },
          }, "删除并重记")
        );
      } else {
        const weightInput = h("input", {
          type: "number", step: "0.01", placeholder: "可选：观察到的稳定重量（g）", autocomplete: "off",
        });
        const nowLabel = h("div", { class: "li-sub" }, "");
        const recBtn = h("button", { class: "btn outline" }, "确认记录");
        const tick = () => (nowLabel.textContent = `当前手机时间：${fmtPhoneTime(Date.now())}`);
        tick();
        const t = setInterval(tick, 100);
        recBtn.addEventListener("click", async () => {
          if (!confirm(`确认记录${title}？`)) return;
          try {
            const { tz, offset } = phoneTzInfo();
            await SCALE_SYNC_API.putAnchor(sess.session_id, kind, {
              client_epoch_ms: Date.now(),
              client_perf_ms: Math.round(performance.now() * 1000) / 1000,
              client_timezone: tz,
              client_utc_offset_minutes: offset,
              observed_weight_g: weightInput.value ? parseFloat(weightInput.value) : null,
              note: "",
            });
            clearInterval(t);
            refresh();
          } catch (err) { toast(err.message); }
        });
        card.appendChild(nowLabel);
        card.appendChild(h("div", { class: "field" }, [weightInput]));
        card.appendChild(recBtn);
        // end anchor disabled until start exists is enforced by only showing
        // this card when appropriate; here we also block if start missing.
        if (kind === "end") {
          const hasStart = (sess.anchors || []).some((x) => x.kind === "start");
          if (!hasStart) {
            recBtn.disabled = true;
            recBtn.textContent = "需先记录开始锚点";
          }
        }
      }
      return card;
    }

    function importCard(sess) {
      const card = h("div", { class: "card ssync-card" });
      card.appendChild(h("div", { class: "section-head" }, [h("h2", {}, "从 U 盘上传 CSV")]));
      const imports = sess.imports || [];
      if (imports.length) {
        imports.forEach((imp) => {
          const s = imp.summary || {};
          card.appendChild(
            h("div", { class: "list-item", onClick: () => showReadings(sess, imp) }, [
              h("div", { class: "li-main" }, [
                h("div", { class: "li-title" }, imp.original_filename),
                h("div", { class: "li-sub" },
                  `${imp.sha256.slice(0, 12)}… · ${imp.byte_count} B · ${s.row_count} 行 · ${s.encoding || "?"}`),
              ]),
              h("span", { class: "count-pill" }, "查看读数"),
            ])
          );
        });
      }
      const fileInput = h("input", { type: "file", accept: ".csv,text/csv" });
      const uploadBtn = h("button", { class: "btn primary", onClick: async () => {
        const f = fileInput.files && fileInput.files[0];
        if (!f) { toast("请先选择文件"); return; }
        try {
          await SCALE_SYNC_API.importCsv(sess.session_id, f);
          toast("上传成功");
          refresh();
        } catch (err) { toast(err.message); }
      } }, "上传");
      card.appendChild(h("div", { class: "field" }, [fileInput]));
      card.appendChild(uploadBtn);
      return card;
    }

    function calculateCard(sess) {
      return h("div", { class: "card ssync-card" }, [
        h("button", {
          class: "btn primary",
          onClick: async () => {
            try { await SCALE_SYNC_API.calculate(sess.session_id); refresh(); }
            catch (err) { toast(err.message); }
          },
        }, "计算校时"),
      ]);
    }

    function resultCard(sess) {
      const m = sess.calculated_model;
      const level = (sess.warnings && sess.warnings.length) ? "yellow" : "green";
      const lvl = (sess.calculated_model && sess.warnings && sess.warnings.some(w => w.includes("ppm") || w.includes("时区"))) ? "red" : level;
      const card = h("div", { class: `card ssync-card ssync-result ${lvl}` });
      card.appendChild(h("div", { class: "section-head" }, [h("h2", {}, "校时结果")]));
      card.appendChild(h("div", { class: "li-main" }, `开始：${fmtOffsetSec(m.start_offset_ms)}`));
      card.appendChild(h("div", { class: "li-main" }, `漂移：${fmtDrift(m.drift_ppm)}`));
      const statusText = lvl === "red" ? "存在异常，请检查" : lvl === "yellow" ? "可用于本会话（有提醒）" : "可用于本会话的时间匹配";
      card.appendChild(h("div", { class: `ssync-badge ${lvl}` }, statusText));
      if (sess.warnings && sess.warnings.length) {
        const w = h("ul", { class: "ssync-warn" });
        sess.warnings.forEach((x) => w.appendChild(h("li", {}, x)));
        card.appendChild(w);
      }
      // 映射预览
      const preview = sess.readings_preview || [];
      if (preview.length) {
        card.appendChild(h("div", { class: "li-sub" }, "映射后读数预览（最多 20 行）："));
        const tbl = h("table", { class: "ssync-table" });
        tbl.appendChild(h("thead", {}, [h("tr", {}, [
          h("th", {}, "行"), h("th", {}, "天平时间"), h("th", {}, "重量"),
          h("th", {}, "手机时间"), h("th", {}, "状态"),
        ])]));
        const tb = h("tbody", {});
        preview.forEach((r) => {
          tb.appendChild(h("tr", {}, [
            h("td", {}, String(r.source_line_no)),
            h("td", {}, new Date(r.scale_epoch_ms).toLocaleString("zh-CN")),
            h("td", {}, `${r.weight_g} ${r.unit || ""}`),
            h("td", {}, fmtPhoneTime(r.phone_epoch_ms)),
            h("td", {}, r.within_window ? "窗口内" : "窗口外·不可用"),
          ]));
        });
        tbl.appendChild(tb);
        card.appendChild(h("div", { class: "ssync-table-wrap" }, [tbl]));
      }
      // 下载摘要 + 新建
      const dlBtn = h("button", { class: "btn outline" }, "下载校时摘要 JSON");
      dlBtn.addEventListener("click", () => {
        const blob = new Blob([JSON.stringify(sess, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url; a.download = `scale-sync-${sess.session_id.slice(0, 8)}.json`;
        a.click();
        URL.revokeObjectURL(url);
      });
      card.appendChild(h("div", { class: "ssync-actions" }, [
        dlBtn,
        h("button", { class: "btn ghost", onClick: () => { persistSid(""); refresh(); } }, "开始新的校时"),
      ]));
      return card;
    }

    async function showReadings(sess, imp) {
      const overlay = h("div", {
        class: "ssync-modal",
        onClick: (e) => { if (e.target === overlay) overlay.remove(); },
      }, []);
      const box = h("div", { class: "card ssync-modal-card" });
      box.appendChild(h("div", { class: "section-head" }, [
        h("h2", {}, `读数 · ${imp.original_filename}`),
        h("button", { class: "link", onClick: () => overlay.remove() }, "关闭 ✕"),
      ]));
      const a = {};
      (sess.anchors || []).forEach((x) => (a[x.kind] = x));
      const filterInput = h("input", { placeholder: "筛选（重量/行内容）", autocomplete: "off" });
      const defaultW = a.start && a.start.observed_weight_g;
      if (defaultW) filterInput.value = String(defaultW);
      const listWrap = h("div", { class: "ssync-readings" }, [h("div", { class: "empty" }, "加载中…")]);
      box.appendChild(h("div", { class: "field" }, [filterInput]));
      box.appendChild(listWrap);
      overlay.appendChild(box);
      document.body.appendChild(overlay);

      async function loadList() {
        listWrap.innerHTML = "";
        listWrap.appendChild(h("div", { class: "empty" }, "加载中…"));
        try {
          const q = filterInput.value.trim();
          const data = await SCALE_SYNC_API.readings(sess.session_id, imp.import_id, q);
          listWrap.innerHTML = "";
          if (!data.items.length) {
            listWrap.appendChild(h("div", { class: "empty" }, "没有匹配的读数"));
            return;
          }
          data.items.forEach((r) => {
            listWrap.appendChild(
              h("div", { class: "list-item ssync-reading" }, [
                h("div", { class: "li-main" }, [
                  h("div", { class: "li-title" }, `#${r.source_line_no} · ${r.weight_g} ${r.unit || ""}`),
                  h("div", { class: "li-sub" }, `${new Date(r.scale_epoch_ms).toLocaleString("zh-CN")} · ${r.raw_line}`),
                ]),
                h("div", { class: "ssync-match-btns" }, [
                  h("button", { class: "btn ghost ssync-mini", onClick: () => doMatch("start", r) }, "设为开始"),
                  h("button", { class: "btn ghost ssync-mini", onClick: () => doMatch("end", r) }, "设为结束"),
                ]),
              ])
            );
          });
        } catch (err) { listWrap.innerHTML = ""; listWrap.appendChild(h("div", { class: "empty" }, err.message)); }
      }

      async function doMatch(kind, r) {
        try {
          await SCALE_SYNC_API.matchAnchor(sess.session_id, kind, {
            import_id: imp.import_id, source_line_no: r.source_line_no,
          });
          toast(`${kind === "start" ? "开始" : "结束"}锚点已绑定行 #${r.source_line_no}`);
          overlay.remove();
          refresh();
        } catch (err) { toast(err.message); }
      }

      filterInput.addEventListener("input", () => { clearTimeout(filterInput._t); filterInput._t = setTimeout(loadList, 250); });
      loadList();
    }

    await refresh();
    return () => clockCtl.stop();
  }

  /* ------------------------------------------------------------------ *
   * 路由注册
   * ------------------------------------------------------------------ */
  route("/", viewHome);
  route("/scan", viewScan);
  route("/record", viewRecord);
  route("/done", viewDone);
  route("/box/:cageId", viewBoxRecords);
  route("/mouse/:recordId", viewMouse);
  route("/manage", viewManage);
  route("/manage/new", viewBoxNew);
  route("/settings", viewSettings);
  route("/scale-sync", viewScaleSync);

  const HERO_SVG = `
<svg viewBox="0 0 320 180" fill="none" xmlns="http://www.w3.org/2000/svg">
  <rect x="70" y="120" width="180" height="16" rx="6" fill="#e9ecef"/>
  <rect x="96" y="96" width="128" height="30" rx="6" fill="#ffffff" stroke="#dee2e6"/>
  <rect x="150" y="104" width="60" height="16" rx="3" fill="#1b1f22"/>
  <text x="180" y="117" font-size="12" fill="#28a745" text-anchor="middle" font-family="monospace">22.43g</text>
  <ellipse cx="130" cy="92" rx="34" ry="18" fill="#adb5bd"/>
  <circle cx="104" cy="86" r="6" fill="#adb5bd"/>
  <circle cx="102" cy="84" r="2" fill="#495057"/>
  <path d="M150 78 q22 -14 30 6" stroke="#adb5bd" stroke-width="4" fill="none" stroke-linecap="round"/>
</svg>`;

  render();
})();
