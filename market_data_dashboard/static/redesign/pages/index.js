// Market data command deck.
(function () {
  var PAGE = 'index';
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

  function getSparkFromSession(livePage) {
    var candles = livePage && livePage.chart && livePage.chart.candles;
    if (!candles || !candles.length) return (window.MOCK.instruments || [])[0].spark || [];
    return candles.map(function (row) { return Number(row.c || 0); });
  }

  function serviceChip(ok, label) {
    return '<span class="chip ' + (ok ? 'pos' : 'warn') + '"><span class="dot"></span>' + esc(label) + '</span>';
  }

  function getFallbackData() {
    return {
      source: 'mock',
      health: {
        overall: 'degraded',
        checks: {
          marketDataApi: { ok: true, detail: { latency_ms: 184, status: 'healthy' } },
          redis: { ok: true, detail: { latency_ms: 2, status: 'healthy' } },
          liveStrategyService: { ok: true, detail: { status: 'healthy' } },
          strategyEvalService: { ok: true, detail: { status: 'healthy' } },
        },
      },
      marketDataHealth: {
        status: 'healthy',
        upstream_latency_ms: 184,
      },
      systemMode: {
        mode: 'live',
      },
      livePage: {
        session: { instrument: 'NIFTY 50' },
        kpis: { marketState: 'OPEN' },
        chart: { candles: window.MOCK.candles || [] },
      },
    };
  }

  function renderKpis(data) {
    var health = data.health || {};
    var market = data.marketDataHealth || {};
    var live = data.livePage || {};
    var mode = String((data.systemMode && data.systemMode.mode) || 'unknown').toUpperCase();
    var items = [
      { label: 'MARKET STATE', value: (live.kpis && live.kpis.marketState) || mode || 'UNKNOWN', cls: 'pos', sub: 'Resolved from live session API' },
      { label: 'SYSTEM MODE', value: mode || '--', sub: 'System mode endpoint' },
      { label: 'DASHBOARD', value: String(health.overall || 'unknown').toUpperCase(), cls: health.overall === 'healthy' ? 'pos' : 'warn', sub: 'Operator API health' },
      { label: 'MARKET DATA API', value: String(market.status || 'unknown').toUpperCase(), cls: market.status === 'healthy' ? 'pos' : 'warn', sub: Number.isFinite(Number(market.upstream_latency_ms)) ? (Number(market.upstream_latency_ms).toFixed(0) + 'ms upstream') : 'Upstream status' },
      { label: 'REDIS', value: health.checks && health.checks.redis && health.checks.redis.ok ? 'HEALTHY' : 'WARN', cls: health.checks && health.checks.redis && health.checks.redis.ok ? 'pos' : 'warn', sub: Number.isFinite(Number(health.checks && health.checks.redis && health.checks.redis.detail && health.checks.redis.detail.latency_ms)) ? (Number(health.checks.redis.detail.latency_ms).toFixed(0) + 'ms ping') : 'Dependency check' },
      { label: 'SERVICES', value: ((health.checks && health.checks.liveStrategyService && health.checks.liveStrategyService.ok) ? 'LIVE ' : 'LIVE? ') + ((health.checks && health.checks.strategyEvalService && health.checks.strategyEvalService.ok) ? '/ EVAL' : '/ EVAL?'), cls: 'pos', sub: 'Backend monitor state' },
    ];
    return '<div class="kpi-strip" style="--cols:6">' + items.map(function (item) {
      return '<div class="kpi"><div class="kpi-label">' + item.label + '</div><div class="kpi-value ' + (item.cls || '') + '">' + item.value + '</div><div class="kpi-sub">' + item.sub + '</div></div>';
    }).join('') + '</div>';
  }

  function renderInstrumentCards() {
    var mock = window.MOCK || {};
    var charts = window.QCharts;
    return (mock.instruments || []).map(function (ins, index) {
      return '' +
        '<div class="panel" style="padding:12px">' +
          '<div class="rowx" style="align-items:flex-start">' +
            '<div>' +
              '<div style="font-weight:600; font-size:13px; color:var(--ink)">' + esc(ins.sym) + '</div>' +
              '<div class="mono tiny muted">' + esc(ins.ex) + '</div>' +
            '</div>' +
            '<span class="chip ' + (ins.chg >= 0 ? 'pos' : 'neg') + '">' + fmtSigned(ins.chgPct, 2, '%') + '</span>' +
          '</div>' +
          '<div style="margin-top:8px" class="num bigpx">' + Number(ins.px).toFixed(2) + '</div>' +
          '<div class="tiny muted mono" style="margin-top:2px">Mock watchlist card until per-instrument market-data endpoint exists.</div>' +
          '<div style="margin-top:10px">' + charts.sparkline(ins.spark, { w: 260, h: 38 }) + '</div>' +
          '<div class="rowx tiny muted mono" style="margin-top:8px"><span>slot ' + (index + 1) + '</span><span>mock</span><span>preview</span></div>' +
        '</div>';
    }).join('');
  }

  function renderServiceHealth(data) {
    var checks = data.health && data.health.checks ? data.health.checks : {};
    var market = data.marketDataHealth || {};
    var rows = [
      { name: 'Market-data API', ok: checks.marketDataApi && checks.marketDataApi.ok, value: String(market.status || 'unknown') + (Number.isFinite(Number(market.upstream_latency_ms)) ? (' - ' + Number(market.upstream_latency_ms).toFixed(0) + 'ms') : '') },
      { name: 'Redis', ok: checks.redis && checks.redis.ok, value: checks.redis && checks.redis.detail && checks.redis.detail.status ? checks.redis.detail.status : 'unknown' },
      { name: 'Live strategy monitor', ok: checks.liveStrategyService && checks.liveStrategyService.ok, value: checks.liveStrategyService && checks.liveStrategyService.detail && checks.liveStrategyService.detail.status ? checks.liveStrategyService.detail.status : 'healthy' },
      { name: 'Strategy evaluation', ok: checks.strategyEvalService && checks.strategyEvalService.ok, value: checks.strategyEvalService && checks.strategyEvalService.detail && checks.strategyEvalService.detail.status ? checks.strategyEvalService.detail.status : 'healthy' },
      { name: 'System mode', ok: true, value: String((data.systemMode && data.systemMode.mode) || 'unknown') },
    ];
    return rows.map(function (row) {
      return '<div class="rowx" style="padding:10px 14px; border-bottom:1px solid var(--line-1)"><span>' + esc(row.name) + '</span>' + serviceChip(!!row.ok, row.value) + '</div>';
    }).join('');
  }

  function render(data) {
    var model = data || getFallbackData();
    var charts = window.QCharts;
    var live = model.livePage || {};
    var spark = getSparkFromSession(live);
    var focusPrice = spark.length ? spark[spark.length - 1] : ((window.MOCK.instruments || [])[0] || {}).px || 0;

    return '' +
      '<div id="market-data-view" data-source="' + esc(model.source || 'mock') + '">' +
        '<div class="page-head">' +
          '<div>' +
            '<div class="page-crumbs">Operator - Overview</div>' +
            '<h1 class="page-title">Market Data Command Deck</h1>' +
            '<p class="page-sub">Real service health is wired to the dashboard APIs. Instrument tiles remain mock until a per-instrument backend feed is exposed.</p>' +
          '</div>' +
          '<div class="page-actions">' +
            '<select class="inp" style="height:28px"><option>' + esc((live.session && live.session.instrument) || 'NIFTY 50') + '</option></select>' +
            '<button class="btn">Refresh</button>' +
            '<button class="btn primary">Open live monitor -></button>' +
          '</div>' +
        '</div>' +

        renderKpis(model) +

        '<div class="g-main-side">' +
          '<div>' +
            '<div class="panel-title" style="margin:0 0 8px 2px">Watchlist preview</div>' +
            '<div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px">' + renderInstrumentCards() + '</div>' +
          '</div>' +
          '<div style="display:grid;gap:14px">' +
            '<div class="panel">' +
              '<div class="panel-head">' +
                '<div class="panel-title">Focus - ' + esc((live.session && live.session.instrument) || 'NIFTY 50') + '</div>' +
                '<div class="seg"><button class="btn" aria-pressed="false">1m</button><button class="btn" aria-pressed="true">5m</button><button class="btn" aria-pressed="false">15m</button><button class="btn" aria-pressed="false">1h</button></div>' +
              '</div>' +
              '<div class="panel-body">' +
                '<div class="bigpx" style="font-size:38px">' + Number(focusPrice || 0).toFixed(2) + '</div>' +
                '<div class="row mono tiny" style="margin-top:4px; color:var(--ink-3)">' + esc((live.kpis && live.kpis.marketState) || 'UNKNOWN') + ' - session chart from live strategy API</div>' +
                '<div style="margin-top:14px">' + charts.sparkline(spark, { w: 300, h: 60 }) + '</div>' +
                '<div class="hr"></div>' +
                '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px 18px">' +
                  '<div class="rowx" style="font-size:12px"><span class="muted">System mode</span><span class="mono" style="font-weight:500">' + esc(String((model.systemMode && model.systemMode.mode) || 'unknown')) + '</span></div>' +
                  '<div class="rowx" style="font-size:12px"><span class="muted">Dashboard</span><span class="mono" style="font-weight:500">' + esc(String(model.health && model.health.overall || 'unknown')) + '</span></div>' +
                  '<div class="rowx" style="font-size:12px"><span class="muted">Market API</span><span class="mono" style="font-weight:500">' + esc(String(model.marketDataHealth && model.marketDataHealth.status || 'unknown')) + '</span></div>' +
                  '<div class="rowx" style="font-size:12px"><span class="muted">Redis</span><span class="mono" style="font-weight:500">' + esc(String(model.health && model.health.checks && model.health.checks.redis && model.health.checks.redis.detail && model.health.checks.redis.detail.status || 'unknown')) + '</span></div>' +
                '</div>' +
              '</div>' +
            '</div>' +

            '<div class="panel">' +
              '<div class="panel-head"><div class="panel-title">Service health</div>' + serviceChip(model.health && model.health.overall === 'healthy', String(model.health && model.health.overall || 'unknown')) + '</div>' +
              '<div class="panel-body" style="padding:0">' + renderServiceHealth(model) + '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>';
  }

  async function loadData() {
    if (!window.DashAPI) throw new Error('DashAPI is not loaded');
    var results = await Promise.all([
      window.DashAPI.fetchDashHealth(),
      window.DashAPI.fetchMarketDataHealth(),
      window.DashAPI.fetchSystemMode(),
      window.DashAPI.fetchLiveSession({ limit_votes: 4, limit_trades: 4 }),
    ]);
    return {
      source: 'api',
      health: window.DashAPI.healthToPageData(results[0]),
      marketDataHealth: results[1],
      systemMode: results[2],
      livePage: window.DashAPI.sessionToPageData(results[3]),
    };
  }

  function mount() {
    var page = document.getElementById('page');
    if (!page) return;

    if (cache) {
      var currentView = document.getElementById('market-data-view');
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
        console.error('Failed to hydrate market data page:', err);
      })
      .finally(function () {
        pending = null;
      });
  }

  window.PAGES = window.PAGES || {};
  window.PAGES.index = render;
  window.PAGE_MOUNTS = window.PAGE_MOUNTS || {};
  window.PAGE_MOUNTS.index = mount;
})();
