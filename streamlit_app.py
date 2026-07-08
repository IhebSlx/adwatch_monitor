"""AdWatch — Streamlit dashboard (the main UI).

Run:  streamlit run streamlit_app.py

Imports the same services/pipeline the CLI uses — no HTTP layer in between — so
what you see here is exactly what a scheduled `python run.py run` would store.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from adwatch import config, services
from adwatch.db import init_db
from adwatch.pipeline import reseed_from_file, run_once, seed_companies_if_empty
from adwatch.sources.meta import search_term

st.set_page_config(page_title="AdWatch — Ad Activity Monitor", page_icon="📡", layout="wide")

STATUS_EMOJI = {
    "confirmed": "🟢",
    "ambiguous": "🟡",
    "no_ads_found": "🔴",
    "pending": "⚪",
}
CATEGORY_LABELS = {
    "recruitment": "Hiring",
    "product_sale": "Selling",
    "brand_awareness": "Brand",
    "event_promo": "Event",
    "other": "Other",
}

# --- one-time DB setup (idempotent, cheap on rerun) -------------------------
init_db()
seed_companies_if_empty()


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


# ============================================================================
# Sidebar — mode, fetch, admin
# ============================================================================
if "mode" not in st.session_state:
    st.session_state.mode = config.MODE

with st.sidebar:
    st.markdown("## 📡 AdWatch")
    st.caption("Ad-activity monitor · Meta Ad Library")

    mode = st.radio(
        "Data mode",
        options=["live", "mock"],
        index=0 if st.session_state.mode == "live" else 1,
        format_func=lambda m: "🔴 Live (real Apify calls)" if m == "live" else "🧪 Mock (offline sample data)",
        help="Live spends Apify credits. Mock generates deterministic fake data to exercise the UI for free.",
    )
    st.session_state.mode = mode
    # pipeline/classifier read config.MODE at call time, so this override is enough
    config.MODE = mode

    if mode == "live" and not config.APIFY_API_TOKEN:
        st.error("Live mode selected but APIFY_API_TOKEN is empty in `.env`.")

    st.divider()

    fetch = st.button("⬇️  Fetch latest ads", type="primary", use_container_width=True,
                      disabled=(mode == "live" and not config.APIFY_API_TOKEN))
    st.caption(
        "Live runs one Apify call per company (identity check + data pull in one). "
        "Expect 1–3 min for the full list."
    )

    if fetch:
        with st.spinner("Fetching… resolving pages and pulling ads. This can take a few minutes."):
            try:
                summary = run_once()
                st.session_state.last_summary = summary
            except Exception as exc:  # noqa: BLE001
                st.session_state.last_summary = {"error": str(exc)}

    if "last_summary" in st.session_state:
        s = st.session_state.last_summary
        if "error" in s:
            st.error(f"Run failed: {s['error']}")
        else:
            st.success(
                f"Week of {s['week_start']} · {s['collected']}/{s['companies']} collected"
                + (f" · {s['errors']} error(s)" if s['errors'] else "")
            )

    st.divider()
    backend = config.LIVE_SOURCE if mode == "live" else "mock"
    st.caption(f"Backend: `{backend}`  ·  Country: `{config.DEFAULT_COUNTRY}`")
    with st.expander("⚠️ Reset company list"):
        st.caption("Wipes all companies **and their collected data**, reloads from `config/companies.yaml`.")
        if st.button("Reset from file", use_container_width=True):
            n = reseed_from_file()
            st.success(f"Re-seeded {n} companies.")
            st.rerun()


# ============================================================================
# Header
# ============================================================================
metrics = services.latest_metrics()
companies = services.list_companies()
name_to_id = {c["name"]: c["id"] for c in companies}

badge = "🔴 LIVE" if st.session_state.mode == "live" else "🧪 MOCK"
st.title("Ad Activity Monitor")
week = next((m["week_start"] for m in metrics if m.get("week_start")), None)
st.caption(f"{badge}  ·  {len(companies)} companies tracked"
           + (f"  ·  latest data: week of {week}" if week else "  ·  no data collected yet"))

if not any(m["has_data"] for m in metrics):
    st.info("No data yet. Pick a mode in the sidebar and click **Fetch latest ads** to populate the dashboard.")

tab_insights, tab_companies, tab_help = st.tabs(["📊 Insights", "🏢 Companies", "ℹ️ How it works"])

# ============================================================================
# Tab: Insights
# ============================================================================
with tab_insights:
    # -- KPI row --
    have = [m for m in metrics if m["has_data"]]
    total_ads = sum(m["total_active_ads"] or 0 for m in have)
    total_hiring = sum((m["ads_by_category"] or {}).get("recruitment", 0) for m in have)
    total_selling = sum((m["ads_by_category"] or {}).get("product_sale", 0) for m in have)
    spend_lo = sum(m["spend_low"] or 0 for m in have)
    spend_hi = sum(m["spend_high"] or 0 for m in have)
    active_advertisers = sum(1 for m in have if (m["total_active_ads"] or 0) > 0)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Active advertisers", f"{active_advertisers}/{len(companies)}")
    k2.metric("Total active ads", total_ads)
    k3.metric("Hiring / Selling ads", f"{total_hiring} / {total_selling}")
    k4.metric("Est. spend / week", f"{_eur(spend_lo)}–{_eur(spend_hi)}" if have else "—")

    st.divider()
    st.subheader("This week by company")

    rows = []
    for m in metrics:
        cats = m["ads_by_category"] or {}
        delta = m.get("delta_ads")
        rows.append({
            "": STATUS_EMOJI.get(m["resolution_status"], "⚪"),
            "Company": m["company"],
            "Active ads": m["total_active_ads"] if m["has_data"] else None,
            "Δ vs last wk": (f"{'+' if delta > 0 else ''}{delta}" if delta not in (None, 0) else ""),
            "Hiring": cats.get("recruitment", 0) if m["has_data"] else None,
            "Selling": cats.get("product_sale", 0) if m["has_data"] else None,
            "Products": ", ".join(m["products"] or []) if m["has_data"] else "",
            "Est. spend / wk": _spend_cell(m),
            "Note": "" if m["resolution_status"] in ("confirmed", "pending") else m["status_label"],
        })
    df = pd.DataFrame(rows)
    st.dataframe(
        df, use_container_width=True, hide_index=True,
        column_config={
            "": st.column_config.TextColumn(width="small", help="🟢 confirmed  🟡 ambiguous  🔴 no ads found  ⚪ pending"),
            "Products": st.column_config.TextColumn(width="medium"),
            "Note": st.column_config.TextColumn(width="medium"),
        },
    )
    st.caption("Spend is a **modelled low–high estimate**, not published by Meta. "
               "Tune assumptions in `config/spend_assumptions.yaml`.")

    # -- per-company drill-down --
    st.divider()
    st.subheader("Company detail")
    if companies:
        sel = st.selectbox("Pick a company", [c["name"] for c in companies])
        cid = name_to_id[sel]
        detail = next((m for m in metrics if m["company"] == sel), None)
        hist = services.company_history(cid)
        run = services.latest_run_ads(cid)

        if detail:
            c1, c2, c3 = st.columns(3)
            c1.metric("Status", f"{STATUS_EMOJI.get(detail['resolution_status'], '⚪')} {detail['resolution_status']}")
            c2.metric("Active ads (latest)", detail["total_active_ads"] if detail["has_data"] else "—")
            c3.metric("Est. spend / wk", _spend_cell(detail))
            if detail.get("page_name") and detail["page_name"] != sel:
                st.caption(f"Matched Facebook page: **{detail['page_name']}**")
            if detail["resolution_status"] == "no_ads_found":
                st.warning("A name search returned **zero ads**. Either the name doesn't match how they "
                           "appear in the Ad Library, or they genuinely run no ads. Worth a manual check on "
                           "[facebook.com/ads/library](https://www.facebook.com/ads/library/).")

        # trend over weeks
        if len(hist) > 1:
            hdf = pd.DataFrame(hist).set_index("week_start")
            st.markdown("**Weekly trend**")
            st.line_chart(hdf[["total_active_ads", "recruitment", "product_sale"]])
        elif len(hist) == 1:
            st.caption("Only one week of data so far — trends appear once you've collected multiple weeks.")

        # individual ads
        if run["has_run"] and run["ads"]:
            st.markdown(f"**Ads in latest run** · {run['ads_scraped']} scraped · {run['run_date']}")
            adf = pd.DataFrame(run["ads"])
            adf["category"] = adf["category"].map(lambda c: CATEGORY_LABELS.get(c, c))
            st.dataframe(
                adf[["category", "product", "cta", "media_type", "reach", "start_date", "ad_text"]],
                use_container_width=True, hide_index=True,
                column_config={
                    "ad_text": st.column_config.TextColumn("Ad text", width="large"),
                    "reach": st.column_config.NumberColumn("EU reach"),
                },
            )
        elif run["has_run"]:
            st.caption(f"Latest run ({run['run_date']}): status **{run['status']}**, no individual ads stored.")

# ============================================================================
# Tab: Companies
# ============================================================================
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
    st.subheader("Manage existing")
    st.caption("Renaming clears the matched page — it's re-resolved on the next fetch. "
               "Use **Verify / confirm page** to fix the identity manually when auto-match is unsure.")

    def _render_candidates(cid: int, cands: list[dict], key_prefix: str):
        """Render candidate pages with a one-click 'use this page' confirm button."""
        if not cands:
            st.caption("No candidate pages to show. Run a search below.")
            return
        for i, cand in enumerate(cands):
            cc = st.columns([5, 2, 1.6])
            nm = cand.get("name") or "(unnamed page)"
            cat = f" · {cand['category']}" if cand.get("category") else ""
            sim = cand.get("similarity")
            with cc[0]:
                st.markdown(f"**{nm}**{cat}")
                bits = []
                if cand.get("active_ad_count") is not None:
                    bits.append(f"{cand['active_ad_count']} active / {cand.get('ad_count', 0)} total ads")
                if sim is not None:
                    bits.append(f"name match {int(sim * 100)}%")
                st.caption(" · ".join(bits) + f" · page id `{cand.get('page_id')}`")
            with cc[1]:
                if cand.get("profile_uri"):
                    st.markdown(f"[Open page ↗]({cand['profile_uri']})")
            with cc[2]:
                if st.button("✓ Use", key=f"{key_prefix}_use_{i}", use_container_width=True):
                    services.set_company_page(cid, cand.get("page_id"), cand.get("name"),
                                              cand.get("category"))
                    st.success(f"Confirmed “{nm}”.")
                    st.rerun()

    for c in companies:
        cols = st.columns([0.5, 6, 2])
        cols[0].markdown(f"### {STATUS_EMOJI.get(c['resolution_status'], '⚪')}")
        with cols[1]:
            new = st.text_input(
                c["name"], value=c["name"], key=f"name_{c['id']}",
                label_visibility="collapsed",
            )
            sub = c["status_label"]
            if c.get("page_name") and c["page_name"] != c["name"]:
                sub += f" · matched: {c['page_name']}"
            if c.get("page_id"):
                sub += f" · page id {c['page_id']}"
            st.caption(sub)
        with cols[2]:
            b1, b2 = st.columns(2)
            if b1.button("Save", key=f"save_{c['id']}", use_container_width=True,
                         disabled=(new.strip() == c["name"])):
                try:
                    services.update_company(c["id"], new)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
            if b2.button("🗑", key=f"del_{c['id']}", use_container_width=True):
                services.delete_company(c["id"])
                st.rerun()

        with st.expander("🔎 Verify / confirm page"
                         + ("" if c["resolution_status"] == "confirmed" else "  ⚠️ needs attention")):
            if c["resolution_status"] == "confirmed":
                st.success(f"Locked to page **{c.get('page_name') or c.get('page_id')}**. "
                           "A `0 active ads` result for this company is now trustworthy.")
                if st.button("Unlock (reset to pending)", key=f"unlock_{c['id']}"):
                    services.clear_resolution(c["id"])
                    st.rerun()

            # candidates captured during the last fetch
            if c.get("candidates"):
                st.markdown("**Candidate pages from the last fetch** — pick the correct one:")
                _render_candidates(c["id"], c["candidates"], key_prefix=f"stored_{c['id']}")

            st.divider()
            st.markdown("**Search the Ad Library for the right page** (one Apify call):")
            default_term = search_term(c["name"])
            sc = st.columns([5, 1.4])
            term = sc[0].text_input("Search term", value=default_term,
                                    key=f"term_{c['id']}", label_visibility="collapsed")
            if sc[1].button("Search", key=f"find_{c['id']}", use_container_width=True,
                            disabled=(st.session_state.mode == "live" and not config.APIFY_API_TOKEN)):
                if st.session_state.mode != "live":
                    st.warning("Switch to Live mode to search the real Ad Library.")
                else:
                    with st.spinner("Searching Ad Library…"):
                        try:
                            st.session_state[f"cands_{c['id']}"] = services.find_candidates(term)
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"Search failed: {exc}")
            found = st.session_state.get(f"cands_{c['id']}")
            if found:
                if found["candidates"]:
                    st.caption(f"Searched “{found['search_term']}” — {len(found['candidates'])} page(s) found:")
                    _render_candidates(c["id"], found["candidates"], key_prefix=f"live_{c['id']}")
                else:
                    st.warning(f"No pages found for “{found['search_term']}”. "
                               "Try a shorter/brand-only term, or paste the page ID directly below.")

            st.divider()
            st.markdown("**Or paste a Facebook page ID directly** "
                        "(from the page's Ad Library URL, `view_all_page_id=…`):")
            mc = st.columns([5, 1.4])
            manual_id = mc[0].text_input("Page ID", key=f"manual_{c['id']}",
                                         label_visibility="collapsed", placeholder="e.g. 290782014318379")
            if mc[1].button("Confirm", key=f"confirmmanual_{c['id']}", use_container_width=True,
                            disabled=not manual_id.strip()):
                services.set_company_page(c["id"], manual_id.strip())
                st.rerun()

# ============================================================================
# Tab: Help
# ============================================================================
with tab_help:
    st.subheader("Resolution status — is “0 ads” real, or a wrong name?")
    st.markdown(
        """
