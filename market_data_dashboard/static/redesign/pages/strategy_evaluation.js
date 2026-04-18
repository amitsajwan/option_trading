// Strategy evaluation compare page.
(function () {
  var PAGE = 'strategy_evaluation';
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

  function fmtPctPoint(value) {
    var num = Number(value);
    if (!Number.isFinite(num)) return '--';
    return fmtSigned(num * 100, 1, 'pp');
  }

  function fmtRatio(value, digits) {
    var num = Number(value);
    if (!Number.isFinite(num)) return '--';
    return num.toFixed(digits == null ? 2 : digits);
  }

  function topStrategy(rows) {
    if (!rows || !rows.length) return '--';
    return rows.slice().sort(function (a, b) { return b.value - a.value; })[0].label || '--';
  }

  function buildScenario(label, runLabel, pageData) {
    return {
      label: label,
      runLabel: runLabel,
      kpis: pageData.kpis || {},
      overall: pageData.overall || {},
      byStrategy: pageData.byStrategy || [],
      equityCurve: pageData.equityCurve || [],
      tradePnls: pageData.tradePnls || [],
      trades: pageData.trades || [],
    };
  }

  function compareValue(a, b) {
    var left = Number(a);
    var right = Number(b);
    if (!Number.isFinite(left) || !Number.isFinite(right)) return null;
    return left - right;
  }

  function compareDir(delta) {
    if (!Number.isFinite(delta)) return '';
    return delta >= 0 ? 'pos' : 'neg';
  }

  function buildByStrategyRows(currentRows, baselineRows) {
    var map = {};
    currentRows.forEach(function (row) {
      map[row.label] = {
        name: row.label,
        curTr: Number(row.trades || 0),
        curR: Number(row.value || 0),
        baseTr: 0,
        baseR: 0,
      };
    });
    baselineRows.forEach(function (row) {
      var slot = map[row.label] || {
        name: row.label,
        curTr: 0,
        curR: 0,
        baseTr: 0,
        baseR: 0,
      };
      slot.baseTr = Number(row.trades || 0);
      slot.baseR = Number(row.value || 0);
      map[row.label] = slot;
    });
    return Object.keys(map).map(function (key) {
      var row = map[key];
      row.delta = row.curR - row.baseR;
      return row;
    }).sort(function (a, b) { return b.delta - a.delta; });
  }

  function buildVerdict(current, baseline) {
    var returnDelta = compareValue(current.overall.net_return_pct, baseline.overall.net_return_pct);
    var winRateDelta = compareValue(current.overall.win_rate, baseline.overall.win_rate);
    var drawdownDelta = compareValue(current.overall.max_drawdown_pct, baseline.overall.max_drawdown_pct);
    var tradeCount = Number(current.overall.trade_count || 0);
    var gates = [
      { label: 'Net return delta >= +0.25%', value: returnDelta, threshold: 0.0025, format: function (v) { return fmtSigned(v * 100, 2, '%'); } },
      { label: 'Max drawdown delta >= 0.00%', value: drawdownDelta, threshold: 0.0, format: function (v) { return fmtSigned(v * 100, 2, '%'); } },
      { label: 'Win rate delta >= +3pp', value: winRateDelta, threshold: 0.03, format: fmtPctPoint },
      { label: 'Min trades >= 5', value: tradeCount, threshold: 5, format: function (v) { return String(v); } },
    ].map(function (gate) {
      var pass = Number.isFinite(gate.value) && gate.value >= gate.threshold;
      return {
        label: gate.label,
        value: Number.isFinite(gate.value) ? gate.format(gate.value) : '--',
        pass: pass,
      };
    });
    var allPass = gates.every(function (gate) { return gate.pass; });
    return {
      label: allPass ? 'PROMOTE' : 'REVIEW',
      cls: allPass ? 'pos' : 'warn',
      summary: allPass ? 'Current live session beats the baseline on all configured gates.' : 'Current live session does not clear every comparison gate yet.',
      gates: gates,
      returnDelta: returnDelta,
      winRateDelta: winRateDelta,
      drawdownDelta: drawdownDelta,
    };
  }

  function buildCompareRows(current, baseline) {
    return [
      { name: 'Trades', current: String(current.kpis.tradeCount || 0), baseline: String(baseline.kpis.tradeCount || 0), dir: '' },
      { name: 'Net return', current: current.kpis.netReturn || '--', baseline: baseline.kpis.netReturn || '--', dir: compareDir(compareValue(current.overall.net_return_pct, baseline.overall.net_return_pct)) },
      { name: 'Profit factor', current: fmtRatio(current.kpis.profitFactor), baseline: fmtRatio(baseline.kpis.profitFactor), dir: compareDir(compareValue(current.kpis.profitFactor, baseline.kpis.profitFactor)) },
      { name: 'Win rate', current: Number.isFinite(Number(current.kpis.winRate)) ? (current.kpis.winRate * 100).toFixed(1) + '%' : '--', baseline: Number.isFinite(Number(baseline.kpis.winRate)) ? (baseline.kpis.winRate * 100).toFixed(1) + '%' : '--', dir: compareDir(compareValue(current.kpis.winRate, baseline.kpis.winRate)) },
      { name: 'Max drawdown', current: current.kpis.maxDrawdown || '--', baseline: baseline.kpis.maxDrawdown || '--', dir: compareDir(compareValue(current.overall.max_drawdown_pct, baseline.overall.max_drawdown_pct)) },
      { name: 'Sharpe', current: fmtRatio(current.kpis.sharpe), baseline: fmtRatio(baseline.kpis.sharpe), dir: compareDir(compareValue(current.kpis.sharpe, baseline.kpis.sharpe)) },
      { name: 'Avg hold', current: current.kpis.avgHoldBars != null ? String(current.kpis.avgHoldBars) + 'b' : '--', baseline: baseline.kpis.avgHoldBars != null ? String(baseline.kpis.avgHoldBars) + 'b' : '--', dir: '' },
      { name: 'Top strategy', current: topStrategy(current.byStrategy), baseline: topStrategy(baseline.byStrategy), dir: '' },
    ];
  }

  function buildPageData(current, baseline) {
    var verdict = buildVerdict(current, baseline);
    var returnDelta = verdict.returnDelta;
    return {
      source: 'api',
      current: current,
      baseline: baseline,
      verdict: verdict,
      compareRows: buildCompareRows(current, baseline),
      byStrategyRows: buildByStrategyRows(current.byStrategy, baseline.byStrategy),
      kpis: [
        { label: 'CURRENT TRADES', value: String(current.kpis.tradeCount || 0), sub: current.runLabel || current.label },
        { label: 'CURRENT RETURN', value: current.kpis.netReturn || '--', cls: current.kpis.netReturnCls || '', sub: current.label },
        { label: 'BASELINE RETURN', value: baseline.kpis.netReturn || '--', cls: baseline.kpis.netReturnCls || '', sub: baseline.runLabel || baseline.label },
        { label: 'DELTA RETURN', value: Number.isFinite(returnDelta) ? fmtSigned(returnDelta * 100, 2, '%') : '--', cls: Number.isFinite(returnDelta) && returnDelta >= 0 ? 'pos' : 'neg', sub: 'Current - baseline' },
        { label: 'DELTA WIN RATE', value: fmtPctPoint(verdict.winRateDelta), cls: Number.isFinite(verdict.winRateDelta) && verdict.winRateDelta >= 0 ? 'pos' : 'neg', sub: 'Live - baseline' },
        { label: 'VERDICT', value: verdict.label, cls: verdict.cls, sub: verdict.gates.filter(function (gate) { return gate.pass; }).length + '/' + verdict.gates.length + ' gates pass' },
      ],
    };
  }

  function getFallbackData() {
    var mock = window.MOCK || {};
    var current = buildScenario('Current live session', 'live_2026-04-18', {
      kpis: {
        tradeCount: 7,
        netReturn: '+1.62%',
        netReturnCls: 'pos',
        winRate: 0.71,
        maxDrawdown: '-0.22%',
        profitFactor: 3.4,
        sharpe: 2.81,
        avgHoldBars: 16,
      },
      overall: {
        trade_count: 7,
        net_return_pct: 0.0162,
        win_rate: 0.71,
        max_drawdown_pct: -0.0022,
      },
      byStrategy: [
        { label: 'mom_breakout_v3', trades: 3, value: 0.89 },
        { label: 'ml_ensemble_v5', trades: 1, value: 0.64 },
        { label: 'gap_fade_v1', trades: 1, value: 0.22 },
        { label: 'mean_rev_band_v2', trades: 2, value: -0.04 },
      ],
      equityCurve: mock.equityCurrent || [],
      tradePnls: mock.tradePnls || [],
      trades: mock.closedTrades || [],
    });
    var baseline = buildScenario('Latest historical run', 'run_hist_20260411', {
      kpis: {
        tradeCount: 11,
        netReturn: '+0.98%',
        netReturnCls: 'pos',
        winRate: 0.63,
        maxDrawdown: '-0.41%',
        profitFactor: 2.1,
        sharpe: 1.94,
        avgHoldBars: 24,
      },
      overall: {
        trade_count: 11,
        net_return_pct: 0.0098,
        win_rate: 0.63,
        max_drawdown_pct: -0.0041,
      },
      byStrategy: [
        { label: 'mom_breakout_v3', trades: 4, value: 0.34 },
        { label: 'ml_ensemble_v5', trades: 3, value: 0.52 },
        { label: 'gap_fade_v1', trades: 2, value: -0.09 },
        { label: 'mean_rev_band_v2', trades: 1, value: 0.12 },
      ],
      equityCurve: mock.equityBaseline || [],
      tradePnls: mock.tradePnls || [],
      trades: mock.closedTrades || [],
    });
    var pageData = buildPageData(current, baseline);
    pageData.source = 'mock';
    return pageData;
  }

  function render(model) {
    var data = model || getFallbackData();
    var charts = window.QCharts;
    var current = data.current;
    var baseline = data.baseline;
    var verdict = data.verdict;
    var currentEquity = current.equityCurve && current.equityCurve.length ? current.equityCurve : [0];
    var baselineEquity = baseline.equityCurve && baseline.equityCurve.length ? baseline.equityCurve : [0];

    return '' +
      '<div id="strategy-evaluation-view" data-source="' + esc(data.source || 'mock') + '">' +
        '<div class="page-head">' +
          '<div>' +
            '<div class="page-crumbs">Research - Evaluation</div>' +
            '<h1 class="page-title">Strategy Evaluation Compare</h1>' +
            '<p class="page-sub">Compare the current live session against the latest completed historical evaluation run using the dashboard evaluation APIs.</p>' +
          '</div>' +
          '<div class="page-actions">' +
            '<div class="row gap-s">' +
              '<span class="field-label">Current</span>' +
              '<input class="inp" value="' + esc(current.runLabel || current.label) + '" style="width:220px">' +
              '<span class="field-label">Baseline</span>' +
              '<input class="inp" value="' + esc(baseline.runLabel || baseline.label) + '" style="width:220px">' +
            '</div>' +
            '<button class="btn primary">Load comparison</button>' +
          '</div>' +
        '</div>' +

        '<div class="kpi-strip" style="--cols:6">' + data.kpis.map(function (item) {
          return '<div class="kpi"><div class="kpi-label">' + item.label + '</div><div class="kpi-value ' + (item.cls || '') + '">' + item.value + '</div><div class="kpi-sub">' + item.sub + '</div></div>';
        }).join('') + '</div>' +

        '<div class="g-main-side">' +
          '<div class="panel">' +
            '<div class="panel-head">' +
              '<div class="panel-title">Equity curve - current vs baseline</div>' +
              '<div class="row gap-s">' +
                '<span class="chip pos"><span class="dot"></span>current</span>' +
                '<span class="chip"><span class="dot"></span>baseline</span>' +
                '<button class="btn sm ghost">Export</button>' +
              '</div>' +
            '</div>' +
            '<div class="panel-body" style="padding:14px 14px 10px">' +
              charts.equityChart(currentEquity, baselineEquity, { w: 900, h: 260 }) +
            '</div>' +
            '<div class="panel-head" style="border-top:1px solid var(--line-1); border-bottom:0"><div class="panel-title">Per-trade PnL - current run</div></div>' +
            '<div class="panel-body" style="padding:8px 14px 14px">' +
              charts.barChart(current.tradePnls && current.tradePnls.length ? current.tradePnls : [0], null, { w: 900, h: 140 }) +
            '</div>' +
          '</div>' +

          '<div class="panel">' +
            '<div class="panel-head"><div class="panel-title">Verdict</div><span class="chip ' + verdict.cls + '"><span class="dot"></span>' + verdict.label.toLowerCase() + '</span></div>' +
            '<div class="panel-body">' +
              '<div style="font-size:15px; font-weight:600; color:var(--ink); letter-spacing:-0.01em">' + esc(verdict.summary) + '</div>' +
              '<div class="hr"></div>' +
              '<div style="display:grid;gap:10px">' + verdict.gates.map(function (gate) {
                return '<div class="rowx" style="font-size:12px"><span>' + esc(gate.label) + '</span><span class="chip ' + (gate.pass ? 'pos' : 'warn') + '"><span class="dot"></span>' + esc(gate.value) + '</span></div>';
              }).join('') + '</div>' +
              '<div class="hr"></div>' +
              '<div class="muted tiny">Current source: live dataset for the resolved session date. Baseline source: latest completed historical evaluation run.</div>' +
              '<div style="margin-top:14px; display:grid; gap:8px">' +
                '<button class="btn primary">Promote to production</button>' +
                '<button class="btn">Flag for review</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +

        '<div class="g2">' +
          '<div class="panel">' +
            '<div class="panel-head"><div class="panel-title">Headline metrics</div></div>' +
            '<div class="panel-body flush">' +
              '<table class="tbl">' +
                '<thead><tr><th>Metric</th><th class="r">Current</th><th class="r">Baseline</th><th></th></tr></thead>' +
                '<tbody>' + data.compareRows.map(function (row) {
                  return '<tr><td>' + esc(row.name) + '</td><td class="r" style="font-weight:600">' + esc(row.current) + '</td><td class="r muted">' + esc(row.baseline) + '</td><td class="r">' + (row.dir === 'pos' ? '<span class="chip pos">up</span>' : (row.dir === 'neg' ? '<span class="chip neg">down</span>' : '')) + '</td></tr>';
                }).join('') + '</tbody>' +
              '</table>' +
            '</div>' +
          '</div>' +
          '<div class="panel">' +
            '<div class="panel-head"><div class="panel-title">By strategy</div></div>' +
            '<div class="panel-body flush">' +
              '<table class="tbl">' +
                '<thead><tr><th>Strategy</th><th class="r">Cur tr</th><th class="r">Cur PnL</th><th class="r">Base tr</th><th class="r">Base PnL</th><th class="r">Delta</th></tr></thead>' +
                '<tbody>' + data.byStrategyRows.map(function (row) {
                  return '<tr>' +
                    '<td>' + esc(row.name) + '</td>' +
                    '<td class="r">' + row.curTr + '</td>' +
                    '<td class="r ' + (row.curR >= 0 ? 'pos' : 'neg') + '">' + fmtSigned(row.curR, 2, '%') + '</td>' +
                    '<td class="r muted">' + row.baseTr + '</td>' +
                    '<td class="r muted">' + fmtSigned(row.baseR, 2, '%') + '</td>' +
                    '<td class="r ' + (row.delta >= 0 ? 'pos' : 'neg') + '">' + fmtSigned(row.delta, 2, '%') + '</td>' +
                  '</tr>';
                }).join('') + '</tbody>' +
              '</table>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>';
  }

  async function loadData() {
    if (!window.DashAPI) throw new Error('DashAPI is not loaded');
    var initial = await Promise.all([
      window.DashAPI.fetchLiveSession({ limit_votes: 6, limit_trades: 12 }),
      window.DashAPI.fetchLatestEvalRun({ dataset: 'historical', status: 'completed' }),
    ]);
    var liveSession = initial[0];
    var baselineRun = initial[1];
    var liveDate = (liveSession.session && liveSession.session.date_ist) || new Date().toISOString().slice(0, 10);

    var second = await Promise.all([
      window.DashAPI.fetchEvalSummary({ dataset: 'live', date_from: liveDate, date_to: liveDate }),
      window.DashAPI.fetchEvalEquity({ dataset: 'live', date_from: liveDate, date_to: liveDate }),
      window.DashAPI.fetchEvalTrades({ dataset: 'live', date_from: liveDate, date_to: liveDate, page: 1, page_size: 100 }),
      window.DashAPI.fetchEvalSummary({
        dataset: 'historical',
        date_from: baselineRun.date_from,
        date_to: baselineRun.date_to,
        run_id: baselineRun.run_id,
      }),
      window.DashAPI.fetchEvalEquity({
        dataset: 'historical',
        date_from: baselineRun.date_from,
        date_to: baselineRun.date_to,
        run_id: baselineRun.run_id,
      }),
      window.DashAPI.fetchEvalTrades({
        dataset: 'historical',
        date_from: baselineRun.date_from,
        date_to: baselineRun.date_to,
        run_id: baselineRun.run_id,
        page: 1,
        page_size: 100,
      }),
    ]);

    var current = buildScenario(
      'Live ' + liveDate,
      (liveSession.active_run_id || (liveSession.session && liveSession.session.run_id) || ('live_' + liveDate)),
      window.DashAPI.evalToPageData(second[0], second[1], second[2])
    );
    var baseline = buildScenario(
      'Historical ' + baselineRun.date_from + ' -> ' + baselineRun.date_to,
      baselineRun.run_id,
      window.DashAPI.evalToPageData(second[3], second[4], second[5])
    );
    return buildPageData(current, baseline);
  }

  function mount() {
    var page = document.getElementById('page');
    if (!page) return;

    if (cache) {
      var currentView = document.getElementById('strategy-evaluation-view');
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
        console.error('Failed to hydrate strategy evaluation page:', err);
      })
      .finally(function () {
        pending = null;
      });
  }

  window.PAGES = window.PAGES || {};
  window.PAGES.strategy_evaluation = render;
  window.PAGE_MOUNTS = window.PAGE_MOUNTS || {};
  window.PAGE_MOUNTS.strategy_evaluation = mount;
})();
