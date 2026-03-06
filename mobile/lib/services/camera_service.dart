import 'dart:async';
import 'package:dio/dio.dart';
import '../models/camera_config.dart';
import '../models/recording_file.dart';
import '../utils/digest_auth.dart';

/// Service for communicating with Dahua IP cameras.
///
/// Implements the Dahua HTTP API for:
/// - Availability checks via recordManager.cgi
/// - File listing via mediaFileFind.cgi (create/findFile/findNextFile/close/destroy)
/// - File download via RPC_Loadfile
///
/// Uses HTTP Digest authentication.
class CameraService {
  CameraService({required this.config}) {
    _dio = Dio(BaseOptions(
      baseUrl: config.baseUrl,
      connectTimeout: Duration(seconds: config.connectTimeoutSeconds),
      receiveTimeout: Duration(seconds: config.downloadTimeoutSeconds),
    ));
    _dio.interceptors.add(DigestAuthInterceptor(
      username: config.username,
      password: config.password,
    ));
  }

  final CameraConfig config;
  late final Dio _dio;

  /// Check if the camera is reachable and responding.
  ///
  /// Uses the recordManager getCaps endpoint.
  /// Returns true if the camera responds successfully.
  Future<bool> checkAvailability() async {
    try {
      final response = await _dio.get(
        '/cgi-bin/recordManager.cgi',
        queryParameters: {'action': 'getCaps'},
      );
      return response.statusCode == 200;
    } on DioException {
      return false;
    }
  }

  /// List recording files on the camera within the given time range.
  ///
  /// Uses the Dahua mediaFileFind protocol:
  /// 1. factory.create -> get object ID
  /// 2. startFind with conditions (channel, time range, type)
  /// 3. findNextFile in a loop until no more results
  /// 4. close and destroy the search object
  Future<List<RecordingFile>> listFiles({
    required DateTime startTime,
    required DateTime endTime,
    int? channel,
  }) async {
    final ch = channel ?? config.channel;
    final objectId = await _createFindObject();
    if (objectId == null) {
      throw CameraException('Failed to create mediaFileFind object');
    }

    try {
      await _startFind(
        objectId: objectId,
        channel: ch,
        startTime: startTime,
        endTime: endTime,
      );

      final files = <RecordingFile>[];
      var hasMore = true;

      while (hasMore) {
        final result = await _findNextFiles(objectId: objectId, count: 100);
        if (result.isEmpty) {
          hasMore = false;
        } else {
          files.addAll(result);
        }
      }

      await _closeFindObject(objectId);
      await _destroyFindObject(objectId);

      return files;
    } catch (e) {
      // Try to clean up the find object on error.
      try {
        await _closeFindObject(objectId);
        await _destroyFindObject(objectId);
      } catch (_) {}
      rethrow;
    }
  }

  /// Download a recording file from the camera.
  ///
  /// Uses the RPC_Loadfile endpoint for streaming download.
  /// [onProgress] is called with (received, total) bytes.
  /// Returns the raw response stream for the caller to write to disk.
  Future<Response<dynamic>> downloadFile(
    RecordingFile file, {
    String? savePath,
    void Function(int received, int total)? onProgress,
    CancelToken? cancelToken,
  }) async {
    final downloadPath = '/cgi-bin/RPC_Loadfile${file.filePath}';

    if (savePath != null) {
      return _dio.download(
        downloadPath,
        savePath,
        onReceiveProgress: onProgress,
        cancelToken: cancelToken,
      );
    }

    return _dio.get(
      downloadPath,
      options: Options(responseType: ResponseType.stream),
      cancelToken: cancelToken,
    );
  }

  /// Take a snapshot from the camera.
  ///
  /// Returns the image bytes.
  Future<List<int>> takeSnapshot({int? channel}) async {
    final ch = channel ?? config.channel;
    final response = await _dio.get<List<int>>(
      '/cgi-bin/snapshot.cgi',
      queryParameters: {'channel': ch},
      options: Options(responseType: ResponseType.bytes),
    );
    return response.data ?? [];
  }

  // --- Private Dahua mediaFileFind protocol methods ---

  /// Step 1: Create a mediaFileFind object.
  /// Returns the object ID.
  Future<int?> _createFindObject() async {
    final response = await _dio.get(
      '/cgi-bin/mediaFileFind.cgi',
      queryParameters: {'action': 'factory.create'},
    );
    return _parseObjectId(response.data.toString());
  }

