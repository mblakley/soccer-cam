"""Pipeline configuration — single source of truth for all paths and settings.

Loads config.toml from the pipeline package directory. Worker config can
override settings via a local worker_config.toml.

Usage:
    from training.pipeline.config import load_config
    cfg = load_config()
    print(cfg.paths.registry_db)
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_CONFIG_DIR = Path(__file__).parent


@dataclass(frozen=True)
class ArchivePaths:
    root: str = "F:/"
    video_sources: str = "F:/training_data"
    checkpoints: str = "F:/training_checkpoints"
    backups: str = "F:/training_backups"


@dataclass(frozen=True)
class Paths:
    registry_db: str = "D:/training_data/registry.db"
    work_queue_db: str = "D:/training_data/work_queue.db"
    games_dir: str = "D:/training_data/games"
    staging: str = "D:/training_data/staging"
    training_sets: str = "D:/training_data/training_sets"
    game_registry: str = "D:/training_data/game_registry.json"
    log_file: str = "D:/training_data/pipeline.log"
    server_work_dir: str = "G:/pipeline_work"
    archive: ArchivePaths = field(default_factory=ArchivePaths)


@dataclass(frozen=True)
class ServerConfig:
    hostname: str = "DESKTOP-5L867J8"
    ip: str = "192.168.86.152"
    share_training: str = "\\\\192.168.86.152\\training"
    share_video: str = "\\\\192.168.86.152\\video"


@dataclass(frozen=True)
class OrchestratorConfig:
    check_interval: int = 60
    stale_heartbeat: int = 7200
    max_staging_concurrent: int = 1
    min_new_games_for_retrain: int = 2
    min_new_labels_for_retrain: int = 5000


@dataclass(frozen=True)
class MachineConfig:
    hostname: str = ""
    gpu: str = ""
    capabilities: list[str] = field(default_factory=list)
    deploy_dir: str = "C:/soccer-cam-label"


@dataclass(frozen=True)
class TilingConfig:
    frame_interval: int = 4
    diff_threshold: float = 2.0
    tile_cols: int = 7
    tile_rows: int = 3
    tile_size: int = 640


@dataclass(frozen=True)
class LabelingConfig:
    onnx_model: str = "model.onnx"
    confidence: float = 0.45
    nms_iou: float = 0.5


@dataclass(frozen=True)
class TrainingConfig:
    model_base: str = "yolo26l.pt"
    epochs: int = 50
    batch_size: int = 16
    imgsz: int = 640
    neg_ratio: float = 1.0
    patience: int = 30


@dataclass(frozen=True)
class QAConfig:
    sonnet_batch_limit: int = 100
    sonnet_batch_size: int = 20


@dataclass(frozen=True)
class NtfyConfig:
    topic: str = "video_grouper_mblakley43431"
    enabled: bool = True


@dataclass(frozen=True)
class WorkerConfig:
    """Worker-specific config, loaded from worker_config.toml on remote machines."""

    hostname: str = ""
    capabilities: list[str] = field(default_factory=list)
    server_share: str = "\\\\192.168.86.152\\training"
    local_work_dir: str = "C:/soccer-cam-label/work"
    local_models_dir: str = "C:/soccer-cam-label/models"
    max_gpu_temp: int = 85
    min_disk_free_gb: int = 20
    gpu_device: int = 0
    idle_games: list[str] = field(
        default_factory=lambda: [
            "FortniteClient-Win64-Shipping",
            "RobloxPlayerBeta",
            "RocketLeague",
        ]
    )
    heartbeat_interval: int = 30


@dataclass(frozen=True)
class PipelineConfig:
    paths: Paths = field(default_factory=Paths)
    server: ServerConfig = field(default_factory=ServerConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    machines: dict[str, MachineConfig] = field(default_factory=dict)
    tiling: TilingConfig = field(default_factory=TilingConfig)
    labeling: LabelingConfig = field(default_factory=LabelingConfig)
    training: TrainingConfig = field(default_factory=TrainingConfig)
    qa: QAConfig = field(default_factory=QAConfig)
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)


def _build_dataclass(cls, data: dict):
    """Recursively build a dataclass from a dict, ignoring unknown keys."""
    import dataclasses

    if not dataclasses.is_dataclass(cls):
        return data

    field_names = {f.name for f in dataclasses.fields(cls)}
    filtered = {}
    for key, val in data.items():
        if key not in field_names:
            continue
        f = next(f for f in dataclasses.fields(cls) if f.name == key)
        if dataclasses.is_dataclass(f.type):
            filtered[key] = _build_dataclass(f.type, val)
        else:
            filtered[key] = val
    return cls(**filtered)


def _merge_dicts(base: dict, override: dict) -> dict:
    """Deep-merge override into base."""
    merged = base.copy()
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _merge_dicts(merged[key], val)
        else:
            merged[key] = val
    return merged


def load_config(
    config_path: Path | None = None,
    worker_config_path: Path | None = None,
) -> PipelineConfig:
    """Load pipeline config from TOML files.

    Args:
        config_path: Path to main config.toml. Defaults to the one in this package.
        worker_config_path: Optional worker_config.toml to overlay worker settings.
    """
    if config_path is None:
        config_path = _CONFIG_DIR / "config.toml"

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    # Worker config overlay
    if worker_config_path and worker_config_path.exists():
        with open(worker_config_path, "rb") as f:
            worker_raw = tomllib.load(f)
        raw = _merge_dicts(raw, worker_raw)

    # Also check PIPELINE_CONFIG env var
    env_config = os.environ.get("PIPELINE_CONFIG")
    if env_config:
        env_path = Path(env_config)
        if env_path.exists():
            with open(env_path, "rb") as f:
                raw = _merge_dicts(raw, tomllib.load(f))

    # Build paths with nested archive
    paths_data = raw.get("paths", {})
    archive_data = paths_data.pop("archive", {})
    archive = _build_dataclass(ArchivePaths, archive_data)
    paths = Paths(**{k: v for k, v in paths_data.items() if k != "archive"}, archive=archive)

    # Build machines dict
    machines_raw = raw.get("machines", {})
    machines = {}
    for name, mdata in machines_raw.items():
        machines[name] = _build_dataclass(MachineConfig, mdata)

    # Build worker config
    worker = _build_dataclass(WorkerConfig, raw.get("worker", {}))

    return PipelineConfig(
        paths=paths,
        server=_build_dataclass(ServerConfig, raw.get("server", {})),
        orchestrator=_build_dataclass(OrchestratorConfig, raw.get("orchestrator", {})),
        machines=machines,
        tiling=_build_dataclass(TilingConfig, raw.get("tiling", {})),
        labeling=_build_dataclass(LabelingConfig, raw.get("labeling", {})),
        training=_build_dataclass(TrainingConfig, raw.get("training", {})),
        qa=_build_dataclass(QAConfig, raw.get("qa", {})),
        ntfy=_build_dataclass(NtfyConfig, raw.get("ntfy", {})),
        worker=worker,
    )
