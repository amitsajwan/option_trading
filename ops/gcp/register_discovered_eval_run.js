// One-off: register a tmux replay run in strategy_eval_runs so the Eval UI picker finds it.
const runId = "9e3789a3-deb5-4dcd-ba8a-9a646a1033bd";
const profile = "trader_master_ml_entry_det_dir_v1";
const existing = db.strategy_eval_runs.findOne({ run_id: runId });
if (existing) {
  // Update trade_count and status now that replay is complete
  const agg2 = db.strategy_positions_historical.aggregate([
    { $match: { run_id: runId, event: "POSITION_CLOSE" } },
    { $group: { _id: null, trade_count: { $sum: 1 }, min_date: { $min: "$trade_date_ist" }, max_date: { $max: "$trade_date_ist" } } },
  ]).toArray()[0];
  db.strategy_eval_runs.updateOne({ run_id: runId }, { $set: {
    status: "completed",
    trade_count: Number(agg2?.trade_count || existing.trade_count),
    date_from: agg2?.min_date || existing.date_from,
    date_to: agg2?.max_date || existing.date_to,
    ended_at: new Date(),
    message: "dir_fix_replay: multi-signal direction + prob>=0.65 — 40 trades Aug-Oct 2024",
  }});
  print("updated existing registration");
  printjson(db.strategy_eval_runs.findOne({ run_id: runId }));
} else {
  const agg = db.strategy_positions_historical.aggregate([
    { $match: { run_id: runId, event: "POSITION_CLOSE" } },
    {
      $group: {
        _id: null,
        min_date: { $min: "$trade_date_ist" },
        max_date: { $max: "$trade_date_ist" },
        trade_count: { $sum: 1 },
      },
    },
  ]).toArray()[0];
  const now = new Date();
  const doc = {
    run_id: runId,
    dataset: "historical",
    status: "completed",
    date_from: agg?.min_date || "2024-08-01",
    date_to: agg?.max_date || "2024-10-31",
    speed: 0,
    base_path: "",
    risk_config: {},
    submitted_at: now,
    started_at: now,
    ended_at: now,
    progress_pct: 100,
    message: "dir_fix_replay: multi-signal direction + prob>=0.65 (registered for UI)",
    strategy_profile_id: profile,
    trade_count: Number(agg?.trade_count || 0),
  };
  db.strategy_eval_runs.insertOne(doc);
  print("registered");
  printjson(doc);
}
