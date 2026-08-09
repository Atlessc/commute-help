"""Discover and report the local SUMO runtime without initializing a simulation."""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from functools import cached_property
from pathlib import Path
from typing import Literal

from backend.app.core.settings import Settings
from backend.app.schemas.simulation import SimulationCapabilities


class SumoEnvironmentService:
    """Own bounded SUMO capability checks for API, setup, and doctor commands."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def capabilities(self) -> SimulationCapabilities:
        """Return current local capabilities without exposing filesystem paths."""

        return self._detected_capabilities.model_copy(deep=True)

    @cached_property
    def _detected_capabilities(self) -> SimulationCapabilities:
        """Perform bounded discovery once for this application process."""

        sumo = _resolve_binary(self._settings.sumo_binary, "sumo")
        netconvert = _resolve_binary(self._settings.netconvert_binary, "netconvert")
        sumolib_available = _module_imports("sumolib")
        libsumo_available = _module_imports("libsumo")
        version = _binary_version(sumo) if sumo else None
        runtime_mode = self._runtime_mode(sumo is not None, libsumo_available)
        warnings: list[str] = []

        if runtime_mode is None:
            warnings.append("No usable SUMO runtime was found; run `npm run setup`.")
        if netconvert is None:
            warnings.append("netconvert is unavailable; SUMO networks cannot be built yet.")
        if not self._settings.sumo_network_manifest_path.is_file():
            warnings.append("No active SUMO network manifest exists yet.")
        if not self._settings.sumo_active_model_manifest_path.is_file():
            warnings.append("No frozen SUMO model bundle exists yet.")

        return SimulationCapabilities(
            available=runtime_mode is not None,
            sumo_version=version,
            runtime_mode=runtime_mode,
            sumo_available=sumo is not None,
            netconvert_available=netconvert is not None,
            sumolib_available=sumolib_available,
            libsumo_available=libsumo_available,
            network_ready=self._settings.sumo_network_manifest_path.is_file(),
            model_ready=self._settings.sumo_active_model_manifest_path.is_file(),
            max_parallel_runs=max(1, self._settings.sumo_max_parallel_runs),
            offline_only=self._settings.sumo_offline_only,
            warnings=warnings,
        )

    def _runtime_mode(
        self,
        sumo_available: bool,
        libsumo_available: bool,
    ) -> Literal["libsumo", "subprocess"] | None:
        requested = self._settings.sumo_runtime_mode
        if requested == "libsumo":
            return "libsumo" if libsumo_available else None
        if requested == "subprocess":
            return "subprocess" if sumo_available else None
        if libsumo_available:
            return "libsumo"
        return "subprocess" if sumo_available else None


def _resolve_binary(configured: Path | None, command: str) -> str | None:
    if configured is not None:
        candidate = configured.expanduser().resolve()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    discovered = shutil.which(command)
    if discovered is not None:
        return discovered
    virtual_environment_candidate = Path(sys.executable).parent / command
    if virtual_environment_candidate.is_file() and os.access(
        virtual_environment_candidate,
        os.X_OK,
    ):
        return str(virtual_environment_candidate)
    return None


def _module_imports(module_name: str) -> bool:
    if importlib.util.find_spec(module_name) is None:
        return False
    try:
        result = subprocess.run(
            [sys.executable, "-c", f"import {module_name}"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _binary_version(binary: str) -> str | None:
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = f"{result.stdout}\n{result.stderr}"
    match = re.search(
        r"\bEclipse\s+SUMO\s+sumo\s+([0-9]+(?:\.[0-9]+){1,2})\b",
        output,
        flags=re.IGNORECASE,
    )
    if match is None:
        match = re.search(
            r"^\s*sumo\s+([0-9]+(?:\.[0-9]+){1,2})\b",
            output,
            flags=re.IGNORECASE | re.MULTILINE,
        )
    if match is None:
        match = re.search(r"\bv([0-9]+(?:\.[0-9]+){1,2})\b", output)
    return match.group(1) if match else None
