/// E2E integration test: full video processing pipeline with live UI.
///
/// Launches the real app, configures camera to point at the emulator,
/// then drives the pipeline through the actual UI screens:
///   1. Camera Setup -> enter emulator config -> save
///   2. Dashboard -> tap "Scan Camera" -> discover files
///   3. Watch pipeline progress: download -> combine -> (wait for trim)
///
/// Prerequisites:
///   Camera emulators running (Docker):
///     docker compose --profile dahua up -d --build
///
///   Android emulator running:
///     emulator -avd Pixel_6
///
/// Run with:
///   cd mobile && flutter test integration_test/pipeline_e2e_test.dart -d emulator-5554
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:soccer_cam_mobile/app.dart';
import 'package:soccer_cam_mobile/models/camera_config.dart';
import 'package:soccer_cam_mobile/models/pipeline_state.dart';
import 'package:soccer_cam_mobile/services/pipeline_orchestrator.dart';
import 'package:soccer_cam_mobile/utils/storage_manager.dart';

/// Default host: 127.0.0.1 via `adb reverse tcp:9554 tcp:9554` for fast
/// USB-bridged downloads (~37 MB/s vs ~0.7 MB/s through 10.0.2.2).
String get emulatorHost =>
    Platform.environment['EMULATOR_HOST'] ?? '127.0.0.1';

