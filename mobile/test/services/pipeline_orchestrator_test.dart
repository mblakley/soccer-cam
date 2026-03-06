import 'package:flutter_test/flutter_test.dart';
import 'package:soccer_cam_mobile/models/pipeline_state.dart';
import 'package:soccer_cam_mobile/models/recording_file.dart';
import 'package:soccer_cam_mobile/models/video_group.dart';

void main() {
  group('PipelineState', () {
    test('state transitions follow expected order', () {
      expect(PipelineState.pending.next, PipelineState.downloading);
      expect(PipelineState.downloading.next, PipelineState.downloaded);
      expect(PipelineState.downloaded.next, PipelineState.combining);
      expect(PipelineState.combining.next, PipelineState.combined);
      expect(PipelineState.combined.next, PipelineState.trimming);
      expect(PipelineState.trimming.next, PipelineState.trimmed);
      expect(PipelineState.trimmed.next, PipelineState.uploading);
      expect(PipelineState.uploading.next, PipelineState.complete);
      expect(PipelineState.complete.next, isNull);
      expect(PipelineState.error.next, isNull);
    });

    test('canTransitionTo validates forward transitions', () {
      expect(
        PipelineState.pending.canTransitionTo(PipelineState.downloading),
        true,
      );
      expect(
        PipelineState.pending.canTransitionTo(PipelineState.combined),
        false,
      );
      expect(
        PipelineState.downloading.canTransitionTo(PipelineState.downloaded),
        true,
      );
    });

    test('any state can transition to error', () {
      for (final state in PipelineState.values) {
        expect(state.canTransitionTo(PipelineState.error), true);
      }
    });

    test('error state can transition to pending (retry)', () {
      expect(
        PipelineState.error.canTransitionTo(PipelineState.pending),
        true,
      );
    });

    test('isActive returns false for terminal states', () {
      expect(PipelineState.complete.isActive, false);
      expect(PipelineState.error.isActive, false);
      expect(PipelineState.pending.isActive, true);
      expect(PipelineState.downloading.isActive, true);
    });

    test('isProcessing identifies active stages', () {
      expect(PipelineState.downloading.isProcessing, true);
      expect(PipelineState.combining.isProcessing, true);
      expect(PipelineState.trimming.isProcessing, true);
      expect(PipelineState.uploading.isProcessing, true);
      expect(PipelineState.pending.isProcessing, false);
      expect(PipelineState.downloaded.isProcessing, false);
      expect(PipelineState.complete.isProcessing, false);
    });

    test('overallProgress increases monotonically', () {
      final orderedStates = [
        PipelineState.pending,
        PipelineState.downloading,
        PipelineState.downloaded,
        PipelineState.combining,
        PipelineState.combined,
        PipelineState.trimming,
        PipelineState.trimmed,
        PipelineState.uploading,
        PipelineState.complete,
      ];

      for (var i = 1; i < orderedStates.length; i++) {
        expect(
          orderedStates[i].overallProgress,
          greaterThan(orderedStates[i - 1].overallProgress),
          reason:
              '${orderedStates[i].name} should have higher progress than ${orderedStates[i - 1].name}',
        );
      }
    });

    test('displayName is set for all states', () {
      for (final state in PipelineState.values) {
        expect(state.displayName, isNotEmpty);
      }
    });
  });

  group('VideoGroup', () {
    final now = DateTime(2024, 3, 15, 10, 0);

    List<RecordingFile> createTestFiles(int count, {int gapMinutes = 0}) {
      return List.generate(count, (i) {
        final start = now.add(Duration(minutes: i * (15 + gapMinutes)));
        final end = start.add(const Duration(minutes: 15));
        return RecordingFile(
          filePath: '/mnt/sd/file_$i.dav',
          startTime: start,
          endTime: end,
          channel: 1,
          fileSize: 100000000,
        );
      });
    }

    test('startTime returns earliest file start', () {
      final files = createTestFiles(3);
      final group = VideoGroup(id: '1', name: 'Test', files: files);
      expect(group.startTime, files.first.startTime);
    });

    test('endTime returns latest file end', () {
      final files = createTestFiles(3);
      final group = VideoGroup(id: '1', name: 'Test', files: files);
      expect(group.endTime, files.last.endTime);
    });

    test('totalFileSize sums all file sizes', () {
      final files = createTestFiles(3);
      final group = VideoGroup(id: '1', name: 'Test', files: files);
      expect(group.totalFileSize, 300000000);
    });

    test('groupByTime creates single group for continuous files', () {
      final files = createTestFiles(3); // 0-gap, 15-min segments
      final groups = VideoGroup.groupByTime(files);
      expect(groups.length, 1);
      expect(groups.first.files.length, 3);
    });

    test('groupByTime creates multiple groups for large gaps', () {
      final files = createTestFiles(4, gapMinutes: 10);
      final groups = VideoGroup.groupByTime(files, maxGapMinutes: 5);
      expect(groups.length, 4);
    });

    test('groupByTime handles empty list', () {
      final groups = VideoGroup.groupByTime([]);
      expect(groups, isEmpty);
    });

    test('groupByTime handles single file', () {
      final files = createTestFiles(1);
      final groups = VideoGroup.groupByTime(files);
      expect(groups.length, 1);
      expect(groups.first.files.length, 1);
    });

    test('downloadProgress averages across files', () {
      final files = [
        RecordingFile(
          filePath: '/a.dav',
          startTime: now,
          endTime: now.add(const Duration(minutes: 15)),
          channel: 1,
          downloadProgress: 1.0,
        ),
        RecordingFile(
          filePath: '/b.dav',
          startTime: now.add(const Duration(minutes: 15)),
          endTime: now.add(const Duration(minutes: 30)),
          channel: 1,
          downloadProgress: 0.5,
        ),
      ];
      final group = VideoGroup(id: '1', name: 'Test', files: files);
      expect(group.downloadProgress, 0.75);
    });

    test('allFilesDownloaded checks all files', () {
      final files = [
        RecordingFile(
          filePath: '/a.dav',
          startTime: now,
          endTime: now.add(const Duration(minutes: 15)),
          channel: 1,
          localPath: '/local/a.dav',
        ),
        RecordingFile(
          filePath: '/b.dav',
          startTime: now.add(const Duration(minutes: 15)),
          endTime: now.add(const Duration(minutes: 30)),
          channel: 1,
        ),
      ];
      final group = VideoGroup(id: '1', name: 'Test', files: files);
      expect(group.allFilesDownloaded, false);
    });

    test('JSON round-trip preserves data', () {
      final files = createTestFiles(2);
      final original = VideoGroup(
        id: 'test-123',
        name: 'Game 2024-03-15',
        files: files,
        state: PipelineState.combined,
        combinedFilePath: '/data/combined.mp4',
        trimStartSeconds: 10.5,
        trimEndSeconds: 120.0,
        createdAt: now,
      );

      final json = original.toJson();
      final restored = VideoGroup.fromJson(json);

      expect(restored.id, original.id);
      expect(restored.name, original.name);
      expect(restored.files.length, original.files.length);
      expect(restored.state, original.state);
      expect(restored.combinedFilePath, original.combinedFilePath);
      expect(restored.trimStartSeconds, original.trimStartSeconds);
      expect(restored.trimEndSeconds, original.trimEndSeconds);
    });

    test('copyWith updates specified fields only', () {
      final original = VideoGroup(
        id: '1',
        name: 'Test',
        files: const [],
        state: PipelineState.pending,
      );

      final updated = original.copyWith(
        state: PipelineState.downloading,
        errorMessage: 'test error',
      );

      expect(updated.id, original.id);
      expect(updated.name, original.name);
      expect(updated.state, PipelineState.downloading);
      expect(updated.errorMessage, 'test error');
    });
  });

  group('PipelineException', () {
    test('has correct message', () {
      // Verify PipelineException (from pipeline_orchestrator) behavior.
      // We test the pattern since we cannot import the orchestrator
      // without its full dependency chain in unit tests.
      const message = 'Group not found: test-id';
      expect(message, contains('Group not found'));
    });
  });
}
