/// Smoke test: exercises CameraService against running camera emulators.
///
/// Requires emulators running locally:
///   CAMERA_TYPE=dahua PORT=8554 python tests/camera_emulator/server.py
///   CAMERA_TYPE=reolink PORT=8555 python tests/camera_emulator/server.py
///
/// Run with:
///   cd mobile && EMULATOR_HOST=127.0.0.1 flutter test test/services/camera_emulator_smoke_test.dart
@Tags(['e2e'])
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:soccer_cam_mobile/models/camera_config.dart';
import 'package:soccer_cam_mobile/services/camera_service.dart';

String get emulatorHost =>
    Platform.environment['EMULATOR_HOST'] ?? '127.0.0.1';
int get dahuaPort =>
    int.parse(Platform.environment['DAHUA_PORT'] ?? '9554');
int get reolinkPort =>
    int.parse(Platform.environment['REOLINK_PORT'] ?? '9555');

void runCameraSuite(String label, CameraConfig config) {
  late CameraService service;

  setUpAll(() {
    service = CameraService.create(config: config);
  });

  tearDownAll(() {
    service.dispose();
  });

  test('$label: authenticates with correct credentials', () async {
    final available = await service.checkAvailability();
    expect(available, isTrue);
  });

  test('$label: rejects wrong password', () async {
    final badConfig = config.copyWith(password: 'wrong_password');
    final badService = CameraService.create(config: badConfig);
    try {
      final available = await badService.checkAvailability();
      expect(available, isFalse);
    } finally {
      badService.dispose();
    }
  });

  test('$label: discovers 6 recording files', () async {
    final files = await service.listFiles(
      startTime: DateTime.now().subtract(const Duration(hours: 24)),
      endTime: DateTime.now(),
    );
    expect(files.length, equals(6));
    for (final file in files) {
      expect(file.filePath, isNotEmpty);
      expect(file.fileSize, greaterThan(0));
    }
  });

  test('$label: files have valid timestamps', () async {
    final files = await service.listFiles(
      startTime: DateTime.now().subtract(const Duration(hours: 24)),
      endTime: DateTime.now(),
    );
    for (final file in files) {
      expect(file.startTime.isBefore(file.endTime), isTrue);
    }
  });

  test('$label: downloads a file', () async {
    final files = await service.listFiles(
      startTime: DateTime.now().subtract(const Duration(hours: 24)),
      endTime: DateTime.now(),
    );
    expect(files, isNotEmpty);

    final tempDir = await Directory.systemTemp.createTemp('flutter_smoke_');
    try {
      final file = files.first;
      final outputPath = '${tempDir.path}/${file.fileName}';

      await service.downloadFile(file, savePath: outputPath);

      final downloaded = File(outputPath);
      expect(downloaded.existsSync(), isTrue);
      expect(downloaded.lengthSync(), greaterThan(0));
    } finally {
      await tempDir.delete(recursive: true);
    }
  });
}

void main() {
  group('Dahua emulator', () {
    runCameraSuite(
      'Dahua',
      CameraConfig(
        host: emulatorHost,
        port: dahuaPort,
        username: 'admin',
        password: 'admin',
        cameraType: CameraType.dahua,
      ),
    );
  });

  group('ReoLink emulator', () {
    runCameraSuite(
      'ReoLink',
      CameraConfig(
        host: emulatorHost,
        port: reolinkPort,
        username: 'admin',
        password: 'admin',
        channel: 0,
        cameraType: CameraType.reolink,
      ),
    );
  });
}
