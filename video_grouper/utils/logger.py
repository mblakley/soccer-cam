import logging
import logging.handlers
from pathlib import Path
from datetime import datetime
from typing import Optional
from .paths import get_shared_data_path
from video_grouper.utils.paths import resolve_path


def setup_logging(
    level: str = "INFO",
    log_dir: Optional[Path] = None,
    log_file: Optional[Path] = None,
    app_name: str = "video_grouper",
    backup_count: int = 30
) -> None:
    """
    Set up logging configuration for the application.
    
    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_dir: Directory to store log files (defaults to shared_data/logs)
        log_file: File to store log entries (defaults to log_dir/app_name.log)
        app_name: Application name for log file naming
        backup_count: Number of backup log files to keep
    """
    # Create log directory if not provided
    if log_dir is None:
        log_dir = get_shared_data_path() / "logs"
    
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # Create daily rotating file handler
    if log_file is None:
        log_file = log_dir / f"{app_name}.log"
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=log_file,
        when='midnight',
        interval=1,
        backupCount=backup_count,
        encoding='utf-8'
    )
    
    # Create console handler
    console_handler = logging.StreamHandler()
    
    # Create formatter
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # Set formatter for both handlers
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    # Get the root logger and configure it
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper()))
    
    # Remove any existing handlers to avoid duplicates
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Add our handlers
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    
    # Set specific loggers to WARNING to reduce noise
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger with the specified name.
    
    Args:
        name: Logger name (usually __name__)
        
    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)


def close_loggers() -> None:
    """
    Close all loggers and release file handles.
    This is important for cleanup, especially in tests.
    """
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        handler.close()
        root_logger.removeHandler(handler)
    
    # Also close handlers on child loggers
    for logger_name in logging.root.manager.loggerDict:
        logger = logging.getLogger(logger_name)
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


def setup_logging_from_config(config) -> None:
    """
    Set up logging configuration from a Config object.
    
    Args:
        config: Configuration object with logging settings
    """
    log_dir = None
    log_file = None
    storage_path = getattr(config.storage, 'path', None) if hasattr(config, 'storage') else None
    if hasattr(config, 'logging'):
        if hasattr(config.logging, 'log_dir') and storage_path:
            log_dir = resolve_path(config.logging.log_dir, storage_path)
        if hasattr(config.logging, 'log_file') and storage_path:
            log_file = resolve_path(config.logging.log_file, storage_path)
    app_name = getattr(config.logging, 'app_name', 'video_grouper') if hasattr(config, 'logging') else 'video_grouper'
    backup_count = getattr(config.logging, 'backup_count', 30) if hasattr(config, 'logging') else 30
    level = getattr(config.logging, 'level', 'INFO') if hasattr(config, 'logging') else 'INFO'
    setup_logging(
        level=level,
        log_dir=log_dir,
        log_file=log_file,
        app_name=app_name,
        backup_count=backup_count
    )


# Create the main application logger
logger = get_logger("video_grouper")
