import 'pipeline_state.dart';
import 'recording_file.dart';

/// A group of recording files that form a single game/session.
///
/// Files are grouped by temporal proximity (gaps < threshold = same group).
/// Mirrors the video group concept from soccer-cam's directory_state.py.
class VideoGroup {
  const VideoGroup({
    required this.id,
    required this.name,
    required this.files,
    this.state = PipelineState.pending,
    this.combinedFilePath,
    this.trimmedFilePath,
    this.trimStartSeconds,
    this.trimEndSeconds,
    this.youtubeVideoId,
    this.errorMessage,
    this.createdAt,
    this.updatedAt,
  });

  /// Unique identifier for this group.
  final String id;

  /// Display name, typically derived from date/time.
  final String name;

  /// Recording files in this group, ordered by start time.
  final List<RecordingFile> files;

  /// Current pipeline state.
  final PipelineState state;

  /// Path to the combined MP4 file.
  final String? combinedFilePath;

  /// Path to the trimmed MP4 file.
  final String? trimmedFilePath;

  /// Trim start position in seconds.
  final double? trimStartSeconds;

  /// Trim end position in seconds.
  final double? trimEndSeconds;

  /// YouTube video ID after upload.
  final String? youtubeVideoId;

  /// Error message if state is error.
  final String? errorMessage;

  /// When this group was first discovered.
  final DateTime? createdAt;

  /// When this group was last updated.
  final DateTime? updatedAt;

  /// Earliest start time across all files.
  DateTime get startTime {
    if (files.isEmpty) return DateTime.now();
    return files
        .map((f) => f.startTime)
        .reduce((a, b) => a.isBefore(b) ? a : b);
  }

  /// Latest end time across all files.
  DateTime get endTime {
    if (files.isEmpty) return DateTime.now();
    return files.map((f) => f.endTime).reduce((a, b) => a.isAfter(b) ? a : b);
  }

  /// Total duration across all files.
  Duration get totalDuration => endTime.difference(startTime);

  /// Total file size in bytes across all files.
  int get totalFileSize => files.fold(0, (sum, f) => sum + f.fileSize);

  /// Number of files in the group.
  int get fileCount => files.length;

  /// Average download progress across all files.
  double get downloadProgress {
    if (files.isEmpty) return 0.0;
    final total = files.fold(0.0, (sum, f) => sum + f.downloadProgress);
    return total / files.length;
  }

  /// Whether all files have been downloaded.
  bool get allFilesDownloaded => files.every((f) => f.isDownloaded);

  /// Group recording files by temporal proximity.
  ///
  /// Files with gaps less than [maxGapMinutes] are considered part of
  /// the same group.
  static List<VideoGroup> groupByTime(
    List<RecordingFile> files, {
    int maxGapMinutes = 5,
    String Function(int index, DateTime startTime)? nameGenerator,
  }) {
    if (files.isEmpty) return [];

    final sorted = List<RecordingFile>.from(files)
      ..sort((a, b) => a.startTime.compareTo(b.startTime));

    final groups = <VideoGroup>[];
    var currentGroupFiles = <RecordingFile>[sorted.first];

    for (var i = 1; i < sorted.length; i++) {
      final gap = sorted[i]
          .startTime
          .difference(sorted[i - 1].endTime)
          .inMinutes
          .abs();

      if (gap <= maxGapMinutes) {
        currentGroupFiles.add(sorted[i]);
      } else {
        // Start a new group.
        final groupIndex = groups.length;
        final groupStart = currentGroupFiles.first.startTime;
        final groupName = nameGenerator != null
            ? nameGenerator(groupIndex, groupStart)
            : _defaultGroupName(groupStart);

        groups.add(VideoGroup(
          id: '${groupStart.millisecondsSinceEpoch}',
          name: groupName,
          files: List.unmodifiable(currentGroupFiles),
          createdAt: DateTime.now(),
        ));
        currentGroupFiles = [sorted[i]];
      }
    }

    // Add the final group.
    final groupIndex = groups.length;
    final groupStart = currentGroupFiles.first.startTime;
    final groupName = nameGenerator != null
        ? nameGenerator(groupIndex, groupStart)
        : _defaultGroupName(groupStart);

    groups.add(VideoGroup(
      id: '${groupStart.millisecondsSinceEpoch}',
      name: groupName,
      files: List.unmodifiable(currentGroupFiles),
      createdAt: DateTime.now(),
    ));

    return groups;
  }

