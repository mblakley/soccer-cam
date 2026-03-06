import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../models/pipeline_state.dart';
import '../models/video_group.dart';
import '../services/pipeline_orchestrator.dart';
import '../widgets/job_card.dart';

/// Main dashboard screen showing all video processing jobs.
///
/// Displays a list of JobCard widgets with status badges, progress
/// indicators, and action buttons. Provides FAB to scan camera for
/// new recordings.
class DashboardScreen extends ConsumerStatefulWidget {
  const DashboardScreen({super.key});

  @override
  ConsumerState<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends ConsumerState<DashboardScreen> {
  bool _isScanning = false;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final groupMap = ref.watch(pipelineProvider);
    final groups = groupMap.values.toList()
      ..sort((a, b) => b.startTime.compareTo(a.startTime));

    return Scaffold(
      appBar: AppBar(
        title: const Text('Soccer Cam'),
        actions: [
          IconButton(
            icon: const Icon(Icons.settings),
            tooltip: 'Settings',
            onPressed: () => Navigator.pushNamed(context, '/settings'),
          ),
        ],
      ),
      body: groups.isEmpty ? _buildEmptyState(theme) : _buildJobList(groups),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: _isScanning ? null : _scanForRecordings,
        icon: _isScanning
            ? const SizedBox(
                width: 20,
                height: 20,
                child: CircularProgressIndicator(strokeWidth: 2),
              )
            : const Icon(Icons.videocam),
        label: Text(_isScanning ? 'Scanning...' : 'Scan Camera'),
      ),
    );
  }

  Widget _buildEmptyState(ThemeData theme) {
    return Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(
            Icons.videocam_off_outlined,
            size: 80,
            color: theme.colorScheme.outline.withValues(alpha: 0.5),
          ),
          const SizedBox(height: 16),
          Text(
            'No recordings yet',
            style: theme.textTheme.headlineSmall?.copyWith(
              color: theme.colorScheme.outline,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            'Tap "Scan Camera" to discover recordings,\nor set up your camera first.',
            textAlign: TextAlign.center,
            style: theme.textTheme.bodyMedium?.copyWith(
              color: theme.colorScheme.outline.withValues(alpha: 0.7),
            ),
          ),
          const SizedBox(height: 24),
          OutlinedButton.icon(
            onPressed: () => Navigator.pushNamed(context, '/camera-setup'),
            icon: const Icon(Icons.settings),
            label: const Text('Camera Setup'),
          ),
        ],
      ),
    );
  }

  Widget _buildJobList(List<VideoGroup> groups) {
    // Separate groups by status category.
    final active = groups.where((g) => g.state.isProcessing).toList();
    final pending = groups
        .where((g) =>
            g.state == PipelineState.pending ||
            g.state == PipelineState.downloaded ||
            g.state == PipelineState.combined ||
            g.state == PipelineState.trimmed)
        .toList();
    final completed =
        groups.where((g) => g.state == PipelineState.complete).toList();
    final errors =
        groups.where((g) => g.state == PipelineState.error).toList();

    return ListView(
      padding: const EdgeInsets.only(top: 8, bottom: 88),
      children: [
        if (active.isNotEmpty) ...[
          _buildSectionHeader('Active'),
          ...active.map(_buildJobCard),
        ],
        if (errors.isNotEmpty) ...[
          _buildSectionHeader('Errors'),
          ...errors.map(_buildJobCard),
        ],
        if (pending.isNotEmpty) ...[
          _buildSectionHeader('Ready'),
          ...pending.map(_buildJobCard),
        ],
        if (completed.isNotEmpty) ...[
          _buildSectionHeader('Completed'),
          ...completed.map(_buildJobCard),
        ],
      ],
    );
  }

  Widget _buildSectionHeader(String title) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 16, 20, 4),
      child: Text(
        title,
        style: Theme.of(context).textTheme.titleSmall?.copyWith(
              color: Theme.of(context).colorScheme.outline,
              fontWeight: FontWeight.w600,
              letterSpacing: 0.5,
            ),
      ),
    );
  }

  Widget _buildJobCard(VideoGroup group) {
    final orchestrator = ref.read(pipelineProvider.notifier);

    return JobCard(
      group: group,
      onTap: () => _openGroupDetail(group),
      onRetry: group.state == PipelineState.error
          ? () => orchestrator.retryGroup(group.id)
          : null,
      onCancel: group.state.isProcessing
          ? () => orchestrator.cancelGroup(group.id)
          : null,
      onDelete: () => _confirmDelete(group),
    );
  }

  void _openGroupDetail(VideoGroup group) {
    if (group.state == PipelineState.combined) {
      // Navigate to trim screen.
      Navigator.pushNamed(context, '/trim', arguments: group.id);
    } else if (group.state.isProcessing) {
      // Navigate to processing screen.
      Navigator.pushNamed(context, '/processing', arguments: group.id);
    }
  }

  Future<void> _scanForRecordings() async {
    setState(() => _isScanning = true);

    try {
      // In a real app, this would use CameraService to list files
      // and group them into VideoGroups.
      final orchestrator = ref.read(pipelineProvider.notifier);

      // Placeholder: show camera setup if not configured.
      if (!mounted) return;

      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Set up your camera connection in Settings first.'),
          action: SnackBarAction(label: 'Setup', onPressed: null),
        ),
      );

      // The actual implementation would:
      // 1. cameraService.checkAvailability()
      // 2. cameraService.listFiles(startTime, endTime)
      // 3. VideoGroup.groupByTime(files)
      // 4. orchestrator.addGroup(group) for each new group
      // 5. orchestrator.processGroup(group.id) for auto-process

      // Example of adding a dummy group for testing:
      // final group = VideoGroup(
      //   id: DateTime.now().millisecondsSinceEpoch.toString(),
      //   name: 'Test Group',
      //   files: [],
      //   createdAt: DateTime.now(),
      // );
      // orchestrator.addGroup(group);

      _ = orchestrator; // suppress unused variable warning
    } finally {
      if (mounted) {
        setState(() => _isScanning = false);
      }
    }
  }

  void _confirmDelete(VideoGroup group) {
    showDialog(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete Recording'),
        content: Text(
          'Delete "${group.name}" and all associated files?\n'
          'This cannot be undone.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () {
              ref.read(pipelineProvider.notifier).cleanupGroup(group.id);
              Navigator.pop(context);
            },
            style: FilledButton.styleFrom(
              backgroundColor: Theme.of(context).colorScheme.error,
            ),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
  }
}
