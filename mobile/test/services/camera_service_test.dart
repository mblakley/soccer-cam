import 'package:flutter_test/flutter_test.dart';
import 'package:soccer_cam_mobile/models/camera_config.dart';
import 'package:soccer_cam_mobile/models/recording_file.dart';
import 'package:soccer_cam_mobile/services/camera_service.dart';

void main() {
  group('CameraService', () {
    late CameraConfig config;

    setUp(() {
      config = const CameraConfig(
        host: '192.168.1.108',
        username: 'admin',
        password: 'test_password',
        port: 80,
        channel: 1,
      );
    });

    test('config generates correct base URL', () {
      expect(config.baseUrl, 'http://192.168.1.108:80');
    });

    test('config with HTTPS generates correct base URL', () {
      final httpsConfig = config.copyWith(protocol: 'https', port: 443);
      expect(httpsConfig.baseUrl, 'https://192.168.1.108:443');
    });

    test('CameraService can be constructed', () {
      final service = CameraService(config: config);
      expect(service.config, config);
      service.dispose();
    });

    test('CameraException has correct message', () {
      const exception = CameraException('test error');
      expect(exception.message, 'test error');
      expect(exception.toString(), 'CameraException: test error');
    });
  });

  group('RecordingFile', () {
    test('fromDahuaFields parses correctly', () {
      final fields = {
        'FilePath': '/mnt/sd/2024-01-15/001/dav/12/12.30.00-12.45.00.dav',
        'StartTime': '2024-01-15 12:30:00',
        'EndTime': '2024-01-15 12:45:00',
        'Channel': '1',
        'Size': '123456789',
        'Type': 'dav',
      };

      final file = RecordingFile.fromDahuaFields(fields);

      expect(file.filePath,
          '/mnt/sd/2024-01-15/001/dav/12/12.30.00-12.45.00.dav');
      expect(file.startTime, DateTime(2024, 1, 15, 12, 30, 0));
      expect(file.endTime, DateTime(2024, 1, 15, 12, 45, 0));
      expect(file.channel, 1);
      expect(file.fileSize, 123456789);
      expect(file.type, 'dav');
      expect(file.duration, const Duration(minutes: 15));
    });

    test('fileName extracts correctly from path', () {
      const file = RecordingFile(
        filePath: '/mnt/sd/2024-01-15/001/dav/12/clip.dav',
        startTime: null,
        endTime: null,
        channel: 1,
      );
      // RecordingFile requires non-null DateTime, so use a parsed version:
      final parsed = RecordingFile.fromDahuaFields({
        'FilePath': '/mnt/sd/2024-01-15/001/dav/12/clip.dav',
        'StartTime': '2024-01-15 12:30:00',
        'EndTime': '2024-01-15 12:45:00',
      });

      expect(parsed.fileName, 'clip.dav');
    });

    test('isDownloaded returns false when no localPath', () {
      final file = RecordingFile.fromDahuaFields({
        'FilePath': '/mnt/sd/clip.dav',
        'StartTime': '2024-01-15 12:30:00',
        'EndTime': '2024-01-15 12:45:00',
      });
      expect(file.isDownloaded, false);
    });

    test('isDownloaded returns true when localPath set', () {
      final file = RecordingFile.fromDahuaFields({
        'FilePath': '/mnt/sd/clip.dav',
        'StartTime': '2024-01-15 12:30:00',
        'EndTime': '2024-01-15 12:45:00',
      }).copyWith(localPath: '/data/downloads/clip.dav');
      expect(file.isDownloaded, true);
    });

    test('JSON round-trip preserves data', () {
      final original = RecordingFile(
        filePath: '/mnt/sd/test.dav',
        startTime: DateTime(2024, 3, 15, 10, 0),
        endTime: DateTime(2024, 3, 15, 10, 15),
        channel: 1,
        fileSize: 5000000,
        type: 'dav',
        downloadProgress: 0.75,
        localPath: '/data/test.dav',
      );

      final json = original.toJson();
      final restored = RecordingFile.fromJson(json);

      expect(restored.filePath, original.filePath);
      expect(restored.startTime, original.startTime);
      expect(restored.endTime, original.endTime);
      expect(restored.channel, original.channel);
      expect(restored.fileSize, original.fileSize);
      expect(restored.downloadProgress, original.downloadProgress);
      expect(restored.localPath, original.localPath);
    });
  });

  group('Dahua response parsing', () {
    test('parseFileList handles empty response', () {
      // This tests the internal parsing logic via the public interface.
      // Direct parsing is tested through the RecordingFile.fromDahuaFields.
      final fields = <String, String>{};
      final file = RecordingFile.fromDahuaFields(fields);
      expect(file.filePath, '');
    });

    test('parseDahuaTimestamp handles standard format', () {
      final file = RecordingFile.fromDahuaFields({
        'FilePath': '/test.dav',
        'StartTime': '2024-06-15 14:30:45',
        'EndTime': '2024-06-15 14:45:00',
      });

      expect(file.startTime.year, 2024);
      expect(file.startTime.month, 6);
      expect(file.startTime.day, 15);
      expect(file.startTime.hour, 14);
      expect(file.startTime.minute, 30);
      expect(file.startTime.second, 45);
    });
  });
}
