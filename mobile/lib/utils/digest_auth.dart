import 'dart:convert';
import 'dart:math';
import 'package:crypto/crypto.dart';
import 'package:dio/dio.dart';

/// Dio interceptor that implements HTTP Digest Authentication (RFC 2617).
///
/// On receiving a 401 response with a WWW-Authenticate: Digest header,
/// this interceptor parses the challenge, computes the digest response,
/// and retries the request with an Authorization header.
class DigestAuthInterceptor extends Interceptor {
  DigestAuthInterceptor({
    required this.username,
    required this.password,
  });

  final String username;
  final String password;

  // Cached challenge parameters for subsequent requests.
  String? _realm;
  String? _nonce;
  String? _qop;
  String? _opaque;
  String? _algorithm;
  int _nonceCount = 0;

  @override
  void onError(DioException err, ErrorInterceptorHandler handler) {
    final response = err.response;
    if (response == null || response.statusCode != 401) {
      handler.next(err);
      return;
    }

    final wwwAuth = response.headers.value('www-authenticate');
    if (wwwAuth == null || !wwwAuth.toLowerCase().startsWith('digest')) {
      handler.next(err);
      return;
    }

    _parseChallenge(wwwAuth);

    // Retry the original request with digest auth.
    _retryWithDigest(err, handler);
  }

  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    // If we have cached challenge params, add auth header proactively.
    if (_nonce != null && _realm != null) {
      final method = options.method.toUpperCase();
      final uri = options.uri.path +
          (options.uri.query.isNotEmpty ? '?${options.uri.query}' : '');
      final authHeader = _buildAuthHeader(method, uri);
      options.headers['Authorization'] = authHeader;
    }
    handler.next(options);
  }

  /// Parse the WWW-Authenticate: Digest challenge header.
  void _parseChallenge(String wwwAuthenticate) {
    // Remove "Digest " prefix.
    final params = wwwAuthenticate.substring(7);
    final fields = _parseHeaderFields(params);

    _realm = fields['realm'];
    _nonce = fields['nonce'];
    _qop = fields['qop'];
    _opaque = fields['opaque'];
    _algorithm = fields['algorithm'] ?? 'MD5';
    _nonceCount = 0;
  }

  /// Parse comma-separated key=value or key="value" pairs.
  Map<String, String> _parseHeaderFields(String header) {
    final result = <String, String>{};
    // Match key=value or key="value" patterns.
    final regex = RegExp(r'(\w+)=(?:"([^"]*)"|([\w]+))');
    for (final match in regex.allMatches(header)) {
      final key = match.group(1)!.toLowerCase();
      final value = match.group(2) ?? match.group(3) ?? '';
      result[key] = value;
    }
    return result;
  }

  /// Build the Authorization header value for a digest response.
  String _buildAuthHeader(String method, String uri) {
    _nonceCount++;
    final nc = _nonceCount.toRadixString(16).padLeft(8, '0');
    final cnonce = _generateCnonce();

    // HA1 = MD5(username:realm:password)
    final ha1 = _md5Hash('$username:$_realm:$password');

    // HA2 = MD5(method:uri)
    final ha2 = _md5Hash('$method:$uri');

    // Response hash depends on qop.
    String responseHash;
    if (_qop != null && _qop!.contains('auth')) {
      // response = MD5(HA1:nonce:nc:cnonce:qop:HA2)
      responseHash = _md5Hash('$ha1:$_nonce:$nc:$cnonce:auth:$ha2');
    } else {
      // response = MD5(HA1:nonce:HA2)
      responseHash = _md5Hash('$ha1:$_nonce:$ha2');
    }

    final buffer = StringBuffer('Digest ');
    buffer.write('username="$username", ');
    buffer.write('realm="$_realm", ');
    buffer.write('nonce="$_nonce", ');
    buffer.write('uri="$uri", ');
    if (_qop != null) {
      buffer.write('qop=auth, ');
      buffer.write('nc=$nc, ');
      buffer.write('cnonce="$cnonce", ');
    }
    buffer.write('response="$responseHash"');
    if (_opaque != null) {
      buffer.write(', opaque="$_opaque"');
    }
    if (_algorithm != null) {
      buffer.write(', algorithm=$_algorithm');
    }

    return buffer.toString();
  }

  /// Retry the failed request with the computed digest auth header.
  Future<void> _retryWithDigest(
    DioException err,
    ErrorInterceptorHandler handler,
  ) async {
    final options = err.requestOptions;
    final method = options.method.toUpperCase();
    final uri = options.uri.path +
        (options.uri.query.isNotEmpty ? '?${options.uri.query}' : '');

    options.headers['Authorization'] = _buildAuthHeader(method, uri);

    try {
      final dio = Dio();
      final response = await dio.fetch(options);
      handler.resolve(response);
    } on DioException catch (e) {
      handler.next(e);
    }
  }

  /// Generate a random client nonce.
  String _generateCnonce() {
    final random = Random.secure();
    final bytes = List<int>.generate(16, (_) => random.nextInt(256));
    return bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
  }

  /// Compute MD5 hash of the input string, returned as hex.
  String _md5Hash(String input) {
    final bytes = utf8.encode(input);
    final digest = md5.convert(bytes);
    return digest.toString();
  }
}
