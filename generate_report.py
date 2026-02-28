#!/usr/bin/env python3
"""
generate_report.py — Genera un report HTML delle performance di trading Hyperliquid

Uso:
    python3 generate_report.py trade_history.csv
    python3 generate_report.py trade_history.csv -o mio_report.html
    python3 generate_report.py trade_history.csv -o report_2026.html --open
"""

import argparse
import csv
import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Parsing e calcolo
# ---------------------------------------------------------------------------

def parse_trades(csv_path: str) -> dict:
    rows = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            row['time']      = datetime.strptime(row['time'], '%d/%m/%Y - %H:%M:%S')
            row['px']        = float(row['px'])
            row['sz']        = float(row['sz'])
            row['ntl']       = float(row['ntl'])
            row['fee']       = float(row['fee'])
            row['closedPnl'] = float(row['closedPnl'])
            row['coin_clean'] = (row['coin']
                                 .replace(' (xyz)', '').replace('(xyz)', '')
                                 .replace('/USDC', '').strip())
            if '(xyz)' in row['coin']:
                row['market'] = 'XYZ'
            elif '/USDC' in row['coin']:
                row['market'] = 'SPOT'
            else:
                row['market'] = 'PERP'
            rows.append(row)

    rows.sort(key=lambda r: r['time'])

    trades     = []
    open_stack = {}   # coin -> [open_row, ...]
    TOL        = 1e-6  # tolleranza floating point per confronto size

    for row in rows:
        c = row['coin_clean']
        d = row['dir']

        if d in ('Open Long', 'Open Short'):
            open_stack.setdefault(c, []).append(row)

        elif d in ('Close Long', 'Close Short', 'Auto-Deleveraging'):
            stack = open_stack.get(c, [])
            if not stack:
                continue

            close_sz  = row['sz']
            close_pnl = row['closedPnl']
            close_fee = abs(row['fee'])

            # Consuma le open nell'ordine FIFO finché la size totale copre la close
            # Gestisce: 1 close che copre N open, oppure 1 close parziale su 1 open
            consumed  = []
            remaining = close_sz

            while stack and remaining > TOL:
                op = stack[0]
                if op['sz'] <= remaining + TOL:
                    # Open coperta completamente
                    consumed.append(stack.pop(0))
                    remaining -= op['sz']
                else:
                    # Open coperta parzialmente: splitta la open rimasta
                    ratio   = remaining / op['sz']
                    partial = dict(op)
                    partial['sz']  = remaining
                    partial['ntl'] = op['ntl'] * ratio
                    partial['fee'] = op['fee'] * ratio
                    stack[0] = dict(op)
                    stack[0]['sz']  = op['sz'] - remaining
                    stack[0]['ntl'] = op['ntl'] * (1 - ratio)
                    stack[0]['fee'] = op['fee'] * (1 - ratio)
                    consumed.append(partial)
                    remaining = 0

            if not consumed:
                continue

            total_open_ntl = sum(o['ntl'] for o in consumed)
            total_open_fee = sum(abs(o['fee']) for o in consumed)
            avg_open_px    = (sum(o['px'] * o['sz'] for o in consumed)
                              / sum(o['sz'] for o in consumed))
            first_open     = consumed[0]
            fee_total      = total_open_fee + close_fee
            dur            = (row['time'] - first_open['time']).total_seconds() / 3600

            trades.append({
                'coin':       c,
                'market':     first_open['market'],
                'dir':        first_open['dir'],
                'open_time':  first_open['time'].strftime('%d/%m/%Y %H:%M'),
                'close_time': row['time'].strftime('%d/%m/%Y %H:%M'),
                'open_px':    round(avg_open_px, 6),
                'close_px':   row['px'],
                'ntl':        round(total_open_ntl, 4),
                'pnl':        round(close_pnl, 6),
                'fee':        round(fee_total, 6),
                'net_pnl':    round(close_pnl - fee_total, 4),
                'duration_h': round(dur, 1),
                'pnl_pct':    round(close_pnl / total_open_ntl * 100, 2) if total_open_ntl else 0,
                'n_opens':    len(consumed),
            })

    # Posizioni ancora aperte (open non matchate)
    open_positions = []
    for c, stack in open_stack.items():
        for row in stack:
            open_positions.append({
                'coin':      c,
                'market':    row['market'],
                'dir':       row['dir'],
                'open_time': row['time'].strftime('%d/%m/%Y %H:%M'),
                'open_px':   row['px'],
                'ntl':       round(row['ntl'], 4),
            })

    # Statistiche
    winners = [t for t in trades if t['pnl'] > 0]
    losers  = [t for t in trades if t['pnl'] <= 0]

    total_pnl  = sum(t['pnl']     for t in trades)
    total_fees = sum(t['fee']     for t in trades)
    total_net  = sum(t['net_pnl'] for t in trades)
    win_rate   = len(winners) / len(trades) * 100 if trades else 0
    avg_win    = sum(t['pnl'] for t in winners) / len(winners) if winners else 0
    avg_loss   = sum(t['pnl'] for t in losers)  / len(losers)  if losers  else 0
    pf_denom   = abs(sum(t['pnl'] for t in losers))
    pf         = sum(t['pnl'] for t in winners) / pf_denom if pf_denom else 999
    avg_dur    = sum(t['duration_h'] for t in trades) / len(trades) if trades else 0

    best  = max(trades, key=lambda t: t['pnl']) if trades else None
    worst = min(trades, key=lambda t: t['pnl']) if trades else None

    # Per simbolo
    by_coin = {}
    for t in trades:
        c = t['coin']
        if c not in by_coin:
            by_coin[c] = {'pnl': 0.0, 'net_pnl': 0.0, 'count': 0, 'wins': 0}
        by_coin[c]['pnl']     += t['pnl']
        by_coin[c]['net_pnl'] += t['net_pnl']
        by_coin[c]['count']   += 1
        if t['pnl'] > 0:
            by_coin[c]['wins'] += 1
    for c in by_coin:
        by_coin[c]['pnl']     = round(by_coin[c]['pnl'], 2)
        by_coin[c]['net_pnl'] = round(by_coin[c]['net_pnl'], 2)

    return {
        'trades':         trades,
        'open_positions': open_positions,
        'stats': {
            'total_trades':  len(trades),
            'total_pnl':     round(total_pnl, 2),
            'total_fees':    round(total_fees, 4),
            'total_net':     round(total_net, 2),
            'win_rate':      round(win_rate, 1),
            'avg_win':       round(avg_win, 2),
            'avg_loss':      round(avg_loss, 2),
            'profit_factor': round(pf, 2),
            'avg_duration_h': round(avg_dur, 1),
            'best_trade':    best,
            'worst_trade':   worst,
            'by_coin':       by_coin,
        },
        'generated_at': datetime.now().strftime('%d/%m/%Y %H:%M:%S'),
        'source_file':  str(csv_path),
    }


