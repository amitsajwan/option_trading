var db = db.getSiblingDB('trading_ai');
var runIds = [
    "a7729382-daf9-4666-94a0-fd7e94897618",
    "90398e41-c2a9-4838-b274-b68cafa2fe77",
    "c323b149-30b9-416e-8436-1bdcad2c5d4f",
    "32fca5d4-d648-4848-a74d-9b9f0c44a792",
    "05cd2f2f-f8e6-4ead-9163-f778240a5d43",
    "de13bfcd-6bfa-4878-8488-a69b42213cc2",
    "a49914b2-bfe9-48bd-bb25-9089bfe8212b",
    "ba77702e-cfc0-4e5c-8b2e-61aa0ebae94e",
    "8546e2e9-0238-466a-bc52-35de297d4e0f"
];
var dates = ["2026-06-01","2026-06-02","2026-06-03","2026-06-10","2026-06-11","2026-06-12","2026-06-15","2026-06-16","2026-06-17"];

print("Run ID       Date       Signals  Positions  Votes    Snapshots");
print("-" * 65);

for (var i = 0; i < runIds.length; i++) {
    var rid = runIds[i];
    var date = dates[i];
    var sig = db.trade_signals_sim.countDocuments({run_id: rid});
    var pos = db.strategy_positions_sim.countDocuments({run_id: rid});
    var votes = db.strategy_votes_sim.countDocuments({run_id: rid});
    var snaps = db.phase1_market_snapshots_sim.countDocuments({run_id: rid});
    print(rid.substring(0,8) + "  " + date + "  " + sig + "        " + pos + "         " + votes + "       " + snaps);
}
