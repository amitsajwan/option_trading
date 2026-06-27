var count = db.decision_traces.countDocuments({trade_date_ist: "2026-06-18"});
print("decisions_today=" + count);
