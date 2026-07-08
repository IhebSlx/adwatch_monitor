/* AdWatch frontend — vanilla JS, no build step, no framework.
   Talks to the FastAPI backend in adwatch/web.py via fetch()/SSE. */
(() => {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const CATEGORY_LABELS = { recruitment: "Hiring", product_sale: "Selling",
    brand_awareness: "Brand", event_promo: "Event", other: "Other" };
  const STATUS_LABEL = { confirmed: "confirmed", ambiguous: "ambiguous",
    no_ads_found: "no ads found", pending: "pending" };
  const RUN_STATUS_LABEL = { ok: "ok", no_active_ads: "no active ads",
    no_ads_found: "no ads found", ambiguous_match: "ambiguous", error: "error" };
  const FLAG_LABEL = { new_campaign: "New campaign", first_seen: "First activity",
    biggest_mover: "Biggest mover", most_active: "Most active",
    hiring_push: "Hiring push", went_quiet: "Went quiet" };

  let STATE = null;
  let selectedCompanyId = null;
  const searchTermCache = {};   // company_id -> default search term
  const expandedPages = new Set(); // company_ids whose "Linked pages" panel is open

  // ------------------------------------------------------------------ utils
  async function api(path, method, body) {
    const opts = { method: method || "GET", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(path, opts);
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

  const eur = (v) => v == null ? "—" : "€" + Math.round(v).toLocaleString("de-DE");

  // facebook.com/<page_id> is a stable permalink for any real Facebook page —
  // no need to store a separate profile URL per page.
  const fbPageUrl = (pageId) => `https://www.facebook.com/${encodeURIComponent(pageId)}`;

  function spendCell(m) {
    if (!m.has_data) return "—";
    if (m.total_active_ads === 0) return "€0";
    return `${eur(m.spend_low)} – ${eur(m.spend_high)}`;
  }

  // ------------------------------------------------------------------ load + render
  async function loadState() {
    STATE = await api("/api/state");
    render();
  }

  function render() {
    renderTopbar();
    renderSignals();
    renderKpis();
    renderCompanyTable();
    renderCompaniesTab();
    if (selectedCompanyId != null) loadDetail(selectedCompanyId);
  }

  function renderTopbar() {
    $$(".mode-btn").forEach(b => b.classList.toggle("active", b.dataset.mode === STATE.mode));
    const tags = [`<span class="tag">Backend: ${esc(STATE.backend)}</span>`,
                  `<span class="tag">Country: ${esc(STATE.country)}</span>`,
                  `<span class="tag">Classifier: ${esc(STATE.classifier)}</span>`];
    $("#subbarTags").innerHTML = tags.join("");

    const fetchBtn = $("#fetchBtn");
    fetchBtn.disabled = STATE.fetch_running || (STATE.mode === "live" && !STATE.apify_configured);
    fetchBtn.textContent = STATE.fetch_running ? "Fetching…" : "Fetch latest ads";

    const week = (STATE.metrics.find(m => m.week_start) || {}).week_start;
    const modeTag = STATE.mode === "live" ? "LIVE" : "MOCK · sample data";
    $("#pageSub").textContent = `${modeTag} · ${STATE.companies.length} companies tracked`
      + (week ? ` · data for week of ${week}` : " · no data collected yet");

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
    const totalNew = sum(m => m.new_ads);
    const hiring = sum(m => (m.ads_by_category || {}).recruitment);
    const selling = sum(m => (m.ads_by_category || {}).product_sale);
    const spendLo = sum(m => m.spend_low), spendHi = sum(m => m.spend_high);
    const active = have.filter(m => (m.total_active_ads || 0) > 0).length;

    const kpis = [
      ["Active advertisers", `${active}/${STATE.companies.length}`],
      ["Active ads", totalAds],
      ["New this week", totalNew],
      ["Hiring / Selling", `${hiring} / ${selling}`],
      ["Est. spend / week", have.length ? `${eur(spendLo)}–${eur(spendHi)}` : "—"],
    ];
    $("#kpis").innerHTML = kpis.map(([label, value]) =>
      `<div class="kpi"><div class="kpi-label">${esc(label)}</div><div class="kpi-value">${value}</div></div>`
    ).join("");
  }

  function renderCompanyTable() {
    const rows = [...STATE.metrics].sort((a, b) => (b.score || 0) - (a.score || 0));
    const body = $("#companyTableBody");
    body.innerHTML = rows.map(m => {
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
        <td class="num">${m.has_data ? m.total_active_ads : "—"}</td>
        <td class="num">${deltaHtml}</td>
        <td class="num">${m.has_data ? (m.new_ads ?? "—") : "—"}</td>
        <td class="num">${m.has_data ? (cats.recruitment || 0) : "—"}</td>
        <td class="num">${m.has_data ? (cats.product_sale || 0) : "—"}</td>
        <td>${esc((m.products || []).join(", "))}</td>
        <td>${spendCell(m)}</td>
        <td class="muted">${esc(note)}</td>
      </tr>`;
    }).join("");

    $$("tr", body).forEach(tr => {
      tr.addEventListener("click", () => {
        selectedCompanyId = Number(tr.dataset.cid);
        $$("tr", body).forEach(r => r.classList.toggle("selected", r === tr));
        loadDetail(selectedCompanyId);
      });
    });
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
      html += week.pages.map(p => `
        <div class="page-row">
          <span class="dot dot-${p.status === "ok" ? "confirmed" : (p.status === "error" ? "no_ads_found" : "pending")}"></span>
          <b>${esc(p.page_name || p.page_id)}</b><span class="role-badge">${esc(p.role || "main")}</span>
          — ${p.ads} ads · fetched ${esc(p.run_date)}
          ${p.page_id ? ` · <a class="link" href="${esc(fbPageUrl(p.page_id))}" target="_blank">Open Facebook page ↗</a>` : ""}
        </div>`).join("");
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
          <th>Category</th><th>Product</th><th>From page</th><th>CTA</th><th>Media</th>
          <th class="num">EU reach</th><th>Start</th><th>Ad text</th><th>Links</th>
        </tr></thead><tbody>` +
        week.ads.map(a => `<tr>
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

  // ------------------------------------------------------------------ companies & pages tab
  function renderCompaniesTab() {
    const list = $("#companiesList");
    list.innerHTML = STATE.companies.map(c => {
      const open = expandedPages.has(c.id);
      return `<div class="company-row" data-cid="${c.id}">
        <div class="company-row-head">
          <span class="dot dot-${c.resolution_status}"></span>
          <div style="flex:1">
            <input type="text" class="company-name-input" value="${esc(c.name)}" data-orig="${esc(c.name)}">
            <div class="status-label">${esc(c.status_label)}</div>
          </div>
          <div class="company-actions">
            <button class="btn btn-sm fetch-one-btn" ${STATE.fetch_running ? "disabled" : ""}
                    title="Fetch only this company's data">Fetch</button>
            <button class="btn btn-sm save-btn" disabled>Save</button>
            <button class="btn btn-sm del-btn">Delete</button>
          </div>
        </div>
        <button class="pages-toggle">${open ? "Hide" : "Show"} linked pages (${c.pages.length})${
          (c.resolution_status !== "confirmed" && c.resolution_status !== "pending") ? " — needs attention" : ""}</button>
        <div class="pages-body ${open ? "open" : ""}" id="pages-${c.id}"></div>
      </div>`;
    }).join("");

    $$(".company-row", list).forEach(row => {
      const cid = Number(row.dataset.cid);
      const c = STATE.companies.find(x => x.id === cid);
      const input = $(".company-name-input", row);
      const saveBtn = $(".save-btn", row);
      input.addEventListener("input", () => saveBtn.disabled = input.value.trim() === input.dataset.orig);
      saveBtn.addEventListener("click", async () => {
        try { await api(`/api/companies/${cid}`, "PATCH", { name: input.value.trim() }); await loadState(); }
        catch (e) { alert(e.message); }
      });
      $(".del-btn", row).addEventListener("click", async () => {
        if (!confirm(`Delete "${c.name}" and all its collected data?`)) return;
        await api(`/api/companies/${cid}`, "DELETE"); await loadState();
      });
      $(".fetch-one-btn", row).addEventListener("click", () => startFetch(cid));
      $(".pages-toggle", row).addEventListener("click", async () => {
        if (expandedPages.has(cid)) { expandedPages.delete(cid); }
        else { expandedPages.add(cid); await ensureSearchTerm(cid); }
        renderCompaniesTab();
      });
      if (expandedPages.has(cid)) renderPagesBody(cid, c);
    });
  }

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
    return `<div class="candidate-item">
      <div>
        <b>${esc(cand.name || "(unnamed page)")}</b>${cand.category ? ` · ${esc(cand.category)}` : ""}
        <div class="page-meta">${esc(bits.join(" · "))}</div>
        ${cand.profile_uri ? `<a class="link" href="${esc(cand.profile_uri)}" target="_blank">Open page ↗</a>` : ""}
      </div>
      <button class="btn btn-sm use-btn">Use as main</button>
    </div>`;
  }

  function renderPagesBody(cid, c) {
    const box = $(`#pages-${cid}`);
    box.classList.add("open");
    let html = "";

    if (c.pages.length) {
      html += c.pages.map(p => {
        const ev = p.evidence || {};
        let evLine = "";
        if (ev.method === "landing_url") evLine = `evidence: <code>${esc(ev.url)}</code> · utm "${esc(ev.utm_campaign)}"`;
        else if (ev.method === "name_search") evLine = `evidence: name search · similarity ${esc(ev.similarity)}`;
        return `<div class="page-item">
          <div>
            <b>${esc(p.page_name || p.page_id)}</b>
            <span class="role-badge">${esc(p.role)}</span>
            <span class="role-badge">${esc(p.status_label)}</span>
            <div class="page-meta">${evLine}</div>
            <div class="page-meta">page id ${esc(p.page_id)}
              · <a class="link" href="${esc(fbPageUrl(p.page_id))}" target="_blank">Open Facebook page ↗</a></div>
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
        <button class="btn btn-sm search-btn" ${STATE.mode !== "live" ? "disabled title=\"Switch to Live mode\"" : ""}>Search</button>
      </div>
      <div class="live-candidates"></div>
      <div class="manual-row">
        <input type="text" class="manual-pageid-input" placeholder="Add page by ID (view_all_page_id=…)">
        <select class="manual-role-select"><option value="main">main</option><option value="partner">partner</option></select>
        <button class="btn btn-sm link-manual-btn">Link</button>
      </div>`;

    box.innerHTML = html;

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
  function startFetch(companyId) {
    const body = companyId != null ? { company_id: companyId } : undefined;
    api("/api/fetch", "POST", body).then(({ run_id }) => {
      STATE.fetch_running = true;
      renderTopbar();
      const panel = $("#fetchPanel"), bar = $("#progressFill"), log = $("#fetchLog"), status = $("#fetchStatus");
      panel.classList.remove("hidden"); log.innerHTML = ""; bar.style.width = "0%";
      const companyName = companyId != null ? (STATE.companies.find(c => c.id === companyId) || {}).name : null;
      status.textContent = companyName
        ? `Fetching ${companyName} in ${STATE.mode.toUpperCase()} mode…`
        : `Running in ${STATE.mode.toUpperCase()} mode…`;

      const es = new EventSource(`/api/fetch/stream/${run_id}`);
      const addLog = (text) => { const d = document.createElement("div"); d.textContent = text; log.appendChild(d); log.scrollTop = log.scrollHeight; };

      es.onmessage = (ev) => {
        const evt = JSON.parse(ev.data);
        const phase = evt.phase;
        if (phase === "begin") status.textContent = `Fetching ${evt.total} ${evt.total === 1 ? "company" : "companies"} via ${evt.backend}…`;
        else if (phase === "company_start") {
          bar.style.width = `${100 * (evt.i - 1) / Math.max(evt.total, 1)}%`;
          addLog(`→ ${evt.company} — resolving / fetching…`);
        } else if (phase === "company_done") {
          bar.style.width = `${100 * evt.i / Math.max(evt.total, 1)}%`;
          const extra = evt.page_name ? ` · ${evt.page_name}` : "";
          addLog(`${evt.status === "error" ? "✗" : "✓"} ${evt.company} — ${evt.status} · ${evt.ads} ads${extra}`);
        } else if (phase === "sweep_start") {
          bar.style.width = "97%";
          addLog("→ Partner sweep — searching hub campaigns for partner accounts…");
        } else if (phase === "sweep_done") {
          addLog(evt.error ? `✗ Partner sweep failed: ${evt.error}`
                            : `Partner sweep — ${evt.linked} page(s) newly linked, ${evt.attributed} ad(s) attributed`);
        } else if (phase === "result") {
          bar.style.width = "100%";
          if (evt.error) status.textContent = `Run failed: ${evt.error}`;
          else status.textContent = `Done · ${evt.summary.collected}/${evt.summary.companies} collected`
            + (evt.summary.errors ? ` · ${evt.summary.errors} error(s)` : "");
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

  // ------------------------------------------------------------------ wiring
  function wireStatic() {
    $$(".mode-btn").forEach(btn => btn.addEventListener("click", async () => {
      if (btn.classList.contains("active")) return;
      try { await api("/api/mode", "POST", { mode: btn.dataset.mode }); selectedCompanyId = null;
            $("#detailPanel").classList.add("hidden"); await loadState(); }
      catch (e) { alert(e.message); }
    }));

    $("#fetchBtn").addEventListener("click", startFetch);

    $("#resetBtn").addEventListener("click", async () => {
      if (!confirm("Wipe all companies and their collected data for the current mode, then reload from config/companies.yaml?")) return;
      try { await api("/api/reseed", "POST"); selectedCompanyId = null; await loadState(); }
      catch (e) { alert(e.message); }
    });

    $$(".tab").forEach(tab => tab.addEventListener("click", () => {
      $$(".tab").forEach(t => t.classList.toggle("active", t === tab));
      $$(".tab-panel").forEach(p => p.classList.toggle("active", p.id === `tab-${tab.dataset.tab}`));
    }));

    $("#addCompanyForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = $("#newCompanyName");
      const name = input.value.trim();
      if (!name) return;
      try { await api("/api/companies", "POST", { name }); input.value = ""; await loadState(); }
      catch (e) { alert(e.message); }
    });

    $("#pdfBtn").addEventListener("click", async () => {
      const btn = $("#pdfBtn"), link = $("#pdfLink");
      btn.disabled = true; btn.textContent = "Building…";
      try {
        const r = await fetch("/api/report/top5");
        if (!r.ok) throw new Error("Report generation failed");
        const blob = await r.blob();
        const cd = r.headers.get("Content-Disposition") || "";
        const m = cd.match(/filename="?([^"]+)"?/);
        link.href = URL.createObjectURL(blob);
        link.download = m ? m[1] : "adwatch_top5.pdf";
        link.classList.remove("hidden");
      } catch (e) { alert(e.message); }
      finally { btn.disabled = false; btn.textContent = "Generate Top-5 PDF"; }
    });
  }

  wireStatic();
  loadState();
})();
