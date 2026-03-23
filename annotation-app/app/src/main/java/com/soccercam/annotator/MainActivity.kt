package com.soccercam.annotator

import android.annotation.SuppressLint
import android.content.SharedPreferences
import android.os.Bundle
import android.view.View
import android.view.WindowManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var setupLayout: LinearLayout
    private lateinit var serverUrlInput: EditText
    private lateinit var prefs: SharedPreferences

    private val PREFS_NAME = "annotator_prefs"
    private val KEY_SERVER_URL = "server_url"

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        // Keep screen on during annotation
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        // Edge-to-edge / immersive mode
        WindowCompat.setDecorFitsSystemWindows(window, false)
        val controller = WindowInsetsControllerCompat(window, window.decorView)
        controller.hide(WindowInsetsCompat.Type.systemBars())
        controller.systemBarsBehavior =
            WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE

        prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
        webView = findViewById(R.id.webView)
        setupLayout = findViewById(R.id.setupLayout)
        serverUrlInput = findViewById(R.id.serverUrlInput)

        setupWebView()

        // Check for saved URL
        val savedUrl = prefs.getString(KEY_SERVER_URL, null)
        if (savedUrl != null) {
            loadServer(savedUrl)
        } else {
            showSetup()
        }

        findViewById<Button>(R.id.connectButton).setOnClickListener {
            val url = serverUrlInput.text.toString().trim()
            if (url.isNotEmpty()) {
                val normalizedUrl = if (!url.startsWith("http")) "http://$url" else url
                prefs.edit().putString(KEY_SERVER_URL, normalizedUrl).apply()
                loadServer(normalizedUrl)
            } else {
                Toast.makeText(this, "Enter a server URL", Toast.LENGTH_SHORT).show()
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            // Allow mixed content for Tailscale HTTP
            mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            // Better image loading
            loadsImagesAutomatically = true
            // Cache for offline resilience
            cacheMode = android.webkit.WebSettings.LOAD_DEFAULT
        }

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView?,
                request: WebResourceRequest?
            ): Boolean = false

            override fun onReceivedError(
                view: WebView?,
                errorCode: Int,
                description: String?,
                failingUrl: String?
            ) {
                runOnUiThread {
                    Toast.makeText(
                        this@MainActivity,
                        "Connection failed: $description",
                        Toast.LENGTH_LONG
                    ).show()
                    showSetup()
                }
            }
        }

        webView.webChromeClient = WebChromeClient()
    }

    private fun showSetup() {
        setupLayout.visibility = View.VISIBLE
        webView.visibility = View.GONE
        val savedUrl = prefs.getString(KEY_SERVER_URL, "")
        if (!savedUrl.isNullOrEmpty()) {
            serverUrlInput.setText(savedUrl)
        }
    }

    private fun loadServer(url: String) {
        setupLayout.visibility = View.GONE
        webView.visibility = View.VISIBLE
        webView.loadUrl(url)
    }

    @Deprecated("Use OnBackPressedCallback")
    override fun onBackPressed() {
        if (webView.visibility == View.VISIBLE && webView.canGoBack()) {
            webView.goBack()
        } else if (webView.visibility == View.VISIBLE) {
            // Long-press back to go to setup
            showSetup()
        } else {
            @Suppress("DEPRECATION")
            super.onBackPressed()
        }
    }
}
