import 'package:dio/dio.dart';
import '../models/camera_config.dart';
import '../models/recording_file.dart';
import '../utils/digest_auth.dart';

/// Abstract interface for camera communication services.
///
/// Implemented by [DahuaCameraService] and [ReolinkCameraService].
abstract class CameraService {
  CameraService({required this.config});

  final CameraConfig config;

  /// Check if the camera is reachable and responding.
  Future<bool> checkAvailability();

  /// List recording files on the camera within the given time range.
  Future<List<RecordingFile>> listFiles({
    required DateTime startTime,
    required DateTime endTime,
    int? channel,
  });

  /// Download a recording file from the camera.
  ///
  /// [onProgress] is called with (received, total) bytes.
  Future<Response<dynamic>> downloadFile(
    RecordingFile file, {
    String? savePath,
    void Function(int received, int total)? onProgress,
    CancelToken? cancelToken,
  });

  /// Dispose resources.
  void dispose();

  /// Factory that creates the correct service based on camera type.
  factory CameraService.create({required CameraConfig config}) {
    switch (config.cameraType) {
      case CameraType.dahua:
        return DahuaCameraService(config: config);
      case CameraType.reolink:
        return ReolinkCameraService(config: config);
    }
  }
}

/// Exception for camera communication errors.
class CameraException implements Exception {
  const CameraException(this.message);
  final String message;

  @override
  String toString() => 'CameraException: $message';
}

// ── Dahua Implementation ─────────────────────────────────────────────

/// Service for communicating with Dahua IP cameras.
///
/// Implements the Dahua HTTP API:
/// - Availability checks via recordManager.cgi
/// - File listing via mediaFileFind.cgi (create/findFile/findNextFile/close/destroy)
/// - File download via RPC_Loadfile
/// - HTTP Digest authentication
class DahuaCameraService extends CameraService {
  DahuaCameraService({required super.config}) {
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

  late final Dio _dio;

  @override
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

  @override
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
      try {
        await _closeFindObject(objectId);
        await _destroyFindObject(objectId);
      } catch (_) {}
      rethrow;
    }
  }

  @override
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

  /// Take a snapshot from the camera (Dahua-specific).
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

  Future<int?> _createFindObject() async {
    final response = await _dio.get(
      '/cgi-bin/mediaFileFind.cgi',
      queryParameters: {'action': 'factory.create'},
    );
    return _parseObjectId(response.data.toString());
  }

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

  Future<void> _closeFindObject(int objectId) async {
    await _dio.get(
      '/cgi-bin/mediaFileFind.cgi',
      queryParameters: {'action': 'close', 'object': objectId},
    );
  }

  Future<void> _destroyFindObject(int objectId) async {
    await _dio.get(
      '/cgi-bin/mediaFileFind.cgi',
      queryParameters: {'action': 'destroy', 'object': objectId},
    );
  }

  int? _parseObjectId(String responseBody) {
    final match = RegExp(r'result=(\d+)').firstMatch(responseBody);
    if (match != null) {
      return int.tryParse(match.group(1)!);
    }
    return null;
  }

  List<RecordingFile> _parseFileList(String responseBody) {
    final lines = responseBody.split('\n').map((l) => l.trim()).toList();

    final foundLine = lines.firstWhere(
      (l) => l.startsWith('found='),
      orElse: () => 'found=0',
    );
    final found = int.tryParse(foundLine.split('=').last) ?? 0;
    if (found == 0) return [];

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

  String _formatDahuaTime(DateTime dt) {
    return '${dt.year}-'
        '${dt.month.toString().padLeft(2, '0')}-'
        '${dt.day.toString().padLeft(2, '0')} '
        '${dt.hour.toString().padLeft(2, '0')}:'
        '${dt.minute.toString().padLeft(2, '0')}:'
        '${dt.second.toString().padLeft(2, '0')}';
  }

  @override
  void dispose() {
    _dio.close();
  }
}

// ── ReoLink Implementation ───────────────────────────────────────────

/// Service for communicating with ReoLink IP cameras.
///
/// Implements the ReoLink HTTP JSON API:
/// - Token-based authentication (Login command)
/// - Availability checks via GetDevInfo
/// - File listing via Search command
/// - File download via Download command with token auth
class ReolinkCameraService extends CameraService {
  ReolinkCameraService({required super.config}) {
    _dio = Dio(BaseOptions(
      baseUrl: config.baseUrl,
      connectTimeout: Duration(seconds: config.connectTimeoutSeconds),
      receiveTimeout: Duration(seconds: config.downloadTimeoutSeconds),
    ));
  }

  late final Dio _dio;
  String? _token;
  DateTime? _tokenExpiry;

  // ── Token management ────────────────────────────────────────────

