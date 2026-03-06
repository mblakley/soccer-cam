/// Supported camera types.
enum CameraType {
  dahua,
  reolink;

  String get displayName {
    switch (this) {
      case CameraType.dahua:
        return 'Dahua';
      case CameraType.reolink:
        return 'ReoLink';
    }
  }
}

/// Configuration for connecting to an IP camera.
class CameraConfig {
  const CameraConfig({
    required this.host,
    required this.username,
    required this.password,
    this.port = 80,
    this.channel = 1,
    this.protocol = 'http',
    this.connectTimeoutSeconds = 10,
    this.downloadTimeoutSeconds = 300,
    this.cameraType = CameraType.dahua,
  });

  final String host;
  final int port;
  final String username;
  final String password;
  final int channel;
  final String protocol;
  final int connectTimeoutSeconds;
  final int downloadTimeoutSeconds;
  final CameraType cameraType;

  String get baseUrl => '$protocol://$host:$port';

  factory CameraConfig.fromJson(Map<String, dynamic> json) {
    return CameraConfig(
      host: json['host'] as String,
      port: json['port'] as int? ?? 80,
      username: json['username'] as String,
      password: json['password'] as String,
      channel: json['channel'] as int? ?? 1,
      protocol: json['protocol'] as String? ?? 'http',
      connectTimeoutSeconds: json['connect_timeout_seconds'] as int? ?? 10,
      downloadTimeoutSeconds: json['download_timeout_seconds'] as int? ?? 300,
      cameraType: CameraType.values.firstWhere(
        (t) => t.name == (json['camera_type'] as String? ?? 'dahua'),
        orElse: () => CameraType.dahua,
      ),
    );
  }

  Map<String, dynamic> toJson() {
    return {
      'host': host,
      'port': port,
      'username': username,
      'password': password,
      'channel': channel,
      'protocol': protocol,
      'connect_timeout_seconds': connectTimeoutSeconds,
      'download_timeout_seconds': downloadTimeoutSeconds,
      'camera_type': cameraType.name,
    };
  }

  CameraConfig copyWith({
    String? host,
    int? port,
    String? username,
    String? password,
    int? channel,
    String? protocol,
    int? connectTimeoutSeconds,
    int? downloadTimeoutSeconds,
    CameraType? cameraType,
  }) {
    return CameraConfig(
      host: host ?? this.host,
      port: port ?? this.port,
      username: username ?? this.username,
      password: password ?? this.password,
      channel: channel ?? this.channel,
      protocol: protocol ?? this.protocol,
      connectTimeoutSeconds:
          connectTimeoutSeconds ?? this.connectTimeoutSeconds,
      downloadTimeoutSeconds:
          downloadTimeoutSeconds ?? this.downloadTimeoutSeconds,
      cameraType: cameraType ?? this.cameraType,
    );
  }

  @override
  String toString() =>
      'CameraConfig(${cameraType.displayName} $baseUrl, channel=$channel)';
}
