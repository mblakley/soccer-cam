import 'dart:async';
import 'dart:io';
import 'package:dio/dio.dart';
import 'package:path/path.dart' as p;
import '../models/recording_file.dart';
import '../models/video_group.dart';
import '../utils/storage_manager.dart';
import 'camera_service.dart';

/// Callback for download progress updates.
typedef DownloadProgressCallback = void Function(
  RecordingFile file,
  int received,
  int total,
  double overallProgress,
);

/// Service for downloading recording files from the camera.
///
/// Handles chunked downloads with progress tracking, cancellation,
/// and resume support. Downloads all files in a video group sequentially.
class DownloadService {
  DownloadService({
    required this.cameraService,
    required this.storageManager,
  });

  final CameraService cameraService;
  final StorageManager storageManager;

  final Map<String, CancelToken> _activeCancelTokens = {};

  /// Download all files in a video group.
  ///
  /// Returns an updated [VideoGroup] with local paths set on each file.
  /// Calls [onProgress] with download progress updates.
  /// Throws if any download fails (after cleanup).
  Future<VideoGroup> downloadGroup(
    VideoGroup group, {
    DownloadProgressCallback? onProgress,
  }) async {
    final groupDir = await storageManager.createGroupDir(group.id);
    final updatedFiles = <RecordingFile>[];
    final totalFiles = group.files.length;

    for (var i = 0; i < group.files.length; i++) {
      final file = group.files[i];
      final cancelToken = CancelToken();
      _activeCancelTokens[_fileKey(group.id, file)] = cancelToken;

      try {
        final localPath = p.join(groupDir.path, file.fileName);

        await cameraService.downloadFile(
          file,
          savePath: localPath,
          onProgress: (received, total) {
            final fileProgress = total > 0 ? received / total : 0.0;
            final overallProgress = (i + fileProgress) / totalFiles;

            onProgress?.call(file, received, total, overallProgress);
          },
          cancelToken: cancelToken,
        );

        // Verify the download.
        final localFile = File(localPath);
        if (!await localFile.exists()) {
          throw DownloadException(
            'Downloaded file not found: $localPath',
            file: file,
          );
        }

        final downloadedSize = await localFile.length();
        updatedFiles.add(file.copyWith(
          localPath: localPath,
          downloadProgress: 1.0,
          fileSize: downloadedSize,
        ));
      } on DioException catch (e) {
        if (e.type == DioExceptionType.cancel) {
          throw DownloadCancelledException(
            'Download cancelled for ${file.fileName}',
            file: file,
          );
        }
        throw DownloadException(
          'Failed to download ${file.fileName}: ${e.message}',
          file: file,
        );
      } finally {
        _activeCancelTokens.remove(_fileKey(group.id, file));
      }
    }

    return group.copyWith(files: updatedFiles);
  }

  /// Download a single file from the camera.
  ///
  /// Returns the updated [RecordingFile] with localPath set.
  Future<RecordingFile> downloadSingleFile(
    RecordingFile file, {
    required String destinationDir,
    void Function(int received, int total)? onProgress,
    CancelToken? cancelToken,
  }) async {
    final localPath = p.join(destinationDir, file.fileName);

    await cameraService.downloadFile(
      file,
      savePath: localPath,
      onProgress: onProgress,
      cancelToken: cancelToken,
    );

    final localFile = File(localPath);
    if (!await localFile.exists()) {
      throw DownloadException(
        'Downloaded file not found: $localPath',
        file: file,
      );
    }

    final downloadedSize = await localFile.length();
    return file.copyWith(
      localPath: localPath,
      downloadProgress: 1.0,
      fileSize: downloadedSize,
    );
  }

  /// Cancel downloads for a specific group.
  void cancelGroupDownload(String groupId) {
    final keysToCancel = _activeCancelTokens.keys
        .where((key) => key.startsWith('$groupId/'))
        .toList();

    for (final key in keysToCancel) {
      _activeCancelTokens[key]?.cancel('User cancelled download');
      _activeCancelTokens.remove(key);
    }
  }

  /// Cancel all active downloads.
  void cancelAll() {
    for (final token in _activeCancelTokens.values) {
      token.cancel('All downloads cancelled');
    }
    _activeCancelTokens.clear();
  }

  /// Check if a group has any active downloads.
  bool isDownloading(String groupId) {
    return _activeCancelTokens.keys
        .any((key) => key.startsWith('$groupId/'));
  }

  /// Check if a specific file has already been downloaded.
  Future<bool> isFileDownloaded(
    RecordingFile file,
    String groupId,
  ) async {
    final expectedPath =
        p.join(storageManager.groupDownloadPath(groupId), file.fileName);
    final localFile = File(expectedPath);
    if (!await localFile.exists()) return false;

    // If we know the expected size, verify it.
    if (file.fileSize > 0) {
      final localSize = await localFile.length();
      return localSize == file.fileSize;
    }

    return true;
  }

  String _fileKey(String groupId, RecordingFile file) =>
      '$groupId/${file.fileName}';
}

/// Exception for download errors.
class DownloadException implements Exception {
  const DownloadException(this.message, {this.file});
  final String message;
  final RecordingFile? file;

  @override
  String toString() => 'DownloadException: $message';
}

/// Exception for cancelled downloads.
class DownloadCancelledException extends DownloadException {
  const DownloadCancelledException(super.message, {super.file});

  @override
  String toString() => 'DownloadCancelledException: $message';
}
