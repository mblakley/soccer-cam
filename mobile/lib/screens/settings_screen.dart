import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../services/pipeline_orchestrator.dart';
import '../utils/storage_manager.dart';

/// Application settings screen.
///
/// Provides configuration for camera, storage limits, YouTube account,
/// and general app settings.
class SettingsScreen extends ConsumerStatefulWidget {
  const SettingsScreen({super.key});

  @override
  ConsumerState<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends ConsumerState<SettingsScreen> {
  String _storageUsed = 'Calculating...';
  bool _autoProcess = true;
  bool _autoUpload = false;
  bool _cleanupAfterUpload = true;
  int _maxGapMinutes = 5;
  String _defaultPrivacy = 'unlisted';
  bool _youtubeSignedIn = false;
  String? _youtubeEmail;

  @override
  void initState() {
    super.initState();
    _loadSettings();
    _calculateStorage();
  }

  Future<void> _loadSettings() async {
    final prefs = await SharedPreferences.getInstance();
    setState(() {
      _autoProcess = prefs.getBool('auto_process') ?? true;
      _autoUpload = prefs.getBool('auto_upload') ?? false;
      _cleanupAfterUpload = prefs.getBool('cleanup_after_upload') ?? true;
      _maxGapMinutes = prefs.getInt('max_gap_minutes') ?? 5;
      _defaultPrivacy = prefs.getString('default_privacy') ?? 'unlisted';
    });

    // Check YouTube sign-in status.
    final youtubeService = ref.read(youtubeServiceProvider);
    setState(() {
      _youtubeSignedIn = youtubeService.isAuthenticated;
      _youtubeEmail = youtubeService.currentUserEmail;
    });
  }

  Future<void> _calculateStorage() async {
    try {
      final storage = StorageManager.instance;
      final bytes = await storage.getTotalStorageUsed();
      setState(() {
        _storageUsed = StorageManager.formatBytes(bytes);
      });
    } catch (_) {
      setState(() => _storageUsed = 'Unable to calculate');
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Settings'),
      ),
      body: ListView(
        children: [
          // Camera section.
          _SectionHeader(title: 'Camera'),
          ListTile(
            leading: const Icon(Icons.videocam),
            title: const Text('Camera Configuration'),
            subtitle: const Text('IP address, credentials, channel'),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => Navigator.pushNamed(context, '/camera-setup'),
          ),
          const Divider(),

          // Processing section.
          _SectionHeader(title: 'Processing'),
          SwitchListTile(
            secondary: const Icon(Icons.auto_mode),
            title: const Text('Auto-process'),
            subtitle: const Text(
              'Automatically start processing after scan',
            ),
            value: _autoProcess,
            onChanged: (value) async {
              setState(() => _autoProcess = value);
              final prefs = await SharedPreferences.getInstance();
              await prefs.setBool('auto_process', value);
            },
          ),
          ListTile(
            leading: const Icon(Icons.timer),
            title: const Text('Group gap threshold'),
            subtitle: Text('$_maxGapMinutes minutes between recordings'),
            trailing: SizedBox(
              width: 120,
              child: Slider(
                value: _maxGapMinutes.toDouble(),
                min: 1,
                max: 30,
                divisions: 29,
                label: '$_maxGapMinutes min',
                onChanged: (value) async {
                  setState(() => _maxGapMinutes = value.round());
                  final prefs = await SharedPreferences.getInstance();
                  await prefs.setInt('max_gap_minutes', _maxGapMinutes);
                },
              ),
            ),
          ),
          const Divider(),

          // YouTube section.
          _SectionHeader(title: 'YouTube'),
          ListTile(
            leading: Icon(
              Icons.account_circle,
              color: _youtubeSignedIn ? Colors.green : null,
            ),
            title: Text(
              _youtubeSignedIn
                  ? 'Signed in as $_youtubeEmail'
                  : 'Not signed in',
            ),
            subtitle: Text(
              _youtubeSignedIn
                  ? 'Tap to sign out'
                  : 'Sign in to upload videos',
            ),
            trailing: _youtubeSignedIn
                ? TextButton(
                    onPressed: _signOutYouTube,
                    child: const Text('Sign Out'),
                  )
                : FilledButton.tonal(
                    onPressed: _signInYouTube,
                    child: const Text('Sign In'),
                  ),
          ),
          SwitchListTile(
            secondary: const Icon(Icons.upload),
            title: const Text('Auto-upload'),
            subtitle: const Text(
              'Automatically upload after trimming',
            ),
            value: _autoUpload,
            onChanged: (value) async {
              setState(() => _autoUpload = value);
              final prefs = await SharedPreferences.getInstance();
              await prefs.setBool('auto_upload', value);
            },
          ),
          ListTile(
            leading: const Icon(Icons.privacy_tip),
            title: const Text('Default privacy'),
            trailing: DropdownButton<String>(
              value: _defaultPrivacy,
              onChanged: (value) async {
                if (value == null) return;
                setState(() => _defaultPrivacy = value);
                final prefs = await SharedPreferences.getInstance();
                await prefs.setString('default_privacy', value);
              },
              items: const [
                DropdownMenuItem(value: 'public', child: Text('Public')),
                DropdownMenuItem(value: 'unlisted', child: Text('Unlisted')),
                DropdownMenuItem(value: 'private', child: Text('Private')),
              ],
            ),
          ),
          const Divider(),

          // Storage section.
          _SectionHeader(title: 'Storage'),
          ListTile(
            leading: const Icon(Icons.storage),
            title: const Text('Storage used'),
            subtitle: Text(_storageUsed),
            trailing: TextButton(
              onPressed: _calculateStorage,
              child: const Text('Refresh'),
            ),
          ),
          SwitchListTile(
            secondary: const Icon(Icons.cleaning_services),
            title: const Text('Clean up after upload'),
            subtitle: const Text(
              'Delete local files after successful YouTube upload',
            ),
            value: _cleanupAfterUpload,
            onChanged: (value) async {
              setState(() => _cleanupAfterUpload = value);
              final prefs = await SharedPreferences.getInstance();
              await prefs.setBool('cleanup_after_upload', value);
            },
          ),
          ListTile(
            leading: const Icon(Icons.delete_sweep),
            title: const Text('Clear all local files'),
            subtitle: const Text('Free up storage space'),
            trailing: TextButton(
              onPressed: _clearAllFiles,
              child: Text(
                'Clear',
                style: TextStyle(color: theme.colorScheme.error),
              ),
            ),
          ),
          const Divider(),

          // About section.
          _SectionHeader(title: 'About'),
          const ListTile(
            leading: Icon(Icons.info),
            title: Text('Soccer Cam Mobile'),
            subtitle: Text('Version 1.0.0'),
          ),
          const SizedBox(height: 32),
        ],
      ),
    );
  }

