/* MouseVision Edge — PC admin SPA */
(function () {
  const $app = document.getElementById("app");

  const state = {
    user: null,
    route: location.pathname.replace(/^\/pc\/?/, "") || "data",
    records: [],
    stats: {},
    selectedId: null,
    selected: null,
    tab: "all",
    page: 1,
    pageSize: 20,
    total: 0,
    filters: { strain: "", cage_id: "", q: "", date_from: "", date_to: "" },
    ovFilters: { strain: "", cage_id: "", date_from: "", date_to: "", status: "" },
    boxes: [],
    overview: null,
    logs: [],
    settings: {},
    pendingBadge: 0,
    miceGroups: [],
    expandedCages: {},
  };

  const ROUTES = [
    { id: "data", label: "数据管理", section: "数据管理" },
    { id: "overview", label: "数据总览", section: "数据管理" },
    { id: "verify", label: "快捷核对", section: "数据管理", badge: () => state.pendingBadge },
    { id: "publish", label: "发布管理", section: "数据管理" },
    { id: "export", label: "导出管理", section: "数据管理" },
    { id: "boxes", label: "箱子管理", section: "基础信息" },
    { id: "mice", label: "小鼠管理", section: "基础信息" },
    { id: "users", label: "用户管理", section: "系统管理", roles: ["admin"] },
    { id: "logs", label: "操作日志", section: "系统管理", roles: ["admin", "operator"] },
    { id: "settings", label: "系统设置", section: "系统管理", roles: ["admin"] },
  ];

  const TITLES = Object.fromEntries(ROUTES.map((r) => [r.id, r.label]));

  // publish route pins the published tab on the data grid.
  // verify has its own quick-verify view (cage-grouped), not the data grid.
  // Applied BEFORE loadRecords() fetches to avoid showing the previous tab.
  const ROUTE_TAB = { publish: "published" };

  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    if (props) {
      Object.entries(props).forEach(([k, v]) => {
        if (k === "class") el.className = v;
        else if (k === "html") el.innerHTML = v;
        else if (k.startsWith("on") && typeof v === "function")
          el.addEventListener(k.slice(2).toLowerCase(), v);
        else if (v !== undefined && v !== null) el.setAttribute(k, v);
      });
    }
    children.flat().forEach((c) => {
      if (c == null) return;
      el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return el;
  }

  async function api(url, options = {}) {
    const res = await apiFetch(url, options);
    if (res.status === 401 && !url.includes("/api/login") && !url.includes("/api/me/password")) {
      state.user = null;
      navigate("login");
      throw new Error("unauthorized");
    }
    if (res.status === 403) {
      let detail = "权限不足";
      try {
        const j = await res.clone().json();
        detail = j.detail || detail;
      } catch (_) {}
      if (detail.includes("修改密码") || res.headers.get("X-Must-Change-Password") === "1") {
        state.route = "change-password";
        render();
        throw new Error(detail);
      }
      throw new Error(detail);
    }
    if (!res.ok) {
      let msg = res.statusText;
      try {
        const j = await res.json();
        msg = j.detail || msg;
      } catch (_) {}
      throw new Error(msg);
    }
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return res.json();
    return res;
  }

  function canWrite() {
    return state.user && ["admin", "operator"].includes(state.user.role);
  }

  function fmtWeight(w) {
    if (w == null) return "-- g";
    return `${Number(w).toFixed(2)} g`;
  }

  function statusLabel(s) {
    return { pending: "待核对", published: "已发布", deleted: "已删除" }[s] || s;
  }

  function navigate(route, replace) {
    state.route = route;
    const path = route === "data" ? "/pc" : `/pc/${route}`;
    if (replace) history.replaceState(null, "", path);
    else history.pushState(null, "", path);
    render();
    loadRoute();
  }

  async function bootstrap() {
    try {
      const me = await api("/api/me");
      state.user = me.authenticated ? me.user : null;
    } catch (_) {
      state.user = null;
    }
    if (!state.user && state.route !== "login") {
      navigate("login", true);
      return;
    }
    if (state.user?.must_change_password) {
      state.route = "change-password";
      render();
      return;
    }
    await loadRoute();
    render();
  }

  async function loadVerifyCages() {
    const p = new URLSearchParams();
    ["cage_id", "strain", "date_from", "date_to"].forEach((k) => {
      if (state.filters[k]) p.set(k, state.filters[k]);
    });
    state.verifyCages = await api(`/api/verify-cages?${p}`);
    state.pendingBadge = state.verifyCages.total_records || 0;
  }

  async function loadRoute() {
    if (!state.user) return;
    try {
      if (state.route === "verify") {
        await loadVerifyCages();
      } else if (["data", "publish"].includes(state.route)) {
        // Pin tab from route before fetching; "data" keeps whatever tab the
        // user last selected. Reset to page 1 so filters don't strand the view.
        if (state.route in ROUTE_TAB) {
          state.tab = ROUTE_TAB[state.route];
          state.page = 1;
        }
        await loadRecords();
        const r = await api("/api/mice-admin");
        state.miceGroups = r.items || [];
      }
      if (state.route === "overview") await loadOverview();
      if (state.route === "boxes") {
        const r = await api("/api/boxes?limit=200");
        state.boxes = r.items || [];
      }
      if (state.route === "mice") {
        const r = await api("/api/mice-admin");
        state.miceGroups = r.items || [];
      }
      if (state.route === "logs") {
        const r = await api("/api/logs?limit=100");
        state.logs = r.items || [];
      }
      if (state.route === "settings") state.settings = await api("/api/settings");
      if (state.route === "users") {
        const r = await api("/api/users");
        state.users = r.items || [];
      }
      if (state.pendingBadge === undefined || state.pendingBadge === 0) {
        state.pendingBadge = state.stats.pending_count || 0;
      }
      render();
    } catch (e) {
      console.error(e);
    }
  }

  async function loadRecords() {
    const p = new URLSearchParams();
    p.set("tab", state.tab);
    p.set("page", String(state.page));
    p.set("page_size", String(state.pageSize));
    Object.entries(state.filters).forEach(([k, v]) => {
      if (v) p.set(k, v);
    });
    const data = await api(`/api/records?${p}`);
    state.records = data.items || [];
    state.stats = data.stats || {};
    state.total = data.total || 0;
    state.pendingBadge = state.stats.pending_count || 0;
    if (state.selectedId) {
      state.selected = state.records.find((r) => r.record_id === state.selectedId) || null;
      if (!state.selected) {
        try {
          state.selected = await api(`/api/records/${state.selectedId}`);
        } catch (_) {
          state.selectedId = null;
          state.selected = null;
        }
      }
    }
  }

  // Daily counts bar chart with real date axis (gaps filled as 0 by backend).
  function drawDailyChart(canvas, points) {
    if (!canvas || !points?.length) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width = canvas.clientWidth * 2;
    const H = canvas.height = canvas.clientHeight * 2;
    ctx.scale(2, 2);
    const cw = W / 2, ch = H / 2;
    ctx.clearRect(0, 0, cw, ch);
    const padL = 28, padR = 12, padT = 10, padB = 32;
    const plotW = cw - padL - padR;
    const plotH = ch - padT - padB;
    const vals = points.map((p) => p.count ?? 0);
    const max = Math.max(...vals, 1);
    const barSlot = plotW / vals.length;
    const barW = Math.max(3, barSlot - 3);

    // Y-axis gridlines + labels
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.fillStyle = "#9aa3af";
    ctx.font = "10px sans-serif";
    ctx.textAlign = "right";
    const ySteps = Math.min(max, 4);
    for (let s = 0; s <= ySteps; s++) {
      const v = (max / ySteps) * s;
      const y = padT + plotH - (v / max) * plotH;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      if (s > 0) ctx.fillText(String(Math.round(v)), padL - 4, y + 3);
    }

    // Bars
    vals.forEach((v, i) => {
      const bh = (v / max) * plotH;
      const bx = padL + i * barSlot + 1.5;
      ctx.fillStyle = v > 0 ? "#3ddc84" : "rgba(61,220,132,0.15)";
      ctx.fillRect(bx, padT + plotH - bh, barW, bh);
    });

    // X-axis date labels — show first, middle, last to avoid crowding
    ctx.textAlign = "center";
    const labelIdx = points.length <= 1
      ? [0]
      : [0, Math.floor(points.length / 2), points.length - 1];
    [...new Set(labelIdx)].forEach((i) => {
      const p = points[i];
      if (!p || !p.date) return;
      const x = padL + i * barSlot + barSlot / 2;
      // MM-DD format
      const label = p.date.length >= 10 ? p.date.slice(5) : p.date;
      ctx.fillText(label, x, ch - 12);
    });
  }

  // Weight distribution: histogram bars (width from bin edges) + optional
  // normal fit curve (only when backend says show_fit, i.e. n>=30).
  function drawDistChart(canvas, ws) {
    if (!canvas || !ws || ws.n === 0) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width = canvas.clientWidth * 2;
    const H = canvas.height = canvas.clientHeight * 2;
    ctx.scale(2, 2);
    const cw = W / 2, ch = H / 2;
    ctx.clearRect(0, 0, cw, ch);
    const padL = 36, padR = 14, padT = 12, padB = 30;
    const plotW = cw - padL - padR;
    const plotH = ch - padT - padB;

    const bins = ws.hist_bins;
    const counts = ws.hist_counts;
    const fitX = ws.fit_x || [];
    const fitY = ws.fit_y || [];
    // x-axis domain = union of histogram bins and fit curve range, so the
    // normal curve never draws outside the plot area.
    const xLo = Math.min(bins[0], ...(fitX.length ? [fitX[0]] : []));
    const xHi = Math.max(bins[bins.length - 1], ...(fitX.length ? [fitX[fitX.length - 1]] : []));
    const xRange = xHi - xLo || 1;
    const yMax = Math.max(...counts, ...(fitY.length ? [Math.max(...fitY)] : []), 1);

    const xPx = (v) => padL + ((v - xLo) / xRange) * plotW;
    const yPx = (v) => padT + plotH - (v / yMax) * plotH;

    // Y-axis gridlines
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.fillStyle = "#9aa3af";
    ctx.font = "10px sans-serif";
    ctx.textAlign = "right";
    const ySteps = Math.min(Math.ceil(yMax), 4);
    for (let s = 0; s <= ySteps; s++) {
      const v = (yMax / ySteps) * s;
      const y = yPx(v);
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      if (s > 0) ctx.fillText(String(Math.round(v)), padL - 4, y + 3);
    }

    // Histogram bars — width from consecutive bin edges (fixes P1 overlap)
    ctx.fillStyle = "rgba(61,220,132,0.4)";
    counts.forEach((c, i) => {
      const bx0 = xPx(bins[i]);
      const bx1 = xPx(bins[i + 1]);
      const bw = Math.max(1, bx1 - bx0 - 1);
      const bh = (c / yMax) * plotH;
      ctx.fillRect(bx0, padT + plotH - bh, bw, bh);
    });

    // Normal fit curve — only when show_fit (n >= 30)
    if (ws.show_fit && fitX.length > 1) {
      ctx.strokeStyle = "#f2f4f7";
      ctx.lineWidth = 2;
      ctx.beginPath();
      fitX.forEach((x, i) => {
        const px = xPx(x), py = yPx(fitY[i]);
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      });
      ctx.stroke();
    }

    // Mean line (amber)
    if (ws.mean != null) {
      ctx.strokeStyle = "rgba(245,166,35,0.7)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(xPx(ws.mean), padT);
      ctx.lineTo(xPx(ws.mean), padT + plotH);
      ctx.stroke();
    }

    // X-axis labels
    ctx.fillStyle = "#9aa3af";
    ctx.textAlign = "center";
    const xLabelIdx = bins.length <= 1 ? [0] : [0, Math.floor(bins.length / 2), bins.length - 1];
    xLabelIdx.forEach((i) => {
      ctx.fillText(`${bins[i].toFixed(1)}`, xPx(bins[i]), ch - 10);
    });
  }

  // Per-cage strip plot: each mouse = a dot, grouped by cage column, with
  // cage median line. Outliers (cage median ±2g) drawn red.
  function drawStripPlot(canvas, cw_data) {
    if (!canvas || !cw_data || !cw_data.cages?.length) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width = canvas.clientWidth * 2;
    const H = canvas.height = canvas.clientHeight * 2;
    ctx.scale(2, 2);
    const cw = W / 2, ch = H / 2;
    ctx.clearRect(0, 0, cw, ch);
    const padL = 36, padR = 14, padT = 14, padB = 34;
    const plotW = cw - padL - padR;
    const plotH = ch - padT - padB;
    const cages = cw_data.cages;

    // Y-axis domain: union of all weights with padding
    const allW = cages.flatMap((c) => c.points.map((p) => p.weight));
    if (!allW.length) return;
    const yLo = Math.floor(Math.min(...allW) - 0.5);
    const yHi = Math.ceil(Math.max(...allW) + 0.5);
    const yRange = yHi - yLo || 1;
    const yPx = (v) => padT + plotH - ((v - yLo) / yRange) * plotH;

    // Y-axis gridlines + labels
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.fillStyle = "#9aa3af";
    ctx.font = "10px sans-serif";
    ctx.textAlign = "right";
    const ySteps = Math.min(yRange, 5);
    for (let s = 0; s <= ySteps; s++) {
      const v = yLo + (yRange / ySteps) * s;
      const y = yPx(v);
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      ctx.fillText(v.toFixed(1), padL - 4, y + 3);
    }

    // Each cage = a vertical column
    const colW = plotW / cages.length;
    cages.forEach((cage, ci) => {
      const colCx = padL + colW * (ci + 0.5);
      // Median line (amber, spans column width)
      if (cage.median != null) {
        ctx.strokeStyle = "rgba(245,166,35,0.8)";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(colCx - colW * 0.3, yPx(cage.median));
        ctx.lineTo(colCx + colW * 0.3, yPx(cage.median));
        ctx.stroke();
      }
      // Dots — jitter horizontally within column
      const dotR = 4;
      const spread = colW * 0.28;
      cage.points.forEach((p, pi) => {
        // Deterministic jitter based on index
        const jitter = ((pi % 3) - 1) * (spread / 2.5);
        const px = colCx + jitter;
        const py = yPx(p.weight);
        ctx.beginPath();
        ctx.arc(px, py, dotR, 0, Math.PI * 2);
        ctx.fillStyle = p.outlier ? "#ff4d4f" : "#3ddc84";
        ctx.fill();
      });
      // Cage label below
      ctx.fillStyle = "#9aa3af";
      ctx.font = "10px sans-serif";
      ctx.textAlign = "center";
      const label = cage.cage_id.length > 8 ? cage.cage_id.slice(0, 7) + "…" : cage.cage_id;
      ctx.fillText(label, colCx, ch - 16);
      ctx.fillText(`n=${cage.n}`, colCx, ch - 4);
    });
  }

  function shell(nodes) {
    const sections = {};
    ROUTES.forEach((r) => {
      if (r.roles && state.user && !r.roles.includes(state.user.role) && state.user.role !== "admin")
        return;
      sections[r.section] = sections[r.section] || [];
      sections[r.section].push(r);
    });
    const nav = h("nav", { class: "nav" });
    Object.entries(sections).forEach(([sec, items]) => {
      nav.appendChild(h("div", { class: "nav-section" }, sec));
      items.forEach((r) => {
        const badge = r.badge ? r.badge() : 0;
        const btn = h(
          "button",
          {
            class: state.route === r.id ? "active" : "",
            onClick: () => navigate(r.id),
          },
          r.label,
          badge > 0 ? h("span", { class: "badge" }, String(badge)) : null
        );
        nav.appendChild(btn);
      });
    });
    return h(
      "div",
      { class: "shell" },
      h(
        "aside",
        { class: "sidebar" },
        h(
          "div",
          { class: "sidebar-brand" },
          h("span", { class: "logo" }, "MV"),
          h("div", null, h("div", { class: "name" }, "MouseVision Edge"), h("div", { class: "sub" }, "数据管理系统"))
        ),
        nav,
        h(
          "div",
          { class: "sidebar-foot" },
          h("strong", null, state.user?.display_name || state.user?.username || ""),
          state.user?.role || "",
          h("br"),
          h("button", {
            class: "btn ghost",
            style: "padding:4px 0;font-size:12px",
            onClick: () => { state.route = "change-password"; render(); },
          }, "修改密码"),
          h("br"),
          h("a", { href: "/mobile", style: "color:var(--mv-green)" }, "手机录制"),
          " · ",
          h("a", { href: "/", style: "color:var(--mv-green)" }, "入口")
        )
      ),
      h(
        "div",
        { class: "main" },
        h(
          "header",
          { class: "topbar" },
          h("h1", null, TITLES[state.route] || "管理"),
          h(
            "div",
            { class: "topbar-actions" },
            h("button", { class: "btn ghost", onClick: () => loadRoute() }, "刷新"),
            h("button", {
              class: "btn ghost",
              onClick: async () => {
                await api("/api/logout", { method: "POST" });
                state.user = null;
                navigate("login");
              },
            }, "退出")
          )
        ),
        h("div", { class: "content" }, ...nodes)
      )
    );
  }

  function kpiRow(stats) {
    // average_weight may be a number (raw) or a pre-formatted string (e.g.
    // overview passes "Mean ± SEM g"); only format if it's numeric.
    const avgDisplay =
      typeof stats.average_weight === "string"
        ? stats.average_weight
        : fmtWeight(stats.average_weight);
    const cards = [
      ["总记录数", stats.total_records, ""],
      ["待核对", stats.pending_count, "green"],
      ["已发布", stats.published_count, "green"],
      ["已删除", stats.deleted_count, ""],
      ["平均体重", avgDisplay, "green"],
    ];
    return h(
      "div",
      { class: "kpi-row" },
      ...cards.map(([label, val, cls]) =>
        h("div", { class: "kpi" }, h("div", { class: "label" }, label), h("div", { class: `value ${cls}` }, String(val ?? "--")))
      )
    );
  }

  function filtersBar(onApply) {
    const wrap = h("div", { class: "filters" });
    const fields = [
      ["date_from", "开始日期", "date"],
      ["date_to", "结束日期", "date"],
      ["strain", "品系", "text"],
      ["cage_id", "箱号", "text"],
      ["q", "搜索", "text"],
    ];
    fields.forEach(([key, label, type]) => {
      const inp = h("input", { type, value: state.filters[key] || "", placeholder: label });
      inp.addEventListener("input", (e) => (state.filters[key] = e.target.value));
      wrap.appendChild(h("label", { class: "field" }, h("span", null, label), inp));
    });
    wrap.appendChild(
      h("button", {
        class: "btn primary",
        onClick: () => { state.page = 1; onApply(); },
      }, "筛选")
    );
    wrap.appendChild(
      h("button", {
        class: "btn",
        onClick: () => {
          state.filters = { strain: "", cage_id: "", q: "", date_from: "", date_to: "" };
          state.page = 1;
          onApply();
        },
      }, "重置")
    );
    return wrap;
  }

  function recordGrid(onSelect) {
    if (!state.records.length)
      return h("div", { class: "empty" }, "暂无记录");
    return h(
      "div",
      { class: "record-grid" },
      ...state.records.map((rec) => {
        const needsReview = !!rec.needs_review;
        const needsManual = !!rec.requires_manual_weight;
        const card = h("button", {
          class: `record-card${state.selectedId === rec.record_id ? " selected" : ""}${needsReview || needsManual ? " needs-review" : ""}`,
          onClick: () => onSelect(rec),
        });
        const thumb = h("div", { class: "thumb" });
        thumb.appendChild(h("img", { src: rec.photo_url + "?size=thumb", alt: "" }));
        thumb.appendChild(h("span", { class: "idx" }, `#${String(rec.ordinal).padStart(2, "0")}`));
        thumb.appendChild(h("span", { class: `status-badge status-${rec.status}` }, statusLabel(rec.status)));
        if (needsManual) {
          thumb.appendChild(h("span", { class: "review-badge" }, "无稳定帧"));
        } else if (needsReview) {
          thumb.appendChild(h("span", { class: "review-badge" }, "待复核"));
        }
        const weightLabel = needsManual
          ? `猜测 ${fmtWeight(rec.guessed_weight != null ? rec.guessed_weight : rec.weight)}`
          : fmtWeight(rec.weight);
        const metaBits = [rec.cage_id];
        if (needsManual) metaBits.push("请手填");
        else if (needsReview) metaBits.push("待复核");
        if (rec.timestamp) metaBits.push(rec.timestamp);
        card.appendChild(thumb);
        card.appendChild(
          h("div", { class: "body" },
            h("div", { class: "weight" }, weightLabel),
            h("div", { class: "meta" }, metaBits.join(" · "))
          )
        );
        return card;
      })
    );
  }

  function detailPanel(rec) {
    if (!rec) return h("div", { class: "detail empty" }, "选择一条记录查看详情");
    const photo = rec.status === "deleted"
      ? `${rec.photo_url}?include_deleted=true&size=full`
      : `${rec.photo_url}?size=full`;
    const needsManual = !!rec.requires_manual_weight;
    const dl = h("dl");
    const weightRow = needsManual
      ? `猜测 ${fmtWeight(rec.guessed_weight != null ? rec.guessed_weight : rec.weight)}（待手填）`
      : fmtWeight(rec.weight);
    const rows = [
      ["记录 ID", rec.record_id],
      ["箱号", rec.cage_id],
      ["品系", rec.strain || "-"],
      ["小鼠编号", rec.ordinal != null ? String(rec.ordinal).padStart(2, "0") : "-"],
      ["体重", weightRow],
      ["置信度", rec.confidence != null ? Number(rec.confidence).toFixed(2) : "-"],
      [
        "复核",
        needsManual
          ? `无稳定帧 (${rec.review_reason || "no_stable_platform"})`
          : rec.needs_review
            ? `待复核 (${rec.review_reason || "-"})`
            : "否",
      ],
      ["称重时间", rec.timestamp || "-"],
      ["录制时长", rec.duration_sec != null ? `${rec.duration_sec}s` : "-"],
      ["状态", statusLabel(rec.status)],
    ];
    rows.forEach(([k, v]) => dl.appendChild(h("div", null, h("dt", null, k), h("dd", null, v))));

    const mediaKids = [h("img", { src: photo, alt: "" })];
    if (rec.clip_url) {
      mediaKids.push(
        h("video", {
          class: "session-clip",
          src: rec.clip_url,
          controls: true,
          playsInline: true,
        })
      );
    }

    // P2-b: correction input for ALL records (universal endpoint).
    let manualBlock = null;
    if (canWrite()) {
      const input = h("input", {
        type: "number",
        step: "0.01",
        min: "0.1",
        max: "79.9",
        class: "manual-weight-input",
        placeholder: "修正体重 (g)",
        value: rec.weight != null ? String(rec.weight) : (rec.guessed_weight != null ? String(rec.guessed_weight) : ""),
      });
      const confirmBtn = h(
        "button",
        {
          class: "btn primary",
          onClick: async () => {
            const v = Number(input.value);
            if (!(v > 0) || !(v < 80)) {
              alert("请输入有效体重 (0–80 g)");
              return;
            }
            try {
              await api(`/api/records/${rec.record_id}/confirm-weight`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ weight: v }),
              });
              await loadRecords();
              try {
                state.selected = await api(`/api/records/${rec.record_id}`);
                state.selectedId = rec.record_id;
              } catch (_) {}
              render();
            } catch (err) {
              alert(err.message || String(err));
            }
          },
        },
        needsManual ? "确认体重" : "修正体重",
      );
      const hint = needsManual
        ? "无稳定帧：算法未找到可靠平台。请回看片段后手填实际体重；未确认前不可核对/发布。"
        : "如算法体重与实际不符，可在此修正（原始值保留用于训练飞轮）。";
      manualBlock = h("div", { class: "manual-weight-panel" }, [
        h("p", { class: "manual-weight-hint" }, hint),
        h("div", { class: "manual-weight-row" }, input, confirmBtn),
      ]);
    }

    // P2-b: detection label buttons for training flywheel.
    let detectionBlock = null;
    if (canWrite()) {
      const labels = ["mouse", "glove", "empty", "other"];
      const currentLabel = rec.detection_label || "";
      const labelBtns = labels.map((lab) => {
        const props = {
          class: `btn ${lab === currentLabel ? "primary" : ""}`.trim(),
          title: `标记为 ${lab}`,
          onClick: async () => {
            try {
              await api(`/api/records/${rec.record_id}/detection-label`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ label: lab }),
              });
              await loadRecords();
              try {
                state.selected = await api(`/api/records/${rec.record_id}`);
                state.selectedId = rec.record_id;
              } catch (_) {}
              render();
            } catch (err) {
              alert(err.message || String(err));
            }
          },
        };
        return h("button", props, lab);
      });
      detectionBlock = h("div", { class: "detection-label-panel" }, [
        h("span", { class: "muted", style: "font-size:11px;margin-right:6px" }, "检测标注:"),
        ...labelBtns,
      ]);
    }

    const actions = h("div", { class: "actions" });
    if (canWrite()) {
      const btns = [
        ["核对通过", "primary", () => act(rec.record_id, "verify"), needsManual && rec.weight == null],
        ["发布", "primary", () => act(rec.record_id, "publish"), needsManual && rec.weight == null],
        ["撤回发布", "", () => act(rec.record_id, "unpublish"), false],
        ["删除", "danger", () => act(rec.record_id, "delete"), false],
        ["恢复", "", () => act(rec.record_id, "restore"), false],
        ["回放复核", "", () => reviewPlayback(rec), false],
      ];
      btns.forEach(([label, cls, fn, disabled]) => {
        const props = {
          class: `btn ${cls}`.trim(),
          title: disabled ? "请先手填确认体重" : "",
          onClick: fn,
        };
        if (disabled) props.disabled = "disabled";
        actions.appendChild(h("button", props, label));
      });
    }

    const helpText = needsManual
      ? "此会话无稳定平台读数。体重以实验员手填为准；下方片段便于核对 LCD。"
      : "体重为稳定称重曲线算法计算;照片用于确认小鼠在秤状态,数字可能略有差异。如需核验完整称重过程,请点击「回放复核」。";

    return h(
      "div",
      { class: "detail" },
      h("h3", null, "记录详情"),
      h("div", { class: "media" }, ...mediaKids),
      dl,
      manualBlock,
      detectionBlock,
      h("p", { class: "muted", style: "font-size:11px;line-height:1.5" }, helpText),
      rec.notes ? h("p", { class: "muted", style: "font-size:12px" }, `备注: ${rec.notes}`) : null,
      actions
    );
  }

  async function act(recordId, action) {
    const map = {
      verify: "verify",
      publish: "publish",
      unpublish: "unpublish",
      delete: "delete",
      restore: "restore",
    };
    const ep = map[action];
    if (!ep) return;
    const method = action === "delete" ? "DELETE" : "POST";
    const url =
      action === "delete"
        ? `/api/records/${recordId}`
        : `/api/records/${recordId}/${ep}`;
    try {
      await api(url, { method });
      await loadRecords();
      render();
    } catch (err) {
      alert(err.message || String(err));
    }
  }

  async function reviewPlayback(rec) {
    if (!rec.run_id || rec.ordinal == null) {
      alert("该记录缺少回放参数");
      return;
    }
    await api("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cage_id: rec.cage_id,
        run_id: rec.run_id,
        ordinal: rec.ordinal,
        persist: false,
        speed: 1,
      }),
    });
    window.open("/legacy", "_blank");
  }

  function viewData() {
    const tabs = [
      ["all", "全部"],
      ["pending", "待核对"],
      ["published", "已发布"],
      ["deleted", "已删除"],
    ];
    const tabBar = h("div", { class: "tabs" });
    tabs.forEach(([id, label]) =>
      tabBar.appendChild(
        h("button", {
          class: `tab${state.tab === id ? " active" : ""}`,
          onClick: () => { state.tab = id; state.page = 1; loadRecords().then(render); },
        }, label)
      )
    );
    async function select(rec) {
      state.selectedId = rec.record_id;
      state.selected = rec;
      try {
        const qs = rec.status === "deleted" ? "?include_deleted=true" : "";
        state.selected = await api(`/api/records/${rec.record_id}${qs}`);
      } catch (_) {}
      render();
    }

    // Two-level cage → mice list. Filter miceGroups by current tab.
    const groups = (state.miceGroups || []).map((g) => {
      const recs = g.records.filter((r) => {
        if (state.tab === "all") return true;
        return r.status === state.tab;
      });
      const weights = recs.map((r) => r.weight).filter((w) => w != null);
      const mean = weights.length ? (weights.reduce((a, b) => a + b, 0) / weights.length) : null;
      const pendingN = g.records.filter((r) => r.status === "pending").length;
      return { ...g, filtered: recs, mean_weight: mean, pending_n: pendingN };
    }).filter((g) => g.filtered.length > 0);

    function toggleCage(cageId) {
      state.expandedCages[cageId] = !state.expandedCages[cageId];
      render();
    }

    const cageList = h("div", { class: "cage-row-list" });
    if (!groups.length) {
      cageList.appendChild(h("div", { class: "empty" }, "暂无记录"));
    }
    groups.forEach((g) => {
      const expanded = !!state.expandedCages[g.cage_id];
      const row = h("div", { class: `cage-row${expanded ? " expanded" : ""}` });
      const head = h("div", { class: "cage-row-head", onClick: () => toggleCage(g.cage_id) },
        h("span", { class: "cage-row-caret" }, expanded ? "▾" : "▸"),
        h("strong", null, g.cage_id),
        h("span", { class: "muted" }, g.strain),
        h("span", { class: "muted" }, `${g.filtered.length} 只`),
        g.mean_weight != null ? h("span", { class: "muted" }, `均 ${fmtWeight(g.mean_weight)}`) : null,
        g.pending_n > 0 ? h("span", { class: "cage-row-badge" }, `${g.pending_n} 待核对`) : null
      );
      row.appendChild(head);
      if (expanded) {
        const thumbs = h("div", { class: "cage-thumbs" });
        g.filtered.forEach((rec) => {
          const thumb = h("div", {
            class: `cage-thumb${state.selectedId === rec.record_id ? " selected" : ""}`,
            onClick: () => select(rec),
          });
          thumb.appendChild(h("img", { src: rec.photo_url + "?size=thumb", alt: "" }));
          thumb.appendChild(h("span", { class: "cage-thumb-idx" }, `#${String(rec.ordinal).padStart(2, "0")}`));
          const wspan = h("span", { class: "cage-thumb-w" }, fmtWeight(rec.weight));
          thumb.appendChild(wspan);
          thumbs.appendChild(thumb);
        });
        row.appendChild(h("div", { class: "cage-row-body" }, thumbs));
      }
      cageList.appendChild(row);
    });

    return [
      filtersBar(() => loadRecords().then(render)),
      kpiRow(state.stats),
      tabBar,
      h("div", { class: "data-layout" },
        cageList,
        detailPanel(state.selected)
      ),
    ];
  }

  async function loadOverview() {
    const p = new URLSearchParams();
    ["strain", "cage_id", "date_from", "date_to", "status"].forEach((k) => {
      if (state.ovFilters[k]) p.set(k, state.ovFilters[k]);
    });
    state.overview = await api(`/api/overview?${p}`);
    render();
  }

  function viewOverview() {
    const o = state.overview || {};
    const ws = o.weight_stats || { n: 0 };
    const cw = o.cage_weights || { cages: [], total_n: 0, total_outliers: 0 };
    const n = (o.filters && o.filters.n != null) ? o.filters.n : ws.n;

    const dailyCanvas = h("canvas");
    const stripCanvas = h("canvas");
    const distCanvas = h("canvas");
    setTimeout(() => {
      drawDailyChart(dailyCanvas, o.daily_counts || []);
      drawStripPlot(stripCanvas, cw);
      drawDistChart(distCanvas, ws);
    }, 50);

    // --- Filter bar (cohort selection for QC) ---
    const fbar = h("div", { class: "filters" });
    const ff = [
      ["date_from", "开始日期", "date"],
      ["date_to", "结束日期", "date"],
      ["strain", "品系", "text"],
      ["cage_id", "箱号", "text"],
    ];
    ff.forEach(([key, label, type]) => {
      const inp = h("input", { type, value: state.ovFilters[key] || "", placeholder: label });
      inp.addEventListener("input", (e) => (state.ovFilters[key] = e.target.value));
      fbar.appendChild(h("label", { class: "field" }, h("span", null, label), inp));
    });
    const statusSel = h("select", null,
      h("option", { value: "" }, "全部状态"),
      h("option", { value: "pending" }, "待核对"),
      h("option", { value: "published" }, "已发布"),
    );
    statusSel.value = state.ovFilters.status || "";
    statusSel.addEventListener("change", (e) => (state.ovFilters.status = e.target.value));
    fbar.appendChild(h("label", { class: "field" }, h("span", null, "状态"), statusSel));
    fbar.appendChild(h("button", { class: "btn primary", onClick: () => loadOverview() }, "筛选"));
    fbar.appendChild(h("button", {
      class: "btn",
      onClick: () => {
        state.ovFilters = { strain: "", cage_id: "", date_from: "", date_to: "", status: "" };
        loadOverview();
      },
    }, "重置"));

    // --- KPI cards ---
    const meanSd = ws.mean != null ? `${ws.mean} ± ${ws.sd} g` : "--";
    const rangeStr = ws.min != null ? `${ws.min}–${ws.max} g` : "--";
    const outlierWarn = cw.total_outliers > 0;

    return [
      fbar,
      h("div", { class: "kpi-row" },
        h("div", { class: "kpi" },
          h("div", { class: "label" }, "总记录 (n)"),
          h("div", { class: "value" }, String(n)),
          h("div", { class: "delta" }, `待核对 ${o.pending_count ?? 0} · 已发布 ${o.published_count ?? 0}`)
        ),
        h("div", { class: "kpi" },
          h("div", { class: "label" }, "Mean ± SD"),
          h("div", { class: "value green" }, meanSd),
          h("div", { class: "delta" }, ws.median != null ? `中位数 ${ws.median} g` : "")
        ),
        h("div", { class: "kpi" },
          h("div", { class: "label" }, "体重范围"),
          h("div", { class: "value" }, rangeStr),
          ws.range != null ? h("div", { class: "delta" }, `Δ ${ws.range} g`) : null
        ),
        h("div", { class: "kpi" },
          h("div", { class: "label" }, "异常候选"),
          h("div", { class: `value ${outlierWarn ? "warn" : "green"}` }, String(cw.total_outliers)),
          h("div", { class: "delta" }, outlierWarn ? "箱内中位数 ±2g 外" : "无异常")
        )
      ),
      h("div", { class: "chart-box" },
        h("div", { class: "muted", style: "margin-bottom:8px" }, "每日记录数"),
        dailyCanvas
      ),
      h("div", { class: "chart-box" },
        h("div", { class: "chart-title" },
          h("span", { class: "muted" }, "按笼体重 (strip plot)"),
          h("span", { class: "chart-legend" },
            h("span", { class: "dot green" }), "正常",
            h("span", { class: "dot red" }), "异常",
            h("span", { class: "line amber" }), "笼内中位数"
          )
        ),
        stripCanvas
      ),
      h("div", { class: "chart-box" },
        h("div", { class: "chart-title" },
          h("span", { class: "muted" }, ws.show_fit ? "体重分布 (直方图 + 正态拟合)" : "体重分布 (直方图)"),
          ws.show_fit ? null : h("span", { class: "chart-note" }, ws.n < 30 ? `n=${ws.n} < 30, 拟合已隐藏` : "需筛选单一箱号才显示拟合")
        ),
        distCanvas
      ),
    ];
  }

  function viewQuickVerify() {
    const vc = state.verifyCages || { cages: [], total_cages: 0, total_records: 0, average_weight: null };
    const cages = vc.cages || [];
    const avg = vc.average_weight;
    const isOutlier = (w) =>
      w != null && avg != null && Math.abs(w - avg) > 2.0;

    // Slim filter bar: only cage_id, strain, date range (no free-text q).
    const fbar = h("div", { class: "filters" });
    const ff = [
      ["date_from", "开始日期", "date"],
      ["date_to", "结束日期", "date"],
      ["strain", "品系", "text"],
      ["cage_id", "箱号", "text"],
    ];
    ff.forEach(([key, label, type]) => {
      const inp = h("input", { type, value: state.filters[key] || "", placeholder: label });
      inp.addEventListener("input", (e) => (state.filters[key] = e.target.value));
      fbar.appendChild(h("label", { class: "field" }, h("span", null, label), inp));
    });
    fbar.appendChild(h("button", {
      class: "btn primary",
      onClick: () => { loadVerifyCages().then(render); },
    }, "筛选"));
    fbar.appendChild(h("button", {
      class: "btn",
      onClick: () => {
        ["cage_id", "strain", "date_from", "date_to"].forEach((k) => (state.filters[k] = ""));
        loadVerifyCages().then(render);
      },
    }, "重置"));

    const kpi = kpiRow({
      total_records: vc.total_cages,
      pending_count: vc.total_records,
      published_count: null,
      deleted_count: null,
      average_weight: avg,
    });
    // Override KPI labels for verify context.
    const kpiCards = kpi.querySelectorAll(".kpi");
    if (kpiCards[0]) kpiCards[0].querySelector(".label").textContent = "待核对笼数";
    if (kpiCards[1]) kpiCards[1].querySelector(".label").textContent = "待核对记录数";

    if (!cages.length) {
      return [fbar, kpi, h("div", { class: "empty" }, "没有待核对的记录")];
    }

    async function passCage(cage) {
      const ids = cage.records.map((r) => r.record_id).filter(Boolean);
      if (!ids.length) return;
      await api("/api/records/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ record_ids: ids, action: "publish" }),
      });
      await loadVerifyCages();
      render();
    }

    async function delOne(rec) {
      if (!confirm(`删除 ${rec.cage_id} 第 ${rec.ordinal} 只 (${fmtWeight(rec.weight)})？`)) return;
      await api(`/api/records/${rec.record_id}`, { method: "DELETE" });
      await loadVerifyCages();
      render();
    }

    const list = cages.map((cage) => {
      const weightsLine = cage.records.map((r) => {
        const cls = isOutlier(r.weight) ? "cage-weight warn" : "cage-weight";
        return h("span", { class: cls }, fmtWeight(r.weight).replace(" g", ""));
      });
      const thumbs = h("div", { class: "cage-thumbs" });
      cage.records.forEach((rec) => {
        const thumb = h("div", { class: "cage-thumb" });
        thumb.appendChild(h("img", { src: rec.photo_url + "?size=thumb", alt: "" }));
        thumb.appendChild(h("span", { class: "cage-thumb-idx" }, `#${String(rec.ordinal).padStart(2, "0")}`));
        const wspan = h("span", { class: isOutlier(rec.weight) ? "cage-thumb-w warn" : "cage-thumb-w" }, fmtWeight(rec.weight));
        thumb.appendChild(wspan);
        if (canWrite()) {
          const del = h("button", {
            class: "thumb-del",
            title: "删除该只",
            onClick: (e) => { e.stopPropagation(); delOne(rec); },
          }, "×");
          thumb.appendChild(del);
        }
        thumbs.appendChild(thumb);
      });
      const card = h("div", { class: "cage-card" },
        h("div", { class: "cage-card-head" },
          h("strong", null, cage.cage_id),
          h("span", { class: "muted" }, `${cage.strain} · ${cage.count} 只 · 均 ${fmtWeight(cage.mean_weight)}`)
        ),
        h("div", { class: "cage-weights" }, ...weightsLine),
        thumbs,
        canWrite()
          ? h("div", { class: "cage-actions" },
              h("button", {
                class: "btn primary",
                onClick: () => passCage(cage),
              }, `整笼通过 (${cage.count} 只)`)
            )
          : null
      );
      return card;
    });

    return [fbar, kpi, h("div", { class: "cage-list" }, ...list)];
  }

  function viewPublish() {
    return viewData();
  }

  function viewExport() {
    const buildUrl = (fmt) => {
      const p = new URLSearchParams({ format: fmt, tab: state.tab });
      Object.entries(state.filters).forEach(([k, v]) => { if (v) p.set(k, v); });
      return `/api/export?${p}`;
    };
    return [
      h("p", { class: "muted" }, "按当前筛选条件导出记录数据。"),
      filtersBar(() => Promise.resolve()),
      h("div", { style: "display:flex;gap:10px" },
        h("a", { class: "btn primary", href: buildUrl("csv") }, "导出 CSV"),
        h("a", { class: "btn", href: buildUrl("xlsx") }, "导出 XLSX")
      ),
    ];
  }

  function viewBoxes() {
    if (!state.boxes?.length) return [h("div", { class: "empty" }, "暂无箱子")];
    const table = h("table");
    table.appendChild(h("tr", null,
      ...["箱号", "品系", "记录数", "待处理", "备注", ""].map((t) => h("th", null, t))
    ));
    state.boxes.forEach((box) => {
      table.appendChild(h("tr", null,
        h("td", null, box.cage_id),
        h("td", null, box.strain),
        h("td", null, String(box.record_count ?? 0)),
        h("td", null, String(box.pending_count ?? 0)),
        h("td", null, box.notes || ""),
        h("td", null, h("a", { href: `/api/boxes/${box.cage_id}/qr.svg`, target: "_blank" }, "二维码"))
      ));
    });
    const form = h("div", { class: "filters", style: "margin-top:16px" });
    const cageIn = h("input", { placeholder: "新箱号" });
    const strainIn = h("input", { placeholder: "品系(可选)" });
    form.appendChild(h("label", { class: "field" }, h("span", null, "箱号"), cageIn));
    form.appendChild(h("label", { class: "field" }, h("span", null, "品系"), strainIn));
    if (canWrite()) {
      form.appendChild(h("button", {
        class: "btn primary",
        onClick: async () => {
          await api("/api/boxes", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ cage_id: cageIn.value, strain: strainIn.value || null }),
          });
          await loadRoute();
        },
      }, "新建箱子"));
    }
    return [h("div", { class: "table-wrap" }, table), form];
  }

  function viewMice() {
    const groups = state.miceGroups || [];
    if (!groups.length) return [h("div", { class: "empty" }, "暂无小鼠记录")];
    const table = h("table");
    table.appendChild(h("tr", null,
      ...["箱号", "品系", "小鼠数", "最新体重", "最近称重"].map((t) => h("th", null, t))
    ));
    groups.forEach((g) => {
      table.appendChild(h("tr", null,
        h("td", null, g.cage_id),
        h("td", null, g.strain),
        h("td", null, String(g.mouse_count)),
        h("td", null, fmtWeight(g.latest_weight)),
        h("td", null, g.latest_at || "-")
      ));
    });
    return [h("div", { class: "table-wrap" }, table)];
  }

  function viewUsers() {
    const users = state.users || [];
    const table = h("table");
    table.appendChild(h("tr", null,
      ...["用户名", "显示名", "角色", "状态", ""].map((t) => h("th", null, t))
    ));
    users.forEach((u) => {
      table.appendChild(h("tr", null,
        h("td", null, u.username),
        h("td", null, u.display_name),
        h("td", null, u.role),
        h("td", null, u.disabled ? "禁用" : "正常"),
        h("td", null,
          u.id !== state.user?.id
            ? h("button", {
                class: "btn danger",
                onClick: async () => {
                  await api(`/api/users/${u.id}`, { method: "DELETE" });
                  await loadRoute();
                },
              }, "删除")
            : null
        )
      ));
    });
    const un = h("input", { placeholder: "用户名" });
    const pw = h("input", { type: "password", placeholder: "密码" });
    const role = h("select", null,
      h("option", { value: "operator" }, "operator"),
      h("option", { value: "viewer" }, "viewer"),
      h("option", { value: "admin" }, "admin")
    );
    const addForm = h("div", { class: "filters" },
      h("label", { class: "field" }, h("span", null, "用户名"), un),
      h("label", { class: "field" }, h("span", null, "密码"), pw),
      h("label", { class: "field" }, h("span", null, "角色"), role),
      h("button", {
        class: "btn primary",
        onClick: async () => {
          await api("/api/users", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username: un.value, password: pw.value, role: role.value }),
          });
          await loadRoute();
        },
      }, "添加用户")
    );
    return [h("div", { class: "table-wrap" }, table), addForm];
  }

  function viewLogs() {
    const table = h("table");
    table.appendChild(h("tr", null,
      ...["时间", "操作者", "动作", "目标", "详情"].map((t) => h("th", null, t))
    ));
    (state.logs || []).forEach((log) => {
      table.appendChild(h("tr", null,
        h("td", null, log.at),
        h("td", null, log.actor),
        h("td", null, log.action),
        h("td", null, `${log.target_type || ""} ${log.target_id || ""}`),
        h("td", null, log.detail ? JSON.stringify(log.detail) : "")
      ));
    });
    return [h("div", { class: "table-wrap" }, table)];
  }

  function viewSettings() {
    const s = state.settings || {};
    const fields = [
      ["project_id", "默认项目号"],
      ["default_strain", "默认品系"],
      ["mouse_no_pad", "编号位数"],
      ["retention_days", "保留天数"],
      ["publish_target", "发布目标 URL"],
    ];
    const form = h("div", { style: "max-width:480px;display:grid;gap:12px" });
    const inputs = {};
    fields.forEach(([key, label]) => {
      const inp = h("input", { value: s[key] ?? "" });
      inputs[key] = inp;
      form.appendChild(h("label", { class: "field" }, h("span", null, label), inp));
    });
    form.appendChild(h("button", {
      class: "btn primary",
      onClick: async () => {
        const body = {};
        Object.entries(inputs).forEach(([k, inp]) => {
          body[k] = inp.type === "number" ? Number(inp.value) : inp.value;
        });
        await api("/api/settings", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        await loadRoute();
      },
    }, "保存设置"));
    form.appendChild(h("p", { class: "muted", style: "font-size:12px" }, s.admin_password_hint || ""));
    return [form];
  }

  function viewChangePassword(forced) {
    const cur = h("input", { type: "password", placeholder: "当前密码" });
    const next = h("input", { type: "password", placeholder: "新密码（至少 8 位）" });
    const again = h("input", { type: "password", placeholder: "确认新密码" });
    const err = h("p", { class: "muted", style: "color:var(--mv-danger)" });
    return h(
      "div",
      { class: "login-wrap" },
      h(
        "div",
        { class: "login-card" },
        h("h1", null, forced ? "请修改密码" : "修改密码"),
        h("p", { class: "muted", style: "text-align:center;margin:0" },
          forced ? "首次登录必须修改默认/随机密码后才能继续使用管理台" : "更新当前登录账号密码"),
        h("label", { class: "field" }, h("span", null, "当前密码"), cur),
        h("label", { class: "field" }, h("span", null, "新密码"), next),
        h("label", { class: "field" }, h("span", null, "确认新密码"), again),
        err,
        h("button", {
          class: "btn primary",
          onClick: async () => {
            if (next.value.length < 8) {
              err.textContent = "新密码至少 8 位";
              return;
            }
            if (next.value !== again.value) {
              err.textContent = "两次输入的新密码不一致";
              return;
            }
            try {
              const r = await api("/api/me/password", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  current_password: cur.value,
                  new_password: next.value,
                }),
              });
              state.user = r.user;
              navigate("data", true);
              await loadRoute();
            } catch (e) {
              err.textContent = e.message || "修改失败";
            }
          },
        }, "保存新密码"),
        !forced
          ? h("button", { class: "btn ghost", onClick: () => navigate("data") }, "返回")
          : null
      )
    );
  }

  function viewLogin() {
    const user = h("input", { placeholder: "用户名", value: "admin" });
    const pass = h("input", { type: "password", placeholder: "密码" });
    const err = h("p", { class: "muted", style: "color:var(--mv-danger)" });
    return h(
      "div",
      { class: "login-wrap" },
      h(
        "div",
        { class: "login-card" },
        h("h1", null, "MouseVision Edge"),
        h("p", { class: "muted", style: "text-align:center;margin:0" }, "电脑端数据管理登录"),
        h("label", { class: "field" }, h("span", null, "用户名"), user),
        h("label", { class: "field" }, h("span", null, "密码"), pass),
        err,
        h("button", {
          class: "btn primary",
          onClick: async () => {
            try {
              const r = await api("/api/login", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ username: user.value, password: pass.value }),
              });
              state.user = r.user;
              if (r.user.must_change_password) {
                state.route = "change-password";
                render();
                return;
              }
              navigate("data", true);
              await loadRoute();
            } catch (e) {
              err.textContent = e.message || "登录失败";
            }
          },
        }, "登录"),
        h("p", { class: "muted", style: "font-size:12px;text-align:center" },
          "默认账号 admin；若未设置 MOUSEVISION_ADMIN_PASSWORD，首次启动密码打印在服务日志中")
      )
    );
  }

  function render() {
    $app.replaceChildren();
    if (state.route === "login" || !state.user) {
      $app.appendChild(viewLogin());
      return;
    }
    if (state.route === "change-password" || state.user.must_change_password) {
      $app.appendChild(viewChangePassword(Boolean(state.user.must_change_password)));
      return;
    }
    const views = {
      data: viewData,
      overview: viewOverview,
      verify: viewQuickVerify,
      publish: viewPublish,
      export: viewExport,
      boxes: viewBoxes,
      mice: viewMice,
      users: viewUsers,
      logs: viewLogs,
      settings: viewSettings,
    };
    const fn = views[state.route] || viewData;
    const content = fn();
    const nodes = Array.isArray(content) ? content : [content];
    $app.appendChild(shell(nodes));
  }

  window.addEventListener("popstate", () => {
    state.route = location.pathname.replace(/^\/pc\/?/, "") || "data";
    loadRoute();
  });

  bootstrap();
})();
