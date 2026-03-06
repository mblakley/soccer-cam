import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:video_player/video_player.dart';
import 'dart:io';
import '../models/pipeline_state.dart';
import '../services/pipeline_orchestrator.dart';

/// Screen for trimming a combined video with start/end markers.
///
/// Displays a video player with a range slider for selecting the
/// trim range. Shows current position, total duration, and the
/// selected trim range.
class TrimScreen extends ConsumerStatefulWidget {
  const TrimScreen({super.key});

  @override
  ConsumerState<TrimScreen> createState() => _TrimScreenState();
}

class _TrimScreenState extends ConsumerState<TrimScreen> {
  VideoPlayerController? _controller;
  bool _isInitialized = false;
  String? _errorMessage;

  // Trim range in seconds.
  double _trimStart = 0.0;
  double _trimEnd = 0.0;
  double _videoDuration = 0.0;

  String? _groupId;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final groupId = ModalRoute.of(context)?.settings.arguments as String?;
    if (groupId != null && groupId != _groupId) {
      _groupId = groupId;
      _initializePlayer();
    }
  }

  Future<void> _initializePlayer() async {
    final groupMap = ref.read(pipelineProvider);
    final group = groupMap[_groupId];
    if (group == null || group.combinedFilePath == null) {
      setState(() => _errorMessage = 'No combined video file found.');
      return;
    }

    final videoFile = File(group.combinedFilePath!);
    if (!await videoFile.exists()) {
      setState(() => _errorMessage = 'Video file not found on disk.');
      return;
    }

    _controller?.dispose();
    _controller = VideoPlayerController.file(videoFile);

    try {
      await _controller!.initialize();
      _controller!.addListener(_onVideoUpdate);

      setState(() {
        _isInitialized = true;
        _videoDuration =
            _controller!.value.duration.inMilliseconds / 1000.0;
        _trimStart = group.trimStartSeconds ?? 0.0;
        _trimEnd = group.trimEndSeconds ?? _videoDuration;
      });
    } catch (e) {
      setState(() => _errorMessage = 'Failed to load video: $e');
    }
  }

  void _onVideoUpdate() {
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    _controller?.removeListener(_onVideoUpdate);
    _controller?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Trim Video'),
        actions: [
          TextButton.icon(
            onPressed: _isInitialized ? _saveTrimPoints : null,
            icon: const Icon(Icons.check),
            label: const Text('Apply'),
          ),
        ],
      ),
      body: _errorMessage != null
          ? _buildError(theme)
          : !_isInitialized
              ? const Center(child: CircularProgressIndicator())
              : _buildTrimInterface(theme),
    );
  }

  Widget _buildError(ThemeData theme) {
    return Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(
            Icons.error_outline,
            size: 64,
            color: theme.colorScheme.error,
          ),
          const SizedBox(height: 16),
          Text(
            _errorMessage!,
            style: theme.textTheme.bodyLarge,
            textAlign: TextAlign.center,
          ),
        ],
      ),
    );
  }

  Widget _buildTrimInterface(ThemeData theme) {
    final position = _controller!.value.position;
    final positionSeconds = position.inMilliseconds / 1000.0;
    final isPlaying = _controller!.value.isPlaying;

    return Column(
      children: [
        // Video player.
        Expanded(
          flex: 3,
          child: Container(
            color: Colors.black,
            child: Center(
              child: AspectRatio(
                aspectRatio: _controller!.value.aspectRatio,
                child: VideoPlayer(_controller!),
              ),
            ),
          ),
        ),

        // Playback controls.
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 8),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              // Jump to trim start.
              IconButton(
                onPressed: () => _seekTo(_trimStart),
                icon: const Icon(Icons.skip_previous),
                tooltip: 'Go to trim start',
              ),
              // Seek backward 10s.
              IconButton(
                onPressed: () =>
                    _seekTo((positionSeconds - 10).clamp(0, _videoDuration)),
                icon: const Icon(Icons.replay_10),
              ),
              // Play/Pause.
              IconButton.filled(
                onPressed: () {
                  if (isPlaying) {
                    _controller!.pause();
                  } else {
                    _controller!.play();
                  }
                },
                icon: Icon(isPlaying ? Icons.pause : Icons.play_arrow),
                iconSize: 32,
              ),
              // Seek forward 10s.
              IconButton(
                onPressed: () =>
                    _seekTo((positionSeconds + 10).clamp(0, _videoDuration)),
                icon: const Icon(Icons.forward_10),
              ),
              // Jump to trim end.
              IconButton(
                onPressed: () => _seekTo(_trimEnd),
                icon: const Icon(Icons.skip_next),
                tooltip: 'Go to trim end',
              ),
            ],
          ),
        ),

        // Position indicator.
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                _formatTime(positionSeconds),
                style: theme.textTheme.bodySmall,
              ),
              Text(
                _formatTime(_videoDuration),
                style: theme.textTheme.bodySmall,
              ),
            ],
          ),
        ),

        // Position scrubber.
        Slider(
          value: positionSeconds.clamp(0, _videoDuration),
          min: 0,
          max: _videoDuration,
          onChanged: (value) => _seekTo(value),
        ),

        const Divider(),

        // Trim range controls.
        Expanded(
          flex: 2,
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Trim Range',
                  style: theme.textTheme.titleMedium,
                ),
                const SizedBox(height: 8),

                // Trim range slider.
                RangeSlider(
                  values: RangeValues(_trimStart, _trimEnd),
                  min: 0,
                  max: _videoDuration,
                  divisions: (_videoDuration * 10).round().clamp(1, 10000),
                  labels: RangeLabels(
                    _formatTime(_trimStart),
                    _formatTime(_trimEnd),
                  ),
                  onChanged: (values) {
                    setState(() {
                      _trimStart = values.start;
                      _trimEnd = values.end;
                    });
                  },
                ),

                // Trim time display.
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text('Start', style: theme.textTheme.labelSmall),
                        Text(
                          _formatTime(_trimStart),
                          style: theme.textTheme.bodyLarge?.copyWith(
                            fontFamily: 'monospace',
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ],
                    ),
                    Column(
                      children: [
                        Text('Duration', style: theme.textTheme.labelSmall),
                        Text(
                          _formatTime(_trimEnd - _trimStart),
                          style: theme.textTheme.bodyLarge?.copyWith(
                            fontFamily: 'monospace',
                            color: theme.colorScheme.primary,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ],
                    ),
                    Column(
                      crossAxisAlignment: CrossAxisAlignment.end,
                      children: [
                        Text('End', style: theme.textTheme.labelSmall),
                        Text(
                          _formatTime(_trimEnd),
                          style: theme.textTheme.bodyLarge?.copyWith(
                            fontFamily: 'monospace',
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ],
                    ),
                  ],
                ),

                const SizedBox(height: 16),

                // Set markers from current position.
                Row(
                  children: [
                    Expanded(
                      child: OutlinedButton.icon(
                        onPressed: () {
                          setState(() => _trimStart = positionSeconds);
                        },
                        icon: const Icon(Icons.first_page, size: 18),
                        label: const Text('Set Start'),
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: OutlinedButton.icon(
                        onPressed: () {
                          setState(() => _trimEnd = positionSeconds);
                        },
                        icon: const Icon(Icons.last_page, size: 18),
                        label: const Text('Set End'),
                      ),
                    ),
                  ],
                ),

                const SizedBox(height: 8),

                // Preview trim button.
                Center(
                  child: TextButton.icon(
                    onPressed: _previewTrim,
                    icon: const Icon(Icons.preview, size: 18),
                    label: const Text('Preview from start'),
                  ),
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }

  void _seekTo(double seconds) {
    _controller?.seekTo(Duration(milliseconds: (seconds * 1000).round()));
  }

  void _previewTrim() {
    _seekTo(_trimStart);
    _controller?.play();
    // Stop at trim end (approximate - video_player does not have native range playback).
  }

  void _saveTrimPoints() {
    if (_groupId == null) return;

    final orchestrator = ref.read(pipelineProvider.notifier);
    orchestrator.setTrimPoints(_groupId!, _trimStart, _trimEnd);

    // Check if we should auto-advance to trimming.
    final group = ref.read(pipelineProvider)[_groupId!];
    if (group != null && group.state == PipelineState.combined) {
      orchestrator.processGroup(_groupId!);
    }

    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          'Trim set: ${_formatTime(_trimStart)} - ${_formatTime(_trimEnd)}',
        ),
        behavior: SnackBarBehavior.floating,
      ),
    );

    Navigator.pop(context);
  }

  String _formatTime(double seconds) {
    final h = (seconds / 3600).floor();
    final m = ((seconds % 3600) / 60).floor();
    final s = (seconds % 60).floor();
    final ms = ((seconds % 1) * 100).round();

    if (h > 0) {
      return '${h.toString().padLeft(2, '0')}:'
          '${m.toString().padLeft(2, '0')}:'
          '${s.toString().padLeft(2, '0')}.'
          '${ms.toString().padLeft(2, '0')}';
    }
    return '${m.toString().padLeft(2, '0')}:'
        '${s.toString().padLeft(2, '0')}.'
        '${ms.toString().padLeft(2, '0')}';
  }
}
