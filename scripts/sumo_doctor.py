"""Read-only readiness report for the local SUMO subsystem."""

from backend.app.core.settings import get_settings
from backend.app.services.sumo.environment import SumoEnvironmentService


def main() -> None:
    capabilities = SumoEnvironmentService(get_settings()).capabilities()
    print("\nCommute Help SUMO doctor\n")
    print(f"[{'OK' if capabilities.available else 'FAIL'}] Runtime: "
          f"{capabilities.runtime_mode or 'unavailable'}")
    print(f"[{'OK' if capabilities.sumo_version else 'WARN'}] Version: "
          f"{capabilities.sumo_version or 'unknown'}")
    print(f"[{'OK' if capabilities.netconvert_available else 'FAIL'}] netconvert: "
          f"{'available' if capabilities.netconvert_available else 'unavailable'}")
    print(f"[{'OK' if capabilities.sumolib_available else 'WARN'}] sumolib: "
          f"{'available' if capabilities.sumolib_available else 'unavailable'}")
    print(f"[{'OK' if capabilities.libsumo_available else 'WARN'}] libsumo: "
          f"{'available' if capabilities.libsumo_available else 'using subprocess fallback'}")
    print(f"[{'OK' if capabilities.network_ready else 'WARN'}] Network: "
          f"{'ready' if capabilities.network_ready else 'not built yet'}")
    print(f"[{'OK' if capabilities.schedule_ready else 'WARN'}] 24/7 schedule: "
          f"{capabilities.schedule_version or 'not active yet'}")
    print(f"[{'OK' if capabilities.proxy_demand_ready else 'WARN'}] Local 24/7 proxy demand: "
          f"{capabilities.proxy_demand_model_version or 'not ready yet'}")
    print(f"[{'OK' if capabilities.model_ready else 'WARN'}] Model bundle: "
          f"{'ready' if capabilities.model_ready else 'not frozen yet'}")
    for warning in capabilities.warnings:
        print(f"[WARN] {warning}")
    if not capabilities.available or not capabilities.netconvert_available:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