  static String _defaultGroupName(DateTime startTime) {
    final date =
        '${startTime.year}-${startTime.month.toString().padLeft(2, '0')}-${startTime.day.toString().padLeft(2, '0')}';
    final time =
        '${startTime.hour.toString().padLeft(2, '0')}:${startTime.minute.toString().padLeft(2, '0')}';
    return 'Game $date $time';
  }

  factory VideoGroup.fromJson(Map<String, dynamic> json) {
    return VideoGroup(
      id: json['id'] as String,
      name: json['name'] as String,
      files: (json['files'] as List<dynamic>)
          .map((f) => RecordingFile.fromJson(f as Map<String, dynamic>))
          .toList(),
      state: PipelineState.values.firstWhere(
        (s) => s.name == json['state'],
        orElse: () => PipelineState.pending,
      ),
      combinedFilePath: json['combined_file_path'] as String?,
      trimmedFilePath: json['trimmed_file_path'] as String?,
      trimStartSeconds: (json['trim_start_seconds'] as num?)?.toDouble(),
      trimEndSeconds: (json['trim_end_seconds'] as num?)?.toDouble(),
      youtubeVideoId: json['youtube_video_id'] as String?,
      errorMessage: json['error_message'] as String?,
      createdAt: json['created_at'] != null
          ? DateTime.parse(json['created_at'] as String)
          : null,
      updatedAt: json['updated_at'] != null
          ? DateTime.parse(json['updated_at'] as String)
          : null,
    );
  }

  Map<String, dynamic> toJson() {
    return {
      'id': id,
      'name': name,
      'files': files.map((f) => f.toJson()).toList(),
      'state': state.name,
      'combined_file_path': combinedFilePath,
      'trimmed_file_path': trimmedFilePath,
      'trim_start_seconds': trimStartSeconds,
      'trim_end_seconds': trimEndSeconds,
      'youtube_video_id': youtubeVideoId,
      'error_message': errorMessage,
      'created_at': createdAt?.toIso8601String(),
      'updated_at': updatedAt?.toIso8601String(),
    };
  }

  VideoGroup copyWith({
    String? id,
    String? name,
    List<RecordingFile>? files,
    PipelineState? state,
    String? combinedFilePath,
    String? trimmedFilePath,
    double? trimStartSeconds,
    double? trimEndSeconds,
    String? youtubeVideoId,
    String? errorMessage,
    DateTime? createdAt,
    DateTime? updatedAt,
  }) {
    return VideoGroup(
      id: id ?? this.id,
      name: name ?? this.name,
      files: files ?? this.files,
      state: state ?? this.state,
      combinedFilePath: combinedFilePath ?? this.combinedFilePath,
      trimmedFilePath: trimmedFilePath ?? this.trimmedFilePath,
      trimStartSeconds: trimStartSeconds ?? this.trimStartSeconds,
      trimEndSeconds: trimEndSeconds ?? this.trimEndSeconds,
      youtubeVideoId: youtubeVideoId ?? this.youtubeVideoId,
      errorMessage: errorMessage ?? this.errorMessage,
      createdAt: createdAt ?? this.createdAt,
      updatedAt: updatedAt ?? DateTime.now(),
    );
  }

  @override
  String toString() =>
      'VideoGroup($name, ${files.length} files, state=$state)';
}
