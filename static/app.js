/* AdWatch frontend — vanilla JS, no build step, no framework.
   Talks to the FastAPI backend in adwatch/web.py via fetch()/SSE. */
(() => {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const debounce = (fn, ms = 200) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  // ---------------------------------------------------------------- toasts
  // In-app notifications instead of browser alert() popups. Shadowing `alert`
  // inside this IIFE routes every existing alert(...) call here unchanged.
  function toast(message, type) {
    let stack = $("#toastStack");
    if (!stack) {
      stack = document.createElement("div");
      stack.id = "toastStack";
      stack.className = "toast-stack";
      document.body.appendChild(stack);
    }
    const kind = type || (/could not|failed|error|invalid|nicht/i.test(String(message)) ? "error" : "info");
    const el = document.createElement("div");
    el.className = `toast toast-${kind}`;
    el.setAttribute("role", "status");
    el.innerHTML = `<span class="toast-dot"></span><div class="toast-msg"></div>
      <button class="toast-close" aria-label="Dismiss">✕</button>`;
    el.querySelector(".toast-msg").textContent = String(message);
    const remove = () => {
      el.classList.add("toast-out");
      setTimeout(() => el.remove(), 200);
    };
    el.querySelector(".toast-close").addEventListener("click", remove);
    stack.appendChild(el);
    while (stack.children.length > 4) stack.firstChild.remove();  // cap the pile
    setTimeout(remove, kind === "error" ? 8000 : 5000);
  }
  const alert = (msg) => toast(msg);   // route all legacy alert() calls to toasts

  const CATEGORY_LABELS = { recruitment: "Hiring", product_sale: "Selling",
    brand_awareness: "Brand", event_promo: "Event", other: "Other" };
  // These describe ONE thing only: whether we found the company's Meta/Facebook
  // page. They say nothing about Google (a separate per-source link) — so the
  // labels name Meta explicitly instead of a bare "confirmed" that reads like a
  // whole-company verdict.
  const STATUS_LABEL = { confirmed: "Meta page found", ambiguous: "Meta page unclear",
    no_ads_found: "No Meta page found", pending: "Meta not checked yet", locked: "Meta page locked" };
  // Enrichment status (see enrich/service.py) — German, user-facing.
  const ENRICH_STATUS_LABEL = {
    none: "noch nicht angereichert", enriched: "angereichert",
    needs_review: "Website-Vorschlag prüfen", no_website_found: "keine Website gefunden",
    error: "Fehler",
  };
  // Customer lifecycle (derived from the Umsatz columns — see customers.py).
  const CUSTOMER_STATE_LABEL = { active: "Aktiver Kunde", new: "Neukunde",
    lapsed: "Ehemaliger Kunde", never: "Nie gekauft" };
  const CUSTOMER_STATE_SHORT = { active: "aktiv", new: "neu", lapsed: "ehemalig", never: "nie" };

  function customerStateChip(state) {
    if (!state) return '<span class="muted">—</span>';
    return `<span class="state-chip state-${esc(state)}" title="${esc(CUSTOMER_STATE_LABEL[state] || state)}">${esc(CUSTOMER_STATE_SHORT[state] || state)}</span>`;
  }

  function fitCell(fit) {
    if (fit == null) return '<span class="muted">—</span>';
    const cls = fit >= 85 ? "fit-high" : (fit >= 60 ? "fit-mid" : "fit-low");
    return `<span class="fit-badge ${cls}">${Math.round(fit)}</span>`;
  }

  const RUN_STATUS_LABEL = { ok: "ok", no_active_ads: "no active ads",
    no_ads_found: "no ads found", ambiguous_match: "ambiguous", error: "error" };
  const FLAG_LABEL = { new_campaign: "New campaign", first_seen: "First activity",
    biggest_mover: "Biggest mover", most_active: "Most active",
    hiring_push: "Hiring push", went_quiet: "Went quiet" };

  let STATE = null;
  let selectedCompanyId = null;
  const searchTermCache = {};   // company_id -> default search term
  const expandedPages = new Set(); // company_ids whose "Linked pages" panel is open
  const CUST_DROP = {};   // Companies Explorer checkbox dropdowns, keyed by filter field
  const COMP_DROP = {};   // Dashboard quick-filter checkbox dropdowns, keyed by filter field

  // ------------------------------------------------------------------ utils
  async function api(path, method, body) {
    const opts = { method: method || "GET", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let r;
    try {
      r = await fetch(path, opts);
    } catch (e) {
      // fetch() rejects only when the request never got a response at all —
      // the server is down or was killed mid-request. "Failed to fetch" alone
      // doesn't tell you that, and for a send it doesn't say whether the mail
      // went out, so name both.
      throw new Error(`Server nicht erreichbar (läuft die App noch?) — Anfrage an ${path} `
                      + "hat keine Antwort erhalten. Bei einem Versand bitte im Log prüfen, "
                      + "ob die Mail rausging, bevor du erneut sendest.");
    }
    if (!r.ok) {
      let msg = r.statusText;
      try { msg = (await r.json()).detail || msg; } catch (e) { /* ignore */ }
      throw new Error(msg);
    }
    const ct = r.headers.get("content-type") || "";
    return ct.includes("application/json") ? r.json() : r;
  }

  const esc = (s) => (s == null ? "" : String(s))
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  // In-app confirm modal (replaces the browser's native confirm()). Returns a
  // Promise<boolean>. `message` may contain \n (line break) and \n\n (paragraph).
  function appConfirm(message, { title = "Please confirm", confirmText = "Confirm",
                                 cancelText = "Cancel", danger = false } = {}) {
    return new Promise((resolve) => {
      const body = String(message).split("\n\n")
        .map(p => `<p>${esc(p).replace(/\n/g, "<br>")}</p>`).join("");
      const overlay = document.createElement("div");
      overlay.className = "modal-backdrop";
      overlay.innerHTML = `
        <div class="modal" role="dialog" aria-modal="true">
          <h3 class="modal-title">${esc(title)}</h3>
          <div class="modal-body">${body}</div>
          <div class="modal-actions">
            <button class="btn modal-cancel">${esc(cancelText)}</button>
            <button class="btn ${danger ? "btn-danger" : "btn-primary"} modal-confirm">${esc(confirmText)}</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      const cancelBtn = $(".modal-cancel", overlay);
      const confirmBtn = $(".modal-confirm", overlay);
      const done = (val) => { overlay.remove(); document.removeEventListener("keydown", onKey); resolve(val); };
      const onKey = (e) => {
        if (e.key === "Escape") { e.preventDefault(); done(false); return; }
        // Enter confirms ONLY when the confirm button holds focus — never when
        // the user has tabbed to Cancel (a real footgun in front of bulk delete).
        if (e.key === "Enter" && document.activeElement === confirmBtn) { e.preventDefault(); done(true); return; }
        // Trap focus inside the dialog so Tab can't wander into the dimmed page.
        if (e.key === "Tab") {
          const focusables = [cancelBtn, confirmBtn];
          const i = focusables.indexOf(document.activeElement);
          e.preventDefault();
          const next = e.shiftKey ? (i <= 0 ? focusables.length - 1 : i - 1) : (i + 1) % focusables.length;
          focusables[next].focus();
        }
      };
      overlay.addEventListener("click", (e) => { if (e.target === overlay) done(false); });
      cancelBtn.addEventListener("click", () => done(false));
      confirmBtn.addEventListener("click", () => done(true));
      document.addEventListener("keydown", onKey);
      // focus Cancel by default for danger dialogs (safe default), else Confirm
      (danger ? cancelBtn : confirmBtn).focus();
    });
  }

  const eur = (v) => v == null ? "—" : "€" + Math.round(v).toLocaleString("de-DE");

  // facebook.com/<page_id> is a stable permalink for any real Facebook page —
  // no need to store a separate profile URL per page.
  const fbPageUrl = (pageId) => `https://www.facebook.com/${encodeURIComponent(pageId)}`;

  function fbPageCellHtml(r) {
    // Prefer the explicit page_url — serper/website handle-only confirms have a
    // real Facebook/Instagram URL but no numeric page_id yet. Falling back to
    // page_id-only would hide a resolved identity behind a blank "+ Link".
    const href = r.page_url || (r.page_id ? fbPageUrl(r.page_id) : null);
    // icon-only on purpose: this column sits at the far right as a reference
    // link, so the affordance lives in the tooltip rather than in column width
    const editBtn = `<button class="btn btn-sm fb-edit-btn" data-id="${r.id}" title="${href ? "Verknüpfte Meta-Seite bearbeiten" : "Facebook-Seite verknüpfen"}">${href ? "✎" : "+"}</button>`;
    if (!href) return `<span class="muted">—</span> ${editBtn}`;
    const label = r.page_name || r.page_id || r.page_url;
    const isIG = href.includes("instagram.com");
    const platform = isIG ? ` <span class="role-badge" title="Instagram profile — same Meta ad identity as a Facebook page">IG</span>` : "";
    const titleAttr = ` title="${esc(label)}"`;   // the link is truncated — full name on hover
    const needsId = (!r.page_id && r.resolution_status === "confirmed")
      ? ` <span class="candidate-flag flag-warn" title="Meta-Seite bestätigt, aber die numerische Page-ID für den Ad lookup fehlt noch — der Ad lookup ermittelt sie, oder per ✎ manuell eintragen.">⚠</span>`
      : "";
    return `<a class="link" href="${esc(href)}" target="_blank"${titleAttr}>${esc(label)}</a>${platform}${needsId} ${editBtn}`;
  }

  function spendCell(m) {
    if (!m.has_data) return "—";
    if (m.total_active_ads === 0) return "€0";
    return `${eur(m.spend_low)} – ${eur(m.spend_high)}`;
  }

  // Checkbox multi-select dropdown: a button ("Segment (2)") that opens a
  // searchable checkbox list. Used for every KV/Segment/Sub-segment/Vertriebsweg/
  // Land filter (both "include" and "exclude") across the Companies Explorer
  // and the Dashboard's quick filters — one widget, one behavior everywhere.
  function mountCheckDropdown(containerId, { placeholder, onChange, labelFor }) {
    const label = labelFor || ((v) => v);
    const container = $(`#${containerId}`);
    container.classList.add("checkdrop");
    container.innerHTML = `
      <button type="button" class="btn btn-sm checkdrop-btn"></button>
      <div class="checkdrop-panel hidden">
        <input type="text" class="checkdrop-search" placeholder="Search…">
        <div class="checkdrop-list"></div>
      </div>`;
    const btn = $(".checkdrop-btn", container);
    const panel = $(".checkdrop-panel", container);
    const searchEl = $(".checkdrop-search", container);
    const listEl = $(".checkdrop-list", container);

    function getSelected() {
      return $$("input[type=checkbox]:checked", listEl).map(cb => cb.value);
    }
    function updateLabel() {
      const n = getSelected().length;
      btn.textContent = n ? `${placeholder} (${n})` : placeholder;
      btn.classList.toggle("checkdrop-btn-active", n > 0);
    }
    function setOptions(values) {
      const kept = new Set(getSelected());
      listEl.innerHTML = values.map(v => `
        <label class="checkdrop-item">
          <input type="checkbox" value="${esc(v)}" ${kept.has(v) ? "checked" : ""}>
          <span>${esc(label(v))}</span>
        </label>`).join("");
      updateLabel();
    }
    function clear() {
      $$("input[type=checkbox]", listEl).forEach(cb => { cb.checked = false; });
      updateLabel();
    }
    function setSelected(values) {
      const want = new Set(values || []);
      $$("input[type=checkbox]", listEl).forEach(cb => { cb.checked = want.has(cb.value); });
      updateLabel();
    }

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const willOpen = panel.classList.contains("hidden");
      $$(".checkdrop-panel").forEach(p => p.classList.add("hidden"));
      if (willOpen) {
        panel.classList.remove("hidden");
        searchEl.value = "";
        $$(".checkdrop-item", listEl).forEach(l => l.classList.remove("hidden"));
      }
    });
    panel.addEventListener("click", (e) => e.stopPropagation());
    searchEl.addEventListener("input", () => {
      const q = searchEl.value.trim().toLowerCase();
      $$(".checkdrop-item", listEl).forEach(l =>
        l.classList.toggle("hidden", !l.textContent.toLowerCase().includes(q)));
    });
    listEl.addEventListener("change", () => { updateLabel(); onChange(); });

    updateLabel();
    return { setOptions, getSelected, clear, setSelected };
  }
  document.addEventListener("click", () => $$(".checkdrop-panel").forEach(p => p.classList.add("hidden")));

  // ------------------------------------------------------------------ resizable columns
  // Every data table gets drag handles on the RIGHT EDGE of its headers (and
  // only there). The first drag freezes the current auto-layout widths and
  // switches the table to table-layout:fixed so one column's width never
  // reflows the others; double-click on a handle resets the whole table.
  function makeColumnsResizable(table) {
    const ths = $$("thead th", table);
    if (!ths.length || table.dataset.resizable) return;
    table.dataset.resizable = "1";

    function freeze() {
      if (table.classList.contains("col-resized")) return;
      ths.forEach(th => { if (th.offsetParent !== null) th.style.width = th.offsetWidth + "px"; });
      table.classList.add("col-resized");
      table.style.tableLayout = "fixed";
      table.style.width = "max-content";   // grows/shrinks with the columns, scrolls in .table-wrap
    }
    function reset() {
      ths.forEach(th => { th.style.width = ""; });
      table.classList.remove("col-resized");
      table.style.tableLayout = "";
      table.style.width = "";
    }

    ths.forEach(th => {
      const grip = document.createElement("span");
      grip.className = "col-grip";
      grip.title = "Ziehen: Spaltenbreite · Doppelklick: zurücksetzen";
      th.appendChild(grip);
      // a resize gesture must never count as a header CLICK (sort/filter menu)
      grip.addEventListener("click", e => e.stopPropagation());
      grip.addEventListener("dblclick", e => { e.stopPropagation(); reset(); });
      grip.addEventListener("pointerdown", (e) => {
        e.preventDefault(); e.stopPropagation();
        freeze();
        const startX = e.clientX;
        const startW = th.offsetWidth;
        const move = (ev) => { th.style.width = Math.max(44, startW + (ev.clientX - startX)) + "px"; };
        const up = () => {
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", up);
          document.body.classList.remove("col-resizing");
        };
        document.body.classList.add("col-resizing");
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
      });
    });
  }
  // ------------------------------------------------------------ sort + filter, any table
  // The Firmen table gets its column menus from the server (it is paginated, so
  // sorting and filtering have to happen in SQL). Chancen, Objekte and Prüfen
  // arrive complete in one response, so they get the same affordances computed
  // in the browser: click a header to sort, tick values to filter.
  //
  // State lives OUTSIDE the table, keyed by the wrapper id, because every reload
  // replaces the whole <table> — without this, hitting Aktualisieren silently
  // dropped whatever you had filtered to.
  const TABLE_STATE = new Map();   // wrapId -> {sort:{col,dir}, filters:{col:Set}}
  // wrapId -> how many rows matched on the SERVER (may exceed what was sent)
  const TABLE_TOTALS = {};

  const _stateFor = (wrapId) => {
    if (!TABLE_STATE.has(wrapId)) TABLE_STATE.set(wrapId, { sort: null, filters: {} });
    return TABLE_STATE.get(wrapId);
  };

  // One popover, shared with the Firmen column menus. Created on demand rather
  // than assumed: the Firmen wiring builds it too, but far later in this file,
  // and depending on that order would break the moment either side moved.
  function _menuEl() {
    let el = $("#thMenu");
    if (!el) {
      el = document.createElement("div");
      el.id = "thMenu";
      el.className = "th-menu hidden";
      el.addEventListener("click", (e) => e.stopPropagation());
      document.body.appendChild(el);
      document.addEventListener("click", () => el.classList.add("hidden"));
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") el.classList.add("hidden");
      });
    }
    return el;
  }

  // Sort key for a cell: a number when the column reads as numeric (German
  // formatting, currency and % included), a date for dd.mm.yyyy / ISO, else
  // lowercased text. Empty always sorts last, whichever direction.
  function _cellKey(td) {
    const raw = (td?.textContent || "").trim();
    if (!raw || raw === "—") return { empty: true, v: "" };
    const iso = raw.match(/(\d{4})-(\d{2})-(\d{2})/);
    if (iso) return { v: `${iso[1]}${iso[2]}${iso[3]}` };
    const de = raw.match(/^(\d{2})\.(\d{2})\.(\d{4})/);
    if (de) return { v: `${de[3]}${de[2]}${de[1]}` };
    const num = raw.replace(/[^\d,.\-]/g, "");
    if (num && /\d/.test(num)) {
      const n = Number(num.replace(/\./g, "").replace(",", "."));
      if (Number.isFinite(n) && /^[\s€%\d.,\-+]*$/.test(raw)) return { v: n, num: true };
    }
    return { v: raw.toLowerCase() };
  }

  function _applyTableState(table, wrapId) {
    const st = _stateFor(wrapId);
    const body = table.tBodies[0];
    if (!body) return;
    const rows = [...body.rows];

    // filters: a row survives only if every active column filter accepts it
    const active = Object.entries(st.filters).filter(([, s]) => s && s.size);
    rows.forEach(tr => {
      const ok = active.every(([col, set]) =>
        set.has((tr.cells[col]?.textContent || "").trim()));
      tr.classList.toggle("row-filtered", !ok);
    });

    if (st.sort) {
      const { col, dir } = st.sort;
      const sorted = rows.slice().sort((a, b) => {
        const ka = _cellKey(a.cells[col]), kb = _cellKey(b.cells[col]);
        if (ka.empty !== kb.empty) return ka.empty ? 1 : -1;   // blanks last, always
        if (ka.v === kb.v) return 0;
        return (ka.v > kb.v ? 1 : -1) * (dir === "desc" ? -1 : 1);
      });
      sorted.forEach(tr => body.appendChild(tr));
    }

    $$("thead th", table).forEach((th, i) => {
      th.classList.toggle("sorted-asc", !!st.sort && st.sort.col === i && st.sort.dir === "asc");
      th.classList.toggle("sorted-desc", !!st.sort && st.sort.col === i && st.sort.dir === "desc");
      const srv = (SERVER_COLUMNS[wrapId] || {})[i];
      const srvActive = srv ? !!$(srv.select)?.value : false;
      th.classList.toggle("th-filtered", srvActive || !!(st.filters[i] && st.filters[i].size));
    });

    const shown = rows.filter(r => !r.classList.contains("row-filtered")).length;
    let tag = table.parentElement.querySelector(".table-count");
    if (!tag) {
      tag = document.createElement("div");
      tag.className = "table-count muted";
      table.parentElement.insertBefore(tag, table);
    }
    // TABLE_TOTALS[wrapId] = how many rows MATCHED on the server. The table only
    // ever holds a capped slice (Objekte 300 of 52.796), so saying "34 von 300"
    // let a reader conclude 300 was everything. Name the cap and the real total.
    const total = TABLE_TOTALS[wrapId];
    const loaded = rows.length;
    const capped = total != null && total > loaded;
    let txt = shown === loaded ? `${loaded} Zeilen` : `${shown} von ${loaded} geladenen Zeilen`;
    if (capped) txt += ` · ${total.toLocaleString("de-DE")} insgesamt (nur die ersten ${loaded} geladen)`;
    if (shown !== loaded && capped) txt += " — Spaltenfilter wirkt nur auf die geladenen Zeilen";
    tag.textContent = txt;
  }

  // Columns that map onto a SERVER filter. Filtering these in the browser would
  // search the loaded slice (Objekte: 300 of 52.796) and quietly report a
  // fraction of the truth, so the menu drives the server control instead.
  const SERVER_COLUMNS = {
    objekteWrap: {1: {select: "#objekteStatus", label: "Status"}},
  };

  function _openColMenu(table, wrapId, th, colIdx) {
    const st = _stateFor(wrapId);
    const server = (SERVER_COLUMNS[wrapId] || {})[colIdx];
    if (server) return _openServerColMenu(table, wrapId, th, colIdx, server);
    const body = table.tBodies[0];
    const values = [...new Set([...(body ? body.rows : [])]
      .map(tr => (tr.cells[colIdx]?.textContent || "").trim()))]
      .filter(v => v !== "").sort((a, b) => a.localeCompare(b, "de"));
    const chosen = st.filters[colIdx] || new Set();
    // A column of 300 distinct free-text values is a search box, not a checklist.
    const listable = values.length <= 60;

    const menu = _menuEl();
    menu.innerHTML = `
      <div class="thm-head">${esc(th.textContent.replace("▾", "").trim())}</div>
      <div class="thm-sec thm-sort">
        <button class="btn btn-sm thm-sort-btn${st.sort && st.sort.col === colIdx && st.sort.dir === "asc" ? " btn-primary" : ""}" data-dir="asc">↑ Aufsteigend</button>
        <button class="btn btn-sm thm-sort-btn${st.sort && st.sort.col === colIdx && st.sort.dir === "desc" ? " btn-primary" : ""}" data-dir="desc">↓ Absteigend</button>
      </div>
      ${listable ? `
      <div class="thm-sec"><div class="thm-sec-title">Werte (${values.length})</div>
        <input type="text" class="thm-input" id="thmValSearch" placeholder="suchen…">
        <div class="thm-list">${values.map(v => `
          <label class="thm-item"><input type="checkbox" class="thm-val" value="${esc(v)}"${chosen.has(v) ? " checked" : ""}>
            <span>${esc(v.length > 42 ? v.slice(0, 42) + "…" : v)}</span></label>`).join("")}</div>
      </div>` : `
      <div class="thm-sec"><div class="thm-sec-title">Enthält</div>
        <input type="text" class="thm-input" id="thmContains" placeholder="Text…">
      </div>`}
      <div class="thm-sec"><button class="btn btn-sm" id="thmClearCol">Spaltenfilter löschen</button></div>`;

    const r = th.getBoundingClientRect();
    menu.classList.remove("hidden");
    const w = Math.min(300, window.innerWidth - 24);
    menu.style.width = w + "px";
    menu.style.top = Math.round(r.bottom + 4) + "px";
    menu.style.left = Math.round(Math.min(r.left, window.innerWidth - w - 12)) + "px";

    const close = () => menu.classList.add("hidden");
    $$(".thm-sort-btn", menu).forEach(b => b.addEventListener("click", () => {
      st.sort = { col: colIdx, dir: b.dataset.dir };
      close(); _applyTableState(table, wrapId);
    }));
    $("#thmValSearch", menu)?.addEventListener("input", (e) => {
      const q = e.target.value.trim().toLowerCase();
      $$(".thm-item", menu).forEach(l =>
        l.classList.toggle("hidden", !l.textContent.toLowerCase().includes(q)));
    });
    $$(".thm-val", menu).forEach(cb => cb.addEventListener("change", () => {
      const picked = $$(".thm-val:checked", menu).map(x => x.value);
      st.filters[colIdx] = new Set(picked);
      _applyTableState(table, wrapId);
    }));
    $("#thmContains", menu)?.addEventListener("input", (e) => {
      const q = e.target.value.trim().toLowerCase();
      st.filters[colIdx] = q
        ? new Set(values.filter(v => v.toLowerCase().includes(q)))
        : new Set();
      _applyTableState(table, wrapId);
    });
    $("#thmClearCol", menu)?.addEventListener("click", () => {
      delete st.filters[colIdx];
      close(); _applyTableState(table, wrapId);
    });
  }

  function _openServerColMenu(table, wrapId, th, colIdx, server) {
    const st = _stateFor(wrapId);
    const sel = $(server.select);
    const menu = _menuEl();
    menu.innerHTML = `
      <div class="thm-head">${esc(th.textContent.replace("▾", "").trim())}</div>
      <div class="thm-sec thm-sort">
        <button class="btn btn-sm thm-sort-btn${st.sort && st.sort.col === colIdx && st.sort.dir === "asc" ? " btn-primary" : ""}" data-dir="asc">↑ Aufsteigend</button>
        <button class="btn btn-sm thm-sort-btn${st.sort && st.sort.col === colIdx && st.sort.dir === "desc" ? " btn-primary" : ""}" data-dir="desc">↓ Absteigend</button>
      </div>
      <div class="thm-sec"><div class="thm-sec-title">${esc(server.label)}</div>
        <select class="thm-input" id="thmServerSel">${
          [...sel.options].map(o => `<option value="${esc(o.value)}"${sel.value === o.value ? " selected" : ""}>${esc(o.textContent)}</option>`).join("")
        }</select>
        <div class="sub" style="margin-top:5px">Filtert alle Zeilen, nicht nur die geladenen.</div>
      </div>`;
    const r = th.getBoundingClientRect();
    menu.classList.remove("hidden");
    const w = Math.min(300, window.innerWidth - 24);
    menu.style.width = w + "px";
    menu.style.top = Math.round(r.bottom + 4) + "px";
    menu.style.left = Math.round(Math.min(r.left, window.innerWidth - w - 12)) + "px";
    $$(".thm-sort-btn", menu).forEach(b => b.addEventListener("click", () => {
      st.sort = { col: colIdx, dir: b.dataset.dir };
      menu.classList.add("hidden"); _applyTableState(table, wrapId);
    }));
    $("#thmServerSel", menu)?.addEventListener("change", (e) => {
      menu.classList.add("hidden");
      sel.value = e.target.value;
      sel.dispatchEvent(new Event("change", { bubbles: true }));   // reloads from the server
    });
  }

  function makeTableInteractive(table) {
    const wrap = table.closest(".table-wrap");
    // The Firmen table drives its menus from the server; leave it alone.
    if (!wrap || !wrap.id || table.id === "customersTable" || table.dataset.interactive) return;
    table.dataset.interactive = "1";
    $$("thead th", table).forEach((th, i) => {
      th.classList.add("th-has-menu");
      th.insertAdjacentHTML("beforeend", ` <span class="th-caret">▾</span>`);
      th.addEventListener("click", (e) => {
        if (e.target.classList.contains("col-grip")) return;
        e.stopPropagation();
        const menu = $("#thMenu");
        const same = !menu.classList.contains("hidden") && menu.dataset.owner === `${wrap.id}:${i}`;
        menu.classList.add("hidden");
        if (!same) { menu.dataset.owner = `${wrap.id}:${i}`; _openColMenu(table, wrap.id, th, i); }
      });
    });
    _applyTableState(table, wrap.id);
  }

  function enhanceTable(table) {
    makeColumnsResizable(table);
    makeTableInteractive(table);
  }

  // Clipped text must stay readable somewhere. Measuring every cell after every
  // render would cost a full layout pass on tables of 500 rows, so the tooltip
  // is attached on hover, only to the cell under the pointer, and only when it
  // is actually truncated. Cells that already carry a title keep theirs.
  document.addEventListener("mouseover", (e) => {
    const td = e.target.closest?.("td");
    if (!td || td.dataset.titled || td.title || !td.closest(".table-wrap")) return;
    td.dataset.titled = "1";
    if (td.scrollWidth > td.clientWidth + 1) {
      const full = td.textContent.trim();
      if (full) td.title = full;
    }
  }, true);

  // all data tables (each sits in a .table-wrap); decorative info tables are skipped
  $$(".table-wrap table").forEach(enhanceTable);

  // Chancen, Objekte and Prüfen build their tables AFTER this runs, so a one-time
  // pass reached only the tables present in the HTML — which is why resizing
  // worked on Firmen and nowhere else. Watching the wrappers covers every table
  // the app will ever render, without a call to remember at each render site.
  new MutationObserver((muts) => {
    for (const m of muts) {
      for (const node of m.addedNodes) {
        if (node.nodeType !== 1) continue;
        if (node.tagName === "TABLE") enhanceTable(node);
        else $$("table", node).forEach(enhanceTable);
      }
    }
  }).observe(document.body, { childList: true, subtree: true });

  // ------------------------------------------------------------------ load + render
  async function loadState() {
    const notice = $("#noDataNotice");
    if (!STATE) { notice.classList.remove("hidden"); notice.textContent = "Lädt…"; }
    try {
      STATE = await api("/api/state");
    } catch (e) {
      notice.classList.remove("hidden");
      notice.innerHTML = `Verbindung zum Server fehlgeschlagen. `
        + `<button class="btn btn-sm" id="retryStateBtn">Erneut versuchen</button>`;
      $("#retryStateBtn").addEventListener("click", loadState);
      return;                       // leave the last good render in place, don't blank the app
    }
    render();
    loadDivergence();   // independent fetch — never blocks the main render
  }

  // Divergenz = Marketing-Aktivität × Umsatz-Lücke (insights/divergence.py).
  // The ranked "who to call" list at the top of the dashboard.
  const DIV_LABEL_CLASS = { "Win-back": "div-winback", "Neupotenzial": "div-neu", "Aufsteiger": "div-rising" };
  async function loadDivergence() {
    let d;
    try { d = await api("/api/divergence"); }
    catch { return; }   // dashboard stays usable if this fails
    const rows = d.rows.filter(r => r.divergence > 0).slice(0, 15);
    $("#divergenceMeta").textContent =
      `· ${d.rated} Partner bewertet · ${d.unrated.toLocaleString("de-DE")} ohne Anzeigen-Daten`;
    $("#divergenceEmpty").classList.toggle("hidden", rows.length > 0);
    $("#divergenceTable").classList.toggle("hidden", rows.length === 0);
    $("#divergenceBody").innerHTML = rows.map((r, i) => `
      <tr data-cid="${r.company_id}" title="Click for full company details">
        <td class="muted">${i + 1}</td>
        <td><b>${esc(r.company)}</b></td>
        <td>
          <div class="score-cell">
            <div class="score-track"><div class="score-fill" style="width:${r.divergence}%"></div></div>
            <span class="score-num">${r.divergence}</span>
          </div>
        </td>
        <td>${r.label ? `<span class="div-badge ${DIV_LABEL_CLASS[r.label] || ""}">${esc(r.label)}</span>` : `<span class="muted">—</span>`}</td>
        <td class="div-reason">${esc(r.reason)}</td>
        <td class="num">${r.best_prior_revenue ? "€" + Math.round(r.best_prior_revenue).toLocaleString("de-DE") : "—"}</td>
      </tr>`).join("");
    $$("#divergenceBody tr[data-cid]").forEach(tr => tr.addEventListener("click", (e) => {
      if (e.target.closest("a")) return;   // links keep working
      openCompanyDrawer(Number(tr.dataset.cid));
    }));
  }

  function render() {
    renderTopbar();
    renderSignals();
    renderKpis();
    renderCompanyTable();
    refreshOpenPagesBodies();
    if (selectedCompanyId != null) loadDetail(selectedCompanyId);
  }

  // Re-render any currently-expanded "Pages" panel (Companies tab) whenever
  // STATE.companies reloads — e.g. after linking/unlinking/confirming a page.
  function refreshOpenPagesBodies() {
    expandedPages.forEach(id => {
      if (!document.getElementById(`pages-${id}`)) return;
      renderPagesBodyLazy(id);
    });
  }

  // candidates are no longer shipped in /api/state (they were ~4 MB); fetch the
  // one company's candidate list on demand, cache it on the STATE object, then
  // render the Pages panel.
  async function renderPagesBodyLazy(id) {
    const c = STATE.companies.find(x => x.id === id);
    const el = document.getElementById(`pages-${id}`);
    if (!c) { if (el) el.innerHTML = `<p class="hint">Loading…</p>`; return; }
    if (c.candidates === undefined) {
      if (c.has_candidates) {
        if (el) el.innerHTML = `<p class="hint">Loading…</p>`;
        try { const d = await api(`/api/companies/${id}/detail`); c.candidates = d.company?.candidates || []; }
        catch { c.candidates = []; }
      } else {
        c.candidates = [];
      }
    }
    renderPagesBody(id, c);
  }

  function renderTopbar() {
    const fetchBtn = $("#fetchBtn");
    fetchBtn.disabled = STATE.fetch_running || !STATE.apify_configured;
    fetchBtn.textContent = STATE.fetch_running ? "Fetching…" : "Fetch latest ads";

    const anyData = STATE.metrics.some(m => m.has_data);
    $("#noDataNotice").classList.toggle("hidden", anyData);
  }

  function renderSignals() {
    const box = $("#signals");
    if (!STATE.flags.length) { box.innerHTML = ""; return; }
    box.innerHTML = STATE.flags.map(f => `
      <div class="flag flag-${f.type}">
        <span class="flag-type">${esc(FLAG_LABEL[f.type] || f.type)}</span>
        <span class="flag-company">${esc(f.company)}</span>
        <span class="muted">${esc(f.detail)}</span>
      </div>`).join("");
  }

  function renderKpis() {
    const have = STATE.metrics.filter(m => m.has_data);
    const sum = (fn) => have.reduce((a, m) => a + (fn(m) || 0), 0);
    const totalAds = sum(m => m.total_active_ads);
    const metaAds = sum(m => m.meta_active_ads);
    const googleAds = sum(m => m.google_active_ads);
    const totalNew = sum(m => m.new_ads);
    const hiring = sum(m => (m.ads_by_category || {}).recruitment);
    const selling = sum(m => (m.ads_by_category || {}).product_sale);
    const spendLo = sum(m => m.spend_low), spendHi = sum(m => m.spend_high);
    const active = have.filter(m => (m.total_active_ads || 0) > 0).length;

    const kpis = [
      ["Active advertisers", `${active}/${STATE.companies.length}`],
      ["Active ads · Meta", metaAds],
      ["Active ads · Google", googleAds],
      ["Active ads · Total", totalAds],
      ["New this week", totalNew],
      ["Hiring / Selling", `${hiring} / ${selling}`],
      ["Est. spend / week", have.length ? `${eur(spendLo)}–${eur(spendHi)}` : "—"],
    ];
    $("#kpis").innerHTML = kpis.map(([label, value]) =>
      `<div class="kpi"><div class="kpi-label">${esc(label)}</div><div class="kpi-value">${value}</div></div>`
    ).join("");
  }

  const COMP = { sort: "score", direction: "desc" };

  function companyFilters() {
    return {
      q: $("#compSearch").value.trim().toLowerCase(),
      status: $("#compStatus").value,
      minTotal: $("#compMinTotal").value ? Number($("#compMinTotal").value) : null,
      minMeta: $("#compMinMeta").value ? Number($("#compMinMeta").value) : null,
      minGoogle: $("#compMinGoogle").value ? Number($("#compMinGoogle").value) : null,
      segment: COMP_DROP.segment.getSelected(),
      subSegment: COMP_DROP.subSegment.getSelected(),
      kv: COMP_DROP.kv.getSelected(),
      revenueHistory: $("#compRevenueHistory").value || null,
    };
  }

  // Mirrors customers.py's revenue_history SQL logic client-side, since this
  // table is filtered in-memory from the already-fetched /api/state metrics.
  function matchesRevenueHistory(m, key) {
    if (!key) return true;
    const y0 = m.revenue_y0 || 0;
    const priorAny = [1, 2, 3, 4].some(i => (m[`revenue_y${i}`] || 0) > 0);
    if (key === "lapsed") return y0 <= 0 && priorAny;
    if (key === "new") return y0 > 0 && !priorAny;
    if (key === "any") return y0 > 0 || priorAny;
    if (key === "never") return y0 <= 0 && !priorAny;
    return true;
  }

  function companySortValue(m, key) {
    const cats = m.ads_by_category || {};
    if (key === "company") return (m.company || "").toLowerCase();
    if (key === "hiring") return cats.recruitment || 0;
    if (key === "selling") return cats.product_sale || 0;
    if (key === "spend_low") return m.spend_low || 0;
    return m[key];
  }

  function renderCompanyTable() {
    const f = companyFilters();
    let rows = STATE.metrics.filter(m => {
      if (f.q && !(m.company || "").toLowerCase().includes(f.q)) return false;
      if (f.status && m.resolution_status !== f.status) return false;
      if (f.minTotal != null && (m.total_active_ads || 0) < f.minTotal) return false;
      if (f.minMeta != null && (m.meta_active_ads || 0) < f.minMeta) return false;
      if (f.minGoogle != null && (m.google_active_ads || 0) < f.minGoogle) return false;
      if (f.segment.length && !f.segment.includes(m.segment)) return false;
      if (f.subSegment.length && !f.subSegment.includes(m.sub_segment)) return false;
      if (f.kv.length && !f.kv.includes(m.kv)) return false;
      if (!matchesRevenueHistory(m, f.revenueHistory)) return false;
      return true;
    });
    rows = [...rows].sort((a, b) => {
      const av = companySortValue(a, COMP.sort), bv = companySortValue(b, COMP.sort);
      const an = av == null, bn = bv == null;
      if (an || bn) return an === bn ? 0 : (an ? 1 : -1);   // nulls last
      const cmp = typeof av === "string" ? av.localeCompare(bv) : av - bv;
      return COMP.direction === "desc" ? -cmp : cmp;
    });

    // Cap the DOM: rendering all ~3,600 rows x 13 cols on every keystroke made
    // the dashboard janky. Show the first RENDER_CAP after sort; the count text
    // and a footer note make the truncation explicit (refine filters to narrow).
    const RENDER_CAP = 300;
    const total = rows.length;
    const shown = rows.slice(0, RENDER_CAP);
    // This table only ever shows companies with an ad footprint, so it counts
    // against those — not against the whole book, which is 48k rows and would
    // make every number here look broken.
    const tracked = STATE.metrics.length;
    $("#compFilterCount").textContent = total > RENDER_CAP
      ? `${shown.length} von ${total} angezeigt (${tracked} mit Anzeigen-Daten) — Filter verfeinern`
      : `${total} von ${tracked} mit Anzeigen-Daten`;
    $$("#companyTable th[data-sort]").forEach(th => {
      th.classList.toggle("sorted-asc", th.dataset.sort === COMP.sort && COMP.direction === "asc");
      th.classList.toggle("sorted-desc", th.dataset.sort === COMP.sort && COMP.direction === "desc");
    });

    const body = $("#companyTableBody");
    body.innerHTML = shown.map(m => {
      const cats = m.ads_by_category || {};
      const delta = m.delta_ads;
      let deltaHtml = "";
      if (delta != null && delta !== 0) {
        deltaHtml = `<span class="${delta > 0 ? "delta-up" : "delta-down"}">${delta > 0 ? "+" : ""}${delta}</span>`;
      }
      const note = (m.resolution_status === "confirmed" || m.resolution_status === "pending") ? "" : m.status_label;
      const score = m.score;
      const scoreHtml = score == null ? "—" : `
        <div class="score-cell">
          <div class="score-track"><div class="score-fill" style="width:${Math.max(0, Math.min(100, score))}%"></div></div>
          <span class="score-num">${score.toFixed(0)}</span>
        </div>`;
      return `<tr data-cid="${m.company_id}" class="${m.company_id === selectedCompanyId ? "selected" : ""}">
        <td class="col-dot"><span class="dot dot-${m.resolution_status}" title="${esc(STATUS_LABEL[m.resolution_status] || "")}"></span></td>
        <td>${esc(m.company)}</td>
        <td>${scoreHtml}</td>
        <td class="num">${m.has_data ? (m.meta_active_ads ?? 0) : "—"}</td>
        <td class="num">${m.has_data ? (m.google_active_ads ?? 0) : "—"}</td>
        <td class="num">${m.has_data ? m.total_active_ads : "—"}</td>
        <td class="num">${deltaHtml}</td>
        <td class="num">${m.has_data ? (m.new_ads ?? "—") : "—"}</td>
        <td class="num">${m.has_data ? (cats.recruitment || 0) : "—"}</td>
        <td class="num">${m.has_data ? (cats.product_sale || 0) : "—"}</td>
        <td>${esc((m.products || []).join(", "))}</td>
        <td>${spendCell(m)}</td>
        <td class="muted">${esc(note)}</td>
      </tr>`;
    }).join("")
      + (total > RENDER_CAP
          ? `<tr><td colspan="13" class="muted" style="text-align:center;padding:12px">
             … ${total - RENDER_CAP} weitere ausgeblendet — suchen oder filtern zum Eingrenzen</td></tr>`
          : "");
    // one delegated click listener (was one-per-row over thousands of rows)
    if (!body.dataset.wired) {
      body.dataset.wired = "1";
      body.addEventListener("click", (e) => {
        const tr = e.target.closest("tr[data-cid]");
        if (tr) openCompanyDrawer(Number(tr.dataset.cid));
      });
    }
  }

  function wireCompanyTableControls() {
    COMP_DROP.segment = mountCheckDropdown("compSegmentDrop", { placeholder: "All segments", onChange: renderCompanyTable });
    COMP_DROP.subSegment = mountCheckDropdown("compSubSegmentDrop", { placeholder: "All sub-segments", onChange: renderCompanyTable });
    COMP_DROP.kv = mountCheckDropdown("compKvDrop", { placeholder: "All KV", onChange: renderCompanyTable });

    const debouncedRender = debounce(renderCompanyTable, 200);
    ["compSearch", "compMinTotal", "compMinMeta", "compMinGoogle"].forEach(id =>
      $(`#${id}`).addEventListener("input", debouncedRender));
    $("#compStatus").addEventListener("change", renderCompanyTable);
    $("#compRevenueHistory").addEventListener("change", renderCompanyTable);
    $("#compMoreFiltersBtn").addEventListener("click", () => {
      const nowHidden = $("#compMoreFilters").classList.toggle("hidden");
      $("#compMoreFiltersBtn").textContent = nowHidden ? "Filter ▾" : "Filter ▲";
    });
    $("#compClearFilterBtn").addEventListener("click", () => {
      $("#compSearch").value = ""; $("#compStatus").value = "";
      $("#compMinTotal").value = ""; $("#compMinMeta").value = ""; $("#compMinGoogle").value = "";
      COMP_DROP.segment.clear(); COMP_DROP.subSegment.clear(); COMP_DROP.kv.clear();
      $("#compRevenueHistory").value = "";
      renderCompanyTable();
    });
    $$("#companyTable th[data-sort]").forEach(th => th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (COMP.sort === key) COMP.direction = COMP.direction === "asc" ? "desc" : "asc";
      else { COMP.sort = key; COMP.direction = "desc"; }
      renderCompanyTable();
    }));
  }

  // ------------------------------------------------------------------ detail panel
  async function loadDetail(cid) {
    const panel = $("#detailPanel");
    let data;
    try {
      data = await api(`/api/companies/${cid}/detail`);
    } catch (e) {
      panel.classList.remove("hidden");
      panel.innerHTML = `<p class="muted">Failed to load detail: ${esc(e.message)}</p>`;
      return;
    }
    const m = data.metric, week = data.week, hist = data.history;

    let html = `<div class="detail-head"><h2>${esc(m.company)}</h2>
      <button class="btn btn-sm fetch-company-btn" ${STATE.fetch_running ? "disabled" : ""}
              title="Fetch only this company's data">Fetch this company</button>
    </div>`;
    html += `<div class="detail-kpis">
      <div class="kpi"><div class="kpi-label">Score</div><div class="kpi-value">${m.score != null ? m.score.toFixed(0) + "/100" : "—"}</div></div>
      <div class="kpi"><div class="kpi-label">Active ads</div><div class="kpi-value">${m.has_data ? m.total_active_ads : "—"}</div></div>
      <div class="kpi"><div class="kpi-label">New this week</div><div class="kpi-value">${m.has_data ? (m.new_ads ?? "—") : "—"}</div></div>
      <div class="kpi"><div class="kpi-label">Est. spend / wk</div><div class="kpi-value">${spendCell(m)}</div></div>
    </div>`;

    if (m.resolution_status === "no_ads_found") {
      html += `<div class="warning-box">A name search returned zero ads. Either the name doesn't match the
        Ad Library, or they genuinely run no ads — verify in the Companies &amp; Pages tab.</div>`;
    }

    if (week.has_run && week.pages.length) {
      html += `<div class="detail-section-title">Pages contributing this week</div>`;
      html += week.pages.map(p => {
        const link = !p.page_id ? "" : p.source === "google"
          ? ` · <a class="link" href="https://adstransparency.google.com/advertiser/${esc(p.page_id)}" target="_blank">Open Google Ads Transparency ↗</a>`
          : ` · <a class="link" href="${esc(fbPageUrl(p.page_id))}" target="_blank">Open Facebook page ↗</a>`;
        return `<div class="page-row">
          <span class="dot dot-${p.status === "ok" ? "confirmed" : (p.status === "error" ? "no_ads_found" : "pending")}"></span>
          <b>${esc(p.page_name || p.page_id)}</b><span class="role-badge">${esc(p.source || "meta")}</span><span class="role-badge">${esc(p.role || "main")}</span>
          — ${p.ads} ads · fetched ${esc(p.run_date)}${link}
        </div>`;
      }).join("");
    }

    if (hist.length > 1) {
      html += `<div class="detail-section-title">Weekly trend</div>
        <div class="charts-row">
          <div class="chart-box"><div class="chart-title">Active ads · Hiring · Selling</div><canvas id="chartAds"></canvas></div>
          <div class="chart-box"><div class="chart-title">Score</div><canvas id="chartScore"></canvas></div>
        </div>`;
    } else if (hist.length === 1) {
      html += `<p class="hint">One week of data so far — trends appear from the second week on.</p>`;
    }

    if (week.has_run && week.ads.length) {
      html += `<div class="detail-section-title">All ads (latest week)</div>
        <div class="table-wrap"><table><thead><tr>
          <th>Platform</th><th>Category</th><th>Product</th><th>From page</th><th>CTA</th><th>Media</th>
          <th class="num">EU reach</th><th>Start</th><th>Ad text</th><th>Links</th>
        </tr></thead><tbody>` +
        week.ads.map(a => `<tr>
          <td>${esc(a.source || "meta")}</td>
          <td>${esc(CATEGORY_LABELS[a.category] || a.category)}</td>
          <td>${esc(a.product || "")}</td>
          <td>${esc(a.page_name || "")}</td>
          <td>${esc(a.cta || "")}</td>
          <td>${esc(a.media_type || "")}</td>
          <td class="num">${a.reach ?? ""}</td>
          <td>${esc(a.start_date || "")}</td>
          <td style="white-space:normal;max-width:340px">${esc(a.ad_text || "")}</td>
          <td>${a.ad_library_url ? `<a class="link" href="${esc(a.ad_library_url)}" target="_blank">View ad ↗</a>` : ""}
              ${a.landing_url ? `<br><a class="link" href="${esc(a.landing_url)}" target="_blank">Landing ↗</a>` : ""}</td>
        </tr>`).join("") +
        `</tbody></table></div>`;
    }

    panel.classList.remove("hidden");
    panel.innerHTML = html;
    $(".fetch-company-btn", panel).addEventListener("click", () => startFetch(cid));

    if (hist.length > 1) {
      const labels = hist.map(h => h.week_start);
      drawLineChart($("#chartAds"), labels, [
        { data: hist.map(h => h.total_active_ads), color: "#2f5fa8" },
        { data: hist.map(h => h.recruitment), color: "#a86a1f" },
        { data: hist.map(h => h.product_sale), color: "#1f8a5f" },
      ]);
      drawLineChart($("#chartScore"), labels, [
        { data: hist.map(h => h.score || 0), color: "#2f5fa8" },
      ], { min: 0, max: 100 });
    }
  }

  // Small hand-rolled multi-line chart — no dependency, no build step.
  function drawLineChart(canvas, labels, series, fixedRange) {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 360, h = canvas.clientHeight || 160;
    canvas.width = w * dpr; canvas.height = h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    const pad = 8;
    const allVals = fixedRange ? [fixedRange.min, fixedRange.max] : series.flatMap(s => s.data);
    const min = fixedRange ? fixedRange.min : Math.min(0, ...allVals);
    const max = fixedRange ? fixedRange.max : Math.max(1, ...allVals);
    const n = labels.length;
    const x = (i) => pad + (i / Math.max(n - 1, 1)) * (w - 2 * pad);
    const y = (v) => h - pad - ((v - min) / Math.max(max - min, 1e-9)) * (h - 2 * pad);

    series.forEach(s => {
      ctx.beginPath();
      s.data.forEach((v, i) => i === 0 ? ctx.moveTo(x(i), y(v)) : ctx.lineTo(x(i), y(v)));
      ctx.strokeStyle = s.color; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.stroke();
      s.data.forEach((v, i) => {
        ctx.beginPath(); ctx.arc(x(i), y(v), 2.4, 0, 7); ctx.fillStyle = s.color; ctx.fill();
      });
    });
  }

  // ------------------------------------------------------------------ per-row page management (Companies tab)
  async function ensureSearchTerm(cid) {
    if (searchTermCache[cid] !== undefined) return;
    try { searchTermCache[cid] = (await api(`/api/companies/${cid}/search-term`)).term; }
    catch (e) { searchTermCache[cid] = ""; }
  }

  function candidateRow(cid, cand, onUse) {
    const bits = [];
    if (cand.active_ad_count != null) bits.push(`${cand.active_ad_count} active / ${cand.ad_count || 0} total ads`);
    if (cand.similarity != null) bits.push(`name match ${Math.round(cand.similarity * 100)}%`);
    bits.push(`page id ${cand.page_id}`);
    const flags = [
      cand.site_match ? `<span class="candidate-flag flag-ok">✓ website match</span>` : "",
      cand.blocked ? `<span class="candidate-flag flag-warn">⚠ excluded category</span>` : "",
    ].join("");
    return `<div class="candidate-item">
      <div>
        <b>${esc(cand.name || "(unnamed page)")}</b>${cand.category ? ` · ${esc(cand.category)}` : ""} ${flags}
        <div class="page-meta">${esc(bits.join(" · "))}</div>
        ${cand.profile_uri ? `<a class="link" href="${esc(cand.profile_uri)}" target="_blank">Open page ↗</a>` : ""}
      </div>
      <button class="btn btn-sm use-btn">Use as main</button>
    </div>`;
  }

  function renderPagesBody(cid, c) {
    const box = $(`#pages-${cid}`);
    box.classList.add("open");
    let html = `
      <div class="page-manage-head">
        <input type="text" class="company-name-input" value="${esc(c.name)}" data-orig="${esc(c.name)}">
        <input type="text" class="company-domain-input" placeholder="Website domain, e.g. solarlux.com (for Google Ads)"
               value="${esc(c.website_domain || "")}" data-orig="${esc(c.website_domain || "")}">
        <button class="btn btn-sm save-btn" disabled>Save</button>
        <button class="btn btn-sm fetch-one-btn" ${STATE.fetch_running ? "disabled" : ""}
                title="Fetch only this company's data">Fetch</button>
        <button class="btn btn-sm del-btn">Delete</button>
      </div>
      <div class="status-label">${esc(c.status_label)}${
        c.website_domain ? ` · Google: ${esc(c.google_status || "not fetched yet")}` : ""}</div>
      <hr class="section-divider">`;

    if (c.pages.length) {
      html += c.pages.map(p => {
        const ev = p.evidence || {};
        const isGoogle = p.source === "google";
        let evLine = "";
        if (ev.method === "landing_url") evLine = `evidence: <code>${esc(ev.url)}</code> · utm "${esc(ev.utm_campaign)}"`;
        else if (ev.method === "name_search") evLine = `evidence: name search · similarity ${esc(ev.similarity)}`;
        else if (ev.method === "domain_lookup") evLine = `evidence: domain lookup · <code>${esc(ev.domain)}</code>`;
        const link = isGoogle
          ? `<a class="link" href="https://adstransparency.google.com/advertiser/${esc(p.page_id)}" target="_blank">Open Google Ads Transparency ↗</a>`
          : `<a class="link" href="${esc(fbPageUrl(p.page_id))}" target="_blank">Open Facebook page ↗</a>`;
        return `<div class="page-item">
          <div>
            <b>${esc(p.page_name || p.page_id)}</b>
            <span class="role-badge">${esc(isGoogle ? "google" : "meta")}</span>
            <span class="role-badge">${esc(p.role)}</span>
            <span class="role-badge">${esc(p.status_label)}</span>
            <div class="page-meta">${evLine}</div>
            <div class="page-meta">page id ${esc(p.page_id)} · ${link}</div>
          </div>
          <button class="btn btn-sm unlink-btn" data-pid="${p.id}">Unlink</button>
        </div>`;
      }).join("");
    } else {
      html += `<p class="hint">No pages linked yet — fetched on the next run, or link one below.</p>`;
    }

    if (c.candidates && c.candidates.length) {
      html += `<div class="detail-section-title" style="margin-top:14px">Candidates from the last name search</div>`;
      html += c.candidates.map((cand, i) => candidateRow(cid, cand, i)).join("");
    }

    html += `<hr class="section-divider">
      <div class="search-row">
        <input type="text" class="search-term-input" placeholder="Search term" value="${esc(searchTermCache[cid] || "")}">
        <button class="btn btn-sm search-btn">Search</button>
      </div>
      <div class="live-candidates"></div>
      <div class="manual-row">
        <input type="text" class="manual-pageid-input" placeholder="Add page by ID (view_all_page_id=…)">
        <select class="manual-role-select"><option value="main">main</option><option value="partner">partner</option></select>
        <button class="btn btn-sm link-manual-btn">Link</button>
      </div>`;

    box.innerHTML = html;

    const nameInput = $(".company-name-input", box);
    const domainInput = $(".company-domain-input", box);
    const saveBtn = $(".save-btn", box);
    const checkDirty = () => saveBtn.disabled =
      nameInput.value.trim() === nameInput.dataset.orig && domainInput.value.trim() === domainInput.dataset.orig;
    nameInput.addEventListener("input", checkDirty);
    domainInput.addEventListener("input", checkDirty);
    saveBtn.addEventListener("click", async () => {
      try {
        await api(`/api/companies/${cid}`, "PATCH",
          { name: nameInput.value.trim(), website_domain: domainInput.value.trim() || null });
        await loadState();
        await loadCustomers();
      } catch (e) { alert(e.message); }
    });
    $(".fetch-one-btn", box).addEventListener("click", () => startFetch(cid));
    $(".del-btn", box).addEventListener("click", async () => {
      if (!await appConfirm(`Delete “${c.name}” and all its collected data?`,
        { title: "Delete company", confirmText: "Delete", danger: true })) return;
      expandedPages.delete(cid);
      await api(`/api/companies/${cid}`, "DELETE");
      await loadState();
      await loadCustomers();
    });

    $$(".unlink-btn", box).forEach(btn => btn.addEventListener("click", async () => {
      await api(`/api/pages/${btn.dataset.pid}`, "DELETE"); await loadState();
    }));
    $$(".use-btn", box).forEach((btn, i) => btn.addEventListener("click", async () => {
      const cand = c.candidates[i];
      try { await api(`/api/companies/${cid}/confirm`, "POST",
        { page_id: cand.page_id, page_name: cand.name, category: cand.category }); await loadState(); }
      catch (e) { alert(e.message); }
    }));
    $(".search-btn", box).addEventListener("click", async () => {
      const term = $(".search-term-input", box).value.trim();
      const out = $(".live-candidates", box);
      out.innerHTML = `<p class="hint">Searching Ad Library… (one Apify call)</p>`;
      try {
        const res = await api(`/api/companies/${cid}/search`, "POST", { term });
        if (!res.candidates.length) {
          out.innerHTML = `<p class="hint">No pages found for "${esc(res.search_term)}". Try a shorter term or add the page id directly below.</p>`;
        } else {
          out.innerHTML = `<p class="hint">"${esc(res.search_term)}" — ${res.candidates.length} page(s):</p>`
            + res.candidates.map(cand => candidateRow(cid, cand)).join("");
          $$(".use-btn", out).forEach((btn, i) => btn.addEventListener("click", async () => {
            const cand = res.candidates[i];
            try { await api(`/api/companies/${cid}/confirm`, "POST",
              { page_id: cand.page_id, page_name: cand.name, category: cand.category }); await loadState(); }
            catch (e) { alert(e.message); }
          }));
        }
      } catch (e) { out.innerHTML = `<p class="hint">Search failed: ${esc(e.message)}</p>`; }
    });
    $(".link-manual-btn", box).addEventListener("click", async () => {
      const pageId = $(".manual-pageid-input", box).value.trim();
      const role = $(".manual-role-select", box).value;
      if (!pageId) return;
      try { await api(`/api/companies/${cid}/pages`, "POST", { page_id: pageId, role }); await loadState(); }
      catch (e) { alert(e.message); }
    });
  }

  // ------------------------------------------------------------------ fetch + progress
  function selectedSources() {
    const sources = [];
    if ($("#sourceMeta").checked) sources.push("meta");
    if ($("#sourceGoogle").checked) sources.push("google");
    return sources.length ? sources : ["meta"];
  }

  function startFetch(companyId, sources) {
    const sourcesList = sources || selectedSources();
    const body = { sources: sourcesList };
    if (companyId != null) body.company_id = companyId;
    api("/api/fetch", "POST", body).then(({ run_id }) => {
      STATE.fetch_running = true;
      renderTopbar();
      const panel = $("#fetchPanel"), bar = $("#progressFill"), log = $("#fetchLog"), status = $("#fetchStatus");
      panel.classList.remove("hidden"); log.innerHTML = ""; bar.style.width = "0%";
      const companyName = companyId != null ? (STATE.companies.find(c => c.id === companyId) || {}).name : null;
      const totalSources = sourcesList.length;
      status.textContent = companyName ? `Fetching ${companyName}…` : "Starting…";

      const es = new EventSource(`/api/fetch/stream/${run_id}`);
      const addLog = (text) => { const d = document.createElement("div"); d.textContent = text; log.appendChild(d); log.scrollTop = log.scrollHeight; };
      // Progress is shown as one combined bar across ALL selected sources, not
      // per-source — otherwise it'd visibly reset to 0% when Google starts
      // after Meta finishes, which reads as "it broke" rather than "next source".
      const setBar = (fractionWithinSource, srcIdx) =>
        bar.style.width = `${Math.max(0, Math.min(100, 100 * (srcIdx + fractionWithinSource) / totalSources))}%`;

      es.onmessage = (ev) => {
        const evt = JSON.parse(ev.data);
        const phase = evt.phase;
        const srcIdx = evt.source ? Math.max(0, sourcesList.indexOf(evt.source)) : 0;
        const tag = evt.source ? `[${evt.source}] ` : "";
        const srcCount = totalSources > 1 ? ` (source ${srcIdx + 1}/${totalSources})` : "";
        if (phase === "source_start") addLog(`— Starting ${evt.source} fetch${srcCount} —`);
        else if (phase === "source_done") addLog(`— ${evt.source} fetch done —`);
        else if (phase === "begin") status.textContent = `${tag}Fetching ${evt.total} ${evt.total === 1 ? "company" : "companies"} via ${evt.backend}${srcCount}…`;
        else if (phase === "company_start") {
          setBar((evt.i - 1) / Math.max(evt.total, 1), srcIdx);
          addLog(`${tag}→ ${evt.company} — resolving / fetching…`);
        } else if (phase === "company_done") {
          setBar(evt.i / Math.max(evt.total, 1), srcIdx);
          const extra = evt.page_name ? ` · ${evt.page_name}` : "";
          addLog(`${tag}${evt.status === "error" ? "✗" : "✓"} ${evt.company} — ${evt.status} · ${evt.ads} ads${extra}`);
        } else if (phase === "quota_exceeded") {
          setBar(1, srcIdx);
          status.innerHTML = `⚠ <b>Apify-Kontingent aufgebraucht</b> — Abruf gestoppt. `
            + `Bitte Kontingent/API-Key unter Einstellungen prüfen.`;
          status.classList.add("status-error");
          addLog(`${tag}✗ Apify quota/hard-limit reached — batch stopped at “${evt.company}”. `
            + `Remaining companies were not fetched.`);
          toast("Apify-Kontingent aufgebraucht — Abruf gestoppt. Kontingent/Key prüfen.", "error");
        } else if (phase === "sweep_start") {
          setBar(0.97, srcIdx);
          addLog(`${tag}→ Partner sweep — searching hub campaigns for partner accounts…`);
        } else if (phase === "sweep_done") {
          addLog(evt.error ? `${tag}✗ Partner sweep failed: ${evt.error}`
                            : `${tag}Partner sweep — ${evt.linked} page(s) newly linked, ${evt.attributed} ad(s) attributed`);
        } else if (phase === "result") {
          bar.style.width = "100%";
          if (evt.error) { status.textContent = `Run failed: ${evt.error}`; return; }
          const summaries = Object.values(evt.summary || {});
          const collected = summaries.reduce((n, s) => n + (s.collected || 0), 0);
          const companies = summaries.reduce((n, s) => n + (s.companies || 0), 0);
          const errors = summaries.reduce((n, s) => n + (s.errors || 0), 0);
          if (summaries.some(s => s.quota_exceeded)) {
            status.innerHTML = `⚠ <b>Apify-Kontingent aufgebraucht</b> — Abruf vorzeitig gestoppt `
              + `(${collected}/${companies} abgerufen). Kontingent/API-Key prüfen.`;
            status.classList.add("status-error");
          } else {
            status.classList.remove("status-error");
            status.textContent = `Done · ${collected}/${companies} collected` + (errors ? ` · ${errors} error(s)` : "");
          }
        }
      };
      es.addEventListener("done", async () => {
        es.close();
        STATE.fetch_running = false;
        await loadState();
        setTimeout(() => panel.classList.add("hidden"), 2500);
      });
      es.onerror = async () => {
        es.close();
        STATE.fetch_running = false;
        await loadState();
      };
    }).catch(e => alert(e.message));
  }

  // ------------------------------------------------------------------ identity status labels + page cell (shared by filter + drawer)
  const ID_STATUS_ORDER = ["locked", "confirmed", "ambiguous", "no_ads_found", "pending"];
  const ID_STATUS_LABEL = { locked: "Meta page locked", confirmed: "Meta page found", ambiguous: "Meta page unclear",
    no_ads_found: "No Meta page found", pending: "Meta not checked" };
  // The identity-status filter dropdown also carries two PSEUDO options that
  // filter on fetch-readiness (numeric page id present) rather than status —
  // they map to the separate page_id_state filter, never to resolution_status.
  const ID_FILTER_OPTIONS = [...ID_STATUS_ORDER, "with_id", "without_id"];
  const ID_FILTER_LABEL = { ...ID_STATUS_LABEL, with_id: "With Meta page ID", without_id: "Without Meta page ID" };

  function idFbCell(r) {
    // Prefer the explicit page_url (serper handle-only confirms have no numeric
    // id yet); fall back to the numeric-id permalink; else nothing.
    const href = r.page_url || (r.page_id ? fbPageUrl(r.page_id) : null);
    if (!href) return `<span class="muted">—</span>`;
    const label = r.page_name || r.page_id || r.page_url;
    const isIG = href.includes("instagram.com");
    const platform = isIG ? ` <span class="role-badge" title="Instagram profile — same Meta ad identity as a Facebook page">Instagram</span>` : "";
    const needsId = !r.page_id && (r.resolution_status === "confirmed")
      ? ` <span class="candidate-flag flag-warn" title="Meta page known, but the numeric page ID needed for Ad lookup isn't captured yet — open the page and lock it with the ID, or Ad lookup will resolve it.">⚠ no page id</span>`
      : "";
    return `<a class="link" href="${esc(href)}" target="_blank">${esc(label)}</a>${platform}${needsId}`;
  }

  // ------------------------------------------------------------------ wiring
  function wireStatic() {
    $("#fetchBtn").addEventListener("click", () => startFetch());

    // Collapsible sidebar: icon-only mode, remembered across sessions. Tab
    // labels become tooltips so the icons stay self-explanatory.
    const applyNavCollapsed = (on) => {
      document.body.classList.toggle("nav-collapsed", on);
      $("#navToggle").textContent = on ? "»" : "«";
      try { localStorage.setItem("navCollapsed", on ? "1" : ""); } catch { /* private mode */ }
    };
    $$(".tab").forEach(t => { t.title = t.textContent.trim(); });
    $("#navToggle").addEventListener("click", () =>
      applyNavCollapsed(!document.body.classList.contains("nav-collapsed")));
    try { if (localStorage.getItem("navCollapsed") === "1") applyNavCollapsed(true); } catch { }

    function showTab(name) {
      const btn = $$(".tab").find(t => t.dataset.tab === name);
      if (!btn) return;
      $$(".tab").forEach(t => t.classList.toggle("active", t === btn));
      $$(".tab-panel").forEach(p => p.classList.toggle("active", p.id === `tab-${name}`));
      if (name === "customers") ensureCustomersLoaded();
      if (name === "chancen") ensureChancenLoaded();
      if (name === "pruefen") ensurePruefenLoaded();
      if (name === "objekte") ensureObjekteLoaded();
      if (name === "profil") loadIcpStatus();
      if (name === "reports") {
        // If a Companies filter is active, default the report to use it — so
        // "generate a report for the filtered companies" just works without
        // having to remember the checkbox.
        if (activeFilterSummary(currentCustomerFilters()).length) $("#reportUseFilter").checked = true;
        updateReportFilterHint();
      }
      if (name === "logs") ensureLogsLoaded();
      if (name === "settings") ensureSettingsLoaded();
    }
    $$(".tab").forEach(tab => tab.addEventListener("click", () => {
      localStorage.setItem("adwatch.activeTab", tab.dataset.tab);  // survive a refresh
      showTab(tab.dataset.tab);
    }));
    wireCustomers();
    wireCompanyTableControls();
    wireChancen();
    wirePruefen();
    wireObjekte();
    wireLogsTabControls();
    $$(".settings-save-btn").forEach(b => b.addEventListener("click", saveSettings));

    // restore the last-open tab after a refresh (default: dashboard) — AFTER
    // the wire* calls above, since showTab("customers") loads the table, which
    // reads the filter dropdowns that wireCustomers() mounts
    const savedTab = localStorage.getItem("adwatch.activeTab");
    if (savedTab && savedTab !== "dashboard") showTab(savedTab);

    $("#addCompanyForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = $("#newCompanyName");
      const name = input.value.trim();
      if (!name) return;
      try { await api("/api/companies", "POST", { name }); input.value = ""; await loadState(); }
      catch (e) { alert(e.message); }
    });

    $("#addRecipientForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const emailInput = $("#newRecipientEmail"), nameInput = $("#newRecipientName");
      const email = emailInput.value.trim();
      if (!email) return;
      try {
        await api("/api/recipients", "POST", { email, name: nameInput.value.trim() || null });
        emailInput.value = ""; nameInput.value = "";
        await loadReports();
      } catch (e) { alert(e.message); }
    });

    $("#reportUseFilter").addEventListener("change", updateReportFilterHint);

    $("#generateReportBtn").addEventListener("click", async () => {
      const btn = $("#generateReportBtn");
      btn.disabled = true; btn.textContent = "Generating…";
      try {
        const useFilter = $("#reportUseFilter").checked;
        await api("/api/reports/generate", "POST", {
          report: $("#reportTypeSelect").value,
          filters: useFilter ? currentCustomerFilters() : null,
        });
        await loadReports();
      } catch (e) { alert(e.message); }
      finally { btn.disabled = false; btn.textContent = "Generate"; }
    });

    fillDaySelect($("#fetchDay"));
    fillDaySelect($("#sendDay"));
    $("#saveScheduleBtn").addEventListener("click", async () => {
      const btn = $("#saveScheduleBtn");
      btn.disabled = true; btn.textContent = "Saving…";
      try {
        const fetchSources = [];
        if ($("#fetchSourceMeta").checked) fetchSources.push("meta");
        if ($("#fetchSourceGoogle").checked) fetchSources.push("google");
        await api("/api/schedule", "PUT", {
          fetch_enabled: $("#fetchEnabled").checked,
          fetch_day: Number($("#fetchDay").value),
          fetch_time: $("#fetchTime").value,
          fetch_sources: fetchSources.length ? fetchSources : ["meta"],
          send_enabled: $("#sendEnabled").checked,
          send_day: Number($("#sendDay").value),
          send_time: $("#sendTime").value,
          send_report: $("#sendReportType").value,
        });
        await loadSchedule();
      } catch (e) { alert(e.message); }
      finally { btn.disabled = false; btn.textContent = "Save schedule"; }
    });
  }

  const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  function fillDaySelect(sel) {
    sel.innerHTML = DAY_NAMES.map((d, i) => `<option value="${i}">${d}</option>`).join("");
  }

  async function loadSchedule() {
    const s = await api("/api/schedule");
    $("#fetchEnabled").checked = s.fetch_enabled;
    $("#fetchDay").value = s.fetch_day;
    $("#fetchTime").value = s.fetch_time;
    const fetchSources = s.fetch_sources || ["meta"];
    $("#fetchSourceMeta").checked = fetchSources.includes("meta");
    $("#fetchSourceGoogle").checked = fetchSources.includes("google");
    $("#sendEnabled").checked = s.send_enabled;
    $("#sendDay").value = s.send_day;
    $("#sendTime").value = s.send_time;
    $("#sendReportType").value = s.send_report;
    const next = s.next_run || {};
    const fmt = (iso) => iso ? new Date(iso).toLocaleString("de-DE") : "not scheduled";
    $("#scheduleNextRun").textContent = `Next fetch: ${fmt(next.fetch)} · Next send: ${fmt(next.send)}`;
  }

  // ------------------------------------------------------------------ Reports tab
  let REPORTS_STATE = null;
  // The tick state lives on the recipient row (ReportRecipient.preselected) and
  // is PATCHed on every change — it used to be a JS Set that died on reload, so
  // an unticked colleague silently came back next time the tab was opened.
  // A newly added recipient still starts ticked (the column defaults to true).

  function fmtSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    return `${(bytes / 1024).toFixed(1)} KB`;
  }

  async function loadReports() {
    REPORTS_STATE = await api("/api/reports");
    renderRecipients();
    renderReportsTable();
  }

  function renderRecipients() {
    const box = $("#recipientsList");
    const recipients = REPORTS_STATE.recipients;
    if (!recipients.length) {
      box.innerHTML = `<p class="hint">No recipients yet — add one below.</p>`;
      return;
    }
    box.innerHTML = recipients.map(r => `
      <div class="page-item">
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
          <input type="checkbox" class="recipient-check" data-rid="${r.id}" ${r.preselected === false ? "" : "checked"}>
          <span><b>${esc(r.name || r.email)}</b>${r.name ? ` <span class="muted">${esc(r.email)}</span>` : ""}</span>
        </label>
        <button class="btn btn-sm del-recipient-btn" data-rid="${r.id}">Remove</button>
      </div>`).join("");
    $$(".recipient-check", box).forEach(cb => cb.addEventListener("change", async () => {
      const rid = Number(cb.dataset.rid);
      const on = cb.checked;
      cb.disabled = true;
      try {
        await api(`/api/recipients/${rid}`, "PATCH", { preselected: on });
        // keep the cached copy in step so the other send boxes agree without a reload
        const row = (REPORTS_STATE.recipients || []).find(x => x.id === rid);
        if (row) row.preselected = on;
        if (SAVED_STATE && SAVED_STATE.recipients) {
          const r2 = SAVED_STATE.recipients.find(x => x.id === rid);
          if (r2) r2.preselected = on;
        }
      } catch (e) {
        cb.checked = !on;                 // the choice didn't persist — don't pretend it did
        toast(`Auswahl konnte nicht gespeichert werden: ${e.message || e}`, "error");
      } finally {
        cb.disabled = false;
      }
    }));
    $$(".del-recipient-btn", box).forEach(btn => btn.addEventListener("click", async () => {
      const rid = Number(btn.dataset.rid);
      await api(`/api/recipients/${rid}`, "DELETE");
      await loadReports();
    }));
  }

  function renderReportsTable() {
    const reports = REPORTS_STATE.reports;
    $("#reportsEmptyHint").classList.toggle("hidden", reports.length > 0);
    $("#reportsTableBody").innerHTML = reports.map(r => {
      // ② make filtered/saved reports identifiable instead of an anonymous "Report — KW30".
      const tag = r.definition ? `<span class="tag tag-saved">gespeichert</span>`
                : (r.filter_label ? `<span class="tag tag-filtered">gefiltert</span>` : "");
      const scope = r.definition ? `„${esc(r.definition)}"` : (r.filter_label ? esc(r.filter_label) : "");
      return `
      <tr data-filename="${esc(r.filename)}">
        <td>${esc(r.label)} ${tag}${scope ? `<div class="muted small">${scope}</div>` : ""}</td>
        <td>${esc(r.created_at)}</td>
        <td class="num">${fmtSize(r.size_bytes)}</td>
        <td style="white-space:nowrap">
          <a class="btn btn-sm" href="/api/reports/${encodeURIComponent(r.filename)}" target="_blank">Download</a>
          <button class="btn btn-sm send-report-btn">Send</button>
        </td>
      </tr>`;
    }).join("");
    $$(".send-report-btn", $("#reportsTableBody")).forEach(btn => {
      btn.addEventListener("click", async () => {
        const filename = btn.closest("tr").dataset.filename;
        const recipient_ids = REPORTS_STATE.recipients
          .filter(r => !uncheckedRecipients.has(r.id))
          .map(r => r.id);
        if (!recipient_ids.length && !REPORTS_STATE.recipients.length) {
          alert("Add a recipient first.");
          return;
        }
        if (!recipient_ids.length) { alert("Check at least one recipient above."); return; }
        btn.disabled = true; btn.textContent = "Sending…";
        try {
          await api(`/api/reports/${encodeURIComponent(filename)}/send-email`, "POST", { recipient_ids });
          btn.textContent = "Sent ✓";
          setTimeout(() => { btn.textContent = "Send"; btn.disabled = false; }, 2500);
        } catch (e) {
          alert(`Send failed: ${e.message}`);
          btn.textContent = "Send"; btn.disabled = false;
        }
      });
    });
  }

  // ---------------- Saved reports + inline send (Companies-tab report flow) ----------
  // These let a filtered report be emailed right after generating (no tab hop),
  // and let a filter be saved as a named, re-runnable, optionally-scheduled report.
  let SAVED_STATE = null;        // { definitions, recipients } from /api/report-defs
  let LAST_REPORT = null;        // { filename, filters, reportType } of the just-generated report
  let SAVE_FILTERS = null;       // the filter blob currently being saved
  let EDIT_DEF_ID = null;        // non-null while editing an existing definition

  async function ensureRecipients() {
    if (!SAVED_STATE) { try { await loadSavedReports(); } catch { SAVED_STATE = { definitions: [], recipients: [] }; } }
    return (SAVED_STATE && SAVED_STATE.recipients) || [];
  }

  // Human-readable scope for a saved/loaded filter (reuses the Explorer summary,
  // plus the explicit-selection case the summary doesn't cover).
  function describeScope(filters) {
    if (!filters || !Object.keys(filters).length) return "";
    if (filters.ids && filters.ids.length) return `${filters.ids.length} ausgewählte Firmen`;
    return activeFilterSummary(filters).join(" · ");
  }

  // `preselected` is an explicit id list (a saved definition's recipients).
  // Without one, fall back to the remembered per-recipient tick state instead of
  // ticking everybody, so the inline send and the pipeline panel agree with the
  // choice made in the Reports tab.
  function renderRecipientChecks(container, recipients, preselected) {
    const pre = new Set(preselected != null ? preselected
                        : recipients.filter(r => r.active && r.preselected !== false).map(r => r.id));
    if (!recipients.length) {
      container.innerHTML = `<p class="hint">Keine Empfänger — im Reports-Tab unter „Recipients" anlegen.</p>`;
      return;
    }
    container.innerHTML = recipients.map(r => `
      <label class="recipient-check-row">
        <input type="checkbox" class="rc-check" data-rid="${r.id}" ${pre.has(r.id) ? "checked" : ""}>
        <span>${esc(r.name || r.email)}${r.name ? ` <span class="muted">${esc(r.email)}</span>` : ""}${r.active ? "" : ` <span class="muted">(inaktiv)</span>`}</span>
      </label>`).join("");
  }

  function checkedRecipientIds(container) {
    return [...container.querySelectorAll(".rc-check:checked")].map(cb => Number(cb.dataset.rid));
  }

  function hideCompanyPanels() {
    ["#fetchPlanPanel", "#reportReadyPanel", "#saveReportPanel"].forEach(s => { const el = $(s); if (el) el.classList.add("hidden"); });
  }

  // ① After a report is generated (Companies tab): show download + inline send.
  async function showReportReadyPanel(filename, infoText) {
    const recipients = await ensureRecipients();
    $("#reportReadyInfo").textContent = infoText || "";
    $("#reportReadyDownload").href = `/api/reports/${encodeURIComponent(filename)}`;
    renderRecipientChecks($("#reportReadyRecipients"), recipients);
    // "Save as report" only makes sense for a reusable filter, not a one-off id selection.
    const savable = !(LAST_REPORT && LAST_REPORT.filters && LAST_REPORT.filters.ids);
    $("#reportReadySaveBtn").classList.toggle("hidden", !savable);
    hideCompanyPanels();
    const p = $("#reportReadyPanel");
    p.classList.remove("hidden");
    p.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function sendReportReady() {
    if (!LAST_REPORT) return;
    const ids = checkedRecipientIds($("#reportReadyRecipients"));
    if (!ids.length) { toast("Mindestens einen Empfänger auswählen.", "error"); return; }
    const btn = $("#reportReadySendBtn"); btn.disabled = true; btn.textContent = "Senden…";
    try {
      await api(`/api/reports/${encodeURIComponent(LAST_REPORT.filename)}/send-email`, "POST", { recipient_ids: ids });
      toast(`✓ Bericht an ${ids.length} Empfänger gesendet.`, "info");
      $("#reportReadyPanel").classList.add("hidden");
    } catch (e) {
      toast(`Senden fehlgeschlagen: ${e.message}`, "error");
    } finally { btn.disabled = false; btn.textContent = "Senden"; }
  }

  // ③ Save-as-report panel (create from current filter, or edit an existing def).
  async function openSaveReportPanel({ filters, reportType, def } = {}) {
    const recipients = await ensureRecipients();
    EDIT_DEF_ID = def ? def.id : null;
    SAVE_FILTERS = def ? (def.filters || {}) : (filters || currentCustomerFilters());
    $("#saveReportTitle").textContent = def ? "Bericht bearbeiten" : "Als wiederkehrenden Bericht speichern";
    $("#saveReportName").value = def ? def.name : "";
    $("#saveReportType").value = def ? def.report_type : (reportType || "full");
    $("#saveReportScope").textContent = "Umfang: " + (describeScope(SAVE_FILTERS) || "alle Firmen");
    renderRecipientChecks($("#saveReportRecipients"), recipients, def ? def.recipient_ids : undefined);
    $("#saveReportSchedule").checked = def ? def.schedule_enabled : false;
    $("#saveReportDay").value = def ? def.schedule_day : 0;
    $("#saveReportTime").value = def ? def.schedule_time : "07:00";
    // the panel lives in the Companies (customers) tab — make sure it's showing
    // (matters when editing from the Reports tab). Clicking the tab button reuses
    // the app's own tab-switch logic.
    const custTab = $$(".tab").find(t => t.dataset.tab === "customers");
    if (custTab && !custTab.classList.contains("active")) custTab.click();
    hideCompanyPanels();
    const p = $("#saveReportPanel");
    p.classList.remove("hidden");
    p.scrollIntoView({ behavior: "smooth", block: "nearest" });
    $("#saveReportName").focus();
  }

  async function saveReportDef() {
    const name = $("#saveReportName").value.trim();
    if (!name) { toast("Bitte einen Namen eingeben.", "error"); return; }
    const payload = {
      name,
      report_type: $("#saveReportType").value,
      filters: SAVE_FILTERS || {},
      recipient_ids: checkedRecipientIds($("#saveReportRecipients")),
      schedule_enabled: $("#saveReportSchedule").checked,
      schedule_day: Number($("#saveReportDay").value),
      schedule_time: $("#saveReportTime").value || "07:00",
    };
    const btn = $("#saveReportSaveBtn"); btn.disabled = true; btn.textContent = "Speichern…";
    try {
      if (EDIT_DEF_ID) await api(`/api/report-defs/${EDIT_DEF_ID}`, "PUT", payload);
      else await api("/api/report-defs", "POST", payload);
      toast(`✓ Bericht „${name}" gespeichert${payload.schedule_enabled ? " (mit Zeitplan)" : ""}.`, "info");
      $("#saveReportPanel").classList.add("hidden");
      await loadSavedReports();
    } catch (e) {
      toast(`Speichern fehlgeschlagen: ${e.message}`, "error");
    } finally { btn.disabled = false; btn.textContent = "Speichern"; }
  }

  async function loadSavedReports() {
    SAVED_STATE = await api("/api/report-defs");
    renderSavedReports();
  }

  function renderSavedReports() {
    const defs = (SAVED_STATE && SAVED_STATE.definitions) || [];
    const recips = (SAVED_STATE && SAVED_STATE.recipients) || [];
    const byId = Object.fromEntries(recips.map(r => [r.id, r]));
    const empty = $("#savedReportsEmpty"), list = $("#savedReportsList");
    if (empty) empty.classList.toggle("hidden", defs.length > 0);
    if (!list) return;
    list.innerHTML = defs.map(d => {
      const rnames = (d.recipient_ids || []).map(id => byId[id] ? (byId[id].name || byId[id].email) : `#${id}`).join(", ") || "—";
      const sched = d.schedule_enabled ? `${DAY_NAMES[d.schedule_day]} ${d.schedule_time}` : "manuell";
      const scope = describeScope(d.filters) || "alle Firmen";
      return `<div class="saved-report" data-id="${d.id}">
        <div class="saved-report-main">
          <b>${esc(d.name)}</b> <span class="muted">· ${d.report_type === "full" ? "Voll" : "Top 5"} · ${esc(scope)}</span>
          <div class="muted small">Empfänger: ${esc(rnames)} · Zeitplan: ${esc(sched)}${d.last_status ? ` · zuletzt: ${esc(d.last_status)}` : ""}</div>
        </div>
        <div class="saved-report-actions">
          <button class="btn btn-sm sr-run" title="Jetzt erstellen und an die Empfänger senden">▶ Jetzt senden</button>
          <button class="btn btn-sm sr-edit">Bearbeiten</button>
          <button class="btn btn-sm btn-danger sr-del">Löschen</button>
        </div>
      </div>`;
    }).join("");
    $$(".saved-report", list).forEach(row => {
      const id = Number(row.dataset.id);
      const def = defs.find(d => d.id === id);
      $(".sr-run", row).addEventListener("click", async (e) => {
        const btn = e.currentTarget; btn.disabled = true; btn.textContent = "Senden…";
        try {
          const res = await api(`/api/report-defs/${id}/run`, "POST", { send: true });
          toast(res.sent ? `✓ „${def.name}" an ${res.recipients.length} gesendet.`
                         : `Bericht erstellt, nicht gesendet (${res.reason || "keine Empfänger"}).`,
                res.sent ? "info" : "error");
          await loadReports();
        } catch (err) { toast(`Fehlgeschlagen: ${err.message}`, "error"); }
        finally { btn.disabled = false; btn.textContent = "▶ Jetzt senden"; await loadSavedReports(); }
      });
      $(".sr-edit", row).addEventListener("click", () => openSaveReportPanel({ def }));
      $(".sr-del", row).addEventListener("click", async () => {
        if (!confirm(`Bericht „${def.name}" löschen?`)) return;
        await api(`/api/report-defs/${id}`, "DELETE");
        toast("Bericht gelöscht.", "info");
        await loadSavedReports();
      });
    });
  }

  // ------------------------------------------------------- Chancen tab
  // Customers quiet against their OWN order rhythm. The health labels come
  // straight from insights/rfm.py — kept identical so a value in the table can
  // always be traced to the classification that produced it.
  // Millions/thousands short form. A full "€27.567.067" overflows a KPI card and
  // gets clipped to "€27.567.06", which reads as a completely different number.
  const eurShort = (v) => {
    if (v == null) return "—";
    const n = Math.abs(v);
    if (n >= 1e6) return "€" + (v / 1e6).toLocaleString("de-DE", { maximumFractionDigits: 1 }) + " Mio.";
    if (n >= 1e4) return "€" + Math.round(v / 1e3).toLocaleString("de-DE") + " Tsd.";
    return eur(v);
  };
  const HEALTH_LABEL = {
    aktiv: "aktiv", beobachten: "beobachten", "gefährdet": "gefährdet",
    verloren: "verloren", einmalig: "nur Kleinteile", nie: "nie gekauft",
  };
  let chancenLoaded = false;

  async function loadChancen() {
    const wrap = $("#chancenTableWrap");
    const adsOnly = $("#chancenAdsOnly").checked;
    const minValue = Number($("#chancenMinValue").value || 0);
    wrap.innerHTML = `<p class="muted" style="padding:12px">Wird geladen …</p>`;
    let data;
    try {
      data = await api(`/api/chancen?limit=500&min_value=${minValue}`
        + `&advertising_only=${adsOnly ? "true" : "false"}`);
    } catch (e) {
      wrap.innerHTML = `<p class="muted" style="padding:12px">Konnte nicht geladen werden: ${esc(e.message)}</p>`;
      return;
    }
    TABLE_TOTALS["chancenTableWrap"] = data.total;
    const rows = data.rows || [];
    const s = data.summary || {};
    const risk = (s["gefährdet"]?.companies || 0) + (s.verloren?.companies || 0);
    const riskEur = (s["gefährdet"]?.value || 0) + (s.verloren?.value || 0);
    $("#chancenSummary").innerHTML = `
      <div class="kpi"><div class="kpi-label">Überfällig / verloren</div>
        <div class="kpi-value">${risk.toLocaleString("de-DE")}</div></div>
      <div class="kpi"><div class="kpi-label">Umsatz historisch</div>
        <div class="kpi-value" title="${eur(riskEur)}">${eurShort(riskEur)}</div></div>
      <div class="kpi"><div class="kpi-label">Davon mit Werbung</div>
        <div class="kpi-value">${(data.advertising || 0).toLocaleString("de-DE")}</div></div>
      <div class="kpi"><div class="kpi-label">Aktive Kunden</div>
        <div class="kpi-value">${(s.aktiv?.companies || 0).toLocaleString("de-DE")}</div></div>`;
    if (!rows.length) {
      wrap.innerHTML = `<p class="muted" style="padding:12px">Keine Treffer für diesen Filter.</p>`;
      return;
    }
    wrap.innerHTML = `
      <table class="data-table">
        <thead><tr>
          <th>Firma</th><th>Segment / Untersegment</th><th>Land</th>
          <th class="num">Umsatz</th><th class="num">Bestellungen</th>
          <th class="num">Rhythmus</th><th class="num">still seit</th>
          <th class="num">überfällig</th><th>Status</th><th>Werbung</th>
        </tr></thead>
        <tbody>${rows.map(r => `
          <tr class="clickable" data-company="${r.company_id}">
            <td>${esc(r.name)}${r.city ? `<div class="sub">${esc(r.city)}</div>` : ""}</td>
            <td>${esc(r.segment || "—")}${r.sub_segment ? `<div class="sub">${esc(r.sub_segment)}</div>` : ""}</td>
            <td>${esc(r.country || "—")}</td>
            <td class="num">${eur(r.value)}</td>
            <td class="num">${r.events}${r.material_events !== r.events
              ? `<div class="sub">${r.material_events} materiell</div>` : ""}</td>
            <td class="num">${r.cadence_days ? Math.round(r.cadence_days) + " T" : "—"}</td>
            <td class="num">${r.days_since != null ? r.days_since + " T" : "—"}</td>
            <td class="num">${r.overdue_factor ? r.overdue_factor.toFixed(1) + "×" : "—"}</td>
            <td><span class="state-chip">${esc(HEALTH_LABEL[r.health] || r.health)}</span></td>
            <td>${r.advertising ? "✓" : ""}</td>
          </tr>`).join("")}</tbody>
      </table>`;
    $$("#chancenTableWrap tr.clickable").forEach(tr =>
      tr.addEventListener("click", () => openCompanyDrawer(Number(tr.dataset.company))));
  }

  function ensureChancenLoaded() {
    if (chancenLoaded) return;
    chancenLoaded = true;
    loadChancen();
  }

  function wireChancen() {
    const reload = $("#chancenReload");
    if (!reload) return;
    reload.addEventListener("click", loadChancen);
    $("#chancenAdsOnly").addEventListener("change", loadChancen);
    $("#chancenMinValue").addEventListener("change", loadChancen);
  }

  // Das Dossier — Kurzprofil, Belege-Historie, jede VC in jeder ROLLE, Objekte.
  const ROLE_LABEL = { kaeufer: "Als Käufer", architekt: "Als Architekt", endkunde: "Als Endkunde" };

  function dossierSection(d) {
    if (!d) return "";
    const kv = (o) => Object.entries(o || {}).map(([k, v]) => `${esc(k)} (${v})`).join(" · ");
    let html = `<div class="drawer-section"><h3>Dossier</h3>`;
    if (d.kurzprofil) html += `<p style="font-size:13px;line-height:1.55">${esc(d.kurzprofil)}</p>`;
    const b = d.belege || {};
    if (b.events) {
      const years = Object.entries(b.by_year || {})
        .map(([y, v]) => `${y}: ${eurShort(v)}`).join(" · ");
      html += `<div style="font-size:12.5px;margin-top:6px">
        <b>Belege:</b> ${b.events} Bestellungen · ${eurShort(b.total)} · ${esc(b.first)} → ${esc(b.last)}
        <div class="sub">${esc(years)}</div></div>`;
    }
    // What this company asks Solarlux for. The euros are QUOTED across won AND
    // lost deals, so the label says "angefragt" — a dealer who asks for €2 Mio
    // and buys €200k is a different conversation from one who asks for €200k.
    const pr = d.produkte;
    if (pr && pr.families?.length) {
      const max = Math.max(...pr.families.map(f => f.value || 0), 1);
      html += `<div style="margin-top:12px;font-size:12.5px">
        <b>Produkte</b> — ${pr.positions} Positionen${pr.value_quoted ? ` · ${eurShort(pr.value_quoted)} angefragt` : ""}
        ${pr.first ? `<span class="sub"> · ${esc(pr.first)} → ${esc(pr.last)}</span>` : ""}
        <div style="display:flex;flex-direction:column;gap:3px;margin-top:6px">
          ${pr.families.map(f => `
            <div style="display:grid;grid-template-columns:1fr 62px 74px;gap:8px;align-items:center">
              <div style="min-width:0">
                <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.family)}</div>
                <div style="height:3px;border-radius:2px;background:var(--accent);opacity:.55;
                            width:${Math.max(2, Math.round(100 * (f.value || 0) / max))}%"></div>
              </div>
              <span class="sub" style="text-align:right">${f.positions ? f.positions + " Pos." : "—"}</span>
              <span class="sub" style="text-align:right">${f.value ? eurShort(f.value) : "—"}</span>
            </div>`).join("")}
        </div>
        <div class="sub" style="margin-top:4px">Werte sind <b>angefragt</b> (gewonnene und verlorene
          Verkaufschancen), nicht fakturierter Umsatz.</div>
      </div>`;
    }
    for (const [role, blk] of Object.entries(d.rollen || {})) {
      html += `<div style="margin-top:10px;font-size:12.5px">
        <b>${ROLE_LABEL[role] || role}:</b> ${blk.vcs} Verkaufschancen —
        ${blk.won} gewonnen${blk.win_rate != null ? ` (${Math.round(blk.win_rate * 100)} %)` : ""},
        ${blk.open} offen${blk.invoiced_value ? ` · fakturiert ${eurShort(blk.invoiced_value)}` : ""}${blk.quoted_value ? ` · angeboten ${eurShort(blk.quoted_value)}` : ""}
        ${Object.keys(blk.building_types || {}).length ? `<div class="sub">Gebäude: ${kv(blk.building_types)}</div>` : ""}
        ${Object.keys(blk.origins || {}).length ? `<div class="sub">Herkunft: ${kv(blk.origins)}</div>` : ""}
        ${Object.keys(blk.lost_reasons || {}).length ? `<div class="sub">Verluste: ${kv(blk.lost_reasons)}</div>` : ""}
      </div>`;
    }
    const pj = d.projekte || [];
    if (pj.length) {
      html += `<div style="margin-top:10px;font-size:12.5px"><b>Objekte (${pj.length}):</b>
        ${pj.slice(0, 6).map(p => `<div class="sub">• ${esc((p.name || "").slice(0, 60))} —
          ${esc(p.status)}, ${p.members} VC${p.members > 1 ? "s" : ""}${p.type_of_use ? `, ${esc(p.type_of_use)}` : ""}${p.value ? `, ${eurShort(p.value)}` : ""}</div>`).join("")}
      </div>`;
    }
    return html + `</div>`;
  }

  // CRM / Belege / Anreicherung im Drawer — every column the DB holds, visible.
  // Nulls are shown as '—' rather than hidden: "nicht angegeben" is information.
  function crmSection(c) {
    if (!c) return "";
    const j = (v) => Array.isArray(v) ? v.join(", ") : (v ?? null);
    const yn = (v) => v === true || v === 1 ? "ja" : v === false || v === 0 ? "nein" : null;
    const dt2 = (v) => v ? String(v).slice(0, 10) : null;
    const money = (v) => v != null ? eur(v) : null;
    const rows = [
      ["CRM-ID", c.crm_id], ["SAP-Nr.", c.sap_number], ["Quelle", c.lead_source || (c.crm_id ? "CRM" : "manuell")],
      ["Import-Typ", c.import_type], ["Kundenstatus", c.customer_state], ["Gesundheit", c.health],
      ["Belege (Anzahl)", c.beleg_count], ["Belege (Umsatz)", money(c.beleg_sum)],
      ["Erster / letzter Beleg", [dt2(c.beleg_first), dt2(c.beleg_last)].filter(Boolean).join(" → ") || null],
      ["Ø Grundrabatt", c.avg_discount != null ? c.avg_discount + " %" : null],
      ["Angebote (Anzahl)", c.quote_count], ["Angebote (Summe)", money(c.quote_sum)],
      ["Angebots-Konversion", c.conversion_rate != null ? Math.round(c.conversion_rate * 100) + " %" : null],
      ["Rückgewinnungs-Score", c.winback_score],
      ["Architekt: Projekte / gewonnen", c.arch_projects ? `${c.arch_projects} / ${c.arch_won} (${money(c.arch_won_value) || "€0"})` : null],
      ["Website geprüft", c.identity_status ? `${c.identity_status}${c.identity_matched_by ? " (" + c.identity_matched_by + ")" : ""}` : null],
      ["Profil", c.enrich_profile === "architekt" ? "Architekt/Planer" : (c.enrich_profile ? "Betrieb" : null)],
      ["Solarlux-Relevanz", c.solarlux_relevance], ["Bürotyp", c.office_type],
      ["Solarlux-Passung", c.solarlux_fit], ["Vertragspartner von", j(c.partner_of)],
      ["Montiert selbst", yn(c.installs)],
      ["Entscheidungsrolle", c.decision_role], ["Referenz-Umfang", c.reference_scale],
      ["Rechtsform", c.legal_form], ["Positionierung", c.positioning],
      ["Eigene Fertigung", yn(c.own_fabrication)], ["Showroom", yn(c.has_showroom)],
      ["Projektfokus", j(c.project_focus)], ["Zertifikate", j(c.certifications)],
      ["Fremdmarken", j(c.competitor_brands)], ["Erwähnt Solarlux", yn(c.mentions_solarlux)],
      ["Einsatzgebiet", c.service_area], ["Seitensprache", c.site_language],
      ["Notiz", c.notes],
      ["Konzerngesellschaft", yn(c.is_intercompany)],
      ["Fit-Begründung", c.fit_breakdown && typeof c.fit_breakdown === "object"
        ? Object.entries(c.fit_breakdown).map(([k, v]) => `${k}: ${v}`).join(" · ")
        : c.fit_breakdown],
      ["Identitäts-Beleg", c.identity_evidence?.signals
        ? Object.entries(c.identity_evidence.signals).filter(([, v]) => v).map(([k]) => k).join(", ") || "kein Signal"
        : null],
      ["Importiert / CRM-Sync", [dt2(c.imported_at), dt2(c.crm_synced_at)].filter(Boolean).join(" · ") || null],
      ["Identität geprüft", dt2(c.identity_checked_at)],
      ["Facebook", c.facebook_url ? `<a class="link" target="_blank" href="${esc(c.facebook_url)}">Profil ↗</a>` : null],
      ["Instagram", c.instagram_url ? `<a class="link" target="_blank" href="${esc(c.instagram_url)}">Profil ↗</a>` : null],
      ["LinkedIn", c.linkedin_url ? `<a class="link" target="_blank" href="${esc(c.linkedin_url)}">Profil ↗</a>` : null],
    ];
    const cells = rows.map(([k, v]) => `
      <div style="display:flex;gap:8px;padding:3px 0;border-bottom:1px solid var(--border)">
        <span class="muted" style="flex:0 0 46%">${k}</span>
        <span style="flex:1">${v === null || v === undefined || v === "" ? "—" : (String(v).startsWith("<a") ? v : esc(String(v)))}</span>
      </div>`).join("");
    const assess = c.assessment ? `<p class="hint" style="margin-top:8px">${esc(c.assessment)}</p>` : "";
    return `
      <div class="drawer-section">
        <h3>CRM &amp; Anreicherung — alle Felder</h3>
        <div style="font-size:12.5px">${cells}</div>
        ${assess}
      </div>`;
  }

  // ------------------------------------------------------- Objekte tab
  // Projects instead of loose Verkaufschancen — one win makes a project won.
  let objekteLoaded = false;

  async function loadObjekte() {
    const wrap = $("#objekteWrap");
    const st = $("#objekteStatus").value;
    const mm = $("#objekteMulti").checked ? 2 : 1;
    wrap.innerHTML = `<p class="muted" style="padding:12px">Wird geladen …</p>`;
    let data;
    try {
      data = await api(`/api/projekte?limit=300&min_members=${mm}${st ? `&status=${st}` : ""}`);
      TABLE_TOTALS["objekteWrap"] = data.total;
    } catch (e) {
      wrap.innerHTML = `<p class="muted" style="padding:12px">Fehler: ${esc(e.message)}</p>`;
      return;
    }
    const o = data.overview || {};
    $("#objekteKpis").innerHTML = `
      <div class="kpi"><div class="kpi-label">Projekte</div>
        <div class="kpi-value">${(o.projects || 0).toLocaleString("de-DE")}</div></div>
      <div class="kpi"><div class="kpi-label">Gewonnen</div>
        <div class="kpi-value">${(o.gewonnen || 0).toLocaleString("de-DE")}</div></div>
      <div class="kpi"><div class="kpi-label">Projekt-Gewinnrate</div>
        <div class="kpi-value">${o.project_win_rate != null ? (o.project_win_rate * 100).toFixed(1) + " %" : "—"}</div></div>
      <div class="kpi"><div class="kpi-label">Gewonnener Wert</div>
        <div class="kpi-value" title="${eur(o.won_value)}">${eurShort(o.won_value)}</div></div>`;
    const rows = data.rows || [];
    if (!rows.length) {
      wrap.innerHTML = `<p class="muted" style="padding:12px">Keine Projekte für diesen Filter.</p>`;
      return;
    }
    wrap.innerHTML = `
      <table class="data-table">
        <thead><tr><th>Objekt</th><th>Status</th><th class="num">VCs</th>
          <th class="num">Wert</th><th>Firmen</th><th>Architekten</th><th>Verlustgründe</th></tr></thead>
        <tbody>${rows.map(p => `
          <tr>
            <td style="max-width:280px">${esc(p.name)}${p.created ? `<div class="sub">${esc(p.created)}</div>` : ""}</td>
            <td><span class="state-chip">${esc(p.status)}</span></td>
            <td class="num">${p.members}${p.won_members ? ` <span class="sub">(${p.won_members} gew.)</span>` : ""}</td>
            <td class="num">${eur(p.order_value ?? p.estimated_value)}</td>
            <td style="max-width:220px">${esc((p.firms || []).join(", "))}</td>
            <td style="max-width:180px">${esc((p.architects || []).join(", "))}</td>
            <td style="max-width:200px" class="sub">${esc((p.lost_reasons || []).join(", "))}</td>
          </tr>`).join("")}</tbody>
      </table>`;
  }

  function ensureObjekteLoaded() {
    if (objekteLoaded) return;
    objekteLoaded = true;
    loadObjekte();
  }

  function wireObjekte() {
    const btn = $("#objekteReload");
    if (!btn) return;
    btn.addEventListener("click", loadObjekte);
    $("#objekteStatus").addEventListener("change", loadObjekte);
    $("#objekteMulti").addEventListener("change", loadObjekte);
  }

  // ------------------------------------------------------- Prüfen tab
  // Human yes/no on unproven website identities. Accept re-enriches from the
  // approved site; reject clears the domain so the finder searches again.
  let pruefenLoaded = false;

  async function loadPruefen() {
    const wrap = $("#pruefenWrap");
    // Every filter is a plain multi-value param; empty means "all". The old
    // screen had one hard-coded "Nur Spanien" checkbox, so every other market
    // became unreachable the moment one existed.
    const qs = ["country", "lead_source", "segment"]
      .map(k => {
        const v = $(`#pruefen${{country: "Land", lead_source: "Quelle", segment: "Segment"}[k]}`).value;
        return v ? `&${k}=${encodeURIComponent(v)}` : "";
      }).join("");
    wrap.innerHTML = `<p class="muted" style="padding:12px">Wird geladen …</p>`;
    let data;
    try {
      data = await api(`/api/identity/review?limit=200${qs}`);
      TABLE_TOTALS["pruefenWrap"] = data.total;
    } catch (e) {
      wrap.innerHTML = `<p class="muted" style="padding:12px">Fehler: ${esc(e.message)}</p>`;
      return;
    }
    fillPruefenFacets(data.facets || {});
    const cnt = $("#pruefenCount");
    if (cnt) cnt.textContent = data.total != null
      ? `${data.shown} von ${data.total} offen` : "";
    const rows = data.rows || [];
    if (!rows.length) {
      wrap.innerHTML = `<p class="muted" style="padding:12px">Nichts zu prüfen — alles entschieden. 🎉</p>`;
      return;
    }
    wrap.innerHTML = `
      <table class="data-table">
        <thead><tr><th>Firma</th><th>Ort</th><th>Kandidat</th>
          <th>Hinweis</th><th style="width:170px">Entscheidung</th></tr></thead>
        <tbody>${rows.map(r => `
          <tr data-company="${r.company_id}">
            <td>${esc(r.name)}${r.import_type ? `<div class="sub">${esc(r.import_type)}</div>` : ""}</td>
            <td>${esc(r.city || "—")}${r.postal_code ? ` <span class="sub">${esc(r.postal_code)}</span>` : ""}</td>
            <td>${r.domain ? `<a href="https://${esc(r.domain)}" target="_blank" rel="noopener">${esc(r.domain)}</a>` : "—"}</td>
            <td style="max-width:340px">${esc(r.clue || r.what || r.notes || "")}</td>
            <td>
              <button class="btn btn-primary pruefen-ok" data-domain="${esc(r.domain || "")}">✓ Ja</button>
              <button class="btn pruefen-no">✗ Nein</button>
            </td>
          </tr>`).join("")}</tbody>
      </table>`;
    $$("#pruefenWrap .pruefen-ok").forEach(b => b.addEventListener("click", async (ev) => {
      const tr = ev.target.closest("tr");
      const cid = Number(tr.dataset.company);
      ev.target.disabled = true;
      try {
        await api(`/api/companies/${cid}/enrichment/accept`, "POST",
                  { page_id: ev.target.dataset.domain });
        tr.style.opacity = "0.45";
        toast("Website bestätigt — wird angereichert.");
      } catch (e) { toast(e.message, "error"); ev.target.disabled = false; }
    }));
    $$("#pruefenWrap .pruefen-no").forEach(b => b.addEventListener("click", async (ev) => {
      const tr = ev.target.closest("tr");
      const cid = Number(tr.dataset.company);
      ev.target.disabled = true;
      try {
        await api(`/api/companies/${cid}/identity/reject`, "POST", {});
        tr.style.opacity = "0.45";
        toast("Abgelehnt — Suche nach der richtigen Website ist wieder offen.");
      } catch (e) { toast(e.message, "error"); ev.target.disabled = false; }
    }));
  }

  // Options come from what is ACTUALLY in the queue, so the screen adapts to
  // whichever markets are loaded instead of naming one in the HTML. The current
  // selection is preserved across reloads.
  const PRUEFEN_FACETS = {country: "#pruefenLand", lead_source: "#pruefenQuelle",
                          segment: "#pruefenSegment"};
  const LEAD_SOURCE_LABEL = (v) => v === null || v === "" ? "Alle Quellen" : v;

  function fillPruefenFacets(facets) {
    for (const [key, sel] of Object.entries(PRUEFEN_FACETS)) {
      const el = $(sel);
      if (!el) continue;
      const keep = el.value;
      const all = el.options[0] ? el.options[0].textContent : "Alle";
      const vals = facets[key] || [];
      el.innerHTML = `<option value="">${esc(all)}</option>` +
        vals.map(v => `<option value="${esc(v)}"${v === keep ? " selected" : ""}>${esc(LEAD_SOURCE_LABEL(v))}</option>`).join("");
      el.value = vals.includes(keep) ? keep : "";
    }
  }

  function ensurePruefenLoaded() {
    if (pruefenLoaded) return;
    pruefenLoaded = true;
    loadPruefen();
  }

  function wirePruefen() {
    const btn = $("#pruefenReload");
    if (!btn) return;
    btn.addEventListener("click", loadPruefen);
    ["#pruefenLand", "#pruefenQuelle", "#pruefenSegment"]
      .forEach(sel => $(sel)?.addEventListener("change", loadPruefen));
  }

  // ---------------- Profil tab (Ideal Customer Profile) ----------------
  let ICP_FILTERS = null;   // the winners filter behind the current preview

  function icpWinnersFilters() {
    return $("#icpWinnersMode").value === "filter" ? currentCustomerFilters() : {};
  }

  // Below this share of buyers, a percentage is arithmetic on a handful of rows
  // and reads far more confident than it is — say so instead of drawing a bar
  // chart of "50% sind 20-49 Mitarbeiter" computed from 2% of the base.
  const ICP_COVERAGE_WEAK = 0.5;

  // A plain-German sentence a salesperson can act on, built only from features
  // that are actually populated enough to mean something.
  function icpPlainSummary(p) {
    const solid = Object.entries(p.features)
      .filter(([, d]) => d.coverage >= ICP_COVERAGE_WEAK && d.top.length
                         && d.top[0][1] < 0.9);
    if (!solid.length) return "";
    const bits = solid.slice(0, 3).map(([, d]) => {
      const top = d.top.slice(0, 2).map(([v, sh]) => `${esc(v)} (${Math.round(sh * 100)}%)`);
      return `<b>${esc(d.label)}:</b> meist ${top.join(" oder ")}`;
    });
    const weak = Object.entries(p.features).filter(([, d]) => d.coverage < ICP_COVERAGE_WEAK);
    return `<p class="icp-plain"><b>Kurz gesagt:</b> Wer bei uns kauft, ist typischerweise —
      ${bits.join(" · ")}. Solche Firmen bekommen einen hohen Fit-Score.</p>`
      + (weak.length
        ? `<p class="hint">Für ${weak.map(([, d]) => esc(d.label)).join(", ")} liegen noch zu
             wenige Daten vor, um daraus etwas zu schließen — die Datenanreicherung füllt das auf.</p>`
        : "");
  }

  function renderIcpFeatures(p) {
    $("#icpProfileTitle").textContent =
      `Profil aus ${p.winners_count.toLocaleString("de-DE")} kaufenden Firmen`;
    $("#icpPlainSummary").innerHTML = icpPlainSummary(p);
    $("#icpFeatures").innerHTML = Object.entries(p.features).map(([key, d]) => {
      const maxShare = d.top.length ? d.top[0][1] : 0;
      const excluded = maxShare >= 0.9 && key !== "products";
      const weak = d.coverage < ICP_COVERAGE_WEAK;
      const rows = d.top.slice(0, 5).map(([v, share]) => `
        <div class="fit-row">
          <span class="fit-row-label">${esc(v)}</span>
          <span class="fit-bar"><span class="fit-bar-fill" style="width:${Math.round(share * 100)}%"></span></span>
          <span class="fit-row-pct">${Math.round(share * 100)}%</span>
        </div>`).join("");
      const cov = Math.round(d.coverage * 100);
      return `<div class="icp-feature${excluded ? " icp-feature-excluded" : ""}${weak ? " icp-feature-weak" : ""}">
        <div class="icp-feature-head"><b>${esc(d.label)}</b>
          <span class="muted small">bei ${cov}% der Käufer bekannt</span>
          ${weak ? `<span class="tag tag-warn" title="Die Prozente unten beruhen nur auf ${cov}% der kaufenden Firmen — als Hinweis lesen, nicht als Aussage über den Markt. Mehr Datenanreicherung behebt das.">zu wenige Daten</span>` : ""}
          ${excluded ? `<span class="tag" title="Fast alle Käufer haben hier denselben Wert. Ein Merkmal, das alle teilen, unterscheidet niemanden — es zählt deshalb nicht in den Fit-Score.">unterscheidet nicht</span>` : ""}
        </div>
        ${rows || '<p class="hint">keine Daten — Anreicherung erhöht die Abdeckung</p>'}
      </div>`;
    }).join("");
    $("#icpProfileCard").classList.remove("hidden");
  }

  // ONE action: compute the profile AND immediately score every company with
  // it — after "berechnen" the Fit column is simply there, no second click.
  async function icpPreview() {
    const btn = $("#icpPreviewBtn");
    btn.disabled = true; btn.textContent = "Berechne & bewerte…";
    try {
      ICP_FILTERS = icpWinnersFilters();
      const p = await api("/api/icp/preview", "POST", { filters: ICP_FILTERS });
      if (p.winners_count < 5) {
        $("#icpWinnersHint").textContent =
          `Nur ${p.winners_count} Firmen im Gewinner-Filter — zu wenige für ein belastbares Profil.`;
        return;
      }
      $("#icpWinnersHint").textContent = "";
      renderIcpFeatures(p);
      const res = await api("/api/icp/apply", "POST", { filters: ICP_FILTERS || {} });
      toast(`✓ Profil angewendet — ${res.companies_scored.toLocaleString("de-DE")} Firmen bewertet. `
        + `Die Fit-Spalte im Companies-Tab ist aktuell.`, "info");
      await loadIcpStatus();
      loadCustomers().catch(() => {});   // Fit column changed
    } catch (e) {
      toast(`Profil fehlgeschlagen: ${e.message}`, "error");
    } finally { btn.disabled = false; btn.textContent = "Profil berechnen & anwenden"; }
  }

  async function loadIcpStatus() {
    const box = $("#icpStatus");
    try {
      const p = await api("/api/icp/latest");
      if (!p.id) {
        box.innerHTML = `<p class="hint">Noch kein Profil angewendet — oben berechnen &amp; anwenden.
          Danach hat jede Firma einen <b>Fit</b>-Wert (Spalte im Companies-Tab, sortierbar).</p>`;
        return;
      }
      box.innerHTML = `<p><b>${esc(p.name)}</b> · aus ${p.winners_count.toLocaleString("de-DE")} Gewinnern
        · angewendet ${esc(p.applied_at || p.created_at)}</p>
        <p class="hint">Beste Ziele finden: im Companies-Tab nach <b>Fit</b> sortieren und z. B. auf
        „Ehemaliger Kunde" filtern — oder nach dem Ziel-Score (Fit × Chance) in der Firmenakte schauen.</p>`;
    } catch (e) {
      box.innerHTML = `<p class="hint status-error">${esc(e.message)}</p>`;
    }
  }

  // ------------------------------------------------------------------ Companies tab (Explorer)
  const CUST = {
    loaded: false,
    filters: {},
    sort: "name",
    direction: "asc",
    page: 1,
    pageSize: 50,
    total: 0,
    selected: new Set(),   // company ids, persists across pages/filters
  };

  function currentCustomerFilters() {
    const tracked = $("#custTracked").value;
    // the status dropdown mixes real statuses with the two page-id pseudo
    // options — split them apart here (both/neither id option = no restriction)
    const idSel = CUST_DROP.status.getSelected();
    const withId = idSel.includes("with_id"), withoutId = idSel.includes("without_id");
    // The ad-activity dropdown packs both dimensions into one value
    // ("active:meta" = running ads, Meta only); split into orthogonal params.
    const [adAct, adSrc] = ($("#custAdActivity").value || "").split(":");
    return {
      q: $("#custSearch").value.trim() || null,
      resolution_status: idSel.filter(v => v !== "with_id" && v !== "without_id"),
      page_id_state: withId === withoutId ? null : (withId ? "with" : "without"),
      kv: CUST_DROP.kv.getSelected(),
      segment: CUST_DROP.segment.getSelected(),
      sub_segment: CUST_DROP.subSegment.getSelected(),
      sales_channel: CUST_DROP.salesChannel.getSelected(),
      country: CUST_DROP.country.getSelected(),
      has_website: $("#custHasWebsite").checked,
      no_website: $("#custNoWebsite").checked,
      enrichment_status: $("#custEnrichStatus").value ? [$("#custEnrichStatus").value] : [],
      customer_state: $("#custCustomerState").value ? [$("#custCustomerState").value] : [],
      solarlux_relevance: $("#custRelevance").value ? [$("#custRelevance").value] : [],
      solarlux_fit: $("#custFit").value ? [$("#custFit").value] : [],
      decision_role: $("#custDecisionRole").value ? [$("#custDecisionRole").value] : [],
      fit_min: $("#custFitMin").value ? Number($("#custFitMin").value) : null,
      revenue_min: $("#custRevenueMin").value ? Number($("#custRevenueMin").value) : null,
      revenue_max: $("#custRevenueMax").value ? Number($("#custRevenueMax").value) : null,
      revenue_history: $("#custRevenueHistory").value || null,
      ad_activity: adAct || null,
      ad_source: adSrc || null,
      exclude_kv: CUST_DROP.excludeKv.getSelected(),
      exclude_segment: CUST_DROP.excludeSegment.getSelected(),
      exclude_sub_segment: CUST_DROP.excludeSubSegment.getSelected(),
      tracked: tracked === "" ? null : tracked === "true",
    };
  }

  const REVENUE_HISTORY_LABEL = { lapsed: "lapsed buyers", new: "new buyers", any: "ever had revenue", never: "never had revenue" };

  function activeFilterSummary(f) {
    const parts = [];
    if (f.q) parts.push(`search "${f.q}"`);
    if (f.resolution_status?.length) parts.push(`status: ${f.resolution_status.map(s => ID_STATUS_LABEL[s] || s).join(", ")}`);
    if (f.kv?.length) parts.push(`KV: ${f.kv.join(", ")}`);
    if (f.segment?.length) parts.push(`segment: ${f.segment.join(", ")}`);
    if (f.sub_segment?.length) parts.push(`sub-segment: ${f.sub_segment.join(", ")}`);
    if (f.sales_channel?.length) parts.push(`channel: ${f.sales_channel.join(", ")}`);
    if (f.country?.length) parts.push(`country: ${f.country.join(", ")}`);
    if (f.revenue_min != null && f.revenue_max != null) parts.push(`revenue €${f.revenue_min}–€${f.revenue_max}`);
    else if (f.revenue_min != null) parts.push(`revenue ≥ €${f.revenue_min}`);
    else if (f.revenue_max != null) parts.push(`revenue ≤ €${f.revenue_max}`);
    if (f.revenue_history) parts.push(REVENUE_HISTORY_LABEL[f.revenue_history] || f.revenue_history);
    if (f.ad_activity) {
      const base = { active: "running ads", any: "any ads ever", none: "no active ads" }[f.ad_activity] || f.ad_activity;
      const src = { meta: "Meta", google: "Google" }[f.ad_source];
      parts.push(src ? `${base} (${src})` : base);
    }
    if (f.exclude_kv?.length) parts.push(`excl. KV: ${f.exclude_kv.join(", ")}`);
    if (f.exclude_segment?.length) parts.push(`excl. segment: ${f.exclude_segment.join(", ")}`);
    if (f.exclude_sub_segment?.length) parts.push(`excl. sub-segment: ${f.exclude_sub_segment.join(", ")}`);
    if (f.has_website) parts.push("has website");
    if (f.no_website) parts.push("without website");
    if (f.enrichment_status?.length)
      parts.push(`enrichment: ${f.enrichment_status.map(s => ENRICH_STATUS_LABEL[s] || s).join(", ")}`);
    if (f.customer_state?.length)
      parts.push(f.customer_state.map(s => CUSTOMER_STATE_LABEL[s] || s).join(", "));
    if (f.solarlux_relevance?.length) parts.push(`Relevanz: ${f.solarlux_relevance.join(", ")}`);
    if (f.solarlux_fit?.length) parts.push(`Passung: ${f.solarlux_fit.join(", ")}`);
    if (f.decision_role?.length) parts.push(f.decision_role.join(", "));
    if (f.fit_min != null) parts.push(`Fit ≥ ${f.fit_min}`);
    if (f.page_id_state === "with") parts.push("with Meta page ID");
    if (f.page_id_state === "without") parts.push("without Meta page ID");
    if (f.tracked === true) parts.push("tracked only");
    if (f.tracked === false) parts.push("untracked only");
    return parts;
  }

  function updateReportFilterHint() {
    const hint = $("#reportFilterHint");
    const parts = activeFilterSummary(currentCustomerFilters());
    const on = $("#reportUseFilter").checked;
    hint.classList.toggle("hint-scoped", on && parts.length > 0);
    if (!on) {
      hint.textContent = parts.length
        ? `⚠ Companies filter is active (${parts.join(", ")}) but the box is unchecked — the report will cover ALL companies. Check the box to scope it.`
        : "No Companies filter active — report covers all companies.";
    } else {
      hint.textContent = parts.length
        ? `✓ Report scoped to your Companies filter: ${parts.join(", ")}. This appears in the report header.`
        : "No Companies filter active — report covers all companies.";
    }
  }

  // ------------------------------------------------------------------ Logs tab
  const LOGS = { page: 1, pageSize: 50, total: 0, loaded: false };

  function ensureLogsLoaded() {
    if (LOGS.loaded) return;
    LOGS.loaded = true;
    loadLogs();
  }

  // ---- Berichte & Versand: the report audit trail (ReportEvent) -------------
  const REV_KIND = {
    created:     { label: "erstellt",       cls: "ev-created" },
    sent:        { label: "gesendet ✓",     cls: "ev-sent" },
    send_failed: { label: "Versand ✗",      cls: "ev-failed" },
  };
  const REV_SOURCE = {
    manual: "manuell", pipeline: "Pipeline", schedule: "Zeitplan",
    definition: "gespeicherter Bericht",
  };

  async function loadReportEvents() {
    let events = [];
    try {
      events = (await api("/api/report-events?limit=200")).events || [];
    } catch (e) {
      toast(`Berichts-Historie nicht ladbar: ${e.message || e}`, "error");
      return;
    }
    const want = $("#revKind").value;
    const rows = want ? events.filter(e => e.kind === want) : events;
    const body = $("#reportEventsBody");
    $("#reportEventsEmpty").style.display = rows.length ? "none" : "";
    body.innerHTML = rows.map(e => {
      const k = REV_KIND[e.kind] || { label: e.kind, cls: "" };
      const to = (e.recipients || []).join(", ");
      return `<tr>
        <td class="nowrap">${esc(fmtDateTime(e.at))}</td>
        <td><span class="ev-tag ${k.cls}">${esc(k.label)}</span></td>
        <td>${esc(e.filename)}${e.report_type ? ` <span class="muted">(${esc(e.report_type)})</span>` : ""}</td>
        <td class="muted small">${esc(e.scope || "—")}</td>
        <td>${to ? esc(to) : "<span class='muted'>—</span>"}</td>
        <td class="muted small">${esc(REV_SOURCE[e.source] || e.source || "—")}</td>
        <td class="muted small">${esc(e.detail || "")}</td>
      </tr>`;
    }).join("");
  }

  function fmtDateTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString("de-DE",
      { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  async function loadLogs() {
    loadReportEvents();          // independent of the fetch-log query below
    const params = new URLSearchParams();
    const status = $("#logStatus").value, source = $("#logSource").value, q = $("#logSearch").value.trim();
    if (status) params.set("status", status);
    if (source) params.set("source", source);
    if (q) params.set("q", q);
    params.set("page", LOGS.page);
    params.set("page_size", LOGS.pageSize);
    const data = await api(`/api/logs?${params.toString()}`);
    LOGS.total = data.total;
    renderLogs(data);
  }

  function renderLogs(data) {
    const body = $("#logsTableBody");
    $("#logsEmptyHint").classList.toggle("hidden", data.total > 0);
    $("#logTotal").textContent = `${data.total.toLocaleString("de-DE")} runs`;
    body.innerHTML = data.rows.map(r => {
      const short = r.error ? esc(r.error.replace(/\s+/g, " ").trim().slice(0, 80)) + (r.error.length > 80 ? "…" : "") : "—";
      return `<tr class="log-row" data-full-error="${esc(r.error || "")}">
        <td class="muted">${esc(r.run_date)}</td>
        <td>${esc(r.company)}</td>
        <td>${esc(r.source)}</td>
        <td class="muted">${esc(r.page_name || r.page_role || "—")}</td>
        <td><span class="run-status run-status-${esc(r.status)}">${esc(RUN_STATUS_LABEL[r.status] || r.status)}</span></td>
        <td class="num">${r.ads_scraped}</td>
        <td class="log-error-cell">${short}</td>
      </tr>`;
    }).join("");

    $$(".log-row", body).forEach(tr => tr.addEventListener("click", () => {
      const full = tr.dataset.fullError;
      if (!full) return;
      const cell = $(".log-error-cell", tr);
      tr.classList.toggle("expanded");
      if (tr.classList.contains("expanded")) {
        cell.innerHTML = `<pre class="log-error-full">${esc(full)}</pre>`;
      } else {
        cell.textContent = esc(full.replace(/\s+/g, " ").trim().slice(0, 80)) + (full.length > 80 ? "…" : "");
      }
    }));

    const from = data.total ? (data.page - 1) * data.page_size + 1 : 0;
    const to = Math.min(data.page * data.page_size, data.total);
    $("#logPageInfo").textContent = `${from}–${to} of ${data.total.toLocaleString("de-DE")}`;
    $("#logPrevBtn").disabled = data.page <= 1;
    $("#logNextBtn").disabled = to >= data.total;
  }

  function wireLogsTabControls() {
    $("#revKind").addEventListener("change", loadReportEvents);
    $("#revRefresh").addEventListener("click", loadReportEvents);
    $("#logSearch").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { LOGS.page = 1; loadLogs(); }
    });
    $("#logStatus").addEventListener("change", () => { LOGS.page = 1; loadLogs(); });
    $("#logSource").addEventListener("change", () => { LOGS.page = 1; loadLogs(); });
    $("#logClearBtn").addEventListener("click", () => {
      $("#logSearch").value = ""; $("#logStatus").value = ""; $("#logSource").value = "";
      LOGS.page = 1; loadLogs();
    });
    $("#logPrevBtn").addEventListener("click", () => { if (LOGS.page > 1) { LOGS.page--; loadLogs(); } });
    $("#logNextBtn").addEventListener("click", () => { LOGS.page++; loadLogs(); });

    $("#logsPurgeBtn").addEventListener("click", async () => {
      if (!await appConfirm(
        "Clear the fetch log?\n\nAll log entries WITHOUT stored ads are deleted "
        + "(errors, no-ads runs, skips). Entries with collected ad copies are kept — "
        + "clearing the log never deletes ad data.",
        { title: "Clear logs", confirmText: "Clear logs", danger: true })) return;
      try {
        const res = await api("/api/logs/clear", "POST");
        LOGS.page = 1;
        await loadLogs();
        $("#logTotal").textContent =
          `${res.kept.toLocaleString("de-DE")} runs (cleared ${res.deleted.toLocaleString("de-DE")})`;
      } catch (e) { alert(`Could not clear logs: ${e.message}`); }
    });
  }

  // ------------------------------------------------------------------ Settings tab
  let SETTINGS_LOADED = false;
  const SETTINGS_ORIG = {};            // key -> loaded non-secret value (change detection)
  const SETTINGS_CLEARED = new Set();  // keys the user staged to reset to .env/default

  function ensureSettingsLoaded() { if (!SETTINGS_LOADED) { SETTINGS_LOADED = true; loadSettings(); } }

  const SET_SOURCE_LABEL = { custom: "custom", env: ".env", default: "default" };

  async function loadSettings() {
    SETTINGS_CLEARED.clear();
    let data;
    try { data = await api("/api/settings"); } catch (e) { alert(`Could not load settings: ${e.message}`); return; }
    $("#settingsGroups").innerHTML = data.groups.map(g => `
      <div class="card">
        <h2>${esc(g.name)}</h2>
        ${g.fields.map(settingFieldHtml).join("")}
      </div>`).join("");

    $$(".set-test").forEach(btn => btn.addEventListener("click", () => runSettingTest(btn)));
    $$(".set-eye").forEach(btn => btn.addEventListener("click", () => toggleReveal(btn)));
    // typing marks the field dirty so Save/Test use the new value (not a
    // programmatically-revealed one that the user never edited)
    $$(".set-input").forEach(inp => inp.addEventListener("input", () => { inp.dataset.dirty = "1"; markSettingsDirty(); }));
    // clear any stale dirty cue on (re)load
    $("#settingsSaveHint").textContent = "";
    $("#settingsSaveHint").classList.remove("dirty");
    $$(".settings-save-btn").forEach(b => b.classList.remove("has-changes"));
    $$(".set-reset").forEach(btn => btn.addEventListener("click", () => {
      const key = btn.dataset.key;
      SETTINGS_CLEARED.add(key);
      const inp = $(`.set-input[data-key="${key}"]`);
      inp.value = ""; inp.disabled = true; inp.placeholder = "will reset to .env / default on save";
      delete inp.dataset.dirty;
      btn.remove();
    }));
    $("#settingsSaveHint").textContent = "";
  }

  // provider name (test endpoint) <-> config key, so auto-test after save works
  const SET_PROVIDER_BY_KEY = { APIFY_API_TOKEN: "apify", SERPER_API_KEY: "serper", ANTHROPIC_API_KEY: "anthropic" };

  function settingFieldHtml(f) {
    SETTINGS_ORIG[f.key] = f.secret ? "" : (f.value || "");
    const badge = `<span class="set-badge set-badge-${f.source}">${SET_SOURCE_LABEL[f.source] || f.source}</span>`;
    const reset = f.source === "custom"
      ? `<button class="btn btn-sm btn-ghost set-reset" data-key="${f.key}" title="Remove your override, fall back to .env / default">Reset</button>` : "";
    const test = f.test
      ? `<button class="btn btn-sm set-test" data-test="${esc(f.test)}">Test</button>` : "";
    const inputEl = f.secret
      ? `<div class="set-input-wrap">
           <input type="password" class="set-input" data-key="${f.key}" data-secret="1" autocomplete="off"
                  placeholder="${f.configured ? esc(f.hint) + " · gesetzt — zum Ändern eingeben" : "nicht gesetzt"}">
           <button type="button" class="set-eye" data-key="${f.key}" title="Anzeigen / verbergen" aria-label="Anzeigen">👁</button>
         </div>`
      : `<input type="text" class="set-input" data-key="${f.key}" value="${esc(f.value || "")}" autocomplete="off">`;
    return `
      <div class="set-field">
        <div class="set-field-top">
          <label class="set-label">${esc(f.label)}</label>
          ${badge}<span class="spacer"></span>${test}${reset}
        </div>
        ${inputEl}
        <span class="set-test-result"></span>
        ${f.help ? `<p class="hint">${esc(f.help)}</p>` : ""}
      </div>`;
  }

  // eye: reveal the stored secret (fetched on demand) or hide it again
  async function toggleReveal(btn) {
    const key = btn.dataset.key;
    const inp = $(`.set-input[data-key="${key}"]`);
    if (inp.disabled) return;
    if (inp.type === "password") {
      if (!inp.value && inp.dataset.dirty !== "1") {
        try { const r = await api("/api/settings/reveal", "POST", { key }); inp.value = r.value || ""; inp.dataset.revealed = "1"; }
        catch (e) { toast(`Anzeigen fehlgeschlagen: ${e.message}`, "error"); return; }
      }
      inp.type = "text"; btn.textContent = "🙈"; btn.classList.add("on");
    } else {
      inp.type = "password"; btn.textContent = "👁"; btn.classList.remove("on");
      // a value that was only revealed (never typed) is cleared on hide so it
      // re-masks and can't be accidentally re-saved
      if (inp.dataset.revealed === "1" && inp.dataset.dirty !== "1") { inp.value = ""; delete inp.dataset.revealed; }
    }
  }

  async function runSettingTest(btn) {
    const which = btn.dataset.test;
    const field = btn.closest(".set-field");
    const inp = field.querySelector(".set-input");
    // test the just-typed value when the user has entered one; else the saved key
    const value = (inp && inp.dataset.dirty === "1") ? inp.value.trim() : undefined;
    const result = field.querySelector(".set-test-result");
    btn.disabled = true; const label = btn.textContent; btn.textContent = "Testing…";
    result.textContent = ""; result.className = "set-test-result";
    try {
      const res = await api("/api/settings/test", "POST", value ? { which, value } : { which });
      result.textContent = (res.ok ? "✓ " : "✗ ") + res.detail;
      result.classList.add(res.ok ? "set-ok" : "set-fail");
    } catch (e) {
      result.textContent = "✗ " + e.message; result.classList.add("set-fail");
    } finally { btn.disabled = false; btn.textContent = label; }
  }

  async function saveSettings() {
    const changes = {};
    $$(".set-input").forEach(inp => {
      const key = inp.dataset.key;
      if (SETTINGS_CLEARED.has(key)) { changes[key] = ""; return; }
      const v = inp.value.trim();
      if (inp.dataset.secret === "1") {
        if (inp.dataset.dirty === "1" && v !== "") changes[key] = v;   // only a genuinely typed secret
      } else if (v !== (SETTINGS_ORIG[key] || "")) { changes[key] = v; }
    });
    const hint = $("#settingsSaveHint");
    if (!Object.keys(changes).length) { hint.textContent = "Keine Änderungen zu speichern."; return; }
    const btns = $$(".settings-save-btn");
    btns.forEach(b => { b.disabled = true; b.textContent = "Speichern…"; });
    try {
      const res = await api("/api/settings", "PUT", { settings: changes });
      toast(`${res.saved.length} Einstellung${res.saved.length === 1 ? "" : "en"} gespeichert.`, "info");
      const savedProviders = res.saved.map(k => SET_PROVIDER_BY_KEY[k]).filter(Boolean);
      SETTINGS_LOADED = false;
      await loadSettings();                     // refresh masks + source badges (clears dirty cue)
      if (typeof loadState === "function") loadState();  // reflect e.g. apify_configured
      // auto-test every credential that was just saved, so the ✓/✗ reflects the NEW key
      savedProviders.forEach(prov => { const b = $(`.set-test[data-test="${prov}"]`); if (b) runSettingTest(b); });
    } catch (e) { alert(`Speichern fehlgeschlagen: ${e.message}`); }
    finally { $$(".settings-save-btn").forEach(b => { b.disabled = false; b.textContent = "Save settings"; }); }
  }

  // reflect unsaved changes in the sticky top bar + highlight the Save buttons
  function markSettingsDirty() {
    $("#settingsSaveHint").textContent = "● Ungespeicherte Änderungen";
    $("#settingsSaveHint").classList.add("dirty");
    $$(".settings-save-btn").forEach(b => b.classList.add("has-changes"));
  }

  // DEFAULT FILTER: "Private Endkunden" is excluded from the start — BD hunts
  // trade partners, not consumers. Undoable in the Segment column menu
  // ("Ausschließen" abhaken); Zurücksetzen restores this default.
  const DEFAULT_EXCLUDE_SEGMENTS = ["Private Endkunden"];
  function applyDefaultExclusion() {
    const available = new Set(CUST_OPTS.segment || []);
    const wanted = DEFAULT_EXCLUDE_SEGMENTS.filter(s => available.has(s));
    if (wanted.length) CUST_DROP.excludeSegment.setSelected(wanted);
  }

  function ensureCustomersLoaded() {
    if (CUST.loaded) return;
    CUST.loaded = true;
    loadCustomerFilterOptions().then(() => {
      applyDefaultExclusion();          // options must exist before they can be checked
      loadCustomers();
    });
    loadJobs().then(jobs => {
      if (jobs.some(j => j.status === "running" || j.status === "queued")) startJobPolling();
    });
  }

  // distinct filter values, cached for the column-header menus
  let CUST_OPTS = { kv: [], segment: [], sub_segment: [], sales_channel: [], country: [] };

  async function loadCustomerFilterOptions() {
    try {
      const opts = await api("/api/customers/filter-options");
      CUST_OPTS = opts;
      CUST_DROP.kv.setOptions(opts.kv);
      CUST_DROP.segment.setOptions(opts.segment);
      CUST_DROP.subSegment.setOptions(opts.sub_segment);
      CUST_DROP.salesChannel.setOptions(opts.sales_channel);
      CUST_DROP.country.setOptions(opts.country);
      CUST_DROP.excludeKv.setOptions(opts.kv);
      CUST_DROP.excludeSegment.setOptions(opts.segment);
      CUST_DROP.excludeSubSegment.setOptions(opts.sub_segment);
      COMP_DROP.kv.setOptions(opts.kv);
      COMP_DROP.segment.setOptions(opts.segment);
      COMP_DROP.subSegment.setOptions(opts.sub_segment);
    } catch (e) { /* no data yet */ }
  }

  async function loadCustomers(append = false) {
    if (!append) CUST.page = 1;   // any filter/sort reload starts from the top
    CUST.filters = currentCustomerFilters();
    const params = new URLSearchParams();
    Object.entries(CUST.filters).forEach(([k, v]) => {
      if (Array.isArray(v)) { v.forEach(item => params.append(k, item)); return; }
      if (v !== null && v !== "" && !(k === "has_website" && v === false)) params.set(k, v);
    });
    params.set("sort", CUST.sort);
    params.set("direction", CUST.direction);
    params.set("page", CUST.page);
    params.set("page_size", CUST.pageSize);
    const data = await api(`/api/customers?${params.toString()}`);
    CUST.total = data.total;
    renderCustomers(data, append);
    updateFilterBadge();
  }

  // Infinite scroll: when the sentinel below the table comes into view and more
  // rows exist, the next page is fetched and APPENDED — no pager clicks.
  let _custLoadingMore = false;
  async function loadMoreCustomers() {
    if (_custLoadingMore) return;
    if ((CUST.page * CUST.pageSize) >= CUST.total) return;   // everything is loaded
    _custLoadingMore = true;
    try { CUST.page++; await loadCustomers(true); }
    catch { CUST.page--; }
    finally { _custLoadingMore = false; }
  }

  // which header owns which filter keys — powers the per-column active marker
  const _COL_FILTER_KEYS = {
    name: ["q"], kunde: ["customer_state"], fit: ["fit_min"], anzeigen: ["ad_activity"],
    fb: ["resolution_status", "page_id_state", "tracked"],
    website: ["has_website", "no_website", "enrichment_status"],
    umsatz: ["revenue_min", "revenue_max", "revenue_history"],
    kv: ["kv", "exclude_kv"],
    segment: ["segment", "exclude_segment", "solarlux_relevance", "decision_role", "solarlux_fit"],
    subseg: ["sub_segment", "exclude_sub_segment"], kanal: ["sales_channel"], land: ["country"],
  };
  function _filterActive(f, key) {
    const v = f[key];
    return Array.isArray(v) ? v.length > 0 : (v !== null && v !== undefined && v !== "" && v !== false);
  }

  function updateFilterBadge() {
    const f = currentCustomerFilters();
    const parts = activeFilterSummary(f);
    const info = $("#custFilterInfo");
    if (info) info.textContent = parts.length
      ? `${parts.length} Filter aktiv: ${parts.join(" · ").slice(0, 90)}${parts.join(" · ").length > 90 ? "…" : ""}`
      : "";
    // mark filtered columns in the header (little dot via CSS)
    $$("#customersTable thead th[data-col]").forEach(th => {
      const keys = _COL_FILTER_KEYS[th.dataset.col] || [];
      th.classList.toggle("th-filtered", keys.some(k => _filterActive(f, k)));
    });
  }

  function renderCustomers(data, append = false) {
    CUST.lastRows = append ? (CUST.lastRows || []).concat(data.rows) : data.rows;
    const body = $("#customersTableBody");
    $("#customersEmptyHint").classList.toggle("hidden", data.total > 0);
    // Always render the FULL accumulated list (not just the new page): rebuilding
    // the tbody keeps row event wiring single-bound — appending HTML and re-running
    // the $$(...) wiring below would double-bind listeners on the older rows.
    const html = CUST.lastRows.map(r => {
      const open = expandedPages.has(r.id);
      const website = r.website_domain
        ? `<a class="link" href="${esc(/^https?:\/\//.test(r.website_domain) ? r.website_domain : "https://" + r.website_domain)}" target="_blank" title="${esc(r.website_domain)}">${esc(r.website_domain)}</a>`
        : "";
      return `
      <tr data-id="${r.id}" class="${CUST.selected.has(r.id) ? "selected" : ""}">
        <td class="col-check"><input type="checkbox" class="cust-check" ${CUST.selected.has(r.id) ? "checked" : ""}></td>
        <td class="col-dot"><span class="dot dot-${r.resolution_status}" title="${esc(STATUS_LABEL[r.resolution_status] || r.resolution_status)}"></span></td>
        <td class="cell-name">${esc(r.name)}</td>
        <td>${customerStateChip(r.customer_state)}</td>
        <td class="num">${fitCell(r.fit_score)}</td>
        <td class="num">${r.active_ads == null ? '<span class="muted">—</span>' : (r.active_ads > 0 ? `<strong>${r.active_ads}</strong>` : '<span class="muted">0</span>')}</td>
        <td class="cell-ort">${esc(r.city || "")}</td>
        <td class="num">${eur(r.revenue_y0)}</td>
        <td class="col-extra">${esc(r.kv || "")}</td>
        <td class="col-extra">${esc(r.segment || "")}</td>
        <td class="col-extra">${esc(r.sub_segment || "")}</td>
        <td class="col-extra">${esc(r.sales_channel || "")}</td>
        <td class="col-extra">${esc(r.country || "")}</td>
        <td class="col-extra num">${eur(r.revenue_y1)}</td>
        <td class="col-extra num">${eur(r.revenue_y2)}</td>
        <td class="col-extra num">${eur(r.revenue_y3)}</td>
        <td class="col-extra num">${eur(r.revenue_y4)}</td>
        <td class="fb-page-cell">${fbPageCellHtml(r)}</td>
        <td class="cell-website">${website}</td>
        <td class="col-extra">${esc(r.sap_number || "")}</td>
        <td><button class="btn btn-sm pages-toggle-row" data-id="${r.id}">${open ? "Hide" : "Pages"}</button></td>
      </tr>
      <tr class="pages-row ${open ? "" : "hidden"}" data-pages-for="${r.id}">
        <td colspan="21"><div class="pages-body-inline" id="pages-${r.id}"></div></td>
      </tr>`;
    }).join("");
    body.innerHTML = html;

    $$(".cust-check", body).forEach(cb => cb.addEventListener("change", () => {
      const tr = cb.closest("tr"); const id = Number(tr.dataset.id);
      if (cb.checked) CUST.selected.add(id); else CUST.selected.delete(id);
      tr.classList.toggle("selected", cb.checked);
      updateSelectionUI();
    }));

    // click anywhere else on a row -> full company detail drawer. The checkbox
    // column is for SELECTING only — clicking it (even the padding) toggles the
    // box and never opens the info drawer.
    $$("tr[data-id]", body).forEach(tr => tr.addEventListener("click", (e) => {
      if (e.target.closest("button, a, input")) return;   // don't hijack controls
      const checkCell = e.target.closest(".col-check");
      if (checkCell) {
        const cb = $(".cust-check", checkCell);
        cb.checked = !cb.checked;
        cb.dispatchEvent(new Event("change", { bubbles: true }));
        return;
      }
      openCompanyDrawer(Number(tr.dataset.id));
    }));

    $$(".fb-edit-btn", body).forEach(btn => btn.addEventListener("click", () => {
      const id = Number(btn.dataset.id);
      const td = btn.closest("td");
      const row = CUST.lastRows.find(r => r.id === id);
      td.innerHTML = `
        <input type="text" class="fb-edit-input" value="${esc(row?.page_id || "")}" placeholder="Facebook page ID" style="width:120px">
        <button class="btn btn-sm fb-save-btn">Save</button>
        <button class="btn btn-sm fb-cancel-btn">✕</button>
      `;
      const input = $(".fb-edit-input", td);
      input.focus();
      const save = async () => {
        const val = input.value.trim();
        if (!val) { alert("Enter a Facebook page ID."); return; }
        try {
          await api(`/api/companies/${id}/pages`, "POST", { page_id: val, role: "main" });
          await loadCustomers();
        } catch (e) { alert(e.message); }
      };
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
      $(".fb-save-btn", td).addEventListener("click", save);
      $(".fb-cancel-btn", td).addEventListener("click", () => {
        renderCustomers({ total: CUST.total, page: CUST.page, page_size: CUST.pageSize, rows: CUST.lastRows });
      });
    }));

    $$(".pages-toggle-row", body).forEach(btn => btn.addEventListener("click", async () => {
      const id = Number(btn.dataset.id);
      const row = $(`tr[data-pages-for="${id}"]`, body);
      if (expandedPages.has(id)) {
        expandedPages.delete(id);
        row.classList.add("hidden");
        btn.textContent = "Pages";
      } else {
        expandedPages.add(id);
        await ensureSearchTerm(id);
        row.classList.remove("hidden");
        btn.textContent = "Hide";
        renderPagesBodyLazy(id);
      }
    }));

    // sort indicators
    $$("#customersTable th[data-sort]").forEach(th => {
      th.classList.toggle("sorted-asc", th.dataset.sort === CUST.sort && CUST.direction === "asc");
      th.classList.toggle("sorted-desc", th.dataset.sort === CUST.sort && CUST.direction === "desc");
    });

    const shown = CUST.lastRows.length;
    $("#custTotal").textContent = `${data.total.toLocaleString("de-DE")} Firmen`;
    $("#custPageInfo").textContent = data.total > shown
      ? `${shown.toLocaleString("de-DE")} von ${data.total.toLocaleString("de-DE")} geladen — weiter scrollen lädt mehr…`
      : (data.total ? `alle ${data.total.toLocaleString("de-DE")} geladen` : "");
    $("#custSelectPage").checked = CUST.lastRows.length > 0 && CUST.lastRows.every(r => CUST.selected.has(r.id));
    updateSelectionUI();
  }

  function updateSelectionUI() {
    const n = CUST.selected.size;
    $("#custActionBar").classList.toggle("hidden", n === 0);
    $("#custSelCount").textContent = n ? `${n} selected` : "";
    $("#custEditBtn").disabled = n !== 1;
    $("#custEditBtn").title = n === 1 ? "Open this company's details" : "Select exactly one company to edit";
  }

  // ---------------- selection actions: identity check / delete ----------------

  async function runIdentityAction() {
    const ids = [...CUST.selected];
    if (!ids.length) return;
    if (!await appConfirm(
      `Run the online identity check for ${ids.length} compan${ids.length === 1 ? "y" : "ies"}?\n\n`
      + `Resolves each Facebook/Instagram page via Google (serper) + AI relevance check — `
      + `no ads fetched, no Apify quota. Locked companies are skipped.`,
      { title: "Online identity check", confirmText: "Run check" })) return;
    try {
      await api("/api/identity-jobs", "POST", { company_ids: ids });
      CUST.selected.clear();
      await showJobProgressNow();          // progress modal first, table after
    } catch (e) { alert(`Could not start identity check: ${e.message}`); }
  }

  // Enrichment for a set of companies: find the missing website, then read facts
  // off the company's own site. Costs ~$0.001 (search) + ~$0.003 (LLM) each, so
  // the confirm states the rough total rather than hiding it.
  async function runEnrichForIds(ids) {
    if (!ids.length) return;
    const est = (ids.length * 0.004).toFixed(2);
    if (!await appConfirm(
      `Daten für ${ids.length} Firma/Firmen anreichern?\n\n`
      + `• Fehlende Website: erst aus der E-Mail-Domain (kostenlos), sonst per Websuche\n`
      + `• Danach werden Beschreibung, Produkte, Gründungsjahr, Größe und genannte `
      + `Marken (Solarlux/Wettbewerber) von der eigenen Website gelesen\n`
      + `• Eine gefundene Website wird nur automatisch übernommen, wenn Telefon/PLZ/Straße `
      + `auf der Seite bestätigt werden — sonst landet sie zur Prüfung in der Warteschlange\n`
      + `• Vorhandene Websites und Stammdaten werden nie überschrieben\n\n`
      + `Geschätzte Kosten: ca. $${est}. Läuft im Hintergrund (abbrechbar).`,
      { title: "Daten anreichern", confirmText: "Anreichern" })) return;
    try {
      await api("/api/enrich-jobs", "POST", { company_ids: ids });
      CUST.selected.clear();
      await showJobProgressNow();          // progress modal first, table after
    } catch (e) { alert(`Anreicherung konnte nicht gestartet werden: ${e.message}`); }
  }

  // Every company id under the current filter, WITHOUT touching the user's
  // selection. (selectAllMatching() lives inside wireStatic() and is therefore
  // not reachable from here — calling it threw "selectAllMatching is not
  // defined" and silently killed both this panel and the filtered-enrich
  // button.) Reading the scope is also simply the right behaviour: acting on
  // "everything filtered" shouldn't mutate what the user had ticked.
  async function filteredCompanyIds() {
    const n = CUST.total || 0;
    if (!n) return [];
    const { ids } = await api("/api/customers/select-top", "POST",
      { filters: currentCustomerFilters(), sort: CUST.sort, direction: CUST.direction, n });
    return ids || [];
  }

  // ---------------- Komplett-Pipeline ----------------
  let PIPE_IDS = [];

  function pipelinePlan() {
    const ads = [];
    if ($("#pipeAdsMeta").checked) ads.push("meta");
    if ($("#pipeAdsGoogle").checked) ads.push("google");
    return {
      enrich: $("#pipeEnrich").checked,
      identity: $("#pipeIdentity").checked,
      ads: $("#pipeAds").checked ? ads : [],
      report: $("#pipeReport").checked ? $("#pipeReportType").value : null,
      send_to: $("#pipeSend").checked ? checkedRecipientIds($("#pipeRecipients")) : [],
    };
  }

  // Mirrors jobs.resolve_step_order() — the backend stays the authority, this
  // only shows the user which order their selection will actually run in.
  function pipelineOrder(p) {
    const order = [];
    if (p.enrich && p.identity) order.push("Domains ableiten (gratis)");
    if (p.identity) order.push("Identität");
    if (p.enrich) order.push("Anreichern");
    if (p.ads.length) order.push(`Anzeigen (${p.ads.join("+")})`);
    if (p.report) order.push("Bericht");
    if (p.send_to.length) order.push(`Senden (${p.send_to.length})`);
    return order;
  }

  function renderPipelineSummary() {
    const p = pipelinePlan();
    const n = PIPE_IDS.length;
    // per-company cost/time, deliberately rough and labelled as an estimate.
    // The free domain pre-pass costs nothing but does fetch each candidate page
    // to validate it, so it costs TIME — don't hide that.
    let cost = 0, secs = 0;
    if (p.enrich && p.identity) secs += n * 4;
    if (p.enrich) { cost += n * 0.004; secs += n * 8; }
    if (p.identity) { cost += n * 0.005; secs += n * 15; }
    if (p.ads.includes("meta")) { cost += n * 0.004; secs += n * 20; }
    if (p.ads.includes("google")) { cost += n * 0.012; secs += n * 110; }
    const steps = pipelineOrder(p);
    const orderEl = $("#pipelineOrder");
    if (orderEl) {
      orderEl.innerHTML = steps.length
        ? `<b>Reihenfolge:</b> ${steps.map(esc).join(" → ")}`
        : "";
    }
    const fmt = s => s < 90 ? `${Math.round(s)} s` : (s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);
    $("#pipelineSummary").innerHTML = `
      <div><div class="stat-label">Firmen</div><div class="stat-value">${n.toLocaleString("de-DE")}</div></div>
      <div><div class="stat-label">Schritte</div><div class="stat-value">${steps.length}</div></div>
      <div><div class="stat-label">Geschätzte Dauer</div><div class="stat-value">${secs ? fmt(secs) : "—"}</div></div>
      <div><div class="stat-label">Geschätzte Kosten</div><div class="stat-value">${cost ? "$" + cost.toFixed(2) : "$0"}</div></div>
      <p class="hint" style="flex-basis:100%">Ablauf: ${steps.join(" → ") || "— kein Schritt gewählt"}</p>`;
    $("#pipelineStartBtn").disabled = !steps.length || !n;
    $("#pipeRecipientsWrap").classList.toggle("hidden", !$("#pipeSend").checked);
    // sending needs a report in the same run
    if ($("#pipeSend").checked && !$("#pipeReport").checked) {
      $("#pipelineSummary").innerHTML +=
        `<p class="hint status-error" style="flex-basis:100%">Zum Senden muss Schritt 4 (Bericht erstellen) aktiv sein.</p>`;
      $("#pipelineStartBtn").disabled = true;
    }
  }

  async function openPipelinePanel() {
    // scope = current selection if any, else the whole filtered set
    PIPE_IDS = CUST.selected.size ? [...CUST.selected] : await filteredCompanyIds();
    $("#pipelineScope").textContent =
      `Umfang: ${PIPE_IDS.length.toLocaleString("de-DE")} Firmen` +
      (activeFilterSummary(currentCustomerFilters()).length
        ? ` · Filter: ${activeFilterSummary(currentCustomerFilters()).join(" · ")}` : "");
    renderRecipientChecks($("#pipeRecipients"), await ensureRecipients());
    hideCompanyPanels();
    $("#pipelinePanel").classList.remove("hidden");
    renderPipelineSummary();
    $("#pipelinePanel").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function startPipeline() {
    const p = pipelinePlan();
    const n = PIPE_IDS.length;
    const order = [p.enrich && "1 Daten anreichern", p.identity && "2 Identitätsprüfung",
                   p.ads.length && `3 Ad lookup (${p.ads.join(" + ")})`,
                   p.report && `4 Bericht (${p.report === "full" ? "vollständig" : "Top 5"})`,
                   p.send_to.length && `5 Senden an ${p.send_to.length} Empfänger`].filter(Boolean);
    if (!await appConfirm(
      `Pipeline für ${n} Firmen starten?\n\n${order.join("\n")}\n\n`
      + `Die Schritte laufen in dieser Reihenfolge im Hintergrund. Fortschritt und `
      + `jede einzelne Firma siehst du live; abbrechen ist jederzeit möglich.`,
      { title: "Komplett-Pipeline", confirmText: "Pipeline starten" })) return;
    const btn = $("#pipelineStartBtn");
    btn.disabled = true; btn.textContent = "Startet…";
    try {
      await api("/api/pipeline-jobs", "POST", {
        company_ids: PIPE_IDS, plan: p,
        label: `Pipeline: ${order.length} Schritte · ${n} Firmen`,
      });
      $("#pipelinePanel").classList.add("hidden");
      CUST.selected.clear();
      await showJobProgressNow();
    } catch (e) {
      toast(`Pipeline konnte nicht gestartet werden: ${e.message}`, "error");
    } finally { btn.disabled = false; btn.textContent = "Pipeline starten"; }
  }

  async function runEnrichAction() {
    await runEnrichForIds([...CUST.selected]);
  }

  async function runEnrichForFilter() {
    await runEnrichForIds(await filteredCompanyIds());   // every company under the current filter
  }

  async function runDeleteAction() {
    const ids = [...CUST.selected];
    if (!ids.length) return;
    const names = ids.slice(0, 5).map(id => CUST.lastRows.find(r => r.id === id)?.name).filter(Boolean);
    const preview = names.join(", ") + (ids.length > names.length ? ", …" : "");
    if (!await appConfirm(
      `Delete ${ids.length} compan${ids.length === 1 ? "y" : "ies"} and ALL their collected data?\n\n${preview}\n\nThis cannot be undone.`,
      { title: "Delete companies", confirmText: `Delete ${ids.length}`, danger: true })) return;
    const strip = $("#jobStrip");
    strip.className = "job-strip running";
    for (let i = 0; i < ids.length; i++) {
      strip.innerHTML = `<span class="spinner"></span><b>Deleting…</b><span class="job-strip-note">${i + 1}/${ids.length}</span>`;
      try { await api(`/api/companies/${ids[i]}`, "DELETE"); }
      catch (e) { alert(`Could not delete company #${ids[i]}: ${e.message}`); break; }
    }
    strip.innerHTML = `✓ <b>Deleted ${ids.length} compan${ids.length === 1 ? "y" : "ies"}</b>
      <span class="spacer"></span><button class="btn btn-sm btn-ghost job-strip-dismiss">Dismiss</button>`;
    strip.className = "job-strip done";
    $(".job-strip-dismiss", strip).addEventListener("click", () => strip.classList.add("hidden"));
    CUST.selected.clear();
    expandedPages.clear();
    await loadCustomers();
    loadState();   // dashboard table shares this data
  }

  async function _generateReport(body, btn, successMsg) {
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = "Generating…";
    try {
      const { filename } = await api("/api/reports/generate", "POST", body);
      toast(successMsg, "info");
      LAST_REPORT = { filename, filters: body.filters || {}, reportType: body.report || "full" };
      // Inline panel: download AND email it right here — no hop to the Reports tab.
      await showReportReadyPanel(filename, successMsg);
      try { await loadReports(); } catch { /* Reports tab refresh is best-effort */ }
    } catch (e) {
      toast(`Report failed: ${e.message}`, "error");
    } finally {
      btn.disabled = false; btn.textContent = orig;
    }
  }

  // Report for the hand-picked SELECTION (scoped by ids -> "Auswahl: N Firmen").
  async function runReportForSelected() {
    const ids = [...CUST.selected];
    if (!ids.length) return;
    await _generateReport({ report: "full", filters: { ids } }, $("#custReportBtn"),
      `✓ Report generated for ${ids.length} ${ids.length === 1 ? "company" : "companies"} — also under Reports`);
  }

  // Report for the whole FILTERED set (scoped by the filter itself -> the
  // "Gefiltert nach: …" scope banner shows the actual filter, no selection needed).
  async function runReportForFilter() {
    const n = CUST.total || 0;
    await _generateReport({ report: "full", filters: currentCustomerFilters() }, $("#custReportAllBtn"),
      `✓ Report generated for the current filter (${n} ${n === 1 ? "company" : "companies"}) — the filter is shown in the report header`);
  }

  // ---------------- company detail drawer ----------------

  function drawerKv(label, value) {
    return `<div class="drawer-kv"><dt>${esc(label)}</dt><dd>${value || '<span class="muted">—</span>'}</dd></div>`;
  }

  function closeCompanyDrawer() {
    $("#companyDrawer").classList.add("hidden");
    $("#drawerBackdrop").classList.add("hidden");
  }

  // ---------------- enrichment (drawer section + review queue) ----------------
  function enrichFieldsHtml(e) {
    const f = e.fields || {};
    const prov = e.provenance || {};
    // every value shows WHERE it came from — hover gives the source + the quote
    const kv = (label, value, key) => {
      if (value === undefined || value === null || value === "" ||
          (Array.isArray(value) && !value.length)) return "";
      const p = prov[key];
      const tip = p ? `${p.source}${p.confidence ? ` · ${Math.round(p.confidence * 100)}%` : ""}${p.evidence ? ` · „${p.evidence}"` : ""}` : "";
      const shown = Array.isArray(value) ? value.join(", ") : value;
      return `<div class="drawer-kv"><dt>${esc(label)}</dt><dd${tip ? ` title="${esc(tip)}"` : ""}>${esc(String(shown))}${p ? ` <span class="tag">${esc(p.source)}</span>` : ""}</dd></div>`;
    };
    const solarlux = f.mentions_solarlux === true
      ? `<span class="tag tag-saved">nennt Solarlux</span>`
      : (f.mentions_solarlux === false ? `<span class="tag">nennt Solarlux nicht</span>` : "");
    const comps = (f.competitor_brands || []).length
      ? `<span class="tag tag-filtered">Wettbewerber: ${esc(f.competitor_brands.join(", "))}</span>` : "";
    const body = [
      kv("Beschreibung", f.description_de, "description_de"),
      kv("Produkte", f.products, "products"),
      kv("Gegründet", f.founded_year, "founded_year"),
      kv("Größe", f.employee_hint, "employee_hint"),
      kv("Rechtsform", f.legal_form, "legal_form"),
      kv("Einsatzgebiet", f.service_area, "service_area"),
    ].join("");
    return `${solarlux || comps ? `<p style="margin:0 0 8px">${solarlux} ${comps}</p>` : ""}
      ${body ? `<dl class="drawer-grid">${body}</dl>` : `<p class="hint">Noch keine Felder extrahiert.</p>`}
      <p class="hint" style="margin-top:6px">Status: <b>${esc(ENRICH_STATUS_LABEL[e.status] || e.status || "—")}</b>${
        e.website_source ? ` · Website-Quelle: ${esc(e.website_source)}${e.website_validated_by ? ` (${esc(e.website_validated_by)})` : ""}` : ""}${
        e.enriched_at ? ` · ${esc(e.enriched_at)}` : ""}${e.error ? ` · <span class="status-error">${esc(e.error)}</span>` : ""}</p>`;
  }

  // needs_review: show each unproven candidate with why it failed + accept/reject.
  // Same plausibility rule as the backend: own-data candidates always, search
  // hits only when a name signal ties them to the company — unrelated portals
  // stay in the audit blob but are not offered for review.
  function enrichCandidatesHtml(e) {
    const worthy = c => c && c.domain && (
      ["email_domain", "sap_salvaged"].includes(c.origin)
      || c.signals?.name_in_text || c.signals?.name_in_domain);
    const cands = (e.website_candidates || []).filter(worthy);
    if (e.status !== "needs_review" || !cands.length) return "";
    return `<div class="enrich-review">
      <p class="hint"><b>Website-Vorschläge</b> — nicht automatisch bestätigt, weil Telefon/PLZ/Straße auf der Seite nicht gefunden wurden. Bitte prüfen:</p>
      ${cands.map(c => `
        <div class="page-item" data-domain="${esc(c.domain)}">
          <span><a class="link" href="https://${esc(c.domain)}" target="_blank" rel="noopener">${esc(c.domain)}</a>
            <span class="muted small">${esc(c.origin || "")}${c.reachable === false ? " · nicht erreichbar" : ""}</span></span>
          <button class="btn btn-sm enrich-accept">Das ist die richtige</button>
        </div>`).join("")}
      <button class="btn btn-sm btn-ghost enrich-reject" style="margin-top:6px">Keine davon</button>
    </div>`;
  }

  async function loadDrawerEnrichment(id) {
    const box = $("#drawerEnrichBody");
    if (!box) return;
    try {
      const e = await api(`/api/companies/${id}/enrichment`);
      box.innerHTML = enrichFieldsHtml(e) + enrichCandidatesHtml(e);
      $$(".enrich-accept", box).forEach(btn => btn.addEventListener("click", async () => {
        const domain = btn.closest("[data-domain]").dataset.domain;
        btn.disabled = true; btn.textContent = "Übernehme…";
        try {
          await api(`/api/companies/${id}/enrichment/accept`, "POST", { page_id: domain });
          toast(`✓ ${domain} übernommen.`, "info");
          await loadDrawerEnrichment(id); await loadCustomers();
        } catch (err) { toast(`Fehlgeschlagen: ${err.message}`, "error"); btn.disabled = false; btn.textContent = "Das ist die richtige"; }
      }));
      const rej = $(".enrich-reject", box);
      if (rej) rej.addEventListener("click", async () => {
        await api(`/api/companies/${id}/enrichment/reject`, "POST");
        toast("Vorschläge verworfen.", "info");
        await loadDrawerEnrichment(id); await loadCustomers();
      });
    } catch (e) {
      box.innerHTML = `<p class="hint status-error">Konnte Firmeninfos nicht laden: ${esc(e.message)}</p>`;
    }
  }

  async function openCompanyDrawer(id) {
    const drawer = $("#companyDrawer");
    $("#drawerBackdrop").classList.remove("hidden");
    drawer.classList.remove("hidden");
    drawer.innerHTML = `<div class="drawer-body"><p class="hint">Loading…</p></div>`;

    let detail = null;
    try { detail = await api(`/api/companies/${id}/detail`); } catch (e) { /* untracked is fine */ }
    // works from ANY tab — prefer the Explorer's loaded row (may not exist yet
    // when the drawer opens from the dashboard), else the API's copy
    const row = (CUST.lastRows || []).find(r => r.id === id) || detail?.company;
    if (!row) { closeCompanyDrawer(); alert("Could not load this company."); return; }
    const m = detail?.metric;
    const st = row.resolution_status;

    const revRow = ["y0", "y1", "y2", "y3", "y4"].map((k, i) =>
      `<div class="drawer-rev"><span>${i === 0 ? "Akt. Jahr" : "-" + i}</span><b>${eur(row["revenue_" + k])}</b></div>`).join("");

    const website = row.website_domain
      ? `<a class="link" href="${esc(/^https?:\/\//.test(row.website_domain) ? row.website_domain : "https://" + row.website_domain)}" target="_blank">${esc(row.website_domain)}</a>` : "";

    const adBlock = m && m.has_data ? `
        ${drawerKv("Active ads", `<b>${m.total_active_ads}</b> (Meta ${m.meta_active_ads ?? 0} · Google ${m.google_active_ads ?? 0})`)}
        ${drawerKv("New this week", m.new_ads ?? "—")}
        ${drawerKv("Score", m.score != null ? Math.round(m.score) + "/100" : "—")}
        ${drawerKv("Est. spend / wk", m.spend_low != null ? `${eur(m.spend_low)} – ${eur(m.spend_high)}` : "—")}
        ${drawerKv("Products", esc((m.products || []).join(", ")))}`
      : `<p class="hint">No ad data yet — run an <b>Ad lookup</b> on this company to fetch its ads.</p>`;

    // the actual current ad copies (latest tracked week), newest first
    const weekAds = (detail?.week?.ads || []).filter(a => a.ad_text || a.ad_library_url);
    const adListHtml = weekAds.slice(0, 6).map(a => `
      <div class="drawer-ad">
        <div class="drawer-ad-head">
          <span class="role-badge">${esc(CATEGORY_LABELS[a.category] || a.category || "ad")}</span>
          ${a.start_date ? `<span class="muted">seit ${esc(a.start_date)}</span>` : ""}
          <span class="spacer"></span>
          ${a.ad_library_url ? `<a class="link" href="${esc(a.ad_library_url)}" target="_blank">Ad Library ↗</a>` : ""}
        </div>
        <div class="drawer-ad-text">${esc((a.ad_text || "").slice(0, 200)) || `<span class="muted">(kein Anzeigentext)</span>`}</div>
      </div>`).join("")
      + (weekAds.length > 6 ? `<p class="hint">+ ${weekAds.length - 6} weitere Anzeigen in dieser Woche</p>` : "");

    // candidate alternatives the identity check found — the review pick-list
    const cands = detail?.company?.candidates || [];
    const candHtml = cands.length ? `
        <div class="drawer-section">
          <h3>Candidates — is one of these the right page?</h3>
          <p class="hint" style="margin:0 0 10px">Ranked by the identity check. “Use” sets it as the verified page (protected from auto-changes).</p>
          ${cands.map((cand, i) => {
            const uri = cand.profile_uri || (cand.page_id ? fbPageUrl(cand.page_id) : "#");
            const sig = [
              cand.site_match ? `<span class="candidate-flag flag-ok">✓ website</span>` : "",
              cand.city_match ? `<span class="candidate-flag flag-ok">✓ city</span>` : "",
              cand.blocked ? `<span class="candidate-flag flag-warn">⚠ excluded</span>` : "",
              cand.active_ad_count != null ? `<span class="muted">${cand.active_ad_count} active ads</span>` : "",
              `<span class="muted">match ${Math.round((cand.similarity || 0) * 100)}%</span>`,
            ].filter(Boolean).join(" · ");
            // match on numeric id when present, else on the profile URL — so a
            // handle-only confirm (page_id null) doesn't tag every id-less
            // candidate as "current"
            const isCurrent = (!!cand.page_id && String(cand.page_id) === String(row.page_id || ""))
              || (!!row.page_url && !!cand.profile_uri && cand.profile_uri === row.page_url);
            return `<div class="candidate-item">
              <div style="min-width:0">
                <a class="link" href="${esc(uri)}" target="_blank">${esc(cand.name || cand.page_id)}</a>
                ${isCurrent ? `<span class="role-badge">current</span>` : ""}
                <div class="page-meta">${esc(cand.category || "")}${cand.category ? " · " : ""}${sig}</div>
              </div>
              ${cand.page_id && !isCurrent
                ? `<button class="btn btn-sm btn-primary drawer-use-cand" data-i="${i}">Use</button>` : ""}
            </div>`;
          }).join("")}
        </div>` : "";

    const isReviewable = st === "ambiguous" || st === "no_ads_found";

    drawer.innerHTML = `
      <div class="drawer-head">
        <div>
          <span class="id-status id-status-${st}">${esc(ID_STATUS_LABEL[st] || st)}</span>
          <h2>${esc(row.name)}</h2>
          <span class="muted">SAP ${esc(row.sap_number || "—")}</span>
        </div>
        <div class="drawer-head-actions">
          ${isReviewable ? `<button class="btn btn-sm drawer-next-review" title="Jump to the next company needing review on this page">Next to review →</button>` : ""}
          <button class="btn btn-ghost drawer-close" title="Close">✕</button>
        </div>
      </div>
      <div class="drawer-body">
        <div class="drawer-section">
          <div class="drawer-section-head">
            <h3>Identity — Meta page</h3>
            <button id="drawerRecheckBtn" class="btn btn-sm" title="Re-run the identity check for this company (website + Google + AI)">↻ Recheck</button>
          </div>
          <div class="drawer-idrow">${idFbCell(row)}</div>
          <div class="inline-form" style="margin-top:10px">
            <input type="text" id="drawerPageId" placeholder="Page ID" value="${esc(row.page_id || "")}" style="flex:1;min-width:110px">
            <input type="text" id="drawerPageName" placeholder="Page name" value="${esc(row.page_name || "")}" style="flex:1.4;min-width:130px">
            ${st === "locked"
              ? `<button id="drawerUnlockBtn" class="btn btn-sm">Unlock</button>`
              : `<button id="drawerLockBtn" class="btn btn-sm btn-primary" title="Save and freeze — never overwritten by automatic checks">🔒 Lock</button>`}
            ${(row.page_id || row.page_url)
              ? `<button id="drawerUnlinkBtn" class="btn btn-sm btn-danger" title="Remove this wrong page — keeps the candidate list for review">✕ Unlink</button>` : ""}
          </div>
          <p class="hint">Locking freezes this page as verified — automatic identity checks will never overwrite it.</p>
        </div>
        ${candHtml}
        ${dossierSection(detail?.dossier)}
        ${crmSection(detail?.company || row)}
        <div class="drawer-section">
          <h3>Ad activity</h3>
          ${adBlock}
        </div>
        ${weekAds.length ? `
        <div class="drawer-section">
          <h3>Current ads (${weekAds.length})</h3>
          ${adListHtml}
        </div>` : ""}
        <div class="drawer-section">
          <h3>Score</h3>
          <dl class="drawer-grid">
            ${drawerKv("Kundenstatus", customerStateChip(row.customer_state))}
            ${drawerKv("Fit zum Kundenprofil", row.fit_score != null ? fitCell(row.fit_score) : "")}
            ${drawerKv("Chance (Divergenz)", row.opportunity_score != null ? String(Math.round(row.opportunity_score)) : "")}
            ${drawerKv("Ziel-Score", row.target_score != null ? `<b>${Math.round(row.target_score)}</b>` : "")}
          </dl>
          <div id="drawerFitBreakdown"></div>
        </div>
        <div class="drawer-section" id="drawerEnrichSection">
          <div class="drawer-section-head">
            <h3>Firmeninfos (angereichert)</h3>
            <button id="drawerEnrichBtn" class="btn btn-sm" title="Website finden (falls fehlend) und Firmeninfos von der eigenen Website lesen">✨ Anreichern</button>
          </div>
          <div id="drawerEnrichBody"><p class="hint">Lade…</p></div>
        </div>
        <div class="drawer-section">
          <h3>Master data</h3>
          <dl class="drawer-grid">
            ${drawerKv("KV", esc(row.kv))}
            ${drawerKv("Segment", esc(row.segment))}
            ${drawerKv("Untersegment", esc(row.sub_segment))}
            ${drawerKv("Vertriebsweg", esc(row.sales_channel))}
            ${drawerKv("Adresse", esc([row.street, [row.postal_code, row.city].filter(Boolean).join(" ")].filter(Boolean).join(", ")))}
            ${drawerKv("Land", esc(row.country))}
            ${drawerKv("Telefon", esc(row.phone))}
            ${drawerKv("E-Mail", row.email ? `<a class="link" href="mailto:${esc(row.email)}">${esc(row.email)}</a>` : "")}
            ${drawerKv("Website", website)}
          </dl>
        </div>
        <div class="drawer-section">
          <h3>Umsatz</h3>
          <div class="drawer-revs">${revRow}</div>
        </div>
        <div class="drawer-section">
          <h3>Edit company</h3>
          <div class="inline-form">
            <input type="text" id="drawerName" value="${esc(row.name)}" style="flex:2;min-width:170px">
            <input type="text" id="drawerDomain" placeholder="website domain" value="${esc(row.website_domain || "")}" style="flex:1.4;min-width:140px">
            <button id="drawerSaveBtn" class="btn btn-sm">Save</button>
          </div>
        </div>
      </div>`;

    $(".drawer-close", drawer).addEventListener("click", closeCompanyDrawer);
    const refresh = async () => { await loadCustomers(); closeCompanyDrawer();
      openCompanyDrawer(id); };   // drawer no longer depends on the Explorer's page

    $("#drawerLockBtn", drawer)?.addEventListener("click", async () => {
      const pid = $("#drawerPageId").value.trim();
      if (!pid) { alert("Enter a page ID to lock."); return; }
      try { await api(`/api/companies/${id}/lock`, "POST",
        { page_id: pid, page_name: $("#drawerPageName").value.trim() || null }); await refresh(); }
      catch (e) { alert(e.message); }
    });
    $("#drawerUnlockBtn", drawer)?.addEventListener("click", async () => {
      if (!await appConfirm("Unlock this identity? Automatic checks may overwrite it again.",
        { title: "Unlock identity", confirmText: "Unlock" })) return;
      try { await api(`/api/companies/${id}/unlock`, "POST"); await refresh(); }
      catch (e) { alert(e.message); }
    });
    $("#drawerUnlinkBtn", drawer)?.addEventListener("click", async () => {
      if (!await appConfirm(
        `Unlink the current page from “${row.name}”?\n\nThe wrong link is removed and the company goes back to review — its candidate list is kept so you can pick the right one or recheck.`,
        { title: "Unlink page", confirmText: "Unlink", danger: true })) return;
      try { await api(`/api/companies/${id}/unlink`, "POST"); toast("Page unlinked.", "info"); await refresh(); }
      catch (e) { alert(e.message); }
    });
    $("#drawerSaveBtn", drawer).addEventListener("click", async () => {
      try { await api(`/api/companies/${id}`, "PATCH",
        { name: $("#drawerName").value.trim(), website_domain: $("#drawerDomain").value.trim() || null });
        await refresh(); }
      catch (e) { alert(e.message); }
    });

    // Recheck identity — re-run the full pipeline for this one company, live.
    $("#drawerRecheckBtn", drawer)?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true; btn.textContent = "Checking…";
      try {
        const r = await api(`/api/companies/${id}/identity-check`, "POST");
        const label = { confirmed: "Meta page found", locked: "Meta page locked", ambiguous: "Meta page still unclear",
          no_ads_found: "no Meta page found", skipped_locked: "Meta page locked (skipped)" }[r.status] || r.status;
        toast(`Identity rechecked → ${label}${r.page_name ? " · " + r.page_name : ""}`, "info");
        await refresh();
      } catch (err) {
        btn.disabled = false; btn.textContent = "↻ Recheck";
        alert(err.message);
      }
    });

    // Score breakdown ("Warum dieser Fit?") — per-feature bars from the applied profile.
    (async () => {
      const box = $("#drawerFitBreakdown");
      if (!box || row.fit_score == null) return;
      try {
        const full = await api(`/api/companies/${id}`);
        const feats = full?.fit_breakdown?.features || [];
        if (!feats.length) return;
        box.innerHTML = `<p class="hint" style="margin:8px 0 4px"><b>Warum dieser Fit?</b> (Übereinstimmung mit den Gewinnern je Merkmal)</p>`
          + feats.map(f => `
            <div class="fit-row" title="Gewicht ${f.weight}">
              <span class="fit-row-label">${esc(f.label)}: ${esc(String(f.value))}</span>
              <span class="fit-bar"><span class="fit-bar-fill" style="width:${Math.round(f.points * 100)}%"></span></span>
              <span class="fit-row-pct">${Math.round(f.points * 100)}%</span>
            </div>`).join("");
      } catch { /* breakdown is optional */ }
    })();

    // Enrichment: load this company's enriched facts, and allow a manual re-run.
    loadDrawerEnrichment(id);
    $("#drawerEnrichBtn", drawer)?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true; btn.textContent = "Anreichern…";
      try {
        const r = await api(`/api/companies/${id}/enrich`, "POST");
        toast(r.website ? `✓ ${r.website} (${r.validated_by}) · ${r.fields_found} Felder`
                        : `${ENRICH_STATUS_LABEL[r.status] || r.status}`,
              r.status === "enriched" ? "info" : "error");
        await loadDrawerEnrichment(id);
        await loadCustomers();
      } catch (err) {
        toast(`Anreichern fehlgeschlagen: ${err.message}`, "error");
      } finally { btn.disabled = false; btn.textContent = "✨ Anreichern"; }
    });

    // Use a candidate — set it as the verified (human-confirmed) page.
    $$(".drawer-use-cand", drawer).forEach(btn => btn.addEventListener("click", async () => {
      const cand = cands[Number(btn.dataset.i)];
      if (!cand?.page_id) return;
      btn.disabled = true; btn.textContent = "…";
      try {
        await api(`/api/companies/${id}/confirm`, "POST",
          { page_id: String(cand.page_id), page_name: cand.name || null, category: cand.category || null });
        toast(`Set “${cand.name || cand.page_id}” as ${row.name}'s page.`, "info");
        await refresh();
      } catch (e) { btn.disabled = false; btn.textContent = "Use"; alert(e.message); }
    }));

    // Next to review — jump to the next ambiguous/no-page company on this page.
    $(".drawer-next-review", drawer)?.addEventListener("click", () => {
      const list = CUST.lastRows || [];
      const start = list.findIndex(r => r.id === id);
      const next = list.slice(start + 1).find(r => ["ambiguous", "no_ads_found"].includes(r.resolution_status));
      if (next) openCompanyDrawer(next.id);
      else toast("No more companies to review on this page.", "info");
    });
  }

  async function downloadExport(bodyObj) {
    const r = await fetch("/api/customers/export", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(bodyObj),
    });
    if (!r.ok) { alert("Export failed"); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "customers_export.xlsx";
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  }

  function wireCustomers() {
    $("#customerImportBtn").addEventListener("click", async () => {
      const fileInput = $("#customerFile");
      const result = $("#customerImportResult");
      if (!fileInput.files.length) { result.textContent = "Choose a file first."; return; }
      const btn = $("#customerImportBtn");
      btn.disabled = true; btn.textContent = "Importing…"; result.textContent = "";
      try {
        const fd = new FormData();
        fd.append("file", fileInput.files[0]);
        const r = await fetch("/api/customers/import", { method: "POST", body: fd });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || "Import failed");
        result.textContent = `Imported ${data.received} rows — ${data.inserted} new, ${data.updated} updated.`
          + (data.warnings && data.warnings.length ? ` (${data.warnings.join("; ")})` : "");
        CUST.page = 1;
        await loadCustomerFilterOptions();
        await loadCustomers();
      } catch (e) {
        result.textContent = `Import failed: ${e.message}`;
      } finally { btn.disabled = false; btn.textContent = "Import"; }
    });

    // No "Apply" button — every filter applies itself: text/number fields on
    // Enter, dropdowns (native <select> or the checkbox-dropdown widgets) the
    // moment a value is picked or changed.
    const applyNow = () => { CUST.page = 1; loadCustomers(); };
    const applyOnEnter = (sel) => $(sel).addEventListener("keydown", (e) => { if (e.key === "Enter") applyNow(); });
    const applyOnChange = (sel) => $(sel).addEventListener("change", applyNow);

    CUST_DROP.status = mountCheckDropdown("custStatusDrop", {
      placeholder: "Identity status", labelFor: (v) => ID_FILTER_LABEL[v] || v, onChange: applyNow });
    CUST_DROP.status.setOptions(ID_FILTER_OPTIONS);
    CUST_DROP.kv = mountCheckDropdown("custKvDrop", { placeholder: "All KV", onChange: applyNow });
    CUST_DROP.segment = mountCheckDropdown("custSegmentDrop", { placeholder: "All segments", onChange: applyNow });
    CUST_DROP.subSegment = mountCheckDropdown("custSubSegmentDrop", { placeholder: "All sub-segments", onChange: applyNow });
    CUST_DROP.salesChannel = mountCheckDropdown("custChannelDrop", { placeholder: "All channels", onChange: applyNow });
    CUST_DROP.country = mountCheckDropdown("custCountryDrop", { placeholder: "All countries", onChange: applyNow });
    CUST_DROP.excludeKv = mountCheckDropdown("custExcludeKvDrop", { placeholder: "Exclude KV", onChange: applyNow });
    CUST_DROP.excludeSegment = mountCheckDropdown("custExcludeSegmentDrop", { placeholder: "Exclude segment", onChange: applyNow });
    CUST_DROP.excludeSubSegment = mountCheckDropdown("custExcludeSubSegmentDrop", { placeholder: "Exclude sub-segment", onChange: applyNow });

    applyOnEnter("#custSearch");
    applyOnEnter("#custRevenueMin");
    applyOnEnter("#custRevenueMax");
    applyOnChange("#custRevenueHistory");
    applyOnChange("#custHasWebsite");
    applyOnChange("#custTracked");
    applyOnChange("#custAdActivity");
    applyOnChange("#custNoWebsite");
    applyOnChange("#custEnrichStatus");
    applyOnChange("#custCustomerState");
    applyOnChange("#custRelevance");
    applyOnChange("#custFit");
    applyOnChange("#custDecisionRole");
    applyOnChange("#custFitMin");

    $("#custClearBtn").addEventListener("click", () => {
      $("#custSearch").value = "";
      CUST_DROP.status.clear();
      CUST_DROP.kv.clear(); CUST_DROP.segment.clear(); CUST_DROP.subSegment.clear();
      CUST_DROP.salesChannel.clear(); CUST_DROP.country.clear();
      CUST_DROP.excludeKv.clear(); CUST_DROP.excludeSegment.clear(); CUST_DROP.excludeSubSegment.clear();
      $("#custRevenueMin").value = ""; $("#custRevenueMax").value = ""; $("#custRevenueHistory").value = "";
      $("#custHasWebsite").checked = false; $("#custTracked").value = "";
      $("#custAdActivity").value = "";
      $("#custNoWebsite").checked = false; $("#custEnrichStatus").value = "";
      $("#custCustomerState").value = ""; $("#custFitMin").value = "";
      $("#custRelevance").value = ""; $("#custDecisionRole").value = "";
      $("#custFit").value = "";
      applyDefaultExclusion();   // Zurücksetzen = back to DEFAULT, incl. "ohne Private Endkunden"
      CUST.page = 1; loadCustomers();
    });

    // Infinite scroll replaces the pager: the sentinel under the table triggers
    // the next page ~600px before it becomes visible. IntersectionObserver is
    // the primary signal; a throttled scroll listener backs it up because IO
    // callbacks are throttled/paused in backgrounded or embedded windows.
    const _sentinelNear = () => {
      const el = $("#custScrollSentinel");
      if (!el || !$("#tab-customers").classList.contains("active")) return false;
      return el.getBoundingClientRect().top < window.innerHeight + 600;
    };
    new IntersectionObserver(entries => {
      if (entries.some(e => e.isIntersecting)) loadMoreCustomers();
    }, { rootMargin: "600px" }).observe($("#custScrollSentinel"));
    let _scrollTick = false;
    window.addEventListener("scroll", () => {
      if (_scrollTick) return;
      _scrollTick = true;
      setTimeout(() => { _scrollTick = false; if (_sentinelNear()) loadMoreCustomers(); }, 150);
    }, { passive: true });

    // All columns show by default; this hides the secondary ones on demand.
    $("#custColsBtn").addEventListener("click", () => {
      const hidden = $("#customersTable").classList.toggle("hide-extra");
      $("#custColsBtn").textContent = hidden ? "Alle Spalten ▾" : "Weniger Spalten ▴";
    });

    // "⚡ Aktionen (gefiltert)" dropdown — the five filtered-set actions in one
    // place. The item buttons keep their original ids/handlers; this wiring
    // only opens/closes the panel (and closes it once an action is picked).
    const actionsPanel = $("#custActionsMenu .action-menu-panel");
    $("#custActionsBtn").addEventListener("click", (e) => {
      e.stopPropagation();
      actionsPanel.classList.toggle("hidden");
    });
    actionsPanel.addEventListener("click", () => actionsPanel.classList.add("hidden"));
    document.addEventListener("click", () => actionsPanel.classList.add("hidden"));

    // ---------------- column-header menus: click a header -> sort + THAT
    // column's filter, Excel-style. The controls proxy into the hidden
    // state-holders (#custFilterState), so currentCustomerFilters() and all
    // report/export/job flows keep working unchanged. ----------------
    const _setVal = (sel, v) => { const el = $(sel); el.value = v; el.dispatchEvent(new Event("change", { bubbles: true })); };
    const _chk = (sel, on) => { $(sel).checked = on; };

    function _optionsHtml(el) {
      return [...$(el).options].map(o => `<option value="${esc(o.value)}"${$(el).value === o.value ? " selected" : ""}>${esc(o.textContent)}</option>`).join("");
    }
    function _checkListHtml(values, selected, cls) {
      const sel = new Set(selected);
      return `<div class="thm-list">` + values.map(v => `
        <label class="thm-item"><input type="checkbox" class="${cls}" value="${esc(v)}" ${sel.has(v) ? "checked" : ""}> <span>${esc(v)}</span></label>`).join("") + `</div>`;
    }
    function _incExcSection(title, dropGetter, values, cls) {
      return `<div class="thm-sec"><div class="thm-sec-title">${title}</div>${_checkListHtml(values, dropGetter().getSelected(), cls)}</div>`;
    }

    const COL_MENUS = {
      name: () => `<div class="thm-sec"><div class="thm-sec-title">Suche</div>
        <input type="text" class="thm-input" id="thmSearch" value="${esc($("#custSearch").value)}" placeholder="Name oder SAP…"></div>`,
      kunde: () => `<div class="thm-sec"><div class="thm-sec-title">Kundenstatus</div>
        <select class="thm-input" id="thmProxySel" data-target="#custCustomerState">${_optionsHtml("#custCustomerState")}</select></div>`,
      fit: () => `<div class="thm-sec"><div class="thm-sec-title">Fit mindestens (0–100)</div>
        <input type="number" min="0" max="100" class="thm-input" id="thmFitMin" value="${esc($("#custFitMin").value)}"></div>`,
      anzeigen: () => `<div class="thm-sec"><div class="thm-sec-title">Anzeigen-Aktivität</div>
        <select class="thm-input" id="thmProxySel" data-target="#custAdActivity">${_optionsHtml("#custAdActivity")}</select></div>`,
      fb: () => _incExcSection("Meta-Identität", () => CUST_DROP.status,
            ID_FILTER_OPTIONS, "thm-inc-status")
        + `<div class="thm-sec"><div class="thm-sec-title">Tracking</div>
        <select class="thm-input" id="thmProxySel" data-target="#custTracked">${_optionsHtml("#custTracked")}</select></div>`,
      website: () => `<div class="thm-sec"><div class="thm-sec-title">Website</div>
        <select class="thm-input" id="thmWebsite">
          <option value="">alle</option>
          <option value="with"${$("#custHasWebsite").checked ? " selected" : ""}>nur mit Website</option>
          <option value="without"${$("#custNoWebsite").checked ? " selected" : ""}>nur ohne Website</option>
        </select></div>
        <div class="thm-sec"><div class="thm-sec-title">Anreicherung</div>
        <select class="thm-input" id="thmProxySel" data-target="#custEnrichStatus">${_optionsHtml("#custEnrichStatus")}</select></div>`,
      umsatz: () => `<div class="thm-sec"><div class="thm-sec-title">Umsatz akt. Jahr (€)</div>
        <div class="thm-row"><input type="number" class="thm-input" id="thmRevMin" placeholder="von" value="${esc($("#custRevenueMin").value)}">
        <input type="number" class="thm-input" id="thmRevMax" placeholder="bis" value="${esc($("#custRevenueMax").value)}"></div></div>
        <div class="thm-sec"><div class="thm-sec-title">Umsatz-Historie</div>
        <select class="thm-input" id="thmProxySel" data-target="#custRevenueHistory">${_optionsHtml("#custRevenueHistory")}</select></div>`,
      kv: () => _incExcSection("Nur diese KV", () => CUST_DROP.kv, CUST_OPTS.kv, "thm-inc-kv")
        + _incExcSection("Ausschließen", () => CUST_DROP.excludeKv, CUST_OPTS.kv, "thm-exc-kv"),
      segment: () => _incExcSection("Nur diese Segmente", () => CUST_DROP.segment, CUST_OPTS.segment, "thm-inc-seg")
        + _incExcSection("Ausschließen", () => CUST_DROP.excludeSegment, CUST_OPTS.segment, "thm-exc-seg")
        // Only filled for Architekten/Planer, and this is the menu someone is
        // already in when they narrow to that segment.
        + `<div class="thm-sec"><div class="thm-sec-title">Solarlux-Passung (Verarbeiter)</div>
        <select class="thm-input" id="thmProxySel" data-target="#custFit">${_optionsHtml("#custFit")}</select></div>
        <div class="thm-sec"><div class="thm-sec-title">Solarlux-Relevanz (Architekten)</div>
        <select class="thm-input" id="thmProxySel" data-target="#custRelevance">${_optionsHtml("#custRelevance")}</select></div>
        <div class="thm-sec"><div class="thm-sec-title">Entscheidungsrolle</div>
        <select class="thm-input" id="thmProxySel" data-target="#custDecisionRole">${_optionsHtml("#custDecisionRole")}</select></div>`,
      subseg: () => _incExcSection("Nur diese Untersegmente", () => CUST_DROP.subSegment, CUST_OPTS.sub_segment, "thm-inc-sub")
        + _incExcSection("Ausschließen", () => CUST_DROP.excludeSubSegment, CUST_OPTS.sub_segment, "thm-exc-sub"),
      kanal: () => _incExcSection("Nur diese Vertriebswege", () => CUST_DROP.salesChannel, CUST_OPTS.sales_channel, "thm-inc-chan"),
      land: () => _incExcSection("Nur diese Länder", () => CUST_DROP.country, CUST_OPTS.country, "thm-inc-land"),
    };
    const _CHECK_BINDINGS = {
      "thm-inc-status": () => CUST_DROP.status, "thm-inc-kv": () => CUST_DROP.kv,
      "thm-exc-kv": () => CUST_DROP.excludeKv, "thm-inc-seg": () => CUST_DROP.segment,
      "thm-exc-seg": () => CUST_DROP.excludeSegment, "thm-inc-sub": () => CUST_DROP.subSegment,
      "thm-exc-sub": () => CUST_DROP.excludeSubSegment, "thm-inc-chan": () => CUST_DROP.salesChannel,
      "thm-inc-land": () => CUST_DROP.country,
    };

    const thMenu = document.createElement("div");
    thMenu.id = "thMenu"; thMenu.className = "th-menu hidden";
    document.body.appendChild(thMenu);
    thMenu.addEventListener("click", e => e.stopPropagation());
    const closeThMenu = () => thMenu.classList.add("hidden");
    document.addEventListener("click", closeThMenu);
    document.addEventListener("keydown", e => { if (e.key === "Escape") closeThMenu(); });

    function openThMenu(th) {
      const col = th.dataset.col;
      const sortKey = th.dataset.sort;
      const label = ID_FILTER_LABEL;   // ensure closure keeps labels for status list
      let html = `<div class="thm-head">${esc(th.textContent.replace("▾", "").trim())}</div>`;
      if (sortKey) {
        html += `<div class="thm-sec thm-sort">
          <button class="btn btn-sm thm-sort-btn${CUST.sort === sortKey && CUST.direction === "asc" ? " btn-primary" : ""}" data-dir="asc">↑ Aufsteigend</button>
          <button class="btn btn-sm thm-sort-btn${CUST.sort === sortKey && CUST.direction === "desc" ? " btn-primary" : ""}" data-dir="desc">↓ Absteigend</button>
        </div>`;
      }
      if (COL_MENUS[col]) html += COL_MENUS[col]();
      thMenu.innerHTML = html;

      // position under the header, clamped to the viewport
      const r = th.getBoundingClientRect();
      thMenu.classList.remove("hidden");
      const w = Math.min(300, window.innerWidth - 24);
      thMenu.style.width = w + "px";
      thMenu.style.top = Math.round(r.bottom + 4) + "px";
      thMenu.style.left = Math.round(Math.min(r.left, window.innerWidth - w - 12)) + "px";

      // wiring — everything applies live via the hidden state-holders
      $$(".thm-sort-btn", thMenu).forEach(b => b.addEventListener("click", () => {
        CUST.sort = sortKey; CUST.direction = b.dataset.dir;
        closeThMenu(); loadCustomers();
      }));
      $$("#thmProxySel", thMenu).forEach(sel => sel.addEventListener("change",
        () => _setVal(sel.dataset.target, sel.value)));
      $("#thmSearch", thMenu)?.addEventListener("keydown", e => {
        if (e.key === "Enter") { $("#custSearch").value = e.target.value; closeThMenu(); applyNow(); }
      });
      $("#thmFitMin", thMenu)?.addEventListener("change", e => _setVal("#custFitMin", e.target.value));
      $("#thmWebsite", thMenu)?.addEventListener("change", e => {
        _chk("#custHasWebsite", e.target.value === "with");
        _chk("#custNoWebsite", e.target.value === "without");
        applyNow();
      });
      $("#thmRevMin", thMenu)?.addEventListener("change", e => { $("#custRevenueMin").value = e.target.value; applyNow(); });
      $("#thmRevMax", thMenu)?.addEventListener("change", e => { $("#custRevenueMax").value = e.target.value; applyNow(); });
      Object.entries(_CHECK_BINDINGS).forEach(([cls, getDrop]) => {
        const boxes = $$(`.${cls}`, thMenu);
        if (!boxes.length) return;
        boxes.forEach(cb => cb.addEventListener("change", () => {
          getDrop().setSelected($$(`.${cls}:checked`, thMenu).map(x => x.value));
          applyNow();
        }));
      });
    }

    // decorate headers: caret + click-to-open (replaces sort-on-click)
    $$("#customersTable thead th[data-col]").forEach(th => {
      th.classList.add("th-has-menu");
      th.insertAdjacentHTML("beforeend", ` <span class="th-caret">▾</span>`);
      th.addEventListener("click", (e) => {
        e.stopPropagation();
        const open = !thMenu.classList.contains("hidden") && thMenu.dataset.col === th.dataset.col;
        closeThMenu();
        if (!open) { thMenu.dataset.col = th.dataset.col; openThMenu(th); }
      });
    });
    $(".table-wrap", $("#tab-customers"))?.addEventListener("scroll", closeThMenu, { passive: true });

    // Select every row currently rendered on this page (in-memory, instant).
    function selectPage(checked) {
      $$(".cust-check", $("#customersTableBody")).forEach(cb => {
        const tr = cb.closest("tr"); const id = Number(tr.dataset.id);
        cb.checked = checked;
        if (checked) CUST.selected.add(id); else CUST.selected.delete(id);
        tr.classList.toggle("selected", checked);
      });
      $("#custSelectPage").checked = checked;   // keep the header box in sync
      updateSelectionUI();
    }
    $("#custSelectPage").addEventListener("change", (e) => selectPage(e.target.checked));

    // Select the ENTIRE filtered set, across all pages (one server call for ids).
    async function selectAllMatching() {
      const n = CUST.total || 0;
      if (!n) return;
      try {
        const { ids } = await api("/api/customers/select-top", "POST",
          { filters: currentCustomerFilters(), sort: CUST.sort, direction: CUST.direction, n });
        ids.forEach(id => CUST.selected.add(id));
        await loadCustomers();
      } catch (err) { alert(`Could not select all: ${err.message}`); }
    }

    // Far-left header menu: page vs. whole-filtered-set selection.
    const selMenu = $("#custSelMenu");
    const selMenuBtn = $("#custSelMenuBtn");
    function closeSelMenu() { selMenu.classList.add("hidden"); }
    selMenuBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const willOpen = selMenu.classList.contains("hidden");
      closeSelMenu();
      if (!willOpen) return;
      $("[data-sel='all']", selMenu).textContent =
        `Select all ${(CUST.total || 0).toLocaleString("de-DE")} matching`;
      const r = selMenuBtn.getBoundingClientRect();
      selMenu.classList.remove("hidden");            // measure before placing
      const mh = selMenu.offsetHeight;
      const below = r.bottom + 4;
      // flip above the button when it would overflow the bottom of the viewport
      selMenu.style.top = (below + mh > window.innerHeight ? r.top - mh - 4 : below) + "px";
      selMenu.style.left = `${r.left}px`;
    });
    selMenu.addEventListener("click", (e) => {
      const act = e.target.closest("button")?.dataset.sel;
      if (!act) return;
      if (act === "page") selectPage(true);
      else if (act === "all") selectAllMatching();
      else if (act === "none") { CUST.selected.clear(); loadCustomers(); }
      closeSelMenu();
    });
    document.addEventListener("click", closeSelMenu);

    $("#custSelectTopBtn").addEventListener("click", async () => {
      const n = Math.max(1, Number($("#custTopN").value) || 30);
      const { ids } = await api("/api/customers/select-top", "POST",
        { filters: currentCustomerFilters(), sort: CUST.sort, direction: CUST.direction, n });
      ids.forEach(id => CUST.selected.add(id));
      await loadCustomers();
    });

    $("#custClearSelBtn").addEventListener("click", () => { CUST.selected.clear(); loadCustomers(); });

    $("#custExportSelBtn").addEventListener("click", () =>
      downloadExport({ ids: [...CUST.selected] }));
    $("#custExportAllBtn").addEventListener("click", () =>
      downloadExport({ filters: currentCustomerFilters(), sort: CUST.sort, direction: CUST.direction }));

    // Act on the WHOLE filtered set (no manual selection needed):
    $("#custReportAllBtn").addEventListener("click", runReportForFilter);
    $("#custFetchAllBtn").addEventListener("click", async () => {
      await selectAllMatching();     // pull every filtered company id into the selection
      openFetchPlan();               // pre-flight estimate + confirm before any Apify spend
    });

    $("#custFetchBtn").addEventListener("click", openFetchPlan);
    $("#planRecalcBtn").addEventListener("click", refreshFetchPlan);
    $("#planCancelBtn").addEventListener("click", () => $("#fetchPlanPanel").classList.add("hidden"));
    $("#planConfirmBtn").addEventListener("click", confirmFetchPlan);

    // Save-as-report + inline send-after-generate wiring
    fillDaySelect($("#saveReportDay"));
    $("#custSaveReportBtn").addEventListener("click",
      () => openSaveReportPanel({ filters: currentCustomerFilters(), reportType: "full" }));
    $("#reportReadySendBtn").addEventListener("click", sendReportReady);
    $("#reportReadySaveBtn").addEventListener("click",
      () => openSaveReportPanel({ filters: (LAST_REPORT && LAST_REPORT.filters) || currentCustomerFilters(),
                                  reportType: (LAST_REPORT && LAST_REPORT.reportType) || "full" }));
    $("#reportReadyCloseBtn").addEventListener("click", () => $("#reportReadyPanel").classList.add("hidden"));
    $("#saveReportSaveBtn").addEventListener("click", saveReportDef);
    $("#saveReportCancelBtn").addEventListener("click", () => $("#saveReportPanel").classList.add("hidden"));

    // Komplett-Pipeline
    $("#custPipelineBtn").addEventListener("click", openPipelinePanel);
    $("#pipelineStartBtn").addEventListener("click", startPipeline);
    $("#pipelineCancelBtn").addEventListener("click", () => $("#pipelinePanel").classList.add("hidden"));
    ["#pipeEnrich", "#pipeIdentity", "#pipeAds", "#pipeAdsMeta", "#pipeAdsGoogle",
     "#pipeReport", "#pipeReportType", "#pipeSend"].forEach(sel =>
      $(sel).addEventListener("change", renderPipelineSummary));
    $("#pipeRecipients").addEventListener("change", renderPipelineSummary);

    // ICP tab
    $("#icpPreviewBtn").addEventListener("click", icpPreview);

    // selection actions + drawer chrome
    $("#custIdentityBtn").addEventListener("click", runIdentityAction);
    $("#custEnrichBtn").addEventListener("click", runEnrichAction);
    $("#custEnrichAllBtn").addEventListener("click", runEnrichForFilter);
    $("#custReportBtn").addEventListener("click", runReportForSelected);
    $("#custDeleteBtn").addEventListener("click", runDeleteAction);
    $("#custEditBtn").addEventListener("click", () => {
      const ids = [...CUST.selected];
      if (ids.length === 1) openCompanyDrawer(ids[0]);
    });
    $("#drawerBackdrop").addEventListener("click", closeCompanyDrawer);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeCompanyDrawer(); });
  }

  // ------------------------------------------------------------------ Fetch plan (pre-flight) + jobs
  let PLAN_COMPANY_IDS = [];

  function planSources() {
    const sources = [];
    if ($("#planSourceMeta").checked) sources.push("meta");
    if ($("#planSourceGoogle").checked) sources.push("google");
    return sources;
  }

  async function openFetchPlan() {
    $("#fetchPlanPanel").classList.remove("hidden");
    await refreshFetchPlan();
  }

  async function refreshFetchPlan() {
    const summaryBox = $("#fetchPlanSummary");
    summaryBox.textContent = "Calculating…";
    PLAN_COMPANY_IDS = [...CUST.selected];
    const sources = planSources();
    if (!PLAN_COMPANY_IDS.length || !sources.length) {
      summaryBox.innerHTML = `<p class="hint">${!sources.length ? "Pick at least one source." : "Nothing selected."}</p>`;
      $("#planConfirmBtn").disabled = true;
      return;
    }
    const est = await api("/api/fetch-jobs/estimate", "POST", { company_ids: PLAN_COMPANY_IDS, sources });
    // Per-source routing: Meta runs only where a page was found, Google only
    // where a website is set — so we show what will actually be fetched.
    const routeStats = [];
    if (est.meta_fetchable != null)
      routeStats.push(`<div><div class="stat-label">Meta pages ready</div><div class="stat-value">${est.meta_fetchable}</div></div>`);
    if (est.google_fetchable != null)
      routeStats.push(`<div><div class="stat-label">Websites (Google)</div><div class="stat-value">${est.google_fetchable}</div></div>`);
    summaryBox.innerHTML = `
      <div><div class="stat-label">Companies</div><div class="stat-value">${est.company_count}</div></div>
      ${routeStats.join("")}
      <div><div class="stat-label">Fetches to run</div><div class="stat-value">${est.total_units}</div></div>
      <div><div class="stat-label">Sources</div><div class="stat-value">${est.sources.join(" + ")}</div></div>
      <div><div class="stat-label">Est. time</div><div class="stat-value">${fmtDuration(est.est_seconds_low)}–${fmtDuration(est.est_seconds_high)}</div></div>
      <div><div class="stat-label">Est. Apify cost</div><div class="stat-value">$${est.est_cost_usd_low.toFixed(2)}–$${est.est_cost_usd_high.toFixed(2)}</div></div>
    `;
    const skips = [];
    if (est.meta_skipped)
      skips.push(`<b>${est.meta_skipped}</b> without a Meta page (Meta skipped — run the <b>Identity check</b> first)`);
    if (est.google_skipped)
      skips.push(`<b>${est.google_skipped}</b> without a website (Google skipped)`);
    if (skips.length)
      summaryBox.innerHTML += `<p class="hint" style="flex-basis:100%">⏭ ${skips.join("; ")}.</p>`;
    $("#planConfirmBtn").disabled = est.total_units === 0;
    if (est.total_units === 0) {
      summaryBox.innerHTML += `<p class="hint" style="flex-basis:100%">Nothing to fetch — the selection
        has no Meta page and no website for the chosen source${sources.length === 1 ? "" : "s"}.</p>`;
    }
  }

  function fmtDuration(sec) {
    if (sec < 90) return `${sec}s`;
    return `${Math.round(sec / 60)}m`;
  }

  async function confirmFetchPlan() {
    const btn = $("#planConfirmBtn");
    btn.disabled = true; btn.textContent = "Starting…";
    try {
      await api("/api/fetch-jobs", "POST", { company_ids: PLAN_COMPANY_IDS, sources: planSources() });
      $("#fetchPlanPanel").classList.add("hidden");
      // Selection persists across filters/pages by design (so you can build it
      // up across views) — but once a job locks in its company list, leaving
      // old picks checked would silently pad the NEXT fetch you start.
      CUST.selected.clear();
      await showJobProgressNow();          // progress modal first, table after
    } catch (e) {
      alert(`Could not start job: ${e.message}`);
    } finally { btn.disabled = false; btn.textContent = "Start fetch"; }
  }

  // After starting ANY background job (ad lookup, identity check, enrichment) the
  // progress modal must appear at once. It used to be rendered only after
  // loadCustomers() had finished re-fetching the whole table, which on a few
  // thousand rows left a second or two of "did my click do anything?" — the ad
  // lookup masked it with its plan panel, the other two showed nothing at all.
  // So: jobs first (that paints the modal), table refresh afterwards in the
  // background. Also clears the dismiss/hide flags so a NEW job is never
  // suppressed by a previous one the user had hidden.
  async function showJobProgressNow() {
    _stripHiddenJobId = null;
    _stripDismissedJobId = null;
    await loadJobs();          // -> updateJobStrip() paints the spinner + bar
    startJobPolling();
    loadCustomers().catch(() => { /* table refresh is cosmetic here */ });
  }

  let _jobPollTimer = null;

  function startJobPolling() {
    if (_jobPollTimer) return;
    _jobPollTimer = setInterval(async () => {
      const jobs = await loadJobs();
      // Confirm (or stop tracking) any cancellation the user requested.
      for (const jid of [..._cancelRequestedJobs]) {
        const j = jobs.find(x => x.id === jid);
        if (!j || ["running", "queued", "cancelling"].includes(j.status)) continue;  // still finishing
        if (j.status === "cancelled") toast(`✓ ${jobKindLabel(j)} abgebrochen.`, "info");
        _cancelRequestedJobs.delete(jid);
      }
      if (!jobs.some(j => j.status === "running" || j.status === "queued" || j.status === "cancelling")) {
        clearInterval(_jobPollTimer);
        _jobPollTimer = null;
        await loadCustomers();   // tracked ad counts etc. may have changed
      }
    }, 1500);
  }

  async function loadJobs() {
    const jobs = await api("/api/fetch-jobs");
    renderJobs(jobs);
    updateJobStrip(jobs);
    return jobs;
  }

  // ---------------- live progress overlay (identity + ad lookups) ----------------
  // A centered modal card over a dimmed/blurred page while a check runs, then a
  // done/failed result the user dismisses. "Hide" keeps the job running in the
  // background (still visible in the Background jobs card) without blocking.
  let _stripActiveJobId = null;     // job the overlay is/was following
  let _stripDismissedJobId = null;  // finished job the user dismissed
  let _stripHiddenJobId = null;     // job the user hid while it runs
  const _cancelRequestedJobs = new Set();  // jobs the user asked to cancel — toast once cancelled

  function jobKindLabel(j) {
    return { identity: "Identity check", enrich: "Datenanreicherung",
             pipeline: "Komplett-Pipeline" }[j.kind] || "Ad lookup";
  }

  function hideJobStrip() { $("#jobStrip").classList.add("hidden"); }

  // The last few log lines as a live, fading activity feed — the real per-company
  // output streaming from the search (e.g. "✓ Linara Ahaus — confirmed → …"),
  // newest at the bottom, older lines dimmer.
  function stripFeedHtml(job) {
    const lines = (job.log || []).filter(e => e.text !== "Job complete.").slice(-6);
    if (!lines.length) return `<div class="feed-line" style="opacity:.6">Searching…</div>`;
    return lines.map((e, i) => {
      const op = (0.3 + 0.7 * ((i + 1) / lines.length)).toFixed(2);
      return `<div class="feed-line" style="opacity:${op}">${esc(e.text)}</div>`;
    }).join("");
  }

  function updateJobStrip(jobs) {
    const strip = $("#jobStrip");
    if (!strip) return;
    const active = jobs.find(j => ["running", "queued", "cancelling"].includes(j.status));

    if (active) {
      _stripActiveJobId = active.id;
      if (active.id === _stripHiddenJobId) { strip.classList.add("hidden"); return; }
      const pct = active.total ? Math.round(100 * active.completed / active.total) : 0;
      // Build the skeleton ONCE per job so the spinner/bar don't restart each poll.
      if (strip.dataset.job !== String(active.id) || !strip.classList.contains("running")) {
        strip.dataset.job = String(active.id);
        strip.className = "job-overlay running";
        strip.innerHTML = `
          <div class="job-modal">
            <div class="job-modal-head">
              <span class="spinner"></span>
              <div class="job-modal-titles">
                <b class="js-label"></b>
                <span class="job-strip-note js-note"></span>
              </div>
            </div>
            <div class="job-strip-bar"><div class="job-strip-bar-fill js-fill"></div></div>
            <div class="job-strip-feed js-feed"></div>
            <div class="job-modal-actions">
              <button class="btn btn-sm btn-ghost job-strip-hide">Hide</button>
              <button class="btn btn-sm btn-danger job-strip-cancel">Cancel</button>
            </div>
          </div>`;
        $(".job-strip-cancel", strip).addEventListener("click", async (ev) => {
          const b = ev.currentTarget; b.disabled = true; b.textContent = "Cancelling…";
          const jid = active.id;
          try {
            await api(`/api/fetch-jobs/${jid}/cancel`, "POST");
          } catch (e) {
            toast(`Could not cancel: ${e.message}`, "error");
            b.disabled = false; b.textContent = "Cancel"; return;
          }
          // Unblock the app right away — the in-flight fetch finishes in the
          // background, then the job stops. A toast confirms once it's cancelled.
          _cancelRequestedJobs.add(jid);
          _stripHiddenJobId = jid;
          hideJobStrip();
          toast("Abbruch angefordert — der laufende Abruf wird noch beendet, dann stoppt der Job. "
            + "Du kannst normal weiterarbeiten.", "info");
          await loadJobs();
        });
        const hide = () => { _stripHiddenJobId = active.id; hideJobStrip(); };
        $(".job-strip-hide", strip).addEventListener("click", hide);
        // click the dimmed backdrop (outside the card) = Hide, not Cancel
        strip.addEventListener("click", (e) => { if (e.target === strip) hide(); });
      }
      $(".js-label", strip).textContent = active.status === "cancelling"
        ? `${jobKindLabel(active)} — cancelling…` : `${jobKindLabel(active)} running…`;
      $(".js-note", strip).textContent = `${active.completed} of ${active.total} · ${pct}%`;
      $(".js-fill", strip).style.width = pct + "%";
      const feed = $(".js-feed", strip);
      feed.innerHTML = stripFeedHtml(active);
      feed.scrollTop = feed.scrollHeight;
      return;
    }

    // no active job — show the finished result once, unless already dismissed/hidden
    strip.dataset.job = "";
    if (_stripActiveJobId && _stripActiveJobId !== _stripDismissedJobId
        && _stripActiveJobId !== _stripHiddenJobId) {
      const j = jobs.find(x => x.id === _stripActiveJobId);
      if (j && !["running", "queued", "cancelling"].includes(j.status)) {
        const ok = j.status === "done" && !j.errors;
        strip.className = "job-overlay " + (ok ? "done" : "warn");
        strip.innerHTML = `
          <div class="job-modal job-modal-result">
            <div class="result-icon">${ok ? "✓" : "⚠"}</div>
            <b>${jobKindLabel(j)} ${esc(JOB_STATUS_LABEL[j.status] || j.status).toLowerCase()}</b>
            <span class="job-strip-note">${j.completed}/${j.total} processed · ${j.errors} error(s)${j.kind === "fetch" ? ` · ${j.ads_collected} ads` : ""}</span>
            <div class="job-modal-actions"><button class="btn btn-sm btn-primary job-strip-dismiss">Done</button></div>
          </div>`;
        const jid = j.id;
        const dismiss = () => { _stripDismissedJobId = jid; hideJobStrip(); };
        $(".job-strip-dismiss", strip).addEventListener("click", dismiss);
        strip.addEventListener("click", (e) => { if (e.target === strip) dismiss(); });
      }
    }
  }

  const JOB_STATUS_LABEL = {
    queued: "Queued", running: "Running", done: "Done", failed: "Failed",
    cancelled: "Cancelled", interrupted: "Interrupted", cancelling: "Cancelling…",
  };

  let JOBS_EXPANDED = false;

  function renderJobs(jobList) {
    const box = $("#jobsList");
    if (!jobList.length) { box.innerHTML = `<p class="hint">Noch keine Jobs — oben Firmen auswählen und eine Aktion starten.</p>`; return; }
    // Day-to-day only the CURRENT job matters: show anything still live, else
    // just the newest — the older history sits behind one expander instead of
    // a wall of cards.
    const live = jobList.filter(j => ["running", "queued", "cancelling"].includes(j.status));
    const shownJobs = JOBS_EXPANDED ? jobList : (live.length ? live : jobList.slice(0, 1));
    const hiddenCount = jobList.length - shownJobs.length;
    box.innerHTML = shownJobs.map(j => {
      const pct = j.total ? Math.round(100 * j.completed / j.total) : 0;
      const canResume = j.status === "interrupted" || j.status === "queued";
      const canCancel = j.status === "running" || j.status === "queued";
      const logTail = (j.log || []).slice(-8);
      const isIdentity = j.kind === "identity";
      const isFetch = j.kind === "fetch" || !j.kind;
      const what = isIdentity ? "identity check"
                 : j.kind === "enrich" ? "Datenanreicherung · Website + Firmeninfos"
                 : j.kind === "pipeline" ? "Komplett-Pipeline · " + [
                     (j.plan || {}).enrich && "anreichern", (j.plan || {}).identity && "Identität",
                     ((j.plan || {}).ads || []).length && "Anzeigen",
                     (j.plan || {}).report && "Bericht",
                     ((j.plan || {}).send_to || []).length && "senden"].filter(Boolean).join(" → ")
                 : `ad lookup · ${j.sources.join(" + ")}`;
      // an ad count is only meaningful for a real ad fetch
      const stats = `${j.completed}/${j.total} · ${j.errors} error(s)` + (isFetch ? ` · ${j.ads_collected} ads` : "");
      return `<div class="job-item" data-job="${j.id}">
        <div class="job-item-head">
          <span class="job-status job-status-${j.status}">${esc(JOB_STATUS_LABEL[j.status] || j.status)}</span>
          <span class="job-kind ${isIdentity ? "job-kind-identity" : ""}">${esc(what)}</span>
          <b>#${j.id}</b>
          <span class="muted">${j.company_ids.length} companies</span>
          <span class="spacer"></span>
          <span class="muted">${stats}</span>
          ${canResume ? `<button class="btn btn-sm job-resume-btn">Resume</button>` : ""}
          ${canCancel ? `<button class="btn btn-sm job-cancel-btn">Cancel</button>` : ""}
        </div>
        <div class="job-progress-track"><div class="job-progress-fill" style="width:${pct}%"></div></div>
        <div class="job-log">${logTail.map(e => `<div>${esc(e.text)}</div>`).join("")}</div>
      </div>`;
    }).join("")
      + (hiddenCount > 0 ? `<button class="btn btn-sm btn-ghost" id="jobsMoreBtn">${hiddenCount} ältere Jobs anzeigen ▾</button>` : "")
      + (JOBS_EXPANDED && jobList.length > 1 ? `<button class="btn btn-sm btn-ghost" id="jobsMoreBtn">Nur aktuellen Job anzeigen ▴</button>` : "");
    $("#jobsMoreBtn")?.addEventListener("click", () => { JOBS_EXPANDED = !JOBS_EXPANDED; renderJobs(jobList); });

    $$(".job-resume-btn", box).forEach(btn => btn.addEventListener("click", async () => {
      const jobId = Number(btn.closest(".job-item").dataset.job);
      try { await api(`/api/fetch-jobs/${jobId}/resume`, "POST"); await loadJobs(); startJobPolling(); }
      catch (e) { alert(e.message); }
    }));
    $$(".job-cancel-btn", box).forEach(btn => btn.addEventListener("click", async () => {
      const jobId = Number(btn.closest(".job-item").dataset.job);
      btn.disabled = true; btn.textContent = "Cancelling…";
      try { await api(`/api/fetch-jobs/${jobId}/cancel`, "POST"); }
      catch (e) { toast(`Could not cancel: ${e.message}`, "error"); return; }
      _cancelRequestedJobs.add(jobId);
      toast("Abbruch angefordert — der laufende Abruf wird noch beendet, dann stoppt der Job.", "info");
      await loadJobs();
      startJobPolling();   // ensure we detect the final 'cancelled' and confirm it
    }));
  }

  wireStatic();
  loadState();
  loadReports();
  loadSavedReports().catch(() => { /* saved-reports list is best-effort at boot */ });
  loadSchedule();
  loadCustomerFilterOptions();
})();
