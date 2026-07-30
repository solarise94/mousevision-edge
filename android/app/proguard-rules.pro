# The JavaScript bridge methods are called by name from WebView.
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
