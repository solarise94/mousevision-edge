const $ = (id) => document.getElementById(id);

let currentMouseNo = null;
let currentCageId = "C57-023";
let currentRunId = null;
let view = "list"; // list | detail
let pollTimer = null;
let batchMode = false;
let lastKnownCount = 0;
let playbackActive = false;
let playbackToken = null;
let historySnapshot = null;

function fmtRec(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `REC ${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

function fmtWeight(w) {
  if (w == null || Number.isNaN(Number(w))) return "-- g";
  return `${Number(w).toFixed(2)} g`;
}

function drawCurve(points) {
  const canvas = $("curve");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "rgba(255,255,255,0.03)";
  ctx.fillRect(0, 0, w, h);
  if (!points || points.length < 2) return;

  const ys = points.map((p) => p.w);
  const minY = Math.min(...ys, 0);
  const maxY = Math.max(...ys, 1);
  const span = Math.max(0.5, maxY - minY);

  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.beginPath();
  for (let i = 1; i <= 3; i++) {
    const y = (h * i) / 4;
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
  }
  ctx.stroke();

  ctx.strokeStyle = "#3ddc84";
  ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = (i / (points.length - 1)) * (w - 8) + 4;
    const y = h - ((p.w - minY) / span) * (h - 12) - 6;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function applyHistorySnapshot() {
  if (!historySnapshot) return;
  const h = historySnapshot;
  $("weight").textContent = fmtWeight(h.weight);
  $("score").textContent =
    h.confidence != null ? Number(h.confidence).toFixed(2) : "-";
  $("stable").innerHTML = '<span class="stable-on">✓ 稳定</span>';
  $("state").textContent = "HISTORY";
  $("note").textContent = "历史记录 · 点击只读复核播放该只片段";
  $("message").textContent = "已加载历史记录";
  $("hint").textContent = "只读复核不会写入新记录";
  $("recChip").hidden = true;
  if (h.photoUrl) {
    $("photoPreviewWrap").hidden = false;
    $("photoPreview").src = `${h.photoUrl}?t=${Date.now()}`;
    $("photoLink").hidden = false;
    $("photoLink").href = h.photoUrl;
    // Idle: show stable photo in main viewport instead of black stream.
    $("stream").src = `${h.photoUrl}?t=${Date.now()}`;
  }
}

function showList() {
  view = "list";
  playbackActive = false;
  playbackToken = null;
  historySnapshot = null;
  batchMode = false;
  $("batchBanner").hidden = true;
  $("viewList").hidden = false;
  $("viewDetail").hidden = true;
  $("listControls").hidden = false;
  $("detailControls").hidden = true;
  loadRuns().then(loadMice);
}

function showDetail(mouseNo, cageId, opts = {}) {
  view = "detail";
  batchMode = false;
  $("batchBanner").hidden = true;
  currentMouseNo = mouseNo;
  currentCageId = cageId || currentCageId;
  currentRunId = opts.runId || currentRunId;
  $("viewList").hidden = true;
  $("viewDetail").hidden = false;
  $("listControls").hidden = true;
  $("detailControls").hidden = false;
  $("detailBadge").textContent = `第 ${String(mouseNo).padStart(2, "0")} 只 / Current Record`;
  $("mouseIndex").textContent = String(mouseNo).padStart(2, "0");
  $("boxIdLabel").textContent = currentCageId;
  $("qrText").textContent = `箱号 ${currentCageId}`;

  if (opts.weight != null) {
    historySnapshot = {
      weight: opts.weight,
      confidence: opts.confidence,
      photoUrl: opts.photoUrl || null,
      ordinal: mouseNo,
      runId: currentRunId,
      cageId: currentCageId,
    };
    playbackActive = false;
    playbackToken = null;
    applyHistorySnapshot();
  } else {
    historySnapshot = null;
    $("weight").textContent = "-- g";
    $("score").textContent = "-";
    $("stable").innerHTML = '<span class="stable-off">等待</span>';
    $("note").textContent = "待称量";
    $("message").textContent = "点击重新分析并保存";
    $("photoPreviewWrap").hidden = true;
  }

  if (opts.autoStart) {
    if (opts.weight != null && currentRunId && mouseNo != null) {
      // History card: play that mouse's clip read-only.
      startPlayback({
        continuous: false,
        persist: false,
        runId: currentRunId,
        ordinal: mouseNo,
      });
    } else {
      // New single weighing batch from video start.
      startPlayback({ continuous: false, persist: true });
    }
  }
}
async function loadRuns() {
  const res = await fetch("/api/runs");
  const data = await res.json();
  const sel = $("runSelect");
  const items = data.items || [];
  const active = data.active_run_id;
  sel.innerHTML = "";
  if (!items.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "暂无批次";
    sel.appendChild(opt);
    currentRunId = null;
    return data;
  }
  items.forEach((r) => {
    const opt = document.createElement("option");
    opt.value = r.run_id;
    const n = r.record_count || 0;
    const when = (r.started_at || "").replace("T", " ").slice(0, 19);
    opt.textContent = `${r.cage_id} · ${n}只 · ${when}`;
    if (r.run_id === active) opt.selected = true;
    sel.appendChild(opt);
  });
  currentRunId = active || items[0].run_id;
  const activeRun = items.find((r) => r.run_id === currentRunId);
  if (activeRun && activeRun.cage_id) {
    $("cageId").value = activeRun.cage_id;
    currentCageId = activeRun.cage_id;
  }
  return data;
}

async function loadMice() {
  const q = currentRunId ? `?run_id=${encodeURIComponent(currentRunId)}` : "";
  const res = await fetch(`/api/mice${q}`);
  const data = await res.json();
  const items = data.items || [];
  lastKnownCount = items.length;
  const cage = data.cage_id || currentCageId || "-";
  $("btnNew").textContent = `+ 新批次单只`;

  const grid = $("mouseGrid");
  grid.innerHTML = "";
  $("listEmpty").hidden = items.length > 0;

  const reviewN = items.filter((m) => m.needs_review).length;
  const cleanN = items.length - reviewN;
  $("listStat").textContent =
    `箱 ${cage} · 本批次 ${items.length} 只` +
    (items.length ? ` · 干净 ${cleanN} · 待复核 ${reviewN}` : "");

  items.forEach((m) => {
    const card = document.createElement("button");
    const needsReview = !!m.needs_review;
    card.className = needsReview ? "mouse-card needs-review" : "mouse-card";
    card.type = "button";
    const ordinal = m.ordinal ?? m.index;
    const statusLabel = needsReview
      ? "待复核"
      : `评分 ${m.confidence != null ? Number(m.confidence).toFixed(2) : "-"}`;
    card.innerHTML = `
      <div class="thumb">
        <img src="${m.photo_url}?size=thumb" alt="mouse ${ordinal}" loading="lazy" />
        <span class="idx">#${String(ordinal).padStart(2, "0")}</span>
        ${needsReview ? '<span class="review-badge">待复核</span>' : ""}
      </div>
      <div class="card-body">
        <div class="card-title">${m.label || "第 " + String(ordinal).padStart(2, "0") + " 只"}</div>
        <div class="card-weight">${fmtWeight(m.weight)}</div>
        <div class="card-meta">
          <span>${m.cage_id || m.box_id || "-"}</span>
          <span>${statusLabel}</span>
        </div>
        <div class="card-time">${(m.timestamp || "").replace("T", " ")}</div>
      </div>
    `;
    card.addEventListener("click", () => {
      showDetail(ordinal, m.cage_id || m.box_id, {
        photoUrl: m.photo_url,
        weight: m.weight,
        confidence: m.confidence,
        runId: m.run_id,
        autoStart: true,
      });
    });
    grid.appendChild(card);
  });
}

function renderStatus(status) {
  if (batchMode) {
    $("batchBanner").hidden = false;
    $("batchText").textContent =
      status.message ||
      `整段回放中 · 已检出 ${status.session_count || 0} 只`;
    if (!status.playing) {
      batchMode = false;
      playbackActive = false;
      $("batchBanner").hidden = true;
      if (status.run_id) currentRunId = status.run_id;
      loadRuns().then(loadMice);
    } else if ((status.session_count || 0) !== lastKnownCount) {
      loadMice();
    }
    return;
  }

  if (view !== "detail") return;

  // Idle history view: do not let global EMPTY status wipe the selected mouse.
  if (!playbackActive) {
    applyHistorySnapshot();
    return;
  }
  if (playbackToken && status.token && status.token !== playbackToken) {
    return;
  }

  $("clock").textContent = status.timestamp || "--";
  $("strain").textContent = status.strain || "-";
  $("mouseIndex").textContent = String(
    status.target_ordinal ?? status.mouse_no ?? currentMouseNo ?? "-"
  ).padStart(2, "0");
  $("boxIdLabel").textContent = status.cage_id || status.box_id || currentCageId;
  $("state").textContent = status.state || "EMPTY";
  $("message").textContent = status.message || "";
  $("hint").textContent = status.hint || "";
  $("detailBadge").textContent = `第 ${String(
    status.target_ordinal ?? status.mouse_no ?? currentMouseNo ?? 0
  ).padStart(2, "0")} 只 / ${status.persist === false ? "只读复核" : "Current Record"}`;

  const w = status.weight;
  if (w != null) {
    $("weight").textContent = fmtWeight(w);
  }
  if (status.confidence != null && Number(status.confidence) > 0) {
    $("score").textContent = Number(status.confidence).toFixed(2);
  }

  if (status.stable) {
    $("stable").innerHTML = '<span class="stable-on">✓ 稳定</span>';
  } else if (!status.saved) {
    $("stable").innerHTML = '<span class="stable-off">等待</span>';
  }

  const recChip = $("recChip");
  if (status.recording) {
    recChip.hidden = false;
    $("recText").textContent = fmtRec(status.rec_seconds);
  } else {
    recChip.hidden = true;
  }

  const steps = status.steps || [];
  document.querySelectorAll("#steps li").forEach((li) => {
    li.classList.remove("done", "active", "todo");
    const step = steps.find((s) => s.key === li.dataset.key);
    if (step) li.classList.add(step.status);
  });

  if (status.saved && status.persist !== false) {
    $("note").textContent = "已保存到新批次";
    const rid = status.run_id || "";
    const photoOrdinal =
      status.saved_ordinal ?? status.last_saved_index ?? status.mouse_no;
    const photoUrl = `/api/mice/${photoOrdinal}/photo?run_id=${encodeURIComponent(rid)}`;
    $("photoPreviewWrap").hidden = false;
    $("photoPreview").src = `${photoUrl}&t=${Date.now()}`;
    $("photoLink").hidden = false;
    $("photoLink").href = photoUrl;
    if (status.target_ordinal != null && status.saved_ordinal != null) {
      $("detailBadge").textContent = `来源 #${String(
        status.target_ordinal
      ).padStart(2, "0")} → 新批次 #${String(status.saved_ordinal).padStart(2, "0")}`;
    }
  }

  if (!status.playing) {
    playbackActive = false;
    if (historySnapshot && status.persist === false) {
      // Keep historical weight after review ends.
      applyHistorySnapshot();
      $("message").textContent = status.message || "复核完成（未写入）";
      $("note").textContent = "复核完成 · 历史记录未改动";
    }
  }

  drawCurve(status.curve || []);
}

