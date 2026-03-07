import 'dart:io';
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
  /// and serves it alongside any video file paths registered via [addVideoPath].
  Future<void> start() async {
    if (_server != null) return;

    // Extract HTML asset to temp dir so HttpServer can serve it.
    final tempDir = await getTemporaryDirectory();
    _assetDir = p.join(tempDir.path, 'dewarp_viewer');
    await Directory(_assetDir!).create(recursive: true);

    final htmlContent = await rootBundle.loadString('assets/dewarp_viewer.html');
    await File(p.join(_assetDir!, 'viewer.html')).writeAsString(htmlContent);

    _server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    _port = _server!.port;

    _server!.listen(_handleRequest);
  }

  Future<void> _handleRequest(HttpRequest request) async {
    final path = request.uri.path;

    try {
      if (path == '/viewer.html' || path == '/') {
        // Serve the HTML viewer.
        final file = File(p.join(_assetDir!, 'viewer.html'));
        request.response.headers.contentType = ContentType.html;
        await request.response.addStream(file.openRead());
      } else if (path.startsWith('/video/')) {
        // Serve a video file by absolute path encoded in the URL.
        // URL format: /video/<url-encoded-absolute-path>
        final filePath = Uri.decodeComponent(path.substring('/video/'.length));
        final file = File(filePath);
        if (!await file.exists()) {
          request.response.statusCode = HttpStatus.notFound;
          request.response.write('File not found');
        } else {
          final ext = p.extension(filePath).toLowerCase();
          final mimeType = _mimeForExtension(ext);
          request.response.headers.contentType = ContentType.parse(mimeType);

          // Support range requests for video seeking.
          final fileLength = await file.length();
          final rangeHeader = request.headers.value('range');
          if (rangeHeader != null && rangeHeader.startsWith('bytes=')) {
            final range = rangeHeader.substring(6);
            final parts = range.split('-');
            final start = int.parse(parts[0]);
            final end = parts[1].isNotEmpty ? int.parse(parts[1]) : fileLength - 1;

            request.response.statusCode = HttpStatus.partialContent;
            request.response.headers.set('Content-Range', 'bytes $start-$end/$fileLength');
            request.response.headers.contentLength = end - start + 1;
            request.response.headers.set('Accept-Ranges', 'bytes');

            await request.response.addStream(
              file.openRead(start, end + 1),
            );
          } else {
            request.response.headers.contentLength = fileLength;
            request.response.headers.set('Accept-Ranges', 'bytes');
            await request.response.addStream(file.openRead());
          }
        }
      } else {
        request.response.statusCode = HttpStatus.notFound;
        request.response.write('Not found');
      }
    } catch (e) {
      request.response.statusCode = HttpStatus.internalServerError;
      request.response.write('Error: $e');
    }

    await request.response.close();
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

  /// Build a viewer URL for a given video file path.
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
