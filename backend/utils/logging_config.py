"""
Logging configuration utilities.
"""
import logging
import os
import sys
from pathlib import Path


def setup_logging(log_dir: Path = Path("/app/logs")) -> None:
    """Configure logging for the application.

    Level is controlled by the LOG_LEVEL environment variable (default: INFO).
    Logs are written to both stdout and a file in log_dir so they can be
    accessed on the host by mounting that directory in docker-compose.
    """
    log_level = getattr(logging, os.environ.get('LOG_LEVEL', 'INFO').upper(), logging.INFO)
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    file_handler = logging.FileHandler(log_dir / 'media-cleanup.log')
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Suppress noisy third-party connection pool logs
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
