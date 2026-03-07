import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';
import '../services/local_file_server.dart';

/// Full-screen dewarped video viewer using a WebView + Three.js half-cylinder
/// geometry. Corrects the Dahua cylindrical projection to rectilinear so
/// straight lines in the real world appear straight on screen.
///
/// Supports pan (drag) and zoom (pinch) across the 180-degree FOV.
class DewarpViewerScreen extends StatefulWidget {
  const DewarpViewerScreen({super.key, required this.videoPath});

  final String videoPath;

  @override
  State<DewarpViewerScreen> createState() => _DewarpViewerScreenState();
}

class _DewarpViewerScreenState extends State<DewarpViewerScreen> {
  final _server = LocalFileServer.instance;
  WebViewController? _controller;
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _initViewer();
  }

  Future<void> _initViewer() async {
    try {
      // Start the local file server if not already running.
      if (!_server.isRunning) {
        await _server.start();
      }

      final url = _server.viewerUrl(widget.videoPath);
      debugPrint('DewarpViewer: loading URL: $url');

      final controller = WebViewController()
        ..setJavaScriptMode(JavaScriptMode.unrestricted)
        ..setBackgroundColor(Colors.black)
        ..setOnConsoleMessage((message) {
          debugPrint('DewarpViewer JS [${message.level.name}]: ${message.message}');
        })
        ..setNavigationDelegate(
          NavigationDelegate(
            onPageFinished: (_) {
              if (mounted) setState(() => _loading = false);
            },
            onWebResourceError: (error) {
              debugPrint('DewarpViewer: resource error: ${error.description} (isForMainFrame: ${error.isForMainFrame})');
              // Only show error UI for main frame failures, not sub-resources.
              if (error.isForMainFrame ?? false) {
                if (mounted) {
                  setState(() {
                    _error = 'WebView error: ${error.description}';
                    _loading = false;
                  });
                }
              }
            },
          ),
        )
        ..loadRequest(Uri.parse(url));

      if (mounted) {
        setState(() => _controller = controller);
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = 'Failed to start viewer: $e';
          _loading = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        title: const Text('Dewarped View'),
        backgroundColor: Colors.black,
        foregroundColor: Colors.white,
      ),
      body: _buildBody(),
    );
  }

  Widget _buildBody() {
    if (_error != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              const Icon(Icons.error_outline, color: Colors.red, size: 48),
              const SizedBox(height: 16),
              Text(
                _error!,
                style: const TextStyle(color: Colors.white),
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 16),
              ElevatedButton(
                onPressed: () {
                  setState(() {
                    _error = null;
                    _loading = true;
                  });
                  _initViewer();
                },
                child: const Text('Retry'),
              ),
            ],
          ),
        ),
      );
    }

    return Stack(
      children: [
        if (_controller != null)
          WebViewWidget(controller: _controller!),
        if (_loading)
          const Center(
            child: CircularProgressIndicator(color: Colors.white),
          ),
      ],
    );
  }
}
