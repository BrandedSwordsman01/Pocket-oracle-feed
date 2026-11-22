/* Backtest results per setup (research/SETUPS.md, test period Sep 23 - Nov 22 2026, best horizon). */
(function (root) {
  'use strict';
  var SETUP_STATS = {
    ema_cross:      { horizon: '15m', testMeanBp: -0.3, testHit: 0.481, t: -0.7, verdict: 'none',  note: 'No edge in backtest. Visual only.' },
    breakout_60:    { horizon: '15m', testMeanBp: 0.2,  testHit: 0.451, t: 0.3,  verdict: 'none',  note: 'No edge in backtest. Visual only.' },
    bb_squeeze:     { horizon: '60m', testMeanBp: -2.6, testHit: 0.473, t: -1.7, verdict: 'none',  note: 'No edge in backtest. Visual only.' },
    momentum_60:    { horizon: '60m', testMeanBp: 2.1,  testHit: 0.467, t: 0.6,  verdict: 'none',  note: 'Unstable between periods. Visual only.' },
    shock_reversal: { horizon: '60m', testMeanBp: -15,  testHit: 0.530, t: -1.7, verdict: 'none',  note: 'No edge in backtest. Visual only.' },
    rsi_extreme:    { horizon: '15m', testMeanBp: 1.5,  testHit: 0.581, t: 1.4,  verdict: 'weak',  note: 'Bounces 58% of the time, but the average move is smaller than fees.' },
    vwap_stretch:   { horizon: '60m', testMeanBp: 8.0,  testHit: 0.652, t: 1.5,  verdict: 'watch', note: 'Best candidate: reverted 65% of the time (+8 bps/60m). Not yet statistically proven.' }
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = SETUP_STATS;
  else root.SETUP_STATS = SETUP_STATS;
})(this);