int get dahuaPort =>
    int.parse(Platform.environment['DAHUA_PORT'] ?? '9554');

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('Full pipeline e2e: setup -> scan -> download -> combine',
      (tester) async {
    // Initialize storage for FFmpeg temp files.
    await StorageManager.instance.initialize();

    // Clear SharedPreferences so we always start on Camera Setup.
    final prefs = await SharedPreferences.getInstance();
    await prefs.clear();
    debugPrint('E2E: Cleared SharedPreferences for fresh start');

    // Create a ProviderScope container so we can inject the camera config.
    final container = ProviderContainer();
    addTearDown(container.dispose);

    // Pre-configure camera to point at the emulator.
    container.read(cameraConfigProvider.notifier).state = CameraConfig(
      host: emulatorHost,
      port: dahuaPort,
      username: 'admin',
      password: 'admin',
      cameraType: CameraType.dahua,
      downloadTimeoutSeconds: 600,
    );

    // Launch the real app with our pre-configured container.
    await tester.pumpWidget(
      UncontrolledProviderScope(
        container: container,
        child: const SoccerCamApp(),
      ),
    );

    // Wait for the startup router to check config and navigate.
    await tester.pumpAndSettle(const Duration(seconds: 3));

    // With cleared prefs, StartupRouter should go to CameraSetup.
    expect(find.text('Camera Setup'), findsOneWidget,
        reason: 'Should start on Camera Setup screen');
    debugPrint('E2E: On Camera Setup screen, filling in config...');

    // Find all TextFormFields.
    final formFields = find.byType(TextFormField);
    final fieldCount = formFields.evaluate().length;
    debugPrint('E2E: Found $fieldCount form fields');

    // Field order: IP Address, Port, Channel, Username, Password
    if (fieldCount >= 1) {
      await tester.enterText(formFields.at(0), emulatorHost);
    }
    if (fieldCount >= 2) {
      await tester.enterText(formFields.at(1), dahuaPort.toString());
    }
    // Channel (index 2) defaults to 1, leave it.
    // Username (index 3) defaults to admin, leave it.
    if (fieldCount >= 5) {
      await tester.enterText(formFields.at(4), 'admin');
    }
    await tester.pumpAndSettle();

    // Dismiss the soft keyboard.
    FocusManager.instance.primaryFocus?.unfocus();
    await tester.pumpAndSettle();

    // Scroll down to reveal Save button.
    final scrollable = find.byType(Scrollable);
    if (scrollable.evaluate().isNotEmpty) {
      await tester.drag(scrollable.first, const Offset(0, -500));
      await tester.pumpAndSettle();
    }

    // Tap Save Configuration.
    await tester.tap(find.text('Save Configuration'));
    debugPrint('E2E: Tapped Save Configuration');

    // After save, the app shows a SnackBar and navigates to Dashboard.
    // SnackBar timers prevent pumpAndSettle from completing, so use
    // explicit pump calls to let the navigation animation finish.
    for (var i = 0; i < 10; i++) {
      await tester.pump(const Duration(milliseconds: 500));
    }

    debugPrint('E2E: Config saved, checking for dashboard...');

    // Verify we see the dashboard elements.
    // Poll a few times in case navigation is still settling.
    Finder scanButton = find.text('Scan Camera');
    for (var i = 0; i < 10; i++) {
      if (scanButton.evaluate().isNotEmpty) break;
      await tester.pump(const Duration(milliseconds: 500));
      scanButton = find.text('Scan Camera');
    }
    expect(scanButton, findsOneWidget,
        reason: 'Dashboard should show Scan Camera button');

    debugPrint('E2E: On Dashboard, tapping Scan Camera...');

    // Tap Scan Camera to discover recordings from the emulator.
    await tester.tap(scanButton);
    await tester.pump(); // Start the async operation.

    // Wait for scanning to complete (network calls to emulator).
    debugPrint('E2E: Scanning for recordings...');

    // Poll until scanning completes (max 30 seconds).
    for (var i = 0; i < 60; i++) {
      await tester.pump(const Duration(milliseconds: 500));
      final scanning = find.text('Scanning...');
      if (scanning.evaluate().isEmpty) break;
    }

    // Give a moment for state to propagate.
    for (var i = 0; i < 5; i++) {
      await tester.pump(const Duration(milliseconds: 200));
    }
    debugPrint('E2E: Scan complete');

    // Check the pipeline state -- groups should have been added.
    final groups = container.read(pipelineProvider);

    debugPrint('E2E: Pipeline has ${groups.length} groups');

    if (groups.isNotEmpty) {
      debugPrint('E2E: Groups discovered:');
      for (final entry in groups.entries) {
        final group = entry.value;
        debugPrint(
            '  - ${group.name}: ${group.fileCount} files, state=${group.state}');
      }

      // Listen to progress stream for download diagnostics.
      final orchestrator = container.read(pipelineProvider.notifier);
      double lastLoggedProgress = -1.0;
      final progressSub = orchestrator.progressStream.listen((progress) {
        // Log every 5% to avoid spam.
        final pct = progress.progress * 100;
        if (pct - lastLoggedProgress >= 5.0 || progress.stage != PipelineState.downloading) {
          lastLoggedProgress = pct;
          debugPrint(
              'E2E PROGRESS: stage=${progress.stage}, '
              'progress=${pct.toStringAsFixed(1)}%, '
              'msg=${progress.message}');
        }
      });

      // Wait for the pipeline to progress through download and combine.
      // Downloads can be slow on the emulator's virtual network.
      debugPrint('E2E: Waiting for pipeline to progress...');

      PipelineState? lastState;
      for (var i = 0; i < 600; i++) {
        await tester.pump(const Duration(seconds: 1));

        final currentGroups = container.read(pipelineProvider);
        for (final group in currentGroups.values) {
          // Log on state change or every 10 seconds.
          if (group.state != lastState || i % 10 == 0) {
            debugPrint(
                '  [${i}s] ${group.name}: state=${group.state}, '
                'files=${group.files.where((f) => f.localPath != null).length}/${group.fileCount} downloaded, '
                'error=${group.errorMessage ?? "none"}');
            lastState = group.state;
          }
        }

        // Check if any group has reached 'combined' or beyond.
        final anyCombined = currentGroups.values.any((g) =>
            g.state == PipelineState.combined ||
            g.state == PipelineState.trimmed ||
            g.state == PipelineState.complete);

        // Also check if downloaded (download finished, combine pending).
        final anyDownloaded = currentGroups.values.any((g) =>
            g.state == PipelineState.downloaded ||
            g.state == PipelineState.combining);

        final anyError =
            currentGroups.values.any((g) => g.state == PipelineState.error);

        if (anyCombined) {
          debugPrint('E2E: SUCCESS - At least one group reached combined!');
          break;
        }

        if (anyDownloaded && i % 5 == 0) {
          debugPrint('E2E: Download complete, combining in progress...');
        }

        if (anyError) {
          final errorGroup = currentGroups.values
              .firstWhere((g) => g.state == PipelineState.error);
          debugPrint(
              'E2E: ERROR - Group "${errorGroup.name}" failed: ${errorGroup.errorMessage}');
          break;
        }
      }

      await progressSub.cancel();

      // Pump a few more times to update the UI with final state.
      for (var i = 0; i < 5; i++) {
        await tester.pump(const Duration(milliseconds: 200));
      }

      // Final state check.
      final finalGroups = container.read(pipelineProvider);
      for (final group in finalGroups.values) {
        debugPrint(
            'E2E FINAL: ${group.name}: state=${group.state}, '
            'combined=${group.combinedFilePath}, '
            'files downloaded=${group.files.where((f) => f.localPath != null).length}/${group.fileCount}');
      }

      // Assert true success: at least one group reached combined or beyond.
      final anySuccess = finalGroups.values.any((g) =>
          g.state == PipelineState.combined ||
          g.state == PipelineState.trimmed ||
          g.state == PipelineState.complete);
      final anyError =
          finalGroups.values.any((g) => g.state == PipelineState.error);

      if (anyError) {
        final errorGroup = finalGroups.values
            .firstWhere((g) => g.state == PipelineState.error);
        fail('Pipeline failed: ${errorGroup.errorMessage}');
      }

      expect(anySuccess, isTrue,
          reason: 'At least one group should have reached combined state. '
              'States: ${finalGroups.values.map((g) => '${g.name}=${g.state}').join(', ')}');
    } else {
      debugPrint(
          'E2E: No groups found -- check camera emulator at $emulatorHost:$dahuaPort');
      fail(
          'No groups discovered from camera emulator at $emulatorHost:$dahuaPort');
    }

  });
}
