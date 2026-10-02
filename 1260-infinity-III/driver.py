"""
Agilent 1260 Infinity III edge driver.

Methods marked with ``@command`` are advertised to PUDA. Undecorated methods
are internal. A command signals failure by raising. ``@safety`` is advisory
and does not block dispatch, so the method body enforces the limits.

``@tlm_stream`` polls in a worker thread. OpenLab's .NET SDK is not safe to
call from two threads, so the status stream reads the instrument only when no
command holds the lock and otherwise republishes the last reading.

``@machine_state`` returns cached values. It runs on the event loop.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from puda import command, machine_state, safety, tlm_stream

logger = logging.getLogger(__name__)

_MS_MODES = ("positive", "negative", "positive_negative")


class Driver:
    """Agilent 1260 Infinity III LC controlled through OpenLab CDS by moses-hplc."""

    def __init__(
        self,
        instrument_config_path: str,
        sdk_dir: str,
        automation_core: str,
        automation_instrument: str,
        max_flow_ml_min: float = 5.0,
        max_injection_ul: float = 100.0,
        max_run_time_min: float = 120.0,
    ):
        self._instrument_config_path = instrument_config_path
        self._sdk_dir = sdk_dir
        self._automation_core = automation_core
        self._automation_instrument = automation_instrument
        self._max_flow_ml_min = max_flow_ml_min
        self._max_injection_ul = max_injection_ul
        self._max_run_time_min = max_run_time_min
        self._lock = threading.Lock()
        self._controller = None
        self._instrument_config: dict[str, Any] = {}
        self._software_settings: dict[str, Any] = {}
        self._status: str | None = None
        self._last_sample: str | None = None
        self._connect()

    @machine_state
    def snapshot(self) -> dict:
        """
        Cached connection and last injection, merged into MACHINE_STATE.

        Returns:
            dict: connected, instrument_status, and last_sample.
        """
        return {
            "connected": self._controller is not None,
            "instrument_status": self._status,
            "last_sample": self._last_sample,
        }

    @tlm_stream(interval=10.0, name="status")
    def instrument_status(self) -> dict | None:
        """
        OpenLab instrument state.

        Polled every 10 seconds. ``fresh`` is false when a command is using
        the SDK and this sample is the previous reading.

        Returns:
            dict: status string and fresh flag, or None before the first reading.
        """
        if self._controller is None:
            return None
        if not self._lock.acquire(blocking=False):
            if self._status is None:
                return None
            return {"status": self._status, "fresh": False}
        try:
            status = self._read_status()
        except Exception:
            logger.warning("Status poll failed", exc_info=True)
            return None
        finally:
            self._lock.release()
        return {"status": status, "fresh": True}

    @command
    def read_status(self) -> dict[str, Any]:
        """
        Read the OpenLab instrument state. Does not change the pump or detector.

        Returns:
            dict: status, the OpenLab state string (for example Idle or Running).
        """
        logger.info("Reading instrument status")
        with self._lock:
            return {"status": self._read_status()}

    @command
    @safety(
        summary="Turns the 1260 stack on through OpenLab. Lamps and pump control become active.",
        hazards=["UV exposure", "solvent flow"],
        requires="OpenLab CDS is running and this PC holds the instrument.",
        forbidden_when="Do not turn the stack on while a person is servicing a module.",
        confirm=True,
    )
    def set_on(self) -> dict[str, Any]:
        """
        Turn the instrument on. Moses maps this to OpenLab set-to-on.

        Returns:
            dict: status after the command.
        """
        logger.info("Setting instrument on")
        with self._lock:
            self._require().startup()
            return {"status": self._read_status()}

    @command
    @safety(
        summary="Parks the 1260 at the standby method. Does not power the modules off.",
        hazards=["solvent flow"],
        requires="The standby method in OpenLab is the intended low-flow park.",
        forbidden_when="Do not park the instrument during an injection.",
        confirm=True,
    )
    def standby(self) -> dict[str, Any]:
        """
        Park the instrument in OpenLab standby.

        This is not a power-off. Switch the modules off at the stack.

        Returns:
            dict: status after the command.
        """
        logger.info("Parking instrument in standby")
        with self._lock:
            self._require().standby()
            return {"status": self._read_status(), "powered_off": False}

    @command
    @safety(
        summary="Parks the 1260 in standby and leaves the modules powered.",
        hazards=["solvent flow"],
        requires="The standby method in OpenLab is the intended low-flow park.",
        forbidden_when="Do not shut down during an injection.",
        confirm=True,
    )
    def shutdown(self) -> dict[str, Any]:
        """
        Park the instrument. Moses cannot power off a 1260, so this calls standby.

        Returns:
            dict: status, and powered_off false.
        """
        logger.info("Shutdown parks the instrument; modules stay powered")
        with self._lock:
            controller = self._require()
            controller.standby()
            return {
                "status": self._read_status(),
                "powered_off": False,
                "message": "Parked in standby. Power the 1260 modules off at the instrument.",
            }

    @command
    @safety(
        summary="Builds a gradient method and injects a sample. The pump runs for the full method.",
        hazards=["solvent flow", "high pressure", "sample consumption", "UV exposure"],
        requires="Column, solvents, and waste are ready. The sample position is occupied.",
        forbidden_when="Do not inject while a person is servicing the stack or the column is disconnected.",
        confirm=True,
    )
    def run_gradient(
        self,
        folder: str,
        sample_name: str,
        sample_position: str,
        injection_volume: float,
        gradient: dict,
        ms_mode: str = "",
        exist_ok: bool = False,
    ) -> dict[str, Any]:
        """
        Program a binary gradient from the template method and inject one sample.

        The gradient dict matches ``LCGradient.from_dict`` in moses-hplc.
        Solvent B values are fractions from 0 to 1, not percents. Solvent names
        must match ``instrument_config.eluents``. Channel keys in that map are
        ``A1``, ``A2``, ``B1``, or ``B2``; Moses sends the second character as
        the pump channel.

        Args:
            folder: OpenLab results directory. Moses writes ``<sample_name>.sirslt`` here.
            sample_name: Injection name. Must not contain a path separator.
            sample_position: Autosampler address forwarded to OpenLab. A vialsampler
                uses its vial address. A multisampler uses a drawer address such as
                ``D1F-A1``.
            injection_volume: Injection volume in µL. Must be greater than 0 and
                at most ``MAX_INJECTION_UL`` (default 100).
            gradient: Dict with name, eluents.solvent_a, eluents.solvent_b,
                run_time (minutes), equilibration_time (minutes),
                header_gradient_table, and gradient_table. Flow is mL/min and
                must stay within ``MAX_FLOW_ML_MIN`` (default 5, the G7112B
                binary pump maximum). Run time must stay within
                ``MAX_RUN_TIME_MIN`` (default 120).
            ms_mode: Empty for an LC-only 1260. ``positive``, ``negative``, or
                ``positive_negative`` selects that key under default_methods
                when a mass detector is configured. Default: empty.
            exist_ok: When true, replace an existing result folder for this sample.
                Default: false.

        Returns:
            dict: result folder, OpenLab status, and record metadata when Moses
            can read the run. A 1260 with no mass detector still returns the
            result folder if the chromatogram export cannot be parsed.
        """
        logger.info(
            "Running gradient injection %s at %s, %.3f µL",
            sample_name,
            sample_position,
            injection_volume,
        )
        self._check_injection(sample_name, sample_position, injection_volume, folder)
        parsed = self._parse_gradient(gradient)
        ms_settings = self._ms_settings(ms_mode)
        with self._lock:
            controller = self._require()
            controller.set_acquisition_method(gradient=parsed, ms_settings=ms_settings)
            return self._inject(
                folder=folder,
                sample_name=sample_name,
                sample_position=sample_position,
                injection_volume=injection_volume,
                exist_ok=exist_ok,
            )

    @command
    @safety(
        summary="Injects a sample with an existing OpenLab acquisition method.",
        hazards=["solvent flow", "high pressure", "sample consumption", "UV exposure"],
        requires="The method file is already saved in OpenLab and matches this 1260 stack.",
        forbidden_when="Do not inject while a person is servicing the stack or the column is disconnected.",
        confirm=True,
    )
    def run_method(
        self,
        folder: str,
        sample_name: str,
        sample_position: str,
        injection_volume: float,
        method_path: str,
        run_time_min: float,
        equilibrate: bool = False,
        exist_ok: bool = False,
    ) -> dict[str, Any]:
        """
        Inject one sample using an acquisition method that already exists on disk.

        Moses waits ``run_time_min`` and then until OpenLab reports Idle. It does
        not edit the method. When equilibrate is true, OpenLab uses the configured
        ``.smx`` pretreatment file as it stands.

        Args:
            folder: OpenLab results directory.
            sample_name: Injection name. Must not contain a path separator.
            sample_position: Autosampler address forwarded to OpenLab.
            injection_volume: Injection volume in µL, greater than 0 and at most
                ``MAX_INJECTION_UL`` (default 100).
            method_path: Absolute path to the ``.amx`` acquisition method.
            run_time_min: How long Moses waits for the acquisition, in minutes.
                Greater than 0 and at most ``MAX_RUN_TIME_MIN`` (default 120).
            equilibrate: Use the configured ``.smx`` pretreatment. Default: false.
            exist_ok: Replace an existing result folder for this sample. Default: false.

        Returns:
            dict: result folder, OpenLab status, and record metadata when readable.
        """
        logger.info(
            "Running method %s for %s at %s, %.3f µL",
            method_path,
            sample_name,
            sample_position,
            injection_volume,
        )
        self._check_injection(sample_name, sample_position, injection_volume, folder)
        self._check_run_time(run_time_min)
        if not str(method_path).lower().endswith(".amx"):
            raise ValueError("method_path must be an OpenLab .amx acquisition method")
        with self._lock:
            controller = self._require()
            controller.set_acquisition_method(instrument_method_path=method_path)
            controller.tot_run_time = run_time_min
            controller.equilibration = bool(equilibrate)
            return self._inject(
                folder=folder,
                sample_name=sample_name,
                sample_position=sample_position,
                injection_volume=injection_volume,
                exist_ok=exist_ok,
            )

    def _connect(self) -> None:
        import moses

        moses.set_config(
            instrument="agilent",
            sdk_dir=self._sdk_dir,
            automation_core=self._automation_core,
            automation_instrument=self._automation_instrument,
        )
        from moses.agilent import AgilentController

        config = _load_instrument_config(self._instrument_config_path)
        self._instrument_config = config["instrument_config"]
        self._software_settings = config["software_settings"]
        self._controller = AgilentController(config)
        self._status = str(self._controller.status)
        logger.info("Connected to 1260 Infinity III, status=%s", self._status)

    def _require(self):
        if self._controller is None:
            raise RuntimeError("Instrument is not connected")
        return self._controller

    def _read_status(self) -> str:
        status = str(self._require().status)
        self._status = status
        return status

    def _inject(
        self,
        folder: str,
        sample_name: str,
        sample_position: str,
        injection_volume: float,
        exist_ok: bool,
    ) -> dict[str, Any]:
        controller = self._require()
        if exist_ok:
            result = Path(folder) / f"{sample_name}.sirslt"
            if result.exists():
                import shutil

                shutil.rmtree(result)
        controller.system.start_run(
            folder,
            sample_name,
            sample_position,
            injection_volume,
            controller.acquisition_method,
            controller.tot_run_time,
            equilibration=controller.equilibration,
        )
        result_folder = str(Path(folder) / f"{sample_name}.sirslt")
        self._last_sample = sample_name
        payload: dict[str, Any] = {
            "sample_name": sample_name,
            "sample_position": sample_position,
            "injection_volume_ul": injection_volume,
            "result_folder": result_folder,
            "status": self._read_status(),
        }
        try:
            record = controller.load_run(folder, sample_name)
            saved = Path(folder) / f"{sample_name}.npz"
            record.save(saved)
            payload["record_file"] = str(saved)
            payload["metadata"] = _record_summary(record)
        except Exception as exc:
            logger.warning("Injection finished but the result record could not be read: %s", exc)
            payload["record_error"] = str(exc)
        return payload

    def _check_injection(
        self,
        sample_name: str,
        sample_position: str,
        injection_volume: float,
        folder: str,
    ) -> None:
        if not sample_name or any(sep in sample_name for sep in ("/", "\\", ":")):
            raise ValueError("sample_name must be a single path segment")
        if not str(sample_position).strip():
            raise ValueError("sample_position is required")
        if not str(folder).strip():
            raise ValueError("folder is required")
        if injection_volume <= 0 or injection_volume > self._max_injection_ul:
            raise ValueError(
                f"injection_volume must be within (0, {self._max_injection_ul}] µL"
            )

    def _check_run_time(self, run_time_min: float) -> None:
        if run_time_min <= 0 or run_time_min > self._max_run_time_min:
            raise ValueError(
                f"run time must be within (0, {self._max_run_time_min}] minutes"
            )

    def _ms_settings(self, ms_mode: str) -> dict[str, str] | None:
        if not ms_mode or ms_mode == "lc":
            return None
        if ms_mode not in _MS_MODES:
            raise ValueError(
                "ms_mode must be empty, 'lc', 'positive', 'negative', or 'positive_negative'"
            )
        methods = self._instrument_config_methods()
        if ms_mode not in methods:
            raise ValueError(f"default_methods has no '{ms_mode}' template")
        return {"mode": ms_mode}

    def _instrument_config_methods(self) -> dict[str, Any]:
        return self._software_settings.get("default_methods", {})

    def _parse_gradient(self, gradient: dict):
        from moses.datatypes import LCGradient

        self._check_gradient_limits(gradient)
        return LCGradient.from_dict(gradient)

    def _check_gradient_limits(self, gradient: dict) -> None:
        if not isinstance(gradient, dict):
            raise ValueError("gradient must be an object")
        run_time = gradient.get("run_time")
        if not isinstance(run_time, (int, float)):
            raise ValueError("gradient.run_time is required, in minutes")
        self._check_run_time(float(run_time))
        equilibration = gradient.get("equilibration_time", 0)
        if not isinstance(equilibration, (int, float)) or equilibration < 0 or equilibration > 60:
            raise ValueError("gradient.equilibration_time must be within [0, 60] minutes")
        eluents = gradient.get("eluents") or {}
        configured = set(self._instrument_config.get("eluents", {}).values())
        for key in ("solvent_a", "solvent_b"):
            name = eluents.get(key)
            if name not in configured:
                raise ValueError(f"gradient.eluents.{key} must match a configured eluent name")
        header = gradient.get("header_gradient_table")
        table = gradient.get("gradient_table")
        if not isinstance(header, list) or not isinstance(table, list) or not table:
            raise ValueError("gradient_table must contain at least one row")
        try:
            flow_index = header.index("flow_rate")
            solvent_b_index = header.index(eluents["solvent_b"])
        except ValueError as exc:
            raise ValueError(
                "header_gradient_table must include flow_rate and the solvent B name"
            ) from exc
        for row in table:
            flow = float(row[flow_index])
            if flow <= 0 or flow > self._max_flow_ml_min:
                raise ValueError(
                    f"flow_rate must be within (0, {self._max_flow_ml_min}] mL/min"
                )
            fraction_b = float(row[solvent_b_index])
            if fraction_b < 0 or fraction_b > 1:
                raise ValueError("solvent B must be a fraction from 0 to 1, not a percent")


def _load_instrument_config(path: str) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        config = json.load(handle)
    for key in ("instrument_config", "software_settings"):
        if key not in config:
            raise ValueError(f"Instrument config missing {key}")
    return config


def _record_summary(record: Any) -> dict[str, Any]:
    metadata = record.metadata
    gradient = None
    if metadata.gradient is not None:
        gradient = _jsonable(metadata.gradient.to_dict())
    return {
        "sample_id": record.sample_id,
        "injection_volume_ul": _jsonable(metadata.injection_volume),
        "sample_position": metadata.injection_source,
        "time_stamp": None if metadata.time_stamp is None else str(metadata.time_stamp),
        "gradient": gradient,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "item") and callable(value.item):
        try:
            return value.item()
        except ValueError:
            pass
    return value
