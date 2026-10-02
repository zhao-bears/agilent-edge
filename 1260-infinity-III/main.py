"""
Main entry point for the 1260 Infinity III edge service.

Connects the OpenLab-backed driver to NATS and leaves heartbeat, host health,
telemetry streams, and machine state to EdgeRunner.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from puda import EdgeNatsClient, EdgeRunner

from driver import Driver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)


class Config(BaseSettings):
    machine_id: str
    nats_servers: str
    agilent_sdk_dir: str
    agilent_automation_core: str
    agilent_automation_instrument: str
    instrument_config_path: str
    max_flow_ml_min: float = 5.0
    max_injection_ul: float = 100.0
    max_run_time_min: float = 120.0

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def nats_server_list(self) -> list[str]:
        return [s.strip() for s in self.nats_servers.split(",") if s.strip()]


def load_config() -> Config:
    """Load and validate configuration; exit process on failure."""
    try:
        return Config()
    except Exception as exc:
        logger.error("Failed to load configuration: %s", exc, exc_info=True)
        sys.exit(1)


async def main():
    """Connect to OpenLab through Moses, then serve commands on NATS."""
    config = load_config()
    logger.info("Config loaded for %s", config.machine_id)

    driver = Driver(
        instrument_config_path=config.instrument_config_path,
        sdk_dir=config.agilent_sdk_dir,
        automation_core=config.agilent_automation_core,
        automation_instrument=config.agilent_automation_instrument,
        max_flow_ml_min=config.max_flow_ml_min,
        max_injection_ul=config.max_injection_ul,
        max_run_time_min=config.max_run_time_min,
    )
    logger.info("Machine driver initialized")

    edge_nats_client = EdgeNatsClient(
        servers=config.nats_server_list,
        machine_id=config.machine_id,
    )
    runner = EdgeRunner(nats_client=edge_nats_client, machine_driver=driver)
    await runner.connect()
    logger.info(
        "==================== %s Edge Service Ready ====================",
        config.machine_id,
    )
    await runner.run()


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.warning("Gracefully stopping...")
            sys.exit(0)
        except Exception as exc:
            logger.error("Fatal error: %s", exc, exc_info=True)
            time.sleep(5)
