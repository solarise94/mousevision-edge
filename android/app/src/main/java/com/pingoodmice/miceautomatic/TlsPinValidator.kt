package com.pingoodmice.miceautomatic

import android.content.Context
import android.util.Log
import java.io.BufferedReader
import java.io.IOException
import java.io.InputStreamReader
import java.net.InetSocketAddress
import java.net.Socket
import java.security.KeyStore
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.util.concurrent.atomic.AtomicReference
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager

/**
 * TlsPinValidator.kt
 *
 * 针对真实 API 服务器（TRUSTED_HOST:TRUSTED_PORT）的证书校验器：用内置的 Let's Encrypt
 * ISRG 根（X1 RSA + X2 ECDSA）构建 TrustManager，对服务器发起一次真实 TLS 握手，仅
 * 验证对端证书链，不发任何应用数据。验证通过的结果缓存一段时间，供主线程同步读取。
 *
 * 为什么需要它：
 * - 服务器走 FRP，证书是 Let's Encrypt ECDSA 链（根为 ISRG Root X2，另有 X1 交叉签名）；
 * - 卓易通等旧版 Android 兼容容器的 WebView 信任库不认 X2，会对该服务器报
 *   ERR_CERT_AUTHORITY_INVALID；
 * - 直接对任意 SslError 全放行不安全，因此改为：仅当服务器证书链能锚定到内置 ISRG 根
 *   时才允许 proceed，否则 cancel。合成域（app.miceautomatic.local）走 shouldInterceptRequest
 *   本地拦截，不产生真实 TLS，不经过本校验器。
 *
 * 线程模型（关键）：
 * - [isServerChainTrusted] 在主线程同步调用（onReceivedSslError 不能阻塞），只读缓存结论；
 * - [warmup] 与缓存过期后的重验，都在后台线程做真实 TLS 握手，绝不触碰主线程；
 * - 任何异常都安全失败为 false（全 catch），保证回调不会被网络异常拖垮。
 *
 * 缓存语义（绑定具体 leaf SPKI，而非"主机健康状态"）：
 * - 验证通过缓存 VALID_MS（10 分钟）；窗口内直接返回 true；
 * - 窗口外返回 false，并（非阻塞地）触发一次后台重新验证以刷新缓存；
 * - 验证成功时除了记时间戳，还记录对端 leaf 证书的 SPKI SHA-256，可通过
 *   [verifiedLeafSpkiSha256] 读出。该值用于 SslError 放行时绑定"当前报错证书"：
 *   仅当报错证书的 leaf SPKI 与后台握手验证过的 SPKI 完全一致才放行，防止缓存窗口内
 *   攻击者用另一张（即便我们也认）或无效证书也蒙混过关。缓存因此不再是"主机可信"的
 *   粗粒度结论，而是绑定到具体叶子证书的细粒度断言。
 *
 * 注意：本类只判断「服务器链是否锚定到内置根 + 当前证书是否就是那次验证的证书」，
 * 不替代 host 白名单（host/port 校验仍在 MainActivity.isTrustedApiServer）。
 *
 * @param context 任意 context，内部取 applicationContext。
 * @param host 受信 API 服务器主机名（用于 SNI 与证书校验）。
 * @param port 受信 API 服务器端口。
 */