  /// Step 2: Start a file search with conditions.
  Future<bool> _startFind({
    required int objectId,
    required int channel,
    required DateTime startTime,
    required DateTime endTime,
  }) async {
    final startStr = _formatDahuaTime(startTime);
    final endStr = _formatDahuaTime(endTime);

    final response = await _dio.get(
      '/cgi-bin/mediaFileFind.cgi',
      queryParameters: {
        'action': 'findFile',
        'object': objectId,
        'condition.Channel': channel,
        'condition.StartTime': startStr,
        'condition.EndTime': endStr,
        'condition.Types[0]': 'dav',
        'condition.Flags[0]': 'Event',
      },
    );

    final body = response.data.toString();
    return body.contains('OK') || body.contains('true');
  }

  /// Step 3: Fetch the next batch of found files.
  Future<List<RecordingFile>> _findNextFiles({
    required int objectId,
    int count = 100,
  }) async {
    final response = await _dio.get(
      '/cgi-bin/mediaFileFind.cgi',
      queryParameters: {
        'action': 'findNextFile',
        'object': objectId,
        'count': count,
      },
    );

    final body = response.data.toString();
    return _parseFileList(body);
  }

  /// Step 4: Close the find session.
  Future<void> _closeFindObject(int objectId) async {
    await _dio.get(
      '/cgi-bin/mediaFileFind.cgi',
      queryParameters: {
        'action': 'close',
        'object': objectId,
      },
    );
  }

  /// Step 5: Destroy the find object.
  Future<void> _destroyFindObject(int objectId) async {
    await _dio.get(
      '/cgi-bin/mediaFileFind.cgi',
      queryParameters: {
        'action': 'destroy',
        'object': objectId,
      },
    );
  }

  // --- Response parsers ---

  /// Parse object ID from factory.create response.
  ///
  /// Response format: "result=<id>"
  int? _parseObjectId(String responseBody) {
    final match = RegExp(r'result=(\d+)').firstMatch(responseBody);
    if (match != null) {
      return int.tryParse(match.group(1)!);
    }
    return null;
  }

  /// Parse file list from findNextFile response.
  ///
  /// Response format is key=value pairs like:
  ///   found=3
  ///   items[0].Channel=1
  ///   items[0].StartTime=2024-01-15 12:30:00
  ///   items[0].EndTime=2024-01-15 12:45:00
  ///   items[0].FilePath=/mnt/sd/...
  ///   items[0].Size=123456789
  ///   items[0].Type=dav
  List<RecordingFile> _parseFileList(String responseBody) {
    final lines = responseBody.split('\n').map((l) => l.trim()).toList();

    // Check if any files were found.
    final foundLine = lines.firstWhere(
      (l) => l.startsWith('found='),
      orElse: () => 'found=0',
    );
    final found = int.tryParse(foundLine.split('=').last) ?? 0;
    if (found == 0) return [];

    // Group lines by item index.
    final itemFields = <int, Map<String, String>>{};
    final itemPattern = RegExp(r'items\[(\d+)\]\.(\w+)=(.+)');

    for (final line in lines) {
      final match = itemPattern.firstMatch(line);
      if (match != null) {
        final index = int.parse(match.group(1)!);
        final key = match.group(2)!;
        final value = match.group(3)!.trim();
        itemFields.putIfAbsent(index, () => {});
        itemFields[index]![key] = value;
      }
    }

    return itemFields.values
        .map((fields) => RecordingFile.fromDahuaFields(fields))
        .where((f) => f.filePath.isNotEmpty)
        .toList();
  }

  /// Format a DateTime for Dahua API queries.
  ///
  /// Dahua expects: "YYYY-MM-DD HH:MM:SS"
  String _formatDahuaTime(DateTime dt) {
    return '${dt.year}-'
        '${dt.month.toString().padLeft(2, '0')}-'
        '${dt.day.toString().padLeft(2, '0')} '
        '${dt.hour.toString().padLeft(2, '0')}:'
        '${dt.minute.toString().padLeft(2, '0')}:'
        '${dt.second.toString().padLeft(2, '0')}';
  }

  /// Dispose resources.
  void dispose() {
    _dio.close();
  }
}

/// Exception for camera communication errors.
class CameraException implements Exception {
  const CameraException(this.message);
  final String message;

  @override
  String toString() => 'CameraException: $message';
}
