"""
JARVIS - Main Application Module.

Entry point for the application.
"""
import logging

logger = logging.getLogger(__name__)


def main() -> None:
    """Application entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting JARVIS...")


if __name__ == "__main__":
    main()
