// Shared core: formatters + WebSocket monitor client.
(function (global) {
  'use strict';

  // ── Formatters ────────────────────────────────────────────────────────────
  function esc(s) { return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
  function fmtNum(v,d) { var n=Number(v); return Number.isFinite(n)?n.toFixed(d==null?2:d):'--'; }
  function fmtSigned(v,d,suf) { var n=Number(v); if(!Number.isFinite(n))return '--'; return(n>=0?'+':'')+n.toFixed(d==null?2:d)+(suf||''); }
  function fmtPct(v,d) { return fmtSigned(v*100,d==null?2:d,'%'); }
  function fmtCompact(v) { var n=Number(v); if(!Number.isFinite(n))return '--'; if(Math.abs(n)>=1e6)return(n/1e6).toFixed(1)+'M'; if(Math.abs(n)>=1e3)return(n/1e3).toFixed(1)+'k'; return String(Math.round(n)); }
  var _istFmt = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  function fmtTime(d) {
    if (!(d instanceof Date)) d = new Date(d);
    return _istFmt.format(d);
  }
  function fmtClock(d) {
    if (!(d instanceof Date)) d = new Date(d);
    return _istFmt.format(d) + ' IST';
  }
  function fmtHold(entryMs, exitMs) {
    var s = Math.max(0, Math.round((exitMs - entryMs) / 1000));
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  }

  function strategyContribution(trades) {
    var map = {};
    trades.forEach(function (tr) {
      if (!map[tr.strat]) map[tr.strat] = { label: tr.strat, value: 0, n: 0 };
      map[tr.strat].value += tr.pnlPct;
      map[tr.strat].n += 1;
    });
    return Object.values(map).sort((a, b) => b.value - a.value);
  }

  // ── WebSocket monitor client ──────────────────────────────────────────────
  // getSubscribeMsg(): invoked fresh on each (re)connection — returns the subscribe payload.
  // handlers: { onMessage(msg), onStatus(s) }   s ∈ 'connecting' | 'connected' | 'disconnected'
  // Returns: { send(obj), close() }
  function makeMonitorWS(getSubscribeMsg, handlers) {
    var ws = null, dead = false, retryDelay = 1200, retryTimer = null;

    function wsUrl() {
      return (location.protocol === 'https:' ? 'wss' : 'ws') + '://' + location.host + '/ws/v1/monitor';
    }

    function connect() {
      if (dead) return;
      handlers.onStatus('connecting');
      try { ws = new WebSocket(wsUrl()); } catch (e) { scheduleRetry(); return; }
      ws.onopen = function () {
        retryDelay = 1200;
        handlers.onStatus('connected');
        ws.send(JSON.stringify(getSubscribeMsg()));
      };
      ws.onmessage = function (ev) {
        var msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        handlers.onMessage(msg);
      };
      ws.onerror = function () {};
      ws.onclose = function () {
        if (!dead) { handlers.onStatus('disconnected'); scheduleRetry(); }
      };
    }

    function scheduleRetry() {
      retryTimer = setTimeout(function () {
        retryDelay = Math.min(retryDelay * 1.5, 12000);
        connect();
      }, retryDelay);
    }

    function send(obj) {
      if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(obj)); return true; }
      return false;
    }

    function close() {
      dead = true;
      if (retryTimer) clearTimeout(retryTimer);
      if (ws) { ws.onclose = null; ws.close(); }
    }

    connect();
    return { send: send, close: close };
  }

  async function generateReplayData(tradeDate) {
    const resp = await fetch('/api/historical/replay/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ trade_date: tradeDate }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || 'generate failed');
    }
    return resp.json();
  }

  global.TradingCore = {
    esc, fmtNum, fmtSigned, fmtPct, fmtCompact, fmtTime, fmtClock, fmtHold,
    strategyContribution, makeMonitorWS, generateReplayData,
  };
})(window);