async function poll() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    renderStatus(data);
  } catch (e) {
    if (view === "detail" && playbackActive) $("message").textContent = "状态拉取失败";
  }
}

async function startPlayback({ continuous, persist, runId, ordinal }) {
  const speed = continuous ? $("batchSpeed").value : $("speed").value;
  const cageId = ($("cageId")?.value || currentCageId || "C57-023").trim();
  currentCageId = cageId;
  const rid = runId || currentRunId;
  const ord = ordinal != null ? ordinal : currentMouseNo;

  playbackActive = true;
  const res = await apiFetch("/api/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      cage_id: cageId,
      speed: Number(speed),
      continuous,
      persist,
      run_id: !continuous && rid ? rid : null,
      ordinal: !continuous && ord != null ? Number(ord) : null,
    }),
  });
  const data = await res.json();
  if (res.status === 409 || data.error === "busy") {
    playbackActive = false;
    alert(data.message || "上一次回放尚未结束");
    return data;
  }
  if (res.status === 404 || data.error === "mouse_not_found") {
    playbackActive = false;
    alert(data.message || "未找到该鼠记录");
    return data;
  }
  playbackToken = data.token || null;
  if (data.run_id && persist) currentRunId = data.run_id;
  if (!continuous) {
    $("stream").src = `/api/stream?t=${Date.now()}`;
    if (persist) $("photoPreviewWrap").hidden = true;
    $("note").textContent = persist
      ? "回放中（将保存）"
      : `只读复核第 ${String(ord).padStart(2, "0")} 只片段`;
  }
  poll();
  return data;
}

