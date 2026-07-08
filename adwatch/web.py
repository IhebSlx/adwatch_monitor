"""Local dashboard (FastAPI). Run: python run.py serve  ->  http://127.0.0.1:8000"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import config, services
from .db import init_db
from .pipeline import reseed_from_file, run_once, seed_companies_if_empty

app = FastAPI(title="AdWatch")


class CompanyIn(BaseModel):
    name: str


class CompanyPatch(BaseModel):
    name: str


@app.on_event("startup")
def _startup() -> None:
    init_db()
    seed_companies_if_empty()


@app.get("/api/state")
def state():
    return {
        "mode": config.MODE,
        "backend": (config.LIVE_SOURCE if config.is_live() else "mock"),
        "companies": services.list_companies(),
        "metrics": services.latest_metrics(),
    }


@app.post("/api/companies")
def create_company(payload: CompanyIn):
    try:
        return services.add_company(payload.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/companies/{cid}")
def patch_company(cid: int, payload: CompanyPatch):
    try:
        services.update_company(cid, payload.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/companies/{cid}")
def remove_company(cid: int):
    services.delete_company(cid)
    return {"ok": True}


@app.post("/api/run")
def run():
    try:
        return run_once()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e))


@app.post("/api/reseed")
def reseed():
    n = reseed_from_file()
    return {"reseeded": n}


@app.get("/report.pdf")
def report_pdf():
    from .report import build_report
    path = build_report()
    return FileResponse(path, media_type="application/pdf", filename=path.split("/")[-1])


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


HTML = """
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AdWatch — Ad Activity Monitor</title>
<style>
  :root{--ink:#1f2933;--muted:#647380;--accent:#2b6cb0;--line:#d9e2ec;--bg:#f0f4f8;--card:#fff;--ok:#2f855a;--warn:#b7791f;--danger:#c53030}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg)}
  header{background:var(--card);border-bottom:1px solid var(--line);padding:16px 24px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  header h1{font-size:17px;margin:0;font-weight:650}
  .badge{font-size:11px;padding:3px 9px;border-radius:99px;background:var(--bg);color:var(--muted);border:1px solid var(--line)}
  .badge.mock{color:var(--warn);border-color:#f6e05e}
  .badge.live{color:var(--ok);border-color:#9ae6b4}
  .spacer{flex:1}
  main{max-width:920px;margin:20px auto;padding:0 24px;display:grid;gap:20px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px}
  .card h2{font-size:13px;letter-spacing:.03em;text-transform:uppercase;color:var(--muted);margin:0 0 12px}
  button{font:inherit;cursor:pointer;border-radius:8px;border:1px solid var(--line);background:var(--card);padding:8px 14px;color:var(--ink)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button.ghost{background:transparent}
  button:disabled{opacity:.5;cursor:default}
  input{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:8px;width:100%}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:6px 6px;border-bottom:1px solid var(--line);vertical-align:middle}
  th{font-size:11px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
  .up{color:var(--ok)} .down{color:#c53030}
  .muted{color:var(--muted)}
  .addbar{display:flex;gap:8px;margin-bottom:10px}
  .addbar input{flex:1}
  .toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .prods{font-size:12px;color:var(--muted)}
  .note{font-size:12px;color:var(--muted);margin-top:10px}
  /* compact company row */
  .crow{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--line)}
  .crow:last-child{border-bottom:none}
  .crow input{padding:6px 8px;font-size:13px}
  .dot{width:9px;height:9px;border-radius:99px;flex:0 0 auto;cursor:help}
  .dot.pending{background:#a0aec0}
  .dot.confirmed{background:var(--ok)}
  .dot.ambiguous{background:var(--warn)}
  .dot.no_ads_found{background:var(--danger)}
  .iconbtn{background:transparent;border:none;padding:4px 6px;font-size:15px;line-height:1;color:var(--muted);border-radius:6px}
  .iconbtn:hover{background:var(--bg);color:var(--danger)}
  .save-btn{display:none;font-size:12px;padding:5px 10px}
  .save-btn.show{display:inline-block}
  .pill{font-size:11px;padding:2px 8px;border-radius:99px;background:var(--bg);color:var(--muted)}
</style></head><body>
<header>
  <h1>AdWatch</h1><span id="modeBadge" class="badge">…</span>
  <span class="spacer"></span>
  <div class="toolbar">
    <button id="runBtn" class="primary">Fetch latest ads</button>
    <a href="/report.pdf" target="_blank"><button class="ghost">Download PDF</button></a>
  </div>
</header>
<main>
  <section class="card">
    <h2>Companies <span id="cCount" class="pill"></span></h2>
    <div class="addbar">
      <input id="newName" placeholder="Add a company…">
      <button class="primary" id="addBtn">Add</button>
    </div>
    <div id="companyRows"></div>
    <div class="note">Hover the dot for status. Renaming clears the matched page (re-checked on next fetch). <button class="ghost" id="reseedBtn" style="padding:3px 8px;font-size:11px">Reset list from file</button></div>
  </section>

  <section class="card">
    <h2>Latest insights <span id="weekTag" class="pill"></span></h2>
    <table><thead><tr>
      <th>Company</th><th>Active ads</th><th>Hiring</th><th>Selling</th>
      <th>Products</th><th>Est. spend / wk</th>
    </tr></thead><tbody id="metricRows"></tbody></table>
    <div class="note">Spend is a <b>modelled estimate</b> (low–high), not published by Meta. Tune assumptions in <code>spend_assumptions.yaml</code>.</div>
  </section>
</main>
<script>
const $=s=>document.querySelector(s);
const esc=s=>(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
const eur=v=>v==null?"-":"€"+Math.round(v).toLocaleString("de-DE");
async function api(u,m,b){const o={method:m||"GET",headers:{"Content-Type":"application/json"}};if(b)o.body=JSON.stringify(b);const r=await fetch(u,o);if(!r.ok){throw new Error((await r.json()).detail||r.statusText)}return r.json();}

function companyRow(c){
  const tip=c.status_label+(c.page_name?` — matched: ${esc(c.page_name)}`:"");
  return `<div class="crow" data-id="${c.id}" data-orig="${esc(c.name)}">
    <span class="dot ${c.resolution_status}" title="${esc(tip)}"></span>
    <input value="${esc(c.name)}" data-f="name" style="flex:1">
    <button class="ghost save-btn">Save</button>
    <button class="iconbtn del" title="Remove">🗑</button>
  </div>`;
}
function metricRow(m){
  const cats=m.ads_by_category||{};
  let active=m.has_data?String(m.total_active_ads):'<span class="muted">no data</span>';
  if(m.has_data&&m.delta_ads!=null&&m.delta_ads!==0){
    const up=m.delta_ads>0;active+=` <span class="${up?'up':'down'}">${up?'▲':'▼'}${Math.abs(m.delta_ads)}</span>`;
  }
  let spend='-';
  if(m.has_data){spend=m.total_active_ads===0?'0':`${eur(m.spend_low)}–${eur(m.spend_high)}`;}
  const prods=(m.products&&m.products.length)?esc(m.products.join(", ")):'<span class="muted">—</span>';
  return `<tr><td>${esc(m.company)}</td><td>${active}</td>
    <td>${m.has_data?(cats.recruitment||0):'-'}</td>
    <td>${m.has_data?(cats.product_sale||0):'-'}</td>
    <td class="prods">${prods}</td><td>${spend}</td></tr>`;
}

async function load(){
  const s=await api("/api/state");
  const badge=$("#modeBadge");
  badge.textContent=s.mode==="live"?("live · "+s.backend):"mock · sample data";
  badge.className="badge "+(s.mode==="live"?"live":"mock");
  $("#companyRows").innerHTML=s.companies.map(companyRow).join("");
  $("#cCount").textContent=s.companies.length;
  $("#metricRows").innerHTML=s.metrics.map(metricRow).join("");
  const wk=s.metrics.find(m=>m.week_start);
  $("#weekTag").textContent=wk?("week of "+wk.week_start):"no runs yet";
  bind();
}
function bind(){
  document.querySelectorAll(".crow").forEach(row=>{
    const id=row.dataset.id;
    const input=row.querySelector('[data-f=name]');
    const saveBtn=row.querySelector(".save-btn");
    input.addEventListener("input",()=>{
      saveBtn.classList.toggle("show", input.value.trim()!==row.dataset.orig);
    });
    input.addEventListener("keydown",e=>{if(e.key==="Enter")saveBtn.click();});
    saveBtn.onclick=async()=>{
      try{await api(`/api/companies/${id}`,"PATCH",{name:input.value.trim()});await load();}
      catch(e){alert(e.message);}
    };
    row.querySelector(".del").onclick=async()=>{
      if(!confirm(`Remove "${row.dataset.orig}" and its data?`))return;
      await api(`/api/companies/${id}`,"DELETE");await load();
    };
  });
}
$("#addBtn").onclick=async()=>{
  const name=$("#newName").value.trim();if(!name)return;
  try{await api("/api/companies","POST",{name});$("#newName").value="";await load();}
  catch(e){alert(e.message);}
};
$("#newName").addEventListener("keydown",e=>{if(e.key==="Enter")$("#addBtn").click();});
$("#runBtn").onclick=async()=>{
  const b=$("#runBtn");b.disabled=true;b.textContent="Fetching… this can take a few minutes";
  try{const r=await api("/api/run","POST");
    b.textContent=`Done · ${r.collected} companies, ${r.errors} errors`;
    await load();setTimeout(()=>{b.textContent="Fetch latest ads";b.disabled=false;},3000);
  }catch(e){alert(e.message);b.textContent="Fetch latest ads";b.disabled=false;}
};
$("#reseedBtn").onclick=async()=>{
  if(!confirm("Reset company list to the file? This wipes current companies and their data."))return;
  await api("/api/reseed","POST");await load();
};
load();
</script></body></html>
"""
