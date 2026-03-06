import 'dart:async';
import 'dart:io';
import 'package:googleapis/youtube/v3.dart' as yt;
import 'package:googleapis_auth/googleapis_auth.dart' as auth;
import 'package:google_sign_in/google_sign_in.dart';

/// Callback for upload progress updates.
typedef UploadProgressCallback = void Function(
  int bytesSent,
  int totalBytes,
  double progress,
);

/// Service for uploading videos to YouTube via the Data API v3.
///
/// Uses Google Sign-In for OAuth 2.0 authentication with the
/// youtube.upload scope.
class YouTubeService {
  YouTubeService();

  GoogleSignIn? _googleSignIn;
  yt.YouTubeApi? _youtubeApi;
  auth.AuthClient? _authClient;
  bool _isAuthenticated = false;

  /// Whether the user is currently authenticated.
  bool get isAuthenticated => _isAuthenticated;

  /// The currently signed-in Google account email.
  String? get currentUserEmail => _googleSignIn?.currentUser?.email;

  /// Initialize Google Sign-In and check for existing session.
  Future<void> initialize() async {
    _googleSignIn = GoogleSignIn(
      scopes: [
        'https://www.googleapis.com/auth/youtube.upload',
        'https://www.googleapis.com/auth/youtube',
      ],
    );

    // Try to restore previous sign-in silently.
    final account = await _googleSignIn!.signInSilently();
    if (account != null) {
      await _setupApiClient(account);
    }
  }

  /// Sign in to Google and authorize YouTube access.
  ///
  /// Returns true if sign-in was successful.
  Future<bool> signIn() async {
    try {
      final account = await _googleSignIn?.signIn();
      if (account == null) return false;
      await _setupApiClient(account);
      return true;
    } catch (e) {
      _isAuthenticated = false;
      return false;
    }
  }

  /// Sign out and revoke access.
  Future<void> signOut() async {
    await _googleSignIn?.signOut();
    _authClient?.close();
    _authClient = null;
    _youtubeApi = null;
    _isAuthenticated = false;
  }

  /// Upload a video file to YouTube.
  ///
  /// [filePath] is the local path to the MP4 file.
  /// [title] is the video title.
  /// [description] is the video description.
  /// [privacyStatus] is one of: 'public', 'unlisted', 'private'.
  /// [tags] are optional video tags.
  /// [onProgress] is called with upload progress updates.
  ///
  /// Returns the YouTube video ID on success.
  Future<String> uploadVideo({
    required String filePath,
    required String title,
    String description = '',
    String privacyStatus = 'unlisted',
    List<String>? tags,
    UploadProgressCallback? onProgress,
  }) async {
    if (!_isAuthenticated || _youtubeApi == null) {
      throw YouTubeException('Not authenticated. Call signIn() first.');
    }

    final file = File(filePath);
    if (!await file.exists()) {
      throw YouTubeException('Video file not found: $filePath');
    }

    final fileSize = await file.length();

    // Build the Video resource.
    final video = yt.Video()
      ..snippet = (yt.VideoSnippet()
        ..title = title
        ..description = description
        ..tags = tags
        ..categoryId = '17') // Sports category
      ..status = (yt.VideoStatus()..privacyStatus = privacyStatus);

    // Create a progress-tracking stream.
    var bytesSent = 0;
    final fileStream = file.openRead().map((chunk) {
      bytesSent += chunk.length;
      onProgress?.call(bytesSent, fileSize, bytesSent / fileSize);
      return chunk;
    });

    final media = yt.Media(fileStream, fileSize);

    try {
      final response = await _youtubeApi!.videos.insert(
        video,
        ['snippet', 'status'],
        uploadMedia: media,
      );

      final videoId = response.id;
      if (videoId == null) {
        throw YouTubeException('Upload succeeded but no video ID returned');
      }

      return videoId;
    } catch (e) {
      if (e is YouTubeException) rethrow;
      throw YouTubeException('Upload failed: $e');
    }
  }

  /// Get the URL for an uploaded video.
  static String getVideoUrl(String videoId) {
    return 'https://www.youtube.com/watch?v=$videoId';
  }

  /// List the user's uploaded videos (most recent first).
  Future<List<yt.Video>> listUploads({int maxResults = 10}) async {
    if (!_isAuthenticated || _youtubeApi == null) {
      throw YouTubeException('Not authenticated');
    }

    // Get the user's channel.
    final channelResponse = await _youtubeApi!.channels.list(
      ['contentDetails'],
      mine: true,
    );

    final channels = channelResponse.items;
    if (channels == null || channels.isEmpty) {
      return [];
    }

    final uploadsPlaylistId =
        channels.first.contentDetails?.relatedPlaylists?.uploads;
    if (uploadsPlaylistId == null) return [];

    // Get videos from uploads playlist.
    final playlistResponse = await _youtubeApi!.playlistItems.list(
      ['snippet'],
      playlistId: uploadsPlaylistId,
      maxResults: maxResults,
    );

    final items = playlistResponse.items;
    if (items == null || items.isEmpty) return [];

    // Get full video details.
    final videoIds =
        items.map((i) => i.snippet?.resourceId?.videoId).whereType<String>();
    if (videoIds.isEmpty) return [];

    final videoResponse = await _youtubeApi!.videos.list(
      ['snippet', 'status', 'statistics'],
      id: videoIds.join(','),
    );

    return videoResponse.items ?? [];
  }

  /// Set up the YouTube API client from a signed-in Google account.
  Future<void> _setupApiClient(GoogleSignInAccount account) async {
    final googleAuth = await account.authentication;
    final accessToken = googleAuth.accessToken;

    if (accessToken == null) {
      throw YouTubeException('Failed to get access token');
    }

    final credentials = auth.AccessCredentials(
      auth.AccessToken(
        'Bearer',
        accessToken,
        // Token expiry - will be refreshed by Google Sign-In.
        DateTime.now().toUtc().add(const Duration(hours: 1)),
      ),
      googleAuth.idToken,
      ['https://www.googleapis.com/auth/youtube.upload'],
    );

    _authClient = auth.authenticatedClient(
      auth.clientViaApiKey(''), // base client
      credentials,
    );
    _youtubeApi = yt.YouTubeApi(_authClient!);
    _isAuthenticated = true;
  }

  /// Dispose resources.
  void dispose() {
    _authClient?.close();
  }
}

/// Exception for YouTube API errors.
class YouTubeException implements Exception {
  const YouTubeException(this.message);
  final String message;

  @override
  String toString() => 'YouTubeException: $message';
}
