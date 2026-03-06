/// E2E integration test: Flutter mobile app against the camera emulator.
///
/// Tests both Dahua and ReoLink camera protocols through the same emulator
/// infrastructure, exercising:
///   1. Authentication (Digest for Dahua, Token for ReoLink)
///   2. File discovery
///   3. File download
///
/// Prerequisites:
///   - Dahua emulator running on DAHUA_PORT (default 8554):
///       CAMERA_TYPE=dahua PORT=8554 python tests/camera_emulator/server.py
///   - ReoLink emulator running on REOLINK_PORT (default 8555):
///       CAMERA_TYPE=reolink PORT=8555 python tests/camera_emulator/server.py
///
///   Or via Docker:
///       docker compose -f tests/docker-compose.e2e.yml up
///
/// Run with:
///   cd mobile && flutter test integration_test/camera_emulator_e2e_test.dart
///
/// Override host/port for Android emulator or custom setup:
///   EMULATOR_HOST=10.0.2.2 flutter test integration_test/...
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:soccer_cam_mobile/models/camera_config.dart';
import 'package:soccer_cam_mobile/models/recording_file.dart';
import 'package:soccer_cam_mobile/services/camera_service.dart';

/// Default host: 10.0.2.2 reaches host loopback from Android emulator.
/// On desktop or with a real device on the same network, use 127.0.0.1.
String get emulatorHost =>
    Platform.environment['EMULATOR_HOST'] ?? '10.0.2.2';

int get dahuaPort =>
    int.parse(Platform.environment['DAHUA_PORT'] ?? '9554');

int get reolinkPort =>
    int.parse(Platform.environment['REOLINK_PORT'] ?? '9555');

/// Shared test cases for any camera type.
///
/// Both Dahua and ReoLink emulators return the same 6 test files in 2 groups,
/// so the same assertions apply to both.
void cameraTestSuite({
  required String label,
  required CameraConfig Function() configFactory,
}) {
  late CameraConfig config;
  late CameraService service;

  setUpAll(() {
    config = configFactory();
    service = CameraService.create(config: config);
  });

  tearDownAll(() {
    service.dispose();
  });

  group('$label - Authentication', () {
    testWidgets('authenticates with correct credentials', (tester) async {
      final available = await service.checkAvailability();
      expect(available, isTrue, reason: 'Camera emulator should be reachable');
    });

    testWidgets('rejects wrong password', (tester) async {
      final badConfig = config.copyWith(password: 'wrong_password');
      final badService = CameraService.create(config: badConfig);
      try {
        final available = await badService.checkAvailability();
        expect(available, isFalse,
            reason: 'Wrong password should fail authentication');
      } finally {
        badService.dispose();
      }
    });
  });

  group('$label - File Discovery', () {
    testWidgets('discovers 6 recording files', (tester) async {
      final files = await service.listFiles(
        startTime: DateTime.now().subtract(const Duration(hours: 24)),
        endTime: DateTime.now(),
      );

      expect(files.length, equals(6), reason: 'Emulator generates 6 files');
      for (final file in files) {
        expect(file.filePath, isNotEmpty);
        expect(file.fileSize, greaterThan(0));
        expect(file.startTime, isNotNull);
        expect(file.endTime, isNotNull);
      }
    });

    testWidgets('files have valid timestamps', (tester) async {
      final files = await service.listFiles(
        startTime: DateTime.now().subtract(const Duration(hours: 24)),
        endTime: DateTime.now(),
      );

      for (final file in files) {
        expect(file.startTime.isBefore(file.endTime), isTrue,
            reason: 'Start time should precede end time');
        final cutoff = DateTime.now().subtract(const Duration(hours: 25));
        expect(file.startTime.isAfter(cutoff), isTrue,
            reason: 'File timestamps should be recent');
      }
    });

    testWidgets('detects 2 groups with time gap', (tester) async {
      final files = await service.listFiles(
        startTime: DateTime.now().subtract(const Duration(hours: 24)),
        endTime: DateTime.now(),
      );

      final sorted = List<RecordingFile>.of(files)
        ..sort((a, b) => a.startTime.compareTo(b.startTime));

      Duration maxGap = Duration.zero;
      int gapIndex = 0;
      for (int i = 1; i < sorted.length; i++) {
        final gap = sorted[i].startTime.difference(sorted[i - 1].endTime);
        if (gap > maxGap) {
          maxGap = gap;
          gapIndex = i;
        }
      }

      expect(gapIndex, equals(3),
          reason: 'Gap should be between file 3 and 4 (two groups of 3)');
      expect(maxGap.inSeconds, greaterThanOrEqualTo(5),
          reason: 'Gap between groups should be at least 5 seconds');
    });
  });

  group('$label - File Download', () {
    testWidgets('downloads a single file with progress', (tester) async {
      final files = await service.listFiles(
        startTime: DateTime.now().subtract(const Duration(hours: 24)),
        endTime: DateTime.now(),
      );
      expect(files, isNotEmpty);

      final tempDir = await Directory.systemTemp.createTemp('flutter_e2e_');
      try {
        final file = files.first;
        final outputPath = '${tempDir.path}/${file.fileName}';
        int progressCalls = 0;

        await service.downloadFile(
          file,
          savePath: outputPath,
          onProgress: (received, total) {
            progressCalls++;
            expect(received, greaterThanOrEqualTo(0));
            expect(total, greaterThan(0));
          },
        );

        final downloaded = File(outputPath);
        expect(downloaded.existsSync(), isTrue,
            reason: 'Downloaded file should exist on disk');
        expect(downloaded.lengthSync(), greaterThan(0),
            reason: 'Downloaded file should not be empty');
        expect(progressCalls, greaterThan(0),
            reason: 'Progress callback should have been called');
      } finally {
        await tempDir.delete(recursive: true);
      }
    });

    testWidgets('downloads all 3 files in a group', (tester) async {
      final files = await service.listFiles(
        startTime: DateTime.now().subtract(const Duration(hours: 24)),
        endTime: DateTime.now(),
      );

      final tempDir =
          await Directory.systemTemp.createTemp('flutter_e2e_grp_');
      try {
        final groupFiles = files.take(3).toList();
        for (final file in groupFiles) {
          final outputPath = '${tempDir.path}/${file.fileName}';
          await service.downloadFile(file, savePath: outputPath);

          final downloaded = File(outputPath);
          expect(downloaded.existsSync(), isTrue);
          expect(downloaded.lengthSync(), greaterThan(0));
        }

        final downloadedFiles = tempDir.listSync().whereType<File>().toList();
        expect(downloadedFiles.length, equals(3),
            reason: 'All 3 files in the group should be downloaded');
      } finally {
        await tempDir.delete(recursive: true);
      }
    });
  });
}

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  // ── Dahua camera tests ──────────────────────────────────────────────
  cameraTestSuite(
    label: 'Dahua',
    configFactory: () => CameraConfig(
      host: emulatorHost,
      port: dahuaPort,
      username: 'admin',
      password: 'admin',
      cameraType: CameraType.dahua,
    ),
  );

  // ── ReoLink camera tests ────────────────────────────────────────────
  cameraTestSuite(
    label: 'ReoLink',
    configFactory: () => CameraConfig(
      host: emulatorHost,
      port: reolinkPort,
      username: 'admin',
      password: 'admin',
      channel: 0,
      cameraType: CameraType.reolink,
    ),
  );
}
