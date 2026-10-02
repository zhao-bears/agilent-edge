"""Connect to the 1260 through OpenLab and print its status.

Does not start a run or change the pump. Run this on the OpenLab PC after
.env and the instrument JSON are filled in, with Acquisition already online:

    python tests/manual_status.py
"""

import json
import logging
import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from driver import Driver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)


class Config(BaseSettings):
    agilent_sdk_dir: str
    agilent_automation_core: str
    agilent_automation_instrument: str
    instrument_config_path: str

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


def main() -> None:
    config = Config()
    driver = Driver(
        instrument_config_path=config.instrument_config_path,
        sdk_dir=config.agilent_sdk_dir,
        automation_core=config.agilent_automation_core,
        automation_instrument=config.agilent_automation_instrument,
    )
    status = driver.read_status()
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.error("Status check failed: %s", exc, exc_info=True)
        sys.exit(1)