  Future<bool> _login() async {
    try {
      final response = await _dio.post(
        '/cgi-bin/api.cgi',
        queryParameters: {'cmd': 'Login', 'token': 'null'},
        data: [
          {
            'cmd': 'Login',
            'action': 0,
            'param': {
              'User': {
                'userName': config.username,
                'password': config.password,
              },
            },
          },
        ],
      );

      if (response.statusCode != 200) return false;

      final data = response.data as List;
      if (data.isEmpty || data[0]['code'] != 0) return false;

      final tokenInfo = data[0]['value']['Token'];
      _token = tokenInfo['name'] as String;
      final leaseTime = (tokenInfo['leaseTime'] as int?) ?? 3600;
      _tokenExpiry = DateTime.now().add(Duration(seconds: leaseTime - 60));
      return true;
    } on DioException {
      return false;
    }
  }

  Future<bool> _ensureToken() async {
    if (_token != null &&
        _tokenExpiry != null &&
        DateTime.now().isBefore(_tokenExpiry!)) {
      return true;
    }
    return _login();
  }

  Future<List<dynamic>?> _apiCall(
    String cmd,
    Map<String, dynamic> param, {
    int action = 0,
  }) async {
    if (!await _ensureToken()) return null;

    final response = await _dio.post(
      '/cgi-bin/api.cgi',
      queryParameters: {'cmd': cmd, 'token': _token},
      data: [
        {'cmd': cmd, 'action': action, 'param': param},
      ],
    );

    if (response.statusCode != 200) return null;
    final data = response.data;
    if (data == null || (data is List && data.isEmpty)) return null;
    return data as List;
  }

  static Map<String, int> _dateTimeToReolink(DateTime dt) {
    return {
      'year': dt.year,
      'mon': dt.month,
      'day': dt.day,
      'hour': dt.hour,
      'min': dt.minute,
      'sec': dt.second,
    };
  }

  static String _reolinkTimeToString(Map<String, dynamic> t) {
    return '${(t['year'] as int).toString().padLeft(4, '0')}-'
        '${(t['mon'] as int).toString().padLeft(2, '0')}-'
        '${(t['day'] as int).toString().padLeft(2, '0')} '
        '${(t['hour'] as int).toString().padLeft(2, '0')}:'
        '${(t['min'] as int).toString().padLeft(2, '0')}:'
        '${(t['sec'] as int).toString().padLeft(2, '0')}';
  }

  // ── CameraService implementation ────────────────────────────────

  @override
  Future<bool> checkAvailability() async {
    try {
      final data = await _apiCall(
        'GetDevInfo',
        {'DevInfo': {'channel': config.channel}},
      );
      return data != null && data[0]['code'] == 0;
    } on DioException {
      return false;
    } catch (_) {
      return false;
    }
  }

  @override
  Future<List<RecordingFile>> listFiles({
    required DateTime startTime,
    required DateTime endTime,
    int? channel,
  }) async {
    final ch = channel ?? config.channel;
    final data = await _apiCall(
      'Search',
      {
        'Search': {
          'channel': ch,
          'onlyStatus': 0,
          'streamType': 'main',
          'StartTime': _dateTimeToReolink(startTime),
          'EndTime': _dateTimeToReolink(endTime),
        },
      },
      action: 1,
    );

    if (data == null) return [];
    final resp = data[0] as Map<String, dynamic>;
    if (resp['code'] != 0) return [];

    final searchResult =
        (resp['value'] as Map<String, dynamic>?)?['SearchResult']
            as Map<String, dynamic>?;
    if (searchResult == null) return [];

    final rawFiles = searchResult['File'] as List? ?? [];
    return rawFiles.map((f) {
      final startMap = f['StartTime'] as Map<String, dynamic>;
      final endMap = f['EndTime'] as Map<String, dynamic>;
      return RecordingFile.fromReolinkJson(
        filePath: f['name'] as String? ?? '',
        startTimeStr: _reolinkTimeToString(startMap),
        endTimeStr: _reolinkTimeToString(endMap),
        fileSize: f['size'] as int? ?? 0,
        channel: ch,
      );
    }).toList();
  }

  @override
  Future<Response<dynamic>> downloadFile(
    RecordingFile file, {
    String? savePath,
    void Function(int received, int total)? onProgress,
    CancelToken? cancelToken,
  }) async {
    if (!await _ensureToken()) {
      throw CameraException('Failed to authenticate with ReoLink camera');
    }

    final queryParams = {
      'cmd': 'Download',
      'source': file.filePath,
      'output': file.filePath,
      'token': _token,
    };

    if (savePath != null) {
      return _dio.download(
        '/cgi-bin/api.cgi',
        savePath,
        queryParameters: queryParams,
        onReceiveProgress: onProgress,
        cancelToken: cancelToken,
      );
    }

    return _dio.get(
      '/cgi-bin/api.cgi',
      queryParameters: queryParams,
      options: Options(responseType: ResponseType.stream),
      cancelToken: cancelToken,
    );
  }

  @override
  void dispose() {
    _dio.close();
  }
}
