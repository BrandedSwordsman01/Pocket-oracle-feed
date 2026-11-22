package com.pocketoracle.kalshibot;

import android.app.Activity;
import android.content.Intent;
import android.os.Build;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Exposed to JavaScript as window.Native. */
public final class Bridge {
    private static final String PROD_HOST = "api.elections.kalshi.com";
    private static final String DEMO_HOST = "demo-api.kalshi.co";
    private static final String COINBASE_HOST = "api.exchange.coinbase.com";

    private final Activity activity;
    private final WebView webView;
    private final KeyVault vault;
    private final ExecutorService pool = Executors.newFixedThreadPool(4);

    public Bridge(Activity activity, WebView webView) {
        this.activity = activity;
        this.webView = webView;
        this.vault = new KeyVault(activity.getApplicationContext());
    }

    /** Async HTTP. Result is delivered via window.__nativeResolve(id, status, text). */
    @JavascriptInterface
    public void request(final int id, final String method, final String url, final String body,
                        final String signEnv, final String signPath) {
        pool.execute(() -> {
            int status = 0;
            String text;
            try {
                URL u = new URL(url);
                String host = u.getHost();
                if (!"https".equals(u.getProtocol())
                        || !(PROD_HOST.equals(host) || DEMO_HOST.equals(host) || COINBASE_HOST.equals(host))) {
                    throw new SecurityException("host not allowed: " + host);
                }
                HttpURLConnection c = (HttpURLConnection) u.openConnection();
                c.setConnectTimeout(10000);
                c.setReadTimeout(15000);
                c.setRequestMethod(method);
                c.setRequestProperty("Accept", "application/json");
                c.setRequestProperty("User-Agent", "KalshiBtcBot/0.1 (Android)");
                if (signEnv != null && !signEnv.isEmpty()) {
                    String expectedHost = "demo".equals(signEnv) ? DEMO_HOST : PROD_HOST;
                    if (!expectedHost.equals(host)) throw new SecurityException("key/host mismatch");
                    String ts = String.valueOf(System.currentTimeMillis());
                    String sig = KalshiSigner.sign(vault.privateKey(signEnv), ts + method + signPath);
                    c.setRequestProperty("KALSHI-ACCESS-KEY", vault.keyId(signEnv));
                    c.setRequestProperty("KALSHI-ACCESS-TIMESTAMP", ts);
                    c.setRequestProperty("KALSHI-ACCESS-SIGNATURE", sig);
                }
                if (body != null && !body.isEmpty()) {
                    c.setDoOutput(true);
                    c.setRequestProperty("Content-Type", "application/json");
                    try (OutputStream os = c.getOutputStream()) {
                        os.write(body.getBytes(StandardCharsets.UTF_8));
                    }
                }
                status = c.getResponseCode();
                InputStream is = status >= 400 ? c.getErrorStream() : c.getInputStream();
                text = is == null ? "" : readAll(is);
                c.disconnect();
            } catch (Exception e) {
                status = 0;
                text = "{\"error\":{\"message\":" + JSONObject.quote(e.getClass().getSimpleName() + ": " + e.getMessage()) + "}}";
            }
            final String js = "window.__nativeResolve(" + id + "," + status + "," + JSONObject.quote(text) + ")";
            webView.post(() -> webView.evaluateJavascript(js, null));
        });
    }

    @JavascriptInterface
    public String saveKey(String env, String keyId, String pem) {
        if (!"demo".equals(env) && !"prod".equals(env)) return "bad environment";
        try {
            vault.save(env, keyId, pem);
            return "ok";
        } catch (Exception e) {
            return e.getClass().getSimpleName() + ": " + e.getMessage();
        }
    }

    @JavascriptInterface
    public boolean hasKey(String env) {
        return vault.has(env);
    }

    @JavascriptInterface
    public void clearKey(String env) {
        vault.clear(env);
    }

    /** Foreground service + screen-on while the bot runs, so Android doesn't freeze it. */
    @JavascriptInterface
    public void setKeepAlive(final boolean on) {
        activity.runOnUiThread(() -> {
            Intent i = new Intent(activity, BotService.class);
            if (on) {
                activity.getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                if (Build.VERSION.SDK_INT >= 26) activity.startForegroundService(i);
                else activity.startService(i);
            } else {
                activity.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                activity.stopService(i);
            }
        });
    }

    private static String readAll(InputStream is) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = is.read(buf)) > 0) out.write(buf, 0, n);
        is.close();
        return out.toString("UTF-8");
    }
}
