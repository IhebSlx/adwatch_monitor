"""AdWatch — Streamlit dashboard (the main UI).

Run:  streamlit run streamlit_app.py    (or: python run.py serve)

Three parts, mirrored by the code layout:
  identity/  — which Facebook page(s) belong to each company   -> "Companies & Pages" tab
  collect/   — fetch + store weekly ad data                     -> sidebar "Fetch" button
  insights/  — classification, score, weekly flags              -> "Dashboard" tab
"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from adwatch import config, services
from adwatch.collect.meta_source import search_term
from adwatch.collect.pipeline import reseed_from_file, run_once, seed_companies_if_empty
from adwatch.db import init_db
from adwatch.identity import resolver
from adwatch.insights.flags import compute_flags

st.set_page_config(page_title="AdWatch — Ad Activity Monitor", page_icon="📡",
                   layout="wide", initial_sidebar_state="expanded")

# ---------------------------------------------------------------------------
# Restrained, timeless styling
# ---------------------------------------------------------------------------
st.markdown("""
<style>
  html, body, [class*="css"] { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  h1 { font-weight: 650 !important; letter-spacing: -0.01em; }
  h2, h3 { font-weight: 600 !important; }
  [data-testid="stMetric"] {
    background: var(--secondary-background-color);
    border: 1px solid rgba(128,128,128,.18);
    border-radius: 10px; padding: 14px 16px 10px;
  }
  [data-testid="stMetricLabel"] { opacity: .75; }
  .aw-flag {
    display: inline-block; margin: 0 8px 8px 0; padding: 7px 13px;
    border-radius: 9px; border: 1px solid rgba(128,128,128,.22);
    background: var(--secondary-background-color); font-size: .86rem; line-height: 1.35;
  }
  .aw-flag b { font-weight: 600; }
  .aw-flag .t { opacity: .65; font-size: .78rem; text-transform: uppercase; letter-spacing: .04em; }
  .aw-role {
    display: inline-block; padding: 1px 8px; border-radius: 99px; font-size: .72rem;
    border: 1px solid rgba(128,128,128,.3); opacity: .85; margin-left: 6px;
  }
</style>
""", unsafe_allow_html=True)

STATUS_DOT = {"confirmed": "🟢", "ambiguous": "🟡", "no_ads_found": "🔴", "pending": "⚪"}
CATEGORY_LABELS = {"recruitment": "Hiring", "product_sale": "Selling",
                   "brand_awareness": "Brand", "event_promo": "Event", "other": "Other"}
RUN_STATUS_ICON = {"ok": "🟢", "no_active_ads": "⚪", "no_ads_found": "🔴",
                   "ambiguous_match": "🟡", "error": "❌"}
FLAG_STYLE = {"new_campaign": "🚀", "first_seen": "✨", "biggest_mover": "📈",
              "most_active": "🏆", "hiring_push": "👷", "went_quiet": "💤"}


def _eur(v) -> str:
    if v is None:
        return "—"
    return "€" + format(round(v), ",").replace(",", ".")


def _spend_cell(m: dict) -> str:
    if not m["has_data"]:
        return "—"
    if m["total_active_ads"] == 0:
        return "€0"
    return f"{_eur(m['spend_low'])} – {_eur(m['spend_high'])}"


# ---------------------------------------------------------------------------
# Sidebar — mode, fetch, admin
# ---------------------------------------------------------------------------
if "mode" not in st.session_state:
    st.session_state.mode = config.MODE

with st.sidebar:
    st.markdown("## AdWatch")
    st.caption("Ad-activity monitor · Meta Ad Library")

    mode = st.radio(
        "Data mode",
        options=["live", "mock"],
        index=0 if st.session_state.mode == "live" else 1,
        format_func=lambda m: "Live — real Apify calls" if m == "live" else "Mock — offline sample data",
        help="Live spends Apify credits. Mock generates deterministic sample data for free. "
             "The two modes use separate databases and never mix.",
    )
    st.session_state.mode = mode
    config.MODE = mode          # pipeline/classifier/DB all read this at call time

    init_db()
    seed_companies_if_empty()

    if mode == "live" and not config.APIFY_API_TOKEN:
        st.error("Live mode needs APIFY_API_TOKEN in `.env`.")

    st.divider()
    fetch = st.button("Fetch latest ads", type="primary", use_container_width=True,
                      disabled=(mode == "live" and not config.APIFY_API_TOKEN))
    st.caption("One Apify call per linked page + one partner sweep. "
               "1–3 minutes for the full list; progress appears on the right.")

    st.divider()
    backend = config.LIVE_SOURCE if mode == "live" else "mock"
    llm = "Claude" if (mode == "live" and config.ANTHROPIC_API_KEY) else "keywords"
    st.caption(f"Backend `{backend}` · Country `{config.DEFAULT_COUNTRY}` · Classifier `{llm}`")
    with st.expander("Reset company list"):
        st.caption("Wipes all companies **and their collected data** for the current mode, "
                   "then reloads `config/companies.yaml`.")
        if st.button("Reset from file", use_container_width=True):
            n = reseed_from_file()
            st.success(f"Re-seeded {n} companies.")
            st.rerun()


# ---------------------------------------------------------------------------
# Fetch execution + live progress
# ---------------------------------------------------------------------------
if fetch:
    st.subheader("Fetching latest ads")
    bar = st.progress(0.0, text="Starting…")
    box = st.status(f"Running in {st.session_state.mode.upper()} mode…", expanded=True)

    def _cb(evt):
        phase = evt.get("phase")
        if phase == "begin":
            box.update(label=f"Fetching {evt['total']} companies via {evt['backend']}…")
        elif phase == "company_start":
            bar.progress((evt["i"] - 1) / max(evt["total"], 1),
                         text=f"[{evt['i']}/{evt['total']}] {evt['company']}")
            box.write(f"→ **{evt['company']}** — resolving / fetching…")
        elif phase == "company_done":
            bar.progress(evt["i"] / max(evt["total"], 1), text=f"[{evt['i']}/{evt['total']}] done")
            icon = RUN_STATUS_ICON.get(evt["status"], "•")
            extra = f" · {evt['page_name']}" if evt.get("page_name") else ""
            box.write(f"{icon} **{evt['company']}** — {evt['status']} · {evt['ads']} ads{extra}")
        elif phase == "sweep_start":
            bar.progress(0.97, text="Partner sweep…")
            box.write("→ **Partner sweep** — searching hub campaigns for partner accounts…")
        elif phase == "sweep_done":
            if evt.get("error"):
                box.write(f"❌ Partner sweep failed: {evt['error']}")
            else:
                box.write(f"🔗 Partner sweep — {evt['linked']} page(s) newly linked, "
                          f"{evt['attributed']} ad(s) attributed")

    try:
        summary = run_once(progress=_cb)
        bar.progress(1.0, text="Complete")
        box.update(label=f"Done · {summary['collected']}/{summary['companies']} collected"
                         + (f" · {summary['errors']} error(s)" if summary["errors"] else ""),
                   state="complete", expanded=False)
    except Exception as exc:  # noqa: BLE001
        box.update(label=f"Run failed: {exc}", state="error")
    st.divider()

# ---------------------------------------------------------------------------
# Data + header
# ---------------------------------------------------------------------------
metrics = services.latest_metrics()
companies = services.list_companies()
name_to_id = {c["name"]: c["id"] for c in companies}

st.title("Ad Activity Monitor")
week = next((m["week_start"] for m in metrics if m.get("week_start")), None)
mode_tag = "LIVE" if st.session_state.mode == "live" else "MOCK · sample data"
st.caption(f"{mode_tag}  ·  {len(companies)} companies tracked"
           + (f"  ·  data for week of {week}" if week else "  ·  no data collected yet"))

if not any(m["has_data"] for m in metrics):
    st.info("No data yet — click **Fetch latest ads** in the sidebar to populate the dashboard.")

tab_dash, tab_companies, tab_help = st.tabs(["Dashboard", "Companies & Pages", "How it works"])

# ===========================================================================
# TAB 1 — Dashboard (Part 3: insights)
# ===========================================================================
with tab_dash:
    have = [m for m in metrics if m["has_data"]]

    # ---- weekly signals ----------------------------------------------------
    flags = compute_flags(metrics)
    if flags:
        st.markdown("##### This week's signals")
        chips = "".join(
            f'<span class="aw-flag">{FLAG_STYLE.get(f["type"], "•")} '
            f'<span class="t">{f["label"]}</span><br><b>{f["company"]}</b> — {f["detail"]}</span>'
            for f in flags
        )
        st.markdown(chips, unsafe_allow_html=True)
        st.divider()

    # ---- KPI row -----------------------------------------------------------
    total_ads = sum(m["total_active_ads"] or 0 for m in have)
    total_new = sum(m["new_ads"] or 0 for m in have)
    total_hiring = sum((m["ads_by_category"] or {}).get("recruitment", 0) for m in have)
    total_selling = sum((m["ads_by_category"] or {}).get("product_sale", 0) for m in have)
    spend_lo = sum(m["spend_low"] or 0 for m in have)
    spend_hi = sum(m["spend_high"] or 0 for m in have)
    active_adv = sum(1 for m in have if (m["total_active_ads"] or 0) > 0)

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Active advertisers", f"{active_adv}/{len(companies)}")
    k2.metric("Active ads", total_ads)
    k3.metric("New this week", total_new)
    k4.metric("Hiring / Selling", f"{total_hiring} / {total_selling}")
    k5.metric("Est. spend / week", f"{_eur(spend_lo)}–{_eur(spend_hi)}" if have else "—")

    # ---- Top-5 PDF ----------------------------------------------------------
    pc1, pc2, _ = st.columns([1.2, 1.2, 2.6])
    with pc1:
        if st.button("Generate Top-5 PDF", use_container_width=True,
                     disabled=not any((m["total_active_ads"] or 0) > 0 for m in have)):
            from adwatch.report import build_top5_report
            with st.spinner("Building report…"):
                path = build_top5_report()
                with open(path, "rb") as fh:
                    st.session_state.top5_pdf = fh.read()
                st.session_state.top5_name = os.path.basename(path)
    with pc2:
        if st.session_state.get("top5_pdf"):
            st.download_button("Download Top-5 PDF", data=st.session_state.top5_pdf,
                               file_name=st.session_state.get("top5_name", "adwatch_top5.pdf"),
                               mime="application/pdf", use_container_width=True)

    st.divider()

    # ---- ranked company table ----------------------------------------------
    st.subheader("Companies this week")
    rows = []
    for m in sorted(metrics, key=lambda m: (m["score"] is not None, m["score"] or 0), reverse=True):
        cats = m["ads_by_category"] or {}
        delta = m.get("delta_ads")
        rows.append({
            "": STATUS_DOT.get(m["resolution_status"], "⚪"),
            "Company": m["company"],
            "Score": m["score"],
            "Active ads": m["total_active_ads"] if m["has_data"] else None,
            "Δ wk": (f"{'+' if delta > 0 else ''}{delta}" if delta not in (None, 0) else ""),
            "New": m["new_ads"] if m["has_data"] else None,
            "Hiring": cats.get("recruitment", 0) if m["has_data"] else None,
            "Selling": cats.get("product_sale", 0) if m["has_data"] else None,
            "Products": ", ".join(m["products"] or []) if m["has_data"] else "",
            "Est. spend / wk": _spend_cell(m),
            "Note": "" if m["resolution_status"] in ("confirmed", "pending") else m["status_label"],
        })
    df = pd.DataFrame(rows)
    table_event = st.dataframe(
        df, use_container_width=True, hide_index=True,
        on_select="rerun", selection_mode="single-row", key="company_table",
        column_config={
            "": st.column_config.TextColumn(width="small",
                 help="🟢 confirmed · 🟡 ambiguous · 🔴 no ads found · ⚪ pending"),
            "Score": st.column_config.ProgressColumn(format="%.0f", min_value=0, max_value=100,
                 help="0–100 activity score — volume, momentum, freshness, diversity "
                      "(weights in config/score_config.yaml)"),
            "Products": st.column_config.TextColumn(width="medium"),
            "Note": st.column_config.TextColumn(width="medium"),
        },
    )
    st.caption("Click a row for full detail. Spend is a modelled low–high estimate "
               "(Meta publishes no real spend for commercial ads).")

    clicked = list(table_event.selection.rows) if table_event and table_event.selection else []
    if clicked:
        st.session_state["selected_company"] = df.iloc[clicked[0]]["Company"]

    # ---- inline company detail ----------------------------------------------
    selected = st.session_state.get("selected_company")
    if selected and selected in name_to_id:
        cid = name_to_id[selected]
        m = next((x for x in metrics if x["company"] == selected), None)
        st.divider()
        st.subheader(selected)

        if m:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Score", f"{m['score']:.0f}/100" if m.get("score") is not None else "—")
            c2.metric("Active ads", m["total_active_ads"] if m["has_data"] else "—",
                      delta=m.get("delta_ads") or None)
            c3.metric("New this week", m["new_ads"] if m["has_data"] else "—")
            c4.metric("Est. spend / wk", _spend_cell(m))
            if m["resolution_status"] == "no_ads_found":
                st.warning("A name search returned zero ads. Either the name doesn't match the "
                           "Ad Library, or they genuinely run no ads — verify in the "
                           "Companies & Pages tab.")

        detail = services.latest_week_detail(cid)
        if detail["has_run"] and detail["pages"]:
            st.markdown("**Pages contributing this week**")
            for p in detail["pages"]:
                role = p["role"] or "main"
                st.markdown(
                    f"{RUN_STATUS_ICON.get(p['status'], '•')} **{p['page_name'] or p['page_id']}**"
                    f"<span class='aw-role'>{role}</span> — {p['ads']} ads · fetched {p['run_date']}",
                    unsafe_allow_html=True)

        hist = services.company_history(cid)
        if len(hist) > 1:
            st.markdown("**Weekly trend**")
            hdf = pd.DataFrame(hist).set_index("week_start")
            t1, t2 = st.columns(2)
            with t1:
                st.line_chart(hdf[["total_active_ads", "recruitment", "product_sale"]],
                              height=220)
            with t2:
                st.line_chart(hdf[["score"]], height=220)
        elif len(hist) == 1:
            st.caption("One week of data so far — trends appear from the second week on.")

        if detail["has_run"] and detail["ads"]:
            st.markdown("**All ads (latest week)**")
            adf = pd.DataFrame(detail["ads"])
            adf["category"] = adf["category"].map(lambda c: CATEGORY_LABELS.get(c, c))
            st.dataframe(
                adf[["category", "product", "page_name", "cta", "media_type",
                     "reach", "start_date", "ad_text", "ad_library_url", "landing_url"]],
                use_container_width=True, hide_index=True,
                column_config={
                    "ad_text": st.column_config.TextColumn("Ad text", width="large"),
                    "page_name": st.column_config.TextColumn("From page"),
                    "reach": st.column_config.NumberColumn("EU reach"),
                    "ad_library_url": st.column_config.LinkColumn("View ad", display_text="Open ↗"),
                    "landing_url": st.column_config.LinkColumn("Landing page", display_text="Open ↗"),
                },
            )

# ===========================================================================
# TAB 2 — Companies & Pages (Part 1: identity)
# ===========================================================================
with tab_companies:
    st.subheader("Add a company")
    with st.form("add_company", clear_on_submit=True):
        new_name = st.text_input("Company name", placeholder="e.g. Villatrium Schmidt GmbH")
        if st.form_submit_button("Add", type="primary"):
            try:
                services.add_company(new_name)
                st.success(f"Added “{new_name.strip()}”.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

    st.divider()
    st.subheader("Companies and their linked pages")
    st.caption("Every linked page is fetched weekly and rolled into the company's numbers. "
               "Partner pages are discovered automatically from solarlux.com landing-URL "
               "evidence (editable here). Renaming a company resets all its links.")

    def _render_candidates(cid: int, cands: list[dict], key_prefix: str):
        for i, cand in enumerate(cands):
            cc = st.columns([5, 2, 1.6])
            nm = cand.get("name") or "(unnamed page)"
            cat = f" · {cand['category']}" if cand.get("category") else ""
            with cc[0]:
                st.markdown(f"**{nm}**{cat}")
                bits = []
                if cand.get("active_ad_count") is not None:
                    bits.append(f"{cand['active_ad_count']} active / {cand.get('ad_count', 0)} total ads")
                if cand.get("similarity") is not None:
                    bits.append(f"name match {int(cand['similarity'] * 100)}%")
                st.caption(" · ".join(bits) + f" · page id `{cand.get('page_id')}`")
            with cc[1]:
                if cand.get("profile_uri"):
                    st.markdown(f"[Open page ↗]({cand['profile_uri']})")
            with cc[2]:
                if st.button("Use as main", key=f"{key_prefix}_use_{i}", use_container_width=True):
                    try:
                        resolver.set_main_page(cid, cand.get("page_id"), cand.get("name"),
                                               cand.get("category"))
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

    for c in companies:
        head = st.columns([0.4, 5.6, 2])
        head[0].markdown(f"### {STATUS_DOT.get(c['resolution_status'], '⚪')}")
        with head[1]:
            new = st.text_input(c["name"], value=c["name"], key=f"name_{c['id']}",
                                label_visibility="collapsed")
            st.caption(c["status_label"])
        with head[2]:
            b1, b2 = st.columns(2)
            if b1.button("Save", key=f"save_{c['id']}", use_container_width=True,
                         disabled=(new.strip() == c["name"])):
                try:
                    services.update_company(c["id"], new)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
            if b2.button("Delete", key=f"del_{c['id']}", use_container_width=True):
                services.delete_company(c["id"])
                st.rerun()

        with st.expander(f"Linked pages ({len(c['pages'])})"
                         + ("" if c["resolution_status"] in ("confirmed", "pending")
                            else " — needs attention")):
            # ---- current links ----
            if c["pages"]:
                for p in c["pages"]:
                    pc = st.columns([5, 2, 1.6])
                    with pc[0]:
                        st.markdown(f"**{p['page_name'] or p['page_id']}**"
                                    f"<span class='aw-role'>{p['role']}</span>"
                                    f"<span class='aw-role'>{p['status_label']}</span>",
                                    unsafe_allow_html=True)
                        ev = p.get("evidence") or {}
                        if ev.get("method") == "landing_url":
                            st.caption(f"evidence: `{ev.get('url')}` · utm “{ev.get('utm_campaign')}”")
                        elif ev.get("method") == "name_search":
                            st.caption(f"evidence: name search · similarity {ev.get('similarity')}")
                        st.caption(f"page id `{p['page_id']}`")
                    with pc[2]:
                        if st.button("Unlink", key=f"unlink_{p['id']}", use_container_width=True):
                            resolver.unlink_page(p["id"])
                            st.rerun()
            else:
                st.caption("No pages linked yet — fetched on the next run, or link one below.")

            # ---- candidates from the last resolution attempt ----
            if c.get("candidates"):
                st.markdown("**Candidates from the last name search:**")
                _render_candidates(c["id"], c["candidates"], key_prefix=f"stored_{c['id']}")

            st.divider()
            sc = st.columns([5, 1.4])
            term = sc[0].text_input("Search the Ad Library", value=search_term(c["name"]),
                                    key=f"term_{c['id']}", label_visibility="collapsed",
                                    placeholder="Search term")
            if sc[1].button("Search", key=f"find_{c['id']}", use_container_width=True,
                            disabled=(st.session_state.mode == "live" and not config.APIFY_API_TOKEN)):
                if st.session_state.mode != "live":
                    st.warning("Switch to Live mode to search the real Ad Library.")
                else:
                    with st.spinner("Searching Ad Library… (one Apify call)"):
                        try:
                            st.session_state[f"cands_{c['id']}"] = resolver.find_candidates(term)
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"Search failed: {exc}")
            found = st.session_state.get(f"cands_{c['id']}")
            if found:
                if found["candidates"]:
                    st.caption(f"“{found['search_term']}” — {len(found['candidates'])} page(s):")
                    _render_candidates(c["id"], found["candidates"], key_prefix=f"live_{c['id']}")
                else:
                    st.warning(f"No pages found for “{found['search_term']}”. Try a shorter term "
                               "or add the page id directly below.")

            mc = st.columns([3.5, 1.5, 1.4])
            manual_id = mc[0].text_input("Page ID", key=f"manual_{c['id']}",
                                         label_visibility="collapsed",
                                         placeholder="Add page by ID (view_all_page_id=…)")
            manual_role = mc[1].selectbox("Role", ["main", "partner"], key=f"role_{c['id']}",
                                          label_visibility="collapsed")
            if mc[2].button("Link", key=f"linkmanual_{c['id']}", use_container_width=True,
                            disabled=not manual_id.strip()):
                try:
                    resolver.add_page(c["id"], manual_id.strip(), role=manual_role, status="manual",
                                      evidence={"method": "manual"})
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

# ===========================================================================
# TAB 3 — How it works
# ===========================================================================
with tab_help:
    st.subheader("The three parts")
    st.markdown("""
| Part | What it does | Where |
|---|---|---|
| **1 · Identity** | Link each company to its Facebook page(s) — main page by name search, partner accounts by landing-URL evidence. Once linked, fetching is deterministic. | Companies & Pages tab · `adwatch/identity/` |
| **2 · Collect** | Weekly fetch of every linked page via the Ad Library (Apify), raw data stored per run. | Sidebar fetch · `adwatch/collect/` |
| **3 · Insights** | Classify each ad, estimate spend, score companies, raise weekly flags. | Dashboard tab · `adwatch/insights/` |
""")
    st.subheader("Resolution status — is “0 ads” real, or a wrong name?")
    st.markdown("""
| | Status | Meaning |
|---|---|---|
| 🟢 | **confirmed** | Page locked in. Future fetches hit that exact page — `0 active ads` is a real fact. |
| 🟡 | **ambiguous** | Several pages matched about equally; best guess used, flagged for review. |
| 🔴 | **no ads found** | Name search returned nothing — wrong name or genuinely no ads. Verify manually. |
| ⚪ | **pending** | Not fetched yet. |
""")
    st.subheader("Partner pages")
    st.markdown("""
Some companies also run ads from dedicated partner accounts (e.g. *Solarlux Quality
Partner …*). Those ads link to landing pages like
`solarlux.com/…/wintergarten-nagelschmidt/?utm_campaign=DE Nagelschmidt …` — the company
name in the URL is the fingerprint. A weekly sweep finds such pages and links them
automatically (status *auto-linked*); every link is editable in Companies & Pages.
Settings: `config/partner_discovery.yaml`.
""")
    st.subheader("Score")
    st.markdown("""
0–100 per company per week: **volume** (how many active ads, 40%) + **momentum**
(growing vs shrinking, 25%) + **freshness** (share of ads launched in the last 7 days,
20%) + **diversity** (breadth across hiring/selling/brand/event, 15%).
Weights: `config/score_config.yaml`.
""")
    st.subheader("Classification & spend")
    st.markdown("""
Each ad is classified as **Hiring / Selling / Brand / Event / Other**. With an
`ANTHROPIC_API_KEY` in `.env`, Claude does the classification (recommended); otherwise a
word-boundary keyword model. Evidence for every decision is stored with the ad.

Spend is a **modelled low–high interval** — Meta publishes no real spend for commercial
ads. With EU reach data: `reach × CPM`; otherwise a per-ad daily-cost fallback.
Assumptions: `config/spend_assumptions.yaml`.
""")
