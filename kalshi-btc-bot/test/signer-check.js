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

for (const type of ['pkcs1', 'pkcs8']) {
  const file = path.join(dir, type + '.pem');
  fs.writeFileSync(file, privateKey.export({ type, format: 'pem' }));
  const sig = execFileSync('java', ['-cp', classes, 'SignerCheck', file, msg], { encoding: 'utf8' }).trim();
  const ok = crypto.verify('sha256', Buffer.from(msg), {
    key: publicKey, padding: crypto.constants.RSA_PKCS1_PSS_PADDING, saltLength: 32
  }, Buffer.from(sig, 'base64'));
  if (!ok) { console.error(type + ': signature did NOT verify'); process.exit(1); }
  console.log(type + ': signature verifies');
}
fs.rmSync(dir, { recursive: true });
