// Inspect a June 2026 snapshot structure (read-only) to confirm compression features.
const c = db.phase1_market_snapshots;
const doc = c.findOne({ trade_date_ist: "2026-06-16" });
if (!doc) { print("no doc"); quit(); }
const snap = doc.payload && doc.payload.snapshot;
print("snapshot_type=" + typeof snap);
if (snap && typeof snap === "object") {
  print("snapshot_blocks=" + Object.keys(snap).join(","));
  const fd = snap.futures_derived;
  if (fd && typeof fd === "object") {
    print("futures_derived_keys=" + Object.keys(fd).join(","));
  } else {
    print("futures_derived_missing");
  }
  const ve = snap.velocity_enrichment;
  print("velocity_enrichment_keys=" + (ve ? Object.keys(ve).join(",") : "MISSING"));
}
