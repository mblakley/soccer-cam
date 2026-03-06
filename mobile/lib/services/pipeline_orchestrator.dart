import 'dart:async';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path/path.dart' as p;
import '../models/pipeline_state.dart';
import '../models/video_group.dart';
import '../models/camera_config.dart';
import '../utils/storage_manager.dart';
import 'camera_service.dart';
import 'download_service.dart';
import 'video_processor.dart';
import 'youtube_service.dart';

/// Progress information for a pipeline stage.
class StageProgress {
  const StageProgress({
    required this.stage,
    this.progress = 0.0,
    this.message = '',
    this.elapsed = Duration.zero,
    this.estimatedRemaining,
  });

  final PipelineState stage;
  final double progress;
  final String message;
  final Duration elapsed;
  final Duration? estimatedRemaining;
}

/// Orchestrates the video processing pipeline for a [VideoGroup].
///
/// State machine transitions:
///   pending -> downloading -> downloaded -> combining -> combined
///   -> trimming -> trimmed -> uploading -> complete
///
/// Each group tracks its own state. The orchestrator drives transitions
/// and can be paused, resumed, or cancelled.
class PipelineOrchestrator extends StateNotifier<Map<String, VideoGroup>> {
  PipelineOrchestrator({
    required this.cameraService,
    required this.downloadService,
    required this.videoProcessor,
    required this.youtubeService,
    required this.storageManager,
  }) : super({});

  final CameraService cameraService;
  final DownloadService downloadService;
  final VideoProcessor videoProcessor;
  final YouTubeService youtubeService;
  final StorageManager storageManager;

  /// Stream of progress updates for active stages.
  final _progressController =
      StreamController<StageProgress>.broadcast();
  Stream<StageProgress> get progressStream => _progressController.stream;

  final Set<String> _pausedGroups = {};
  final Set<String> _cancelledGroups = {};
  final Map<String, double> _stageProgress = {};

  /// Current progress (0.0-1.0) for the active stage of each group.
  double getProgress(String groupId) => _stageProgress[groupId] ?? 0.0;

  /// All tracked video groups.
  List<VideoGroup> get groups => state.values.toList();

  /// Add a new video group to track.
  void addGroup(VideoGroup group) {
    state = {...state, group.id: group};
  }

  /// Remove a video group from tracking.
  void removeGroup(String groupId) {
    final newState = Map<String, VideoGroup>.from(state);
    newState.remove(groupId);
    state = newState;
  }

  /// Get a specific group by ID.
  VideoGroup? getGroup(String groupId) => state[groupId];

  /// Update a group's state.
  void _updateGroup(VideoGroup group) {
    state = {...state, group.id: group};
  }

  /// Process a video group through the entire pipeline.
  ///
  /// Starts from the group's current state and advances through
  /// each stage until complete, paused, or an error occurs.
  Future<void> processGroup(String groupId) async {
    var group = state[groupId];
    if (group == null) {
      throw PipelineException('Group not found: $groupId');
    }

    _cancelledGroups.remove(groupId);
    _pausedGroups.remove(groupId);

    try {
      while (group!.state.isActive) {
        if (_cancelledGroups.contains(groupId)) break;
        if (_pausedGroups.contains(groupId)) break;

        final previousState = group.state;
        group = await _executeStage(group);
        _updateGroup(group);

        // If state didn't change, we're waiting for user action (e.g. trim points).
        if (group.state == previousState) break;
      }
    } catch (e) {
      group = group!.copyWith(
        state: PipelineState.error,
        errorMessage: e.toString(),
      );
      _updateGroup(group);
    }
  }

  /// Execute a single pipeline stage and return the updated group.
  Future<VideoGroup> _executeStage(VideoGroup group) async {
    switch (group.state) {
      case PipelineState.pending:
        return _startDownload(group);
      case PipelineState.downloading:
        // Already in progress, skip.
        return group;
      case PipelineState.downloaded:
        return _startCombine(group);
      case PipelineState.combining:
        return group;
      case PipelineState.combined:
        // Wait for user to set trim points before advancing.
        // If trim points are set, proceed; otherwise stay in combined.
        if (group.trimStartSeconds != null) {
          return _startTrim(group);
        }
        return group;
      case PipelineState.trimming:
        return group;
      case PipelineState.trimmed:
        return _startUpload(group);
      case PipelineState.uploading:
        return group;
      case PipelineState.complete:
      case PipelineState.error:
        return group;
    }
  }

