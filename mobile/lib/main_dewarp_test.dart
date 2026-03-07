import 'package:flutter/material.dart';
import 'screens/dewarp_viewer_screen.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(
    MaterialApp(
      title: 'Dewarp Test',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorSchemeSeed: Colors.green,
        useMaterial3: true,
        brightness: Brightness.dark,
      ),
      home: const DewarpViewerScreen(
        videoPath: '/data/user/0/com.soccercam.soccer_cam_mobile/cache/dahua_gameplay.mp4',
      ),
    ),
  );
}
