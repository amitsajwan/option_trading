// Live strategy monitor.
(function () {
  var PAGE = 'live_strategy';
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

  function fmtCellNumber(value) {
    return Number.isFinite(Number(value)) ? Number(value).toFixed(2) : '--';
  }

  function emptyRow(colspan, text) {
    return '<tr><td colspan="' + colspan + '" class="muted" style="text-align:center;padding:18px">' + esc(text) + '</td></tr>';
  }

  function getFallbackData() {
    var mock = window.MOCK || {};
    return {
      source: 'mock',
      session: {
        date_ist: '2026-04-18',
        instrument: 'NIFTY 50',
      },
      currentRunId: 'mock_live_session',
      overall: {
        trade_count: (mock.closedTrades || []).length,
        win_rate: 0.71,
        profit_factor: 3.4,
      },
      kpis: {
        runStatus: 'RUNNING',
        sessionPnl: '+0.85%',
        sessionPnlCls: 'pos',
        openPositions: 2,
        tradesToday: (mock.closedTrades || []).length,
        engineMode: 'ML PURE',
        marketState: 'OPEN',
        dataFreshness: '0.8s',
        dataFreshnessCls: '',
        winRate: 0.71,
        profitFactor: 3.4,
      },
      votes: mock.votes || [],
      trades: mock.closedTrades || [],
      alerts: mock.alerts || [],
      strategyReturns: mock.strategyReturns || [],
      chart: {
        candles: mock.candles || [],
        markers: (mock.trades || []).concat(mock.signals || []),
      },
    };
  }

  function priceSnapshot(candles) {
    var rows = candles && candles.length ? candles : (window.MOCK && window.MOCK.candles) || [];
    if (!rows.length) {
      return { open: '--', high: '--', low: '--', close: '--', title: '--' };
    }
    var first = rows[0];
    var last = rows[rows.length - 1];
    var high = rows.reduce(function (acc, row) { return Math.max(acc, row.h); }, rows[0].h);
    var low = rows.reduce(function (acc, row) { return Math.min(acc, row.l); }, rows[0].l);
    return {
      open: fmtCellNumber(first.o),
      high: fmtCellNumber(high),
      low: fmtCellNumber(low),
      close: fmtCellNumber(last.c),
      title: (first.label || '--') + ' -> ' + (last.label || '--'),
    };
  }

  function buildKpis(data) {
    var k = data.kpis || {};
    var winRateText = Number.isFinite(Number(k.winRate)) ? (k.winRate * 100).toFixed(1) + '% WR' : 'Summary live';
    var pfText = Number.isFinite(Number(k.profitFactor)) ? 'PF ' + Number(k.profitFactor).toFixed(2) : 'PF --';
    return [
      { label: 'RUN STATUS', value: k.runStatus || 'UNKNOWN', cls: 'pos', sub: '<span class="chip pos"><span class="dot"></span>' + esc(k.marketState || 'UNKNOWN') + '</span>' },
      { label: 'SESSION PNL', value: k.sessionPnl || '--', cls: k.sessionPnlCls || '', sub: 'Net capital return' },
      { label: 'OPEN POSITIONS', value: String(k.openPositions != null ? k.openPositions : '--'), sub: 'Active position ledger' },
      { label: 'TRADES TODAY', value: String(k.tradesToday != null ? k.tradesToday : '--'), sub: winRateText + ' - ' + pfText },
      { label: 'ENGINE MODE', value: k.engineMode || 'UNKNOWN', sub: 'Current decision lane' },
      { label: 'DATA FRESHNESS', value: k.dataFreshness || '--', cls: k.dataFreshnessCls || '', sub: 'Oldest session feed age' },
    ];
  }

  function renderKpiStrip(data) {
    return '<div class="kpi-strip" style="--cols:6">' + buildKpis(data).map(function (item) {
      return '' +
        '<div class="kpi">' +
          '<div class="kpi-label">' + item.label + '</div>' +
          '<div class="kpi-value ' + (item.cls || '') + '">' + item.value + '</div>' +
          '<div class="kpi-sub">' + item.sub + '</div>' +
        '</div>';
    }).join('') + '</div>';
  }

  function renderTrades(trades) {
    if (!trades.length) return emptyRow(8, 'No closed trades in the current session.');
    return trades.map(function (trade) {
      var dirCls = trade.dir === 'LONG' ? 'pos' : (trade.dir === 'SHORT' ? 'neg' : '');
      var pnlCls = Number(trade.pnlPct) >= 0 ? 'pos' : 'neg';
      return '' +
        '<tr>' +
          '<td class="muted">' + esc(trade.t) + '</td>' +
          '<td>' + esc(trade.strat) + '</td>' +
          '<td><span class="chip ' + dirCls + '">' + esc(trade.dir) + '</span></td>' +
          '<td class="r mobile-hide">' + esc(trade.qty) + '</td>' +
          '<td class="r mobile-hide">' + fmtCellNumber(trade.entry) + '</td>' +
          '<td class="r mobile-hide">' + fmtCellNumber(trade.exit) + '</td>' +
          '<td class="r ' + pnlCls + '">' + fmtSigned(trade.pnlPct || 0, 2, '%') + '</td>' +
          '<td class="r muted mobile-hide">' + esc(trade.hold || '--') + '</td>' +
        '</tr>';
    }).join('');
  }

  function renderVotes(votes) {
    if (!votes.length) return emptyRow(5, 'No recent strategy votes.');
    return votes.map(function (vote) {
      var dirCls = vote.dir === 'LONG' ? 'pos' : (vote.dir === 'SHORT' ? 'neg' : '');
      return '' +
        '<tr>' +
          '<td class="muted">' + esc(vote.t) + '</td>' +
          '<td>' + esc(vote.strat) + '</td>' +
          '<td><span class="chip ' + dirCls + '">' + esc(vote.dir) + '</span></td>' +
          '<td class="r">' + Number(vote.conf || 0).toFixed(2) + '</td>' +
          '<td>' + (vote.fired
            ? '<span class="chip pos"><span class="dot"></span>fired</span>'
            : '<span class="chip"><span class="dot"></span>held</span>') + '</td>' +
        '</tr>';
    }).join('');
  }

  function renderAlerts(alerts) {
    if (!alerts.length) {
      return '<div class="alert info"><div class="bar"></div><div class="msg">No active alerts.</div><div class="t">--</div></div>';
    }
    return alerts.map(function (alert) {
      return '' +
        '<div class="alert ' + esc(alert.level || 'info') + '">' +
          '<div class="bar"></div>' +
          '<div class="msg">' + (alert.msg || '') + '</div>' +
          '<div class="t">' + esc(alert.t || '--') + '</div>' +
        '</div>';
    }).join('');
  }

  function renderWatchlist() {
    var mock = window.MOCK || {};
    var charts = window.QCharts;
    return (mock.instruments || []).slice(0, 6).map(function (ins, index) {
      return '' +
        '<div class="watch-row ' + (index === 0 ? 'active' : '') + '">' +
          '<div>' +
            '<div class="watch-sym">' + esc(ins.sym) + '</div>' +
            '<div class="watch-ex">' + esc(ins.ex) + '</div>' +
          '</div>' +
          '<div style="width:90px">' + charts.sparkline(ins.spark, { w: 90, h: 22 }) + '</div>' +
          '<div>' +
            '<div class="watch-px">' + Number(ins.px).toFixed(2) + '</div>' +
            '<div class="watch-chg num" style="color:' + (ins.chg >= 0 ? 'var(--pos)' : 'var(--neg)') + '">' + fmtSigned(ins.chgPct, 2, '%') + '</div>' +
          '</div>' +
        '</div>';
    }).join('');
  }

  function renderDepthLadder() {
    var mock = window.MOCK || {};
    var bids = (mock.depthBid || []).slice().reverse();
    var asks = mock.depthAsk || [];
    return '' +
      '<div class="ladder">' +
        '<div class="col bid">' +
          bids.map(function (row) {
            var pct = (row.sz / 3500) * 100;
            return '<div class="row bid"><div class="bar" style="width:' + pct + '%"></div><div class="sz">' + row.sz.toLocaleString() + '</div><div class="px r" style="text-align:right">' + Number(row.px).toFixed(2) + '</div></div>';
          }).join('') +
        '</div>' +
        '<div class="col ask">' +
          asks.map(function (row) {
            var pct = (row.sz / 3500) * 100;
            return '<div class="row ask"><div class="bar" style="width:' + pct + '%"></div><div class="px">' + Number(row.px).toFixed(2) + '</div><div class="sz">' + row.sz.toLocaleString() + '</div></div>';
          }).join('') +
        '</div>' +
      '</div>' +
      '<div class="spread">Static ladder placeholder until live order-book endpoint exists.</div>';
  }

  function render(data) {
    var model = data || getFallbackData();
    var charts = window.QCharts;
    var snap = priceSnapshot(model.chart && model.chart.candles);
    var subtitle = 'Session <span class="mono">' + esc(model.session.date_ist || '--') + '</span>' +
      ' - Active run <span class="mono">' + esc(model.currentRunId || 'session_default') + '</span>' +
      ' - Instrument <span class="mono">' + esc(model.session.instrument || '--') + '</span>.';
    var strategyBars = (model.strategyReturns && model.strategyReturns.length)
      ? charts.hBars(model.strategyReturns, { w: 600, labelW: 160 })
      : '<div class="muted">No strategy contribution rows.</div>';

    return '' +
      '<div id="live-strategy-view" data-source="' + esc(model.source || 'mock') + '">' +
        '<div class="page-head">' +
          '<div>' +
            '<div class="page-crumbs">Operator - Live</div>' +
            '<h1 class="page-title">Live Strategy Monitor</h1>' +
            '<p class="page-sub">' + subtitle + '</p>' +
          '</div>' +
          '<div class="page-actions">' +
            '<div class="seg" role="tablist" aria-label="Timeframe">' +
              '<button class="btn" aria-pressed="false">1m</button>' +
              '<button class="btn" aria-pressed="true">5m</button>' +
              '<button class="btn" aria-pressed="false">15m</button>' +
              '<button class="btn" aria-pressed="false">1h</button>' +
              '<button class="btn" aria-pressed="false">1d</button>' +
            '</div>' +
            '<button class="btn">Pause feed</button>' +
            '<button class="btn primary">Halt all strategies</button>' +
          '</div>' +
        '</div>' +

        '<div class="panel only-mobile" style="padding:14px">' +
          '<div class="rowx">' +
            '<div>' +
              '<div class="muted tiny mono">' + esc(model.session.instrument || 'NIFTY 50') + ' - session</div>' +
              '<div class="bigpx" style="margin-top:4px">' + snap.close + '</div>' +
            '</div>' +
            '<div style="text-align:right">' +
              '<span class="chip ' + ((model.kpis && model.kpis.sessionPnlCls) || '') + '" style="font-size:12px">' + esc((model.kpis && model.kpis.sessionPnl) || '--') + '</span>' +
              '<div class="mono tiny muted" style="margin-top:6px">' + esc((model.kpis && model.kpis.dataFreshness) || '--') + ' freshness</div>' +
            '</div>' +
          '</div>' +
        '</div>' +

        renderKpiStrip(model) +

        '<div class="g-main-side">' +
          '<div class="panel">' +
            '<div class="panel-head">' +
              '<div class="row gap-m">' +
                '<div class="panel-title">' + esc(model.session.instrument || 'Session chart') + ' - ' + esc(snap.title) + '</div>' +
                '<span class="chip pos"><span class="dot"></span>' + esc((model.kpis && model.kpis.marketState) || 'UNKNOWN') + '</span>' +
                '<span class="chip">O ' + snap.open + '</span>' +
                '<span class="chip">H ' + snap.high + '</span>' +
                '<span class="chip">L ' + snap.low + '</span>' +
                '<span class="chip">C <span class="mono">' + snap.close + '</span></span>' +
              '</div>' +
              '<div class="row gap-s">' +
                '<button class="btn sm ghost">Crosshair</button>' +
                '<button class="btn sm ghost">Fit</button>' +
                '<button class="btn sm ghost">Expand</button>' +
              '</div>' +
            '</div>' +
            '<div class="panel-body flush" style="padding:10px 8px 0 8px">' +
              '<div id="live-price-chart" style="width:100%; height:380px; position:relative"></div>' +
              '<div style="display:flex; gap:16px; padding:8px 12px 12px; font-size:11px; color:var(--ink-3); font-family:var(--f-mono)">' +
                '<span><span style="display:inline-block;width:10px;height:2px;background:var(--ink);vertical-align:middle"></span> Session close</span>' +
                '<span style="color:var(--pos)">Entry marker</span>' +
                '<span style="color:var(--neg)">Exit marker</span>' +
                '<span class="muted">Derived from dashboard session API</span>' +
              '</div>' +
            '</div>' +
          '</div>' +

          '<div style="display:grid;gap:14px;min-width:0">' +
            '<div class="panel">' +
              '<div class="panel-head">' +
                '<div class="panel-title">Order Book <span class="count">placeholder</span></div>' +
                '<button class="btn sm ghost">...</button>' +
              '</div>' +
              '<div class="panel-body flush">' + renderDepthLadder() + '</div>' +
            '</div>' +
            '<div class="panel">' +
              '<div class="panel-head">' +
                '<div class="panel-title">Watchlist <span class="count">mock</span></div>' +
                '<button class="btn sm ghost">Edit</button>' +
              '</div>' +
              '<div class="panel-body flush">' + renderWatchlist() + '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +

        '<div class="g2">' +
          '<div class="panel">' +
            '<div class="panel-head">' +
              '<div class="panel-title">Closed Trades - Session <span class="count">' + model.trades.length + '</span></div>' +
              '<div class="row gap-s">' +
                '<span class="chip pos">Win ' + (Number.isFinite(Number(model.kpis.winRate)) ? (model.kpis.winRate * 100).toFixed(1) + '%' : '--') + '</span>' +
                '<span class="chip">PF ' + (Number.isFinite(Number(model.kpis.profitFactor)) ? Number(model.kpis.profitFactor).toFixed(2) : '--') + '</span>' +
                '<button class="btn sm ghost">Export</button>' +
              '</div>' +
            '</div>' +
            '<div class="panel-body flush" style="max-height:260px; overflow:auto">' +
              '<table class="tbl">' +
                '<thead><tr><th>Time</th><th>Strategy</th><th>Dir</th><th class="r mobile-hide">Qty</th><th class="r mobile-hide">Entry</th><th class="r mobile-hide">Exit</th><th class="r">PnL</th><th class="r mobile-hide">Hold</th></tr></thead>' +
                '<tbody>' + renderTrades(model.trades) + '</tbody>' +
              '</table>' +
            '</div>' +
          '</div>' +

          '<div class="panel">' +
            '<div class="panel-head">' +
              '<div class="panel-title">Strategy Votes - Last Ticks <span class="count">' + model.votes.length + '</span></div>' +
              '<div class="row gap-s">' +
                '<span class="chip info"><span class="dot"></span>real session votes</span>' +
                '<button class="btn sm ghost">Explain</button>' +
              '</div>' +
            '</div>' +
            '<div class="panel-body flush" style="max-height:260px; overflow:auto">' +
              '<table class="tbl">' +
                '<thead><tr><th>Time</th><th>Strategy</th><th>Dir</th><th class="r">Conf</th><th>State</th></tr></thead>' +
                '<tbody>' + renderVotes(model.votes) + '</tbody>' +
              '</table>' +
            '</div>' +
          '</div>' +
        '</div>' +

        '<div class="g-side-main">' +
          '<div class="panel">' +
            '<div class="panel-head">' +
              '<div class="panel-title">Alerts <span class="count">' + model.alerts.length + '</span></div>' +
              '<button class="btn sm ghost">Mute</button>' +
            '</div>' +
            '<div class="panel-body flush" style="max-height:320px; overflow:auto">' + renderAlerts(model.alerts) + '</div>' +
          '</div>' +
          '<div class="panel">' +
            '<div class="panel-head">' +
              '<div class="panel-title">Today - Strategy contribution</div>' +
              '<div class="row gap-s">' +
                '<span class="chip">Capital weighted</span>' +
                '<button class="btn sm ghost">Equal</button>' +
              '</div>' +
            '</div>' +
            '<div class="panel-body" style="padding:14px 16px">' + strategyBars + '</div>' +
          '</div>' +
        '</div>' +
      '</div>';
  }

  function mountChart(data) {
    var model = data || getFallbackData();
    var el = document.getElementById('live-price-chart');
    if (!el || !window.InteractiveChart) return;
    if (el.__chart) {
      try { el.__chart.destroy(); } catch (err) {}
    }
    el.__chart = window.InteractiveChart.mount(el, {
      candles: (model.chart && model.chart.candles && model.chart.candles.length) ? model.chart.candles : (window.MOCK.candles || []),
      markers: (model.chart && model.chart.markers) || [],
    });
  }

  async function loadData() {
    if (!window.DashAPI) throw new Error('DashAPI is not loaded');
    var payload = await window.DashAPI.fetchLiveSession({
      limit_votes: 12,
      limit_trades: 12,
      limit_signals: 12,
    });
    return window.DashAPI.sessionToPageData(payload);
  }

  function mount() {
    var page = document.getElementById('page');
    if (!page) return;

    if (cache) {
      var currentView = document.getElementById('live-strategy-view');
      if (!currentView || currentView.getAttribute('data-source') !== 'api') {
        page.innerHTML = render(cache);
      }
      mountChart(cache);
      return;
    }

    mountChart(getFallbackData());
    if (pending) return;

    pending = loadData()
      .then(function (data) {
        cache = data;
        if (window.__opCurrentPage !== PAGE) return;
        var root = document.getElementById('page');
        if (!root) return;
        root.innerHTML = render(data);
        mountChart(data);
      })
      .catch(function (err) {
        console.error('Failed to hydrate live strategy page:', err);
      })
      .finally(function () {
        pending = null;
      });
  }

  window.PAGES = window.PAGES || {};
  window.PAGES.live_strategy = render;
  window.PAGE_MOUNTS = window.PAGE_MOUNTS || {};
  window.PAGE_MOUNTS.live_strategy = mount;
})();
