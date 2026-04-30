"""Live dashboard: aiohttp web server exposing bot state as HTML + JSON."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from typing import TYPE_CHECKING

from aiohttp import web

from market_finder import INTERVAL_5M, get_current_window_start
from signal_engine import gate_vs_market

if TYPE_CHECKING:
    from bot import Bot

logger = logging.getLogger(__name__)


INDEX_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>BTC 5m Bot</title>
<style>
 :root{--bg:#0b0f14;--card:#131a22;--bd:#1e2a36;--fg:#e6edf3;--mut:#8a97a4;--ok:#2fbf71;--bad:#e5484d;--warn:#f5a524;--acc:#5ec4ff}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.4 ui-monospace,Menlo,Consolas,monospace}
 header{padding:10px 16px;border-bottom:1px solid var(--bd);display:flex;gap:16px;align-items:center;flex-wrap:wrap}
 header h1{margin:0;font-size:14px;font-weight:600}
 header .pill{padding:2px 8px;border:1px solid var(--bd);border-radius:4px;background:var(--card);font-size:11px}
 .mode-paper{color:var(--acc)} .mode-live{color:var(--warn)}
 main{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;padding:12px}
 .card{background:var(--card);border:1px solid var(--bd);border-radius:6px;padding:12px}
 .card h2{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut);font-weight:600}
 table{width:100%;border-collapse:collapse;font-size:12px}
 td{padding:3px 0;vertical-align:top}
 td.k{color:var(--mut);white-space:nowrap;padding-right:10px;width:40%}
 td.v{text-align:right;font-variant-numeric:tabular-nums}
 .up{color:var(--ok)} .down{color:var(--bad)} .mut{color:var(--mut)} .warn{color:var(--warn)}
 .bar{height:6px;background:#1e2a36;border-radius:3px;overflow:hidden;margin-top:3px}
 .bar > div{height:100%;background:var(--acc);transition:width .3s}
 .bar.green > div{background:var(--ok)} .bar.red > div{background:var(--bad)}
 .countdown{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
 .trade-row{display:grid;grid-template-columns:60px 70px 60px 1fr 70px;gap:8px;padding:3px 0;font-size:11.5px;border-bottom:1px solid var(--bd)}
 .trade-row:last-child{border:0}
 .trade-row .win{color:var(--ok)} .trade-row .loss{color:var(--bad)} .trade-row .skip{color:var(--mut)}
 #err{color:var(--bad);padding:0 16px;font-size:11px}
 .wide{grid-column:1/-1}
 .feat{display:grid;grid-template-columns:80px 1fr 48px;gap:6px;align-items:center;padding:1px 0;font-size:11.5px}
 .feat .lbl{color:var(--mut)}
 footer{padding:6px 16px;color:var(--mut);border-top:1px solid var(--bd);font-size:11px}
</style></head>
<body>
<header>
 <h1>POLY BTC 5m</h1>
 <span id="mode" class="pill">—</span>
 <span id="clock" class="pill mut">—</span>
 <span id="countdown" class="countdown mut">T-??s</span>
 <span id="bal" class="pill">BAL $—</span>
 <span id="pnl" class="pill">PNL —</span>
 <span id="wr" class="pill">WR —</span>
</header>
<div id="err"></div>
<main>
 <section class="card"><h2>BTC</h2><table id="btc"></table></section>
 <section class="card"><h2>Signal</h2><div id="sig"></div></section>
 <section class="card"><h2>Market</h2><table id="mkt"></table></section>
 <section class="card"><h2>Pending trade</h2><div id="pt"></div></section>
 <section class="card"><h2>Risk</h2><table id="risk"></table></section>
 <section class="card wide"><h2>Recent trades</h2><div id="trades"></div></section>
</main>
<footer>Polls /state every 1s · <span id="last">—</span></footer>
<script>
const $ = s => document.querySelector(s);
const fmtPct = (v, d=4) => (v==null?'—':(v*100).toFixed(d)+'%');
const fmtUsd = v => (v==null?'—':'$'+(+v).toFixed(2));
const fmtNum = (v, d=2) => (v==null?'—':(+v).toFixed(d));
const sign = v => v==null?'':(v>0?'up':(v<0?'down':'mut'));

function row(k, v, cls){ return `<tr><td class="k">${k}</td><td class="v ${cls||''}">${v}</td></tr>`; }
function bar(conf, color){
  const pct = Math.max(0, Math.min(1, conf||0))*100;
  return `<div class="bar ${color||''}"><div style="width:${pct.toFixed(1)}%"></div></div>`;
}
function featLine(lbl, val, align){
  const w = Math.max(0, Math.min(100, Math.abs(align||0)*50+50));
  const cls = align>0?'up':(align<0?'down':'mut');
  const mark = `<div class="bar"><div style="width:${w}%;margin-left:${align>=0?'50%':'0'};transform:${align<0?'translateX(-100%)':''}"></div></div>`;
  return `<div class="feat"><div class="lbl">${lbl}</div>${mark}<div class="v ${cls}">${val}</div></div>`;
}

async function tick(){
  try{
    const r = await fetch('/state', {cache:'no-store'});
    if(!r.ok) throw new Error('HTTP '+r.status);
    const s = await r.json();
    $('#err').textContent='';
    render(s);
    $('#last').textContent = new Date().toLocaleTimeString();
  }catch(e){ $('#err').textContent = 'fetch err: '+e.message; }
}

function render(s){
  // Header
  $('#mode').textContent = 'mode: '+s.mode;
  $('#mode').className = 'pill mode-'+s.mode;
  $('#clock').textContent = new Date(s.now*1000).toLocaleTimeString();
  const sr = Math.max(0, s.seconds_remaining||0);
  $('#countdown').textContent = 'T-'+sr.toFixed(0)+'s';
  $('#countdown').className = 'countdown '+(sr<=10?'down':(sr<=30?'warn':'mut'));
  $('#bal').textContent = 'BAL '+fmtUsd(s.risk.balance);
  const pnl = s.risk.daily_pnl;
  $('#pnl').textContent = 'PNL '+(pnl>=0?'+':'')+fmtNum(pnl);
  $('#pnl').className = 'pill '+(pnl>0?'up':(pnl<0?'down':'mut'));
  $('#wr').textContent = 'WR '+s.risk.daily_wr+'% ('+s.risk.daily_wins+'W/'+s.risk.daily_losses+'L)';

  // BTC
  const b = s.btc;
  $('#btc').innerHTML =
    row('current', fmtUsd(b.current_price)) +
    row('window open', fmtUsd(b.window_open_price)) +
    row('Δ open', fmtPct(b.delta_from_open), sign(b.delta_from_open)) +
    row('Δ 30s vel', fmtPct(b.delta_30s), sign(b.delta_30s)) +
    row('vwap', fmtUsd(b.vwap)) +
    row('vwap div', fmtPct(b.vwap_div), sign(b.vwap_div)) +
    row('momentum', fmtNum(b.momentum,3), sign(b.momentum)) +
    row('vol imb', fmtNum(b.volume_imbalance,3), sign(b.volume_imbalance)) +
    row('book imb', fmtNum(b.book_imbalance,3), sign(b.book_imbalance)) +
    row('realized vol', fmtNum(b.realized_vol*1e4,2)+' bp') +
    row('ticks', b.tick_count);

  // Signal
  const sig = s.signal;
  if(!sig){ $('#sig').innerHTML='<div class="mut">no signal yet</div>'; }
  else{
    const dir = sig.direction;
    const conf = sig.confidence;
    $('#sig').innerHTML =
      `<div style="display:flex;align-items:baseline;gap:8px"><div class="${dir==='UP'?'up':'down'}" style="font-size:20px;font-weight:700">${dir}</div>`+
      `<div style="font-size:18px;font-variant-numeric:tabular-nums">${(conf*100).toFixed(0)}%</div>`+
      `<div class="mut" style="margin-left:auto">sugg $${fmtNum(sig.suggested_price,2)} · EV ${fmtNum(sig.expected_value,4)}</div></div>`+
      bar(conf, dir==='UP'?'green':'red')+
      `<div style="margin-top:10px">`+
      featLine('delta', fmtNum(sig.base_delta_conf,2), sig.base_delta_conf*(b.delta_from_open>0?1:-1))+
      featLine('momentum', fmtNum(sig.mom_align,2), sig.mom_align)+
      featLine('vol imb', fmtNum(sig.vol_align,2), sig.vol_align)+
      featLine('vwap', fmtNum(sig.vwap_align,2), sig.vwap_align)+
      featLine('velocity', fmtNum(sig.vel_align,2), sig.vel_align)+
      featLine('book', fmtNum(sig.book_align,2), sig.book_align)+
      `</div>`+
      `<div class="mut" style="margin-top:6px;font-size:11px">vol_mult=${fmtNum(sig.vol_mult,2)} · time_boost=${fmtNum(sig.time_boost,2)} · T-${sig.seconds_to_close.toFixed(0)}s</div>`;
  }

  // Market
  const m = s.market || {};
  const gate = s.gate;
  const gateCls = gate===true?'up':(gate===false?'down':'mut');
  $('#mkt').innerHTML =
    row('window_ts', m.start_ts||'—') +
    row('question', m.question? ('<span class="mut" style="font-size:11px">'+m.question+'</span>'):'—') +
    row('up token', m.up_token_id?(m.up_token_id.slice(0,10)+'…'):'<span class="warn">missing</span>') +
    row('down token', m.down_token_id?(m.down_token_id.slice(0,10)+'…'):'<span class="warn">missing</span>') +
    row('up ask', fmtNum(m.up_best_ask,2)) +
    row('down ask', fmtNum(m.down_best_ask,2)) +
    row('up bid', fmtNum(m.up_best_bid,2)) +
    row('down bid', fmtNum(m.down_best_bid,2)) +
    row('gate_vs_market', gate==null?'—':(gate?'PASS':'fail'), gateCls);

  // Pending trade
  const t = s.pending_trade;
  if(!t){ $('#pt').innerHTML='<div class="mut">no active order</div>'; }
  else{
    const dcls = t.direction==='UP'?'up':'down';
    $('#pt').innerHTML =
      `<div style="display:flex;gap:12px;align-items:baseline;margin-bottom:6px"><span class="${dcls}" style="font-size:16px;font-weight:700">${t.direction}</span>`+
      `<span>@ $${fmtNum(t.entry_price,2)}</span>`+
      `<span class="mut">size ${fmtUsd(t.size_usdc)}</span>`+
      `<span class="pill" style="margin-left:auto">${t.status}</span></div>`+
      `<table>`+
      row('shares', fmtNum(t.shares,3)) +
      row('conf @ placed', fmtPct(t.confidence,0)) +
      row('btc open', fmtUsd(t.btc_open)) +
      row('stc @ placed', fmtNum(t.seconds_to_close_at_entry,0)+'s') +
      (t.order_id?row('order id', '<span class="mut">'+t.order_id.slice(0,12)+'…</span>'):'') +
      `</table>`;
  }

  // Risk
  const r = s.risk;
  $('#risk').innerHTML =
    row('balance', fmtUsd(r.balance)) +
    row('starting', fmtUsd(r.starting_balance)) +
    row('daily pnl', (r.daily_pnl>=0?'+':'')+fmtNum(r.daily_pnl,2), r.daily_pnl>=0?'up':'down') +
    row('daily trades', r.daily_trades) +
    row('daily W/L', r.daily_wins+' / '+r.daily_losses) +
    row('streak losses', r.consecutive_losses, r.consecutive_losses>0?'down':'mut') +
    row('drawdown hit', r.max_drawdown_hit?'<span class="down">YES</span>':'no') +
    row('session trades', r.total_trades) +
    row('session WR', r.total_wr+'%');

  // Recent trades
  const tr = s.recent_trades||[];
  if(!tr.length){ $('#trades').innerHTML='<div class="mut">no trades yet</div>'; }
  else{
    $('#trades').innerHTML = tr.slice().reverse().map(x=>{
      const cls = x.outcome==='WIN'?'win':(x.outcome==='LOSS'?'loss':'skip');
      const pnl = x.pnl==null?'—':((x.pnl>=0?'+':'')+(+x.pnl).toFixed(2));
      const ts = new Date(x.placed_at*1000).toLocaleTimeString();
      return `<div class="trade-row"><span class="mut">${ts}</span><span class="${x.direction==='UP'?'up':'down'}">${x.direction}</span><span>$${(+x.entry_price).toFixed(2)}</span><span class="mut">${x.status}</span><span class="${cls}">${pnl}</span></div>`;
    }).join('');
  }
}

tick();
setInterval(tick, 1000);
</script>
</body></html>
"""


