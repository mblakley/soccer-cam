import 'dart:async';
import 'dart:io';
import 'package:ffmpeg_kit_flutter_full_gpl/ffmpeg_kit.dart';
import 'package:ffmpeg_kit_flutter_full_gpl/ffmpeg_kit_config.dart';
import 'package:ffmpeg_kit_flutter_full_gpl/ffprobe_kit.dart';
import 'package:ffmpeg_kit_flutter_full_gpl/return_code.dart';
import 'package:ffmpeg_kit_flutter_full_gpl/statistics.dart';
import 'package:path/path.dart' as p;
import '../utils/storage_manager.dart';

/// Callback for FFmpeg processing progress.
typedef ProcessingProgressCallback = void Function(
  double progress,
  Duration elapsed,
  Duration? estimatedRemaining,
);

/// Wraps FFmpegKit for video processing operations.
///
/// Supports:
/// - Combine: concatenate multiple .dav files into a single MP4 (stream copy)
/// - Trim: extract a segment from a video (stream copy)
/// - Duration query via ffprobe
class VideoProcessor {
  VideoProcessor({required this.storageManager});

  final StorageManager storageManager;

  /// Combine multiple video files into a single MP4 using concat demuxer.
  ///
  /// Uses stream copy for video, re-encodes audio to AAC.
  /// Command: ffmpeg -y -f concat -safe 0 -i <filelist> -c:v copy -c:a aac -b:a 192k <output.mp4>
  ///
  /// [inputPaths] must be ordered by recording time.
  /// Returns the path to the combined output file.
  Future<String> combineFiles(
    List<String> inputPaths, {
    required String outputPath,
    ProcessingProgressCallback? onProgress,
  }) async {
    if (inputPaths.isEmpty) {
      throw VideoProcessingException('No input files provided for combine');
    }

    // Verify all input files exist.
    for (final path in inputPaths) {
      if (!await File(path).exists()) {
        throw VideoProcessingException('Input file not found: $path');
      }
    }

    // Create the concat demuxer file list.
    final concatListPath = p.join(storageManager.tempDir.path, 'concat.txt');
    final concatFile = File(concatListPath);
    final concatContent = inputPaths
        .map((path) => "file '${path.replaceAll("'", "'\\''")}'")
        .join('\n');
    await concatFile.writeAsString(concatContent);

    // Get total duration for progress calculation.
    double totalDuration = 0;
    for (final path in inputPaths) {
      final duration = await getDuration(path);
      if (duration != null) {
        totalDuration += duration;
      }
    }

    try {
      final command = '-y '
          '-f concat '
          '-safe 0 '
          '-i "$concatListPath" '
          '-c:v copy '
          '-c:a aac '
          '-b:a 192k '
          '"$outputPath"';

      final startTime = DateTime.now();

      final session = await FFmpegKit.executeAsync(
        command,
        null, // completion callback handled below
        null, // log callback
        totalDuration > 0
            ? (Statistics statistics) {
                final time = statistics.getTime();
                if (time > 0 && totalDuration > 0) {
                  final progress = (time / 1000) / totalDuration;
                  final elapsed = DateTime.now().difference(startTime);
                  Duration? remaining;
                  if (progress > 0.01) {
                    final totalEstimated = elapsed * (1.0 / progress);
                    remaining = totalEstimated - elapsed;
                  }
                  onProgress?.call(
                    progress.clamp(0.0, 1.0),
                    elapsed,
                    remaining,
                  );
                }
              }
            : null,
      );

      final returnCode = await session.getReturnCode();

      if (!ReturnCode.isSuccess(returnCode)) {
        final logs = await session.getAllLogsAsString();
        throw VideoProcessingException(
          'FFmpeg combine failed with code ${returnCode?.getValue()}: $logs',
        );
      }

      // Verify output exists.
      if (!await File(outputPath).exists()) {
        throw VideoProcessingException(
          'Combined output file not created: $outputPath',
        );
      }

      return outputPath;
    } finally {
      // Clean up concat file.
      if (await concatFile.exists()) {
        await concatFile.delete();
      }
    }
  }

