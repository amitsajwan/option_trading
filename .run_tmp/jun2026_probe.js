// Probe snapshot coverage in runtime mongo (read-only).
const c = db.phase1_market_snapshots;
print("total=" + c.estimatedDocumentCount());
const allDates = c.distinct("trade_date_ist").filter(d => !!d).sort();
print("all_days=" + allDates.length + " first=" + allDates[0] + " last=" + allDates[allDates.length - 1]);
const ds = allDates.filter(d => String(d).startsWith("2026-06"));
print("june_days=" + ds.length);
ds.forEach(d => print(d + " " + c.countDocuments({ trade_date_ist: d })));
const sample = c.findOne({ trade_date_ist: allDates[allDates.length - 1] });
if (sample) {
  print("sample_keys=" + Object.keys(sample).join(","));
  print("payload_type=" + typeof sample.payload);
  if (sample.payload && typeof sample.payload === "object") {
    print("payload_keys=" + Object.keys(sample.payload).join(","));
  }
}
