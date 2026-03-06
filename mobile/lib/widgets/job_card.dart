import 'package:flutter/material.dart';
import '../models/pipeline_state.dart';
import '../models/video_group.dart';
import 'progress_indicator.dart' as custom;

/// A card widget displaying a video group's status on the dashboard.
///
/// Shows the group name, file count, current pipeline state with
/// a colored badge, and a progress bar when processing.
class JobCard extends StatelessWidget {
  const JobCard({
    super.key,
    required this.group,
    this.onTap,
    this.onRetry,
    this.onCancel,
    this.onDelete,
    this.onTrim,
    this.onSkipTrim,
    this.currentProgress,
  });

  final VideoGroup group;
  final VoidCallback? onTap;
  final VoidCallback? onRetry;
  final VoidCallback? onCancel;
  final VoidCallback? onDelete;
  final VoidCallback? onTrim;
  final VoidCallback? onSkipTrim;
  final double? currentProgress;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colorScheme = theme.colorScheme;

    return Card(
      elevation: group.state.isProcessing ? 3 : 1,
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Header row: name + status badge.
              Row(
                children: [
                  Expanded(
                    child: Text(
                      group.name,
                      style: theme.textTheme.titleMedium?.copyWith(
                        fontWeight: FontWeight.w600,
                      ),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                  _StatusBadge(state: group.state),
                ],
              ),
              const SizedBox(height: 8),

              // Info row: file count, duration, size.
              Row(
                children: [
                  _InfoChip(
                    icon: Icons.video_file_outlined,
                    label: '${group.fileCount} files',
                  ),
                  const SizedBox(width: 12),
                  _InfoChip(
                    icon: Icons.timer_outlined,
                    label: _formatDuration(group.totalDuration),
                  ),
                  if (group.totalFileSize > 0) ...[
                    const SizedBox(width: 12),
                    _InfoChip(
                      icon: Icons.storage_outlined,
                      label: _formatFileSize(group.totalFileSize),
                    ),
                  ],
                ],
              ),

              // Progress bar when actively processing.
              if (group.state.isProcessing) ...[
                const SizedBox(height: 12),
                custom.PipelineProgressIndicator(
                  state: group.state,
                  progress: currentProgress ?? group.downloadProgress,
                ),
              ],

              // Error message.
              if (group.state == PipelineState.error &&
                  group.errorMessage != null) ...[
                const SizedBox(height: 8),
                Container(
                  padding: const EdgeInsets.all(8),
                  decoration: BoxDecoration(
                    color: colorScheme.errorContainer,
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Row(
                    children: [
                      Icon(
                        Icons.error_outline,
                        size: 16,
                        color: colorScheme.onErrorContainer,
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          group.errorMessage!,
                          style: theme.textTheme.bodySmall?.copyWith(
                            color: colorScheme.onErrorContainer,
                          ),
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                    ],
                  ),
                ),
              ],

              // YouTube link when complete.
              if (group.state == PipelineState.complete &&
                  group.youtubeVideoId != null) ...[
                const SizedBox(height: 8),
                Row(
                  children: [
                    Icon(
                      Icons.play_circle_outline,
                      size: 16,
                      color: colorScheme.primary,
                    ),
                    const SizedBox(width: 4),
                    Text(
                      'youtu.be/${group.youtubeVideoId}',
                      style: theme.textTheme.bodySmall?.copyWith(
                        color: colorScheme.primary,
                      ),
                    ),
                  ],
                ),
              ],

              // Trim action for combined state.
              if (group.state == PipelineState.combined) ...[
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: FilledButton.icon(
                        onPressed: onTrim,
                        icon: const Icon(Icons.content_cut, size: 18),
                        label: const Text('Trim Video'),
                      ),
                    ),
                    const SizedBox(width: 8),
                    OutlinedButton(
                      onPressed: onSkipTrim,
                      child: const Text('Skip'),
                    ),
                  ],
                ),
              ],

              // Action buttons.
              if (group.state == PipelineState.error ||
                  group.state == PipelineState.complete ||
                  group.state.isProcessing) ...[
                const SizedBox(height: 8),
                Row(
                  mainAxisAlignment: MainAxisAlignment.end,
                  children: [
                    if (group.state == PipelineState.error && onRetry != null)
                      TextButton.icon(
                        onPressed: onRetry,
                        icon: const Icon(Icons.refresh, size: 18),
                        label: const Text('Retry'),
                      ),
                    if (group.state.isProcessing && onCancel != null)
                      TextButton.icon(
                        onPressed: onCancel,
                        icon: const Icon(Icons.cancel_outlined, size: 18),
                        label: const Text('Cancel'),
                      ),
                    if (onDelete != null)
                      TextButton.icon(
                        onPressed: onDelete,
                        icon: const Icon(Icons.delete_outline, size: 18),
                        label: const Text('Delete'),
                      ),
                  ],
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  String _formatDuration(Duration duration) {
    final hours = duration.inHours;
    final minutes = duration.inMinutes.remainder(60);
    if (hours > 0) {
      return '${hours}h ${minutes}m';
    }
    return '${minutes}m';
  }

  String _formatFileSize(int bytes) {
    if (bytes < 1024 * 1024) {
      return '${(bytes / 1024).toStringAsFixed(0)} KB';
    }
    if (bytes < 1024 * 1024 * 1024) {
      return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
    }
    return '${(bytes / (1024 * 1024 * 1024)).toStringAsFixed(2)} GB';
  }
}

/// Colored badge showing the pipeline state.
class _StatusBadge extends StatelessWidget {
  const _StatusBadge({required this.state});
  final PipelineState state;

  @override
  Widget build(BuildContext context) {
    final (color, icon) = _stateAppearance(context);

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: color.withValues(alpha: 0.3)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 14, color: color),
          const SizedBox(width: 4),
          Text(
            state.displayName,
            style: TextStyle(
              color: color,
              fontSize: 12,
              fontWeight: FontWeight.w600,
            ),
          ),
        ],
      ),
    );
  }

  (Color, IconData) _stateAppearance(BuildContext context) {
    final colorScheme = Theme.of(context).colorScheme;
    return switch (state) {
      PipelineState.pending => (Colors.grey, Icons.hourglass_empty),
      PipelineState.downloading => (Colors.blue, Icons.download),
      PipelineState.downloaded => (Colors.blue.shade700, Icons.download_done),
      PipelineState.combining => (Colors.orange, Icons.merge_type),
      PipelineState.combined => (Colors.orange.shade700, Icons.merge),
      PipelineState.trimming => (Colors.purple, Icons.content_cut),
      PipelineState.trimmed => (Colors.purple.shade700, Icons.check_circle),
      PipelineState.uploading => (Colors.teal, Icons.upload),
      PipelineState.complete => (colorScheme.primary, Icons.check_circle),
      PipelineState.error => (colorScheme.error, Icons.error),
    };
  }
}

/// Small info chip with icon and text.
class _InfoChip extends StatelessWidget {
  const _InfoChip({required this.icon, required this.label});
  final IconData icon;
  final String label;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, size: 14, color: theme.colorScheme.outline),
        const SizedBox(width: 4),
        Text(
          label,
          style: theme.textTheme.bodySmall?.copyWith(
            color: theme.colorScheme.outline,
          ),
        ),
      ],
    );
  }
}
