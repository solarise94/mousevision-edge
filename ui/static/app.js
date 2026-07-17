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
  }
}

function fmtDelta(d) {
  if (d == null || Number.isNaN(Number(d))) return "—";
  return Number(d).toFixed(2);
}

function renderCompareResult(data) {
  const sum = data.summary || {};
  const el = $("cmpSummary");
  el.hidden = false;
  el.innerHTML = "";
  const stats = [
    ["Agent 只数", sum.n_agent ?? "—"],
    ["经典 只数", sum.n_classic ?? "—"],
    ["可对比", sum.n_comparable ?? "—"],
    ["|Δ|≤0.1", sum.match_0_1 != null ? `${sum.match_0_1}/${sum.n_comparable || 0}` : "—"],
    ["|Δ|≤0.5", sum.match_0_5 != null ? `${sum.match_0_5}/${sum.n_comparable || 0}` : "—"],
    ["mean|Δ|", sum.mean_delta != null ? Number(sum.mean_delta).toFixed(3) : "—"],
  ];
  stats.forEach(([k, v]) => {
    const card = document.createElement("div");
    card.className = "compare-stat";
    card.innerHTML = `<div class="k">${k}</div><div class="v">${v}</div>`;
    el.appendChild(card);
  });

  const agentRecs = ((data.branches || {}).agent || {}).records || [];
  const classicRecs = ((data.branches || {}).classic || {}).records || [];
  const agentNotes = Object.fromEntries(
    agentRecs.map((r) => [r.ordinal, r.agent_note || r.review_reason || ""])
  );
  const classicNotes = Object.fromEntries(
    classicRecs.map((r) => [
      r.ordinal,
      r.review_reason || (r.needs_review ? "needs_review" : "") || "",
    ])
  );

  const tbody = $("cmpTable").querySelector("tbody");
  tbody.innerHTML = "";
  const rows = data.alignment || [];
  // If only one branch, still show its weights
  if (!rows.length) {
    const only =
      agentRecs.length
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
    only.forEach((r) => rows.push(r));
  }
  rows.forEach((r) => {
    const tr = document.createElement("tr");
    const d = r.delta;
    if (d != null && d <= 0.1) tr.className = "match-good";
    else if (d != null && d <= 0.5) tr.className = "match-mid";
    else if (d != null) tr.className = "match-bad";
    const ord = r.ordinal;
    tr.innerHTML = `
      <td>${ord ?? ""}</td>
      <td>${r.agent_weight != null ? Number(r.agent_weight).toFixed(2) : "—"}</td>
      <td>${r.classic_weight != null ? Number(r.classic_weight).toFixed(2) : "—"}</td>
      <td>${fmtDelta(d)}</td>
      <td class="note">${agentNotes[ord] || ""}</td>
      <td class="note">${classicNotes[ord] || ""}</td>`;
    tbody.appendChild(tr);
  });
  $("cmpTableWrap").hidden = !rows.length;

  const br = $("cmpBranches");
  br.hidden = false;
  br.innerHTML = "";
  ["agent", "classic"].forEach((key) => {
    const b = (data.branches || {})[key];
    if (!b) return;
    const card = document.createElement("div");
    card.className = "compare-branch";
    const w = (b.weights || [])
      .map((x) => (x == null ? "null" : Number(x).toFixed(2)))
      .join(", ");
    card.innerHTML = `
      <h3>${key === "agent" ? "Agent" : "经典 " + (b.reader || "")}</h3>
      <div>耗时 <b>${b.elapsed_s ?? "—"}s</b> · n=<b>${b.n ?? 0}</b></div>
      <div>error: ${b.error || "—"}</div>
      <div>run: <code>${b.run_dir || "—"}</code></div>
      <div>weights: ${w || "—"}</div>`;
    br.appendChild(card);
  });
}

$("tabBatch")?.addEventListener("click", () => showLabView("batch"));
$("tabCompare")?.addEventListener("click", () => showLabView("compare"));

$("btnCompare")?.addEventListener("click", async () => {
  const file = $("cmpVideo").files?.[0];
  if (!file) {
    $("cmpStatus").textContent = "请先选择视频文件";
    $("cmpStatus").className = "compare-status err";
    return;
  }
  const fd = new FormData();
  fd.append("video", file);
  fd.append("cage_id", $("cmpCage").value || "compare");
  fd.append("classic_reader", $("cmpClassic").value || "http_ocr");
  fd.append("run_agent", $("cmpRunAgent").checked ? "true" : "false");
  fd.append("run_classic", $("cmpRunClassic").checked ? "true" : "false");

  $("btnCompare").disabled = true;
  $("cmpStatus").className = "compare-status busy";
  $("cmpStatus").textContent = "对照分析中… agent 原片可能需要 1–3 分钟，请勿关闭页面";
  $("cmpSummary").hidden = true;
  $("cmpTableWrap").hidden = true;
  $("cmpBranches").hidden = true;
  try {
    const res = await apiFetch("/api/lab/compare", { method: "POST", body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || res.statusText || "compare failed");
    }
    $("cmpStatus").className = "compare-status ok";
    $("cmpStatus").textContent = `完成 · compare_id=${data.compare_id} · ${file.name}`;
    renderCompareResult(data);
  } catch (e) {
    $("cmpStatus").className = "compare-status err";
    $("cmpStatus").textContent = `失败: ${e.message || e}`;
  } finally {
    $("btnCompare").disabled = false;
  }
});

pollTimer = setInterval(poll, 400);
showList();
