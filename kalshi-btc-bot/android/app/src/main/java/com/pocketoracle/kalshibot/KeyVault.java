package com.pocketoracle.kalshibot;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.util.Base64;
import java.util.HashMap;
import java.util.Map;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * Stores Kalshi API keys per environment ("demo" / "prod"). The PEM is encrypted
 * with an AES-GCM key that lives in the Android Keystore (hardware-backed on the
 * Z Flip 6), so the file on disk is useless without this device.
 */
public final class KeyVault {
    private static final String ALIAS = "kbot_vault_v1";
    private final SharedPreferences prefs;
    private final Map<String, PrivateKey> cache = new HashMap<>();

    public KeyVault(Context ctx) {
        prefs = ctx.getSharedPreferences("kbot_keys", Context.MODE_PRIVATE);
    }

    public synchronized void save(String env, String keyId, String pem) throws Exception {
        PrivateKey parsed = KalshiSigner.parsePem(pem); // validate before storing
        if (keyId == null || keyId.trim().isEmpty()) throw new IllegalArgumentException("missing API key ID");
        prefs.edit()
                .putString(env + "_id", keyId.trim())
                .putString(env + "_pem", encrypt(pem.trim()))
                .apply();
        cache.put(env, parsed);
    }

    public synchronized boolean has(String env) {
        return prefs.contains(env + "_id") && prefs.contains(env + "_pem");
    }

    public synchronized void clear(String env) {
        prefs.edit().remove(env + "_id").remove(env + "_pem").apply();
        cache.remove(env);
    }

    public synchronized String keyId(String env) {
        return prefs.getString(env + "_id", null);
    }

    public synchronized PrivateKey privateKey(String env) throws Exception {
        PrivateKey k = cache.get(env);
        if (k != null) return k;
        String blob = prefs.getString(env + "_pem", null);
        if (blob == null) throw new IllegalStateException("no " + env + " key saved");
        k = KalshiSigner.parsePem(decrypt(blob));
        cache.put(env, k);
        return k;
    }

    private static SecretKey secretKey() throws Exception {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        if (ks.containsAlias(ALIAS)) return ((KeyStore.SecretKeyEntry) ks.getEntry(ALIAS, null)).getSecretKey();
        KeyGenerator gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        gen.init(new KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build());
        return gen.generateKey();
    }

    private static String encrypt(String plain) throws Exception {
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.ENCRYPT_MODE, secretKey());
        byte[] iv = c.getIV();
        byte[] ct = c.doFinal(plain.getBytes(StandardCharsets.UTF_8));
        ByteBuffer buf = ByteBuffer.allocate(1 + iv.length + ct.length);
        buf.put((byte) iv.length).put(iv).put(ct);
        return Base64.getEncoder().encodeToString(buf.array());
    }

    private static String decrypt(String blob) throws Exception {
        ByteBuffer buf = ByteBuffer.wrap(Base64.getDecoder().decode(blob));
        byte[] iv = new byte[buf.get()];
        buf.get(iv);
        byte[] ct = new byte[buf.remaining()];
        buf.get(ct);
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.DECRYPT_MODE, secretKey(), new GCMParameterSpec(128, iv));
        return new String(c.doFinal(ct), StandardCharsets.UTF_8);
    }
}
