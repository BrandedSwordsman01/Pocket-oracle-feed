package com.pocketoracle.kalshibot;

import java.io.ByteArrayOutputStream;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.PrivateKey;
import java.security.Signature;
import java.security.spec.MGF1ParameterSpec;
import java.security.spec.PKCS8EncodedKeySpec;
import java.security.spec.PSSParameterSpec;
import java.util.Arrays;
import java.util.Base64;

/**
 * Kalshi API request signing: base64(sign(timestamp_ms + METHOD + path)).
 *
 * Kalshi supports two key types (docs.kalshi.com/getting_started/api_keys):
 *   Ed25519 (default)  "-----BEGIN PRIVATE KEY-----" PKCS#8, Ed25519 signature over the text (RFC 8032)
 *   RSA 2048           "-----BEGIN RSA PRIVATE KEY-----" PKCS#1 (or PKCS#8), RSA-PSS SHA-256, MGF1 SHA-256, salt 32
 * The PEM header does not identify the type, so it is detected from the parsed key (Ed25519 OID 1.3.101.112).
 * Ed25519 uses the platform provider when available and otherwise a pure-Java RFC 8032 implementation.
 * Pure java.security / java.math so it can be unit-tested on a desktop JDK.
 */
public final class KalshiSigner {
    private KalshiSigner() {}

    /** Parsed Kalshi key: either an RSA PrivateKey or an Ed25519 32-byte seed. */
    public static final class Key {
        public final String type;          // "RSA" or "Ed25519"
        final PrivateKey rsa;
        final byte[] edSeed;
        final byte[] pkcs8;

        Key(String type, PrivateKey rsa, byte[] edSeed, byte[] pkcs8) {
            this.type = type; this.rsa = rsa; this.edSeed = edSeed; this.pkcs8 = pkcs8;
        }
    }

    /** For tests: force the pure-Java Ed25519 path even when the platform supports Ed25519. */
    static boolean forcePureEd25519 = false;

    private static final byte[] ED25519_OID = {0x06, 0x03, 0x2b, 0x65, 0x70};

    public static Key parsePem(String pem) throws Exception {
        if (pem == null) throw new IllegalArgumentException("empty key");
        if (pem.contains("ENCRYPTED")) throw new IllegalArgumentException("password-protected keys are not supported");
        boolean pkcs1 = pem.contains("BEGIN RSA PRIVATE KEY");
        String b64 = pem.replaceAll("-----[A-Z0-9 ]+-----", "").replaceAll("\\s", "");
        if (b64.isEmpty()) throw new IllegalArgumentException("no key data found (paste the whole key file)");
        byte[] der = Base64.getDecoder().decode(b64);
        if (pkcs1) {
            byte[] p8 = wrapPkcs1(der);
            return new Key("RSA", KeyFactory.getInstance("RSA").generatePrivate(new PKCS8EncodedKeySpec(p8)), null, p8);
        }
        if (indexOf(der, ED25519_OID) >= 0) {
            // PKCS#8 Ed25519: ... OCTET STRING { OCTET STRING (32-byte seed) } -> seed is "04 20" + 32 bytes
            int i = indexOf(der, new byte[]{0x04, 0x20});
            while (i >= 0 && i + 34 > der.length) i = -1;
            if (i < 0) throw new IllegalArgumentException("malformed Ed25519 key");
            return new Key("Ed25519", null, Arrays.copyOfRange(der, i + 2, i + 34), der);
        }
        return new Key("RSA", KeyFactory.getInstance("RSA").generatePrivate(new PKCS8EncodedKeySpec(der)), null, der);
    }

    public static String sign(Key key, String message) throws Exception {
        byte[] msg = message.getBytes(StandardCharsets.UTF_8);
        if ("Ed25519".equals(key.type)) {
            byte[] sig = null;
            if (!forcePureEd25519) {
                try {
                    PrivateKey pk = KeyFactory.getInstance("Ed25519").generatePrivate(new PKCS8EncodedKeySpec(key.pkcs8));
                    Signature s = Signature.getInstance("Ed25519");
                    s.initSign(pk);
                    s.update(msg);
                    sig = s.sign();
                } catch (Exception platformMissing) {
                    sig = null;
                }
            }
            if (sig == null) sig = Ed25519.sign(key.edSeed, msg);
            return Base64.getEncoder().encodeToString(sig);
        }
        PSSParameterSpec spec = new PSSParameterSpec("SHA-256", "MGF1", MGF1ParameterSpec.SHA256, 32, 1);
        Signature s;
        try {
            s = Signature.getInstance("SHA256withRSA/PSS"); // Android (Conscrypt)
            try { s.setParameter(spec); } catch (Exception ignored) { /* defaults are already SHA-256/MGF1/32 */ }
        } catch (NoSuchAlgorithmException e) {
            s = Signature.getInstance("RSASSA-PSS");        // desktop JDK
            s.setParameter(spec);
        }
        s.initSign(key.rsa);
        s.update(msg);
        return Base64.getEncoder().encodeToString(s.sign());
    }

    private static int indexOf(byte[] a, byte[] pat) {
        outer:
        for (int i = 0; i + pat.length <= a.length; i++) {
            for (int j = 0; j < pat.length; j++) if (a[i + j] != pat[j]) continue outer;
            return i;
        }
        return -1;
    }

