"""Seller dashboard — trader-grade three-view page (LIVE / HISTORY / BACKTEST).

Reads the seller daemon's state from mongo (written by strategy_app.seller.runner):
  seller_status   — latest heartbeat (mode, decision, gates, open spreads w/ MTM)
  seller_trades   — closed trades (source: live | paper | paper_legacy)
  seller_positions— current open spreads
  seller_replays  — backtest runs mirrored by strategy_app.seller.replay
Endpoints:
  GET /seller                    → the page
  GET /api/seller/state          → live status JSON
  GET /api/seller/trades         → ledger JSON (?source=live|paper|paper_legacy|all)
  GET /api/seller/metrics        → KPIs JSON (?source=...)
  GET /api/seller/replays        → list of backtest runs
  GET /api/seller/replay?id=...  → one backtest run (summary + trades)
Self-contained mongo connection; independent of the rest of the app.
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
        router.add_api_route("/api/seller/replays", self.replays, methods=["GET"])
        router.add_api_route("/api/seller/replay", self.replay_one, methods=["GET"])
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
        opens: list = []
        if self._db is not None:
            try:
                doc = self._db["seller_status"].find_one({}, {"_id": 0}, sort=[("ts", -1)])
                opens = list(self._db["seller_positions"].find({}, {"_id": 0}))
            except Exception:
                pass
        return JSONResponse({"status": doc or {"mode": "unknown"}, "open_positions": opens})

    def trades(self, source: str = Query("live")) -> JSONResponse:
        return JSONResponse({"source": source, "trades": self._trade_docs(source)})

    @staticmethod
    def _kpis(trades: list[dict]) -> dict:
        closed = [t for t in trades if t.get("pnl_rs") is not None]
        # equity curve must be CHRONOLOGICAL (by entry), never sorted by pnl
        closed.sort(key=lambda t: str(t.get("entry_ts") or t.get("day") or ""))
        pnls = [float(t.get("pnl_rs") or 0) for t in closed]
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        eq, run, peak, maxdd = [], 0.0, 0.0, 0.0
        for p in pnls:
            run += p
            peak = max(peak, run)
            maxdd = min(maxdd, run - peak)
            eq.append(round(run))
        gross_w = sum(p for p in pnls if p > 0)
        gross_l = -sum(p for p in pnls if p < 0)
        return {
            "n": n, "win_pct": round(100 * wins / n) if n else 0,
            "total_rs": round(sum(pnls)), "avg_rs": round(sum(pnls) / n) if n else 0,
            "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
            "max_dd_rs": round(maxdd), "best_rs": round(max(pnls)) if pnls else 0,
            "worst_rs": round(min(pnls)) if pnls else 0,
            "equity": eq, "pnls": pnls,
        }

    def metrics(self, source: str = Query("live")) -> JSONResponse:
        out = self._kpis(self._trade_docs(source))
        out["source"] = source
        return JSONResponse(out)

    def replays(self) -> JSONResponse:
        runs = []
        if self._db is not None:
            try:
                for d in self._db["seller_replays"].find({}, {"trades": 0}).sort("_id", -1):
                    runs.append({"id": d.get("_id"), "label": d.get("label"),
                                 "from": d.get("from"), "to": d.get("to"),
                                 "summary": d.get("summary") or {}})
            except Exception:
                pass
        return JSONResponse({"runs": runs})

    def replay_one(self, id: str = Query(...)) -> JSONResponse:
        doc = None
        if self._db is not None:
            try:
                doc = self._db["seller_replays"].find_one({"_id": id})
            except Exception:
                pass
        if not doc:
            return JSONResponse({"error": "not found"}, status_code=404)
        trades = doc.get("trades") or []
        out = self._kpis(trades)
        out.update({"id": id, "label": doc.get("label"), "from": doc.get("from"),
                    "to": doc.get("to"), "summary": doc.get("summary") or {},
                    "trades": trades})
        return JSONResponse(out)

    # ── page ──────────────────────────────────────────────────────────────
    def page(self) -> HTMLResponse:
        return HTMLResponse(_PAGE)


_PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Seller · Premium Desk</title>
<style>
:root{--bg:#0d0f14;--card:#161a22;--bd:#262c38;--mut:#8b95a7;--fg:#e8ecf3;--g:#21c07a;--r:#f0506e;--ac:#5b8cff;--y:#ffcf7a}
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,Arial,sans-serif;background:var(--bg);color:var(--fg)}
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 60px}
.top{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:20px;margin:0;font-weight:650}
.pill{font-size:11px;font-weight:700;letter-spacing:.4px;padding:3px 9px;border-radius:999px;background:#13351f;color:#5fe0a0;border:1px solid #1d6b41}
.pill.paper{background:#33260f;color:var(--y);border-color:#7a5a1f}
.pill.legacy{background:#1a2030;color:#9aa8c7;border-color:#2c3a55}
.sub{color:var(--mut);font-size:13px;margin-top:2px}
.tabs{display:flex;gap:6px;margin:18px 0 8px}
.tabs button{background:transparent;color:var(--mut);border:1px solid var(--bd);border-radius:10px;padding:8px 20px;font-size:13px;font-weight:600;cursor:pointer}
.tabs button.on{background:var(--ac);color:#fff;border-color:var(--ac)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:14px 0}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:12px 14px}
.kpi .l{color:var(--mut);font-size:11px}.kpi .v{font-size:21px;font-weight:700;margin-top:3px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:16px;margin:12px 0}
.card h3{margin:0 0 10px;font-size:13px;font-weight:650;color:#cdd6e6}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 9px;font-size:12.5px;border-bottom:1px solid var(--bd);white-space:nowrap}
th{color:var(--mut);font-weight:600;font-size:11.5px}.g{color:var(--g)}.r{color:var(--r)}.mut{color:var(--mut)}
.bar{display:flex;align-items:flex-end;gap:2px;height:110px;padding-top:6px}
.bar span{flex:1;border-radius:2px 2px 0 0;min-height:2px}
.gates{display:flex;gap:8px;flex-wrap:wrap}
.gate{font-size:11px;padding:3px 10px;border-radius:8px;border:1px solid var(--bd);color:var(--mut)}
.gate.ok{border-color:#1d6b41;color:#5fe0a0}.gate.no{border-color:#7a2a3a;color:#f0889c}
.mtm{margin:6px 0;padding:10px;border:1px solid var(--bd);border-radius:10px;font-size:13px}
.mtmbar{height:6px;border-radius:3px;background:#20263255;margin-top:6px;position:relative;overflow:hidden}
.mtmbar i{position:absolute;top:0;bottom:0;border-radius:3px}
.seg{display:inline-flex;border:1px solid var(--bd);border-radius:10px;overflow:hidden}
.seg button{background:transparent;color:var(--mut);border:0;padding:6px 12px;font-size:12px;cursor:pointer}
.seg button.on{background:var(--ac);color:#fff}
select{background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:6px 10px;font-size:13px}
.warn{background:#2a2114;border:1px solid #5e4a22;color:var(--y);border-radius:12px;padding:10px 14px;font-size:12.5px;margin-top:14px}
</style></head><body><div class=wrap>
<div class=top><h1>Seller — Premium Desk</h1><span class=pill id=mode>…</span>
<span class=pill legacy id=conn>…</span></div>
<div class=sub>iron condor / directional credit spreads · defined risk · TP 50% · stop 2× · DTE exit</div>

<div class=tabs id=tabs>
  <button data-t=live class=on>LIVE</button>
  <button data-t=history>HISTORY</button>
  <button data-t=backtest>BACKTEST</button>
</div>

<!-- ═══ LIVE ═══ -->
<div id=tab-live>
  <div class=card><h3>Right now</h3>
    <div style="display:flex;gap:22px;flex-wrap:wrap;align-items:center;margin-bottom:10px" id=livehead></div>
    <div class=gates id=gates></div>
  </div>
  <div class=card><h3>Open spreads — mark to market</h3><div id=mtm><span class=mut>none</span></div></div>
  <div class=card><h3>Today</h3><div id=today><span class=mut>no trades today</span></div></div>
</div>

<!-- ═══ HISTORY ═══ -->
<div id=tab-history style="display:none">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div class=sub>Dataset:</div>
    <div class=seg id=histseg>
      <button data-s=live class=on>LIVE (real)</button>
      <button data-s=paper>paper</button>
      <button data-s=paper_legacy>legacy paper</button>
      <button data-s=all>all</button>
    </div>
  </div>
  <div class=grid id=histkpis></div>
  <div class=card><h3>Equity (chronological)</h3><div class=bar id=histbar></div></div>
  <div class=card><h3>Ledger</h3><div style="overflow:auto"><table><thead><tr>
    <th>mode</th><th>entered</th><th>exited</th><th>structure</th><th>legs P short/long · C short/long</th>
    <th>credit</th><th>IV-rank</th><th>exit</th><th>held</th><th>₹ P&L</th></tr></thead>
    <tbody id=histtbl></tbody></table></div></div>
</div>

<!-- ═══ BACKTEST ═══ -->
<div id=tab-backtest style="display:none">
  <div style="display:flex;gap:12px;align-items:center">
    <div class=sub>Replay run:</div><select id=btsel></select>
    <span class=sub id=btrange></span>
  </div>
  <div class=grid id=btkpis></div>
  <div class=card><h3>Equity (chronological)</h3><div class=bar id=btbar></div></div>
  <div class=card><h3>Exit reasons</h3><div id=btreasons class=sub></div></div>
  <div class=card><h3>Ledger</h3><div style="overflow:auto"><table><thead><tr>
    <th>entered</th><th>exited</th><th>structure</th><th>legs</th><th>credit</th>
    <th>IV-rank</th><th>exit</th><th>held</th><th>₹ P&L</th></tr></thead>
    <tbody id=bttbl></tbody></table></div></div>
</div>

<div class=warn><b>Seller v2 phased rollout</b> — Phase 0 safety ✓ · Phase 1 truth-harness (19-month backfill via Dhan expired-options API) · Phase 2 edge layers · paper · live ramp. All backtests run the REAL SellerRunner code path.</div>
</div>
<script>
const rs=n=>(n>=0?"+":"−")+"₹"+Math.abs(Math.round(n)).toLocaleString("en-IN");
const dt=s=>{if(!s)return "—";const t=String(s);return t.length>15?t.slice(0,10)+" "+t.slice(11,16):t.slice(0,10)};
const num=(v,d=1)=>v==null?"—":(+v).toFixed(d);
function fmtLegs(legs){if(!legs||!legs.length)return '—';const m={};legs.forEach(l=>{m[(l[0]=='SELL'?'s':'b')+l[1]]=l[2]});
 const put=m.sPE!=null?`P ${m.sPE}/${m.bPE??'—'}`:'';const call=m.sCE!=null?`C ${m.sCE}/${m.bCE??'—'}`:'';
 return [put,call].filter(Boolean).join(' · ')||'—';}
async function j(u){try{const r=await fetch(u);return await r.json()}catch(e){return null}}
const kpi=(l,v,c)=>`<div class=kpi><div class=l>${l}</div><div class="v ${c||''}">${v}</div></div>`;
const chip=s=>s=='live'?'<span class=pill style="font-size:10px;padding:1px 6px">LIVE</span>'
 :(s=='paper'?'<span class="pill paper" style="font-size:10px;padding:1px 6px">PAPER</span>'
 :`<span class="pill legacy" style="font-size:10px;padding:1px 6px">${s||'—'}</span>`);
function bars(el,pnls){const mx=Math.max(1,...pnls.map(Math.abs));
 el.innerHTML=pnls.map(p=>`<span title="${rs(p)}" style="height:${Math.max(2,Math.abs(p)/mx*100)}px;background:${p>=0?'var(--g)':'var(--r)'}"></span>`).join('')||'<span class=mut>no trades</span>';}
function kpiRow(el,m){el.innerHTML=
 kpi('Total P&L',rs(m.total_rs||0),(m.total_rs>=0?'g':'r'))+kpi('Trades',m.n||0)+
 kpi('Win rate',(m.win_pct||0)+'%')+kpi('Avg / trade',rs(m.avg_rs||0),(m.avg_rs>=0?'g':'r'))+
 kpi('Profit factor',m.profit_factor==null?'—':m.profit_factor)+
 kpi('Max drawdown',rs(m.max_dd_rs||0),'r')+kpi('Worst trade',rs(m.worst_rs||0),'r');}
function legDetail(t){
 const legs=t.legs||[];const qty=t.qty||30;
 if(!legs.length||legs[0].length<4)return '<span class=mut>no leg fills recorded (older trade)</span>';
 const rows=legs.map(l=>{
  const[a,ot,k,ein,eout]=l;
  const pnl=(eout!=null&&ein!=null)?((a=='SELL'?(ein-eout):(eout-ein))*qty):null;
  return `<tr><td>${a}</td><td>${ot} ${k}</td><td>${num(ein,2)}</td><td>${eout==null?'—':num(eout,2)}</td>`+
   `<td class=${pnl==null?'mut':(pnl>=0?'g':'r')}>${pnl==null?'—':rs(pnl)}</td></tr>`}).join('');
 const netline=`credit ${num(t.credit)} → exit ${t.exit_value==null?'—':num(t.exit_value)} pts · qty ${qty}`;
 return `<div class=mut style="margin:4px 0">${netline}</div>`+
  `<table style="width:auto"><thead><tr><th>leg</th><th>contract</th><th>entry fill</th><th>exit fill</th><th>leg P&L</th></tr></thead><tbody>${rows}</tbody></table>`;}
function ledgerRows(trades,withMode){return trades.map((t,i)=>{
 const p=t.pnl_rs;const c=p==null?'mut':(p>0?'g':(p<0?'r':'mut'));
 // market date+time (day/exit_day + hhmm are simulated-time in replays); wall-clock ts only as fallback
 const entered=t.day?(dt(t.day).slice(0,10)+(t.entry_hhmm?' '+t.entry_hhmm:'')):dt(t.entry_ts);
 const exited=t.exit_day?(dt(t.exit_day).slice(0,10)+(t.exit_hhmm?' '+t.exit_hhmm:'')):dt(t.exit_ts);
 const rid=`lg${withMode?'m':'b'}${i}`;
 return `<tr style="cursor:pointer" onclick="const e=document.getElementById('${rid}');e.style.display=e.style.display=='none'?'':'none'">`+
  `${withMode?`<td>${chip(t.source)}</td>`:''}<td>${entered}</td><td>${exited}</td>`+
  `<td>${t.structure||''} <span class=mut>▾</span></td><td class=mut>${fmtLegs(t.legs)}</td><td>${num(t.credit)}</td>`+
  `<td>${t.iv_rank==null?'—':num(t.iv_rank,0)}</td><td>${t.reason||''}</td><td>${t.days_held??'—'}d</td>`+
  `<td class=${c}>${p==null?'—':rs(p)}</td></tr>`+
  `<tr id=${rid} style="display:none"><td colspan=${withMode?10:9} style="background:#10141c;padding:10px 16px">${legDetail(t)}</td></tr>`;
 }).join('')||`<tr><td colspan=10 class=mut>no trades</td></tr>`;}

// ── LIVE tab ──
async function loadLive(){
 const st=await j('/api/seller/state');if(!st)return;
 const s=st.status||{};
 document.getElementById('conn').textContent='connected';
 document.getElementById('mode').textContent=(s.mode||'?').toUpperCase();
 document.getElementById('mode').className='pill '+((s.mode||'paper')=='live'?'':'paper');
 const col=s.fires?'var(--g)':'var(--mut)';
 document.getElementById('livehead').innerHTML=
  `<div style="font-size:16px"><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${col};margin-right:7px"></span><b>${s.decision||'—'}</b></div>`+
  `<div class=mut>${(s.reason||'').slice(0,60)}</div>`+
  `<div class=mut>day P&L <b style="color:var(--fg)">${s.daily_pnl_rs!=null?rs(s.daily_pnl_rs):'—'}</b></div>`+
  `<div class=mut>updated ${s.time||'—'}</div>`;
 const g=(name,ok,detail)=>`<span class="gate ${ok===null?'':(ok?'ok':'no')}">${name}${detail?` · ${detail}`:''}</span>`;
 document.getElementById('gates').innerHTML=
  g('IV-rank',s.iv_rank==null?null:s.iv_rank>=30,s.iv_rank==null?'n/a':num(s.iv_rank,0))+
  g('entry window 10:00–14:00',null)+
  g('entry',!s.entry_latched,s.entry_latched?'LATCHED':(s.entered_today?'done today':'armed'))+
  g('fails today',s.entry_fail_count?false:true,String(s.entry_fail_count||0))+
  g('open slots',(s.open_count||0)<1,`${s.open_count||0}/1`);
 const sps=s.spreads||[];
 document.getElementById('mtm').innerHTML=!sps.length?'<span class=mut>none — flat</span>':sps.map(o=>{
  const v=o.value,has=v!=null;
  const range=o.stop_at-o.tp_at;
  const pos=has?Math.min(100,Math.max(0,100*(o.stop_at-v)/range)):null; // 100 = at TP, 0 = at stop
  const pnl=has?(o.credit-v)*30:null;
  return `<div class=mtm><b>${o.structure}</b> <span class=mut>${fmtLegs(o.legs)} · exp ${o.expiry||''} · entered ${o.trade_date||''}</span><br>`+
   `credit ${num(o.credit)} → now <b class=${has&&v<=o.credit?'g':'r'}>${has?num(v):'—'}</b>`+
   (has?` · unrealised <b class=${pnl>=0?'g':'r'}>${rs(pnl)}</b> <span class=mut>(TP ${num(o.tp_at)} · stop ${num(o.stop_at)})</span>`:'')+
   (has?`<div class=mtmbar><i style="left:0;width:${pos}%;background:${pnl>=0?'var(--g)':'var(--r)'}"></i></div>`:'')+
   `</div>`;}).join('');
 const td=await j('/api/seller/trades?source=all');
 if(td&&td.trades){const today=new Date().toISOString().slice(0,10);
  const t2=td.trades.filter(t=>String(t.exit_ts||'').slice(0,10)==today);
  document.getElementById('today').innerHTML=t2.length?
   `<table><tbody>${ledgerRows(t2,true)}</tbody></table>`:'<span class=mut>no trades today</span>';}
}
// ── HISTORY tab ──
let HSRC='live';
async function loadHist(){
 const m=await j('/api/seller/metrics?source='+HSRC);
 if(m){kpiRow(document.getElementById('histkpis'),m);bars(document.getElementById('histbar'),m.pnls||[]);}
 const td=await j('/api/seller/trades?source='+HSRC);
 if(td)document.getElementById('histtbl').innerHTML=ledgerRows(td.trades,true);
}
document.getElementById('histseg').addEventListener('click',e=>{if(e.target.dataset.s){HSRC=e.target.dataset.s;
 [...document.querySelectorAll('#histseg button')].forEach(b=>b.classList.toggle('on',b.dataset.s===HSRC));loadHist();}});
// ── BACKTEST tab ──
async function loadBtList(){
 const r=await j('/api/seller/replays');const sel=document.getElementById('btsel');
 sel.innerHTML=(r&&r.runs||[]).map(x=>`<option value="${x.id}">${x.label} ${x.from}→${x.to} (${(x.summary||{}).closes??'?'} trades)</option>`).join('')||'<option>none yet</option>';
 if(r&&r.runs&&r.runs.length)loadBt(r.runs[0].id);
}
async function loadBt(id){
 const d=await j('/api/seller/replay?id='+encodeURIComponent(id));if(!d||d.error)return;
 document.getElementById('btrange').textContent=`${d.from} → ${d.to}`;
 kpiRow(document.getElementById('btkpis'),d);bars(document.getElementById('btbar'),d.pnls||[]);
 const reasons=(d.summary||{}).exit_reasons||{};
 document.getElementById('btreasons').textContent=Object.entries(reasons).map(([k,v])=>`${k}: ${v}`).join(' · ')||'—';
 document.getElementById('bttbl').innerHTML=ledgerRows(d.trades||[],false);
}
document.getElementById('btsel').addEventListener('change',e=>loadBt(e.target.value));
// ── tabs ──
document.getElementById('tabs').addEventListener('click',e=>{const t=e.target.dataset.t;if(!t)return;
 [...document.querySelectorAll('#tabs button')].forEach(b=>b.classList.toggle('on',b.dataset.t===t));
 for(const x of ['live','history','backtest'])document.getElementById('tab-'+x).style.display=x===t?'':'none';
 if(t==='history')loadHist();if(t==='backtest')loadBtList();});
loadLive();setInterval(loadLive,30000);
</script></body></html>"""