  /// Download all files in the group.
  Future<VideoGroup> _startDownload(VideoGroup group) async {
    var updated = group.copyWith(state: PipelineState.downloading);
    _updateGroup(updated);

    _progressController.add(StageProgress(
      stage: PipelineState.downloading,
      message: 'Downloading ${group.fileCount} files...',
    ));

    _stageProgress[group.id] = 0.0;
    updated = await downloadService.downloadGroup(
      updated,
      onProgress: (file, received, total, overallProgress) {
        _stageProgress[group.id] = overallProgress;
        _progressController.add(StageProgress(
          stage: PipelineState.downloading,
          progress: overallProgress,
          message: 'Downloading ${file.fileName}...',
        ));
        // Trigger UI rebuild by updating state.
        state = {...state};
      },
    );

    _stageProgress.remove(group.id);
    return updated.copyWith(state: PipelineState.downloaded);
  }

  /// Combine all downloaded files into a single MP4.
  Future<VideoGroup> _startCombine(VideoGroup group) async {
    var updated = group.copyWith(state: PipelineState.combining);
    _updateGroup(updated);

    final processedDir =
        await storageManager.createGroupProcessedDir(group.id);
    final outputPath = p.join(processedDir.path, 'combined.mp4');

    final inputPaths = group.files
        .where((f) => f.localPath != null)
        .map((f) => f.localPath!)
        .toList();

    if (inputPaths.isEmpty) {
      throw PipelineException('No downloaded files to combine');
    }

    _progressController.add(StageProgress(
      stage: PipelineState.combining,
      message: 'Combining ${inputPaths.length} files...',
    ));

    _stageProgress[group.id] = 0.0;
    await videoProcessor.combineFiles(
      inputPaths,
      outputPath: outputPath,
      onProgress: (progress, elapsed, remaining) {
        _stageProgress[group.id] = progress;
        _progressController.add(StageProgress(
          stage: PipelineState.combining,
          progress: progress,
          elapsed: elapsed,
          estimatedRemaining: remaining,
          message: 'Combining... ${(progress * 100).toStringAsFixed(1)}%',
        ));
        state = {...state};
      },
    );
    _stageProgress.remove(group.id);

    return updated.copyWith(
      state: PipelineState.combined,
      combinedFilePath: outputPath,
    );
  }

  /// Trim the combined file to the user-specified range.
  Future<VideoGroup> _startTrim(VideoGroup group) async {
    if (group.combinedFilePath == null) {
      throw PipelineException('No combined file to trim');
    }
    if (group.trimStartSeconds == null) {
      throw PipelineException('Trim start time not set');
    }

    var updated = group.copyWith(state: PipelineState.trimming);
    _updateGroup(updated);

    final processedDir =
        await storageManager.createGroupProcessedDir(group.id);
    final outputPath = p.join(processedDir.path, 'trimmed.mp4');

    _progressController.add(StageProgress(
      stage: PipelineState.trimming,
      message: 'Trimming video...',
    ));

    await videoProcessor.trimFile(
      group.combinedFilePath!,
      outputPath: outputPath,
      startSeconds: group.trimStartSeconds!,
      endSeconds: group.trimEndSeconds,
      onProgress: (progress, elapsed, remaining) {
        _progressController.add(StageProgress(
          stage: PipelineState.trimming,
          progress: progress,
          elapsed: elapsed,
          estimatedRemaining: remaining,
          message: 'Trimming... ${(progress * 100).toStringAsFixed(1)}%',
        ));
      },
    );

    return updated.copyWith(
      state: PipelineState.trimmed,
      trimmedFilePath: outputPath,
    );
  }

