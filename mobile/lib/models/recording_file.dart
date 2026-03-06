/// Represents a single .dav recording file on the Dahua camera.
///
/// Mirrors the RecordingFile model from the soccer-cam Python project.
class RecordingFile {
  const RecordingFile({
    required this.filePath,
    required this.startTime,
    required this.endTime,
    required this.channel,
    this.fileSize = 0,
    this.type = 'dav',
    this.downloadProgress = 0.0,
    this.localPath,
  });

  /// Remote path on camera, e.g. /mnt/sd/2024-01-15/001/dav/12/12.30.00-12.45.00[M][0@0][0].dav
  final String filePath;

  /// Recording start time.
  final DateTime startTime;

  /// Recording end time.
  final DateTime endTime;

  /// Camera channel number.
  final int channel;

  /// File size in bytes (0 if unknown).
  final int fileSize;

  /// File type, typically 'dav'.
  final String type;

  /// Download progress 0.0 to 1.0.
  final double downloadProgress;

  /// Local path after download, null if not yet downloaded.
  final String? localPath;

  /// Duration of this recording segment.
  Duration get duration => endTime.difference(startTime);

  /// File name extracted from the full path.
  String get fileName => filePath.split('/').last;

  /// Whether the file has been downloaded locally.
  bool get isDownloaded => localPath != null;

  /// Parse a recording file from Dahua mediaFileFind response fields.
  ///
  /// The Dahua API returns key=value pairs like:
  ///   FilePath=/mnt/sd/...
  ///   StartTime=2024-01-15 12:30:00
  ///   EndTime=2024-01-15 12:45:00
  ///   Channel=1
  ///   Size=123456789
  ///   Type=dav
  factory RecordingFile.fromDahuaFields(Map<String, String> fields) {
    return RecordingFile(
      filePath: fields['FilePath'] ?? '',
      startTime: _parseDahuaTimestamp(fields['StartTime'] ?? ''),
      endTime: _parseDahuaTimestamp(fields['EndTime'] ?? ''),
      channel: int.tryParse(fields['Channel'] ?? '1') ?? 1,
      fileSize: int.tryParse(fields['Size'] ?? '0') ?? 0,
      type: fields['Type'] ?? 'dav',
    );
  }

  /// Parse Dahua timestamp format: "2024-01-15 12:30:00"
  static DateTime _parseDahuaTimestamp(String timestamp) {
    if (timestamp.isEmpty) return DateTime.now();
    try {
      // Dahua format: YYYY-MM-DD HH:MM:SS
      return DateTime.parse(timestamp.replaceFirst(' ', 'T'));
    } catch (_) {
      return DateTime.now();
    }
  }

  factory RecordingFile.fromJson(Map<String, dynamic> json) {
    return RecordingFile(
      filePath: json['file_path'] as String,
      startTime: DateTime.parse(json['start_time'] as String),
      endTime: DateTime.parse(json['end_time'] as String),
      channel: json['channel'] as int? ?? 1,
      fileSize: json['file_size'] as int? ?? 0,
      type: json['type'] as String? ?? 'dav',
      downloadProgress:
          (json['download_progress'] as num?)?.toDouble() ?? 0.0,
      localPath: json['local_path'] as String?,
    );
  }

  Map<String, dynamic> toJson() {
    return {
      'file_path': filePath,
      'start_time': startTime.toIso8601String(),
      'end_time': endTime.toIso8601String(),
      'channel': channel,
      'file_size': fileSize,
      'type': type,
      'download_progress': downloadProgress,
      'local_path': localPath,
    };
  }

  RecordingFile copyWith({
    String? filePath,
    DateTime? startTime,
    DateTime? endTime,
    int? channel,
    int? fileSize,
    String? type,
    double? downloadProgress,
    String? localPath,
  }) {
    return RecordingFile(
      filePath: filePath ?? this.filePath,
      startTime: startTime ?? this.startTime,
      endTime: endTime ?? this.endTime,
      channel: channel ?? this.channel,
      fileSize: fileSize ?? this.fileSize,
      type: type ?? this.type,
      downloadProgress: downloadProgress ?? this.downloadProgress,
      localPath: localPath ?? this.localPath,
    );
  }

  @override
  String toString() =>
      'RecordingFile($fileName, ${startTime.toIso8601String()} - ${endTime.toIso8601String()})';
}
