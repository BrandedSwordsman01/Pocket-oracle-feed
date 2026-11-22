package com.pocketoracle.kalshibot;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.WindowInsets;
import android.widget.FrameLayout;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONObject;

import java.io.InputStream;

public class MainActivity extends Activity {
    private static final int PICK_KEY = 42;
    private WebView webView;
    private Bridge bridge;
    private String pendingEnv, pendingKeyId;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        webView = new WebView(this);
        webView.setBackgroundColor(0xFF131722);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(0xFF131722);
        root.addView(webView, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        setContentView(root);
        // Android 15 draws apps edge-to-edge: pad the container so the status bar and gesture bar never cover the UI.
        root.setOnApplyWindowInsetsListener((View v, WindowInsets insets) -> {
            int top, bottom, left, right;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets b = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                top = b.top; bottom = b.bottom; left = b.left; right = b.right;
            } else {
                top = insets.getSystemWindowInsetTop(); bottom = insets.getSystemWindowInsetBottom();
                left = insets.getSystemWindowInsetLeft(); right = insets.getSystemWindowInsetRight();
            }
            v.setPadding(left, top, right, bottom);
            return insets;
        });

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowContentAccess(false);
        s.setAllowFileAccess(false); // assets under file:///android_asset still load

        bridge = new Bridge(this, webView);
        webView.addJavascriptInterface(bridge, "Native");
        webView.setWebChromeClient(new WebChromeClient()); // enables alert/confirm/prompt
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return true; // never navigate away from the bundled UI
            }
        });
        webView.loadUrl("file:///android_asset/index.html");

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
    }

    void pickKeyFile(String env, String keyId) {
        pendingEnv = env;
        pendingKeyId = keyId;
        runOnUiThread(() -> {
            Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            i.addCategory(Intent.CATEGORY_OPENABLE);
            i.setType("*/*");
            startActivityForResult(i, PICK_KEY);
        });
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != PICK_KEY) return;
        String result;
        if (resultCode != RESULT_OK || data == null || data.getData() == null) {
            result = "cancelled";
        } else {
            try (InputStream is = getContentResolver().openInputStream(data.getData())) {
                result = bridge.saveKey(pendingEnv, pendingKeyId, Bridge.readKeyFile(is));
            } catch (Exception e) {
                result = e.getClass().getSimpleName() + ": " + e.getMessage();
            }
        }
        final String js = "window.__keyImported && window.__keyImported(" + JSONObject.quote(result) + ")";
        webView.post(() -> webView.evaluateJavascript(js, null));
    }

    // Deliberately NOT pausing the WebView in onPause, so the bot's timers keep running
    // in the background while BotService holds the process in the foreground.

    @Override
    public void onBackPressed() {
        moveTaskToBack(true); // back button shouldn't kill a running bot
    }

    @Override
    protected void onDestroy() {
        if (webView != null) webView.destroy();
        super.onDestroy();
    }
}
