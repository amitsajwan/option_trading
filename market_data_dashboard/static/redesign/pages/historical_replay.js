// Historical replay monitor.
(function () {
  var PAGE = 'historical_replay';
  var cache = null;
  var pending = null;

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>]/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[ch];
    });
  }

  function fmtSigned(value, digits, suffix) {
    var num = Number(value);
    if (!Number.isFinite(num)) return '--';
    return (num >= 0 ? '+' : '') + num.toFixed(digits == null ? 2 : digits) + (suffix || '');
  }

  function emptyRow(colspan, text) {
    return '<tr><td colspan="' + colspan + '" class="muted" style="text-align:center;padding:18px">' + esc(text) + '</td></tr>';
  }

  function getFallbackData() {
    var mock = window.MOCK || {};
    return {
      source: 'mock',
      session: {
        date_ist: '2026-04-16',
        instrument: 'NIFTY 50',
      },
      currentRunId: 'mock_historical_run',
      replayStatus: {
        status: 'completed',
        current_replay_timestamp: '2026-04-16T15:30:00+05:30',
        events_emitted: 48200,
        speed: 0,
        start_date: '2026-04-16',
        end_date: '2026-04-16',
        collection_counts: {
          votes: 12,
          signals: 8,
          positions: 16,
        },
      },
      latestCompletedRun: {
        run_id: 'mock_historical_run',
      },
      overall: {
        trade_count: (mock.closedTrades || []).length,
        win_rate: 0.71,
        profit_factor: 2.9,
      },
      equity: {
        net_return_pct: 0.0084,
      },
      votes: (mock.votes || []).slice(0, 6),
      trades: (mock.closedTrades || []).slice(0, 8),
      days: mock.sessionDays || [],
      chart: {
        candles: mock.candles || [],
        markers: mock.trades || [],
      },
    };
  }

  function renderKpis(model) {
    var replay = model.replayStatus || {};
    var dash = window.DashAPI;
    var trades = model.overall && model.overall.trade_count != null ? model.overall.trade_count : model.trades.length;
    var netPnl = model.equity && model.equity.net_return_pct != null
      ? dash.fmtPercentFromRatio(model.equity.net_return_pct, 2)
      : '--';
    var netValue = model.equity && model.equity.net_return_pct != null ? Number(model.equity.net_return_pct) : null;
    var netCls = Number.isFinite(netValue) ? (netValue >= 0 ? 'pos' : 'neg') : '';
    var items = [
      { label: 'REPLAY STATE', value: String(replay.status || 'unknown').toUpperCase(), cls: 'pos', sub: 'Run ' + esc(model.currentRunId || '--') },
      { label: 'VIRTUAL TIME', value: dash.fmtTime(replay.current_replay_timestamp || replay.virtual_time_current).slice(0, 8) || '--', sub: esc(replay.current_trade_date || model.session.date_ist || '--') },
      { label: 'EVENTS EMITTED', value: dash.fmtCompactInt(replay.events_emitted), sub: 'votes ' + Number((replay.collection_counts || {}).votes || 0) + ' - signals ' + Number((replay.collection_counts || {}).signals || 0) },
      { label: 'SPEED', value: replay.speed != null ? String(replay.speed) + 'x' : '--', sub: 'Replay controller' },
      { label: 'SESSION TRADES', value: String(trades), sub: Number.isFinite(Number(model.overall && model.overall.win_rate)) ? ((model.overall.win_rate * 100).toFixed(1) + '% WR') : 'Session summary' },
      { label: 'NET PNL', value: netPnl, cls: netCls, sub: 'Capital weighted' },
    ];
    return '<div class="kpi-strip" style="--cols:6">' + items.map(function (item) {
      return '<div class="kpi"><div class="kpi-label">' + item.label + '</div><div class="kpi-value ' + (item.cls || '') + '">' + item.value + '</div><div class="kpi-sub">' + item.sub + '</div></div>';
    }).join('') + '</div>';
  }

  function renderDayChips(days) {
    if (!days.length) return '<div class="muted">No replay days available for the resolved run.</div>';
    return '<div style="display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:8px">' + days.map(function (day, index) {
      var active = index === 0;
      return '' +
        '<button class="panel" style="padding:10px 12px; text-align:left; border:1px solid ' + (active ? 'var(--ink)' : 'var(--line-1)') + '; background:' + (active ? 'var(--ink)' : 'var(--paper)') + '; color:' + (active ? 'var(--paper)' : 'var(--ink)') + '; cursor:pointer">' +
          '<div class="mono tiny" style="opacity:0.7">' + esc(day.d) + '</div>' +
          '<div style="margin-top:4px; font-size:14px; font-weight:600" class="mono">' + fmtSigned(day.net, 2, '%') + '</div>' +
          '<div class="mono tiny" style="opacity:0.7; margin-top:2px">' + Number(day.trades || 0) + ' trades</div>' +
        '</button>';
    }).join('') + '</div>';
  }

  function renderTradeRows(trades) {
    if (!trades.length) return emptyRow(6, 'No replay trades returned.');
    return trades.map(function (trade) {
      var dirCls = trade.dir === 'LONG' ? 'pos' : (trade.dir === 'SHORT' ? 'neg' : '');
      var pnlCls = Number(trade.pnlPct) >= 0 ? 'pos' : 'neg';
      return '<tr>' +
        '<td class="muted">' + esc(trade.t) + '</td>' +
        '<td>' + esc(trade.strat) + '</td>' +
        '<td><span class="chip ' + dirCls + '">' + esc(trade.dir) + '</span></td>' +
        '<td class="r">' + (Number.isFinite(Number(trade.entry)) ? Number(trade.entry).toFixed(2) : '--') + '</td>' +
        '<td class="r">' + (Number.isFinite(Number(trade.exit)) ? Number(trade.exit).toFixed(2) : '--') + '</td>' +
        '<td class="r ' + pnlCls + '">' + fmtSigned(trade.pnlPct || 0, 2, '%') + '</td>' +
      '</tr>';
    }).join('');
  }

  function renderVoteRows(votes) {
    if (!votes.length) return emptyRow(4, 'No replay signals returned.');
    return votes.map(function (vote) {
      var dirCls = vote.dir === 'LONG' ? 'pos' : (vote.dir === 'SHORT' ? 'neg' : '');
      return '<tr>' +
        '<td class="muted">' + esc(vote.t) + '</td>' +
        '<td>' + esc(vote.strat) + '</td>' +
        '<td><span class="chip ' + dirCls + '">' + esc(vote.dir) + '</span></td>' +
        '<td class="r">' + Number(vote.conf || 0).toFixed(2) + '</td>' +
      '</tr>';
    }).join('');
  }

  function render(model) {
    var data = model || getFallbackData();
    var charts = window.QCharts;
    var chartHtml = charts.priceChart(data.chart.candles, data.chart.markers, { w: 1100, h: 340 });
    var replay = data.replayStatus || {};
    var rangeText = esc(replay.start_date || data.session.date_ist || '--') + ' -> ' + esc(replay.end_date || data.session.date_ist || '--');
    var runId = data.currentRunId || (data.latestCompletedRun && data.latestCompletedRun.run_id) || '--';

    return '' +
      '<div id="historical-replay-view" data-source="' + esc(data.source || 'mock') + '">' +
        '<div class="page-head">' +
          '<div>' +
            '<div class="page-crumbs">Operator - Historical</div>' +
            '<h1 class="page-title">Historical Replay Monitor</h1>' +
            '<p class="page-sub">Replay-first operator view using the dashboard historical session API and evaluation summaries.</p>' +
          '</div>' +
          '<div class="page-actions">' +
            '<div class="field" style="flex-direction:row;align-items:center;gap:6px">' +
              '<span class="field-label">From</span>' +
              '<input class="inp" type="date" value="' + esc(replay.start_date || data.session.date_ist || '') + '" style="width:130px">' +
              '<span class="field-label">To</span>' +
              '<input class="inp" type="date" value="' + esc(replay.end_date || data.session.date_ist || '') + '" style="width:130px">' +
              '<span class="field-label">Speed</span>' +
              '<input class="inp" type="number" value="' + esc(replay.speed != null ? replay.speed : 0) + '" style="width:60px">' +
            '</div>' +
            '<button class="btn">Load range</button>' +
            '<button class="btn primary">Run replay</button>' +
          '</div>' +
        '</div>' +

        renderKpis(data) +

        '<div class="panel">' +
          '<div class="panel-head">' +
            '<div class="panel-title">Session days <span class="count">evaluation</span></div>' +
            '<div class="row gap-s"><button class="btn sm ghost">Prev</button><button class="btn sm ghost">Next</button></div>' +
          '</div>' +
          '<div class="panel-body" style="padding:12px">' + renderDayChips(data.days || []) + '</div>' +
        '</div>' +

        '<div class="g-main-side">' +
          '<div class="panel">' +
            '<div class="panel-head">' +
              '<div class="row gap-m">' +
                '<div class="panel-title">' + esc(data.session.instrument || 'Underlying') + ' - replay day ' + esc(data.session.date_ist || '--') + '</div>' +
                '<span class="chip info"><span class="dot"></span>historical</span>' +
                '<span class="chip">Range ' + rangeText + '</span>' +
              '</div>' +
              '<div class="row gap-s"><button class="btn sm ghost">Fit</button><button class="btn sm ghost">Expand</button></div>' +
            '</div>' +
            '<div class="panel-body flush" style="padding:10px 8px 0">' +
              chartHtml +
              '<div style="display:flex;gap:16px;padding:8px 12px 12px;font-size:11px;color:var(--ink-3);font-family:var(--f-mono)">' +
                '<span>' + Number((data.overall && data.overall.trade_count) || data.trades.length || 0) + ' trades</span>' +
                '<span>run <span class="mono">' + esc(runId) + '</span></span>' +
              '</div>' +
            '</div>' +
          '</div>' +

          '<div style="display:grid;gap:14px">' +
            '<div class="panel">' +
              '<div class="panel-head"><div class="panel-title">Run details</div><span class="chip pos"><span class="dot"></span>' + esc(String(replay.status || 'unknown')) + '</span></div>' +
              '<div class="panel-body">' +
                '<div class="kv"><span class="k">Run ID</span><span class="v">' + esc(runId) + '</span></div>' +
                '<div class="kv"><span class="k">Dataset</span><span class="v">historical</span></div>' +
                '<div class="kv"><span class="k">Range</span><span class="v">' + rangeText + '</span></div>' +
                '<div class="kv"><span class="k">Events emitted</span><span class="v">' + esc(window.DashAPI.fmtCompactInt(replay.events_emitted)) + '</span></div>' +
                '<div class="kv"><span class="k">Votes</span><span class="v">' + Number((replay.collection_counts || {}).votes || 0) + '</span></div>' +
                '<div class="kv"><span class="k">Signals</span><span class="v">' + Number((replay.collection_counts || {}).signals || 0) + '</span></div>' +
              '</div>' +
            '</div>' +
            '<div class="panel">' +
              '<div class="panel-head"><div class="panel-title">Compare</div></div>' +
              '<div class="panel-body" style="display:grid;gap:8px">' +
                '<button class="btn" style="justify-content:space-between">Open compare page <span class="muted mono tiny">-></span></button>' +
                '<button class="btn" style="justify-content:space-between">Latest run JSON <span class="muted mono tiny">-></span></button>' +
                '<button class="btn" style="justify-content:space-between">Trades API <span class="muted mono tiny">-></span></button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +

        '<div class="g3">' +
          '<div class="panel" style="grid-column:span 2">' +
            '<div class="panel-head"><div class="panel-title">Trades - session day <span class="count">' + data.trades.length + '</span></div></div>' +
            '<div class="panel-body flush">' +
              '<table class="tbl">' +
                '<thead><tr><th>Time</th><th>Strategy</th><th>Dir</th><th class="r">Entry</th><th class="r">Exit</th><th class="r">PnL</th></tr></thead>' +
                '<tbody>' + renderTradeRows(data.trades) + '</tbody>' +
              '</table>' +
            '</div>' +
          '</div>' +
          '<div class="panel">' +
            '<div class="panel-head"><div class="panel-title">Signals - recent</div></div>' +
            '<div class="panel-body flush">' +
              '<table class="tbl">' +
                '<thead><tr><th>Time</th><th>Strategy</th><th>Dir</th><th class="r">Conf</th></tr></thead>' +
                '<tbody>' + renderVoteRows(data.votes) + '</tbody>' +
              '</table>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>';
  }

  async function loadData() {
    if (!window.DashAPI) throw new Error('DashAPI is not loaded');
    var replayStatus = await window.DashAPI.fetchHistoricalStatus({});
    var sessionParams = {
      date: replayStatus.date_ist,
      run_id: replayStatus.latest_completed_run_id || undefined,
      limit_votes: 12,
      limit_trades: 12,
      limit_signals: 12,
    };
    var rangeFrom = replayStatus.start_date || replayStatus.date_ist;
    var rangeTo = replayStatus.end_date || replayStatus.date_ist;
    var results = await Promise.all([
      window.DashAPI.fetchHistoricalSession(sessionParams),
      window.DashAPI.fetchEvalDays({
        dataset: 'historical',
        date_from: rangeFrom,
        date_to: rangeTo,
        run_id: replayStatus.latest_completed_run_id || undefined,
        page: 1,
        page_size: 8,
      }),
    ]);
    var pageData = window.DashAPI.sessionToPageData(results[0]);
    pageData.replayStatus = replayStatus;
    pageData.days = (results[1].rows || []).map(window.DashAPI.mapSessionDay);
    pageData.source = 'api';
    return pageData;
  }

  function mount() {
    var page = document.getElementById('page');
    if (!page) return;

    if (cache) {
      var currentView = document.getElementById('historical-replay-view');
      if (!currentView || currentView.getAttribute('data-source') !== 'api') {
        page.innerHTML = render(cache);
      }
      return;
    }

    if (pending) return;
    pending = loadData()
      .then(function (data) {
        cache = data;
        if (window.__opCurrentPage !== PAGE) return;
        var root = document.getElementById('page');
        if (root) root.innerHTML = render(data);
      })
      .catch(function (err) {
        console.error('Failed to hydrate historical replay page:', err);
      })
      .finally(function () {
        pending = null;
      });
  }

  window.PAGES = window.PAGES || {};
  window.PAGES.historical_replay = render;
  window.PAGE_MOUNTS = window.PAGE_MOUNTS || {};
  window.PAGE_MOUNTS.historical_replay = mount;
})();