    /** PKCS#8 PrivateKeyInfo = SEQ { INT 0, SEQ { OID rsaEncryption, NULL }, OCTET STRING pkcs1 } */
    static byte[] wrapPkcs1(byte[] pkcs1) {
        byte[] version = {0x02, 0x01, 0x00};
        byte[] algId = {0x30, 0x0d, 0x06, 0x09, 0x2a, (byte) 0x86, 0x48, (byte) 0x86, (byte) 0xf7, 0x0d, 0x01, 0x01, 0x01, 0x05, 0x00};
        byte[] octet = tlv(0x04, pkcs1);
        ByteArrayOutputStream body = new ByteArrayOutputStream();
        body.write(version, 0, version.length);
        body.write(algId, 0, algId.length);
        body.write(octet, 0, octet.length);
        return tlv(0x30, body.toByteArray());
    }

    private static byte[] tlv(int tag, byte[] value) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.write(tag);
        int len = value.length;
        if (len < 0x80) {
            out.write(len);
        } else {
            int n = 0;
            for (int l = len; l > 0; l >>= 8) n++;
            out.write(0x80 | n);
            for (int i = n - 1; i >= 0; i--) out.write((len >> (8 * i)) & 0xff);
        }
        out.write(value, 0, value.length);
        return out.toByteArray();
    }

    /** Minimal RFC 8032 Ed25519 signer (extended twisted-Edwards coordinates, BigInteger arithmetic). */
    static final class Ed25519 {
        static final BigInteger P = BigInteger.ONE.shiftLeft(255).subtract(BigInteger.valueOf(19));
        static final BigInteger L = BigInteger.ONE.shiftLeft(252).add(new BigInteger("27742317777372353535851937790883648493"));
        static final BigInteger D = BigInteger.valueOf(-121665).multiply(BigInteger.valueOf(121666).modInverse(P)).mod(P);
        static final BigInteger D2 = D.shiftLeft(1).mod(P);
        static final BigInteger SQRT_M1 = BigInteger.TWO.modPow(P.subtract(BigInteger.ONE).shiftRight(2), P);
        static final BigInteger[] B;

        static {
            BigInteger y = BigInteger.valueOf(4).multiply(BigInteger.valueOf(5).modInverse(P)).mod(P);
            BigInteger x = recoverX(y, 0);
            B = new BigInteger[]{x, y, BigInteger.ONE, x.multiply(y).mod(P)};
        }

        static BigInteger recoverX(BigInteger y, int sign) {
            BigInteger y2 = y.multiply(y).mod(P);
            BigInteger x2 = y2.subtract(BigInteger.ONE).multiply(D.multiply(y2).add(BigInteger.ONE).modInverse(P)).mod(P);
            BigInteger x = x2.modPow(P.add(BigInteger.valueOf(3)).shiftRight(3), P);
            if (!x.multiply(x).subtract(x2).mod(P).equals(BigInteger.ZERO)) x = x.multiply(SQRT_M1).mod(P);
            if (x.testBit(0) != (sign == 1)) x = P.subtract(x);
            return x;
        }

        static BigInteger[] add(BigInteger[] p, BigInteger[] q) {
            BigInteger a = p[1].subtract(p[0]).multiply(q[1].subtract(q[0])).mod(P);
            BigInteger b = p[1].add(p[0]).multiply(q[1].add(q[0])).mod(P);
            BigInteger c = p[3].multiply(D2).multiply(q[3]).mod(P);
            BigInteger d = p[2].shiftLeft(1).multiply(q[2]).mod(P);
            BigInteger e = b.subtract(a), f = d.subtract(c), g = d.add(c), h = b.add(a);
            return new BigInteger[]{e.multiply(f).mod(P), g.multiply(h).mod(P), f.multiply(g).mod(P), e.multiply(h).mod(P)};
        }

        static BigInteger[] mul(BigInteger s, BigInteger[] p) {
            BigInteger[] q = {BigInteger.ZERO, BigInteger.ONE, BigInteger.ONE, BigInteger.ZERO};
            for (int i = s.bitLength() - 1; i >= 0; i--) {
                q = add(q, q);
                if (s.testBit(i)) q = add(q, p);
            }
            return q;
        }

        static byte[] encode(BigInteger[] p) {
            BigInteger zi = p[2].modInverse(P);
            BigInteger x = p[0].multiply(zi).mod(P), y = p[1].multiply(zi).mod(P);
            byte[] out = le(y, 32);
            if (x.testBit(0)) out[31] |= (byte) 0x80;
            return out;
        }

        static byte[] le(BigInteger v, int n) {
            byte[] be = v.toByteArray(), out = new byte[n];
            for (int i = 0; i < n && i < be.length; i++) out[i] = be[be.length - 1 - i];
            return out;
        }

        static BigInteger fromLe(byte[] b) {
            byte[] be = new byte[b.length + 1];
            for (int i = 0; i < b.length; i++) be[b.length - i] = b[i];
            return new BigInteger(be);
        }

        static byte[] sha512(byte[]... parts) throws Exception {
            MessageDigest md = MessageDigest.getInstance("SHA-512");
            for (byte[] p : parts) md.update(p);
            return md.digest();
        }

        static byte[] sign(byte[] seed, byte[] msg) throws Exception {
            byte[] h = sha512(seed);
            byte[] a32 = Arrays.copyOfRange(h, 0, 32);
            a32[0] &= (byte) 248; a32[31] &= 127; a32[31] |= 64;
            BigInteger a = fromLe(a32);
            byte[] prefix = Arrays.copyOfRange(h, 32, 64);
            byte[] A = encode(mul(a, B));
            BigInteger r = fromLe(sha512(prefix, msg)).mod(L);
            byte[] R = encode(mul(r, B));
            BigInteger k = fromLe(sha512(R, A, msg)).mod(L);
            BigInteger S = r.add(k.multiply(a)).mod(L);
            byte[] sig = new byte[64];
            System.arraycopy(R, 0, sig, 0, 32);
            System.arraycopy(le(S, 32), 0, sig, 32, 32);
            return sig;
        }
    }
}