# ---------------------------------------------------------------------------
# Generazione HTML
# ---------------------------------------------------------------------------

def generate_html(data: dict) -> str:
    s          = data['stats']
    trades_j   = json.dumps(data['trades'])
    open_j     = json.dumps(data['open_positions'])
    by_coin_j  = json.dumps(s['by_coin'])
    best       = s['best_trade']  or {}
    worst      = s['worst_trade'] or {}
    net_sign   = '+' if s['total_net'] >= 0 else ''
    net_color  = '#00e5a0' if s['total_net'] >= 0 else '#ff4d6d'

    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Performance Report</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:ital,wght@0,400;0,700;1,400&family=Syne:wght@400;600;800&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#0a0a0f;--surface:#12121a;--surface2:#1a1a26;--border:#2a2a3d;
    --accent:#00e5a0;--red:#ff4d6d;--green:#00e5a0;--yellow:#ffd166;
    --text:#e8e8f0;--muted:#6b6b8a;
  }}
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{background:var(--bg);color:var(--text);font-family:'Space Mono',monospace;min-height:100vh;padding:2rem}}
  body::before{{content:'';position:fixed;inset:0;background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.04'/%3E%3C/svg%3E");pointer-events:none;z-index:999;opacity:.4}}
  .header{{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:3rem;padding-bottom:1.5rem;border-bottom:1px solid var(--border)}}
  .header-title{{font-family:'Syne',sans-serif}}
  .header-title h1{{font-size:2.8rem;font-weight:800;letter-spacing:-.03em;line-height:1}}
  .header-title h1 span{{color:var(--accent)}}
  .header-title p{{color:var(--muted);font-size:.75rem;margin-top:.5rem;letter-spacing:.1em;text-transform:uppercase}}
  .header-net{{text-align:right}}
  .header-net .label{{color:var(--muted);font-size:.7rem;letter-spacing:.15em;text-transform:uppercase}}
  .header-net .value{{font-family:'Syne',sans-serif;font-size:3rem;font-weight:800;line-height:1}}
  .header-meta{{color:var(--muted);font-size:.6rem;margin-top:.4rem;text-align:right}}
  .kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1px;background:var(--border);border:1px solid var(--border);margin-bottom:2rem}}
  .kpi{{background:var(--surface);padding:1.2rem 1.4rem;position:relative;overflow:hidden;transition:background .2s}}
  .kpi:hover{{background:var(--surface2)}}
  .kpi::after{{content:'';position:absolute;bottom:0;left:0;right:0;height:2px;background:var(--accent);transform:scaleX(0);transform-origin:left;transition:transform .3s ease}}
  .kpi:hover::after{{transform:scaleX(1)}}
  .kpi .label{{font-size:.65rem;color:var(--muted);text-transform:uppercase;letter-spacing:.12em;margin-bottom:.6rem}}
  .kpi .value{{font-family:'Syne',sans-serif;font-size:1.6rem;font-weight:700;line-height:1}}
  .kpi .value.green{{color:var(--green)}}.kpi .value.red{{color:var(--red)}}.kpi .value.yellow{{color:var(--yellow)}}
  .kpi .sub{{font-size:.65rem;color:var(--muted);margin-top:.3rem}}
  .section{{margin-bottom:2.5rem}}
  .section-title{{font-family:'Syne',sans-serif;font-size:.7rem;font-weight:600;letter-spacing:.2em;text-transform:uppercase;color:var(--muted);margin-bottom:1rem;display:flex;align-items:center;gap:.8rem}}
  .section-title::after{{content:'';flex:1;height:1px;background:var(--border)}}
  .two-col{{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem}}
  .coin-bars{{display:flex;flex-direction:column;gap:.6rem}}
  .coin-bar-row{{display:flex;align-items:center;gap:.8rem}}
  .coin-name{{font-size:.7rem;width:70px;color:var(--text);text-align:right;flex-shrink:0}}
  .coin-bar-wrap{{flex:1;height:22px;background:var(--surface2);position:relative;overflow:hidden}}
  .coin-bar-fill{{height:100%;display:flex;align-items:center;padding-left:8px;font-size:.65rem;font-weight:700;min-width:2px}}
  .coin-bar-fill.pos{{background:linear-gradient(90deg,#00e5a033,#00e5a0);color:var(--bg)}}
  .coin-bar-fill.neg{{background:linear-gradient(90deg,#ff4d6d33,#ff4d6d);color:#fff}}
  .coin-count{{font-size:.6rem;color:var(--muted);width:30px;text-align:right;flex-shrink:0}}
  .trade-table{{width:100%;border-collapse:collapse}}
  .trade-table th{{font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);padding:.5rem .8rem;text-align:left;border-bottom:1px solid var(--border);white-space:nowrap}}
  .trade-table td{{font-size:.7rem;padding:.55rem .8rem;border-bottom:1px solid #1a1a26;white-space:nowrap}}
  .trade-table tr:hover td{{background:var(--surface2)}}
  .pnl-pos{{color:var(--green);font-weight:700}}.pnl-neg{{color:var(--red);font-weight:700}}
  .badge{{display:inline-block;padding:.15rem .5rem;font-size:.58rem;border-radius:2px;font-weight:700;letter-spacing:.05em}}
  .badge-long{{background:#00e5a022;color:var(--green);border:1px solid #00e5a044}}
  .badge-short{{background:#ff4d6d22;color:var(--red);border:1px solid #ff4d6d44}}
  .badge-xyz{{background:#7b5ea722;color:#b09fd8;border:1px solid #7b5ea744}}
  .badge-perp{{background:#ffd16622;color:var(--yellow);border:1px solid #ffd16644}}
  .badge-spot{{background:#00aaff22;color:#66ccff;border:1px solid #00aaff44}}
  .extremes{{display:grid;grid-template-columns:1fr 1fr;gap:1rem}}
  .extreme-card{{background:var(--surface);border:1px solid var(--border);padding:1.2rem;position:relative;overflow:hidden}}
  .extreme-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px}}
  .extreme-card.best::before{{background:var(--green)}}.extreme-card.worst::before{{background:var(--red)}}
  .extreme-card .etype{{font-size:.6rem;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);margin-bottom:.8rem}}
  .extreme-card .ecoin{{font-family:'Syne',sans-serif;font-size:1.4rem;font-weight:800}}
  .extreme-card .epnl{{font-size:1.1rem;font-weight:700;margin-top:.2rem}}
  .extreme-card .emeta{{font-size:.65rem;color:var(--muted);margin-top:.5rem;line-height:1.6}}
  .dir-split{{display:flex;gap:1rem}}
  .dir-box{{flex:1;background:var(--surface);border:1px solid var(--border);padding:1.2rem;text-align:center}}
  .dir-box .dlabel{{font-size:.65rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:.5rem}}
  .dir-box .dval{{font-family:'Syne',sans-serif;font-size:1.8rem;font-weight:800}}
  .dir-box .dsub{{font-size:.65rem;color:var(--muted);margin-top:.3rem}}
  .timeline{{position:relative;padding-left:1.5rem}}
  .timeline::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:1px;background:var(--border)}}
  .tl-item{{position:relative;margin-bottom:.6rem;display:flex;align-items:center;gap:.8rem}}
  .tl-dot{{position:absolute;left:-1.5rem;width:8px;height:8px;border-radius:50%;transform:translateX(-3.5px)}}
  .tl-dot.pos{{background:var(--green);box-shadow:0 0 6px var(--green)}}.tl-dot.neg{{background:var(--red);box-shadow:0 0 6px var(--red)}}
  .tl-date{{font-size:.6rem;color:var(--muted);width:90px;flex-shrink:0}}
  .tl-coin{{font-size:.7rem;width:70px;flex-shrink:0}}
  .tl-bar-wrap{{flex:1;height:14px;position:relative}}
  .tl-bar{{position:absolute;height:100%;display:flex;align-items:center;padding:0 4px;font-size:.6rem;font-weight:700}}
  .tl-bar.pos{{left:50%;background:#00e5a033;border-left:2px solid var(--green);color:var(--green)}}
  .tl-bar.neg{{right:50%;background:#ff4d6d22;border-right:2px solid var(--red);color:var(--red);justify-content:flex-end}}
  .open-pos-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:.8rem}}
  .open-pos-card{{background:var(--surface);border:1px solid var(--border);border-left:3px solid #00aaff;padding:1rem}}
  .open-pos-card .op-coin{{font-family:'Syne',sans-serif;font-size:1.1rem;font-weight:700}}
  .open-pos-card .op-meta{{font-size:.65rem;color:var(--muted);margin-top:.4rem;line-height:1.7}}
  .open-pos-card .op-ntl{{font-size:.85rem;color:var(--yellow);margin-top:.4rem;font-weight:700}}
  @media(max-width:768px){{.two-col,.extremes{{grid-template-columns:1fr}}.header{{flex-direction:column;align-items:flex-start;gap:1rem}}body{{padding:1rem}}}}
</style>
</head>
<body>

<div class="header">
  <div class="header-title">
    <h1>TRADING<br><span>PERFORMANCE</span></h1>
    <p>Hyperliquid · Trade History Analysis</p>
  </div>
  <div class="header-net">
    <div class="label">Net P&amp;L</div>
    <div class="value" style="color:{net_color}">{net_sign}${s['total_net']:.2f}</div>
    <div class="header-meta">Generato il {data['generated_at']}<br>{data['source_file']}</div>
  </div>
</div>

<div class="kpi-grid">
  <div class="kpi">
    <div class="label">Trade chiusi</div>
    <div class="value">{s['total_trades']}</div>
    <div class="sub">+ {len(data['open_positions'])} posizioni aperte</div>
  </div>
  <div class="kpi">
    <div class="label">Gross P&amp;L</div>
    <div class="value {'green' if s['total_pnl']>=0 else 'red'}">{'+' if s['total_pnl']>=0 else ''}${s['total_pnl']:.2f}</div>
    <div class="sub">Fees: ${s['total_fees']:.2f}</div>
  </div>
  <div class="kpi">
    <div class="label">Win Rate</div>
    <div class="value {'green' if s['win_rate']>=50 else 'red'}">{s['win_rate']:.1f}%</div>
    <div class="sub">{len([t for t in data['trades'] if t['pnl']>0])} vinti / {len([t for t in data['trades'] if t['pnl']<=0])} persi</div>
  </div>
  <div class="kpi">
    <div class="label">Profit Factor</div>
    <div class="value yellow">{s['profit_factor']:.2f}</div>
    <div class="sub">Gain totale / Loss totale</div>
  </div>
  <div class="kpi">
    <div class="label">Avg Win</div>
    <div class="value green">+${s['avg_win']:.2f}</div>
    <div class="sub">Avg loss: ${s['avg_loss']:.2f}</div>
  </div>
  <div class="kpi">
    <div class="label">Avg Durata</div>
    <div class="value">{s['avg_duration_h']:.1f}h</div>
    <div class="sub">Media ore per trade</div>
  </div>
</div>

<div class="section">
  <div class="section-title">Trade estremi</div>
  <div class="extremes">
    <div class="extreme-card best">
      <div class="etype">🏆 Best Trade</div>
      <div class="ecoin">{best.get('coin','—')}</div>
      <div class="epnl" style="color:var(--green)">+${best.get('pnl',0):.2f} &nbsp;<small style="font-weight:400;font-size:.8rem">+{best.get('pnl_pct',0):.2f}%</small></div>
      <div class="emeta">
        {'Long' if best.get('dir')=='Open Long' else 'Short'} {best.get('market','')}<br>
        {best.get('open_px',0):.5g} → {best.get('close_px',0):.5g} · {best.get('duration_h',0):.1f}h<br>
        {best.get('open_time','—')}
      </div>
    </div>
    <div class="extreme-card worst">
      <div class="etype">💀 Worst Trade</div>
      <div class="ecoin">{worst.get('coin','—')}</div>
      <div class="epnl" style="color:var(--red)">${worst.get('pnl',0):.2f} &nbsp;<small style="font-weight:400;font-size:.8rem">{worst.get('pnl_pct',0):.2f}%</small></div>
      <div class="emeta">
        {'Long' if worst.get('dir')=='Open Long' else 'Short'} {worst.get('market','')}<br>
        {worst.get('open_px',0):.5g} → {worst.get('close_px',0):.5g} · {worst.get('duration_h',0):.1f}h<br>
        {worst.get('open_time','—')}
      </div>
    </div>
  </div>
</div>

<div class="two-col section">
  <div>
    <div class="section-title">P&amp;L per simbolo</div>
    <div class="coin-bars" id="coinBars"></div>
  </div>
  <div>
    <div class="section-title">Long vs Short</div>
    <div class="dir-split">
      <div class="dir-box">
        <div class="dlabel">Long</div>
        <div class="dval" style="color:var(--green)" id="longCount"></div>
        <div class="dsub" id="longPnl"></div>
      </div>
      <div class="dir-box">
        <div class="dlabel">Short</div>
        <div class="dval" style="color:var(--red)" id="shortCount"></div>
        <div class="dsub" id="shortPnl"></div>
      </div>
    </div>
    <div style="margin-top:1.5rem">
      <div class="section-title">Mercati</div>
      <div class="dir-split">
        <div class="dir-box">
          <div class="dlabel">PERP</div>
          <div class="dval" style="color:var(--yellow)" id="perpCount"></div>
          <div class="dsub" id="perpPnl"></div>
        </div>
        <div class="dir-box">
          <div class="dlabel">XYZ</div>
          <div class="dval" style="color:#b09fd8" id="xyzCount"></div>
          <div class="dsub" id="xyzPnl"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="section">
  <div class="section-title">Timeline P&amp;L</div>
  <div class="timeline" id="timeline"></div>
</div>

<div class="section">
  <div class="section-title">Tutti i trade chiusi</div>
  <div style="overflow-x:auto">
    <table class="trade-table">
      <thead>
        <tr>
          <th>Simbolo</th><th>Mercato</th><th>Dir</th>
          <th>Apertura</th><th>Chiusura</th>
          <th>Px Open</th><th>Px Close</th>
          <th>Nozionale</th><th>Durata</th>
          <th>P&amp;L</th><th>P&amp;L %</th><th>Net P&amp;L</th>
        </tr>
      </thead>
      <tbody id="tradeTableBody"></tbody>
    </table>
  </div>
</div>

<div class="section">
  <div class="section-title">Posizioni aperte ({len(data['open_positions'])})</div>
  <div class="open-pos-grid" id="openPosGrid"></div>
</div>

<script>
const trades  = {trades_j};
const openPos = {open_j};
const byCoin  = {by_coin_j};

// Coin bars
const maxPnl = Math.max(...Object.values(byCoin).map(v => Math.abs(v.pnl)));
const sorted = Object.entries(byCoin).sort((a,b) => b[1].pnl - a[1].pnl);
const barsEl = document.getElementById('coinBars');
sorted.forEach(([coin, d]) => {{
  const pct = Math.abs(d.pnl) / maxPnl * 100;
  const pos = d.pnl >= 0;
  barsEl.innerHTML += `<div class="coin-bar-row">
    <div class="coin-name">${{coin}}</div>
    <div class="coin-bar-wrap"><div class="coin-bar-fill ${{pos?'pos':'neg'}}" style="width:${{pct}}%">${{pos?'+':''}}${{d.pnl.toFixed(2)}}</div></div>
    <div class="coin-count">${{d.wins}}/${{d.count}}</div></div>`;
}});

// Long/Short/PERP/XYZ
const longs  = trades.filter(t => t.dir === 'Open Long');
const shorts = trades.filter(t => t.dir === 'Open Short');
const perpT  = trades.filter(t => t.market === 'PERP');
const xyzT   = trades.filter(t => t.market === 'XYZ');
const sum = arr => arr.reduce((a,b) => a + b.pnl, 0);
document.getElementById('longCount').textContent  = longs.length;
document.getElementById('longPnl').textContent    = `P&L: +$${{sum(longs).toFixed(2)}}`;
document.getElementById('shortCount').textContent = shorts.length;
document.getElementById('shortPnl').textContent   = `P&L: $${{sum(shorts).toFixed(2)}}`;
document.getElementById('perpCount').textContent  = perpT.length;
document.getElementById('perpPnl').textContent    = `P&L: $${{sum(perpT).toFixed(2)}}`;
document.getElementById('xyzCount').textContent   = xyzT.length;
document.getElementById('xyzPnl').textContent     = `P&L: $${{sum(xyzT).toFixed(2)}}`;

// Trade table
const tbody = document.getElementById('tradeTableBody');
trades.forEach(t => {{
  const isLong = t.dir === 'Open Long';
  const pos    = t.pnl >= 0;
  const dur    = t.duration_h < 1 ? `${{Math.round(t.duration_h*60)}}m`
               : t.duration_h < 24 ? `${{t.duration_h.toFixed(1)}}h`
               : `${{(t.duration_h/24).toFixed(1)}}d`;
  const fmtPx  = px => px < 1 ? px.toFixed(5) : px.toFixed(2);
  tbody.innerHTML += `<tr>
    <td><strong>${{t.coin}}</strong></td>
    <td><span class="badge badge-${{t.market.toLowerCase()}}">${{t.market}}</span></td>
    <td><span class="badge ${{isLong?'badge-long':'badge-short'}}">${{isLong?'LONG':'SHORT'}}</span></td>
    <td style="color:var(--muted)">${{t.open_time}}</td>
    <td style="color:var(--muted)">${{t.close_time}}</td>
    <td>${{fmtPx(t.open_px)}}</td><td>${{fmtPx(t.close_px)}}</td>
    <td>$${{t.ntl.toFixed(0)}}</td>
    <td style="color:var(--muted)">${{dur}}</td>
    <td class="${{pos?'pnl-pos':'pnl-neg'}}">${{pos?'+':''}}$${{t.pnl.toFixed(2)}}</td>
    <td class="${{pos?'pnl-pos':'pnl-neg'}}">${{pos?'+':''}}${{t.pnl_pct.toFixed(2)}}%</td>
    <td class="${{pos?'pnl-pos':'pnl-neg'}}">${{pos?'+':''}}$${{t.net_pnl.toFixed(2)}}</td>
  </tr>`;
}});

// Timeline
const maxAbs = Math.max(...trades.map(t => Math.abs(t.pnl)));
const tlEl   = document.getElementById('timeline');
trades.forEach(t => {{
  const pos = t.pnl >= 0;
  const w   = Math.abs(t.pnl) / maxAbs * 45;
  tlEl.innerHTML += `<div class="tl-item">
    <div class="tl-dot ${{pos?'pos':'neg'}}"></div>
    <div class="tl-date">${{t.open_time.split(' ')[0]}}</div>
    <div class="tl-coin">${{t.coin}}</div>
    <div class="tl-bar-wrap"><div class="tl-bar ${{pos?'pos':'neg'}}" style="width:${{w}}%">${{pos?'+':''}}${{t.pnl.toFixed(2)}}</div></div>
  </div>`;
}});

// Open positions
const opGrid = document.getElementById('openPosGrid');
openPos.forEach(p => {{
  const isLong = p.dir === 'Open Long';
  const fmtPx  = px => px < 1 ? px.toFixed(5) : px.toFixed(2);
  opGrid.innerHTML += `<div class="open-pos-card">
    <div class="op-coin">${{p.coin}}</div>
    <div style="margin-top:.3rem">
      <span class="badge badge-${{p.market.toLowerCase()}}">${{p.market}}</span>
      <span class="badge ${{isLong?'badge-long':'badge-short'}}" style="margin-left:.3rem">${{isLong?'LONG':'SHORT'}}</span>
    </div>
    <div class="op-meta">Entry: ${{fmtPx(p.open_px)}}<br>Aperta: ${{p.open_time}}</div>
    <div class="op-ntl">$${{p.ntl.toFixed(0)}} nozionale</div>
  </div>`;
}});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Genera un report HTML delle performance di trading Hyperliquid'
    )
    parser.add_argument('csv_file',         help='File CSV con la trade history')
    parser.add_argument('-o', '--output',   help='File HTML di output (default: trade_report_YYYYMMDD_HHMM.html)')
    parser.add_argument('--open', action='store_true', help='Apri il report nel browser dopo la generazione')
    args = parser.parse_args()

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        print(f"❌ File non trovato: {csv_path}")
        sys.exit(1)

    print(f"📂 Leggo {csv_path}...")
    data = parse_trades(str(csv_path))
    s    = data['stats']

    print(f"✅ Trovati {s['total_trades']} trade chiusi, {len(data['open_positions'])} aperti")
    print(f"   Win rate:      {s['win_rate']}%")
    print(f"   Gross P&L:    ${s['total_pnl']:+.2f}")
    print(f"   Net P&L:      ${s['total_net']:+.2f}")
    print(f"   Profit factor: {s['profit_factor']}")

    out_path = Path(args.output) if args.output else Path(
        f"trade_report_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
    )

    html = generate_html(data)
    out_path.write_text(html, encoding='utf-8')
    print(f"\n📄 Report generato: {out_path.resolve()}")

    if args.open:
        webbrowser.open(out_path.resolve().as_uri())
        print("🌐 Aperto nel browser")


if __name__ == '__main__':
    main()