  Future<void> _signInYouTube() async {
    final youtubeService = ref.read(youtubeServiceProvider);
    final success = await youtubeService.signIn();
    setState(() {
      _youtubeSignedIn = success;
      _youtubeEmail = youtubeService.currentUserEmail;
    });

    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            success
                ? 'Signed in to YouTube'
                : 'YouTube sign-in cancelled or failed',
          ),
          behavior: SnackBarBehavior.floating,
        ),
      );
    }
  }

  Future<void> _signOutYouTube() async {
    final youtubeService = ref.read(youtubeServiceProvider);
    await youtubeService.signOut();
    setState(() {
      _youtubeSignedIn = false;
      _youtubeEmail = null;
    });
  }

  Future<void> _clearAllFiles() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Clear All Files'),
        content: const Text(
          'This will delete all downloaded and processed video files.\n\n'
          'Completed YouTube uploads will not be affected.\n'
          'This cannot be undone.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            style: FilledButton.styleFrom(
              backgroundColor: Theme.of(context).colorScheme.error,
            ),
            child: const Text('Clear All'),
          ),
        ],
      ),
    );

    if (confirmed == true) {
      try {
        final storage = StorageManager.instance;
        await storage.cleanupTemp();
        // Clean all group directories.
        final downloadDir = storage.downloadDir;
        if (await downloadDir.exists()) {
          await for (final entity in downloadDir.list()) {
            await entity.delete(recursive: true);
          }
        }
        final processedDir = storage.processedDir;
        if (await processedDir.exists()) {
          await for (final entity in processedDir.list()) {
            await entity.delete(recursive: true);
          }
        }
        await _calculateStorage();

        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(
              content: Text('All local files cleared'),
              behavior: SnackBarBehavior.floating,
            ),
          );
        }
      } catch (e) {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: Text('Error clearing files: $e'),
              behavior: SnackBarBehavior.floating,
            ),
          );
        }
      }
    }
  }
}

class _SectionHeader extends StatelessWidget {
  const _SectionHeader({required this.title});
  final String title;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
      child: Text(
        title,
        style: Theme.of(context).textTheme.titleSmall?.copyWith(
              color: Theme.of(context).colorScheme.primary,
              fontWeight: FontWeight.w600,
            ),
      ),
    );
  }
}