$("btnBack").addEventListener("click", async () => {
  await apiFetch("/api/stop", { method: "POST" });
  playbackActive = false;
  playbackToken = null;
  showList();
});

$("btnNew").addEventListener("click", async () => {
  const cageId = ($("cageId").value || "C57-023").trim();
  historySnapshot = null;
  showDetail(1, cageId, { autoStart: true, weight: null });
});

$("btnBatch").addEventListener("click", async () => {
  if (
    !confirm(
      "将创建新的称量批次（不删除旧批次），用参考视频整段跑完。继续？"
    )
  )
    return;
  await loadMice();
  batchMode = true;
  playbackActive = true;
  $("batchBanner").hidden = false;
  $("batchText").textContent = "整段回放中…";
  await startPlayback({ continuous: true, persist: true });
});

$("btnBatchStop").addEventListener("click", async () => {
  await apiFetch("/api/stop", { method: "POST" });
  batchMode = false;
  playbackActive = false;
  $("batchBanner").hidden = true;
  loadRuns().then(loadMice);
});

$("btnReset").addEventListener("click", async () => {
  if (!confirm("清空全部批次与鼠只记录？\n需要先在 /pc 以管理员身份登录。")) return;
  const res = await apiFetch("/api/reset", { method: "POST" });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (_) {}
    alert(`清空失败（${res.status}）：${detail}\n请打开 /pc 使用管理员账号登录后再试。`);
    return;
  }
  currentRunId = null;
  loadRuns().then(loadMice);
});

