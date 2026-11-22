package com.pocketoracle.kalshibot;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.NoSuchAlgorithmException;
import java.security.PrivateKey;
import java.security.Signature;
import java.security.spec.MGF1ParameterSpec;
import java.security.spec.PKCS8EncodedKeySpec;
import java.security.spec.PSSParameterSpec;
import java.util.Base64;

/**
 * Kalshi API v2 request signing: base64(RSA-PSS-SHA256(timestamp_ms + METHOD + path)).
 * Accepts both PKCS#1 ("BEGIN RSA PRIVATE KEY", what Kalshi hands out) and PKCS#8 PEM.
 * Pure java.security so it can be unit-tested on a desktop JDK.
 */
public final class KalshiSigner {
    private KalshiSigner() {}

    public static PrivateKey parsePem(String pem) throws Exception {
        if (pem == null) throw new IllegalArgumentException("empty key");
        boolean pkcs1 = pem.contains("BEGIN RSA PRIVATE KEY");
        if (pem.contains("ENCRYPTED")) throw new IllegalArgumentException("password-protected keys are not supported");
        String b64 = pem.replaceAll("-----[A-Z ]+-----", "").replaceAll("\\s", "");
        if (b64.isEmpty()) throw new IllegalArgumentException("no key data found");
        byte[] der = Base64.getDecoder().decode(b64);
        if (pkcs1) der = wrapPkcs1(der);
        return KeyFactory.getInstance("RSA").generatePrivate(new PKCS8EncodedKeySpec(der));
    }

    public static String sign(PrivateKey key, String message) throws Exception {
        PSSParameterSpec spec = new PSSParameterSpec("SHA-256", "MGF1", MGF1ParameterSpec.SHA256, 32, 1);
        Signature s;
        try {
            s = Signature.getInstance("SHA256withRSA/PSS"); // Android (Conscrypt)
            try { s.setParameter(spec); } catch (Exception ignored) { /* defaults are already SHA-256/MGF1/32 */ }
        } catch (NoSuchAlgorithmException e) {
            s = Signature.getInstance("RSASSA-PSS");        // desktop JDK
            s.setParameter(spec);
        }
        s.initSign(key);
        s.update(message.getBytes(StandardCharsets.UTF_8));
        return Base64.getEncoder().encodeToString(s.sign());
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
}
