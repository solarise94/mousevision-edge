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
    boxes: [],
    overview: null,
    logs: [],
    settings: {},
    pendingBadge: 0,
  };

  const ROUTES = [
    { id: "data", label: "数据管理", section: "数据管理" },
    { id: "overview", label: "数据总览", section: "数据管理" },
    { id: "verify", label: "数据核对", section: "数据管理", badge: () => state.pendingBadge },
    { id: "publish", label: "发布管理", section: "数据管理" },
    { id: "export", label: "导出管理", section: "数据管理" },
    { id: "boxes", label: "箱子管理", section: "基础信息" },
    { id: "mice", label: "小鼠管理", section: "基础信息" },
    { id: "users", label: "用户管理", section: "系统管理", roles: ["admin"] },
    { id: "logs", label: "操作日志", section: "系统管理", roles: ["admin", "operator"] },
    { id: "settings", label: "系统设置", section: "系统管理", roles: ["admin"] },
  ];

  const TITLES = Object.fromEntries(ROUTES.map((r) => [r.id, r.label]));

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

  async function loadRoute() {
    if (!state.user) return;
    try {
      if (["data", "verify", "publish"].includes(state.route)) await loadRecords();
      if (state.route === "overview") state.overview = await api("/api/overview");
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
      state.pendingBadge = state.stats.pending_count || 0;
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

  function drawChart(canvas, points, key) {
    if (!canvas || !points?.length) return;
    const ctx = canvas.getContext("2d");
    const w = canvas.width = canvas.clientWidth * 2;
    const h = canvas.height = canvas.clientHeight * 2;
    ctx.scale(2, 2);
    const cw = w / 2, ch = h / 2;
    ctx.clearRect(0, 0, cw, ch);
    const vals = points.map((p) => p[key] ?? p.count ?? 0);
    const max = Math.max(...vals, 1);
    const barW = Math.max(4, (cw - 20) / vals.length - 4);
    vals.forEach((v, i) => {
      const bh = (v / max) * (ch - 30);
      ctx.fillStyle = "#3ddc84";
      ctx.fillRect(10 + i * (barW + 4), ch - bh - 10, barW, bh);
    });
  }

  function shell(children) {
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
        h("div", { class: "content" }, children)
      )
    );
  }

  function kpiRow(stats) {
    const cards = [
      ["总记录数", stats.total_records, ""],
      ["待核对", stats.pending_count, "green"],
      ["已发布", stats.published_count, "green"],
      ["已删除", stats.deleted_count, ""],
      ["平均体重", stats.average_weight != null ? `${stats.average_weight} g` : "--", "green"],
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
        const card = h("button", {
          class: `record-card${state.selectedId === rec.record_id ? " selected" : ""}`,
          onClick: () => onSelect(rec),
        });
        const thumb = h("div", { class: "thumb" });
        thumb.appendChild(h("img", { src: rec.photo_url, alt: "" }));
        thumb.appendChild(h("span", { class: "idx" }, `#${String(rec.ordinal).padStart(2, "0")}`));
        thumb.appendChild(h("span", { class: `status-badge status-${rec.status}` }, statusLabel(rec.status)));
        card.appendChild(thumb);
        card.appendChild(
          h("div", { class: "body" },
            h("div", { class: "weight" }, fmtWeight(rec.weight)),
            h("div", { class: "meta" }, `${rec.cage_id} · ${rec.timestamp || ""}`)
          )
        );
        return card;
      })
    );
  }

  function detailPanel(rec) {
    if (!rec) return h("div", { class: "detail empty" }, "选择一条记录查看详情");
    const photo = rec.status === "deleted"
      ? `${rec.photo_url}?include_deleted=true`
      : rec.photo_url;
    const dl = h("dl");
    const rows = [
      ["记录 ID", rec.record_id],
      ["箱号", rec.cage_id],
      ["品系", rec.strain || "-"],
      ["小鼠编号", rec.ordinal != null ? String(rec.ordinal).padStart(2, "0") : "-"],
      ["体重", fmtWeight(rec.weight)],
      ["置信度", rec.confidence != null ? Number(rec.confidence).toFixed(2) : "-"],
      ["称重时间", rec.timestamp || "-"],
      ["录制时长", rec.duration_sec != null ? `${rec.duration_sec}s` : "-"],
      ["状态", statusLabel(rec.status)],
    ];
    rows.forEach(([k, v]) => dl.appendChild(h("div", null, h("dt", null, k), h("dd", null, v))));

    const actions = h("div", { class: "actions" });
    if (canWrite()) {
      const btns = [
        ["核对通过", "primary", () => act(rec.record_id, "verify")],
        ["发布", "primary", () => act(rec.record_id, "publish")],
        ["撤回发布", "", () => act(rec.record_id, "unpublish")],
        ["删除", "danger", () => act(rec.record_id, "delete")],
        ["恢复", "", () => act(rec.record_id, "restore")],
        ["回放复核", "", () => reviewPlayback(rec)],
      ];
      btns.forEach(([label, cls, fn]) =>
        actions.appendChild(h("button", { class: `btn ${cls}`.trim(), onClick: fn }, label))
      );
    }

    return h(
      "div",
      { class: "detail" },
      h("h3", null, "记录详情"),
      h("div", { class: "media" }, h("img", { src: photo, alt: "" })),
      dl,
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
    await api(url, { method });
    await loadRecords();
    render();
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
    const pager = h("div", { class: "pager" });
    const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
    pager.appendChild(h("button", {
      class: "btn", disabled: state.page <= 1,
      onClick: () => { state.page--; loadRecords().then(render); },
    }, "上一页"));
    pager.appendChild(h("span", null, `${state.page} / ${pages}`));
    pager.appendChild(h("button", {
      class: "btn", disabled: state.page >= pages,
      onClick: () => { state.page++; loadRecords().then(render); },
    }, "下一页"));

    return [
      filtersBar(() => loadRecords().then(render)),
      kpiRow(state.stats),
      tabBar,
      h("div", { class: "data-layout" },
        h("div", null, recordGrid(select), pager),
        detailPanel(state.selected)
      ),
    ];
  }

  function viewOverview() {
    const o = state.overview || {};
    const dailyCanvas = h("canvas");
    const weightCanvas = h("canvas");
    setTimeout(() => {
      drawChart(dailyCanvas, o.daily_counts || [], "count");
      const wpoints = (o.weight_samples || []).map((w, i) => ({ count: w, i }));
      drawChart(weightCanvas, wpoints.slice(0, 30), "count");
    }, 50);
    return [
      kpiRow({
        total_records: o.total_records,
        pending_count: o.pending_count,
        published_count: o.published_count,
        deleted_count: o.deleted_count,
        average_weight: o.average_weight,
      }),
      h("div", { class: "chart-box" }, h("div", { class: "muted", style: "margin-bottom:8px" }, "每日记录数"), dailyCanvas),
      h("div", { class: "chart-box" }, h("div", { class: "muted", style: "margin-bottom:8px" }, "体重样本分布"), weightCanvas),
    ];
  }

  function viewVerify() {
    state.tab = "pending";
    return viewData();
  }

  function viewPublish() {
    state.tab = "published";
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
      verify: viewVerify,
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
    $app.appendChild(shell(...nodes));
  }

  window.addEventListener("popstate", () => {
    state.route = location.pathname.replace(/^\/pc\/?/, "") || "data";
    loadRoute();
  });

  bootstrap();
})();
