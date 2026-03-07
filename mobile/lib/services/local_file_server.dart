import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart' show rootBundle;
import 'package:path_provider/path_provider.dart';
import 'package:path/path.dart' as p;

/// Lightweight local HTTP server for serving the dewarp viewer HTML
/// and video files to a WebView. Avoids CORS issues with file:// URLs.
class LocalFileServer {
  static LocalFileServer? _instance;
  HttpServer? _server;
  int _port = 0;
  String? _assetDir;

  LocalFileServer._();

  static LocalFileServer get instance {
    _instance ??= LocalFileServer._();
    return _instance!;
  }

  int get port => _port;
  bool get isRunning => _server != null;
  String get baseUrl => 'http://127.0.0.1:$_port';

  /// Start the server. Extracts the viewer HTML asset to a temp directory
  /// and serves it alongside any video file paths.
  Future<void> start() async {
    if (_server != null) return;

    final tempDir = await getTemporaryDirectory();
    _assetDir = p.join(tempDir.path, 'dewarp_viewer');
    await Directory(_assetDir!).create(recursive: true);

    final htmlContent = await rootBundle.loadString('assets/dewarp_viewer.html');
    final htmlFile = File(p.join(_assetDir!, 'viewer.html'));
    await htmlFile.writeAsString(htmlContent);

    // Extract Three.js files so the viewer can load them locally.
    for (final jsFile in ['three.module.min.js', 'three.core.min.js']) {
      final bytes = await rootBundle.load('assets/$jsFile');
      final file = File(p.join(_assetDir!, jsFile));
      await file.writeAsBytes(bytes.buffer.asUint8List());
    }

    debugPrint('LocalFileServer: extracted assets to $_assetDir');

    _server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    _port = _server!.port;
    debugPrint('LocalFileServer: listening on port $_port');

    _server!.listen(_handleRequest);
  }

  Future<void> _handleRequest(HttpRequest request) async {
    final reqPath = request.uri.path;
    debugPrint('LocalFileServer: ${request.method} $reqPath');

    // Add CORS headers so video element with crossOrigin works.
    request.response.headers.set('Access-Control-Allow-Origin', '*');
    request.response.headers.set('Access-Control-Allow-Headers', 'Range');
    request.response.headers.set('Access-Control-Expose-Headers', 'Content-Range, Content-Length, Accept-Ranges');

    try {
      if (reqPath == '/viewer.html' || reqPath == '/') {
        await _serveStaticFile(request, 'viewer.html', ContentType.html);
      } else if (reqPath.endsWith('.js')) {
        final filename = reqPath.substring(1); // strip leading /
        await _serveStaticFile(
          request, filename, ContentType.parse('application/javascript'));
      } else if (reqPath.startsWith('/video/')) {
        await _serveVideo(request, reqPath);
      } else {
        request.response.statusCode = HttpStatus.notFound;
        request.response.write('Not found');
      }
    } catch (e) {
      debugPrint('LocalFileServer: error handling $reqPath: $e');
      try {
        request.response.statusCode = HttpStatus.internalServerError;
        request.response.write('Error: $e');
      } catch (_) {
        // Headers already sent, nothing we can do.
      }
    }

    try {
      await request.response.close();
    } catch (_) {}
  }

  Future<void> _serveStaticFile(
      HttpRequest request, String filename, ContentType contentType) async {
    final file = File(p.join(_assetDir!, filename));
    final bytes = await file.readAsBytes();
    request.response.statusCode = HttpStatus.ok;
    request.response.headers.contentType = contentType;
    request.response.headers.contentLength = bytes.length;
    request.response.add(bytes);
  }

  Future<void> _serveVideo(HttpRequest request, String urlPath) async {
    final filePath = Uri.decodeComponent(urlPath.substring('/video/'.length));
    final file = File(filePath);

    if (!await file.exists()) {
      debugPrint('LocalFileServer: file not found: $filePath');
      request.response.statusCode = HttpStatus.notFound;
      request.response.write('File not found: $filePath');
      return;
    }

    final ext = p.extension(filePath).toLowerCase();
    final mimeType = _mimeForExtension(ext);
    final fileLength = await file.length();
    debugPrint('LocalFileServer: serving $filePath ($fileLength bytes)');

    final rangeHeader = request.headers.value('range');
    if (rangeHeader != null && rangeHeader.startsWith('bytes=')) {
      final range = rangeHeader.substring(6);
      final parts = range.split('-');
      final start = int.parse(parts[0]);
      final end =
          parts[1].isNotEmpty ? int.parse(parts[1]) : fileLength - 1;
      final length = end - start + 1;

      request.response.statusCode = HttpStatus.partialContent;
      request.response.headers.contentType = ContentType.parse(mimeType);
      request.response.headers
          .set('Content-Range', 'bytes $start-$end/$fileLength');
      request.response.headers.contentLength = length;
      request.response.headers.set('Accept-Ranges', 'bytes');

      final bytes = await file.openRead(start, end + 1).fold<List<int>>(
        [],
        (prev, chunk) => prev..addAll(chunk),
      );
      request.response.add(bytes);
    } else {
      request.response.statusCode = HttpStatus.ok;
      request.response.headers.contentType = ContentType.parse(mimeType);
      request.response.headers.contentLength = fileLength;
      request.response.headers.set('Accept-Ranges', 'bytes');
      // Stream in chunks to avoid blocking the isolate with large files.
      final raf = await file.open(mode: FileMode.read);
      try {
        const chunkSize = 1024 * 1024; // 1MB chunks
        int remaining = fileLength;
        while (remaining > 0) {
          final toRead = remaining > chunkSize ? chunkSize : remaining;
          final chunk = await raf.read(toRead);
          request.response.add(chunk);
          remaining -= chunk.length;
        }
      } finally {
        await raf.close();
      }
    }
  }

  String _mimeForExtension(String ext) {
    switch (ext) {
      case '.mp4':
        return 'video/mp4';
      case '.webm':
        return 'video/webm';
      case '.mkv':
        return 'video/x-matroska';
      case '.dav':
        return 'video/mp4';
      default:
        return 'application/octet-stream';
    }
  }

  String viewerUrl(String videoFilePath) {
    final encodedPath = Uri.encodeComponent(videoFilePath);
    return '$baseUrl/viewer.html?src=/video/$encodedPath';
  }

  Future<void> stop() async {
    await _server?.close(force: true);
    _server = null;
    _port = 0;
  }
}
