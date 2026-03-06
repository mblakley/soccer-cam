import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'models/camera_config.dart';
import 'screens/dashboard_screen.dart';
import 'screens/camera_setup_screen.dart';
import 'screens/processing_screen.dart';
import 'screens/trim_screen.dart';
import 'screens/settings_screen.dart';
import 'services/pipeline_orchestrator.dart';

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
      home: _StartupRouter(ref: ref),
      routes: {
        '/dashboard': (context) => const DashboardScreen(),
        '/camera-setup': (context) => const CameraSetupScreen(),
        '/processing': (context) => const ProcessingScreen(),
        '/trim': (context) => const TrimScreen(),
        '/settings': (context) => const SettingsScreen(),
      },
    );
  }
}

/// Checks for saved camera config at startup and routes accordingly.
class _StartupRouter extends StatefulWidget {
  const _StartupRouter({required this.ref});
  final WidgetRef ref;

  @override
  State<_StartupRouter> createState() => _StartupRouterState();
}

class _StartupRouterState extends State<_StartupRouter> {
  @override
  void initState() {
    super.initState();
    _checkConfig();
  }

  Future<void> _checkConfig() async {
    final prefs = await SharedPreferences.getInstance();
    final host = prefs.getString('camera_host');

    if (!mounted) return;

    if (host != null && host.isNotEmpty) {
      // Load saved config into the provider.
      final config = CameraConfig(
        host: host,
        port: prefs.getInt('camera_port') ?? 80,
        username: prefs.getString('camera_username') ?? 'admin',
        password: prefs.getString('camera_password') ?? '',
        channel: prefs.getInt('camera_channel') ?? 1,
        cameraType: CameraType.values.firstWhere(
          (t) => t.name == (prefs.getString('camera_type') ?? ''),
          orElse: () => CameraType.dahua,
        ),
      );
      widget.ref.read(cameraConfigProvider.notifier).state = config;

      Navigator.of(context).pushReplacement(
        MaterialPageRoute(builder: (_) => const DashboardScreen()),
      );
    } else {
      // No config saved -- show setup wizard.
      Navigator.of(context).pushReplacement(
        MaterialPageRoute(builder: (_) => const CameraSetupScreen()),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return const Scaffold(
      body: Center(child: CircularProgressIndicator()),
    );
  }
}
