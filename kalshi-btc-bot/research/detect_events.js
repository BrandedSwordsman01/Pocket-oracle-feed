// Run the app's own setup detector (web/js/indicators.js) over historical candles.
// Usage: node detect_events.js COINBASE_1M.json OUT_EVENTS.json
const fs = require('fs');
const path = require('path');
const Ind = require(path.join(__dirname, '..', 'web', 'js', 'indicators.js'));

const raw = JSON.parse(fs.readFileSync(process.argv[2], 'utf8')); // [time, low, high, open, close, volume]
const candles = raw.sort((a, b) => a[0] - b[0]).map((c) => ({ time: c[0], low: c[1], high: c[2], open: c[3], close: c[4], volume: c[5] }));

// The app sees ~300 candles at a time; run on the full series in overlapping chunks so
// indicator warm-up matches, and keep each event once.
const CHUNK = 5000, WARM = 300, seen = new Set(), out = [];
for (let s = 0; s < candles.length; s += CHUNK) {
  const from = Math.max(0, s - WARM);
  const part = candles.slice(from, s + CHUNK);
  for (const e of Ind.detect(part)) {
    if (e.index + from < s) continue; // warm-up region belongs to previous chunk
    const k = e.setup + ':' + e.time;
    if (seen.has(k)) continue;
    seen.add(k);
    out.push({ time: e.time, setup: e.setup, dir: e.dir });
  }
}
fs.writeFileSync(process.argv[3], JSON.stringify(out));
const counts = {};
out.forEach((e) => { counts[e.setup] = (counts[e.setup] || 0) + 1; });
console.error(candles.length, 'candles ->', out.length, 'events', JSON.stringify(counts));
