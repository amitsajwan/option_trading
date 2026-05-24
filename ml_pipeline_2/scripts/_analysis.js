db = db.getSiblingDB("trading_ai");
var pos = db.strategy_positions_historical;
var FROM = "2024-08-01", TO = "2024-10-31";

var closed = pos.find({
  trade_date_ist: {$gte: FROM, $lte: TO},
  event: "POSITION_CLOSE"
}).toArray();

var total = closed.length;
if (total === 0) { print("NO CLOSED TRADES FOUND"); quit(); }

var wins=0, losses=0, targets=0, stops=0, timeouts=0;
var total_pnl=0, pe_count=0, ce_count=0, pe_pnl=0, ce_pnl=0;
var worst=999, best=-999, worst_date="", best_date="";
var daily={}, hold_sum=0;

closed.forEach(function(t) {
  var pnl = parseFloat(t.pnl_pct||0);
  total_pnl += pnl;
  if (pnl > 0) wins++; else losses++;
  var reason = String(t.exit_reason||"").toUpperCase();
  if (reason.indexOf("TARGET")>=0) targets++;
  else if (reason.indexOf("STOP")>=0) stops++;
  else timeouts++;
  var dir = String(t.direction||"").toUpperCase();
  if (dir==="PE") { pe_count++; pe_pnl+=pnl; }
  else if (dir==="CE") { ce_count++; ce_pnl+=pnl; }
  if (pnl < worst) { worst=pnl; worst_date=t.trade_date_ist; }
  if (pnl > best)  { best=pnl;  best_date=t.trade_date_ist; }
  hold_sum += parseInt(t.bars_held||0);
  var d = t.trade_date_ist;
  if (!daily[d]) daily[d]={pnl:0,trades:0,wins:0,pe:0,ce:0};
  daily[d].pnl+=pnl; daily[d].trades+=1;
  if(pnl>0) daily[d].wins+=1;
  if(dir==="PE") daily[d].pe+=1; else if(dir==="CE") daily[d].ce+=1;
});

var avg_pnl  = total_pnl/total;
var avg_hold = hold_sum/total;
var win_rate = (wins/total*100).toFixed(1);
var avg_win  = wins>0  ? closed.filter(t=>parseFloat(t.pnl_pct||0)>0).reduce((a,t)=>a+parseFloat(t.pnl_pct||0),0)/wins  : 0;
var avg_loss = losses>0? closed.filter(t=>parseFloat(t.pnl_pct||0)<=0).reduce((a,t)=>a+parseFloat(t.pnl_pct||0),0)/losses: 0;
var rr = avg_loss!==0 ? Math.abs(avg_win/avg_loss).toFixed(2) : "inf";

print("======================================================");
print("  REPLAY ANALYSIS: " + FROM + " to " + TO);
print("======================================================");
print("Total closed trades  : " + total);
print("Win / Loss           : " + wins + " / " + losses + "  (" + win_rate + "% win rate)");
print("Avg win  (pct)       : +" + (avg_win*100).toFixed(2) + "%");
print("Avg loss (pct)       : " + (avg_loss*100).toFixed(2) + "%");
print("Risk:Reward ratio    : " + rr);
print("Total PnL (sum)      : " + (total_pnl*100).toFixed(2) + "%");
print("Avg PnL per trade    : " + (avg_pnl*100).toFixed(2) + "%");
print("Avg hold (bars)      : " + avg_hold.toFixed(1));
print("------------------------------------------------------");
print("Exit breakdown       : TARGET=" + targets + "  STOP=" + stops + "  TIMEOUT=" + timeouts);
print("PE trades            : " + pe_count + "  pnl=" + (pe_pnl*100).toFixed(2) + "%  avg=" + (pe_count>0?(pe_pnl/pe_count*100).toFixed(2):0) + "%");
print("CE trades            : " + ce_count + "  pnl=" + (ce_pnl*100).toFixed(2) + "%  avg=" + (ce_count>0?(ce_pnl/ce_count*100).toFixed(2):0) + "%");
print("Best  trade          : +" + (best*100).toFixed(2) + "% on " + best_date);
print("Worst trade          : "  + (worst*100).toFixed(2) + "% on " + worst_date);
print("======================================================");
print("  DAILY BREAKDOWN");
print("======================================================");
var days = Object.keys(daily).sort();
var day_wins=0, day_losses=0;
days.forEach(function(d) {
  var dd=daily[d];
  var wr=(dd.wins/dd.trades*100).toFixed(0);
  var sign=dd.pnl>=0?"+":"";
  var type="";
  if(dd.pe>0 && dd.ce>0) type="[PE+CE]";
  else if(dd.pe>0) type="[PE]   ";
  else if(dd.ce>0) type="[CE]   ";
  if(dd.pnl>=0) day_wins++; else day_losses++;
  print(d + " " + type + " trades=" + dd.trades + " pnl=" + sign + (dd.pnl*100).toFixed(2) + "% wr=" + wr + "%");
});
print("------------------------------------------------------");
print("Profitable days: " + day_wins + " / " + days.length + "  (" + (day_wins/days.length*100).toFixed(1) + "%)");
