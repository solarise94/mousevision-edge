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
      "video/mp4",
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
    const titleEl = h("h1", {}, `录制准备 · ${box.cageId}`);
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

    // Hidden source video (camera decode). Visible canvas is what the user
    // sees and what MediaRecorder captures — same 720×1280 pixels.
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
    const timer = h("b", {}, "00:00:00");
    const recTimer = h("div", { class: "rec-timer", hidden: true }, [
      h("span", { class: "rec-dot" }),
      timer,
    ]);
    const shutter = h("button", {
      class: "shutter idle",
      type: "button",
      "aria-label": "开始/结束录制",
    });
    const hint = h("div", { class: "rec-hint" }, "绿框放完整小鼠和秤盘，黄框放体重显示屏");
    const guides = h("div", { class: "weighing-guides", "aria-hidden": "true" }, [
      h("div", { class: "capture-guide mouse-guide" }, [h("span", {}, "小鼠称重区（秤盘）")]),
      h("div", { class: "framing-hint" }, "两个区域都清晰后再开始录制"),
      h("div", { class: "capture-guide weight-guide" }, [h("span", {}, "体重读数区（显示屏）")]),
    ]);
    // Canvas + guides share one 9:16 capture-viewport so display == recording pixels.
    // Shutter / timer / hint live in rec-dock outside the video (no LCD overlap).
    const viewport = h("div", { class: "capture-viewport" }, [
      video,
      canvas,
      guides,
    ]);
    const viewportHost = h("div", { class: "record-viewport-host" }, [viewport]);
    const dock = h("div", { class: "rec-dock" }, [
      recTimer,
      h("div", { class: "rec-dock-actions" }, [shutter]),
      hint,
    ]);
    const stage = h("div", { class: "camera-stage record-stage" }, [
      viewportHost,
      dock,
    ]);
    const screen = h("div", { class: "screen camera-screen record-screen" }, [
      appbar("", {
        back: "/",
        titleNode: titleEl,
        right: switchCamBtn,
      }),
      stage,
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

    function setShutterArmed(armed) {
      if (armed) {
        shutter.disabled = false;
        shutter.classList.remove("waiting");
      } else {
        shutter.disabled = true;
        shutter.classList.add("waiting");
      }
    }
    setShutterArmed(false);

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
        if (!paintedReady) {
          paintedReady = true;
          setShutterArmed(true);
        }
        return true;
      } catch (_) {
        // Transient WebView draw failures: keep the loop alive, do not arm shutter.
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

    function setHint(text, warn) {
      hint.textContent = text || "";
      hint.classList.toggle("warn", !!warn);
    }

    function disableRecording(reason) {
      useCanvas = false;
      paintedReady = false;
      setShutterArmed(false);
      setHint(reason || "当前浏览器无法进行网页录像，请更换浏览器后重试", true);
      shutter.classList.add("hidden");
      switchCamBtn.hidden = true;
      stopDraw();
      stopStream(stream);
      stream = null;
      if (canvasStream) {
        canvasStream.getTracks().forEach((t) => t.stop());
        canvasStream = null;
      }
    }

    async function startCamera(deviceId) {
      paintedReady = false;
      setShutterArmed(false);
      stopStream(stream);
      stream = await openBackCamera(video, deviceId || undefined);
      const settings = trackSettings(stream);
      currentDeviceId = settings.deviceId || deviceId || null;
      lastSourceSize = videoSourceSize(video, stream);
      const facing = (settings.facingMode || "").toLowerCase();
      if (facing && facing !== "environment") {
        setHint("当前可能是前置摄像头，请点右上角「切换」", true);
        switchCamBtn.hidden = false;
        toast("未检测到后置摄像头，请切换");
      } else {
        setHint("绿框放完整小鼠和秤盘，黄框放体重显示屏", false);
      }
      try {
        videoInputs = await listVideoInputs();
        if (videoInputs.length > 1) switchCamBtn.hidden = false;
      } catch (_) {}
    }

    switchCamBtn.addEventListener("click", async () => {
      if (recording || switchCamBtn.disabled) return;
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

    (async () => {
      if (!useCanvas) {
        disableRecording("浏览器不支持网页录像，请更换浏览器后重试");
        return;
      }
      try {
        await startCamera();
        drawing = true;
        scheduleDraw();
        if (window.screen && window.screen.orientation && window.screen.orientation.lock) {
          window.screen.orientation.lock("portrait").catch(() => {});
        }
      } catch (err) {
        disableRecording("无法打开实时相机，请确认 HTTPS 与摄像头权限");
      }
    })();

    function tick() {
      const s = Math.floor((Date.now() - startedAt) / 1000);
      timer.textContent = `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
    }

    function startRec() {
      if (!useCanvas || !stream || !window.MediaRecorder || typeof canvas.captureStream !== "function") {
        toast("当前浏览器不支持网页录像，请更换浏览器后重试");
        disableRecording();
        return;
      }
      // Require a successful paint — never record black/frozen frames.
      if (!paintFrame() || !paintedReady) {
        toast("画面未就绪，请稍候再录");
        return;
      }
      chunks = [];
      try {
        canvasStream = canvas.captureStream(15);
      } catch (err) {
        toast("无法录制当前画面，请稍后重试");
        disableRecording("无法录制当前画面，请更换浏览器后重试");
        return;
      }
      const mime = pickMime();
      if (!mime) {
        toast("当前浏览器不支持 MP4/H.264 录像，请更换浏览器后重试");
        disableRecording("当前浏览器不支持 MP4/H.264 录像");
        return;
      }
      const opts = { videoBitsPerSecond: 1500000, mimeType: mime };
      try { recorder = new MediaRecorder(canvasStream, opts); }
      catch (err2) {
        toast("无法启动 MP4 录像，请更换浏览器后重试");
        disableRecording();
        return;
      }
      recorder.addEventListener("dataavailable", (e) => {
        if (e.data && e.data.size) chunks.push(e.data);
      });
      recorder.addEventListener("stop", () => {
        clearInterval(clockTimer);
        const type = recorder.mimeType || mime || "video/mp4";
        const ext = "mp4";
        const blob = new Blob(chunks, { type });
        const durationSec = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
        // Freeze meta before stopping the camera track (hdjeldn: never refresh
        // geometry after stopStream zeros videoWidth).
        const meta = buildCaptureMeta("canvas");
        stopStream(stream);
        stream = null;
        if (canvasStream) {
          canvasStream.getTracks().forEach((t) => t.stop());
          canvasStream = null;
        }
        doUpload(blob, `mv-${Date.now()}.${ext}`, durationSec, {
          captureMode: "canvas",
          captureMeta: meta,
        });
      });
      // No timeslice: one complete container on stop() (Android fMP4 pitfall).
      recorder.start();
      recording = true;
      startedAt = Date.now();
      tick();
      clockTimer = setInterval(tick, 500);
      recTimer.hidden = false;
      shutter.classList.remove("idle");
      shutter.classList.add("recording");
      switchCamBtn.disabled = true;
      setTitle(`录制中 · ${box.cageId}`);
      setHint("录制中：保持小鼠在绿框、体重数字在黄框内", false);
    }

    function stopRec() {
      if (recorder && recording) {
        recording = false;
        recorder.stop();
      }
    }

    shutter.addEventListener("click", () => (recording ? stopRec() : startRec()));

    function doUpload(blob, filename, durationSec, uploadOpts) {
      stopDraw();
      stopStream(stream);
      stream = null;
      setTitle("上传中");
      renderUploading(box, blob, filename, durationSec, uploadOpts || {});
    }

    return () => {
      document.documentElement.classList.remove("camera-mode", "record-light");
      clearInterval(clockTimer);
      stopDraw();
      if (viewportObserver) {
        try { viewportObserver.disconnect(); } catch (_) {}
        viewportObserver = null;
      } else {
        window.removeEventListener("resize", layoutViewport);
      }
      if (window.screen && window.screen.orientation && window.screen.orientation.unlock) {
        try { window.screen.orientation.unlock(); } catch (_) {}
      }
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
