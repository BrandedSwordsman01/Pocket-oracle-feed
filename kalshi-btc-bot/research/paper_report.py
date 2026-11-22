"""Build an HTML dashboard from a paper-trading journal. Usage: python3 paper_report.py JOURNAL_DIR"""
import json, os, sys, collections, html, datetime

d = sys.argv[1]
load = lambda n: [json.loads(l) for l in open(os.path.join(d, n))] if os.path.exists(os.path.join(d, n)) else []
signals, trades, snaps = load("signals.jsonl"), load("trades.jsonl"), load("snapshots.jsonl")
closed = [t for t in trades if t.get("event") == "close"]
opened = [t for t in trades if t.get("event") == "open"]
by = collections.defaultdict(list)
for t in closed:
    by[t["strategy"]].append(t)
dec = collections.Counter((s.get("strategy"), s.get("decision")) for s in signals if "decision" in s)
rej = collections.Counter(s.get("strategy") for s in signals if s.get("event", "").endswith("rejected"))
errs = [s for s in signals if s.get("event") in ("error", "skip", "no_book")]
fmt_t = lambda ts: datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%m-%d %H:%M")


def spark(vals, w=260, h=48):
    if len(vals) < 2:
        return ""
    lo, hi = min(vals + [0]), max(vals + [0])
    rng = (hi - lo) or 1
    pts = " ".join(f"{i * w / (len(vals) - 1):.1f},{h - (v - lo) / rng * h:.1f}" for i, v in enumerate(vals))
    z = h - (0 - lo) / rng * h
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}"><line x1="0" x2="{w}" y1="{z:.1f}" y2="{z:.1f}" class="zero"/>'
            f'<polyline points="{pts}" class="eq"/></svg>')


rows = []
strategies = sorted({s.get("strategy") for s in signals if s.get("strategy")})
for s in strategies:
    ts = by.get(s, [])
    pnl = [t["pnl_c"] / 100 for t in ts]
    cum, c = [], 0
    for v in pnl:
        c += v; cum.append(c)
    n = len(ts)
    rows.append(f"<tr><td>{html.escape(s)}</td><td>{dec[(s, 'yes')] + dec[(s, 'no')]}</td><td>{dec[(s, 'none')]}</td><td>{rej[s]}</td>"
                f"<td>{n}</td><td>{(sum(1 for v in pnl if v > 0) / n * 100 if n else 0):.0f}%</td>"
                f"<td class='{'pos' if sum(pnl) >= 0 else 'neg'}'>${sum(pnl):+.2f}</td>"
                f"<td>{(sum(pnl) / n * 10 if n else 0):+.2f}¢</td><td>{spark(cum)}</td></tr>")

last = snaps[-1] if snaps else None
latest_sig = [s for s in signals if last and s.get("t") == last["t"] and "decision" in s]
lat_rows = "".join(f"<tr><td>{html.escape(s['strategy'])}</td><td>{'' if s.get('prob') is None else f'{s[chr(112)+chr(114)+chr(111)+chr(98)]:.3f}'}</td>"
                   f"<td>{s['bid']:.2f} / {s['ask']:.2f}</td><td>{s['decision']}</td></tr>" for s in latest_sig)
trade_rows = "".join(
    f"<tr><td>{fmt_t(t['entry_t'])}</td><td>{html.escape(t['strategy'])}</td><td>{t['ticker']}</td><td>{t['side'].upper()}</td>"
    f"<td>{t['entry_px']:.2f} @{t['entry_min']}m</td><td>{t['exit_px']:.2f} ({t['how']})</td>"
    f"<td class='{'pos' if t['pnl_c'] >= 0 else 'neg'}'>${t['pnl_c'] / 100:+.2f}</td></tr>" for t in reversed(closed[-60:]))

page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KXBTC15M Paper Trading</title><style>
:root{{--bg:#fff;--fg:#1a1d24;--mut:#6b7280;--line:#e5e7eb;--pos:#0f8a5f;--neg:#c2410c;--acc:#2563eb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#131722;--fg:#d1d4dc;--mut:#787b86;--line:#2a2e39;--pos:#26a69a;--neg:#ef5350;--acc:#5b8cff}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px;max-width:1100px}}
h1{{font-size:20px}} h2{{font-size:15px;margin-top:28px;color:var(--mut);text-transform:uppercase;letter-spacing:.5px}}
table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}} td,th{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}}
th{{color:var(--mut);font-weight:600;font-size:12px}} .pos{{color:var(--pos)}} .neg{{color:var(--neg)}}
.wrap{{overflow-x:auto}} .eq{{fill:none;stroke:var(--acc);stroke-width:1.5}} .zero{{stroke:var(--line)}}
.note{{color:var(--mut);font-size:12px}}</style></head><body>
<h1>KXBTC15M paper trading</h1>
<p class="note">Simulated fills only (ask + slippage on entry, bid − slippage on exit, Kalshi taker fees, depth-checked against the live book).
No real orders are placed. Journal: {html.escape(os.path.abspath(d))} · {len(snaps)} decision minutes · {len(closed)} closed trades ·
{len(opened) - len(closed)} open · {len(errs)} data issues.</p>
<h2>Performance by strategy</h2><div class="wrap"><table><tr><th>Strategy</th><th>Signals</th><th>No-signal minutes</th><th>Rejected fills</th>
<th>Closed trades</th><th>Win</th><th>Net (10 lots)</th><th>Per contract</th><th>Equity</th></tr>{''.join(rows)}</table></div>
<h2>Latest minute {fmt_t(last['t']) if last else ''}</h2>
<p class="note">{html.escape(last['ticker']) if last else ''} · strike {last['K'] if last else ''} · Coinbase {last['S'] if last else ''} ·
{last['mins'] if last else ''} min left · book size bid/ask {last['bid_size'] if last else ''}/{last['ask_size'] if last else ''}</p>
<div class="wrap"><table><tr><th>Strategy</th><th>Model P(YES)</th><th>Kalshi bid / ask</th><th>Decision</th></tr>{lat_rows}</table></div>
<h2>Recent trades</h2><div class="wrap"><table><tr><th>Entry (UTC)</th><th>Strategy</th><th>Market</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&amp;L</th></tr>{trade_rows}</table></div>
</body></html>"""
out = os.path.join(d, "report.html")
open(out, "w").write(page)
print(out)
