// Shared UI components for all redesign pages.
// Requires: api.js (for DashAPI formatters)
(function (global) {
  'use strict';

  // ── Utilities ─────────────────────────────────────────────────────────────

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

  // ── Engine mode badge ─────────────────────────────────────────────────────
  //
  // mode: raw string from API — 'ML PURE', 'ml_pure', 'DETERMINISTIC', 'HYBRID', etc.
  // detail: optional subtitle string (e.g. run_id or model group)
  //
  // Badge color:
  //   ml_pure       → green  (model inference active)
  //   deterministic → blue-gray (rule-based)
  //   hybrid        → amber (ML + rules)
  //   unknown / other → muted

  function engineModeBadge(mode, detail) {
    var normalized = String(mode || 'unknown').toLowerCase().replace(/[\s_]+/g, '_');
    var label = String(mode || 'UNKNOWN').replace(/_/g, ' ').toUpperCase();

    var palette = {
      ml_pure:       { bg: 'var(--pos)',    fg: '#fff' },
      deterministic: { bg: '#4a5568',       fg: '#fff' },
      hybrid:        { bg: '#b45309',       fg: '#fff' },
    };
    var colors = palette[normalized] || { bg: 'var(--line-2)', fg: 'var(--ink-3)' };

    var badge = '<span style="' +
        'display:inline-flex;align-items:center;gap:5px;' +
        'padding:3px 9px;border-radius:4px;' +
        'font-size:11px;font-weight:700;letter-spacing:0.05em;' +
        'background:' + colors.bg + ';color:' + colors.fg + ';' +
        'font-family:var(--f-mono)">' +
      '<span style="width:6px;height:6px;border-radius:50%;background:currentColor;opacity:0.75"></span>' +
      esc(label) +
    '</span>';

    if (detail) {
      badge += '<span class="muted mono tiny" style="margin-left:6px">' + esc(detail) + '</span>';
    }
    return badge;
  }

  // ── KPI strip ─────────────────────────────────────────────────────────────
  //
  // items: [{ label, value, cls?, sub }]
  //   label — plain text
  //   value — plain text or safe HTML string
  //   cls   — optional CSS class on kpi-value (e.g. 'pos', 'neg')
  //   sub   — plain text or safe HTML string (shown below value)
  // cols: --cols CSS var (default items.length)

  function kpiStrip(items, cols) {
    var colCount = cols || items.length || 6;
    return '<div class="kpi-strip" style="--cols:' + colCount + '">' +
      items.map(function (item) {
        return '<div class="kpi">' +
          '<div class="kpi-label">' + esc(item.label) + '</div>' +
          '<div class="kpi-value ' + (item.cls || '') + '">' + (item.value != null ? item.value : '--') + '</div>' +
          '<div class="kpi-sub">' + (item.sub || '') + '</div>' +
        '</div>';
      }).join('') +
    '</div>';
  }

  // ── Trade table rows ──────────────────────────────────────────────────────
  //
  // Unified 8-column layout (same in live and historical):
  //   Time | Strategy | Dir | Qty* | Entry* | Exit* | PnL | Hold*
  //   (* = mobile-hide)
  //
  // Matching <thead>:
  //   <tr><th>Time</th><th>Strategy</th><th>Dir</th>
  //       <th class="r mobile-hide">Qty</th><th class="r mobile-hide">Entry</th>
  //       <th class="r mobile-hide">Exit</th><th class="r">PnL</th>
  //       <th class="r mobile-hide">Hold</th></tr>

  function tradeTableRows(trades) {
    if (!trades || !trades.length) return emptyRow(8, 'No trades in this session.');
    return trades.map(function (trade) {
      var dirCls = trade.dir === 'LONG' ? 'pos' : (trade.dir === 'SHORT' ? 'neg' : '');
      var pnlCls = Number(trade.pnlPct) >= 0 ? 'pos' : 'neg';
      var entry = Number.isFinite(Number(trade.entry)) ? Number(trade.entry).toFixed(2) : '--';
      var exit  = Number.isFinite(Number(trade.exit))  ? Number(trade.exit).toFixed(2)  : '--';
      return '<tr>' +
        '<td class="muted">' + esc(trade.t) + '</td>' +
        '<td>' + (trade.strat ? esc(trade.strat) : '<span class="muted">--</span>') + '</td>' +
        '<td><span class="chip ' + dirCls + '">' + esc(trade.dir) + '</span></td>' +
        '<td class="r mobile-hide">' + esc(String(trade.qty != null ? trade.qty : '--')) + '</td>' +
        '<td class="r mobile-hide">' + entry + '</td>' +
        '<td class="r mobile-hide">' + exit + '</td>' +
        '<td class="r ' + pnlCls + '">' + fmtSigned(trade.pnlPct != null ? trade.pnlPct : 0, 2, '%') + '</td>' +
        '<td class="r mobile-hide">' + esc(trade.hold || '--') + '</td>' +
      '</tr>';
    }).join('');
  }

  // Canonical thead for the 8-col trade table.
  var TRADE_TABLE_HEADER = '<thead><tr>' +
    '<th>Time</th><th>Strategy</th><th>Dir</th>' +
    '<th class="r mobile-hide">Qty</th>' +
    '<th class="r mobile-hide">Entry</th>' +
    '<th class="r mobile-hide">Exit</th>' +
    '<th class="r">PnL</th>' +
    '<th class="r mobile-hide">Hold</th>' +
  '</tr></thead>';

  // ── Vote / signal table rows ──────────────────────────────────────────────
  //
  // 5-column layout:
  //   Time | Strategy | Dir | Conf | State
  //
  // Matching <thead>:
  //   <tr><th>Time</th><th>Strategy</th><th>Dir</th><th class="r">Conf</th><th>State</th></tr>

  function voteTableRows(votes) {
    if (!votes || !votes.length) return emptyRow(5, 'No signals in this session.');
    return votes.map(function (vote) {
      var dirCls = vote.dir === 'LONG' ? 'pos' : (vote.dir === 'SHORT' ? 'neg' : '');
      var stateHtml = vote.fired
        ? '<span class="chip pos"><span class="dot"></span>fired</span>'
        : ((vote.meta && vote.meta.acted_on === false)
          ? '<span class="chip neg"><span class="dot"></span>rejected</span>'
          : '<span class="chip"><span class="dot"></span>held</span>');
      return '<tr>' +
        '<td class="muted">' + esc(vote.t) + '</td>' +
        '<td>' + esc(vote.strat) + '</td>' +
        '<td><span class="chip ' + dirCls + '">' + esc(vote.dir) + '</span></td>' +
        '<td class="r">' + Number(vote.conf || 0).toFixed(2) + '</td>' +
        '<td>' + stateHtml + '</td>' +
      '</tr>';
    }).join('');
  }

  var VOTE_TABLE_HEADER = '<thead><tr>' +
    '<th>Time</th><th>Strategy</th><th>Dir</th>' +
    '<th class="r">Conf</th><th>State</th>' +
  '</tr></thead>';

  // ── Export ────────────────────────────────────────────────────────────────

  global.QComponents = {
    esc: esc,
    fmtSigned: fmtSigned,
    emptyRow: emptyRow,
    engineModeBadge: engineModeBadge,
    kpiStrip: kpiStrip,
    tradeTableRows: tradeTableRows,
    TRADE_TABLE_HEADER: TRADE_TABLE_HEADER,
    voteTableRows: voteTableRows,
    VOTE_TABLE_HEADER: VOTE_TABLE_HEADER,
  };
})(window);
