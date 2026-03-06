import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../models/camera_config.dart';
import '../services/camera_service.dart';

/// Screen for configuring the Dahua camera connection.
///
/// Provides form fields for IP address, port, username, password,
/// and a "Test Connection" button to verify connectivity.
class CameraSetupScreen extends ConsumerStatefulWidget {
  const CameraSetupScreen({super.key});

  @override
  ConsumerState<CameraSetupScreen> createState() => _CameraSetupScreenState();
}

class _CameraSetupScreenState extends ConsumerState<CameraSetupScreen> {
  final _formKey = GlobalKey<FormState>();
  final _hostController = TextEditingController();
  final _portController = TextEditingController(text: '80');
  final _usernameController = TextEditingController(text: 'admin');
  final _passwordController = TextEditingController();
  final _channelController = TextEditingController(text: '1');

  bool _obscurePassword = true;
  bool _isTesting = false;
  _ConnectionTestResult? _testResult;

  @override
  void initState() {
    super.initState();
    _loadSavedConfig();
  }

  @override
  void dispose() {
    _hostController.dispose();
    _portController.dispose();
    _usernameController.dispose();
    _passwordController.dispose();
    _channelController.dispose();
    super.dispose();
  }

  Future<void> _loadSavedConfig() async {
    final prefs = await SharedPreferences.getInstance();
    final host = prefs.getString('camera_host');
    if (host != null) {
      _hostController.text = host;
      _portController.text = (prefs.getInt('camera_port') ?? 80).toString();
      _usernameController.text = prefs.getString('camera_username') ?? 'admin';
      _passwordController.text = prefs.getString('camera_password') ?? '';
      _channelController.text =
          (prefs.getInt('camera_channel') ?? 1).toString();
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Camera Setup'),
      ),
      body: Form(
        key: _formKey,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            // Camera illustration/icon.
            Icon(
              Icons.videocam,
              size: 64,
              color: theme.colorScheme.primary.withValues(alpha: 0.7),
            ),
            const SizedBox(height: 8),
            Text(
              'Dahua Camera Configuration',
              textAlign: TextAlign.center,
              style: theme.textTheme.titleLarge,
            ),
            const SizedBox(height: 4),
            Text(
              'Enter your camera connection details below.',
              textAlign: TextAlign.center,
              style: theme.textTheme.bodyMedium?.copyWith(
                color: theme.colorScheme.outline,
              ),
            ),
            const SizedBox(height: 24),

            // Host field.
            TextFormField(
              controller: _hostController,
              decoration: const InputDecoration(
                labelText: 'Camera IP Address',
                hintText: '192.168.1.108',
                prefixIcon: Icon(Icons.lan),
                border: OutlineInputBorder(),
              ),
              keyboardType: TextInputType.url,
              validator: (value) {
                if (value == null || value.isEmpty) {
                  return 'IP address is required';
                }
                // Basic IP or hostname validation.
                final ipPattern = RegExp(
                  r'^(\d{1,3}\.){3}\d{1,3}$|^[a-zA-Z0-9.-]+$',
                );
                if (!ipPattern.hasMatch(value)) {
                  return 'Enter a valid IP address or hostname';
                }
                return null;
              },
            ),
            const SizedBox(height: 16),

            // Port and Channel row.
            Row(
              children: [
                Expanded(
                  child: TextFormField(
                    controller: _portController,
                    decoration: const InputDecoration(
                      labelText: 'Port',
                      hintText: '80',
                      prefixIcon: Icon(Icons.numbers),
                      border: OutlineInputBorder(),
                    ),
                    keyboardType: TextInputType.number,
                    validator: (value) {
                      if (value == null || value.isEmpty) return 'Required';
                      final port = int.tryParse(value);
                      if (port == null || port < 1 || port > 65535) {
                        return 'Invalid port';
                      }
                      return null;
                    },
                  ),
                ),
                const SizedBox(width: 16),
                Expanded(
                  child: TextFormField(
                    controller: _channelController,
                    decoration: const InputDecoration(
                      labelText: 'Channel',
                      hintText: '1',
                      prefixIcon: Icon(Icons.tv),
                      border: OutlineInputBorder(),
                    ),
                    keyboardType: TextInputType.number,
                    validator: (value) {
                      if (value == null || value.isEmpty) return 'Required';
                      final ch = int.tryParse(value);
                      if (ch == null || ch < 1) return 'Invalid';
                      return null;
                    },
                  ),
                ),
              ],
            ),
            const SizedBox(height: 16),

            // Username field.
            TextFormField(
              controller: _usernameController,
              decoration: const InputDecoration(
                labelText: 'Username',
                hintText: 'admin',
                prefixIcon: Icon(Icons.person),
                border: OutlineInputBorder(),
              ),
              validator: (value) {
                if (value == null || value.isEmpty) {
                  return 'Username is required';
                }
                return null;
              },
            ),
            const SizedBox(height: 16),

            // Password field.
            TextFormField(
              controller: _passwordController,
              decoration: InputDecoration(
                labelText: 'Password',
                prefixIcon: const Icon(Icons.lock),
                border: const OutlineInputBorder(),
                suffixIcon: IconButton(
                  icon: Icon(
                    _obscurePassword
                        ? Icons.visibility_off
                        : Icons.visibility,
                  ),
                  onPressed: () {
                    setState(() => _obscurePassword = !_obscurePassword);
                  },
                ),
              ),
              obscureText: _obscurePassword,
              validator: (value) {
                if (value == null || value.isEmpty) {
                  return 'Password is required';
                }
                return null;
              },
            ),
            const SizedBox(height: 24),

            // Test Connection button.
            FilledButton.tonalIcon(
              onPressed: _isTesting ? null : _testConnection,
              icon: _isTesting
                  ? const SizedBox(
                      width: 20,
                      height: 20,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.wifi_find),
              label: Text(_isTesting ? 'Testing...' : 'Test Connection'),
            ),
            const SizedBox(height: 12),

            // Test result display.
            if (_testResult != null) _buildTestResult(),

            const SizedBox(height: 24),

            // Save button.
            FilledButton.icon(
              onPressed: _saveConfig,
              icon: const Icon(Icons.save),
              label: const Text('Save Configuration'),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildTestResult() {
    final result = _testResult!;
    final theme = Theme.of(context);
    final isSuccess = result.success;

    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: isSuccess
            ? Colors.green.withValues(alpha: 0.1)
            : theme.colorScheme.errorContainer,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(
          color: isSuccess
              ? Colors.green.withValues(alpha: 0.3)
              : theme.colorScheme.error.withValues(alpha: 0.3),
        ),
      ),
      child: Row(
        children: [
          Icon(
            isSuccess ? Icons.check_circle : Icons.error,
            color: isSuccess ? Colors.green : theme.colorScheme.error,
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  isSuccess ? 'Connection successful' : 'Connection failed',
                  style: theme.textTheme.bodyMedium?.copyWith(
                    fontWeight: FontWeight.w600,
                    color:
                        isSuccess ? Colors.green : theme.colorScheme.error,
                  ),
                ),
                if (result.message.isNotEmpty) ...[
                  const SizedBox(height: 4),
                  Text(
                    result.message,
                    style: theme.textTheme.bodySmall,
                  ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }

  CameraConfig _buildConfig() {
    return CameraConfig(
      host: _hostController.text.trim(),
      port: int.tryParse(_portController.text) ?? 80,
      username: _usernameController.text.trim(),
      password: _passwordController.text,
      channel: int.tryParse(_channelController.text) ?? 1,
    );
  }

  Future<void> _testConnection() async {
    if (!_formKey.currentState!.validate()) return;

    setState(() {
      _isTesting = true;
      _testResult = null;
    });

    try {
      final config = _buildConfig();
      final cameraService = CameraService(config: config);

      final available = await cameraService.checkAvailability();
      cameraService.dispose();

      setState(() {
        _testResult = _ConnectionTestResult(
          success: available,
          message: available
              ? 'Camera is reachable at ${config.baseUrl}'
              : 'Camera did not respond. Check IP address and credentials.',
        );
      });
    } catch (e) {
      setState(() {
        _testResult = _ConnectionTestResult(
          success: false,
          message: 'Error: $e',
        );
      });
    } finally {
      setState(() => _isTesting = false);
    }
  }

  Future<void> _saveConfig() async {
    if (!_formKey.currentState!.validate()) return;

    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('camera_host', _hostController.text.trim());
    await prefs.setInt(
      'camera_port',
      int.tryParse(_portController.text) ?? 80,
    );
    await prefs.setString(
      'camera_username',
      _usernameController.text.trim(),
    );
    await prefs.setString('camera_password', _passwordController.text);
    await prefs.setInt(
      'camera_channel',
      int.tryParse(_channelController.text) ?? 1,
    );

    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Camera configuration saved'),
          behavior: SnackBarBehavior.floating,
        ),
      );
      Navigator.pop(context);
    }
  }
}

class _ConnectionTestResult {
  const _ConnectionTestResult({
    required this.success,
    required this.message,
  });
  final bool success;
  final String message;
}
