import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../models/pipeline_state.dart';
import '../models/video_group.dart';
import '../services/pipeline_orchestrator.dart';
import '../widgets/progress_indicator.dart';

/// Detailed progress view for an active processing job.
///
/// Shows the pipeline stage indicator, current stage progress,
/// file-level details, and control buttons (pause/cancel).
class ProcessingScreen extends ConsumerStatefulWidget {
  const ProcessingScreen({super.key});

  @override
  ConsumerState<ProcessingScreen> createState() => _ProcessingScreenState();
}

class _ProcessingScreenState extends ConsumerState<ProcessingScreen> {
  StreamSubscription<StageProgress>? _progressSub;
  StageProgress? _currentProgress;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _subscribeToProgress();
  }

  void _subscribeToProgress() {
    _progressSub?.cancel();
    final orchestrator = ref.read(pipelineProvider.notifier);
    _progressSub = orchestrator.progressStream.listen((progress) {
      if (mounted) {
        setState(() => _currentProgress = progress);
      }
    });
  }

  @override
  void dispose() {
    _progressSub?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final groupId = ModalRoute.of(context)?.settings.arguments as String?;
    if (groupId == null) {
      return Scaffold(
        appBar: AppBar(title: const Text('Processing')),
        body: const Center(child: Text('No group selected')),
      );
    }

    final groupMap = ref.watch(pipelineProvider);
    final group = groupMap[groupId];
    if (group == null) {
      return Scaffold(
        appBar: AppBar(title: const Text('Processing')),
        body: const Center(child: Text('Group not found')),
      );
    }

    return Scaffold(
      appBar: AppBar(
        title: Text(group.name),
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          // Pipeline stage indicator.
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Pipeline Progress',
                    style: theme.textTheme.titleMedium,
                  ),
                  const SizedBox(height: 16),
                  PipelineStageIndicator(
                    currentState: group.state,
                    activeStageProgress: _currentProgress?.progress ?? 0.0,
                  ),
                  const SizedBox(height: 16),
                  // Overall progress bar.
                  PipelineProgressIndicator(
                    state: group.state,
                    progress: _currentProgress?.progress ??
                        group.state.overallProgress,
                    message: _currentProgress?.message,
                    estimatedRemaining: _currentProgress?.estimatedRemaining,
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),

          // Current stage details.
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Current Stage: ${group.state.displayName}',
                    style: theme.textTheme.titleMedium,
                  ),
                  const SizedBox(height: 8),
                  _buildStageDetails(theme, group),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),

          // File list.
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Files (${group.fileCount})',
                    style: theme.textTheme.titleMedium,
                  ),
                  const SizedBox(height: 8),
                  ...group.files.map((file) => ListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        leading: Icon(
                          file.isDownloaded
                              ? Icons.check_circle
                              : Icons.circle_outlined,
                          color: file.isDownloaded ? Colors.green : Colors.grey,
                          size: 20,
                        ),
                        title: Text(
                          file.fileName,
                          style: theme.textTheme.bodyMedium,
                        ),
                        subtitle: Text(
                          '${_formatDuration(file.duration)} - '
                          '${_formatBytes(file.fileSize)}',
                          style: theme.textTheme.bodySmall,
                        ),
                        trailing: file.downloadProgress > 0 &&
                                file.downloadProgress < 1.0
                            ? SizedBox(
                                width: 40,
                                child: CircularProgressIndicator(
                                  value: file.downloadProgress,
                                  strokeWidth: 3,
                                ),
                              )
                            : null,
                      )),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),

          // Error details if in error state.
          if (group.state == PipelineState.error &&
              group.errorMessage != null) ...[
            Card(
              color: theme.colorScheme.errorContainer,
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Icon(
                          Icons.error,
                          color: theme.colorScheme.onErrorContainer,
                        ),
                        const SizedBox(width: 8),
                        Text(
                          'Error',
                          style: theme.textTheme.titleMedium?.copyWith(
                            color: theme.colorScheme.onErrorContainer,
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 8),
                    Text(
                      group.errorMessage!,
                      style: theme.textTheme.bodyMedium?.copyWith(
                        color: theme.colorScheme.onErrorContainer,
                      ),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 16),
          ],

          // Control buttons.
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceEvenly,
            children: [
              if (group.state.isProcessing) ...[
                OutlinedButton.icon(
                  onPressed: () =>
                      ref.read(pipelineProvider.notifier).pauseGroup(groupId),
                  icon: const Icon(Icons.pause),
                  label: const Text('Pause'),
                ),
                FilledButton.icon(
                  onPressed: () =>
                      ref.read(pipelineProvider.notifier).cancelGroup(groupId),
                  icon: const Icon(Icons.cancel),
                  label: const Text('Cancel'),
                  style: FilledButton.styleFrom(
                    backgroundColor: theme.colorScheme.error,
                  ),
                ),
              ],
              if (group.state == PipelineState.error)
                FilledButton.icon(
                  onPressed: () =>
                      ref.read(pipelineProvider.notifier).retryGroup(groupId),
                  icon: const Icon(Icons.refresh),
                  label: const Text('Retry'),
                ),
              if (group.state == PipelineState.combined)
                FilledButton.icon(
                  onPressed: () => Navigator.pushReplacementNamed(
                    context,
                    '/trim',
                    arguments: groupId,
                  ),
                  icon: const Icon(Icons.content_cut),
                  label: const Text('Trim Video'),
                ),
              if (group.state == PipelineState.complete)
                FilledButton.icon(
                  onPressed: () => Navigator.pop(context),
                  icon: const Icon(Icons.check),
                  label: const Text('Done'),
                ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildStageDetails(ThemeData theme, VideoGroup group) {
    final description = switch (group.state) {
      PipelineState.pending => 'Waiting to start...',
      PipelineState.downloading =>
        'Downloading ${group.fileCount} files from camera.',
      PipelineState.downloaded => 'All files downloaded. Ready to combine.',
      PipelineState.combining =>
        'Combining ${group.fileCount} files into a single video.',
      PipelineState.combined => 'Video combined. Set trim points to continue.',
      PipelineState.trimming => 'Trimming video to selected range.',
      PipelineState.trimmed => 'Video trimmed. Ready to upload.',
      PipelineState.uploading => 'Uploading to YouTube...',
      PipelineState.complete => 'Processing complete.',
      PipelineState.error => 'An error occurred during processing.',
    };

    return Text(
      description,
      style: theme.textTheme.bodyMedium?.copyWith(
        color: theme.colorScheme.outline,
      ),
    );
  }

  String _formatDuration(Duration duration) {
    final minutes = duration.inMinutes;
    final seconds = duration.inSeconds.remainder(60);
    return '${minutes}m ${seconds}s';
  }

  String _formatBytes(int bytes) {
    if (bytes == 0) return 'Unknown size';
    if (bytes < 1024 * 1024) return '${(bytes / 1024).toStringAsFixed(0)} KB';
    if (bytes < 1024 * 1024 * 1024) {
      return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
    }
    return '${(bytes / (1024 * 1024 * 1024)).toStringAsFixed(2)} GB';
  }
}
