"""
loguru 기반 로거
- 파일 로테이션: 10MB, 30일 보관
- logs/ 디렉토리에 일별 로그 파일 생성
"""

import sys
from pathlib import Path
from loguru import logger

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def setup_logger(level: str = "INFO") -> None:
    logger.remove()

    # 콘솔 출력
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )

    # 파일 출력 (일별, 10MB 로테이션, 30일 보관)
    logger.add(
        LOG_DIR / "{time:YYYY-MM-DD}.log",
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} - {message}",
        rotation="10 MB",
        retention="30 days",
        encoding="utf-8",
    )


setup_logger()

__all__ = ["logger"]
