// Check June-2026 coverage and document structure
const c = db.phase1_market_snapshots;
print("total=" + c.estimatedDocumentCount());
const allDates = c.distinct("trade_date_ist").filter(d => !!d).sort();
print("all_days=" + allDates.length + " first=" + allDates[0] + " last=" + allDates[allDates.length - 1]);
const juneDays = allDates.filter(d => String(d).startsWith("2026-06"));
print("june_days=" + juneDays.length);
juneDays.forEach(d => print("  " + d + " cnt=" + c.countDocuments({ trade_date_ist: d })));
const sample = c.findOne({ trade_date_ist: juneDays[0] });
if (sample) {
  print("doc_top_keys=" + Object.keys(sample).join(","));
  const pay = sample.payload || {};
  print("payload_keys=" + Object.keys(pay).join(","));
  const snap = pay.snapshot || {};
  print("snapshot_blocks=" + Object.keys(snap).join(","));
}
