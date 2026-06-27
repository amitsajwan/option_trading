var db = db.getSiblingDB('trading_ai');
var runIds = [
    "b36ade9f-7919-4434-96e4-1bb20dcd84ea", // 2026-06-17 (v2, but failed then completed)
    "490a9c49-1dff-4a9a-8e2c-9c8c5e317dc6", // 2026-06-16
    "2d8c7655-fe4e-4124-a0cd-f201df14dced", // 2026-06-15
    "65a2eb32-bb9e-43b9-ac20-12c5cc201953", // 2026-06-12
    "54e522a9-0f49-418c-91b6-f81abb3f8c62", // 2026-06-11
    "09d9b5c5-62c3-400e-b82b-e48e464dd906", // 2026-06-10
];
var dates = ["2026-06-17","2026-06-16","2026-06-15","2026-06-12","2026-06-11","2026-06-10"];

print("V2 Runs (vol_gate, prob=0.45, vol_gate_enabled=1):");
print("Date       Signals  Positions  Votes  Entry_Votes  ML_Entry  VOL_Gate");
print("-" * 72);

for (var i = 0; i < runIds.length; i++) {
    var rid = runIds[i];
    var date = dates[i];
    var sig = db.trade_signals_sim.countDocuments({run_id: rid});
    var pos = db.strategy_positions_sim.countDocuments({run_id: rid});
    var votes = db.strategy_votes_sim.countDocuments({run_id: rid});
    var entryVotes = db.strategy_votes_sim.countDocuments({run_id: rid, signal_type: "ENTRY"});
    var mlEntryVotes = db.strategy_votes_sim.countDocuments({run_id: rid, signal_type: "ENTRY", strategy_name: "ML_ENTRY"});
    var volGateVotes = db.strategy_votes_sim.countDocuments({run_id: rid, signal_type: "ENTRY", strategy_name: "VOL_GATE_ENTRY"});
    print(date + "  " + sig + "        " + pos + "         " + votes + "     " + entryVotes + "           " + mlEntryVotes + "         " + volGateVotes);
}
