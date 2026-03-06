/// Pipeline states for video group processing.
///
/// State machine transitions:
///   pending -> downloading -> downloaded -> combining -> combined
///   -> trimming -> trimmed -> uploading -> complete
///
/// Any state can transition to [error].
enum PipelineState {
  pending('Pending'),
  downloading('Downloading'),
  downloaded('Downloaded'),
  combining('Combining'),
  combined('Combined'),
  trimming('Trimming'),
  trimmed('Trimmed'),
  uploading('Uploading'),
  complete('Complete'),
  error('Error');

  const PipelineState(this.displayName);
  final String displayName;

  /// Returns true if this state allows forward progression.
  bool get isActive =>
      this != PipelineState.complete && this != PipelineState.error;

  /// Returns true if this state represents a processing stage (not idle).
  bool get isProcessing => switch (this) {
        PipelineState.downloading ||
        PipelineState.combining ||
        PipelineState.trimming ||
        PipelineState.uploading =>
          true,
        _ => false,
      };

  /// Returns the next state in the pipeline, or null if complete/error.
  PipelineState? get next => switch (this) {
        PipelineState.pending => PipelineState.downloading,
        PipelineState.downloading => PipelineState.downloaded,
        PipelineState.downloaded => PipelineState.combining,
        PipelineState.combining => PipelineState.combined,
        PipelineState.combined => PipelineState.trimming,
        PipelineState.trimming => PipelineState.trimmed,
        PipelineState.trimmed => PipelineState.uploading,
        PipelineState.uploading => PipelineState.complete,
        PipelineState.complete => null,
        PipelineState.error => null,
      };

  /// Returns true if transitioning to [target] is valid.
  bool canTransitionTo(PipelineState target) {
    if (target == PipelineState.error) return true;
    if (target == PipelineState.pending && this == PipelineState.error) {
      return true; // allow retry from error
    }
    return next == target;
  }

  /// Fractional progress through the pipeline (0.0 to 1.0).
  double get overallProgress => switch (this) {
        PipelineState.pending => 0.0,
        PipelineState.downloading => 0.125,
        PipelineState.downloaded => 0.25,
        PipelineState.combining => 0.375,
        PipelineState.combined => 0.5,
        PipelineState.trimming => 0.625,
        PipelineState.trimmed => 0.75,
        PipelineState.uploading => 0.875,
        PipelineState.complete => 1.0,
        PipelineState.error => 0.0,
      };
}
