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

  function fitCell(fit, mitBalken = false) {
    if (fit == null) return '<span class="muted">—</span>';
    const cls = fit >= 85 ? "fit-high" : (fit >= 60 ? "fit-mid" : "fit-low");
    const zahl = `<span class="fit-badge ${cls}">${Math.round(fit)}</span>`;
    if (!mitBalken) return zahl;
    // Der Balken beantwortet die Frage, die die Zahl offen laesst: 67 wovon?
    // Nur in der Tabelle, wo Zeilen untereinander stehen und der Vergleich
    // ueberhaupt etwas heisst -- im Steckbrief steht die Zahl allein.
    return `${zahl}<span class="fit-bar" aria-hidden="true"><i style="width:${
      Math.max(0, Math.min(100, fit)).toFixed(0)}%"></i></span>`;
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

  // The API sends dates as ISO ("2023-05-14") because that is what sorts and
  // travels correctly; a German screen has to show 14.05.2023. Parsed by hand
  // rather than via new Date(), which shifts the day across time zones for a
  // bare date string.
  // Coloured status chip for an Objekt or a Verkaufschance. Whitelisted rather
  // than interpolated, so a new CRM value can never inject a class name — it
  // just falls back to the neutral chip.
  const VC_STATES = {gewonnen: "state-gewonnen", verloren: "state-verloren",
                     offen: "state-offen"};
  const stateChip = (v) => `<span class="state-chip ${VC_STATES[v] || ""}">${esc(v || "—")}</span>`;

  const deDate = (iso) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ""));
    return m ? `${m[3]}.${m[2]}.${m[1]}` : (iso || "—");
  };

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

    function totalPx() {
      return ths.reduce((s, th) => s + (parseFloat(th.style.width) || th.offsetWidth || 0), 0);
    }
    function freeze() {
      if (table.classList.contains("col-resized")) return;
      ths.forEach(th => { if (th.offsetParent !== null) th.style.width = th.offsetWidth + "px"; });
      table.classList.add("col-resized");
      table.style.tableLayout = "fixed";
      // Feste PIXELbreite, nicht max-content: `table-layout:fixed` greift nur
      // bei bestimmter Breite. Mit max-content fiel der Browser still ins
      // Auto-Layout zurück, und eine Spalte ließ sich nie UNTER ihre
      // Inhaltsbreite ziehen — auf Tabellen mit langen Texten (Entscheidungen)
      // wirkte das Ziehen deshalb schlicht nicht.
      table.style.width = totalPx() + "px";
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
        const move = (ev) => {
          th.style.width = Math.max(44, startW + (ev.clientX - startX)) + "px";
          table.style.width = totalPx() + "px";   // Gesamtbreite folgt der Spalte
        };
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
  // Monotonic per-table request token. Without it a slow earlier fetch resolves
  // after a newer one and silently repaints the older result — a filter that
  // "did not work" although the request went out correctly.
  const LOAD_SEQ = {};
  const nextSeq = (k) => (LOAD_SEQ[k] = (LOAD_SEQ[k] || 0) + 1);
  const isCurrent = (k, n) => LOAD_SEQ[k] === n;

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
      const srvActive = !srv ? false
        : srv.select ? !!$(srv.select)?.value
        : !!(SERVER_PARAMS[wrapId] || {})[srv.param];
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
  // A column filter either filters EVERYTHING or it is not offered. These
  // columns map onto a server parameter; the rest get sort only when the table
  // holds a capped slice, because filtering 300 of 52.796 and calling it a
  // filter is how the screen ended up claiming 34 won projects out of 8.189.
  const SERVER_COLUMNS = {
    // Die Schlüssel sind SPALTENNUMMERN der gerenderten Tabelle. Als die Spalte
    // „Angelegt" dazukam, rutschte alles dahinter um eins — und niemand merkte
    // es, weil ein Menü ja trotzdem aufging: der Status-Filter hing an
    // „Angelegt", der Wertfilter an „VCs", der Firmenfilter an „Wert". Sichtbar
    // wurde das erst, als die Filterleiste über der Karte Kopf und Inhalt
    // nebeneinander stellte.
    // Reihenfolge: 0 Objekt · 1 Angelegt · 2 Status · 3 VCs · 4 Wert ·
    //              5 Firmen · 6 Architekten · 7 Verlustgründe
    objekteWrap: {
      0: {kind: "text",   param: "q",           label: "Objekt, Firma oder Architekt"},
      2: {kind: "select", select: "#objekteStatus", label: "Status"},
      4: {kind: "number", param: "min_value",   label: "Wert mindestens (€)"},
      5: {kind: "text",   param: "q",           label: "Firma enthält"},
      6: {kind: "text",   param: "q",           label: "Architekt enthält"},
      7: {kind: "text",   param: "lost_reason", label: "Verlustgrund (exakt)"},
    },
    chancenTableWrap: {
      1: {kind: "text",   param: "segment",     label: "Segment (exakt)"},
      2: {kind: "text",   param: "country",     label: "Land (z. B. DE)"},
      3: {kind: "number", param: "min_value",   label: "Umsatz mindestens (€)"},
      8: {kind: "text",   param: "health",      label: "Status (exakt)"},
    },
  };
  // Extra server params per table, set from the column menus and merged into
  // the request by the tab's own loader.
  const SERVER_PARAMS = {objekteWrap: {}, chancenTableWrap: {}};

  function _openColMenu(table, wrapId, th, colIdx) {
    const st = _stateFor(wrapId);
    const server = (SERVER_COLUMNS[wrapId] || {})[colIdx];
    if (server) return _openServerColMenu(table, wrapId, th, colIdx, server);
    // The table holds only a slice: a browser-side filter here would search the
    // loaded rows and report a fraction as if it were the answer. Offer sort,
    // say why there is no filter, and point at the columns that do filter.
    const total = TABLE_TOTALS[wrapId];
    if (total != null && total > (table.tBodies[0]?.rows.length || 0)) {
      return _openSortOnlyMenu(table, wrapId, th, colIdx, total);
    }
    const body = table.tBodies[0];
    const values = [...new Set([...(body ? body.rows : [])]
      .map(tr => (tr.cells[colIdx]?.textContent || "").trim()))]
      .filter(v => v !== "").sort((a, b) => a.localeCompare(b, "de"));
    const chosen = st.filters[colIdx] || new Set();
    // A column of 300 distinct free-text values is a search box, not a checklist.
    const listable = values.length <= 60;

    const menu = _menuEl();
    // Spalten, deren Zellen LISTEN oder Erklärtexte tragen (Firmen, Architekten,
    // Verlustgründe, Warum), haben keine sinnvolle Reihenfolge — eine
    // alphabetische Sortierung über "Müller GmbH, Schmidt AG" sortiert nach dem
    // zufällig erstgenannten Namen. Der Filter bleibt; die Sortier-Knöpfe
    // verschwinden, statt so zu tun, als bedeuteten sie etwas.
    const sortable = !th.hasAttribute("data-nosort");
    menu.innerHTML = `
      <div class="thm-head">${esc(th.textContent.replace("▾", "").trim())}</div>
      ${sortable ? `
      <div class="thm-sec thm-sort">
        <button class="btn btn-sm thm-sort-btn${st.sort && st.sort.col === colIdx && st.sort.dir === "asc" ? " btn-primary" : ""}" data-dir="asc">↑ Aufsteigend</button>
        <button class="btn btn-sm thm-sort-btn${st.sort && st.sort.col === colIdx && st.sort.dir === "desc" ? " btn-primary" : ""}" data-dir="desc">↓ Absteigend</button>
      </div>` : ""}
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
    const sel = server.select ? $(server.select) : null;
    const cur = sel ? sel.value : (SERVER_PARAMS[wrapId]?.[server.param] ?? "");
    const menu = _menuEl();
    const control = server.kind === "select"
      ? `<select class="thm-input" id="thmServerSel">${
          [...sel.options].map(o => `<option value="${esc(o.value)}"${sel.value === o.value ? " selected" : ""}>${esc(o.textContent)}</option>`).join("")
        }</select>`
      : `<input class="thm-input" id="thmServerInput" type="${server.kind === "number" ? "number" : "text"}"
                value="${esc(String(cur))}" placeholder="${esc(server.label)}">
         <div class="sub" style="margin-top:4px">Enter zum Anwenden · leer = kein Filter</div>`;
    menu.innerHTML = `
      <div class="thm-head">${esc(th.textContent.replace("▾", "").trim())}</div>
      <div class="thm-sec thm-sort">
        <button class="btn btn-sm thm-sort-btn${st.sort && st.sort.col === colIdx && st.sort.dir === "asc" ? " btn-primary" : ""}" data-dir="asc">↑ Aufsteigend</button>
        <button class="btn btn-sm thm-sort-btn${st.sort && st.sort.col === colIdx && st.sort.dir === "desc" ? " btn-primary" : ""}" data-dir="desc">↓ Absteigend</button>
      </div>
      <div class="thm-sec"><div class="thm-sec-title">${esc(server.label)}</div>
        ${control}
        <div class="sub" style="margin-top:5px">Filtert <b>alle</b> Zeilen, nicht nur die geladenen.</div>
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
    const input = $("#thmServerInput", menu);
    let applied = false;                 // Enter AND blur both fire; load once
    const applyInput = () => {
      if (applied) return;
      applied = true;
      const v = input.value.trim();
      SERVER_PARAMS[wrapId] = SERVER_PARAMS[wrapId] || {};
      if (v) SERVER_PARAMS[wrapId][server.param] = v;
      else delete SERVER_PARAMS[wrapId][server.param];
      menu.classList.add("hidden");
      (wrapId === "objekteWrap" ? loadObjekte : loadChancen)();
    };
    input?.addEventListener("keydown", (e) => { if (e.key === "Enter") applyInput(); });
    input?.addEventListener("blur", applyInput);
  }

  // Turn the collected server params into a query string for the tab's loader.
  function serverParamQuery(wrapId) {
    return Object.entries(SERVER_PARAMS[wrapId] || {})
      .map(([k, v]) => `&${k}=${encodeURIComponent(v)}`).join("");
  }

  function _openSortOnlyMenu(table, wrapId, th, colIdx, total) {
    const st = _stateFor(wrapId);
    const loaded = table.tBodies[0]?.rows.length || 0;
    const menu = _menuEl();
    const filterable = Object.values(SERVER_COLUMNS[wrapId] || {})
      .map(c => c.label).filter(Boolean);
    // Freitext-/Listenspalten (data-nosort) haben auch hier keine sinnvolle
    // Reihenfolge — statt Sortier-Knöpfen, die nichts bedeuten, sagt das Menü,
    // was stattdessen geht.
    const sortable = !th.hasAttribute("data-nosort");
    menu.innerHTML = `
      <div class="thm-head">${esc(th.textContent.replace("▾", "").trim())}</div>
      ${sortable ? `
      <div class="thm-sec thm-sort">
        <button class="btn btn-sm thm-sort-btn${st.sort && st.sort.col === colIdx && st.sort.dir === "asc" ? " btn-primary" : ""}" data-dir="asc">↑ Aufsteigend</button>
        <button class="btn btn-sm thm-sort-btn${st.sort && st.sort.col === colIdx && st.sort.dir === "desc" ? " btn-primary" : ""}" data-dir="desc">↓ Absteigend</button>
      </div>` : ""}
      <div class="thm-sec">
        <div class="sub">${sortable
          ? `Sortiert die <b>${loaded}</b> geladenen Zeilen von
             ${total.toLocaleString("de-DE")}.`
          : `Freitext-Spalte — eine Sortierung hätte hier keine Bedeutung.`}
          Für diese Spalte gibt es keinen Filter über den ganzen Bestand —
          filtern lässt sich nach: ${esc(filterable.join(" · "))}.</div>
      </div>`;
    const r = th.getBoundingClientRect();
    menu.classList.remove("hidden");
    const w = Math.min(320, window.innerWidth - 24);
    menu.style.width = w + "px";
    menu.style.top = Math.round(r.bottom + 4) + "px";
    menu.style.left = Math.round(Math.min(r.left, window.innerWidth - w - 12)) + "px";
    $$(".thm-sort-btn", menu).forEach(b => b.addEventListener("click", () => {
      st.sort = { col: colIdx, dir: b.dataset.dir };
      menu.classList.add("hidden"); _applyTableState(table, wrapId);
    }));
  }

  function makeTableInteractive(table) {
    const wrap = table.closest(".table-wrap");
    // The Firmen table drives its menus from the server; leave it alone.
    if (!wrap || !wrap.id || table.id === "customersTable" || table.dataset.interactive) return;
    table.dataset.interactive = "1";
    $$("thead th", table).forEach((th, i) => {
      // hasAttribute, nicht dataset-Wahrheitswert: ein wertloses `data-nomenu`
      // liefert dataset.nomenu === "" — und leere Strings sind falsy.
      if (th.hasAttribute("data-nomenu")) return;   // Knopf-/Aktionsspalten: kein Menü, kein Caret
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
    loadHeute();        // dito — Heute-Karten und Entscheidungen-Badge
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

  // HEUTE — die Antwort auf "was braucht mich gerade?": offene Entscheidungen,
  // der laufende Job, der neueste Bericht. Drei vorhandene Endpunkte, keine
  // neue Backend-Logik. Jede Karte degradiert einzeln zu "—", damit ein
  // fehlschlagender Abruf nie die ganze Reihe leert.
  async function loadHeute() {
    const box = $("#heuteCards");
    if (!box) return;
    const [rev, jobs, reps] = await Promise.all([
      api("/api/identity/review?limit=1").catch(() => null),
      api("/api/fetch-jobs").catch(() => null),
      api("/api/reports").catch(() => null),
    ]);

    // Die Zahl an der Navigation IST die Aufgabenliste — sie muss stimmen,
    // auch wenn der Entscheidungen-Tab nie geöffnet wurde.
    const open = rev ? (rev.total || 0) : null;
    setPruefenBadge(open);

    const running = (jobs || []).find(j => j.status === "running" || j.status === "cancelling");
    const interrupted = (jobs || []).find(j => j.status === "interrupted");
    const rep = ((reps || {}).reports || [])[0];

    const jobCard = running
      ? { label: "Läuft gerade", value: `${running.completed}/${running.total}`,
          hint: running.label || running.kind, goto: "logs" }
      : interrupted
        ? { label: "Unterbrochener Lauf", value: `${interrupted.completed}/${interrupted.total}`,
            hint: "Fortsetzbar — in Logs auf „Fortsetzen“", goto: "logs" }
        : { label: "Läufe", value: "keine aktiv", hint: "Nichts wartet im Hintergrund", goto: "logs" };

    const cards = [
      { label: "Entscheidungen offen",
        value: open == null ? "—" : open.toLocaleString("de-DE"),
        hint: open ? "Website-Vorschläge warten auf Ja/Nein" : "Warteschlange ist leer",
        goto: "pruefen" },
      jobCard,
      rep
        ? { label: "Letzter Bericht", value: rep.label || rep.filename,
            hint: rep.filter_label || "ohne Filter", goto: "reports" }
        : { label: "Berichte", value: "—", hint: "Noch keiner erzeugt", goto: "reports" },
    ];
    box.innerHTML = cards.map(c => `
      <button class="kpi kpi-link" data-goto="${c.goto}">
        <div class="kpi-label">${esc(c.label)}</div>
        <div class="kpi-value">${esc(String(c.value))}</div>
        <div class="kpi-hint">${esc(c.hint)}</div>
      </button>`).join("");
    // showTab lebt in wireStatic() und ist hier nicht erreichbar — gotoTab
    // klickt den Nav-Button und nimmt damit denselben Weg wie ein echter Klick
    // des Nutzers (Listener setzt localStorage und ruft showTab). Über gotoTab
    // statt direkt, damit "customers"/"objekte" mit übersetzt werden.
    $$("#heuteCards [data-goto]").forEach(b =>
      b.addEventListener("click", () => gotoTab(b.dataset.goto)));
  }

  function setPruefenBadge(n) {
    const badge = $("#navBadgePruefen");
    if (!badge || n == null) return;
    badge.textContent = n.toLocaleString("de-DE");
    badge.classList.toggle("hidden", !n);
  }

  // ================= PIPELINE-BOARD =================
  // Die Kette pro Markt: Bestand -> Identität -> Anreicherung -> Anzeigen ->
  // Qualifizierung -> ICP -> Bericht. Nur Lesen und Navigieren — Läufe starten
  // weiterhin dort, wo sie wohnen (Firmen, Entscheidungen, Berichte). Sperren
  // werden ANGEZEIGT statt nur durchgesetzt: der ICP-Boden steht mit Zahl da.
  let pipelineLoaded = false;
  function ensurePipelineLoaded() { if (!pipelineLoaded) loadPipeline(); }

  // Kartenklick-Trick von Heute: der echte Nav-Button wird geklickt, damit
  // localStorage + showTab denselben Weg nehmen wie ein Klick des Nutzers.
  //
  // "customers" und "objekte" sind keine Tabs mehr, sondern Bereiche des
  // Explorers. Sie werden hier übersetzt statt an jeder der sechs Aufrufstellen
  // — und weil ein alter localStorage-Eintrag oder ein geteilter Link denselben
  // Namen tragen kann, muss die Übersetzung ohnehin an einer Stelle stehen.
  const EXPLORE_ALIAS = { customers: "firmen", objekte: "projekte" };
  function gotoTab(name) {
    const bereich = EXPLORE_ALIAS[name];
    if (bereich) {
      EXPLORE.bereich = bereich;
      // Wer aus „Lauf über Firmen starten" kommt, will die Tabelle mit ihren
      // Auswahlkästchen sehen, nicht die Karte.
      EXPLORE.ansicht = "liste";
      name = "explore";
    }
    const b = $$(".tab").find(t => t.dataset.tab === name);
    if (b) b.click();
  }

  const deN = (n) => (n || 0).toLocaleString("de-DE");

  async function loadPipeline(country) {
    const board = $("#pipeBoard");
    board.innerHTML = `<p class="hint">Lädt…</p>`;
    let d;
    try {
      d = await api(`/api/pipeline${country ? `?country=${encodeURIComponent(country)}` : ""}`);
    } catch (e) {
      board.innerHTML = `<p class="hint">Fehler beim Laden: ${esc(e.message)}</p>`;
      return;
    }
    pipelineLoaded = true;
    const sel = $("#pipeCountry");
    sel.innerHTML = d.markets.map(m =>
      `<option value="${m.country}"${m.country === d.selected ? " selected" : ""}>` +
      `${esc(m.label)} — ${deN(m.total)} Firmen</option>`).join("");
    if (!sel.dataset.wired) {   // einmal verdrahten — innerHTML ersetzt nur die Optionen
      sel.addEventListener("change", () => loadPipeline(sel.value));
      sel.dataset.wired = "1";
    }
    $("#pipeMeta").textContent = "ohne Private Endkunden und Wettbewerber-Standorte";
    renderPipeline(d.stages);
  }

  function pipeRow(name, pctOrNull, counts, action) {
    const bar = pctOrNull == null ? "" : `
      <div class="pipe-bar" title="${pctOrNull}%">
        <div class="pipe-fill" style="width:${Math.max(pctOrNull, 1.5)}%"></div>
      </div>`;
    return `
      <div class="pipe-row">
        <div class="pipe-name">${esc(name)}</div>
        <div>${bar}</div>
        <div class="pipe-counts">${counts}</div>
        <div class="pipe-action">${action || ""}</div>
      </div>`;
  }

  function renderPipeline(st) {
    const total = st.bestand.total || 1;
    const pct = (n) => Math.round((n / total) * 100);
    const idV = st.identitaet;
    const geschlossen = idV.not_found + idV.unreachable + idV.conflict;

    const rows = [
      pipeRow("Bestand", null,
        `<b>${deN(st.bestand.total)}</b> Firmen · ${deN(st.bestand.mit_website)} mit Website · ` +
        `${deN(st.bestand.kaeufer)} Käufer` +
        // Herkunft sichtbar: sonst wächst der Bestand über Nacht und niemand
        // weiß, ob das CRM gewachsen ist oder wir selbst gesucht haben.
        (st.bestand.selbst_gefunden
          ? ` <span class="muted">(${deN(st.bestand.aus_crm)} aus CRM, ` +
            `${deN(st.bestand.selbst_gefunden)} selbst gefunden)</span>` : ""),
        ""),
      pipeRow("Identität", pct(idV.verified),
        `<b>${deN(idV.verified)}</b> verifiziert · ` +
        `<span class="pipe-warn">${deN(idV.offen)} offen</span> · ` +
        `${deN(idV.unbekannt)} unbekannt · ${deN(geschlossen)} geschlossen (kein Web / Konflikt)`,
        idV.offen ? `<button class="btn btn-sm btn-primary" data-goto="pruefen">${deN(idV.offen)} entscheiden</button>` : "✓"),
      pipeRow("Anreicherung", pct(st.anreicherung.mit_fakten),
        `<b>${deN(st.anreicherung.mit_fakten)}</b> mit Fakten · ` +
        `${deN(st.anreicherung.verified_ohne_fakten)} verifiziert, aber ohne Fakten · ` +
        `${deN(st.anreicherung.ohne_website_final)} ohne auffindbare Website <span class="muted">(Endstand, keine Lücke)</span>`,
        st.anreicherung.verified_ohne_fakten
          ? `<button class="btn btn-sm" data-goto="customers">Lauf über Firmen starten</button>` : "✓"),
      pipeRow("Anzeigen", pct(st.anzeigen.je_abgerufen),
        `<b>${deN(st.anzeigen.je_abgerufen)}</b> je abgerufen · ${deN(st.anzeigen.aktiv)} aktuell aktiv · ` +
        `${deN(st.anzeigen.nie_abgerufen)} nie abgerufen <span class="muted">(unbekannt, nicht inaktiv)</span>`,
        st.anzeigen.apify_konfiguriert
          ? `<button class="btn btn-sm" data-goto="customers">Abruf über Firmen</button>`
          : `<span class="pipe-lock">⛔ Apify nicht konfiguriert</span>`),
      pipeRow("Qualifizierung", null,
        `<b>${deN(st.qualifizierung.betriebe_hoch)}</b> Betriebe Passung hoch · ` +
        `<b>${deN(st.qualifizierung.bueros_hoch)}</b> Büros Relevanz hoch ` +
        `<span class="muted">(davon ${deN(st.qualifizierung.vergibt)} vergeben Aufträge)</span> · ` +
        `${deN(st.qualifizierung.betriebe_mittel)} Betriebe mittel`,
        `<button class="btn btn-sm" data-goto="customers">Liste in Firmen</button>`),
      pipeRow("ICP", null,
        st.icp.modus === "scorecard"
          ? `<span class="pipe-warn">⚠ ${deN(st.icp.material_kaeufer)} von ${st.icp.boden} nötigen Käufern ab 2.000 €</span> — ` +
            `unter dem Boden sind Verteilungen Rauschen: <b>Scorecard statt Modell</b>`
          : `✓ Boden erreicht (${deN(st.icp.material_kaeufer)} Käufer ab 2.000 €) — ` +
            `<span class="muted">ob ein Modell wirklich trennt, entscheidet die Ampel im ICP-Tab</span>`,
        `<button class="btn btn-sm" data-goto="profil">ICP-Tab</button>`),
      pipeRow("Bericht", null,
        st.bericht
          ? `<b>${esc(st.bericht.label)}</b> · ${esc((st.bericht.created_at || "").slice(0, 10))}`
          : `<span class="muted">noch kein Bericht für dieses Land</span>`,
        (st.bericht
          ? `<a class="btn btn-sm" href="/api/reports/${encodeURIComponent(st.bericht.filename)}" target="_blank">Öffnen</a> `
          : "") + `<button class="btn btn-sm" data-goto="reports">Berichte</button>`),
    ];
    $("#pipeBoard").innerHTML = rows.join("");
    $$("#pipeBoard [data-goto]").forEach(b =>
      b.addEventListener("click", () => gotoTab(b.dataset.goto)));
  }

  // ================= ZIELKUNDEN — die vier validierten Profile =================
  // Jede Ansicht trägt ihre GEMESSENE Güte sichtbar mit. Das ist keine
  // Kosmetik: eine AUC von 0,60 (Kalt-Akquise) darf nicht aussehen wie eine von
  // 0,80 (Bestand), sonst liest der Nutzer eine Rangfolge, die es nicht gibt.
  let profileLoaded = false, profileKind = "ipp";
  function ensureProfilesLoaded() { if (!profileLoaded) loadProfile("ipp"); }

  const QUAL_CLASS = (auc) => auc >= 0.70 ? "qual-stark" : auc >= 0.65 ? "qual-mittel" : "qual-schwach";

  // Ein Satz je Ansicht: WEN sie zeigt und WOZU. Steht ueber der Tabelle, weil
  // die Frage "was sehe ich hier eigentlich" vor der ersten Zahl kommt.
  const PROFIL_WAS = {
    ipp: "Alle offenen Bauvorhaben, gereiht nach Ähnlichkeit zu früher gewonnenen. " +
         "Oben anfangen zu arbeiten.",
    funnel: "Firmen, mit denen ein Gespräch läuft (offene Verkaufschance), die aber " +
            "noch nie ein Angebot bekommen haben. Wen davon diese Woche anrufen?",
    bestand: "Firmen, die schon angefragt haben und jetzt gegen ihren eigenen " +
             "Rhythmus verstummen. Die riskantesten zuerst.",
    kalt: "Firmen, mit denen wir noch nie gesprochen haben. Nur eine Vorsortierung " +
          "nach Gewerk — über die einzelne Firma weiß hier niemand etwas.",
  };

  async function loadProfile(kind) {
    profileKind = kind;
    const box = $("#profileBody");
    box.innerHTML = `<p class="hint">Lädt…</p>`;
    $("#profileQuality").textContent = "";
    $("#profileWas").textContent = PROFIL_WAS[kind] || "";
    try {
      if (kind === "ipp") return renderIpp(await api("/api/ipp"),
                                           await api("/api/ipp/triage?limit=40"));
      const d = await api(`/api/profiles/${kind}?limit=60`);
      profileLoaded = true;
      if (kind === "kalt") return renderKalt(d);
      if (kind === "funnel") return renderFunnel(d);
      if (kind === "bestand") return renderBestand(d);
    } catch (e) {
      box.innerHTML = `<p class="hint status-error">Fehler: ${esc(e.message)}</p>`;
    }
  }

  // Lift ist eine Verhaeltniszahl, und "1,40x" sagt niemandem etwas, der nicht
  // weiss, wovon. Daraus wird hier ein Satz: 1,40 -> "40 % haeufiger". Die Zahl
  // bleibt als Tooltip erhalten, damit nachrechnen kann, wer will.
  // Prozent deutsch: 56.1 -> "56,1 %". toFixed liefert immer einen Punkt.
  // null ist NICHT null Prozent. Ohne diese Unterscheidung stand in der
  // Konversionstabelle "0,0 %" fuer Gruppen, zu denen schlicht nichts
  // gemessen wurde — eine erfundene Zahl an der Stelle einer fehlenden.
  const pct = (v, n = 1) =>
    (v == null || Number.isNaN(v)) ? "—" : `${(v * 100).toFixed(n).replace(".", ",")} %`;

  function liftSatz(l) {
    if (!isFinite(l) || l <= 0) return "—";
    if (l >= 1.95) return `${l.toFixed(1).replace(".", ",")}× so oft`;
    if (l >= 1.05) return `${Math.round((l - 1) * 100)} % häufiger`;
    if (l > 0.95) return "wie der Durchschnitt";
    if (l >= 0.5) return `${Math.round((1 - l) * 100)} % seltener`;
    return "weniger als halb so oft";
  }

  // Merkmalsschluessel sind fuer Menschen geschrieben worden, die den Code
  // kennen. Fuer alle anderen uebersetzt das hier.
  const MERKMAL_WORT = {
    mehrere_vcs: "mehrere Verkaufschancen am Objekt",
    architekt_beteiligt: "Architekt beteiligt",
    "vc_offen:ja": "offene Verkaufschance",
    "vc_offen:nein": "keine offene Verkaufschance",
    "vc_verloren:nein": "noch nie eine Chance verloren",
    "vc_verloren:ja": "schon einmal verloren",
    "website:ja": "hat eine Website",
    "website:nein": "keine Website",
  };
  const MERKMAL_PRAEFIX = {
    familie: "Produkt", kanal: "Vertriebsweg", segment: "Segment",
    branche: "Branche", region: "Region", land: "Land",
    vc_anzahl: "Verkaufschancen", frequenz: "bisherige Angebote",
    aktualitaet: "letztes Angebot", volumen: "Angebotsvolumen",
    vc_wert: "Chancenwert",
  };
  function merkmalWort(key) {
    if (MERKMAL_WORT[key]) return MERKMAL_WORT[key];
    const i = String(key).indexOf(":");
    if (i > 0) {
      const p = MERKMAL_PRAEFIX[key.slice(0, i)];
      if (p) return `${p}: ${key.slice(i + 1)}`;
    }
    return String(key).replace(/_/g, " ");
  }

  function qualityBadge(label, value, verdict) {
    return `<span class="qual-badge ${QUAL_CLASS(value)}">${esc(label)} ${value.toFixed(3)}</span>`
         + `<span class="muted"> · ${esc(verdict)}</span>`;
  }

  function renderIpp(p, t) {
    profileLoaded = true;
    const dec = p.test.deciles.map(d => d.win_rate);
    $("#profileQuality").innerHTML = qualityBadge("Lift",
      p.test.lift_top_vs_bottom, p.ranks ? "rankt — belastbare Reihenfolge" : "rankt nicht");
    $("#profileBody").innerHTML = `
      <p class="hint"><b>Welches offene Objekt gewinnen wir?</b> Trainiert auf Projekten vor
        ${p.train.until + 1}, geprüft auf den späteren — ein Modell, das seine eigene Zukunft nicht
        kennt. Gewinnquote je Punktzahl-Dezil:</p>
      <div class="decile-bars">${dec.map((r, i) => `
        <div class="decile" title="Dezil ${i + 1}: ${(r * 100).toFixed(0)}% gewonnen">
          <div class="decile-fill" style="height:${Math.max(r / Math.max(...dec) * 100, 3)}%"></div>
          <span>${(r * 100).toFixed(0)}%</span></div>`).join("")}</div>
      <p class="hint">Basisrate ${pct(p.base_rate)} · ${p.test.n.toLocaleString("de-DE")}
        Projekte im Test · ${p.test.monotone_steps}/9 Stufen monoton steigend</p>
      <h3 style="margin-top:18px">Die stärksten Merkmale</h3>
      <table class="data-table"><thead><tr><th>Merkmal</th>
        <th class="num">gewonnen</th><th>im Vergleich zum Schnitt</th>
        <th class="num">Projekte</th></tr></thead><tbody>
        ${[...p.features.slice(0, 8).map(f => [f, ""]),
           ...p.features.slice(-3).map(f => [f, ' class="feat-neg"'])]
          .map(([f, cls]) => `<tr${cls}><td>${esc(merkmalWort(f.feature))}</td>
          <td class="num">${pct(f.rate, 0)}</td>
          <td title="Lift ${f.lift.toFixed(2)}× gegenüber der Basisrate ${pct(p.base_rate)}"><b>${liftSatz(f.lift)}</b></td>
          <td class="num">${f.total.toLocaleString("de-DE")}</td></tr>`).join("")}
      </tbody></table>
      <h3 style="margin-top:18px">Offene Projekte, gereiht (${t.open_total.toLocaleString("de-DE")} gesamt)</h3>
      <table class="data-table"><thead><tr><th>Objekt</th><th>Ort</th>
        <th class="num">Wert</th><th data-nosort>Warum</th></tr></thead><tbody>
        ${t.rows.map(r => `<tr>
          <td><b>${esc((r.name || "—").slice(0, 54))}</b></td>
          <td>${esc(r.city || "")}</td>
          <td class="num">${r.estimated_value ? eur(r.estimated_value) : "—"}</td>
          <td class="sub">${r.why.map(w =>
            `<div title="Lift ${Number(w.lift).toFixed(2)}×">${esc(merkmalWort(w.feature))}
             — <b>${liftSatz(Number(w.lift))} gewonnen</b></div>`).join("")}</td>
        </tr>`).join("")}</tbody></table>`;
  }

  function renderFunnel(d) {
    $("#profileQuality").innerHTML = qualityBadge("AUC", d.quality.auc, d.quality.verdict);
    $("#profileBody").innerHTML = `
      <p class="hint"><b>Wer im Trichter wird aktiv?</b> Firmen mit Verkaufschance, aber noch ohne
        Angebot. Oberstes Dezil ${d.quality.top_decile_lift}× der Basisrate
        (${(d.base_rate * 100).toFixed(1)} %). Das Signal ist die Kontaktintensität — ohne das
        Merkmal „hat schon gewonnen" ist die Güte unverändert.</p>
      ${listButton("funnel", "Trichter")}
      <table class="data-table"><thead><tr><th>Firma</th><th>Ort</th><th>Branche</th>
        <th class="num">VCs</th><th class="num">Wert</th><th data-nosort>Warum</th></tr></thead><tbody>
        ${d.rows.map(r => `<tr data-cid="${r.company_id}" style="cursor:pointer">
          <td><b>${esc(r.name || "—")}</b></td><td>${esc(r.city || "")}</td>
          <td class="sub">${esc(r.sub_segment || "")}</td>
          <td class="num">${r.vc_n}${r.vc_open ? ` <span class="sub">(${r.vc_open} offen)</span>` : ""}</td>
          <td class="num">${r.vc_value ? eur(r.vc_value) : "—"}</td>
          <td class="sub">${r.why.map(w =>
            `<div title="Lift ${Number(w.lift).toFixed(2)}×">${esc(merkmalWort(w.feature))}
             — <b>${liftSatz(Number(w.lift))}</b></div>`).join("")}</td>
        </tr>`).join("")}</tbody></table>`;
    wireProfileRows();
    wireListButton(d.rows.map(r => ({ company_id: r.company_id, score: r.score })));
  }

  function renderBestand(d) {
    $("#profileQuality").innerHTML = qualityBadge("AUC", d.quality.auc, d.quality.verdict);
    $("#profileBody").innerHTML = `
      <p class="hint"><b>Wer fragt weiter an — und wer bricht ab?</b> Aufsteigend sortiert: die
        <b>riskantesten zuerst</b>. ${esc(d.hinweis)}
        ${d.ausgeblendet ? `<br><span class="muted">${d.ausgeblendet.toLocaleString("de-DE")}
        Kleinstkunden ausgeblendet — unter der Wertgrenze lohnt die Rückholung den Anruf nicht.</span>` : ""}</p>
      ${listButton("bestand", "Rückholung")}
      <table class="data-table"><thead><tr><th>Firma</th><th>Ort</th>
        <th class="num">Angebote</th><th class="num">Angebotsvolumen</th>
        <th class="num">letztes Angebot</th></tr></thead><tbody>
        ${d.at_risk.map(r => `<tr data-cid="${r.company_id}" style="cursor:pointer">
          <td><b>${esc(r.name || "—")}</b></td><td>${esc(r.city || "")}</td>
          <td class="num">${r.orders}</td><td class="num">${eur(r.revenue)}</td>
          <td class="num">vor ${Math.round(r.days_since_last / 30)} Mon.</td>
        </tr>`).join("")}</tbody></table>`;
    wireProfileRows();
    wireListButton(d.at_risk.map(r => ({ company_id: r.company_id, score: r.score })));
  }

  function renderKalt(d) {
    $("#profileQuality").innerHTML = qualityBadge("AUC", d.quality.auc, d.quality.verdict);
    $("#profileBody").innerHTML = `
      <div class="icp-plain" style="border-left-color:#8a5220">
        <b>Vorsortierung, keine Rangliste.</b> ${esc(d.hinweis)} Ein Fensterbauer ist ein besserer
        Erstkontakt als ein Baustoffhändler — aber Rang 3 ist nicht besser als Rang 30.
      </div>
      <p class="hint">Grundgesamtheit ${d.n.toLocaleString("de-DE")} Händler ohne bisheriges
        Angebot (${d.country}), Basisrate ${pct(d.base_rate)}. Gemessene
        Anfragequote je Branche:</p>
      <table class="data-table"><thead><tr><th>Branche</th>
        <th class="num">fragt an</th><th>im Vergleich zum Schnitt</th>
        <th class="num">Firmen</th></tr></thead><tbody>
        ${d.branchen.map(b => `<tr${b.lift < 1 ? ' class="feat-neg"' : ""}>
          <td>${esc(b.branche)}</td>
          <td class="num">${pct(b.rate)}</td>
          <td title="Lift ${b.lift.toFixed(2)}× — ${pct(b.rate)} geteilt durch die Basisrate ${pct(d.base_rate)}"><b>${liftSatz(b.lift)}</b></td>
          <td class="num">${b.n.toLocaleString("de-DE")}</td></tr>`).join("")}
      </tbody></table>`;
  }

  function wireProfileRows() {
    $$("#profileBody tr[data-cid]").forEach(tr => tr.addEventListener("click", () =>
      openCompanyDrawer(Number(tr.dataset.cid))));
  }

  $$("#profileSwitch .prof-btn").forEach(b => b.addEventListener("click", () => {
    $$("#profileSwitch .prof-btn").forEach(x => x.classList.toggle("active", x === b));
    loadProfile(b.dataset.prof);
  }));

  // ================= LISTEN — abarbeiten mit Kontrollgruppe =================
  // Die Kontrollgruppe wird ANGEZEIGT und gesperrt, nicht ausgeblendet. Wer sie
  // nicht sieht, ruft sie irgendwann über einen anderen Weg an — und dann ist
  // die Wirkung der Liste nicht mehr messbar.
  let listenLoaded = false, LISTEN_META = { outcomes: {}, channels: {} };
  function ensureListenLoaded() { if (!listenLoaded) loadListen(); }

  async function loadListen(selectId) {
    const box = $("#listenBody");
    let d;
    try { d = await api("/api/lists"); }
    catch (e) { box.innerHTML = `<p class="hint status-error">Fehler: ${esc(e.message)}</p>`; return; }
    listenLoaded = true;
    LISTEN_META = { outcomes: d.outcomes || {}, channels: d.channels || {} };
    const sel = $("#listenPicker");
    if (!d.lists.length) {
      sel.innerHTML = `<option>— noch keine Liste —</option>`;
      box.innerHTML = `<p class="hint">Noch keine Arbeitsliste. In <b>ICP-Profil</b> ein Profil
        öffnen und dort auf <b>„Als Liste anlegen"</b> klicken — dabei wird die Kontrollgruppe gezogen.</p>`;
      $("#listenWirkung").textContent = "";
      return;
    }
    sel.innerHTML = d.lists.map(l =>
      `<option value="${l.id}">${esc(l.name)} — ${l.n} Firmen, ${l.entschieden} entschieden</option>`).join("");
    if (selectId) sel.value = String(selectId);
    if (!sel.dataset.wired) {
      sel.addEventListener("change", () => renderListe(Number(sel.value)));
      $("#listenOffen").addEventListener("change", () => renderListe(Number(sel.value)));
      sel.dataset.wired = "1";
    }
    // Der Export folgt der Auswahl: wer eine Liste offen hat, will meistens
    // diese Datei, nicht alle drei. Alle bekommt man ueber die Mappe ohne
    // Parameter — deshalb bleibt der Titel ehrlich beschriftet.
    const exp = $("#listenExport");
    if (exp) {
      const setzeZiel = () => {
        exp.href = `/api/lists/export?list_id=${sel.value}`;
        const l = d.lists.find(x => String(x.id) === String(sel.value));
        exp.title = l ? `„${l.name}" als Excel — Kontrollgruppe markiert und rot hinterlegt`
                      : "Liste als Excel";
      };
      setzeZiel();
      if (!exp.dataset.wired) { sel.addEventListener("change", setzeZiel); exp.dataset.wired = "1"; }
    }
    renderListe(Number(sel.value));
  }

  async function renderListe(id) {
    const box = $("#listenBody");
    box.innerHTML = `<p class="hint">Lädt…</p>`;
    const openOnly = $("#listenOffen").checked;
    let d, w;
    try {
      [d, w] = await Promise.all([
        api(`/api/lists/${id}?open_only=${openOnly ? "true" : "false"}`),
        api(`/api/lists/${id}/wirkung`),
      ]);
    } catch (e) { box.innerHTML = `<p class="hint status-error">Fehler: ${esc(e.message)}</p>`; return; }

    const up = w.uplift == null ? "—" : `${(w.uplift * 100).toFixed(1)} Pp.`;
    $("#listenWirkung").innerHTML =
      `Ziel ${w.ziel.kaeufer}/${w.ziel.n} · Kontrolle ${w.kontrolle.kaeufer}/${w.kontrolle.n}` +
      ` · <b>Wirkung ${up}</b>` +
      (w.aussagekraeftig ? "" : ` <span class="qual-badge qual-schwach">nicht belastbar</span>`);

    const opts = Object.entries(LISTEN_META.outcomes)
      .map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    const chans = Object.entries(LISTEN_META.channels)
      .map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");

    box.innerHTML = `
      <p class="hint">${esc(w.hinweis)}</p>
      <table class="data-table"><thead><tr>
        <th style="width:38px">#</th><th>Firma</th><th>Ort</th><th>Branche</th>
        <th style="width:96px">Arm</th><th style="width:300px">Ergebnis</th>
      </tr></thead><tbody>
      ${d.entries.map(e => e.arm === "kontrolle" ? `
        <tr class="arm-kontrolle">
          <td class="muted">${e.rank}</td>
          <td>${esc(e.name || "—")}</td><td>${esc(e.city || "")}</td>
          <td class="sub">${esc(e.sub_segment || "")}</td>
          <td><span class="qual-badge qual-schwach">Kontrolle</span></td>
          <td class="sub">nicht ansprechen — sie misst, was ohne uns passiert</td>
        </tr>` : `
        <tr data-entry="${e.entry_id}" data-cid="${e.company_id}">
          <td class="muted">${e.rank}</td>
          <td><b class="listen-open" style="cursor:pointer">${esc(e.name || "—")}</b></td>
          <td>${esc(e.city || "")}</td>
          <td class="sub">${esc(e.sub_segment || "")}</td>
          <td><span class="qual-badge qual-mittel">Ziel</span></td>
          <td>
            ${e.outcome
              ? `<b>${esc(LISTEN_META.outcomes[e.outcome] || e.outcome)}</b>
                 <span class="sub">${esc(LISTEN_META.channels[e.channel] || e.channel || "")}</span>
                 <button class="btn btn-sm btn-ghost listen-undo">ändern</button>`
              : `<select class="listen-outcome" style="width:150px"><option value="">Ergebnis…</option>${opts}</select>
                 <select class="listen-channel" style="width:104px">${chans}</select>
                 <button class="btn btn-sm btn-primary listen-save">Speichern</button>`}
          </td>
        </tr>`).join("")}
      </tbody></table>`;

    $$("#listenBody .listen-open").forEach(b => b.addEventListener("click", () =>
      openCompanyDrawer(Number(b.closest("tr").dataset.cid))));
    $$("#listenBody .listen-save").forEach(b => b.addEventListener("click", async () => {
      const tr = b.closest("tr");
      const outcome = $(".listen-outcome", tr).value;
      if (!outcome) { toast("Bitte ein Ergebnis wählen.", "error"); return; }
      b.disabled = true;
      try {
        await api(`/api/lists/entries/${tr.dataset.entry}`, "POST",
                  { outcome, channel: $(".listen-channel", tr).value });
        toast("✓ Ergebnis gespeichert.");
        renderListe(id);
      } catch (e) { toast(`Fehlgeschlagen: ${e.message}`, "error"); b.disabled = false; }
    }));
    $$("#listenBody .listen-undo").forEach(b => b.addEventListener("click", async () => {
      const tr = b.closest("tr");
      try {
        await api(`/api/lists/entries/${tr.dataset.entry}`, "POST",
                  { outcome: null, contacted: false });
        renderListe(id);
      } catch (e) { toast(`Fehlgeschlagen: ${e.message}`, "error"); }
    }));
  }

  // "Als Liste anlegen" aus einer Profilansicht heraus — hier wird die
  // Kontrollgruppe gezogen, EINMAL und mit festgehaltenem Startwert.
  async function createListFrom(kind, rows, label) {
    if (!rows.length) { toast("Keine Zeilen zum Anlegen.", "error"); return; }
    const name = prompt("Name der Arbeitsliste:",
                        `${label} — ${new Date().toLocaleDateString("de-DE")}`);
    if (!name) return;
    try {
      const r = await api("/api/lists", "POST",
                          { name, source: kind, rows, holdout_share: 0.15 });
      toast(`✓ Liste angelegt: ${r.n_ziel} Ziel, ${r.n_kontrolle} Kontrolle.`);
      listenLoaded = false;
      gotoTab("listen");
      loadListen(r.id);
    } catch (e) { toast(`Fehlgeschlagen: ${e.message}`, "error"); }
  }

  function listButton(kind, label) {
    return `<button class="btn btn-sm btn-primary" id="mkList" data-kind="${kind}"
      data-label="${esc(label)}" title="Legt die Liste an und zieht dabei eine Kontrollgruppe (15 %)"
      style="margin-bottom:10px">＋ Als Liste anlegen</button>`;
  }

  function wireListButton(rows) {
    const b = $("#mkList");
    if (b) b.addEventListener("click", () => createListFrom(b.dataset.kind, rows, b.dataset.label));
  }

  // ================= KARTE ===============================================
  // EINE Maschine, zwei Instanzen. Firmen und Objekte unterscheiden sich in
  // den Daten und in den Kategorien, nicht in der Mechanik — vorher waren es
  // zwei fast gleiche Leaflet-Aufbauten plus ein dritter für die Höhe.
  //
  // Warum MapLibre statt Leaflet:
  //   * Die dunkle Grundkarte war ein CSS-Filter auf invertierten OSM-Kacheln.
  //     Das ergibt olivgrünes Land und lachsfarbene Straßen — eine Karte, die
  //     man nicht lesen will. CARTO liefert eine ECHTE dunkle Karte, als
  //     Vektor und (als Rückfall) als Raster. Kein Filter, keine Farbunfälle.
  //   * Cluster tragen jetzt eine Aussage. Eine violette Blase mit „413" sagt
  //     nur, dass dort 413 Punkte liegen. Ein Ring aus Segmenten sagt, wie
  //     viele davon gewonnen, offen und verloren sind — dieselbe Fläche,
  //     ungleich mehr Auskunft.
  //   * Flach und Höhe sind DIESELBE Karte mit anderer Kameraneigung, nicht
  //     zwei Karten nebeneinander.
  //
  // Von außen bleibt alles, was der Rest der App benutzt: showCustMap(),
  // loadCustMapPins(), objDim() und so fort.

  // OpenFreeMap statt CARTO für die Vektorkarte: CARTOs Vektorkacheln verlangen
  // inzwischen einen Schlüssel und schreiben sonst „API KEY REQUIRED" quer über
  // die Karte. OpenFreeMap ist frei und ohne Schlüssel nutzbar (OSM-Daten,
  // eigene Server) — geprüft: 200, application/json, kein Wasserzeichen.
  const VEKTOR_STIL = "https://tiles.openfreemap.org/styles/dark";
  // Rasterkarte: Esri „Dark Gray Canvas", zwei Ebenen (Fläche + Beschriftung).
  //
  // CARTO fiel raus: sowohl die Vektor- als auch die Rasterkacheln antworten
  // inzwischen mit 200 — und schreiben „API KEY REQUIRED" quer über das Bild.
  // Ein Statuscode allein ist eben kein Beweis, dass eine Kachel brauchbar ist;
  // das musste erst auf dem Schirm auffallen. Esri liefert ohne Schlüssel und
  // ohne Wasserzeichen, und die Karte ist von Haus aus dunkel — kein Filter,
  // keine Farbunfälle.
  const ESRI = "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas";
  const RASTER_STIL = {
    version: 8,
    sources: {
      flaeche: { type: "raster", tileSize: 256, maxzoom: 16,
        tiles: [`${ESRI}/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`],
        attribution: "Esri, HERE, Garmin, &copy; OpenStreetMap-Mitwirkende" },
      schrift: { type: "raster", tileSize: 256, maxzoom: 16,
        tiles: [`${ESRI}/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}`] },
    },
    layers: [
      { id: "grund", type: "background", paint: { "background-color": "#04060f" } },
      { id: "flaeche", type: "raster", source: "flaeche",
        paint: { "raster-opacity": 0.9 } },
      { id: "schrift", type: "raster", source: "schrift",
        paint: { "raster-opacity": 0.75 } },
    ],
  };

  const LEER_STIL = {
    version: 8, sources: {},
    layers: [{ id: "grund", type: "background",
               paint: { "background-color": "#05060f" } }],
  };

  // Die Grundkarte auf die eigene Palette ziehen. Das ist der Gewinn von
  // Vektorkacheln: der Stil ist Daten, kein Bild. CARTOs „dark matter" ist
  // fast reines Schwarz — Land und Wasser unterscheiden sich kaum.
  function stilAnpassen(m) {
    let s; try { s = m.getStyle(); } catch { return; }
    (s.layers || []).forEach(l => {
      const id = l.id, art = l.type;
      try {
        if (art === "background") m.setPaintProperty(id, "background-color", "#080d1e");
        else if (/water|ocean|sea|bathym/i.test(id) && art === "fill")
          m.setPaintProperty(id, "fill-color", "#050813");
        else if (/land|earth|park|wood|forest|grass|sand/i.test(id) && art === "fill")
          m.setPaintProperty(id, "fill-color", "#0d1428");
        else if (/building/i.test(id) && art === "fill")
          m.setPaintProperty(id, "fill-color", "#141d38");
        else if (/boundary|admin|border/i.test(id) && art === "line") {
          m.setPaintProperty(id, "line-color", "rgba(124,140,255,.34)");
          m.setPaintProperty(id, "line-width", 0.9);
        } else if (/road|highway|transport|bridge|tunnel|rail/i.test(id) && art === "line") {
          m.setPaintProperty(id, "line-color", "rgba(96,116,180,.45)");
          m.setPaintProperty(id, "line-opacity", 0.3);
        } else if (art === "symbol") {
          m.setPaintProperty(id, "text-color", "rgba(168,183,224,.66)");
          m.setPaintProperty(id, "text-halo-color", "rgba(4,6,14,.92)");
        }
      } catch { /* Ebene kennt die Eigenschaft nicht */ }
    });
  }

  // Drei Grundkarten zur Wahl. Vektor ist die schöne, Einfach die robuste
  // (Rasterbilder kommen auch durch Netze, die Vektorkacheln im Worker
  // blockieren), Ohne die, die immer geht.
  const GRUNDKARTEN = {
    vektor: { stil: VEKTOR_STIL, label: "Karte",
              hilfe: "Vektorkarte (OpenFreeMap) — scharf auf jeder Zoomstufe" },
    raster: { stil: RASTER_STIL, label: "Einfach",
              hilfe: "Rasterkarte (Esri) — kommt auch durch strenge Firmennetze" },
    leer:   { stil: LEER_STIL, label: "Ohne",
              hilfe: "Keine Grundkarte — nur die Daten, immer verfügbar" },
  };

  // --- Ringdiagramm als Cluster -------------------------------------------
  // Ein Cluster ist kein Haufen, sondern eine Mischung. Der Ring zeigt sie in
  // einem Blick; die Zahl in der Mitte bleibt lesbar.
  function ringBogen(von, bis, r, r0, farbe) {
    if (bis - von === 1) bis -= 0.00001;
    const a0 = 2 * Math.PI * von - Math.PI / 2, a1 = 2 * Math.PI * bis - Math.PI / 2;
    const x0 = Math.cos(a0), y0 = Math.sin(a0), x1 = Math.cos(a1), y1 = Math.sin(a1);
    const gross = bis - von > 0.5 ? 1 : 0;
    return `<path d="M ${r + r0 * x0} ${r + r0 * y0} L ${r + r * x0} ${r + r * y0} `
      + `A ${r} ${r} 0 ${gross} 1 ${r + r * x1} ${r + r * y1} L ${r + r0 * x1} ${r + r0 * y1} `
      + `A ${r0} ${r0} 0 ${gross} 0 ${r + r0 * x0} ${r + r0 * y0}" fill="${farbe}"/>`;
  }

  function ringHtml(zahlen, farben, gesamt) {
    // Radius nach Größenordnung, nicht linear: sonst wäre ein 10.000er-Cluster
    // dreißigmal so breit wie ein 300er und verdeckte den halben Bildschirm.
    const r = gesamt >= 5000 ? 30 : gesamt >= 1000 ? 26 : gesamt >= 200 ? 22
            : gesamt >= 30 ? 18 : 15;
    const r0 = Math.round(r * 0.62), w = r * 2;
    let html = `<svg width="${w}" height="${w}" viewBox="0 0 ${w} ${w}" `
      + `class="ring-svg" text-anchor="middle" style="font:600 ${r < 18 ? 10 : 12}px var(--sans,sans-serif)">`;
    let off = 0;
    zahlen.forEach((n, i) => {
      if (!n) return;
      html += ringBogen(off / gesamt, (off + n) / gesamt, r, r0, farben[i]);
      off += n;
    });
    html += `<circle cx="${r}" cy="${r}" r="${r0}" fill="rgba(9,13,30,.94)"/>`
      + `<text dominant-baseline="central" transform="translate(${r},${r})" fill="#e8ecff">`
      + `${gesamt.toLocaleString("de-DE")}</text></svg>`;
    const el = document.createElement("div");
    el.className = "ring-cluster";
    el.innerHTML = html;
    return el;
  }

  // --- Eine Karteninstanz --------------------------------------------------
  function karteBauen(id, opt) {
    const zustand = {
      m: null, marker: {}, aufDemSchirm: {}, kategorien: opt.kategorien,
      punkte: [], art: "vektor", hinweis: opt.hinweis || (() => {}),
    };

    let start = "vektor";
    try { start = localStorage.getItem("adwatch.grundkarte") || "vektor"; } catch { /* privat */ }
    if (!GRUNDKARTEN[start]) start = "vektor";
    zustand.art = start;

    const m = new maplibregl.Map({
      container: id, style: GRUNDKARTEN[start].stil,
      center: [10, 50.5], zoom: 4, attributionControl: { compact: true },
      // Eine Erde: renderWorldCopies verhindert, dass die Welt beim Rauszoomen
      // endlos nebeneinander kachelt. NICHT maxBounds dafür verwenden — eine
      // Weltbegrenzung streitet sich mit fitBounds, und die Kamera landete
      // dabei auf Zoom 22 am Antimeridian, also auf ein paar Quadratmetern
      // Nichts. Leaflet brauchte den Umweg, MapLibre hat den Schalter.
      minZoom: 1.6, renderWorldCopies: false,
    });
    zustand.m = m;
    m.addControl(new maplibregl.NavigationControl({ visualizePitch: !!opt.hoehe }), "top-left");
    m.addControl(new maplibregl.ScaleControl({ maxWidth: 110, unit: "metric" }), "bottom-left");

    const ebenenBauen = () => {
      if (zustand.art === "vektor") stilAnpassen(m);
      try { datenEbenen(zustand, opt); } catch (e) {
        console.error("Kartenebenen:", e);
        zustand.hinweis("Karte konnte nicht aufgebaut werden: " + e.message);
      }
    };

    m.on("style.load", ebenenBauen);

    // --- Grundkarte: ausdrücklich gewählt, nicht automatisch erraten --------
    //
    // Vorher stand hier eine Kette, die bei ausbleibenden Kacheln selbsttätig
    // eine Stufe zurückschaltete. Sie war die Ursache jedes Kartenfehlers an
    // diesem Tag: sie feuerte schon, wenn die Leitung nur langsam war, sie
    // stritt sich mit setStyle, und sie hinterließ die Karte in Halbzuständen —
    // einmal Grundkarte ohne Daten, einmal Daten ohne Grundkarte.
    //
    // Jetzt entscheidet der Mensch: drei Knöpfe auf der Karte, die Wahl bleibt
    // gespeichert. Ein blockierendes Firmennetz ist damit ein Klick und keine
    // Kaskade, die im falschen Moment auslöst.
    const setzeGrundkarte = (art, nurKnoepfe) => {
      const g = GRUNDKARTEN[art] || GRUNDKARTEN.vektor;
      zustand.art = art;
      try { localStorage.setItem("adwatch.grundkarte", art); } catch { /* privat */ }
      $$("button", leiste).forEach(b => b.classList.toggle("active", b.dataset.basis === art));
      if (nurKnoepfe) return;
      m.setStyle(g.stil);
      // 'style.load' feuert nach setStyle nicht zuverlässig — gewartet wird auf
      // den Zustand, sonst stünde die Grundkarte ohne eine einzige Datenebene da.
      const warte = setInterval(() => {
        if (!m.isStyleLoaded()) return;
        clearInterval(warte);
        ebenenBauen();
      }, 120);
      setTimeout(() => clearInterval(warte), 20000);
    };
    zustand.setzeGrundkarte = setzeGrundkarte;

    // Fehler werden GEZEIGT, nicht stillschweigend umgangen. Wer sieht, dass
    // die Grundkarte nicht kommt, schaltet um — und weiß dabei, was er tut.
    let gemeldet = false;
    m.on("error", (e) => {
      const text = String((e && (e.error && e.error.message || e.error)) || "");
      if (gemeldet || !/style|sprite|glyph|tiles/i.test(text)) return;
      gemeldet = true;
      console.warn("Grundkarte:", text);
      zustand.hinweis("Grundkarte lädt nicht — unten rechts auf „Einfach“ umschalten.");
    });

    const leiste = document.createElement("div");
    leiste.className = "map-basis";
    leiste.innerHTML = Object.entries(GRUNDKARTEN).map(([k, g]) =>
      `<button type="button" data-basis="${k}" title="${esc(g.hilfe)}">${esc(g.label)}</button>`).join("");
    m.getContainer().appendChild(leiste);
    $$("button", leiste).forEach(b =>
      b.addEventListener("click", () => setzeGrundkarte(b.dataset.basis)));
    setzeGrundkarte(start, true);

    return zustand;
  }

  function datenEbenen(zustand, opt) {
    const m = zustand.m;
    const farben = opt.farben();
    if (m.getSource("pins")) return;   // nach setStyle neu, sonst schon da

    m.addSource("pins", {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] },
      // maxzoom begrenzt, wie viele Zoomstufen supercluster im Voraus indiziert.
      // Ohne die Grenze baut es 0..18 — bei 42.683 Firmen dauerte das im Test
      // fast eine Minute, in der die Karte leer aussah. Ab Stufe 12 werden
      // ohnehin Einzelpunkte gezeigt (clusterMaxZoom), darüber wird die
      // oberste Kachel gestreckt; sichtbar ändert das nichts.
      maxzoom: 12,
      cluster: true, clusterRadius: 48, clusterMaxZoom: 12,
      // Je Kategorie mitzählen — daraus wird der Ring. Ohne diese Aggregate
      // wüsste der Cluster nur, WIE VIELE, nicht WELCHE.
      clusterProperties: Object.fromEntries(opt.kategorien.map((_, i) =>
        [`k${i}`, ["+", ["case", ["==", ["get", "t"], i], 1, 0]]])),
    });

    const farbAusdruck = ["match", ["get", "t"],
      ...opt.kategorien.flatMap((_, i) => [i, farben[i]]), "#64708a"];

    // Hof: macht einen einzelnen Punkt auch auf Kontinentmaßstab auffindbar.
    m.addLayer({ id: "pin-hof", type: "circle", source: "pins",
      filter: ["!", ["has", "point_count"]], paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 7, 10, 16],
        "circle-color": farbAusdruck, "circle-blur": 1,
        "circle-opacity": 0.32 } });
    m.addLayer({ id: "pin", type: "circle", source: "pins",
      filter: ["!", ["has", "point_count"]], paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 3, 10, 6.5],
        "circle-color": farbAusdruck,
        "circle-stroke-width": 1, "circle-stroke-color": "rgba(255,255,255,.6)" } });

    if (opt.hoehe) {
      // Nur die Objektkarte: der Projektwert als Höhe. Die Ebene liegt bereit
      // und wird über die Sichtbarkeit geschaltet, statt bei jedem Umschalten
      // neu gebaut zu werden.
      m.addSource("saeulen", { type: "geojson", tolerance: 0,
        data: { type: "FeatureCollection", features: [] } });
      m.addLayer({ id: "saeule", type: "fill-extrusion", source: "saeulen",
        layout: { visibility: "none" }, paint: {
          "fill-extrusion-color": ["get", "f"],
          // Der Zoom-Ausdruck MUSS außen stehen — innen verschachtelt lehnt
          // MapLibre ihn ab und die Ebene fehlt kommentarlos.
          "fill-extrusion-height": ["interpolate", ["linear"], ["zoom"],
            2, ["*", ["get", "h"], 4.3], 4, ["*", ["get", "h"], 1.1],
            6, ["*", ["get", "h"], 0.30], 8, ["*", ["get", "h"], 0.073],
            10, ["*", ["get", "h"], 0.019], 12, ["*", ["get", "h"], 0.0047],
            14, ["*", ["get", "h"], 0.0012]],
          "fill-extrusion-opacity": 0.78,
          "fill-extrusion-vertical-gradient": true } });
    }

    const pop = new maplibregl.Popup({ closeButton: false, offset: 12,
                                       className: "karte-pop" });
    const zeigen = (e) => {
      const f = e.features && e.features[0];
      if (!f) return;
      m.getCanvas().style.cursor = "pointer";
      pop.setLngLat(e.lngLat).setHTML(opt.popup(f.properties)).addTo(m);
    };
    ["pin", ...(opt.hoehe ? ["saeule"] : [])].forEach(ebene => {
      m.on("mousemove", ebene, zeigen);
      m.on("mouseleave", ebene, () => { m.getCanvas().style.cursor = ""; pop.remove(); });
      if (opt.klick) m.on("click", ebene, (e) => {
        const f = e.features && e.features[0];
        if (f) opt.klick(f.properties);
      });
    });

    m.on("moveend", () => ringeZeichnen(zustand, opt));
    m.on("sourcedata", (e) => {
      if (e.sourceId !== "pins" || !e.isSourceLoaded) return;
      ringeZeichnen(zustand, opt);
      if (opt.fertig) opt.fertig();
    });
    // Kommen die Daten VOR dem Stil an, hat datenSetzen sie nur gemerkt und ist
    // ausgestiegen (die Quelle gab es noch nicht). Hier werden sie nachgereicht
    // — und zwar MIT Einpassen, falls das noch nie lief: sonst bleibt die Karte
    // auf der Startansicht stehen, obwohl sie die Daten hat. Genau das war beim
    // Test der Firmenkarte der Fall.
    if (zustand.punkte.length)
      datenSetzen(zustand, opt, zustand.punkte, zustand.eingepasst);
  }

  // Ringe sind DOM-Marker, keine Kartenebene: ein Kreisdiagramm lässt sich
  // nicht als circle-layer ausdrücken. Gezeichnet werden nur die Cluster im
  // Bild — typisch unter hundert, auch wenn 42.683 Punkte in der Quelle liegen.
  function ringeZeichnen(zustand, opt) {
    const m = zustand.m;
    if (!m.getSource("pins")) return;
    const farben = opt.farben();
    const neu = {};
    let merkmale;
    try { merkmale = m.querySourceFeatures("pins"); } catch { return; }
    for (const f of merkmale) {
      if (!f.properties.cluster) continue;
      const id = f.properties.cluster_id;
      let marker = zustand.marker[id];
      if (!marker) {
        const zahlen = opt.kategorien.map((_, i) => f.properties[`k${i}`] || 0);
        const el = ringHtml(zahlen, farben, f.properties.point_count);
        el.addEventListener("click", () => {
          m.getSource("pins").getClusterExpansionZoom(id).then(z =>
            m.easeTo({ center: f.geometry.coordinates, zoom: z, duration: 600 }))
            .catch(() => {});
        });
        el.title = opt.kategorien.map((k, i) =>
          `${k.label}: ${(f.properties[`k${i}`] || 0).toLocaleString("de-DE")}`).join(" · ");
        marker = zustand.marker[id] =
          new maplibregl.Marker({ element: el }).setLngLat(f.geometry.coordinates);
      }
      neu[id] = marker;
      if (!zustand.aufDemSchirm[id]) marker.addTo(m);
    }
    for (const id in zustand.aufDemSchirm) if (!neu[id]) zustand.aufDemSchirm[id].remove();
    zustand.aufDemSchirm = neu;
  }

  function datenSetzen(zustand, opt, punkte, ohneFlug) {
    const m = zustand.m;
    zustand.punkte = punkte;
    if (!m.getSource("pins")) return;
    m.getSource("pins").setData(geoJsonAus(punkte));
    // Ringe der alten Daten wegräumen, sonst kleben sie über den neuen.
    for (const id in zustand.aufDemSchirm) zustand.aufDemSchirm[id].remove();
    zustand.marker = {}; zustand.aufDemSchirm = {};

    if (opt.hoehe && m.getSource("saeulen"))
      m.getSource("saeulen").setData(saeulenGeoJson(punkte, m.getZoom(), opt.farben()));

    if (!ohneFlug) einpassen(zustand, punkte);
  }

  function geoJsonAus(punkte) {
    return { type: "FeatureCollection", features: punkte.map(p => ({
      type: "Feature", properties: { t: p.t, ...p.props },
      geometry: { type: "Point", coordinates: [p.lng, p.lat] } })) };
  }

  // Wohin die Karte schaut: auf das mittlere 98 % der Punkte. Über ALLE wäre es
  // eine Weltansicht — ein paar Dutzend Adressen liegen in Asien und Amerika,
  // und zwei Punkte auf zwei Kontinenten zwingen den Maßstab, während 99 % der
  // Daten als ein Fleck darin verschwinden. Die Ausreißer bleiben da; sie
  // bestimmen nur nicht mehr den ersten Eindruck.
  function einpassen(zustand, punkte) {
    const m = zustand.m;
    if (!punkte.length) return;
    zustand.eingepasst = true;
    const q = (w, x) => w[Math.min(w.length - 1, Math.max(0, Math.round((w.length - 1) * x)))];
    const lat = punkte.map(p => p.lat).sort((a, b) => a - b);
    const lng = punkte.map(p => p.lng).sort((a, b) => a - b);
    const el = m.getContainer();
    m.fitBounds([[q(lng, 0.01), q(lat, 0.01)], [q(lng, 0.99), q(lat, 0.99)]], {
      duration: 900, maxZoom: 9,
      padding: {
        top: Math.min(90, el.clientHeight * 0.14),
        bottom: Math.min(60, el.clientHeight * 0.1),
        left: Math.min(60, el.clientWidth * 0.06),
        right: Math.min(230, el.clientWidth * 0.2),
      },
    });
  }

  // Ein Punkt kann nicht extrudiert werden — fill-extrusion braucht Flächen.
  // Die Grundfläche wächst mit dem Zoom: feste 500 m sind bei Zoom 4 ganze
  // 0,12 Pixel breit, und die Kachelvereinfachung wirft sie dann komplett weg.
  const SAEULE_PX = 2;
  function saeulenGeoJson(punkte, zoom, farben) {
    const d = (SAEULE_PX / 2) * 360 / (256 * Math.pow(2, zoom));
    return { type: "FeatureCollection", features: punkte
      .filter(p => p.props && p.props.wert)
      .map(p => {
        const dx = d / Math.max(0.2, Math.cos(p.lat * Math.PI / 180));
        return { type: "Feature",
          properties: { ...p.props, f: farben[p.t] || "#64708a",
            // Wurzelähnliche Stauchung: linear wäre das 6-Mio-Projekt 200-mal
            // so hoch wie ein 30.000-Euro-Auftrag und alles andere ein Teppich.
            h: Math.max(2500, Math.pow(p.props.wert || 1, 0.62) * 22) },
          geometry: { type: "Polygon", coordinates: [[
            [p.lng - dx, p.lat - d], [p.lng + dx, p.lat - d],
            [p.lng + dx, p.lat + d], [p.lng - dx, p.lat + d], [p.lng - dx, p.lat - d]]] } };
      }) };
  }

  // --- Farben kommen aus dem Stylesheet ------------------------------------
  // Sonst tragen Legende (CSS) und Pin (JS) zwei Wahrheiten, und beim
  // Hautwechsel zieht nur eine von beiden mit.
  const hautFarbe = (name, ersatz) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim() || ersatz;
  const mapFarben = () => [hautFarbe("--map-kunde", "#3ce8b0"),
                           hautFarbe("--map-architekt", "#f6b954"),
                           hautFarbe("--map-interessent", "#8b5cf6")];
  const objFarben = () => [hautFarbe("--map-gewonnen", "#3ce8b0"),
                           hautFarbe("--map-offen", "#8b5cf6"),
                           hautFarbe("--map-verloren", "#fb7185")];
  let MAP_TYPE_COLOR = mapFarben(), OBJ_TYPE_COLOR = objFarben();

  const FIRMEN_KAT = [{ key: "kunde", label: "Kunde" },
                      { key: "architekt", label: "Architekt/Planer" },
                      { key: "interessent", label: "Interessent" }];
  const OBJ_KAT = [{ key: "gewonnen", label: "gewonnen" },
                   { key: "offen", label: "offen" },
                   { key: "verloren", label: "verloren" }];

  // ================= FIRMENKARTE ==========================================
  let custMap = null, custMapPins = [];
  let custPinsKey = null, custPinsLauf = null, custPinsSeq = 0;

  function zaehlerFirmen() {
    if (!custMap) return;
    const b = custMap.m.getBounds();
    const n = custMapPins.reduce((a, p) => a + (b.contains([p.lng, p.lat]) ? 1 : 0), 0);
    // „…wird gezeichnet" solange die Cluster noch entstehen. Die Zahl stimmt
    // zwar (sie kommt aus den geladenen Daten, nicht aus der Karte), aber neben
    // einer noch leeren Karte liest sie sich wie ein Fehler.
    const fertig = custMap.m.isSourceLoaded("pins")
      && custMap.m.querySourceFeatures("pins").length > 0;
    $("#exploreCount").textContent =
      `Im Kartenausschnitt: ${deN(n)} von ${deN(custMapPins.length)} Firmen`
      + (fertig ? "" : " — wird gezeichnet …");
  }

  function loadCustMapPins() {
    const filters = currentCustomerFilters();
    const key = JSON.stringify(filters);
    if (custPinsKey === key && (custPinsLauf || custMapPins.length))
      return custPinsLauf || Promise.resolve();
    custPinsKey = key;
    custPinsLauf = _ladeCustMapPins(filters).finally(() => { custPinsLauf = null; });
    return custPinsLauf;
  }

  async function _ladeCustMapPins(filters) {
    const seq = ++custPinsSeq;
    $("#exploreCount").textContent = "Lade Pins…";
    let d;
    try { d = await api("/api/map/pins", "POST", { filters }); }
    catch (e) { $("#exploreCount").textContent = `Fehler: ${e.message}`; return; }
    if (seq !== custPinsSeq || !custMap) return;   // überholt
    const typ = { kunde: 0, architekt: 1, interessent: 2 };
    custMapPins = d.pins.map(p => ({
      lat: p.lat, lng: p.lng, t: typ[p.typ] ?? 2,
      props: { id: p.id, name: p.name, ort: p.city || "", prec: p.prec },
    }));
    datenSetzen(custMap, custOpt, custMapPins);
    zaehlerFirmen();
  }

  const custOpt = {
    kategorien: FIRMEN_KAT,
    farben: () => MAP_TYPE_COLOR,
    popup: (f) => `<div class="kp-t">${esc(f.name)}</div>`
      + `<div class="kp-s">${esc(f.ort)}${f.prec === "plz" ? " · PLZ-Zentroid" : ""}</div>`
      + `<div class="kp-a">Klicken öffnet das Dossier</div>`,
    klick: (f) => { location.hash = `#/firma/${f.id}`; },
    fertig: () => zaehlerFirmen(),
  };

  async function showCustMap() {
    if (typeof maplibregl === "undefined") {
      $("#exploreCount").textContent = "Kartenbibliothek nicht geladen (static/vendor fehlt?)";
      return;
    }
    if (!custMap) {
      custMap = karteBauen("custMap", custOpt);
      custMap.m.on("moveend", zaehlerFirmen);
      window._custMap = custMap.m;
    }
    custMap.m.resize();
    await loadCustMapPins();
  }

  // ================= OBJEKTKARTE ==========================================
  let objMap = null, objMapPins = [], objOhne = 0;
  let objPinsKey = null, objPinsLauf = null, objPinsSeq = 0;

  function objekteQuery() {
    const st = $("#objekteStatus").value;
    const [lo, hi] = String($("#objekteVcs").value).split("-");
    return `min_members=${lo}` + (hi ? `&max_members=${hi}` : "")
      + (st ? `&status=${st}` : "") + serverParamQuery("objekteWrap");
  }

  function zaehlerObjekte() {
    if (!objMap) return;
    const b = objMap.m.getBounds();
    const n = objMapPins.reduce((a, p) => a + (b.contains([p.lng, p.lat]) ? 1 : 0), 0);
    $("#exploreCount").textContent =
      `Im Kartenausschnitt: ${deN(n)} von ${deN(objMapPins.length)} Objekten`
      + (objOhne ? ` · ${deN(objOhne)} ohne Bauadresse` : "");
  }

  function loadObjMapPins() {
    const query = objekteQuery();
    if (objPinsKey === query && (objPinsLauf || objMapPins.length))
      return objPinsLauf || Promise.resolve();
    objPinsKey = query;
    objPinsLauf = _ladeObjMapPins(query).finally(() => { objPinsLauf = null; });
    return objPinsLauf;
  }

  async function _ladeObjMapPins(query) {
    const seq = ++objPinsSeq;
    $("#exploreCount").textContent = "Lade Pins…";
    let d;
    try { d = await api(`/api/map/projekt-pins?${query}`); }
    catch (e) { $("#exploreCount").textContent = `Fehler: ${e.message}`; return; }
    if (seq !== objPinsSeq || !objMap) return;
    const typ = { gewonnen: 0, offen: 1, verloren: 2 };
    objOhne = d.ohne_koordinate || 0;
    objMapPins = d.pins.map(p => ({
      lat: p.lat, lng: p.lng, t: typ[p.typ] ?? 1,
      props: { id: p.id, name: p.name, ort: p.city || "",
               wert: p.value || 0, n: p.members },
    }));
    datenSetzen(objMap, objOpt, objMapPins);
    zaehlerObjekte();
  }

  const objOpt = {
    kategorien: OBJ_KAT, hoehe: true,
    farben: () => OBJ_TYPE_COLOR,
    popup: (f) => `<div class="kp-t">${esc(f.name)}</div>`
      + `<div class="kp-s">${esc(f.ort)}${f.wert ? " · " + eur(Number(f.wert)) : ""}`
      + ` · ${f.n} Verkaufschance${Number(f.n) === 1 ? "" : "n"}</div>`
      + `<div class="kp-a">Klicken öffnet die Objektakte</div>`,
    klick: (f) => openProjektDrawer(f.id),
    fertig: () => zaehlerObjekte(),
    hinweis: (text) => { const el = $("#objMapHinweis"); if (el) el.textContent = text; },
  };

  async function showObjMap() {
    if (typeof maplibregl === "undefined") {
      $("#exploreCount").textContent = "Kartenbibliothek nicht geladen";
      return;
    }
    if (!objMap) {
      objMap = karteBauen("objMap", objOpt);
      objMap.m.on("moveend", zaehlerObjekte);
      objMap.m.on("zoomend", () => {
        // Die Säulen-Grundflächen müssen mit dem Zoom nachgemasselt werden.
        if (!objHoch || !objMap.m.getSource("saeulen")) return;
        objMap.m.getSource("saeulen")
          .setData(saeulenGeoJson(objMapPins, objMap.m.getZoom(), objOpt.farben()));
      });
      window._objMap = objMap.m;
    }
    objMap.m.resize();
    await loadObjMapPins();
    let dim = "flach";
    try { dim = localStorage.getItem("adwatch.objDim") || "flach"; } catch { /* privat */ }
    objDim(dim, true);
  }

  // --- flach | Höhe: dieselbe Karte, andere Kamera -------------------------
  // Vorher waren das zwei Karteninstanzen nebeneinander, die sich Zustand und
  // Kachelspeicher teilten, ohne voneinander zu wissen. Jetzt ist es ein Kipp-
  // winkel und eine Ebenensichtbarkeit — der Ausschnitt bleibt beim Umschalten
  // erhalten, was vorher die häufigste Beschwerde war.
  let objHoch = false;
  function objDim(dim, still) {
    objHoch = dim === "hoch";
    $$("#objDim button").forEach(b => b.classList.toggle("active", b.dataset.dim === dim));
    try { localStorage.setItem("adwatch.objDim", dim); } catch { /* privat */ }
    if (!objMap) return;
    const m = objMap.m;
    const setzen = () => {
      if (m.getLayer("saeule"))
        m.setLayoutProperty("saeule", "visibility", objHoch ? "visible" : "none");
      // In der Höhe stören Punkt und Hof: sie liegen flach unter den Säulen und
      // erzeugen einen Teppich, durch den man die Silhouette nicht mehr sieht.
      ["pin", "pin-hof"].forEach(l => { if (m.getLayer(l))
        m.setLayoutProperty(l, "visibility", objHoch ? "none" : "visible"); });
      if (objHoch && m.getSource("saeulen"))
        m.getSource("saeulen").setData(
          saeulenGeoJson(objMapPins, m.getZoom(), objOpt.farben()));
      m.easeTo({ pitch: objHoch ? 52 : 0, bearing: objHoch ? -17 : 0, duration: 700 });
    };
    if (m.isStyleLoaded()) setzen(); else m.once("idle", setzen);
    objOpt.hinweis(objHoch
      ? "Höhe = Projektwert · Position = PLZ-Zentroid der Bauadresse"
      : "Pin-Position = PLZ-Zentroid der Bauadresse (stadtgenau)");
    if (!still) zaehlerObjekte();
  }
  $$("#objDim button").forEach(b =>
    b.addEventListener("click", () => objDim(b.dataset.dim)));

  function objMapSichtbar() {
    return objMap && !$("#objMapWrap").classList.contains("hidden");
  }
  const obj3dSichtbar = () => false;   // es gibt keine zweite Instanz mehr
  const zeigeObj3d = () => objDim("hoch");

  // ================= KONVERSION: Angebot -> Auftrag ========================
  // Die Tabelle zeigt ZWEI Masse nebeneinander, weil eines allein in die Irre
  // fuehrt (Begruendung in insights/konversion.py). Und sie zeigt das
  // Konfidenzintervall als Balken, nicht als Klammerzahl: 37,5 % auf 120
  // Faellen und 21,6 % auf 13.453 sehen als Zahl gleich sicher aus und sind es
  // nicht. Ein Balken, der doppelt so breit ist, sagt das ohne einen Satz.
  let konvGeladen = false;

  async function ladeKonversion() {
    const wrap = $("#konvWrap");
    const dim = $("#konvDimension").value || "segment";
    const land = ($("#konvLand").value || "").trim();
    const min = Number($("#konvMin").value) || 30;
    wrap.innerHTML = `<p class="muted" style="padding:12px">Wird geladen …</p>`;
    let d;
    try {
      d = await api(`/api/konversion?dimension=${encodeURIComponent(dim)}`
        + `&min_entschieden=${min}` + (land ? `&land=${encodeURIComponent(land)}` : ""));
    } catch (e) {
      wrap.innerHTML = `<p class="muted" style="padding:12px">Fehler: ${esc(e.message)}</p>`;
      return;
    }

    // Die Auswahl erst hier fuellen: die Liste der Dimensionen kommt vom Server,
    // damit sie nicht an zwei Orten gepflegt werden muss.
    const sel = $("#konvDimension");
    if (!sel.options.length && d.dimensionen) {
      sel.innerHTML = Object.entries(d.dimensionen)
        .map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
      sel.value = d.dimension;
    }

    $("#konvKpis").innerHTML = `
      <div class="kpi"><div class="kpi-label">Grundlinie Gewinnrate</div>
        <div class="kpi-value">${pct(d.basis_gewinnrate)}</div>
        <div class="sub">${deN(d.entschieden)} entschiedene Verkaufschancen</div></div>
      <div class="kpi"><div class="kpi-label">Angeboten</div>
        <div class="kpi-value" title="${eur(d.angeboten)}">${eurShort(d.angeboten)}</div>
        <div class="sub">Summe der Angebotswerte</div></div>
      <div class="kpi"><div class="kpi-label">Davon fakturiert</div>
        <div class="kpi-value" title="${eur(d.fakturiert)}">${eurShort(d.fakturiert)}</div>
        <div class="sub">${pct(d.basis_euro_quote)} — Untergrenze, siehe Hinweis</div></div>
      <div class="kpi"><div class="kpi-label">Beleg-Deckung</div>
        <div class="kpi-value">${pct(d.beleg_deckung_gesamt)}</div>
        <div class="sub">so viele Chancen tragen einen SAP-Link</div></div>`;
    $("#konvBasis").textContent = d.basis_gewinnrate != null
      ? `Grundlinie ${pct(d.basis_gewinnrate)} — markiert wird nur, wer sie sicher über- oder unterschreitet`
      : "";

    const zeilen = d.zeilen || [];
    if (!zeilen.length) {
      wrap.innerHTML = `<p class="muted" style="padding:12px">Keine Gruppe erreicht
        ${min} entschiedene Verkaufschancen. Schwelle senken oder Land weglassen.</p>`;
      return;
    }
    const maxRate = Math.max(...zeilen.map(z => z.gewinnrate_hi || 0), d.basis_gewinnrate || 0);
    const x = (v) => `${((v || 0) / (maxRate || 1) * 100).toFixed(1)}%`;

    wrap.innerHTML = `
      <table class="data-table">
        <thead><tr>
          <th>${esc(d.titel)}</th>
          <th class="num" title="Nur entschiedene Chancen — offene sind noch nichts">Entschieden</th>
          <th class="num">Gewinnrate</th>
          <th data-nosort title="Der Balken ist das 95-%-Intervall. Breit heißt: wenig Fälle.">Sicherheit</th>
          <th class="num" title="Fakturiert je angeboten — Untergrenze, siehe Hinweis">Euro-Quote</th>
          <th class="num">Angeboten</th>
          <th class="num" title="Anteil der Chancen mit SAP-Beleglink">Belege</th>
        </tr></thead>
        <tbody>${zeilen.map(z => `
          <tr class="${z.ueber_basis ? "konv-ueber" : (z.unter_basis ? "konv-unter" : "")}">
            <td>${esc(String(z.gruppe))}${z.belastbar ? ""
              : ` <span class="chip c-dim" title="unter ${d.min_entschieden} entschiedenen Chancen — als Beleg zu dünn">dünn</span>`}</td>
            <td class="num">${deN(z.entschieden)}</td>
            <td class="num"><b>${pct(z.gewinnrate)}</b></td>
            <td class="konv-bar-zelle">
              <span class="konv-bar" title="95-%-Intervall: ${pct(z.gewinnrate_lo)} bis ${pct(z.gewinnrate_hi)}">
                <i style="left:${x(z.gewinnrate_lo)};width:${x((z.gewinnrate_hi || 0) - (z.gewinnrate_lo || 0))}"></i>
                <u style="left:${x(d.basis_gewinnrate)}"></u>
              </span></td>
            <td class="num">${pct(z.euro_quote)}</td>
            <td class="num" title="${eur(z.angeboten)}">${eurShort(z.angeboten)}</td>
            <td class="num">${pct(z.beleg_deckung)}</td>
          </tr>`).join("")}</tbody>
      </table>`;
  }

  function ensureKonversionLoaded() {
    if (konvGeladen) return;
    konvGeladen = true;
    ["#konvDimension", "#konvLand", "#konvMin"].forEach(id => {
      const el = $(id);
      if (el) el.addEventListener("change", ladeKonversion);
    });
    ladeKonversion();
  }

  // ================= SPALTENFILTER ÜBER DER KARTE =========================
  // Die Kartenansicht soll genau so filtern können wie die Liste. Der billige
  // Weg wäre gewesen, die Filter ein zweites Mal zu bauen — und ab dem Tag
  // hätten Liste und Karte auseinanderdriften können, jedes Mal wenn jemand
  // eine Spalte ergänzt.
  //
  // Stattdessen ist die Leiste eine FERNBEDIENUNG: jeder Knopf klickt den
  // echten Spaltenkopf an, dessen Menü ohnehin existiert, und schiebt danach
  // dasselbe #thMenu unter sich. Beide Tabellen benutzen dieses eine Menü, also
  // funktioniert der Griff für Firmen wie für Projekte. Was die Tabelle kann,
  // kann die Karte damit zwangsläufig auch.
  //
  // Nötig ist das Verschieben, weil der Spaltenkopf in der Kartenansicht
  // ausgeblendet ist: getBoundingClientRect liefert dann Nullen, und das Menü
  // klebte oben links in der Ecke.
  function spaltenFilterLeiste(leisteId, tabelleSel) {
    const box = $(leisteId);
    const tabelle = $(tabelleSel);
    if (!box || !tabelle) return;
    // Nicht auf die Klasse "th-has-menu" prüfen: sie wird von
    // makeTableInteractive gesetzt, und bei der Projekttabelle geschieht das
    // ERST NACH dem Rendern — die Leiste blieb dadurch leer. Ausgeschlossen
    // werden nur Knopf- und Auswahlspalten sowie leere Köpfe.
    const koepfe = $$("thead th", tabelle).filter(th =>
      !th.hasAttribute("data-nomenu")
      && !th.classList.contains("col-selhead")
      && !th.classList.contains("col-dot")
      && th.textContent.replace("▾", "").trim());
    box.innerHTML = koepfe.map((th, i) =>
      `<button type="button" data-i="${i}" class="${th.classList.contains("th-filtered") ? "filtered" : ""}">`
      + `${esc(th.textContent.replace("▾", "").trim())}<span class="caret">▾</span></button>`).join("");
    $$("button", box).forEach(knopf => knopf.addEventListener("click", (e) => {
      e.stopPropagation();
      const th = koepfe[Number(knopf.dataset.i)];
      if (!th) return;
      th.click();
      const menu = $("#thMenu");
      if (!menu || menu.classList.contains("hidden")) return;
      const r = knopf.getBoundingClientRect();
      const w = menu.offsetWidth || 300;
      menu.style.top = Math.round(r.bottom + 6) + "px";
      menu.style.left = Math.round(Math.max(8, Math.min(r.left, innerWidth - w - 12))) + "px";
    }));
  }

  // Nach dem Rendern, nicht waehrenddessen: die Tabelle bekommt ihre
  // Menue-Verdrahtung erst, wenn der Browser sie gesetzt hat.
  function spaltenFilterSpaeter(leisteId, tabelleSel) {
    requestAnimationFrame(() => spaltenFilterLeiste(leisteId, tabelleSel));
  }

  // ================= EXPLORER: Karte|Liste × Firmen|Projekte ==============
  // Firmen und Projekte sind zwei Panels geblieben (#tab-customers,
  // #tab-objekte) — sie tragen viel Verdrahtung, die ein Umbau nur riskiert
  // hätte. Neu ist, dass sie EIN Tab sind: welches Panel gilt, entscheidet
  // hier der Zustand, nicht mehr die Seitenleiste.
  const EXPLORE = { ansicht: "karte", bereich: "firmen" };
  try {
    const g = JSON.parse(localStorage.getItem("adwatch.explore") || "{}");
    if (g.ansicht === "liste" || g.ansicht === "karte") EXPLORE.ansicht = g.ansicht;
    if (g.bereich === "firmen" || g.bereich === "projekte") EXPLORE.bereich = g.bereich;
  } catch { /* private mode */ }

  async function applyExplore() {
    const karte = EXPLORE.ansicht === "karte";
    const firmen = EXPLORE.bereich === "firmen";
    $$("#exploreAnsicht button").forEach(b =>
      b.classList.toggle("active", b.dataset.ansicht === EXPLORE.ansicht));
    $$("#exploreBereich button").forEach(b =>
      b.classList.toggle("active", b.dataset.bereich === EXPLORE.bereich));
    document.body.classList.toggle("explore-karte", karte);
    $("#tab-customers").classList.toggle("active", firmen);
    $("#tab-objekte").classList.toggle("active", !firmen);
    $("#custMapWrap").classList.toggle("hidden", !(karte && firmen));
    $("#objMapWrap").classList.toggle("hidden", !(karte && !firmen));
    $("#exploreCount").textContent = "";
    try { localStorage.setItem("adwatch.explore", JSON.stringify(EXPLORE)); }
    catch { /* private mode */ }
    // Erst den Bestand, dann die Karte — und zwar mit await: die Karte darf
    // nicht mit einem Filter losziehen, den die Tabelle gleich noch ändert.
    if (firmen) {
      await ensureCustomersLoaded();
      spaltenFilterSpaeter("#custColFilters", "#customersTable");
      if (karte) await showCustMap();
    } else {
      await ensureObjekteLoaded();
      if (karte) await showObjMap();
    }
  }

  // ================= HAUT (dunkel | hell) =================================
  // Gesetzt wird sie am <html>, damit das Stylesheet allein die Arbeit macht.
  // Zwei Dinge muessen JS trotzdem nachziehen: die Pin-Farben (sie stehen in
  // Leaflet-Objekten, nicht im DOM, und aendern sich nicht von selbst) und die
  // Browserleisten-Farbe.
  function hautSetzen(haut, neuZeichnen = true) {
    document.documentElement.dataset.theme = haut;
    try { localStorage.setItem("adwatch.haut", haut); } catch { /* privater Modus */ }
    $$("#themeSwitch button").forEach(b => b.classList.toggle("active", b.dataset.haut === haut));
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = haut === "dunkel" ? "#05060f" : "#4f5ce5";
    MAP_TYPE_COLOR = mapFarben();
    OBJ_TYPE_COLOR = objFarben();
    if (!neuZeichnen) return;
    // Die Pins tragen ihre Farbe als Leaflet-Option — ein Hautwechsel erreicht
    // sie nur, wenn sie neu gezeichnet werden. Der Schluessel wird geleert,
    // sonst greift die Deduplizierung und es passiert gar nichts.
    if (custMap && !$("#custMapWrap")?.classList.contains("hidden")) {
      custPinsKey = null; loadCustMapPins().catch(() => {});
    }
    if (objMapSichtbar()) { objPinsKey = null; loadObjMapPins().catch(() => {}); }
  }
  $$("#themeSwitch button").forEach(b =>
    b.addEventListener("click", () => hautSetzen(b.dataset.haut)));
  // Startzustand: das Markup hat die Haut schon gesetzt (kein Aufblitzen),
  // hier wird nur der Schalter darauf ausgerichtet.
  hautSetzen(document.documentElement.dataset.theme === "hell" ? "hell" : "dunkel", false);

  $$("#exploreAnsicht button").forEach(b => b.addEventListener("click", () => {
    EXPLORE.ansicht = b.dataset.ansicht;
    applyExplore();
  }));
  $$("#exploreBereich button").forEach(b => b.addEventListener("click", () => {
    EXPLORE.bereich = b.dataset.bereich;
    applyExplore();
  }));

  // ================= CHATBOT ==============================================
  // Ein Gespraech: der Verlauf geht mit, damit "und in Oesterreich?" versteht,
  // wovon die Rede war. Mitgeschickt wird nur FRAGE + ANTWORT-TEXT frueherer
  // Wechsel, nicht deren Werkzeugaufrufe — die Antwort traegt das Ergebnis
  // bereits in Worten, ein volles Replay wuerde nur Token kosten.
  const CHAT = { verlauf: [], laeuft: false };

  // Winziger Markdown-Uebersetzer. Kein Fremdpaket: das Modell benutzt genau
  // fuenf Dinge — Tabellen, Aufzaehlungen, fett, Code, Ueberschriften. Alles
  // wird ZUERST escaped, danach werden die Muster gesetzt; so kann keine
  // Modellausgabe HTML in die Seite schreiben.
  function mdToHtml(src) {
    const zeilen = esc(src || "").split("\n");
    const out = [];
    let liste = null, absatz = [];
    const absatzSchliessen = () => {
      if (absatz.length) { out.push(`<p>${absatz.join("<br>")}</p>`); absatz = []; }
    };
    const listeSchliessen = () => { if (liste) { out.push(`</${liste}>`); liste = null; } };

    for (let i = 0; i < zeilen.length; i++) {
      const z = zeilen[i];
      const t = z.trim();

      // Tabelle: Kopfzeile + Trennzeile aus |---|
      if (t.startsWith("|") && (zeilen[i + 1] || "").trim().match(/^\|[\s:|-]+\|$/)) {
        absatzSchliessen(); listeSchliessen();
        const zellen = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
        const kopf = zellen(t);
        i += 2;
        const koerper = [];
        while (i < zeilen.length && zeilen[i].trim().startsWith("|")) {
          koerper.push(zellen(zeilen[i])); i++;
        }
        i--;
        out.push(`<div class="tbl-scroll"><table><thead><tr>${
          kopf.map(c => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${
          koerper.map(r => `<tr>${r.map(c => `<td>${inline(c)}</td>`).join("")}</tr>`)
            .join("")}</tbody></table></div>`);
        continue;
      }
      if (!t) { absatzSchliessen(); listeSchliessen(); continue; }
      if (/^#{1,6}\s/.test(t)) {
        absatzSchliessen(); listeSchliessen();
        out.push(`<h3>${inline(t.replace(/^#{1,6}\s*/, ""))}</h3>`);
        continue;
      }
      const auf = t.match(/^[-*]\s+(.*)$/), num = t.match(/^\d+[.)]\s+(.*)$/);
      if (auf || num) {
        absatzSchliessen();
        const art = auf ? "ul" : "ol";
        if (liste !== art) { listeSchliessen(); out.push(`<${art}>`); liste = art; }
        out.push(`<li>${inline((auf || num)[1])}</li>`);
        continue;
      }
      listeSchliessen();
      absatz.push(inline(t));
    }
    absatzSchliessen(); listeSchliessen();
    return out.join("");

    function inline(s) {
      return s.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
              .replace(/`([^`]+)`/g, "<code>$1</code>");
    }
  }

  const chatThread = () => $("#chatThread");

  function chatLeer() {
    chatThread().innerHTML = `<div class="chat-leer">Frag mich etwas über die Daten.</div>`;
  }

  function chatAnhaengen(html, klasse) {
    const leer = $(".chat-leer");
    if (leer) leer.remove();
    const el = document.createElement("div");
    el.className = `chat-turn ${klasse}`;
    el.innerHTML = html;
    chatThread().appendChild(el);
    chatThread().scrollTop = chatThread().scrollHeight;
    return el;
  }

  // Der Chatbot schlaegt Laeufe VOR, gestartet werden sie hier per Knopf.
  //
  // Warum nicht das Modell starten lassen: ein Lauf kostet echtes Geld und
  // laeuft stundenlang; "mach das mal fuer alle" waeren 46.810 Firmen. Das
  // Modell macht, was es gut kann — aus einem Satz einen Filter bauen —, Python
  // zaehlt und schaetzt, und der Mensch drueckt. Verschickt wird der FILTER,
  // nicht eine Liste von IDs: der Server loest ihn im Moment des Klicks auf,
  // die Menge ist damit garantiert aktuell.
  function laufKarte(v) {
    if (!v || !v.vorschlag) return "";
    const summe = (v.kosten || []).reduce((a, k) => a + (k.usd || 0), 0);
    const offen = (v.kosten || []).some(k => k.usd == null);
    const schritte = (v.schritte || []).map(s => `<span class="chip">${esc(s)}</span>`).join(" ");
    return `<div class="lauf-karte" data-lauf='${esc(JSON.stringify(
        { filter: v.filter, plan: v.plan, label: v.label, deckel: v.deckel || 2000 }))}'>
      <div class="lk-kopf">Lauf vorgeschlagen — noch nicht gestartet</div>
      <div class="lk-schritte">${schritte}</div>
      <dl class="lk-zahlen">
        <dt>Firmen im Lauf</dt><dd>${deN(v.im_lauf)}${
          v.gekappt ? ` <span class="muted">(von ${deN(v.treffer_gesamt)} Treffern — auf ${deN(v.deckel)} begrenzt)</span>` : ""}</dd>
        <dt>Geschätzte Kosten</dt><dd>${summe > 0 ? "$" + summe.toFixed(2) : "—"}${
          offen ? ' <span class="muted">+ Apify je Abruf</span>' : ""}</dd>
      </dl>
      <div class="lk-aktionen">
        <button class="btn btn-primary btn-sm lk-start">Lauf starten</button>
        <span class="lk-status muted"></span>
      </div>
    </div>`;
  }

  function wireLaufKarte(wurzel, v) {
    if (!v || !v.vorschlag) return;
    const karte = $(".lauf-karte", wurzel);
    const knopf = $(".lk-start", karte || wurzel);
    if (!karte || !knopf) return;
    knopf.addEventListener("click", async () => {
      knopf.disabled = true;
      const status = $(".lk-status", karte);
      status.textContent = "Wird gestartet …";
      try {
        const job = await api("/api/pipeline-jobs/aus-filter", "POST",
                              JSON.parse(karte.dataset.lauf));
        status.textContent = `Läuft — ${job.total || ""} Schritte. Fortschritt in „Logs".`;
        karte.classList.add("gestartet");
        toast("Lauf gestartet.", "info");
      } catch (e) {
        knopf.disabled = false;
        status.textContent = "";
        toast(e.message, "error");
      }
    });
  }

  async function chatSenden(text) {
    text = (text || "").trim();
    if (!text || CHAT.laeuft) return;
    CHAT.laeuft = true;
    $("#frageSenden").disabled = true;
    $("#frageInput").value = "";
    $("#frageInput").style.height = "auto";

    chatAnhaengen(`<div class="bubble">${esc(text)}</div>`, "user");
    const bot = chatAnhaengen(
      `<div class="chat-denkt"><i></i><i></i><i></i></div>`, "bot");

    try {
      const d = await api("/api/fragen", "POST",
                          { frage: text, verlauf: CHAT.verlauf });
      const meta = (d.verlauf || []).map(v =>
        `<span class="wz${v.fehler ? " err" : ""}" title="${esc(JSON.stringify(v.params || {}))}${
          v.fehler ? " — FEHLER: " + esc(v.fehler) : ""}">${esc(v.werkzeug)}</span>`).join("");
      bot.innerHTML = mdToHtml(d.antwort) + laufKarte(d.vorschlag) +
        `<div class="chat-meta">${meta}<span>${esc(d.model)}</span>` +
        `<span>≈ $${d.kosten_usd}</span><span>${d.dauer_s}s</span></div>`;
      wireLaufKarte(bot, d.vorschlag);
      CHAT.verlauf.push({ frage: text, antwort: d.antwort });
      $("#chatModell").textContent = d.model;
    } catch (e) {
      bot.innerHTML = `<p class="chat-fehler">${esc(e.message)}</p>`;
    } finally {
      CHAT.laeuft = false;
      $("#frageSenden").disabled = false;
      chatThread().scrollTop = chatThread().scrollHeight;
      $("#frageInput").focus();
    }
  }

  const frageFeld = $("#frageInput");
  if (frageFeld) {
    chatLeer();
    // Enter sendet, Shift+Enter macht eine Zeile — wie man es von Chats kennt.
    frageFeld.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); chatSenden(frageFeld.value); }
    });
    // Das Feld waechst mit dem Text, bis die CSS-Hoehe deckelt.
    frageFeld.addEventListener("input", () => {
      frageFeld.style.height = "auto";
      frageFeld.style.height = Math.min(frageFeld.scrollHeight, 180) + "px";
    });
    $("#frageSenden").addEventListener("click", () => chatSenden(frageFeld.value));
    $("#chatNeu").addEventListener("click", () => {
      CHAT.verlauf = []; chatLeer(); frageFeld.focus();
    });
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
    fetchBtn.textContent = STATE.fetch_running ? "Ruft ab…" : "Anzeigen abrufen";

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
    // data-label statt textContent: der Entscheidungen-Tab trägt eine Badge,
    // deren Zahl sonst im Tooltip landen würde ("Entscheidungen 536")
    $$(".tab").forEach(t => { t.title = (t.dataset.label || t.textContent).trim(); });
    $("#navToggle").addEventListener("click", () =>
      applyNavCollapsed(!document.body.classList.contains("nav-collapsed")));
    try { if (localStorage.getItem("navCollapsed") === "1") applyNavCollapsed(true); } catch { }

    function showTab(name) {
      const btn = $$(".tab").find(t => t.dataset.tab === name);
      if (!btn) return;
      $$(".tab").forEach(t => t.classList.toggle("active", t === btn));
      // Der Explorer hat kein eigenes Panel, sondern zwei — welches gilt,
      // sagt EXPLORE.bereich. Deshalb deaktiviert diese Zeile für "explore"
      // erst alles, und applyExplore() schaltet das richtige wieder an.
      $$(".tab-panel").forEach(p => p.classList.toggle("active", p.id === `tab-${name}`));
      $("#exploreBar").classList.toggle("hidden", name !== "explore");
      if (name !== "explore") document.body.classList.remove("explore-karte");
      if (name === "explore") { applyExplore(); return; }
      if (name === "dashboard") loadHeute();   // Karten beim Rückwechsel auffrischen
      if (name === "pipeline") ensurePipelineLoaded();
      if (name === "listen") ensureListenLoaded();
      if (name === "chancen") ensureChancenLoaded();
      if (name === "pruefen") ensurePruefenLoaded();
      if (name === "konversion") ensureKonversionLoaded();
      if (name === "profil") { ensureProfilesLoaded(); loadIcpStatus(); }
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
    // the wire* calls above, since showTab("explore") loads the table, which
    // reads the filter dropdowns that wireCustomers() mounts.
    //
    // Ein gespeichertes "customers"/"objekte" stammt aus der Zeit vor dem
    // Explorer. Übersetzt statt ignoriert: sonst landet jeder, der zuletzt in
    // Firmen war, nach dem Update kommentarlos auf „Heute".
    let savedTab = localStorage.getItem("adwatch.activeTab");
    if (EXPLORE_ALIAS[savedTab]) {
      EXPLORE.bereich = EXPLORE_ALIAS[savedTab];
      savedTab = "explore";
      localStorage.setItem("adwatch.activeTab", savedTab);
    }
    if (savedTab && savedTab !== "dashboard") showTab(savedTab);

    // Deep-Link beim Start: eine geteilte #/firma/123-URL öffnet das Dossier
    // direkt — der Empfänger braucht keinen Klickpfad durch Tabs und Filter.
    const dl = location.hash.match(/^#\/firma\/(\d+)$/);
    if (dl) openCompanyDrawer(Number(dl[1]));

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

  // ---- Microsoft-Anbindung: Personensuche + Teams ------------------------
  // Teams laesst sich NICHT einbetten (Microsoft verbietet frame-ancestors, ein
  // iframe bliebe leer). Ein Deep Link tut, was gemeint ist, und braucht keine
  // einzige Berechtigung.
  function teamsLink(email) {
    const e = (email || "").trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e)) return "";
    return `<a class="btn btn-sm" target="_blank" rel="noopener"
      href="https://teams.microsoft.com/l/chat/0/0?users=${encodeURIComponent(e)}"
      title="Chat in Microsoft Teams öffnen">💬 Teams</a>`;
  }

  // Empfaenger sollen GEWAEHLT werden, nicht getippt: ein Tippfehler schickt
  // den Bericht sonst an eine fremde Person, und niemand merkt es. Ist der
  // Personen-Flow nicht eingerichtet, bleibt das Feld ein normales Eingabefeld
  // — die Empfaengerpflege darf nie an einer Zusatzfunktion haengen.
  function wirePeoplePicker() {
    const feld = $("#newRecipientEmail"), box = $("#peoplePicker");
    if (!feld || !box || feld.dataset.wired) return;
    feld.dataset.wired = "1";
    let timer = null, letzte = "";

    const zu = () => box.classList.add("hidden");
    const waehlen = (p) => {
      feld.value = p.email;
      const nameFeld = $("#newRecipientName");
      if (nameFeld && !nameFeld.value.trim()) nameFeld.value = p.name || "";
      zu();
    };

    async function suchen() {
      const q = feld.value.trim();
      if (q === letzte) return;
      letzte = q;
      if (q.length < 2) return zu();
      let d;
      try { d = await api(`/api/people/search?q=${encodeURIComponent(q)}`); }
      catch { return zu(); }
      if (!d.verfuegbar) {
        const hint = $("#peopleHint");
        if (hint) {
          hint.textContent = "Tipp: Mit dem Flow „Personen suchen“ lassen "
            + "sich Empfänger aus dem Verzeichnis wählen statt abtippen — "
            + "einzurichten unter Einstellungen.";
          hint.classList.remove("hidden");
        }
        return zu();
      }
      if (!d.rows.length) return zu();
      box.innerHTML = d.rows.map((p, i) => `
        <button type="button" class="people-row" data-i="${i}">
          <b>${esc(p.name)}</b>
          <span class="muted">${esc(p.email)}</span>
          ${p.abteilung || p.titel ? `<span class="sub">${
            esc([p.titel, p.abteilung].filter(Boolean).join(" · "))}</span>` : ""}
        </button>`).join("");
      $$(".people-row", box).forEach(b =>
        b.addEventListener("click", () => waehlen(d.rows[Number(b.dataset.i)])));
      box.classList.remove("hidden");
    }

    feld.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(suchen, 220);   // nicht bei jedem Tastendruck fragen
    });
    feld.addEventListener("keydown", (e) => { if (e.key === "Escape") zu(); });
    document.addEventListener("click", (e) => {
      if (!box.contains(e.target) && e.target !== feld) zu();
    });
  }

  function renderRecipients() {
    wirePeoplePicker();
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
        <span style="display:flex;gap:6px;align-items:center">
          ${teamsLink(r.email)}
          <button class="btn btn-sm del-recipient-btn" data-rid="${r.id}">Entfernen</button>
        </span>
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
    // Das Panel wohnt im Firmen-Bereich des Explorers — dorthin schalten
    // (relevant beim Bearbeiten aus dem Berichte-Tab). Und zwar in die LISTE:
    // in der Kartenansicht ist der Bereich ausgeblendet, das Panel wäre
    // aufgeklappt und unsichtbar.
    if (!$("#tab-customers").classList.contains("active")
        || EXPLORE.ansicht !== "liste") gotoTab("customers");
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
    verloren: "verloren", einmalig: "nur Kleinteile", nie: "nie angefragt",
  };
  let chancenLoaded = false;

  async function loadChancen() {
    const wrap = $("#chancenTableWrap");
    const adsOnly = $("#chancenAdsOnly").checked;
    const minValue = Number($("#chancenMinValue").value || 0);
    wrap.innerHTML = `<p class="muted" style="padding:12px">Wird geladen …</p>`;
    const chSeq = nextSeq("chancenTableWrap");
    let data;
    try {
      data = await api(`/api/chancen?limit=500&min_value=${minValue}`
        + `&advertising_only=${adsOnly ? "true" : "false"}`
        + serverParamQuery("chancenTableWrap"));
    } catch (e) {
      wrap.innerHTML = `<p class="muted" style="padding:12px">Konnte nicht geladen werden: ${esc(e.message)}</p>`;
      return;
    }
    if (!isCurrent("chancenTableWrap", chSeq)) return;
    TABLE_TOTALS["chancenTableWrap"] = data.total;
    const rows = data.rows || [];
    const s = data.summary || {};
    const risk = (s["gefährdet"]?.companies || 0) + (s.verloren?.companies || 0);
    const riskEur = (s["gefährdet"]?.value || 0) + (s.verloren?.value || 0);
    $("#chancenSummary").innerHTML = `
      <div class="kpi"><div class="kpi-label">Überfällig / verloren</div>
        <div class="kpi-value">${risk.toLocaleString("de-DE")}</div></div>
      <div class="kpi"><div class="kpi-label">Angebotsvolumen historisch</div>
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
          <th class="num">Angebotsvolumen</th><th class="num">Angebote</th>
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
        <b>Belege:</b> ${b.events} Angebote · ${eurShort(b.total)} · ${esc(b.first)} → ${esc(b.last)}
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
        ${vcTable(blk.recent, blk.vcs, role)}
      </div>`;
    }
    const pj = d.projekte || [];
    if (pj.length) {
      // "Objekte (6)" used to be the length of a list that was itself capped —
      // and derived from the ten newest VCs, so a company on 1.250 buildings
      // showed six and implied that was all of them.
      const total = d.projekte_total ?? pj.length;
      html += `<div style="margin-top:10px;font-size:12.5px"><b>Objekte:</b>
        <span class="sub">${pj.length < total ? `${pj.length} von ${total.toLocaleString("de-DE")}` : total.toLocaleString("de-DE")}</span>
        ${pj.slice(0, 6).map(p => `<div class="sub objekt-link" data-projekt="${esc(p.project_id)}"
            style="cursor:pointer">• ${esc((p.name || "").slice(0, 60))} —
          ${esc(p.status)}, ${p.members} VC${p.members > 1 ? "s" : ""}${p.type_of_use ? `, ${esc(p.type_of_use)}` : ""}${p.value ? `, ${eurShort(p.value)}` : ""}</div>`).join("")}
      </div>`;
    }
    return html + `</div>`;
  }

  // Every Verkaufschance of one company, in one role. The dossier ships the ten
  // newest; the rest load on demand from /api/companies/<id>/verkaufschancen,
  // because one firm carries 1.266 of them and sending all of that into a drawer
  // by default would be slow for the 58% of companies that have exactly one.
  function vcTable(rows, total, role) {
    if (!rows || !rows.length) return "";
    const shown = rows.length;
    return `
      <div class="vc-block" data-role="${esc(role)}" style="margin-top:6px">
        <table class="data-table" style="font-size:12px">
          <thead><tr><th>Angelegt</th><th>Nr.</th><th>Objekt</th><th>Ort</th>
            <th>Status</th><th class="num">Wert</th></tr></thead>
          <tbody>${rows.map(vcRow).join("")}</tbody>
        </table>
        ${shown < total ? `<button class="btn btn-sm vc-more" data-role="${esc(role)}"
            data-offset="${shown}" style="margin-top:5px">
            ${shown} von ${total.toLocaleString("de-DE")} — weitere laden</button>`
          : `<div class="sub" style="margin-top:4px">alle ${total.toLocaleString("de-DE")} angezeigt</div>`}
      </div>`;
  }

  // Clicking a Verkaufschance opens the Objekt it belongs to — that is where the
  // sibling bids, the roles and the timeline live, and it is the same drawer the
  // Objekte tab uses. Re-wired after every "weitere laden", since those rows are
  // new nodes.
  function wireVcLinks(root, companyId) {
    $$(".vc-link, .objekt-link", root).forEach(el => {
      if (el.dataset.wired) return;
      el.dataset.wired = "1";
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        openProjektDrawer(el.dataset.projekt);
      });
    });
    $$(".vc-more", root).forEach(btn => {
      if (btn.dataset.wired) return;
      btn.dataset.wired = "1";
      btn.addEventListener("click", async () => {
        const role = btn.dataset.role, offset = Number(btn.dataset.offset || 0);
        btn.disabled = true;
        btn.textContent = "lädt …";
        try {
          const d = await api(`/api/companies/${companyId}/verkaufschancen`
            + `?role=${encodeURIComponent(role)}&limit=100&offset=${offset}`);
          const tbody = $("tbody", btn.closest(".vc-block"));
          tbody.insertAdjacentHTML("beforeend", (d.rows || []).map(vcRow).join(""));
          const shown = tbody.rows.length;
          if (shown >= d.total) {
            btn.replaceWith(Object.assign(document.createElement("div"),
              {className: "sub", textContent: `alle ${d.total.toLocaleString("de-DE")} angezeigt`}));
          } else {
            btn.dataset.offset = String(shown);
            btn.disabled = false;
            btn.textContent = `${shown} von ${d.total.toLocaleString("de-DE")} — weitere laden`;
          }
          wireVcLinks(btn.closest(".vc-block") || root, companyId);
        } catch (e) {
          btn.disabled = false;
          btn.textContent = "Fehler — nochmal";
          toast(e.message, "error");
        }
      });
    });
  }

  function vcRow(v) {
    // The address on a Verkaufschance is the BUILDING, not the customer's seat —
    // worth showing, because it is the only geography that says where demand is.
    const ort = [v.postal_code, v.city].filter(Boolean).join(" ");
    return `<tr${v.project_id ? ` class="vc-link" data-projekt="${esc(v.project_id)}" style="cursor:pointer"` : ""}>
      <td class="sub" style="white-space:nowrap">${v.created ? esc(deDate(v.created)) : "—"}</td>
      <td class="sub">${esc(v.number || "—")}</td>
      <td style="max-width:210px">${esc(v.name || "(ohne Namen)")}
        ${v.lost_reason && v.lost_reason !== "Zugehörige VC gewonnen"
          ? `<div class="sub">${esc(v.lost_reason)}</div>` : ""}
        ${v.roles && v.roles.length > 1
          ? `<div class="sub">Rollen: ${v.roles.map(r => esc(ROLE_LABEL[r] || r)).join(", ")}</div>` : ""}</td>
      <td class="sub">${esc(ort || "—")}</td>
      <td>${stateChip(v.state)}</td>
      <td class="num">${v.order_value ? eur(v.order_value)
        : (v.estimated_value ? `<span class="sub">${eur(v.estimated_value)}</span>` : "—")}</td>
    </tr>`;
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
      ["Belege (Anzahl)", c.beleg_count], ["Belege (Angebotsvolumen)", money(c.beleg_sum)],
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
    // "2", "1", or a closed range like "3-4" — how many Verkaufschancen hang on
    // the Objekt. The measured spread (39,0 % at 2+ against 19,3 % overall) is
    // why this is a filter and not just a column.
    const [lo, hi] = String($("#objekteVcs").value).split("-");
    wrap.innerHTML = `<p class="muted" style="padding:12px">Wird geladen …</p>`;
    const seq = nextSeq("objekteWrap");
    let data;
    try {
      data = await api(`/api/projekte?limit=300&min_members=${lo}`
        + (hi ? `&max_members=${hi}` : "")
        + (st ? `&status=${st}` : "") + serverParamQuery("objekteWrap"));
      if (!isCurrent("objekteWrap", seq)) return;    // a newer load already ran
      TABLE_TOTALS["objekteWrap"] = data.total;
    } catch (e) {
      wrap.innerHTML = `<p class="muted" style="padding:12px">Fehler: ${esc(e.message)}</p>`;
      return;
    }
    const o = data.overview || {};
    // Every KPI here counts the FILTERED population, so the first one has to say
    // which population that is. It used to read "Objekte mit mehreren VCs" with
    // "+ N mit nur einer VC" underneath — a subtraction against ALL groups that
    // was only true while the filter was the 2+ checkbox. Filter to 5–9 VCs and
    // it claimed "145 mit mehreren VCs + 52.651 mit nur einer".
    $("#objekteKpis").innerHTML = `
      <div class="kpi"><div class="kpi-label">Objekte im Filter</div>
        <div class="kpi-value">${(o.projects || 0).toLocaleString("de-DE")}</div>
        <div class="sub">von ${(o.all_projects || 0).toLocaleString("de-DE")} gesamt</div></div>
      <div class="kpi"><div class="kpi-label">Gewonnen</div>
        <div class="kpi-value">${(o.gewonnen || 0).toLocaleString("de-DE")}</div></div>
      <div class="kpi"><div class="kpi-label">Projekt-Gewinnrate</div>
        <div class="kpi-value">${o.project_win_rate != null ? (o.project_win_rate * 100).toFixed(1) + " %" : "—"}</div></div>
      <div class="kpi"><div class="kpi-label">Gewonnener Wert</div>
        <div class="kpi-value" title="${eur(o.won_value)}">${eurShort(o.won_value)}</div></div>`;
    // Gewinnrate nach Anzahl VCs — über ALLE Objekte, unabhängig vom Filter,
    // damit die Zeile eine Referenz bleibt und nicht das Gefilterte spiegelt.
    const buckets = o.member_buckets || [];
    $("#objekteBuckets").innerHTML = !buckets.length ? "" : buckets.map(b => `
      <button class="btn" data-vcs="${b.max == null ? b.min : b.min + "-" + b.max}"
        title="Objekte mit ${esc(b.label)} — auf diesen Bereich filtern"
        style="flex:1;text-align:left;padding:7px 11px">
        <div class="sub">${esc(b.label)}</div>
        <div style="font-size:15px">${b.win_rate != null ? (b.win_rate * 100).toFixed(1) + " %" : "—"}</div>
        <div class="sub">${(b.projects || 0).toLocaleString("de-DE")} Objekte</div>
      </button>`).join("");
    $$("#objekteBuckets button[data-vcs]").forEach(b =>
      b.addEventListener("click", () => {
        const sel = $("#objekteVcs");
        if ([...sel.options].some(op => op.value === b.dataset.vcs)) {
          sel.value = b.dataset.vcs;
          loadObjekte();
        }
      }));

    // Der Filter gilt für beide Darstellungen. Ohne das hier zeigte die Karte
    // weiter die Pins des vorigen Filters, während die KPIs darüber schon die
    // neue Auswahl zählten — zwei Wahrheiten auf einem Bildschirm.
    if (objMapSichtbar()) loadObjMapPins();
    if (obj3dSichtbar()) zeigeObj3d();

    const rows = data.rows || [];
    if (!rows.length) {
      wrap.innerHTML = `<p class="muted" style="padding:12px">Keine Projekte für diesen Filter.</p>`;
      return;
    }
    wrap.innerHTML = `
      <table class="data-table">
        <thead><tr><th>Objekt</th>
          <th title="Wann die erste Verkaufschance an diesem Objekt im CRM angelegt wurde — das älteste created_on aller zugehörigen VCs. Nicht das Bau-, Angebots- oder Abschlussdatum.">Angelegt</th>
          <th>Status</th><th class="num">VCs</th>
          <th class="num">Wert</th><th data-nosort>Firmen</th><th data-nosort>Architekten</th><th data-nosort>Verlustgründe</th></tr></thead>
        <tbody>${rows.map(p => `
          <tr data-projekt="${esc(p.project_id)}" style="cursor:pointer">
            <td style="max-width:280px">${esc(p.name)}</td>
            <td style="white-space:nowrap">${p.created ? esc(deDate(p.created)) : "—"}</td>
            <td>${stateChip(p.status)}</td>
            <td class="num">${p.members}${p.won_members ? ` <span class="sub">(${p.won_members} gew.)</span>` : ""}</td>
            <td class="num">${eur(p.order_value ?? p.estimated_value)}</td>
            <td style="max-width:220px">${esc((p.firms || []).join(", "))}</td>
            <td style="max-width:180px">${esc((p.architects || []).join(", "))}</td>
            <td style="max-width:200px" class="sub">${esc((p.lost_reasons || []).join(", "))}</td>
          </tr>`).join("")}</tbody>
      </table>`;
    $$("#objekteWrap tr[data-projekt]").forEach(tr =>
      tr.addEventListener("click", () => openProjektDrawer(tr.dataset.projekt)));
    // Die Tabelle wird bei jedem Laden neu gebaut — die Fernbedienung zeigt
    // sonst auf Spaltenköpfe, die es nicht mehr gibt.
    spaltenFilterSpaeter("#objColFilters", "#objekteWrap table");
  }

  // ---- Objekt drawer: everything ever linked to one project ---------------
  // An Objekt has no record of its own in the CRM — it is a GROUP of
  // Verkaufschancen sharing sl_primary_opportunityid — so this is assembled
  // from its members. That assembly IS the point: one win among five losses is
  // a won project, and only here can you see why the other five were lost.
  // The drawer is an empty <aside> in the markup; the chrome — head, close
  // button, and the .drawer-body that actually carries `overflow-y:auto` — is
  // built by whoever opens it. This function used to write into
  // `$(".drawer-body") || drawer`, and opened from the Objekte tab nothing had
  // built a .drawer-body yet, so the fallback dropped the sections straight
  // into .company-drawer. That is a flex column with no scrolling and no head:
  // long Objekte overflowed with no way to scroll and no ✕ to close. It only
  // looked right when a company drawer had been opened first, which is exactly
  // how it got past review.
  function drawerShell(drawer, title, subtitle) {
    $("#drawerBackdrop").classList.remove("hidden");
    drawer.classList.remove("hidden");
    drawer.innerHTML = `
      <div class="drawer-head">
        <div>
          <h2>${esc(title)}</h2>
          ${subtitle ? `<span class="muted">${esc(subtitle)}</span>` : ""}
        </div>
        <div class="drawer-head-actions">
          <button class="btn btn-ghost drawer-close" title="Schließen">✕</button>
        </div>
      </div>
      <div class="drawer-body"></div>`;
    $(".drawer-close", drawer).addEventListener("click", closeCompanyDrawer);
    return $(".drawer-body", drawer);
  }

  async function openProjektDrawer(pid) {
    const drawer = $("#companyDrawer");
    let body = drawerShell(drawer, "Objekt", "wird geladen …");
    body.innerHTML = `<p class="muted" style="padding:14px">Objekt wird geladen …</p>`;
    let d;
    try { d = await api(`/api/projekte/${encodeURIComponent(pid)}`); }
    catch (e) { body.innerHTML = `<p class="muted" style="padding:14px">Fehler: ${esc(e.message)}</p>`; return; }
    // now that the name is known, rebuild the head with it — the Objekt's name
    // is the building address, which is what a reader needs pinned at the top
    // while scrolling through its Verkaufschancen
    body = drawerShell(drawer, d.name || "(ohne Namen)",
                       [d.address, d.channel].filter(Boolean).join(" · "));

    const roleName = {kaeufer: "Käufer", architekt: "Architekt", endkunde: "Endkunde"};
    const kv = (k, v) => v == null || v === "" ? "" : `
      <div style="display:flex;gap:8px;padding:3px 0;border-bottom:1px solid var(--border)">
        <span class="muted" style="flex:0 0 42%">${k}</span><span style="flex:1">${v}</span></div>`;

    body.innerHTML = `
      <div class="drawer-section">
        <h3>Objekt</h3>
        <div style="font-size:12.5px">
          ${kv("Status", stateChip(d.status) +
              (d.won_via ? ` <span class="sub">— ${esc(d.won_via)}</span>` : ""))}
          ${kv("Adresse", d.address ? esc(d.address) : null)}
          ${kv("Nutzung", d.type_of_use ? esc(d.type_of_use) : null)}
          ${kv("Herkunft / Vertriebsweg", [d.origin, d.channel].filter(Boolean).map(esc).join(" · ") || null)}
          ${kv("Verkaufschancen", `${d.members}${d.won_members ? ` · ${d.won_members} gewonnen` : ""}`)}
          ${kv("Auftragswert", d.won_value ? eur(d.won_value) : (d.order_value ? eur(d.order_value) : null))}
          ${kv("Zeitraum", d.first ? `${esc(deDate(d.first))} → ${d.last ? esc(deDate(d.last)) : ""}` : null)}
          ${kv("SAP-Aufträge", (d.sap_orders || []).length ? esc(d.sap_orders.join(", ")) : null)}
          ${kv("Verlustgründe", (d.lost_reasons || []).length ? esc(d.lost_reasons.join(" · ")) : null)}
        </div>
      </div>

      ${(d.produkte || []).length ? `<div class="drawer-section">
        <h3>Produkte im Objekt</h3>
        <div style="font-size:12.5px;display:flex;flex-direction:column;gap:3px">
          ${d.produkte.map(p => `<div style="display:grid;grid-template-columns:1fr 62px 78px;gap:8px">
            <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.family)}</span>
            <span class="sub" style="text-align:right">${p.positions ? p.positions + " Pos." : "—"}</span>
            <span class="sub" style="text-align:right">${p.value ? eurShort(p.value) : "—"}</span>
          </div>`).join("")}
        </div>
        <div class="sub" style="margin-top:4px">Werte sind <b>angefragt</b>, nicht fakturiert.</div>
      </div>` : ""}

      <div class="drawer-section">
        <h3>Beteiligte Firmen (${(d.firms || []).length})</h3>
        <table class="data-table" style="font-size:12.5px">
          <thead><tr><th>Firma</th><th>Rolle</th><th class="num">VCs</th><th class="num">Wert</th></tr></thead>
          <tbody>${(d.firms || []).map(f => `
            <tr${f.company_id ? ` data-firma="${f.company_id}" style="cursor:pointer"` : ""}>
              <td>${esc(f.name)}${f.city ? `<div class="sub">${esc(f.city)}${f.segment ? " · " + esc(f.segment) : ""}</div>` : ""}</td>
              <td class="sub">${esc((f.roles || []).map(r => roleName[r] || r).join(", "))}</td>
              <td class="num">${f.vcs}${f.won ? ` <span class="sub">(${f.won} gew.)</span>` : ""}</td>
              <td class="num">${f.value ? eurShort(f.value) : "—"}</td>
            </tr>`).join("")}</tbody>
        </table>
      </div>

      <div class="drawer-section" id="objMailSection">
        <div class="drawer-section-head">
          <h3>Schriftverkehr zum Objekt</h3>
          <button id="objMailAnalyse" class="btn btn-sm"
            title="Liest den Verkehr dieses Objekts EINMAL mit einem Sprachmodell und speichert das Ergebnis (Bruchteil eines Cents)">✨ Auswerten</button>
        </div>
        <div id="objMailFindings"></div>
        <div id="objMailBody"><p class="hint">Lädt…</p></div>
      </div>
      <div class="drawer-section">
        <h3>Verlauf (${(d.timeline || []).length} Verkaufschancen)</h3>
        <table class="data-table" style="font-size:12.5px">
          <thead><tr><th title="Wann diese Verkaufschance im CRM angelegt wurde">Angelegt</th>
            <th title="Wann sie geschlossen wurde — gewonnen oder verloren. Leer heißt: noch offen.">Geschlossen</th>
            <th>Nr.</th><th>Firma</th><th>Status</th><th class="num">Wert</th></tr></thead>
          <tbody>${(d.timeline || []).map(t => `
            <tr>
              <td class="sub" style="white-space:nowrap">${t.date ? esc(deDate(t.date)) : "—"}</td>
              <td class="sub" style="white-space:nowrap">${t.closed ? esc(deDate(t.closed)) : "—"}</td>
              <td class="sub">${esc(t.number || "—")}</td>
              <td>${esc(t.firm || "—")}</td>
              <td>${stateChip(t.state)}${t.lost_reason ? `<div class="sub">${esc(t.lost_reason)}</div>` : ""}</td>
              <td class="num">${t.value ? eur(t.value) : "—"}</td>
            </tr>`).join("")}</tbody>
        </table>
      </div>`;

    // a firm in the Objekt jumps straight to its own drawer
    $$("tr[data-firma]", body).forEach(tr =>
      tr.addEventListener("click", () => openCompanyDrawer(Number(tr.dataset.firma))));
    loadObjektMails(pid);
  }

  // Schriftverkehr eines Objekts: ANZEIGEN kostet nichts und passiert immer.
  // AUSWERTEN passiert nur auf Klick — 450.000 Mails durch ein Sprachmodell zu
  // schicken waere dreistellig, und die Frage stellt sich nur bei Objekten, die
  // jemand tatsaechlich ansieht. Das Ergebnis wird gespeichert.
  async function loadObjektMails(pid) {
    const box = $("#objMailBody");
    if (!box) return;
    let d;
    try { d = await api(`/api/projekte/${encodeURIComponent(pid)}/emails?limit=60`); }
    catch (e) { box.innerHTML = `<p class="hint status-error">${esc(e.message)}</p>`; return; }
    const btn = $("#objMailAnalyse");
    if (!d.emails.length) {
      box.innerHTML = `<p class="hint">Kein Schriftverkehr zu diesem Objekt.
        <span class="muted">Der Abruf umfasst bisher 2023+, und rund 19 % der in
        Mails genannten Verkaufschancen liegen ausserhalb unseres Spiegels.</span></p>`;
      if (btn) btn.disabled = true;
      return;
    }
    box.innerHTML = `
      <p class="hint"><b>${deN(d.mails)}</b> Mails · <b>${deN(d.eingehend)}</b> eingehend
        · ueber ${d.guids} Verkaufschance(n)${d.mails > d.emails.length
          ? ` · gezeigt: die ${d.emails.length} neuesten` : ""}</p>
      ${d.emails.map(m => `
        <div class="mail-item ${m.richtung === "eingehend" ? "mail-in" : ""}">
          <div class="mail-head">
            <span class="qual-badge ${m.richtung === "eingehend" ? "qual-stark" : "qual-mittel"}">
              ${m.richtung === "eingehend" ? "eingehend" : "ausgehend"}</span>
            <b>${esc(m.betreff || "(ohne Betreff)")}</b>
            <span class="spacer"></span>
            <span class="muted">${m.datum ? esc(deDate(m.datum)) : ""}</span>
          </div>
          <div class="mail-text">${esc(m.anriss || "")}${m.zeichen > 600 ? " …" : ""}</div>
        </div>`).join("")}`;
    if (btn && !btn.dataset.wired) {
      btn.addEventListener("click", () => analyseObjektMails(pid));
      btn.dataset.wired = "1";
    }
  }

  async function analyseObjektMails(pid) {
    const btn = $("#objMailAnalyse"), out = $("#objMailFindings");
    btn.disabled = true; btn.textContent = "Wertet aus…";
    try {
      const r = await api(`/api/projekte/${encodeURIComponent(pid)}/emails/auswerten`, "POST", {});
      const f = r.findings || {};
      const list = (a) => (a && a.length) ? a.map(esc).join(" · ") : "—";
      out.innerHTML = `
        <div class="icp-plain">
          <b>Aus dem Schriftverkehr gelesen</b>
          <span class="muted">— ${r.mails_used} von ${r.mails_total ?? r.mails_used} Mails,
          ${r.cached ? "gespeichertes Ergebnis" : "neu ausgewertet"}, ${esc(r.model || "")}</span>
          <dl class="drawer-grid" style="margin-top:8px">
            ${drawerKv("Kernursache", f.kernursache ? `<b>${esc(f.kernursache)}</b>` : "—")}
            ${drawerKv("Beleg", f.belege && f.belege.kernursache ? `<i>„${esc(f.belege.kernursache)}"</i>` : "—")}
            ${drawerKv("Wettbewerber", list(f.wettbewerber))}
            ${drawerKv("Einwände", list(f.einwaende))}
            ${drawerKv("Wer verstummte", esc(f.wer_verstummte || "—"))}
            ${drawerKv("Produkte", list(f.produkte))}
            ${drawerKv("Offen geblieben", esc(f.naechster_schritt_offen || "—"))}
            ${drawerKv("Stimmung", esc(f.stimmung || "—"))}
          </dl>
          <p class="hint" style="margin:6px 0 0">Abgeleitet, nicht belegt — das
            Modell darf nur wiedergeben, was im Text steht, und antwortet sonst
            mit „—". Vor einer Ansprache trotzdem selbst lesen.</p>
        </div>`;
      btn.textContent = "✨ Neu auswerten";
    } catch (e) {
      out.innerHTML = `<p class="hint status-error">${esc(e.message)}</p>`;
      btn.textContent = "✨ Auswerten";
    }
    btn.disabled = false;
  }

  // Gibt wie ensureCustomersLoaded() ein Versprechen zurück — die Projektkarte
  // wartet darauf, damit nicht Liste und Karte getrennt losziehen.
  let objekteBereit = null;
  function ensureObjekteLoaded() {
    if (objekteLoaded) return objekteBereit || Promise.resolve();
    objekteLoaded = true;
    objekteBereit = loadObjekte();
    return objekteBereit;
  }

  function wireObjekte() {
    const btn = $("#objekteReload");
    if (!btn) return;
    btn.addEventListener("click", loadObjekte);
    $("#objekteStatus").addEventListener("change", loadObjekte);
    $("#objekteVcs").addEventListener("change", loadObjekte);
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
          <th data-nosort>Hinweis</th><th data-nomenu style="width:170px">Entscheidung</th></tr></thead>
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
    pruefenFocus(pruefenRows()[0]);   // Tastatur ist sofort einsatzbereit
    paintPruefenSession();
    // Both counters that name a number of rows: the queue line (#pruefenCount)
    // and the shared "N Zeilen" tag that _applyTableState draws above every
    // table. Updating only the first left "36 Zeilen" frozen while the list
    // shrank underneath it.
    const repaintPruefen = (tbody) => {
      const left = Math.max(TABLE_TOTALS["pruefenWrap"] || 0, 0);
      const cnt = $("#pruefenCount");
      if (cnt) cnt.textContent = `${tbody.rows.length} von ${left} offen`;
      setPruefenBadge(left);   // die Zahl an der Navigation zählt live mit
      const table = tbody.closest("table");
      if (table) _applyTableState(table, "pruefenWrap");
    };

    // The animation, not a timeout: fade+slide the row out, then collapse the
    // space it held. A <tr> will not transition from height:auto, so the cells
    // are pinned to their measured pixel height first, and the reflow between
    // pinning and clearing is what gives the transition a start value.
    const ROW_OUT_MS = 260;
    const fadeRowOut = (tr) => {
      const h = tr.offsetHeight;
      [...tr.cells].forEach(td => {
        td.style.height = `${h}px`;
        td.style.overflow = "hidden";
      });
      void tr.offsetHeight;                       // force reflow
      tr.classList.add("row-out");
      [...tr.cells].forEach(td => { td.style.height = "0px"; });
      return new Promise(done => setTimeout(done, ROW_OUT_MS));
    };
    const undoFade = (tr) => {
      tr.classList.remove("row-out");
      [...tr.cells].forEach(td => {
        td.style.removeProperty("height");
        td.style.removeProperty("overflow");
      });
    };

    // The decision lands the moment you click; the row animates out while the
    // request is still in flight. It used to WAIT for the request, and `accept`
    // ends in enrich_company() — a full website crawl plus an LLM call,
    // synchronously, before the response returns. So a decision took ten seconds
    // or more to visibly land, for work the human is not waiting on: the verdict
    // is written before the enrichment starts.
    //
    // Requests are SERIALISED behind one promise chain. Removing on click makes
    // it easy to click ten rows in two seconds, and ten parallel crawl+LLM calls
    // are how "database is locked" happens.
    let chain = Promise.resolve();
    const decide = (btn, call, msg) => {
      const tr = btn.closest("tr");
      const cid = Number(tr.dataset.company);
      const tbody = tr.parentElement;
      const anchor = tr.nextElementSibling;
      // Maus-Klick auf eine fokussierte Zeile: Fokus wandert weiter, wie beim
      // Tastatur-Weg — eine hinausanimierende Zeile trägt keinen Fokus
      if (tr.classList.contains("row-focus")) {
        const nx = tr.nextElementSibling || tr.previousElementSibling;
        if (nx) pruefenFocus(nx);
      }
      bumpPruefenSession(1);
      $$("button", tr).forEach(x => x.disabled = true);
      // counters move with the animation, not after it — the row is already
      // visibly leaving, so a number that waits 260 ms reads as lag
      TABLE_TOTALS["pruefenWrap"] = Math.max((TABLE_TOTALS["pruefenWrap"] || 1) - 1, 0);
      const gone = fadeRowOut(tr).then(() => { tr.remove(); repaintPruefen(tbody); });
      repaintPruefen(tbody);
      toast(msg);

      chain = chain.then(() => call(cid)).then(() => gone).then(() => {
        if (!tbody.rows.length) {
          const left = TABLE_TOTALS["pruefenWrap"] || 0;
          $("#pruefenWrap").innerHTML = left
            ? `<p class="muted" style="padding:12px">Diese Seite ist abgearbeitet — ${left} weitere warten. <b>Aktualisieren</b> lädt sie.</p>`
            : `<p class="muted" style="padding:12px">Nichts zu prüfen — alles entschieden. 🎉</p>`;
        }
      }, (e) => gone.then(() => {
        // Put it back — a decision that did not save must not look decided. Wait
        // for the animation first, then strip its inline heights, or the row
        // returns collapsed and invisible. The anchor may itself have been
        // decided in the meantime, so only use it while it is still attached.
        undoFade(tr);
        if (!tbody.isConnected) {
          // the table was replaced by the empty-state note while this was in
          // flight — reloading is the only honest way back
          toast(`Nicht gespeichert: ${e.message}`, "error");
          loadPruefen();
          return;
        }
        if (anchor && anchor.parentElement === tbody) tbody.insertBefore(tr, anchor);
        else tbody.appendChild(tr);
        TABLE_TOTALS["pruefenWrap"] = (TABLE_TOTALS["pruefenWrap"] || 0) + 1;
        bumpPruefenSession(-1);   // nicht gespeichert = nicht entschieden
        $$("button", tr).forEach(x => x.disabled = false);
        repaintPruefen(tbody);
        toast(`Nicht gespeichert: ${e.message}`, "error");
      }));
    };

    $$("#pruefenWrap .pruefen-ok").forEach(b => b.addEventListener("click", (ev) =>
      decide(ev.target,
             (cid) => api(`/api/companies/${cid}/enrichment/accept`, "POST",
                          { page_id: ev.target.dataset.domain }),
             "Website bestätigt — Anreicherung läuft im Hintergrund.")));
    $$("#pruefenWrap .pruefen-no").forEach(b => b.addEventListener("click", (ev) =>
      decide(ev.target,
             (cid) => api(`/api/companies/${cid}/identity/reject`, "POST", {}),
             "Abgelehnt — Suche nach der richtigen Website ist wieder offen.")));
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

  // ---- Tastatur für die Entscheidungen-Warteschlange -----------------------
  // 536 Entscheidungen sind Fließbandarbeit — Durchsatz ist ein Feature. Der
  // Handler klickt die ECHTEN Buttons der fokussierten Zeile: Serialisierung,
  // Zähler und Undo-Pfad bleiben exakt der Maus-Weg, nur die Hand bleibt auf
  // der Tastatur. ↑/↓ wählen, J = Ja, N = Nein, O = Website öffnen.
  function pruefenRows() { return $$("#pruefenWrap tbody tr"); }
  function pruefenFocused() { return $("#pruefenWrap tbody tr.row-focus"); }
  function pruefenFocus(tr) {
    pruefenRows().forEach(r => r.classList.toggle("row-focus", r === tr));
    tr?.scrollIntoView({ block: "nearest" });
  }
  function pruefenMove(step) {
    const rows = pruefenRows();
    if (!rows.length) return;
    const cur = pruefenFocused();
    const i = rows.indexOf(cur);
    pruefenFocus(rows[Math.min(Math.max(i + step, 0), rows.length - 1)]);
  }

  document.addEventListener("keydown", (ev) => {
    if (!$("#tab-pruefen")?.classList.contains("active")) return;
    if (/^(input|select|textarea)$/i.test(ev.target.tagName)) return;
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const k = ev.key.toLowerCase();
    if (ev.key === "ArrowDown") { ev.preventDefault(); pruefenMove(1); return; }
    if (ev.key === "ArrowUp") { ev.preventDefault(); pruefenMove(-1); return; }
    if (!["j", "n", "o"].includes(k)) return;
    const tr = pruefenFocused();
    if (!tr) { pruefenMove(1); return; }   // erster Druck holt den Fokus
    if (k === "o") {
      const a = $("td a[href]", tr);
      if (a) window.open(a.href, "_blank", "noopener");
      return;
    }
    // Fokus wandert VOR dem Klick zur Nachbarzeile — die entschiedene Zeile
    // animiert gerade hinaus und kann keinen Fokus mehr tragen
    const next = tr.nextElementSibling || tr.previousElementSibling;
    $(k === "j" ? ".pruefen-ok" : ".pruefen-no", tr)?.click();
    if (next) pruefenFocus(next);
  });

  // Sitzungszähler: sichtbarer Fortschritt motiviert bei 536 Entscheidungen
  // mehr als jede Fortschrittsleiste. Pro Tag in localStorage, ehrlich in beide
  // Richtungen — eine nicht gespeicherte Entscheidung zählt wieder herunter.
  const _pruefenSessionKey = () =>
    "adwatch.pruefenSession." + new Date().toISOString().slice(0, 10);
  function bumpPruefenSession(delta) {
    const n = Math.max((Number(localStorage.getItem(_pruefenSessionKey())) || 0) + delta, 0);
    localStorage.setItem(_pruefenSessionKey(), String(n));
    paintPruefenSession();
  }
  function paintPruefenSession() {
    const el = $("#pruefenSession");
    if (!el) return;
    const n = Number(localStorage.getItem(_pruefenSessionKey())) || 0;
    el.textContent = n ? `· heute entschieden: ${n}` : "";
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
    // Versprechen aus ensureCustomersLoaded(): hält, wenn Optionen geladen,
    // Standardfilter gesetzt und die erste Seite da ist. Die Karte wartet
    // darauf, damit sie denselben Filter schickt wie die Tabelle.
    bereit: null,
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

  // Gibt ein Versprechen zurück, das hält, wenn der FILTER endgültig steht.
  // Das ist wichtiger, als es aussieht: applyDefaultExclusion() setzt „Private
  // Endkunden ausgeschlossen" erst, nachdem die Optionen geladen sind. Wer die
  // Karte vorher öffnete, schickte einen anderen Filter los als die Tabelle
  // eine Sekunde später — zwei Abrufe, und der erste mit der falschen Frage.
  function ensureCustomersLoaded() {
    if (CUST.loaded) return CUST.bereit || Promise.resolve();
    CUST.loaded = true;
    CUST.bereit = loadCustomerFilterOptions().then(() => {
      applyDefaultExclusion();          // options must exist before they can be checked
      return loadCustomers();
    });
    loadJobs().then(jobs => {
      if (jobs.some(j => j.status === "running" || j.status === "queued")) startJobPolling();
    });
    return CUST.bereit;
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
    // Liste und Karte sind DERSELBE Filter in zwei Darstellungen. Jede
    // Filteränderung läuft durch diese Funktion — ist gerade die Karte offen,
    // muss sie mitziehen, sonst filtert man sichtbar ins Leere: die Pins
    // blieben stehen, während die (unsichtbare) Tabelle längst gefiltert war.
    if (!append && custMap && !$("#custMapWrap").classList.contains("hidden"))
      loadCustMapPins().catch(() => {});
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
    // dieselbe Markierung an der Fernbedienung über der Karte
    spaltenFilterLeiste("#custColFilters", "#customersTable");
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
        <td class="num">${fitCell(r.fit_score, true)}</td>
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

  // ---- Deep-Link: #/firma/123 ist die Adresse des Dossiers -----------------
  // Öffnen setzt den Hash, Schließen räumt ihn weg, der Zurück-Knopf des
  // Browsers schließt. Eine in Teams geteilte URL landet damit ohne Klickpfad
  // auf derselben Firma — das Dossier ist eine Seite, kein Zustand.
  let drawerCompanyId = null;
  const firmaHash = (id) => `#/firma/${id}`;

  window.addEventListener("hashchange", () => {
    const m = location.hash.match(/^#\/firma\/(\d+)$/);
    if (m && Number(m[1]) !== drawerCompanyId) openCompanyDrawer(Number(m[1]));
    else if (!m && drawerCompanyId != null) closeCompanyDrawer();
  });

  function closeCompanyDrawer() {
    drawerCompanyId = null;
    $("#companyDrawer").classList.add("hidden");
    $("#drawerBackdrop").classList.add("hidden");
    // Hash aufräumen, ohne einen History-Eintrag anzuhängen — sonst braucht
    // "zurück" nach dem Schließen zwei Klicks
    if (/^#\/firma\//.test(location.hash))
      history.replaceState(null, "", location.pathname + location.search);
  }

  // Wie die Website bewiesen wurde — in der Sprache des Nutzers, nicht des Codes.
  const MATCHED_BY_LABEL = {
    phone: "Telefon auf der Seite gefunden", plz_street: "PLZ + Straße stimmen",
    plz_name: "PLZ + Name stimmen", domain_in_name: "Domain steckt im Firmennamen",
    domain_plus_name: "Domain + Name stimmen", manual: "von Hand bestätigt",
    sap: "aus SAP übernommen (unbewiesen)",
  };
  const IDENTITY_WEB_CHIP = {
    verified:     ["idw-verified", "verifiziert"],
    needs_review: ["idw-review", "Vorschlag offen — im Entscheidungen-Tab"],
    not_found:    ["idw-notfound", "keine Website auffindbar (geprüft)"],
    unreachable:  ["idw-notfound", "Website nicht erreichbar"],
    conflict:     ["idw-conflict", "Konflikt — Seite gehört wem anderen"],
    unverified:   ["idw-unknown", "unbewiesen (Altbestand)"],
  };
  function identityWebChip(status) {
    const [cls, label] = IDENTITY_WEB_CHIP[status] || ["idw-unknown", "noch nicht geprüft"];
    return `<span class="tag ${cls}">${esc(label)}</span>`;
  }

  // ---------------- enrichment (drawer section + review queue) ----------------
  function enrichFieldsHtml(e) {
    const f = e.fields || {};
    const prov = e.provenance || {};
    // Jeder Wert trägt seinen Beweisgrad als Chip: „belegt" (Fakt von der
    // Seite, Konfidenz >= 0,7) vs „KI-Einschätzung" (Ableitung, muss vor einer
    // Ansprache geprüft werden). Hover zeigt Quelle + Zitat. Das ist dieselbe
    // Unterscheidung, die der PDF-Bericht macht — eine Sprache, zwei Orte.
    const provChip = (p) => {
      if (!p) return "";
      const belegt = (p.confidence ?? 0) >= 0.7;
      return ` <span class="tag ${belegt ? "tag-belegt" : "tag-ki"}">${belegt ? "belegt" : "KI-Einschätzung"}</span>`;
    };
    const kv = (label, value, key) => {
      if (value === undefined || value === null || value === "" ||
          (Array.isArray(value) && !value.length)) return "";
      const p = prov[key];
      const tip = p ? `${p.source}${p.confidence ? ` · ${Math.round(p.confidence * 100)}%` : ""}${p.evidence ? ` · „${p.evidence}"` : ""}` : "";
      const shown = Array.isArray(value) ? value.join(", ") : value;
      return `<div class="drawer-kv"><dt>${esc(label)}</dt><dd${tip ? ` title="${esc(tip)}"` : ""}>${esc(String(shown))}${provChip(p)}</dd></div>`;
    };
    const solarlux = f.mentions_solarlux === true
      ? `<span class="tag tag-saved">nennt Solarlux</span>`
      : (f.mentions_solarlux === false ? `<span class="tag">nennt Solarlux nicht</span>` : "");
    const comps = (f.competitor_brands || []).length
      ? `<span class="tag tag-filtered">Wettbewerber: ${esc(f.competitor_brands.join(", "))}</span>` : "";
    const body = [
      kv("Beschreibung", f.description_de, "description_de"),
      kv("Einschätzung", f.assessment_de, "assessment_de"),
      kv("Produkte", f.products, "products"),
      kv("Projekt-Fokus", f.project_focus, "project_focus"),
      kv("Gegründet", f.founded_year, "founded_year"),
      kv("Größe", f.employee_hint, "employee_hint"),
      kv("Rechtsform", f.legal_form, "legal_form"),
      kv("Einsatzgebiet", f.service_area, "service_area"),
      kv("Partner von", f.partner_of, "partner_of"),
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

  // Schriftverkehr im Dossier. Nur Anrisse — die Akte soll lesbar sein, nicht
  // die Seite fluten; der vollstaendige Verlauf lebt im CRM, wo er hingehoert.
  // Eingehende Mails sind hervorgehoben: "die Firma meldet sich bei UNS" ist
  // Nachfrage und damit ein anderes Signal als unser eigener Aufwand.
  async function loadDrawerMails(id) {
    const box = $("#drawerMailBody");
    if (!box) return;
    let d;
    try { d = await api(`/api/companies/${id}/emails?limit=40`); }
    catch (e) { box.innerHTML = `<p class="hint status-error">${esc(e.message)}</p>`; return; }
    const f = d.features || {};
    if (!d.emails.length) {
      box.innerHTML = `<p class="hint">Kein angehaengter Schriftverkehr.
        <span class="muted">Abruf umfasst bisher 2023+.</span></p>`;
      return;
    }
    box.innerHTML = `
      <p class="hint">
        <b>${deN(f.mails)}</b> Mails · <b>${deN(f.eingehend)}</b> eingehend
        (${((f.eingehend_anteil || 0) * 100).toFixed(0)} %) ·
        letzte vor ${f.tage_seit_letzter ?? "?"} Tagen ·
        Verlauf ueber ${deN(f.dauer_tage)} Tage
        <br><span class="muted">Eingehend heisst: die Firma hat sich bei uns
        gemeldet — Nachfrage, nicht Vertriebsaufwand.</span>
      </p>
      ${d.emails.map(m => `
        <div class="mail-item ${m.richtung === "eingehend" ? "mail-in" : ""}">
          <div class="mail-head">
            <span class="qual-badge ${m.richtung === "eingehend" ? "qual-stark" : "qual-mittel"}">
              ${m.richtung === "eingehend" ? "eingehend" : "ausgehend"}</span>
            <b>${esc(m.betreff || "(ohne Betreff)")}</b>
            <span class="spacer"></span>
            <span class="muted">${m.datum ? esc(deDate(m.datum)) : ""}</span>
          </div>
          <div class="mail-text">${esc(m.anriss || "")}${m.zeichen > 400 ? " …" : ""}</div>
        </div>`).join("")}
      ${d.emails.length >= 40 ? `<p class="hint">Nur die 40 neuesten. Der
        vollstaendige Verlauf steht im CRM.</p>` : ""}`;
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
    drawer.innerHTML = `<div class="drawer-body"><p class="hint">Lädt…</p></div>`;
    drawerCompanyId = id;
    if (location.hash !== firmaHash(id)) location.hash = firmaHash(id);

    let detail = null;
    try { detail = await api(`/api/companies/${id}/detail`); } catch (e) { /* untracked is fine */ }
    // works from ANY tab — prefer the Explorer's loaded row (may not exist yet
    // when the drawer opens from the dashboard), else the API's copy
    const row = (CUST.lastRows || []).find(r => r.id === id) || detail?.company;
    if (!row) { closeCompanyDrawer(); alert("Firma konnte nicht geladen werden."); return; }
    const m = detail?.metric;
    const st = row.resolution_status;

    const revRow = ["y0", "y1", "y2", "y3", "y4"].map((k, i) =>
      `<div class="drawer-rev"><span>${i === 0 ? "Akt. Jahr" : "-" + i}</span><b>${eur(row["revenue_" + k])}</b></div>`).join("");

    const website = row.website_domain
      ? `<a class="link" href="${esc(/^https?:\/\//.test(row.website_domain) ? row.website_domain : "https://" + row.website_domain)}" target="_blank">${esc(row.website_domain)}</a>` : "";

    const adBlock = m && m.has_data ? `
        ${drawerKv("Aktive Anzeigen", `<b>${m.total_active_ads}</b> (Meta ${m.meta_active_ads ?? 0} · Google ${m.google_active_ads ?? 0})`)}
        ${drawerKv("Neu diese Woche", m.new_ads ?? "—")}
        ${drawerKv("Score", m.score != null ? Math.round(m.score) + "/100" : "—")}
        ${drawerKv("Gesch. Ausgaben/Wo.", m.spend_low != null ? `${eur(m.spend_low)} – ${eur(m.spend_high)}` : "—")}
        ${drawerKv("Produkte", esc((m.products || []).join(", ")))}`
      : `<p class="hint">Noch keine Anzeigen-Daten — per <b>Ad lookup</b> in Firmen abrufen. <span class="muted">Nie abgerufen heißt unbekannt, nicht inaktiv.</span></p>`;

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
          <h3>Kandidaten — ist eine davon die richtige Seite?</h3>
          <p class="hint" style="margin:0 0 10px">Von der Identitätsprüfung gereiht. „Übernehmen" setzt die Seite als verifiziert (vor automatischen Änderungen geschützt).</p>
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
                ${isCurrent ? `<span class="role-badge">aktuell</span>` : ""}
                <div class="page-meta">${esc(cand.category || "")}${cand.category ? " · " : ""}${sig}</div>
              </div>
              ${cand.page_id && !isCurrent
                ? `<button class="btn btn-sm btn-primary drawer-use-cand" data-i="${i}">Übernehmen</button>` : ""}
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
          <button class="btn btn-sm drawer-copylink" title="Link zu dieser Firma kopieren — öffnet das Dossier direkt">🔗 Link</button>
          ${isReviewable ? `<button class="btn btn-sm drawer-next-review" title="Zur nächsten Firma springen, die eine Prüfung braucht">Nächste prüfen →</button>` : ""}
          <button class="btn btn-ghost drawer-close" title="Schließen">✕</button>
        </div>
      </div>
      <div class="drawer-body">
        <div class="drawer-section">
          <h3>Identität — Website</h3>
          <dl class="drawer-grid">
            ${drawerKv("Status", identityWebChip(row.identity_status))}
            ${row.identity_matched_by ? drawerKv("Beweis", esc(MATCHED_BY_LABEL[row.identity_matched_by] || row.identity_matched_by)) : ""}
            ${drawerKv("Website", website || `<span class="muted">—</span>`)}
            ${row.website_source ? drawerKv("Quelle", esc(row.website_source)) : ""}
          </dl>
          <p class="hint">Eine Website gilt erst als „diese Firma", wenn ein harter Beweis vorliegt — Telefon, PLZ + Straße/Name oder Domain = Name. Alles darunter bleibt Vorschlag.</p>
        </div>
        <div class="drawer-section">
          <div class="drawer-section-head">
            <h3>Identität — Meta-Seite</h3>
            <button id="drawerRecheckBtn" class="btn btn-sm" title="Identitätsprüfung neu laufen lassen (Website + Google + KI)">↻ Neu prüfen</button>
          </div>
          <div class="drawer-idrow">${idFbCell(row)}</div>
          <div class="inline-form" style="margin-top:10px">
            <input type="text" id="drawerPageId" placeholder="Page ID" value="${esc(row.page_id || "")}" style="flex:1;min-width:110px">
            <input type="text" id="drawerPageName" placeholder="Page name" value="${esc(row.page_name || "")}" style="flex:1.4;min-width:130px">
            ${st === "locked"
              ? `<button id="drawerUnlockBtn" class="btn btn-sm">Entsperren</button>`
              : `<button id="drawerLockBtn" class="btn btn-sm btn-primary" title="Speichern und einfrieren — automatische Prüfungen überschreiben nie">🔒 Sperren</button>`}
            ${(row.page_id || row.page_url)
              ? `<button id="drawerUnlinkBtn" class="btn btn-sm btn-danger" title="Falsche Seite entfernen — die Kandidatenliste bleibt zur Prüfung erhalten">✕ Trennen</button>` : ""}
          </div>
          <p class="hint">Sperren friert die Seite als verifiziert ein — automatische Prüfungen überschreiben sie nie.</p>
        </div>
        ${candHtml}
        ${dossierSection(detail?.dossier)}
        ${crmSection(detail?.company || row)}
        <div class="drawer-section">
          <h3>Werbung</h3>
          ${adBlock}
        </div>
        ${weekAds.length ? `
        <div class="drawer-section">
          <h3>Aktuelle Anzeigen (${weekAds.length})</h3>
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
        <div class="drawer-section" id="drawerMailSection">
          <h3>Schriftverkehr (Projektakte)</h3>
          <div id="drawerMailBody"><p class="hint">Lädt…</p></div>
        </div>
        <div class="drawer-section" id="drawerEnrichSection">
          <div class="drawer-section-head">
            <h3>Steckbrief — von der eigenen Website</h3>
            <button id="drawerEnrichBtn" class="btn btn-sm" title="Website finden (falls fehlend) und Firmeninfos von der eigenen Website lesen">✨ Anreichern</button>
          </div>
          <div id="drawerEnrichBody"><p class="hint">Lädt…</p></div>
        </div>
        <div class="drawer-section">
          <h3>Stammdaten</h3>
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
          <h3>Historie</h3>
          <dl class="drawer-grid">
            ${drawerKv("Letzte Identitätsprüfung", row.identity_checked_at ? deDate(row.identity_checked_at) : `<span class="muted">nie</span>`)}
            ${drawerKv("Letzter Anzeigen-Abruf", (m && m.week_start) ? deDate(m.week_start) : `<span class="muted">nie — unbekannt, nicht inaktiv</span>`)}
          </dl>
        </div>
        <div class="drawer-section">
          <h3>Firma bearbeiten</h3>
          <div class="inline-form">
            <input type="text" id="drawerName" value="${esc(row.name)}" style="flex:2;min-width:170px">
            <input type="text" id="drawerDomain" placeholder="Website-Domain" value="${esc(row.website_domain || "")}" style="flex:1.4;min-width:140px">
            <button id="drawerSaveBtn" class="btn btn-sm">Speichern</button>
          </div>
        </div>
      </div>`;

    $(".drawer-close", drawer).addEventListener("click", closeCompanyDrawer);
    $(".drawer-copylink", drawer)?.addEventListener("click", async () => {
      const url = location.origin + location.pathname + firmaHash(id);
      try { await navigator.clipboard.writeText(url); toast("✓ Link kopiert — öffnet dieses Dossier direkt.", "info"); }
      catch { prompt("Link zum Kopieren:", url); }   // Clipboard-API kann in http-Kontexten fehlen
    });
    wireVcLinks(drawer, id);
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
    loadDrawerMails(id);
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