  /// Upload the trimmed file to YouTube.
  Future<VideoGroup> _startUpload(VideoGroup group) async {
    final uploadPath = group.trimmedFilePath ?? group.combinedFilePath;
    if (uploadPath == null) {
      throw PipelineException('No file to upload');
    }

    if (!youtubeService.isAuthenticated) {
      throw PipelineException(
        'Not signed in to YouTube. Please sign in first.',
      );
    }

    var updated = group.copyWith(state: PipelineState.uploading);
    _updateGroup(updated);

    _progressController.add(StageProgress(
      stage: PipelineState.uploading,
      message: 'Uploading to YouTube...',
    ));

    final videoId = await youtubeService.uploadVideo(
      filePath: uploadPath,
      title: group.name,
      description: 'Soccer game recorded on '
          '${group.startTime.toIso8601String().split('T').first}',
      privacyStatus: 'unlisted',
      tags: ['soccer', 'game', 'recording'],
      onProgress: (bytesSent, totalBytes, progress) {
        _progressController.add(StageProgress(
          stage: PipelineState.uploading,
          progress: progress,
          message: 'Uploading... ${(progress * 100).toStringAsFixed(1)}%',
        ));
      },
    );

    return updated.copyWith(
      state: PipelineState.complete,
      youtubeVideoId: videoId,
    );
  }

  /// Set trim points for a video group and resume processing.
  Future<void> setTrimPoints(String groupId, double startSeconds, double? endSeconds) async {
    final group = state[groupId];
    if (group == null) return;

    _updateGroup(group.copyWith(
      trimStartSeconds: startSeconds,
      trimEndSeconds: endSeconds,
    ));

    // Resume pipeline if the group was waiting at combined state.
    if (group.state == PipelineState.combined) {
      await processGroup(groupId);
    }
  }

  /// Skip trimming and proceed directly to upload.
  Future<void> skipTrim(String groupId) async {
    final group = state[groupId];
    if (group == null || group.state != PipelineState.combined) return;

    _updateGroup(group.copyWith(state: PipelineState.trimmed));
    await processGroup(groupId);
  }

  /// Pause processing for a group.
  void pauseGroup(String groupId) {
    _pausedGroups.add(groupId);
  }

  /// Resume processing for a paused group.
  void resumeGroup(String groupId) {
    _pausedGroups.remove(groupId);
    processGroup(groupId);
  }

  /// Cancel processing for a group.
  void cancelGroup(String groupId) {
    _cancelledGroups.add(groupId);
    downloadService.cancelGroupDownload(groupId);
  }

  /// Retry a group that is in error state.
  Future<void> retryGroup(String groupId) async {
    final group = state[groupId];
    if (group == null || group.state != PipelineState.error) return;

    _updateGroup(group.copyWith(
      state: PipelineState.pending,
      errorMessage: null,
    ));

    await processGroup(groupId);
  }

  /// Clean up files for a completed group.
  Future<void> cleanupGroup(String groupId) async {
    await storageManager.cleanupGroup(groupId);
    removeGroup(groupId);
  }

  @override
  void dispose() {
    _progressController.close();
    super.dispose();
  }
}

/// Exception for pipeline errors.
class PipelineException implements Exception {
  const PipelineException(this.message);
  final String message;

  @override
  String toString() => 'PipelineException: $message';
}

// --- Riverpod Providers ---

final cameraConfigProvider = StateProvider<CameraConfig?>((ref) => null);

final cameraServiceProvider = Provider<CameraService>((ref) {
  final config = ref.watch(cameraConfigProvider);
  if (config == null) {
    return CameraService.create(
      config: const CameraConfig(
        host: 'unconfigured',
        username: '',
        password: '',
      ),
    );
  }
  return CameraService.create(config: config);
});

final downloadServiceProvider = Provider<DownloadService>((ref) {
  return DownloadService(
    cameraService: ref.watch(cameraServiceProvider),
    storageManager: StorageManager.instance,
  );
});

final videoProcessorProvider = Provider<VideoProcessor>((ref) {
  return VideoProcessor(storageManager: StorageManager.instance);
});

final youtubeServiceProvider = Provider<YouTubeService>((ref) {
  return YouTubeService();
});

final pipelineProvider =
    StateNotifierProvider<PipelineOrchestrator, Map<String, VideoGroup>>((ref) {
  return PipelineOrchestrator(
    cameraService: ref.watch(cameraServiceProvider),
    downloadService: ref.watch(downloadServiceProvider),
    videoProcessor: ref.watch(videoProcessorProvider),
    youtubeService: ref.watch(youtubeServiceProvider),
    storageManager: StorageManager.instance,
  );
});
