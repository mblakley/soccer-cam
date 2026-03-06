import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'screens/dashboard_screen.dart';
import 'screens/camera_setup_screen.dart';
import 'screens/processing_screen.dart';
import 'screens/trim_screen.dart';
import 'screens/settings_screen.dart';

class SoccerCamApp extends ConsumerWidget {
  const SoccerCamApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return MaterialApp(
      title: 'Soccer Cam',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorSchemeSeed: Colors.green,
        useMaterial3: true,
        brightness: Brightness.light,
      ),
      darkTheme: ThemeData(
        colorSchemeSeed: Colors.green,
        useMaterial3: true,
        brightness: Brightness.dark,
      ),
      themeMode: ThemeMode.system,
      initialRoute: '/',
      routes: {
        '/': (context) => const DashboardScreen(),
        '/camera-setup': (context) => const CameraSetupScreen(),
        '/processing': (context) => const ProcessingScreen(),
        '/trim': (context) => const TrimScreen(),
        '/settings': (context) => const SettingsScreen(),
      },
    );
  }
}