class TlsPinValidator(
    context: Context,
    private val host: String,
    private val port: Int,
) {
    companion object {
        private const val TAG = "TlsPinValidator"

        /** assets 中的根证书 PEM 文件（ISRG Root X1 + X2 两块拼接）。 */
        private const val ROOT_ASSET = "isrg_roots.pem"

        /** 验证通过结果的有效期（毫秒）：10 分钟。过短会频繁握手，过长不利于及时
         *  发现服务器证书被撤销/更换。 */
        private const val VALID_MS = 10L * 60L * 1000L

        /** 后台 TLS 握手超时（毫秒）。服务器走 FRP，给足时间但要有上限避免线程长期挂起。 */
        private const val HANDSHAKE_TIMEOUT_MS = 15_000

        /**
         * 计算 X509 证书的 SPKI SHA-256，返回小写十六进制字符串。
         *
         * 取 subjectPublicKeyInfo 的 DER 编码（即 X509Certificate.getPublicKey().getEncoded()）
         * 做 SHA-256。该值是证书"身份"的稳定指纹：同一密钥对签出的证书（即便换发/
         * 续期）SPKI 相同；不同密钥必然不同。因此既能绑定"当前报错证书是否就是后台
         * 验证过的那一张"，又对证书正常轮换保持鲁棒（只要密钥不变即视为同一证书）。
         *
         * 返回 64 位小写 hex（无分隔符），便于常量时间字符串相等比较。
         */
        internal fun spkiSha256Hex(cert: X509Certificate): String {
            val spki = cert.publicKey.encoded
            val digest = MessageDigest.getInstance("SHA-256").digest(spki)
            val sb = StringBuilder(digest.size * 2)
            for (b in digest) {
                val v = b.toInt() and 0xFF
                sb.append(HEX_LOWER[v ushr 4])
                sb.append(HEX_LOWER[v and 0x0F])
            }
            return sb.toString()
        }

        private val HEX_LOWER = "0123456789abcdef".toCharArray()
    }

    /**
     * 缓存的验证结论。
     *
     * @property trusted 上一次后台握手是否成功（链锚定到内置根）。
     * @property verifiedAtMono 上一次成功验证的单调时钟时间戳（elapsedRealtime）。
     * @property leafSpkiSha256 对端 leaf 证书的 SPKI SHA-256（小写 hex）。仅 trusted=true
     *  时有意义；用于绑定 SslError 放行时"当前报错证书"与后台验证证书的一致性。
     */
    private data class CachedResult(
        val trusted: Boolean,
        val verifiedAtMono: Long,
        val leafSpkiSha256: String?,
    )

    /** 缓存槽：AtomicReference 保证主线程读 / 后台线程写的可见性与原子性。
     *  初始为 null：未验证过 → 视为不可信，直到首次后台握手成功。 */
    private val cache = AtomicReference<CachedResult?>(null)

    /** 防止多个后台验证任务并发跑：用自身做监视器锁，持有锁期间 running=true。 */
    private val verifyLock = Any()
    private var verifying = false

    private val appContext = context.applicationContext

    /** 单调时钟源：SystemClock.elapsedRealtime（不受墙钟回拨影响）。 */
    private fun nowMono(): Long = android.os.SystemClock.elapsedRealtime()

    /**
     * 主线程同步读取：服务器链当前是否可信。
     *
     * - 缓存为 trusted 且在 [VALID_MS] 窗口内 → 返回 true；
     * - 否则返回 false，并（非阻塞地）触发一次后台重新验证以刷新缓存。
     *
     * 绝不在本方法内做网络 IO；onReceivedSslError 调它不会阻塞主线程。
     */
    fun isServerChainTrusted(): Boolean {
        val c = cache.get()
        val fresh = c != null && c.trusted && (nowMono() - c.verifiedAtMono) <= VALID_MS
        if (!fresh) {
            // 缓存缺失/过期/曾失败：非阻塞触发后台重验，本次仍按不可信返回
            // （让本次 SslError 走 cancel；重验成功后下次进入窗口即放行）。
            triggerBackgroundVerify()
        }
        return fresh
    }

    /**
     * 主线程同步读取：后台握手验证过的对端 leaf 证书 SPKI SHA-256（小写 hex）。
     *
     * 仅当缓存新鲜（trusted 且在 [VALID_MS] 窗口内）时返回，否则返回 null。
     * 用于 [MainActivity] 的 onReceivedSslError：放行前比对"当前报错证书的 leaf SPKI"
     * 是否与本值完全一致，确保放行的是后台真正验证过的同一张证书，而非缓存窗口内
     * 被偷换的任意证书。
     */
    fun verifiedLeafSpkiSha256(): String? {
        val c = cache.get()
        val fresh = c != null && c.trusted && (nowMono() - c.verifiedAtMono) <= VALID_MS
        return if (fresh) c!!.leafSpkiSha256 else null
    }

    /**
     * 预热：onCreate 时调用一次，后台发起首次 TLS 验证，使后续进入页面时缓存可能已就绪。
     * 非阻塞；失败静默（仅打日志），不影响应用启动。
     */
    fun warmup() {
        triggerBackgroundVerify()
    }

    /** 非阻塞地触发一次后台验证（若已有任务在跑则跳过，避免重复握手）。 */
    private fun triggerBackgroundVerify() {
        synchronized(verifyLock) {
            if (verifying) return
            verifying = true
        }
        val t = Thread({ verifyOnBackground() }, "TlsPinValidator-worker")
        t.isDaemon = true
        t.start()
    }

    /** 后台线程：加载内置根 → 构造 TrustManager → 发起一次 TLS 握手验证服务器链。 */
    private fun verifyOnBackground() {
        var result = false
        var leafSpki: String? = null
        var socket: SSLSocket? = null
        var raw: Socket? = null
        try {
            val trustManager = loadPinnedTrustManager()
            val sslContext = SSLContext.getInstance("TLS").apply {
                init(null, arrayOf(trustManager), null)
            }
            raw = Socket()
            raw.soTimeout = HANDSHAKE_TIMEOUT_MS
            // 先建普通 TCP 连接（连不上直接抛 IOException，被 catch 后返回 false）。
            raw.connect(InetSocketAddress(host, port), HANDSHAKE_TIMEOUT_MS)
            socket = sslContext.socketFactory.createSocket(raw, host, port, true) as SSLSocket
            // 显式开启 HTTPS endpoint identification：握手时校验证书 SAN/CN 是否匹配 host。
            // 不同 TLS 实现对 createSocket(socket,host,port,autoClose) 的默认 host 校验行为
            // 不一致，显式设置保证 minSdk 26 以上一定校验主机名（防 DNS 劫持/钓鱼）。
            val params = socket.sslParameters
            params.endpointIdentificationAlgorithm = "HTTPS"
            socket.sslParameters = params
            // 仅握手验证证书链，不发应用数据。startHandshake 内部会做链校验（锚定内置根）
            // 与主机名校验，任一失败抛相应异常被下方 catch → false。
            socket.startHandshake()
            // 额外保险：确认能取到对端链（某些 JVM 实现可能不抛异常即视为通过，
            // 显式取一次以确保链非空且已过校验）。
            val session = socket.session
            val peerChain = session.peerCertificates
            if (peerChain.isNotEmpty()) {
                result = true
                // 记录对端 leaf 证书的 SPKI SHA-256，供 onReceivedSslError 放行时绑定
                // "当前报错证书"与"本次后台验证证书"的一致性（防缓存窗口内证书被偷换）。
                val leaf = peerChain[0] as? X509Certificate
                if (leaf != null) {
                    leafSpki = spkiSha256Hex(leaf)
                }
                Log.i(TAG, "verify ok: chain anchored to pinned ISRG roots " +
                    "(serverChainSize=${peerChain.size}, cipher=${session.cipherSuite}, " +
                    "leafSpki=${leafSpki ?: "unknown"})")
            } else {
                Log.w(TAG, "verify fail: empty peer certificate chain")
            }
        } catch (e: Exception) {
            // 任何异常（连接超时 / 证书不匹配 / 信任链断裂 / IO 失败）都安全失败为 false。
            // 分类日志便于排查，但不影响安全语义（false 即 cancel）。
            when (e) {
                is CertificateException ->
                    Log.w(TAG, "verify fail: certificate not pinned (${e.message})")
                is IOException ->
                    Log.w(TAG, "verify fail: network io (${e.message})")
                else ->
                    Log.w(TAG, "verify fail: unexpected (${e.javaClass.simpleName}: ${e.message})")
            }
        } finally {
            // 关 socket（先关 SSL 层再关 TCP 层），吞掉关闭异常。
            try { socket?.close() } catch (_: IOException) { /* ignore */ }
            try { raw?.close() } catch (_: IOException) { /* ignore */ }
            synchronized(verifyLock) { verifying = false }
        }
        // 无论成功失败都写缓存：成功记录时间戳（窗口内复用）+ leaf SPKI，失败也记录
        // 便于排错（失败时 trusted=false、leafSpkiSha256=null，每次 isServerChainTrusted
        // 都会再触发重验）。
        cache.set(CachedResult(result, nowMono(), leafSpki))
    }

    /**
     * 从 assets/isrg_roots.pem 加载根证书，构建仅信任这些根的 [X509TrustManager]。
     *
     * 用空 KeyStore 手动 put 进去每张证书，再交 TrustManagerFactory 用 PKIX 算法派生
     * TrustManager。这样得到的 TrustManager 只锚定内置 ISRG 根，不依赖系统信任库
     * （即旧容器不认 X2 也无所谓，只要服务器链最终能回到内置根即可）。
     */
    private fun loadPinnedTrustManager(): X509TrustManager {
        val pemText = readAsset(ROOT_ASSET)
        val certs = parsePemCertificates(pemText)
        require(certs.isNotEmpty()) { "no pinned roots found in $ROOT_ASSET" }

        val ks = KeyStore.getInstance(KeyStore.getDefaultType()).apply { load(null, null) }
        var idx = 0
        for (cert in certs) {
            // alias 唯一即可；用 subject CN 做可读后缀。
            val alias = "isrg-$idx-${cert.subjectX500Principal.name.hashCode()}"
            ks.setCertificateEntry(alias, cert)
            idx++
        }
        val tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm())
        tmf.init(ks)
        // getDefaultAlgorithm() 返回 PKIX，其 trustManagers 第一个即为 X509TrustManager。
        return tmf.trustManagers.firstOrNull { it is X509TrustManager } as X509TrustManager
    }

    /** 读 assets 文本为 UTF-8 字符串；缺失抛 IOException（由调用方 catch → false）。 */
    private fun readAsset(name: String): String =
        appContext.assets.open(name).use { stream ->
            BufferedReader(InputStreamReader(stream, Charsets.UTF_8)).readText()
        }

    /** 解析 PEM 文本中所有 CERTIFICATE 块为 [X509Certificate] 列表。 */
    private fun parsePemCertificates(pem: String): List<X509Certificate> {
        val factory = CertificateFactory.getInstance("X.509")
        val bytes = pem.toByteArray(Charsets.UTF_8)
        // generateCertificates 一次解析多个 PEM 块，返回 Collection。
        val collection = factory.generateCertificates(bytes.inputStream())
        return collection.mapNotNull { it as? X509Certificate }
    }
}
