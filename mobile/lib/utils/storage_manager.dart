import 'dart:io';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';

/// Manages local file storage for downloaded and processed video files.
///
/// Provides directory creation, cleanup after upload, and storage
/// usage monitoring.
class StorageManager {
  StorageManager._();
  static final instance = StorageManager._();

  Directory? _appDir;
  Directory? _downloadDir;
  Directory? _processedDir;
  Directory? _tempDir;

  /// Initialize storage directories. Must be called before use.
  Future<void> initialize() async {
    _appDir = await getApplicationDocumentsDirectory();
    final base = p.join(_appDir!.path, 'SoccerCam');

    _downloadDir = Directory(p.join(base, 'downloads'));
    _processedDir = Directory(p.join(base, 'processed'));
    _tempDir = Directory(p.join(base, 'temp'));

    await _downloadDir!.create(recursive: true);
    await _processedDir!.create(recursive: true);
    await _tempDir!.create(recursive: true);
  }

  /// Base application directory.
  Directory get appDir {
    _ensureInitialized();
    return _appDir!;
  }

  /// Directory for downloaded .dav files.
  Directory get downloadDir {
    _ensureInitialized();
    return _downloadDir!;
  }

  /// Directory for processed (combined/trimmed) MP4 files.
  Directory get processedDir {
    _ensureInitialized();
    return _processedDir!;
  }

  /// Directory for temporary files (concat lists, etc.).
  Directory get tempDir {
    _ensureInitialized();
    return _tempDir!;
  }

  /// Get the download path for a specific video group.
  String groupDownloadPath(String groupId) {
    _ensureInitialized();
    return p.join(_downloadDir!.path, groupId);
  }

  /// Get the processed file path for a video group.
  String groupProcessedPath(String groupId, String fileName) {
    _ensureInitialized();
    return p.join(_processedDir!.path, groupId, fileName);
  }

  /// Create a group-specific download directory.
  Future<Directory> createGroupDir(String groupId) async {
    _ensureInitialized();
    final dir = Directory(p.join(_downloadDir!.path, groupId));
    await dir.create(recursive: true);
    return dir;
  }

  /// Create a group-specific processed directory.
  Future<Directory> createGroupProcessedDir(String groupId) async {
    _ensureInitialized();
    final dir = Directory(p.join(_processedDir!.path, groupId));
    await dir.create(recursive: true);
    return dir;
  }

  /// Clean up all files for a video group after successful upload.
  Future<void> cleanupGroup(String groupId) async {
    _ensureInitialized();

    final downloadGroupDir = Directory(p.join(_downloadDir!.path, groupId));
    if (await downloadGroupDir.exists()) {
      await downloadGroupDir.delete(recursive: true);
    }

    final processedGroupDir = Directory(p.join(_processedDir!.path, groupId));
    if (await processedGroupDir.exists()) {
      await processedGroupDir.delete(recursive: true);
    }
  }

  /// Clean up temporary files.
  Future<void> cleanupTemp() async {
    _ensureInitialized();
    if (await _tempDir!.exists()) {
      await for (final entity in _tempDir!.list()) {
        await entity.delete(recursive: true);
      }
    }
  }

  /// Get total storage used in bytes across all directories.
  Future<int> getTotalStorageUsed() async {
    _ensureInitialized();
    var total = 0;
    total += await _getDirectorySize(_downloadDir!);
    total += await _getDirectorySize(_processedDir!);
    total += await _getDirectorySize(_tempDir!);
    return total;
  }

  /// Get storage used in bytes for a specific directory.
  Future<int> _getDirectorySize(Directory dir) async {
    var size = 0;
    if (!await dir.exists()) return size;

    await for (final entity in dir.list(recursive: true)) {
      if (entity is File) {
        size += await entity.length();
      }
    }
    return size;
  }

  /// Format bytes as a human-readable string.
  static String formatBytes(int bytes) {
    if (bytes < 1024) return '$bytes B';
    if (bytes < 1024 * 1024) return '${(bytes / 1024).toStringAsFixed(1)} KB';
    if (bytes < 1024 * 1024 * 1024) {
      return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
    }
    return '${(bytes / (1024 * 1024 * 1024)).toStringAsFixed(2)} GB';
  }

  /// Delete a specific file.
  Future<void> deleteFile(String path) async {
    final file = File(path);
    if (await file.exists()) {
      await file.delete();
    }
  }

  void _ensureInitialized() {
    if (_appDir == null) {
      throw StateError(
        'StorageManager not initialized. Call initialize() first.',
      );
    }
  }
}