  /// Trim a video file to a specific time range using stream copy.
  ///
  /// Command: ffmpeg -y -i <input> -ss <start> [-t <duration>] -c copy <output.mp4>
  ///
  /// [startSeconds] is the start position in seconds.
  /// [endSeconds] is optional; if null, trims from start to end of file.
  /// Returns the path to the trimmed output file.
  Future<String> trimFile(
    String inputPath, {
    required String outputPath,
    required double startSeconds,
    double? endSeconds,
    ProcessingProgressCallback? onProgress,
  }) async {
    if (!await File(inputPath).exists()) {
      throw VideoProcessingException('Input file not found: $inputPath');
    }

    // Calculate duration for the trim.
    double? trimDuration;
    if (endSeconds != null) {
      trimDuration = endSeconds - startSeconds;
      if (trimDuration <= 0) {
        throw VideoProcessingException(
          'Invalid trim range: start=$startSeconds, end=$endSeconds',
        );
      }
    }

    final startStr = _formatTimestamp(startSeconds);
    final durationPart =
        trimDuration != null ? '-t ${_formatTimestamp(trimDuration)}' : '';

    final command = '-y '
        '-i "$inputPath" '
        '-ss $startStr '
        '$durationPart '
        '-c copy '
        '"$outputPath"';

    final effectiveDuration =
        trimDuration ?? (await getDuration(inputPath) ?? 0) - startSeconds;
    final startTime = DateTime.now();

    final session = await FFmpegKit.executeAsync(
      command,
      null,
      null,
      effectiveDuration > 0
          ? (Statistics statistics) {
              final time = statistics.getTime();
              if (time > 0) {
                final progress = (time / 1000) / effectiveDuration;
                final elapsed = DateTime.now().difference(startTime);
                Duration? remaining;
                if (progress > 0.01) {
                  final totalEstimated = elapsed * (1.0 / progress);
                  remaining = totalEstimated - elapsed;
                }
                onProgress?.call(
                  progress.clamp(0.0, 1.0),
                  elapsed,
                  remaining,
                );
              }
            }
          : null,
    );

    final returnCode = await session.getReturnCode();

    if (!ReturnCode.isSuccess(returnCode)) {
      final logs = await session.getAllLogsAsString();
      throw VideoProcessingException(
        'FFmpeg trim failed with code ${returnCode?.getValue()}: $logs',
      );
    }

    if (!await File(outputPath).exists()) {
      throw VideoProcessingException(
        'Trimmed output file not created: $outputPath',
      );
    }

    return outputPath;
  }

  /// Get the duration of a media file in seconds using ffprobe.
  ///
  /// Command: ffprobe -v error -show_entries format=duration
  ///          -of default=noprint_wrappers=1:nokey=1 <file>
  ///
  /// Returns null if the duration cannot be determined.
  Future<double?> getDuration(String filePath) async {
    if (!await File(filePath).exists()) return null;

    final session = await FFprobeKit.execute(
      '-v error '
      '-show_entries format=duration '
      '-of default=noprint_wrappers=1:nokey=1 '
      '"$filePath"',
    );

    final returnCode = await session.getReturnCode();
    if (!ReturnCode.isSuccess(returnCode)) return null;

    final output = await session.getOutput();
    if (output == null) return null;

    return double.tryParse(output.trim());
  }

  /// Get video resolution as (width, height).
  Future<(int, int)?> getResolution(String filePath) async {
    if (!await File(filePath).exists()) return null;

    final session = await FFprobeKit.execute(
      '-v error '
      '-select_streams v:0 '
      '-show_entries stream=width,height '
      '-of csv=s=x:p=0 '
      '"$filePath"',
    );

    final returnCode = await session.getReturnCode();
    if (!ReturnCode.isSuccess(returnCode)) return null;

    final output = await session.getOutput();
    if (output == null) return null;

    final parts = output.trim().split('x');
    if (parts.length != 2) return null;

    final width = int.tryParse(parts[0]);
    final height = int.tryParse(parts[1]);
    if (width == null || height == null) return null;

    return (width, height);
  }

  /// Cancel all running FFmpeg sessions.
  Future<void> cancelAll() async {
    await FFmpegKit.cancel();
  }

  /// Format seconds as HH:MM:SS.mmm for FFmpeg.
  String _formatTimestamp(double seconds) {
    final hours = (seconds / 3600).floor();
    final minutes = ((seconds % 3600) / 60).floor();
    final secs = seconds % 60;
    return '${hours.toString().padLeft(2, '0')}:'
        '${minutes.toString().padLeft(2, '0')}:'
        '${secs.toStringAsFixed(3).padLeft(6, '0')}';
  }
}

/// Exception for video processing errors.
class VideoProcessingException implements Exception {
  const VideoProcessingException(this.message);
  final String message;

  @override
  String toString() => 'VideoProcessingException: $message';
}