$("runSelect").addEventListener("change", async () => {
  const runId = $("runSelect").value;
  if (!runId) return;
  await apiFetch(`/api/runs/active?run_id=${encodeURIComponent(runId)}`, {
    method: "POST",
  });
  currentRunId = runId;
  loadMice();
});

$("btnReview").addEventListener("click", () => {
  startPlayback({
    continuous: false,
    persist: false,
    runId: currentRunId,
    ordinal: currentMouseNo,
  });
});

$("btnReanalyze").addEventListener("click", () => {
  if (!confirm("将创建新批次并重新分析该只片段后保存。继续？")) return;
  startPlayback({
    continuous: false,
    persist: true,
    runId: currentRunId,
    ordinal: currentMouseNo,
  });
});

$("btnStop").addEventListener("click", async () => {
  await apiFetch("/api/stop", { method: "POST" });
  playbackActive = false;
  poll();
});

// ---- Agent compare lab ----
const cmpState = {
  source: "local", // "local" | "platform"
  localFile: null, // File | null
  localObjectUrl: null, // string | null
  platformRunId: null, // selected run_id | null
  platformVideos: [], // cached list from /api/lab/videos
  running: false,
  stopwatchTimer: null,
  stopwatchStart: 0,
};

function showLabView(name) {
  const isCompare = name === "compare";
  $("viewCompare").hidden = !isCompare;
  $("viewList").hidden = isCompare;
  $("viewDetail").hidden = true;
  $("listControls").hidden = isCompare;
  $("detailControls").hidden = true;
  $("tabBatch").classList.toggle("active", !isCompare);
  $("tabCompare").classList.toggle("active", isCompare);
  if (!isCompare) {
    showList();
  } else {
    // stop polling stream UI noise while comparing
    playbackActive = false;
    if (!cmpState.platformVideos.length) {
      loadPlatformVideos();
    }
    refreshHistoryDropdown();
  }
}

function fmtDelta(d) {
  if (d == null || Number.isNaN(Number(d))) return "—";
  return Number(d).toFixed(2);
}

