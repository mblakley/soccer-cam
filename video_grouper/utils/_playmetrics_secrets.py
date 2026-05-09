# Tracked in git despite the filename. The Firebase Web API key is not a
# secret per Firebase's security model
# (https://firebase.google.com/docs/projects/api-keys):
#
#   "API keys for Firebase services are not used to control access to
#    backend resources... They are normally not considered to be secrets."
#
# The key only identifies the Firebase project to the auth endpoint;
# real authorization happens via the user's own PlayMetrics login
# (username/password from [PLAYMETRICS] in config.ini) which produces
# a per-user ID token. Treating it as a secret would just force every
# build to thread it through env vars without any security benefit.
PLAYMETRICS_FIREBASE_WEB_API_KEY = "AIzaSyBEB_rFRGuLJja2vzeDCa7J1NZp0E7RN4U"