def _snapshot(bot: "Bot") -> dict:
    now = time.time()
    window_start = get_current_window_start(now)
    window_end = window_start + INTERVAL_5M
    seconds_remaining = max(0.0, window_end - now)

    st = bot.price_feed.state
    vwap_div = ((st.current_price - st.vwap) / st.vwap) if st.vwap > 0 else 0.0

    # Compute a fresh signal — the current Bot doesn't expose `latest_signal`.
    sig = getattr(bot, "latest_signal", None)
    if sig is None:
        try:
            from signal_engine import compute_signal
            sig = compute_signal(st, window_end)
        except Exception:
            sig = None

    sig_payload = None
    gate = None
    if sig is not None:
        try:
            from signal_engine import (
                MOMENTUM_SCALE, VWAP_DIV_SCALE, delta_confidence,
            )
            ddir = 1 if st.delta_from_open > 0 else (-1 if st.delta_from_open < 0 else 0)
            def _clamp(v, lo, hi): return max(lo, min(hi, v))
            mom_n = _clamp(st.momentum / MOMENTUM_SCALE, -1.0, 1.0)
            vol_n = _clamp(getattr(st, "volume_imbalance", 0.0), -1.0, 1.0)
            vwap_n = _clamp(vwap_div / VWAP_DIV_SCALE, -1.0, 1.0) if st.vwap > 0 else 0.0
            base_delta = delta_confidence(st.delta_from_open_abs)
            mom_align = mom_n * ddir if ddir != 0 else mom_n
            vol_align = vol_n * ddir if ddir != 0 else vol_n
            vwap_align = vwap_n * ddir if ddir != 0 else vwap_n
            tb = 1.15 if sig.seconds_to_close <= 15 else (1.08 if sig.seconds_to_close <= 30 else 1.0)
            sig_payload = {
                "direction": sig.direction,
                "confidence": sig.confidence,
                "suggested_price": sig.suggested_price,
                "expected_value": sig.expected_value,
                "seconds_to_close": sig.seconds_to_close,
                "base_delta_conf": base_delta,
                "mom_align": mom_align,
                "vol_align": vol_align,
                "vwap_align": vwap_align,
                "vel_align": 0.0,
                "book_align": 0.0,
                "time_boost": tb,
                "vol_mult": 1.0,
            }
        except Exception as e:
            logger.warning(f"sig_payload build error: {e}")
            sig_payload = None

    mw = bot.current_window
    market_payload = None
    if mw is not None:
        market_payload = {
            "start_ts": mw.start_ts,
            "question": mw.question,
            "up_token_id": mw.up_token_id,
            "down_token_id": mw.down_token_id,
            "up_best_ask": mw.up_best_ask,
            "down_best_ask": mw.down_best_ask,
            "up_best_bid": mw.up_best_bid,
            "down_best_bid": mw.down_best_bid,
        }
        if sig is not None:
            gate = gate_vs_market(sig, ask_up=mw.up_best_ask or 0.0, ask_down=mw.down_best_ask or 0.0)

    pt = bot.pending_trade
    pt_payload = pt.to_dict() if pt is not None else None

    rs = bot.risk_manager.state
    daily_wr = round((rs.daily_wins / rs.daily_trades * 100), 0) if rs.daily_trades else 0
    total_wr = round((rs.total_wins / rs.total_trades * 100), 0) if rs.total_trades else 0
    risk_payload = {
        "balance": rs.current_balance,
        "starting_balance": rs.starting_balance,
        "daily_pnl": rs.daily_pnl,
        "daily_trades": rs.daily_trades,
        "daily_wins": rs.daily_wins,
        "daily_losses": rs.daily_losses,
        "daily_wr": int(daily_wr),
        "consecutive_losses": rs.consecutive_losses,
        "max_drawdown_hit": rs.max_drawdown_hit,
        "total_trades": rs.total_trades,
        "total_wr": int(total_wr),
    }

    recent = [t.to_dict() for t in list(getattr(bot, "recent_trades", []))]

    return {
        "now": now,
        "mode": getattr(__import__("config").CONFIG, "trading_mode", "paper"),
        "seconds_remaining": seconds_remaining,
        "window_start": window_start,
        "btc": {
            "current_price": st.current_price,
            "window_open_price": st.window_open_price,
            "delta_from_open": st.delta_from_open,
            "delta_30s": getattr(st, "delta_30s", 0.0),
            "vwap": st.vwap,
            "vwap_div": vwap_div,
            "momentum": st.momentum,
            "volume_imbalance": getattr(st, "volume_imbalance", 0.0),
            "book_imbalance": getattr(st, "book_imbalance", 0.0),
            "realized_vol": getattr(st, "realized_vol", 0.0),
            "tick_count": st.tick_count,
        },
        "signal": sig_payload,
        "market": market_payload,
        "gate": gate,
        "pending_trade": pt_payload,
        "risk": risk_payload,
        "recent_trades": recent,
    }


async def _index(_req: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def _state(req: web.Request) -> web.Response:
    bot = req.app["bot"]
    try:
        snap = _snapshot(bot)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response(snap)


async def start_dashboard(bot: "Bot", host: str = "127.0.0.1", port: int = 8787):
    """Launch aiohttp server in background. Returns AppRunner for shutdown."""
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", _index)
    app.router.add_get("/state", _state)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"Dashboard live at http://{host}:{port}")
    return runner