function fmtBytes(n) {
  if (!n && n !== 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function setCmpStatus(kind, text) {
  // kind: ready | busy | ok | err
  const el = $("cmpStatus");
  el.className = `cmp-status ${kind}`;
  el.textContent = text;
}

// ----- Source: segmented control -----
function setCmpSource(src) {
  cmpState.source = src;
  document.querySelectorAll("#cmpSourceSeg .cmp-seg-btn").forEach((b) => {
    const on = b.dataset.src === src;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  $("cmpLocalPanel").hidden = src !== "local";
  $("cmpPlatformPanel").hidden = src !== "platform";
  validateCmpReady();
}

document.querySelectorAll("#cmpSourceSeg .cmp-seg-btn").forEach((b) => {
  b.addEventListener("click", () => setCmpSource(b.dataset.src));
});

// ----- Local: drag/drop + click -----
function clearLocalFile() {
  if (cmpState.localObjectUrl) {
    URL.revokeObjectURL(cmpState.localObjectUrl);
    cmpState.localObjectUrl = null;
  }
  cmpState.localFile = null;
  $("cmpFileChip").hidden = true;
  const v = $("cmpPreviewVideo");
  v.hidden = true;
  v.removeAttribute("src");
  v.load();
  $("cmpFileInput").value = "";
  validateCmpReady();
}

function setLocalFile(file) {
  if (!file) return;
  if (!/^video\//.test(file.type) && !/\.(mp4|mov|webm|m4v|avi)$/i.test(file.name)) {
    setCmpStatus("err", "仅支持视频文件");
    return;
  }
  if (cmpState.localObjectUrl) URL.revokeObjectURL(cmpState.localObjectUrl);
  cmpState.localFile = file;
  cmpState.localObjectUrl = URL.createObjectURL(file);
  $("cmpChipName").textContent = file.name;
  const chip = $("cmpFileChip");
  chip.hidden = false;
  const v = $("cmpPreviewVideo");
  v.src = cmpState.localObjectUrl;
  v.hidden = false;
  v.onloadedmetadata = () => {
    const dur = v.duration && Number.isFinite(v.duration) ? `${v.duration.toFixed(1)}s` : "—";
    $("cmpChipMeta").textContent = `${fmtBytes(file.size)} · ${dur}`;
  };
  validateCmpReady();
}

const dz = $("cmpDropzone");
dz.addEventListener("click", () => $("cmpFileInput").click());
dz.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    $("cmpFileInput").click();
  }
});
$("cmpFileInput").addEventListener("change", (e) => {
  const f = e.target.files && e.target.files[0];
  if (f) setLocalFile(f);
});
$("cmpChipX").addEventListener("click", (e) => {
  e.stopPropagation();
  clearLocalFile();
});

["dragenter", "dragover"].forEach((ev) => {
  dz.addEventListener(ev, (e) => {
    e.preventDefault();
    e.stopPropagation();
    dz.classList.add("cmp-drag");
  });
});
["dragleave", "dragend", "drop"].forEach((ev) => {
  dz.addEventListener(ev, (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (ev !== "drop") dz.classList.remove("cmp-drag");
  });
});
dz.addEventListener("drop", (e) => {
  dz.classList.remove("cmp-drag");
  const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (f) setLocalFile(f);
});

// ----- Platform videos grid -----
async function loadPlatformVideos() {
  const grid = $("cmpVideoGrid");
  grid.innerHTML = `<div class="cmp-skeleton-row"></div><div class="cmp-skeleton-row"></div>`;
  $("cmpVideoEmpty").hidden = true;
  try {
    const res = await apiFetch("/api/lab/videos");
    if (!res.ok) throw new Error("加载失败");
    const data = await res.json();
    cmpState.platformVideos = data.items || [];
  } catch (e) {
    cmpState.platformVideos = [];
  }
  renderPlatformVideos();
}

function renderPlatformVideos() {
  const grid = $("cmpVideoGrid");
  grid.innerHTML = "";
  const q = ($("cmpVideoSearch").value || "").trim().toLowerCase();
  const items = cmpState.platformVideos.filter((v) => {
    if (!q) return true;
    return (
      String(v.cage_id || "").toLowerCase().includes(q) ||
      String(v.run_id || "").toLowerCase().includes(q)
    );
  });
  $("cmpPlatformCount").textContent = items.length
    ? `${items.length} 个视频`
    : "";
  if (!items.length) {
    $("cmpVideoEmpty").hidden = false;
    return;
  }
  $("cmpVideoEmpty").hidden = true;
  items.forEach((v) => {
    const card = document.createElement("div");
    card.className = "cmp-video-card";
    card.dataset.runId = v.run_id;
    if (v.run_id === cmpState.platformRunId) card.classList.add("selected");
    const cage = v.cage_id || "-";
    const started = v.started_at
      ? new Date(v.started_at).toLocaleString("zh-CN", { hour12: false })
      : "—";
    card.innerHTML = `
      <div class="cmp-poster-wrap">
        <img loading="lazy" class="cmp-poster" alt=""
          src="/api/lab/videos/${encodeURIComponent(v.run_id)}/poster" />
        <div class="cmp-check" aria-hidden="true">✓</div>
      </div>
      <div class="cmp-card-body">
        <div class="cmp-card-cage">${escapeHtml(cage)}</div>
        <div class="cmp-card-meta">${v.record_count ?? 0} 只 · ${fmtBytes(v.size_bytes)}</div>
        <div class="cmp-card-time">${escapeHtml(started)}</div>
        <code class="cmp-card-run">${escapeHtml(v.run_id || "")}</code>
      </div>`;
    card.addEventListener("click", () => {
      cmpState.platformRunId = v.run_id;
      document.querySelectorAll(".cmp-video-card").forEach((c) =>
        c.classList.remove("selected")
      );
      card.classList.add("selected");
      validateCmpReady();
    });
    grid.appendChild(card);
  });
}

$("cmpVideoSearch").addEventListener("input", renderPlatformVideos);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ----- Readiness / button enable -----
function validateCmpReady() {
  if (cmpState.running) return;
  let ok = false;
  if (cmpState.source === "local") ok = !!cmpState.localFile;
  else ok = !!cmpState.platformRunId;
  $("btnCompare").disabled = !ok;
}
$("cmpRunAgent").addEventListener("change", validateCmpReady);
$("cmpRunClassic").addEventListener("change", validateCmpReady);

// ----- Stopwatch + step progress -----
function startStopwatch() {
  cmpState.stopwatchStart = performance.now();
  $("cmpStopwatch").textContent = "0.0s";
  cmpState.stopwatchTimer = setInterval(() => {
    const elapsed = (performance.now() - cmpState.stopwatchStart) / 1000;
    $("cmpStopwatch").textContent = `${elapsed.toFixed(1)}s`;
  }, 100);
}
function stopStopwatch() {
  if (cmpState.stopwatchTimer) clearInterval(cmpState.stopwatchTimer);
  cmpState.stopwatchTimer = null;
}

function buildSteps(runAgent, runClassic) {
  const steps = [
    { key: "upload", label: "上传视频" },
    { key: "agent", label: "Agent 读数" },
    { key: "classic", label: "经典算法" },
    { key: "align", label: "对齐汇总" },
  ];
  return steps.filter((s) => {
    if (s.key === "agent") return runAgent;
    if (s.key === "classic") return runClassic;
    return true;
  });
}

function markStep(index, state) {
  // state: pending | active | done
  const ol = $("cmpSteps");
  const li = ol.children[index];
  if (!li) return;
  li.classList.remove("pending", "active", "done");
  li.classList.add(state);
}

function startProgress(runAgent, runClassic) {
  const ol = $("cmpSteps");
  ol.innerHTML = "";
  const steps = buildSteps(runAgent, runClassic);
  steps.forEach((s, i) => {
    const li = document.createElement("li");
    li.className = "pending";
    li.dataset.index = String(i);
    li.innerHTML = `<span class="cmp-step-no">${i + 1}</span><span class="cmp-step-label">${s.label}</span>`;
    ol.appendChild(li);
  });
  $("cmpProgress").hidden = false;
  markStep(0, "active");
  startStopwatch();
  // Optimistic staged animation while single long request is in flight.
  const order = steps.map((_, i) => i);
  let cursor = 0;
  cmpState._progressCursor = 0;
  cmpState._progressTimer = setInterval(() => {
    if (cursor >= order.length) return;
    markStep(cursor, "active");
  }, 1000);
  // Advance slowly through the steps to give visual feedback.
  cmpState._advanceTimer = setInterval(() => {
    const stepsNow = ol.children.length;
    if (cmpState._progressCursor >= stepsNow - 1) return;
    markStep(cmpState._progressCursor, "done");
    cmpState._progressCursor += 1;
    markStep(cmpState._progressCursor, "active");
  }, 8000);
}

function finishProgress(success) {
  if (cmpState._advanceTimer) clearInterval(cmpState._advanceTimer);
  if (cmpState._progressTimer) clearInterval(cmpState._progressTimer);
  cmpState._advanceTimer = null;
  cmpState._progressTimer = null;
  const ol = $("cmpSteps");
  for (let i = 0; i < ol.children.length; i++) {
    markStep(i, success ? "done" : "pending");
  }
  stopStopwatch();
}

// ----- Recent compare history dropdown -----
async function refreshHistoryDropdown() {
  const sel = $("cmpHistory");
  if (!sel) return;
  try {
    const res = await apiFetch("/api/lab/compares");
    const data = await res.json();
    const items = data.items || [];
    const prev = sel.value;
    sel.innerHTML = '<option value="">最近对照…</option>';
    items.forEach((it) => {
      const opt = document.createElement("option");
      opt.value = it.compare_id;
      const cage = it.cage_id || "?";
      const agent = it.n_agent != null ? `A${it.n_agent}` : "A-";
      const classic = it.n_classic != null ? `C${it.n_classic}` : "C-";
      opt.textContent = `${cage} · ${agent}/${classic} · ${it.compare_id}`;
      sel.appendChild(opt);
    });
    if (prev) sel.value = prev;
  } catch (e) {
    /* ignore */
  }
}

$("cmpHistory")?.addEventListener("change", async (e) => {
  const id = e.target.value;
  if (!id) return;
  try {
    const res = await apiFetch(`/api/lab/compare/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error("加载失败");
    const data = await res.json();
    setCmpStatus("ok", `已载入历史对照 · ${id}`);
    renderCompareResult(data);
  } catch (err) {
    setCmpStatus("err", `载入失败: ${err.message || err}`);
  }
});

// ----- Lightbox -----
function openLightbox(src) {
  const lb = $("cmpLightbox");
  $("cmpLightboxImg").src = src;
  lb.hidden = false;
}
function closeLightbox() {
  $("cmpLightbox").hidden = true;
  $("cmpLightboxImg").removeAttribute("src");
}
$("cmpLightbox").addEventListener("click", closeLightbox);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("cmpLightbox").hidden) closeLightbox();
});

// ----- Result rendering -----
function pct(num, denom) {
  if (!denom) return "—";
  return `${Math.round((num / denom) * 100)}%`;
}

function renderCompareResult(data) {
  const sum = data.summary || {};
  const branches = data.branches || {};

  $("cmpResult").hidden = false;
  const idText = $("cmpIdText");
  idText.textContent = data.compare_id || "";

  // KPIs
  const kpiBox = $("cmpKpis");
  kpiBox.innerHTML = "";
  const kpis = [
    {
      k: "|Δ|≤0.1 一致率",
      v:
        sum.match_0_1 != null && sum.n_comparable
          ? pct(sum.match_0_1, sum.n_comparable)
          : "—",
      sub: sum.match_0_1 != null ? `${sum.match_0_1}/${sum.n_comparable || 0}` : "",
      big: true,
    },
    { k: "mean|Δ|", v: sum.mean_delta != null ? Number(sum.mean_delta).toFixed(3) : "—" },
    { k: "Agent 只数", v: sum.n_agent ?? "—" },
    { k: "经典 只数", v: sum.n_classic ?? "—" },
    {
      k: "Agent 耗时",
      v: branches.agent?.elapsed_s != null ? `${branches.agent.elapsed_s}s` : "—",
      tone: "agent",
    },
    {
      k: "经典耗时",
      v: branches.classic?.elapsed_s != null ? `${branches.classic.elapsed_s}s` : "—",
      tone: "classic",
    },
  ];
  kpis.forEach((kpi) => {
    const card = document.createElement("div");
    card.className = "cmp-kpi";
    if (kpi.big) card.classList.add("cmp-kpi-hero");
    if (kpi.tone) card.classList.add(`tone-${kpi.tone}`);
    card.innerHTML = `<div class="cmp-kpi-k">${kpi.k}</div><div class="cmp-kpi-v">${kpi.v}</div>${
      kpi.sub ? `<div class="cmp-kpi-sub">${kpi.sub}</div>` : ""
    }`;
    kpiBox.appendChild(card);
  });

  // Alignment table
  const agentRecs = (branches.agent || {}).records || [];
  const classicRecs = (branches.classic || {}).records || [];
  const agentByOrd = Object.fromEntries(agentRecs.map((r) => [Number(r.ordinal), r]));
  const classicByOrd = Object.fromEntries(classicRecs.map((r) => [Number(r.ordinal), r]));

  const tbody = $("cmpTable").querySelector("tbody");
  tbody.innerHTML = "";
  let rows = data.alignment || [];
  if (!rows.length) {
    const only = agentRecs.length
      ? agentRecs.map((r) => ({
          ordinal: r.ordinal,
          agent_weight: r.weight,
          classic_weight: null,
          delta: null,
        }))
      : classicRecs.map((r) => ({
          ordinal: r.ordinal,
          agent_weight: null,
          classic_weight: r.weight,
          delta: null,
        }));
    rows = only;
  }
  rows.forEach((r) => {
    const tr = document.createElement("tr");
    const d = r.delta;
    if (d != null && d <= 0.1) tr.className = "match-good";
    else if (d != null && d <= 0.5) tr.className = "match-mid";
    else if (d != null) tr.className = "match-bad";
    const ord = r.ordinal;
    tr.dataset.ordinal = String(ord);
    tr.style.cursor = "pointer";
    const aNote = (agentByOrd[Number(ord)] || {}).agent_note ||
      (agentByOrd[Number(ord)] || {}).review_reason || "";
    const cNote = (classicByOrd[Number(ord)] || {}).review_reason ||
      ((classicByOrd[Number(ord)] || {}).needs_review ? "needs_review" : "") || "";
    tr.innerHTML = `
      <td>${ord ?? ""}</td>
      <td>${r.agent_weight != null ? Number(r.agent_weight).toFixed(2) : "—"}</td>
      <td>${r.classic_weight != null ? Number(r.classic_weight).toFixed(2) : "—"}</td>
      <td><span class="cmp-delta">${fmtDelta(d)}</span></td>
      <td class="note">${escapeHtml(aNote)}</td>
      <td class="note">${escapeHtml(cNote)}</td>`;
    tr.addEventListener("click", () => scrollToOrdinal(ord));
    tbody.appendChild(tr);
  });

  // Branch pair panels (agent | classic), aligned by ordinal
  renderBranchPanels(branches);
}

function fmtMs(ms) {
  if (ms == null || Number.isNaN(Number(ms))) return null;
  return `${(Number(ms) / 1000).toFixed(1)}s`;
}

function renderBranchPanels(branches) {
  const wrap = $("cmpBranches");
  wrap.innerHTML = "";
  const hasAny = branches.agent || branches.classic;
  if (!hasAny) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;

  // Collect all ordinals across both branches (sorted)
  const ordSet = new Set();
  ["agent", "classic"].forEach((k) => {
    (branches[k] && branches[k].records ? branches[k].records : []).forEach((r) =>
      ordSet.add(Number(r.ordinal))
    );
  });
  const ordinals = Array.from(ordSet).sort((a, b) => a - b);

  const branchesList = [
    { key: "agent", label: "Agent", tone: "agent", accent: "cmp-agent" },
    {
      key: "classic",
      label: branches.classic ? `经典 · ${branches.classic.reader || ""}` : "经典",
      tone: "classic",
      accent: "cmp-classic",
    },
  ];

  branchesList.forEach((b) => {
    const branch = branches[b.key] || null;
    const col = document.createElement("section");
    col.className = `cmp-branch-col tone-${b.tone}`;
    col.dataset.branch = b.key;

    // Header
    const head = document.createElement("header");
    head.className = "cmp-branch-head";
    const err = branch && branch.error;
    head.innerHTML = `
      <div class="cmp-branch-title">
        <span class="cmp-dot"></span>${escapeHtml(b.label)}
        <span class="cmp-branch-meta">${branch ? `n=${branch.n ?? 0} · ${branch.elapsed_s ?? "—"}s` : "未运行"}</span>
      </div>`;
    col.appendChild(head);
    if (err) {
      const errBar = document.createElement("div");
      errBar.className = "cmp-branch-err";
      errBar.textContent = `错误: ${err}`;
      col.appendChild(errBar);
    }

    ordinals.forEach((ord) => {
      const rec = branch && branch.records
        ? branch.records.find((r) => Number(r.ordinal) === ord)
        : null;
      const card = document.createElement("div");
      card.className = "cmp-mouse-card";
      card.id = `cmp-card-${b.key}-${ord}`;
      if (!rec) {
        card.classList.add("cmp-mouse-empty");
        card.innerHTML = `
          <div class="cmp-card-ord">#${ord}</div>
          <div class="cmp-mouse-empty-body">该分支无此序号</div>`;
        col.appendChild(card);
        return;
      }
      const weight = rec.weight != null ? Number(rec.weight).toFixed(2) : "—";
      const conf = rec.confidence != null ? `${Math.round(Number(rec.confidence) * 100)}%` : null;
      const note = rec.agent_note || rec.review_reason || (rec.needs_review ? "needs_review" : "");
      const winStart = fmtMs(rec.clip_start_ms != null ? rec.clip_start_ms : rec.platform_start_ms);
      const winEnd = fmtMs(rec.clip_end_ms != null ? rec.clip_end_ms : rec.platform_end_ms);
      const hasWindow = winStart && winEnd;
      card.innerHTML = `
        <div class="cmp-card-ord">#${ord}</div>
        ${rec.photo_url ? `<img class="cmp-mouse-photo" loading="lazy" alt="" src="${rec.photo_url}?size=thumb" />` : ""}
        <div class="cmp-mouse-weight">${weight}<span class="cmp-unit"> g</span></div>
        <div class="cmp-mouse-meta">
          ${conf ? `<span class="cmp-conf">置信度 ${conf}</span>` : ""}
          ${hasWindow ? `<span class="cmp-window">⏱ ${winStart} – ${winEnd}</span>` : ""}
        </div>
        ${note ? `<div class="cmp-mouse-note">${escapeHtml(note)}</div>` : ""}
        ${rec.clip_url ? `<video class="cmp-mouse-clip" controls preload="metadata" src="${rec.clip_url}"></video>` : ""}`;
      const photo = card.querySelector(".cmp-mouse-photo");
      if (photo && rec.photo_url) {
        photo.addEventListener("click", () => openLightbox(`${rec.photo_url}?size=full`));
      }
      col.appendChild(card);
    });

    wrap.appendChild(col);
  });
}

function scrollToOrdinal(ord) {
  // Try the agent side first; flash both columns.
  ["agent", "classic"].forEach((k) => {
    const el = document.getElementById(`cmp-card-${k}-${ord}`);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("cmp-flash");
    setTimeout(() => el.classList.remove("cmp-flash"), 1200);
  });
}

$("cmpCopy")?.addEventListener("click", () => {
  const txt = $("cmpIdText").textContent || "";
  navigator.clipboard?.writeText(txt).then(
    () => setCmpStatus("ok", `已复制 compare_id: ${txt}`),
    () => setCmpStatus("err", "复制失败")
  );
});

// ----- Run compare -----
$("tabBatch")?.addEventListener("click", () => showLabView("batch"));
$("tabCompare")?.addEventListener("click", () => showLabView("compare"));

$("btnCompare")?.addEventListener("click", async () => {
  if (cmpState.running) return;
  const runAgent = $("cmpRunAgent").checked;
  const runClassic = $("cmpRunClassic").checked;
  if (!runAgent && !runClassic) {
    setCmpStatus("err", "至少选择一条路径");
    return;
  }
  const fd = new FormData();
  fd.append("cage_id", $("cmpCage").value || "compare");
  fd.append("classic_reader", $("cmpClassic").value || "http_ocr");
  fd.append("run_agent", runAgent ? "true" : "false");
  fd.append("run_classic", runClassic ? "true" : "false");
  let videoLabel = "";
  if (cmpState.source === "local") {
    if (!cmpState.localFile) {
      setCmpStatus("err", "请先选择视频文件");
      return;
    }
    fd.append("video", cmpState.localFile);
    videoLabel = cmpState.localFile.name;
  } else {
    if (!cmpState.platformRunId) {
      setCmpStatus("err", "请选择一个平台视频");
      return;
    }
    fd.append("source_run_id", cmpState.platformRunId);
    videoLabel = cmpState.platformRunId;
  }

  cmpState.running = true;
  $("btnCompare").disabled = true;
  $("cmpResult").hidden = true;
  setCmpStatus("busy", "对照分析中… 请勿关闭页面");
  startProgress(runAgent, runClassic);
  try {
    const res = await apiFetch("/api/lab/compare", { method: "POST", body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || res.statusText || "compare failed");
    }
    finishProgress(true);
    setCmpStatus("ok", `完成 · ${data.compare_id} · ${videoLabel}`);
    renderCompareResult(data);
    refreshHistoryDropdown();
  } catch (e) {
    finishProgress(false);
    setCmpStatus("err", `失败: ${e.message || e}`);
  } finally {
    cmpState.running = false;
    validateCmpReady();
  }
});

validateCmpReady();

pollTimer = setInterval(poll, 400);
showList();