This is the core problem the tool solves. When a company name is searched, we group
whatever ads come back by their **actual Facebook page** and pick the best name match:

| | Status | Meaning |
|---|---|---|
| 🟢 | **confirmed** | Page locked in. Future fetches hit that exact page — a `0 active ads` result from here is a **real fact**, not a name mismatch. |
| 🟡 | **ambiguous** | Several pages matched about equally. Best guess is used but flagged — check the matched page name. |
| 🔴 | **no ads found** | The name search returned nothing. Either the name doesn't match the Ad Library, or they truly run no ads — worth a manual check. |
| ⚪ | **pending** | Not fetched yet. |

Renaming a company resets it to *pending* so the next fetch re-resolves it.
        """
    )
    st.subheader("How spend is estimated")
    st.markdown(
        """
Meta does **not** publish spend for ordinary commercial ads, so the figure is a
**modelled low–high interval**:

- **With EU reach data:** `reach × CPM ÷ 1000` at a low and high CPM band.
- **Without reach:** `active ads × daily-cost-per-ad × 7 days` at low/high bounds.

All assumptions live in `config/spend_assumptions.yaml`. Treat the number as a
directional estimate for comparison between companies and over time — not an invoice.
        """
    )
    st.subheader("Categories")
    st.markdown(
        "Each ad is classified as **Hiring** (recruitment), **Selling** (product_sale), "
        "**Brand**, **Event**, or **Other** by a keyword classifier "
        "(optionally upgraded to Claude if `ANTHROPIC_API_KEY` is set)."
    )
