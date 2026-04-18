// Lightweight SVG charts — hand-rolled so they match design tokens exactly.
// No libraries. Inputs are arrays; outputs are SVG strings.

(function (global) {
  const POS = '#0A8F5C';
  const NEG = '#C23E2F';
  const INK = '#0B0F14';
  const MUTED = '#8A94A1';
  const GRID = 'rgba(11,15,20,0.08)';

  function extent(arr, accessor) {
    let min = Infinity, max = -Infinity;
    for (const d of arr) {
      const v = accessor ? accessor(d) : d;
      if (v < min) min = v;
      if (v > max) max = v;
    }
    return [min, max];
  }

  function scale(domain, range) {
    const [d0, d1] = domain;
    const [r0, r1] = range;
    const s = (d1 - d0) === 0 ? 0 : (r1 - r0) / (d1 - d0);
    return v => r0 + (v - d0) * s;
  }

  // ------- sparkline -------
  function sparkline(values, opts = {}) {
    const w = opts.w || 120;
    const h = opts.h || 22;
    const pad = 1;
    if (!values.length) return `<svg viewBox="0 0 ${w} ${h}"></svg>`;
    const [mn, mx] = extent(values);
    const x = scale([0, values.length - 1], [pad, w - pad]);
    const y = scale([mn, mx], [h - pad, pad]);
    const d = values.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(2)} ${y(v).toFixed(2)}`).join(' ');
    const color = opts.color || (values[values.length - 1] >= values[0] ? POS : NEG);
    const areaColor = opts.fill !== false;
    const area = areaColor
      ? `<path d="${d} L ${(w - pad).toFixed(2)} ${h - pad} L ${pad} ${h - pad} Z" fill="${color}" opacity="0.08"/>`
      : '';
    return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:${h}px">
      ${area}
      <path d="${d}" fill="none" stroke="${color}" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/>
    </svg>`;
  }

  // ------- line chart with axes -------
  function priceChart(candles, markers, opts = {}) {
    const w = opts.w || 1200;
    const h = opts.h || 420;
    const padL = 0, padR = 54, padT = 12, padB = 22;
    if (!candles.length) return `<svg viewBox="0 0 ${w} ${h}"></svg>`;

    const xs = candles.map(c => c.t);
    const ys = candles.map(c => c.c);
    const lows = candles.map(c => c.l);
    const highs = candles.map(c => c.h);
    const [x0, x1] = extent(xs);
    const mn = Math.min(...lows), mx = Math.max(...highs);
    const pad = (mx - mn) * 0.08;
    const x = scale([x0, x1], [padL, w - padR]);
    const y = scale([mn - pad, mx + pad], [h - padB, padT]);

    // gridlines — 5 horizontal
    let gridEls = '';
    const yticks = 5;
    for (let i = 0; i <= yticks; i++) {
      const yv = mn - pad + ((mx + pad) - (mn - pad)) * (i / yticks);
      const yy = y(yv);
      gridEls += `<line x1="${padL}" x2="${w - padR}" y1="${yy}" y2="${yy}" stroke="${GRID}" stroke-width="1"/>`;
      gridEls += `<text x="${w - padR + 6}" y="${yy + 3}" fill="${MUTED}" font-size="10" font-family="JetBrains Mono, monospace">${yv.toFixed(2)}</text>`;
    }

    // x axis ticks
    const xticks = 8;
    for (let i = 0; i <= xticks; i++) {
      const idx = Math.floor((candles.length - 1) * (i / xticks));
      const c = candles[idx];
      const xx = x(c.t);
      gridEls += `<text x="${xx}" y="${h - 6}" fill="${MUTED}" font-size="10" font-family="JetBrains Mono, monospace" text-anchor="middle">${c.label || ''}</text>`;
    }

    // area under close
    const closePath = candles.map((c, i) => `${i === 0 ? 'M' : 'L'}${x(c.t).toFixed(2)} ${y(c.c).toFixed(2)}`).join(' ');
    const areaPath = `${closePath} L ${x(candles[candles.length-1].t).toFixed(2)} ${h - padB} L ${x(candles[0].t).toFixed(2)} ${h - padB} Z`;

    // high/low band (subtle)
    const hiPath = candles.map((c, i) => `${i === 0 ? 'M' : 'L'}${x(c.t).toFixed(2)} ${y(c.h).toFixed(2)}`).join(' ');
    const loPath = candles.map((c, i) => `${i === 0 ? 'M' : 'L'}${x(c.t).toFixed(2)} ${y(c.l).toFixed(2)}`).join(' ');

    // markers (trades / signals)
    let markerEls = '';
    if (markers && markers.length) {
      for (const m of markers) {
        const mx2 = x(m.t);
        const my = y(m.price);
        const color = m.side === 'buy' ? POS : (m.side === 'sell' ? NEG : '#B97405');
        if (m.shape === 'triangle') {
          const sz = 5;
          const dir = m.side === 'buy' ? 1 : -1;
          const path = `M ${mx2} ${my + (dir * sz)} L ${mx2 - sz} ${my - (dir * sz)} L ${mx2 + sz} ${my - (dir * sz)} Z`;
          markerEls += `<path d="${path}" fill="${color}" stroke="${INK}" stroke-width="0.5"/>`;
        } else {
          markerEls += `<line x1="${mx2}" x2="${mx2}" y1="${padT}" y2="${h - padB}" stroke="${color}" stroke-dasharray="2 3" stroke-width="1" opacity="0.6"/>`;
          markerEls += `<circle cx="${mx2}" cy="${my}" r="3" fill="${color}" stroke="white" stroke-width="1"/>`;
        }
        if (m.label) {
          markerEls += `<text x="${mx2 + 6}" y="${my - 6}" fill="${color}" font-size="10" font-family="JetBrains Mono, monospace" font-weight="600">${m.label}</text>`;
        }
      }
    }

    // "now" line
    const lastC = candles[candles.length - 1];
    const lastY = y(lastC.c);
    const lastX = x(lastC.t);

    return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:${h}px">
      ${gridEls}
      <path d="${areaPath}" fill="${INK}" opacity="0.04"/>
      <path d="${hiPath}" fill="none" stroke="${MUTED}" stroke-width="0.8" opacity="0.4"/>
      <path d="${loPath}" fill="none" stroke="${MUTED}" stroke-width="0.8" opacity="0.4"/>
      <path d="${closePath}" fill="none" stroke="${INK}" stroke-width="1.5" stroke-linejoin="round"/>
      ${markerEls}
      <line x1="${padL}" x2="${w - padR}" y1="${lastY}" y2="${lastY}" stroke="${INK}" stroke-dasharray="3 3" stroke-width="1" opacity="0.5"/>
      <rect x="${w - padR + 2}" y="${lastY - 8}" width="48" height="16" fill="${INK}"/>
      <text x="${w - padR + 26}" y="${lastY + 3}" fill="white" font-size="10" font-family="JetBrains Mono, monospace" text-anchor="middle" font-weight="600">${lastC.c.toFixed(2)}</text>
    </svg>`;
  }

  // ------- equity curve chart (two series overlaid) -------
  function equityChart(seriesA, seriesB, opts = {}) {
    const w = opts.w || 800;
    const h = opts.h || 240;
    const padL = 44, padR = 12, padT = 10, padB = 22;
    const all = [...seriesA, ...(seriesB || [])];
    const [mn, mx] = extent(all);
    const pad = (mx - mn) * 0.08 || 1;
    const x = scale([0, seriesA.length - 1], [padL, w - padR]);
    const y = scale([mn - pad, mx + pad], [h - padB, padT]);

    let grid = '';
    for (let i = 0; i <= 4; i++) {
      const yv = (mn - pad) + ((mx + pad) - (mn - pad)) * (i / 4);
      const yy = y(yv);
      grid += `<line x1="${padL}" x2="${w - padR}" y1="${yy}" y2="${yy}" stroke="${GRID}"/>`;
      grid += `<text x="${padL - 6}" y="${yy + 3}" fill="${MUTED}" font-size="10" font-family="JetBrains Mono, monospace" text-anchor="end">${yv.toFixed(1)}%</text>`;
    }
    // zero line
    if (mn < 0 && mx > 0) {
      const y0 = y(0);
      grid += `<line x1="${padL}" x2="${w - padR}" y1="${y0}" y2="${y0}" stroke="${MUTED}" stroke-dasharray="2 3"/>`;
    }

    const pathA = seriesA.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(2)} ${y(v).toFixed(2)}`).join(' ');
    const areaA = `${pathA} L ${(w - padR).toFixed(2)} ${h - padB} L ${padL} ${h - padB} Z`;
    const pathB = seriesB ? seriesB.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(2)} ${y(v).toFixed(2)}`).join(' ') : '';

    return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:${h}px">
      ${grid}
      <path d="${areaA}" fill="${INK}" opacity="0.05"/>
      ${pathB ? `<path d="${pathB}" fill="none" stroke="${MUTED}" stroke-width="1.2" stroke-dasharray="3 3"/>` : ''}
      <path d="${pathA}" fill="none" stroke="${INK}" stroke-width="1.6"/>
    </svg>`;
  }

  // bar chart (pnl per trade / hour histogram)
  function barChart(values, labels, opts = {}) {
    const w = opts.w || 600;
    const h = opts.h || 140;
    const padL = 32, padR = 8, padT = 8, padB = 20;
    const mn = Math.min(0, ...values);
    const mx = Math.max(0, ...values);
    const bw = (w - padL - padR) / values.length;
    const y = scale([mn, mx], [h - padB, padT]);
    const y0 = y(0);

    let bars = '';
    values.forEach((v, i) => {
      const xx = padL + bw * i + 1;
      const yy = y(v);
      const hh = Math.abs(yy - y0);
      const color = v >= 0 ? POS : NEG;
      bars += `<rect x="${xx}" y="${Math.min(yy, y0)}" width="${bw - 2}" height="${hh}" fill="${color}" opacity="0.75"/>`;
      if (labels && labels[i] && i % Math.ceil(values.length / 10) === 0) {
        bars += `<text x="${xx + bw / 2}" y="${h - 6}" fill="${MUTED}" font-size="9" font-family="JetBrains Mono, monospace" text-anchor="middle">${labels[i]}</text>`;
      }
    });

    // axis
    let grid = '';
    for (let i = 0; i <= 3; i++) {
      const yv = mn + (mx - mn) * (i / 3);
      const yy = y(yv);
      grid += `<line x1="${padL}" x2="${w - padR}" y1="${yy}" y2="${yy}" stroke="${GRID}"/>`;
      grid += `<text x="${padL - 4}" y="${yy + 3}" fill="${MUTED}" font-size="9" font-family="JetBrains Mono, monospace" text-anchor="end">${yv.toFixed(1)}</text>`;
    }
    grid += `<line x1="${padL}" x2="${w - padR}" y1="${y0}" y2="${y0}" stroke="${INK}" stroke-width="0.8"/>`;

    return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:${h}px">${grid}${bars}</svg>`;
  }

  // horizontal diverging bars (for strategy returns comparison)
  function hBars(rows, opts = {}) {
    const w = opts.w || 360;
    const rowH = 22;
    const h = rows.length * rowH + 8;
    const labelW = opts.labelW || 100;
    const padR = 40;
    const mx = Math.max(...rows.map(r => Math.abs(r.value)));
    const x0 = labelW + 4;
    const x1 = w - padR;
    const mid = (x0 + x1) / 2;
    const halfW = (x1 - x0) / 2;

    let out = '';
    rows.forEach((r, i) => {
      const yy = 4 + i * rowH;
      const bw = (Math.abs(r.value) / mx) * halfW;
      const color = r.value >= 0 ? POS : NEG;
      const xStart = r.value >= 0 ? mid : mid - bw;
      out += `<text x="${labelW - 4}" y="${yy + 14}" text-anchor="end" fill="${INK}" font-size="11" font-family="JetBrains Mono, monospace">${r.label}</text>`;
      out += `<rect x="${xStart}" y="${yy + 4}" width="${bw}" height="${rowH - 10}" fill="${color}" opacity="0.85"/>`;
      out += `<text x="${r.value >= 0 ? xStart + bw + 4 : xStart - 4}" y="${yy + 14}" text-anchor="${r.value >= 0 ? 'start' : 'end'}" fill="${color}" font-size="10.5" font-family="JetBrains Mono, monospace" font-weight="600">${r.value >= 0 ? '+' : ''}${r.value.toFixed(2)}%</text>`;
    });
    out += `<line x1="${mid}" x2="${mid}" y1="4" y2="${h - 4}" stroke="${INK}" stroke-width="0.8" opacity="0.6"/>`;
    return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:${h}px">${out}</svg>`;
  }

  global.QCharts = { sparkline, priceChart, equityChart, barChart, hBars };
})(window);
