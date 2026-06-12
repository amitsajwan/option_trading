"""Seller (S3 iron-condor) dashboard — clean, self-contained view.

Reads the seller daemon's state from mongo (written by strategy_app.seller.runner):
  seller_status   — latest heartbeat (mode, decision, IV-rank, open count)
  seller_trades   — closed trades (live + backtest, tagged by `source`)
  seller_positions— current open spreads
Endpoints:
  GET /seller                 → the page (clean dark UX, polls the API)
  GET /api/seller/state       → live status JSON
  GET /api/seller/trades      → trade ledger JSON (?source=live|sim_2026|all)
  GET /api/seller/metrics     → summary KPIs JSON (?source=...)
Self-contained mongo connection; mongo-independent of the rest of the app.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse

try:
    from pymongo import MongoClient
except Exception:  # pragma: no cover
    MongoClient = None  # type: ignore


class SellerRouter:
    def __init__(self) -> None:
        self._db = None
        if MongoClient is not None:
            try:
                self._db = MongoClient(
                    os.getenv("MONGO_HOST", "mongo"),
                    int(os.getenv("MONGO_PORT", "27017") or 27017),
                    serverSelectionTimeoutMS=1500,
                )[os.getenv("MONGO_DB", "trading_ai")]
            except Exception:
                self._db = None
        router = APIRouter(tags=["seller"])
        router.add_api_route("/seller", self.page, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route("/api/seller/state", self.state, methods=["GET"])
        router.add_api_route("/api/seller/trades", self.trades, methods=["GET"])
        router.add_api_route("/api/seller/metrics", self.metrics, methods=["GET"])
        self.router = router

    # ── data ──────────────────────────────────────────────────────────────
    def _trade_docs(self, source: str) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        q: dict[str, Any] = {} if source in ("all", "") else {"source": source}
        try:
            return list(self._db["seller_trades"].find(q, {"_id": 0}).sort("entry_ts", 1))
        except Exception:
            return []

    def state(self) -> JSONResponse:
        doc = None
        if self._db is not None:
            try:
                doc = self._db["seller_status"].find_one({}, {"_id": 0}, sort=[("ts", -1)])
                opens = list(self._db["seller_positions"].find({}, {"_id": 0}))
            except Exception:
                opens = []
        else:
            opens = []
        return JSONResponse({"status": doc or {"mode": "unknown"}, "open_positions": opens})

    def trades(self, source: str = Query("all")) -> JSONResponse:
        return JSONResponse({"source": source, "trades": self._trade_docs(source)})

    def metrics(self, source: str = Query("sim_2026")) -> JSONResponse:
        ts = self._trade_docs(source)
        closed = [t for t in ts if t.get("pnl_rs") is not None]
        n = len(closed)
        wins = sum(1 for t in closed if (t.get("pnl_rs") or 0) > 0)
        total = sum((t.get("pnl_rs") or 0) for t in closed)
        pnls = sorted((t.get("pnl_rs") or 0) for t in closed)
        eq, run = [], 0.0
        for p in pnls:
            run += p
            eq.append(round(run))
        gross_w = sum(p for p in pnls if p > 0)
        gross_l = -sum(p for p in pnls if p < 0)
        return JSONResponse({
            "source": source, "n": n, "win_pct": round(100 * wins / n) if n else 0,
            "total_rs": round(total), "avg_rs": round(total / n) if n else 0,
            "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
            "drop_top3": round(sum(pnls[:-3])) if n > 3 else round(total),
            "equity": eq, "pnls": pnls,
        })

    # ── page ──────────────────────────────────────────────────────────────
    def page(self) -> HTMLResponse:
        return HTMLResponse(_PAGE)


_PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>S3 Seller · Iron Condor</title>
<style>
:root{--bg:#0d0f14;--card:#161a22;--bd:#262c38;--mut:#8b95a7;--fg:#e8ecf3;--g:#21c07a;--r:#f0506e;--ac:#5b8cff}
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,Arial,sans-serif;background:var(--bg);color:var(--fg)}
.wrap{max-width:1100px;margin:0 auto;padding:28px 20px 60px}
.top{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:22px;margin:0;font-weight:650}.pill{font-size:11px;font-weight:700;letter-spacing:.4px;padding:3px 9px;border-radius:999px;background:#13351f;color:#5fe0a0;border:1px solid #1d6b41}
.pill.paper{background:#33260f;color:#ffcf7a;border-color:#7a5a1f}.sub{color:var(--mut);font-size:13px;margin-top:2px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:14px 16px}
.kpi .l{color:var(--mut);font-size:12px}.kpi .v{font-size:24px;font-weight:700;margin-top:4px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:18px;margin:14px 0}
.card h3{margin:0 0 12px;font-size:14px;font-weight:650;color:#cdd6e6}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 10px;font-size:13px;border-bottom:1px solid var(--bd)}
th{color:var(--mut);font-weight:600}.g{color:var(--g)}.r{color:var(--r)}.mut{color:var(--mut)}
.bar{display:flex;align-items:flex-end;gap:3px;height:120px;padding-top:8px}
.bar span{flex:1;border-radius:3px 3px 0 0;min-height:2px}
.live{display:flex;gap:18px;flex-wrap:wrap;align-items:center}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px}
.warn{background:#2a2114;border:1px solid #5e4a22;color:#ffd79a;border-radius:12px;padding:12px 14px;font-size:13px;margin-top:14px}
.seg{display:inline-flex;border:1px solid var(--bd);border-radius:10px;overflow:hidden}
.seg button{background:transparent;color:var(--mut);border:0;padding:6px 12px;font-size:12px;cursor:pointer}
.seg button.on{background:var(--ac);color:#fff}
</style></head><body><div class=wrap>
<div class=top><h1>S3 — Iron Condor Seller</h1><span class=pill id=mode>PAPER</span>
<span class=pill style="background:#1a2030;color:#9cc;border-color:#2c3a55" id=conn>…</span></div>
<div class=sub id=subtitle>premium-selling · volatility-risk-premium · defined risk</div>

<div class=card><h3>Live (current regime)</h3><div class=live id=live><span class=mut>loading…</span></div></div>

<div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">
  <div class=sub>Results dataset:</div>
  <div class=seg id=seg>
    <button data-s=sim_2026 class=on>2026 (live regime)</button>
    <button data-s=live>live paper</button>
  </div>
</div>
<div class=grid id=kpis></div>
<div class=card><h3>Equity / per-trade P&amp;L</h3><div class=bar id=bar></div></div>
<div class=card><h3>Trade ledger</h3><div style="overflow:auto"><table id=tbl><thead><tr>
<th>date</th><th>IV-rank</th><th>structure</th><th>legs (short/long)</th><th>credit</th><th>outcome</th><th>held</th><th>₹ P&amp;L</th></tr></thead><tbody></tbody></table></div></div>

<div class=warn><b>Paper only — real money OFF.</b> Edge validated mostly on 2024 (higher-IV); the 2026 sample is small (current-regime sanity check). Go-live only after a full live paper cycle (T9) clears net-cost in this regime + Dhan 2-leg execution is proven.</div>
<div class=sub style="margin-top:10px">2024 backtest (209 days): 71% win · +₹1,427/trade · +₹228k · drop-top3 +₹208k (robust).</div>
</div>
<script>
let SRC="sim_2026";
const rs=n=>(n>=0?"+":"−")+"₹"+Math.abs(n).toLocaleString("en-IN");
function fmtLegs(legs){if(!legs||!legs.length)return '—';const m={};legs.forEach(l=>{const a=l[0],t=l[1],k=l[2];m[(a=='SELL'?'s':'b')+t]=k});
 const put=m.sPE!=null?`P ${m.sPE}/${m.bPE}`:'';const call=m.sCE!=null?`C ${m.sCE}/${m.bCE}`:'';return [put,call].filter(Boolean).join(' · ')||'—';}
async function j(u){try{const r=await fetch(u);return await r.json()}catch(e){return null}}
function kpi(l,v,c){return `<div class=kpi><div class=l>${l}</div><div class="v ${c||''}">${v}</div></div>`}
async function load(){
  document.getElementById('conn').textContent='connected';
  const st=await j('/api/seller/state');
  if(st){const s=st.status||{};
    document.getElementById('mode').textContent=(s.mode||'paper').toUpperCase();
    document.getElementById('mode').className='pill '+((s.mode||'paper')=='live'?'':'paper');
    const ivr=s.iv_rank==null?'—':s.iv_rank;
    const dec=s.decision||'—';
    const col=(s.fires)?'var(--g)':'var(--mut)';
    const ops=st.open_positions||[];
    document.getElementById('live').innerHTML=
      `<div><span class=dot style="background:${col}"></span><b>${dec}</b></div>`+
      `<div class=mut>IV-rank <b style="color:var(--fg)">${ivr}</b></div>`+
      `<div class=mut>open positions <b style="color:var(--fg)">${ops.length}</b></div>`+
      `<div class=mut>last update ${s.time||s.ts||'—'}</div>`+
      (ops.length?('<div style="flex-basis:100%;margin-top:8px;border-top:1px solid var(--bd);padding-top:8px">'+
        ops.map(o=>`<div class=mut style="font-size:12px">▸ <b style="color:var(--fg)">${o.structure}</b> ${fmtLegs(o.legs)} · credit ${o.credit}</div>`).join('')+'</div>'):'');
  }
  const m=await j('/api/seller/metrics?source='+SRC);
  if(m){document.getElementById('kpis').innerHTML=
    kpi('Total P&L',rs(m.total_rs||0),(m.total_rs>=0?'g':'r'))+
    kpi('Win rate',(m.win_pct||0)+'%')+
    kpi('Trades',m.n||0)+
    kpi('Avg / trade',rs(m.avg_rs||0),(m.avg_rs>=0?'g':'r'))+
    kpi('Profit factor',m.profit_factor==null?'—':m.profit_factor)+
    kpi('Drop-top3',rs(m.drop_top3||0),(m.drop_top3>=0?'g':'r'));
    const eq=m.pnls||[];const mx=Math.max(1,...eq.map(Math.abs));
    document.getElementById('bar').innerHTML=eq.map(p=>`<span title="${rs(p)}" style="height:${Math.max(2,Math.abs(p)/mx*110)}px;background:${p>=0?'var(--g)':'var(--r)'}"></span>`).join('')||'<span class=mut>no trades yet</span>';
  }
  const td=await j('/api/seller/trades?source='+SRC);
  const tb=document.querySelector('#tbl tbody');
  if(td&&td.trades){tb.innerHTML=td.trades.map(t=>{
    const p=t.pnl_rs;const c=p==null?'mut':(p>0?'g':(p<0?'r':'mut'));
    return `<tr><td>${t.day||t.entry_ts||''}</td><td>${t.iv_rank??'—'}</td><td>${t.structure||''}</td><td class=mut style="font-size:12px">${fmtLegs(t.legs)}</td><td>${t.credit??'—'}</td><td>${t.reason||t.exit_reason||''}</td><td>${t.days_held??'—'}</td><td class=${c}>${p==null?'—':rs(p)}</td></tr>`}).join('')||'<tr><td colspan=8 class=mut>no trades yet — sitting out / low IV</td></tr>';}
}
document.getElementById('seg').addEventListener('click',e=>{if(e.target.dataset.s){SRC=e.target.dataset.s;
  [...document.querySelectorAll('#seg button')].forEach(b=>b.classList.toggle('on',b.dataset.s===SRC));load();}});
load();setInterval(load,30000);
</script></body></html>"""
