import 'package:flutter/material.dart';
import '../models/pipeline_state.dart';

/// Progress indicator that shows the current pipeline stage and progress.
///
/// Displays a linear progress bar with the stage name and percentage.
class PipelineProgressIndicator extends StatelessWidget {
  const PipelineProgressIndicator({
    super.key,
    required this.state,
    required this.progress,
    this.message,
    this.estimatedRemaining,
  });

  final PipelineState state;
  final double progress;
  final String? message;
  final Duration? estimatedRemaining;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final stageColor = _colorForState(context);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        // Progress bar.
        ClipRRect(
          borderRadius: BorderRadius.circular(4),
          child: LinearProgressIndicator(
            value: progress.clamp(0.0, 1.0),
            minHeight: 6,
            backgroundColor: stageColor.withValues(alpha: 0.15),
            valueColor: AlwaysStoppedAnimation(stageColor),
          ),
        ),
        const SizedBox(height: 4),

        // Progress text row.
        Row(
          children: [
            if (message != null)
              Expanded(
                child: Text(
                  message!,
                  style: theme.textTheme.bodySmall?.copyWith(
                    color: theme.colorScheme.outline,
                  ),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                ),
              )
            else
              Expanded(
                child: Text(
                  state.displayName,
                  style: theme.textTheme.bodySmall?.copyWith(
                    color: theme.colorScheme.outline,
                  ),
                ),
              ),
            Text(
              '${(progress * 100).toStringAsFixed(1)}%',
              style: theme.textTheme.bodySmall?.copyWith(
                color: stageColor,
                fontWeight: FontWeight.w600,
              ),
            ),
          ],
        ),

        // Estimated time remaining.
        if (estimatedRemaining != null) ...[
          const SizedBox(height: 2),
          Text(
            'Est. ${_formatDuration(estimatedRemaining!)} remaining',
            style: theme.textTheme.bodySmall?.copyWith(
              color: theme.colorScheme.outline.withValues(alpha: 0.7),
              fontSize: 11,
            ),
          ),
        ],
      ],
    );
  }

  Color _colorForState(BuildContext context) {
    return switch (state) {
      PipelineState.downloading => Colors.blue,
      PipelineState.combining => Colors.orange,
      PipelineState.trimming => Colors.purple,
      PipelineState.uploading => Colors.teal,
      PipelineState.error => Theme.of(context).colorScheme.error,
      _ => Theme.of(context).colorScheme.primary,
    };
  }

  String _formatDuration(Duration duration) {
    if (duration.inHours > 0) {
      return '${duration.inHours}h ${duration.inMinutes.remainder(60)}m';
    }
    if (duration.inMinutes > 0) {
      return '${duration.inMinutes}m ${duration.inSeconds.remainder(60)}s';
    }
    return '${duration.inSeconds}s';
  }
}

/// A multi-stage progress indicator showing all pipeline stages.
///
/// Renders a horizontal stepper-like visualization of the pipeline.
class PipelineStageIndicator extends StatelessWidget {
  const PipelineStageIndicator({
    super.key,
    required this.currentState,
    this.activeStageProgress = 0.0,
  });

  final PipelineState currentState;
  final double activeStageProgress;

  static const _displayStages = [
    PipelineState.downloading,
    PipelineState.combining,
    PipelineState.trimming,
    PipelineState.uploading,
    PipelineState.complete,
  ];

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Row(
      children: [
        for (var i = 0; i < _displayStages.length; i++) ...[
          if (i > 0) _buildConnector(context, i),
          _buildStageCircle(context, theme, _displayStages[i]),
        ],
      ],
    );
  }

  Widget _buildStageCircle(
    BuildContext context,
    ThemeData theme,
    PipelineState stage,
  ) {
    final isComplete =
        stage.overallProgress < currentState.overallProgress;
    final isCurrent = stage == currentState;
    final isError = currentState == PipelineState.error;

    Color circleColor;
    IconData icon;

    if (isError && isCurrent) {
      circleColor = theme.colorScheme.error;
      icon = Icons.error;
    } else if (isComplete) {
      circleColor = theme.colorScheme.primary;
      icon = Icons.check;
    } else if (isCurrent) {
      circleColor = _colorForState(stage);
      icon = _iconForState(stage);
    } else {
      circleColor = theme.colorScheme.outline.withValues(alpha: 0.3);
      icon = _iconForState(stage);
    }

    return Tooltip(
      message: stage.displayName,
      child: Container(
        width: 28,
        height: 28,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: isCurrent || isComplete
              ? circleColor
              : circleColor.withValues(alpha: 0.2),
          border: Border.all(color: circleColor, width: 2),
        ),
        child: Icon(
          icon,
          size: 14,
          color: isCurrent || isComplete
              ? Colors.white
              : circleColor,
        ),
      ),
    );
  }

  Widget _buildConnector(BuildContext context, int index) {
    final prevStage = _displayStages[index - 1];
    final isComplete =
        prevStage.overallProgress < currentState.overallProgress;
    final color = isComplete
        ? Theme.of(context).colorScheme.primary
        : Theme.of(context).colorScheme.outline.withValues(alpha: 0.3);

    return Expanded(
      child: Container(
        height: 2,
        margin: const EdgeInsets.symmetric(horizontal: 4),
        color: color,
      ),
    );
  }

  Color _colorForState(PipelineState state) {
    return switch (state) {
      PipelineState.downloading => Colors.blue,
      PipelineState.combining => Colors.orange,
      PipelineState.trimming => Colors.purple,
      PipelineState.uploading => Colors.teal,
      PipelineState.complete => Colors.green,
      _ => Colors.grey,
    };
  }

  IconData _iconForState(PipelineState state) {
    return switch (state) {
      PipelineState.downloading => Icons.download,
      PipelineState.combining => Icons.merge_type,
      PipelineState.trimming => Icons.content_cut,
      PipelineState.uploading => Icons.upload,
      PipelineState.complete => Icons.check_circle,
      _ => Icons.circle_outlined,
    };
  }
}
