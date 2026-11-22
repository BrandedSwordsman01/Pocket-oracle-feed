// Verifies the Java signer produces Kalshi-compatible RSA-PSS signatures
// for both PKCS#1 and PKCS#8 keys. Usage: node signer-check.js <classes-dir>
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const classes = process.argv[2];
const { privateKey, publicKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'signer-'));
const msg = '1732300000000GET/trade-api/v2/portfolio/balance';
const run = (file, extra) => execFileSync('java', ['-cp', classes, 'SignerCheck', file, msg].concat(extra || []),
  { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();

for (const type of ['pkcs1', 'pkcs8']) {
  const file = path.join(dir, type + '.pem');
  fs.writeFileSync(file, privateKey.export({ type, format: 'pem' }));
  const sig = run(file);
  const ok = crypto.verify('sha256', Buffer.from(msg), {
    key: publicKey, padding: crypto.constants.RSA_PKCS1_PSS_PADDING, saltLength: 32
  }, Buffer.from(sig, 'base64'));
  if (!ok) { console.error('RSA ' + type + ': signature did NOT verify'); process.exit(1); }
  console.log('RSA ' + type + ': signature verifies');
}

// Ed25519 (Kalshi's default key type): platform provider path and pure-Java fallback path
for (let i = 0; i < 3; i++) {
  const ed = crypto.generateKeyPairSync('ed25519');
  const file = path.join(dir, 'ed' + i + '.pem');
  fs.writeFileSync(file, ed.privateKey.export({ type: 'pkcs8', format: 'pem' }));
  for (const mode of ['platform', 'pure']) {
    const sig = run(file, mode === 'pure' ? ['pure'] : []);
    const ok = crypto.verify(null, Buffer.from(msg), ed.publicKey, Buffer.from(sig, 'base64'));
    if (!ok) { console.error('Ed25519 ' + mode + ': signature did NOT verify'); process.exit(1); }
    if (i === 0) console.log('Ed25519 ' + mode + ': signature verifies');
  }
}
fs.rmSync(dir, { recursive: true });
