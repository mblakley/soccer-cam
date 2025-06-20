# YouTube Upload Setup

This document explains how to set up YouTube API credentials for automatic video uploads.

## Prerequisites

1. A Google account with a YouTube channel
2. Access to the [Google Cloud Console](https://console.cloud.google.com/)

## Setup Instructions

### 1. Create a Google Cloud Project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/)
2. Click on the project dropdown at the top of the page and select "New Project"
3. Enter a name for your project and click "Create"
4. Select your newly created project from the project dropdown

### 2. Enable the YouTube Data API v3

1. In your project, go to "APIs & Services" > "Library"
2. Search for "YouTube Data API v3"
3. Click on the API in the results
4. Click "Enable"

### 3. Create OAuth 2.0 Credentials

1. Go to "APIs & Services" > "Credentials"
2. Click "Create Credentials" and select "OAuth client ID"
3. If prompted, configure the OAuth consent screen:
   - User Type: External
   - App name: "Soccer Camera YouTube Uploader" (or your preferred name)
   - User support email: Your email address
   - Developer contact information: Your email address
   - Add the following scopes:
     - `https://www.googleapis.com/auth/youtube.upload`
     - `https://www.googleapis.com/auth/youtube.readonly`
   - Add your email as a test user
4. Create the OAuth client ID:
   - Application type: Desktop app
   - Name: "Soccer Camera YouTube Uploader" (or your preferred name)
5. Click "Create"
6. **Important**: After creating the client ID, click on it to edit and add the following Authorized redirect URIs:
   - `http://localhost:8080/` (note the trailing slash)
7. Click "Save"
8. Download the JSON file by clicking the download icon
9. Rename the downloaded file to `client_secret.json`

### 4. Configure the Application

1. Create a directory named `youtube` in the shared data directory
2. Place the `client_secret.json` file in this directory
3. Update the `config.ini` file to enable YouTube uploads:
   ```ini
   [YOUTUBE]
   enabled = true
   credentials_file = youtube/client_secret.json
   token_file = youtube/token.json
   ```

### 5. First-time Authentication

When the application attempts to upload a video for the first time, it will:

1. Open a browser window asking you to sign in to your Google account
2. Request permission to upload videos to YouTube on your behalf
3. After granting permission, you'll be redirected to a "localhost" page
4. The application will automatically receive the authorization code and save the token

This authentication process only needs to be completed once. Subsequent uploads will use the saved token.

## Troubleshooting

- **Error: "redirect_uri_mismatch"**: This means the redirect URI used by the application doesn't match any authorized URIs in your Google Cloud Console. Make sure you've added both versions (with and without trailing slash) of the redirect URIs to your OAuth client:
  - `http://localhost:8080/` (with trailing slash)
  - `http://localhost:8080` (without trailing slash)
  - `http://127.0.0.1:8080/` (with trailing slash)
  - `http://127.0.0.1:8080` (without trailing slash)

- **Error: "insufficientPermissions"**: Make sure you've added both required scopes to your OAuth consent screen:
  - `https://www.googleapis.com/auth/youtube.upload`
  - `https://www.googleapis.com/auth/youtube.readonly`
  
  You may need to delete your existing `token.json` file and re-authenticate if you've updated the scopes.

- **Error: "invalid_grant"**: The token may have expired. Delete the `token.json` file and restart the authentication process.
- **Error: "quota exceeded"**: YouTube API has daily quotas. If you exceed them, you'll need to wait until the quota resets.
- **Error: "unauthorized_client"**: Ensure your OAuth consent screen is properly configured and your application has the correct scopes.

For more information, refer to the [YouTube Data API documentation](https://developers.google.com/youtube/v3/docs). 