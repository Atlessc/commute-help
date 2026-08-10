import { ApiError, type ClosureComparisonInput, type SelectedLocation } from './routing'

export type PhysicalSimulationSettings = {
  warmupMinutes: number
  analysisMinutes: number
  realVehiclesPerSimulatedVehicle: number
  seed: number
}

export const DEFAULT_PHYSICAL_SIMULATION_SETTINGS: PhysicalSimulationSettings = {
  warmupMinutes: 45,
  analysisMinutes: 60,
  realVehiclesPerSimulatedVehicle: 1,
  seed: 842901,
}

export type PhysicalTripInfo = {
  vehicle_id: string
  depart: number
  arrival: number
  duration: number
  route_length: number
  waiting_time: number
}

export type PhysicalEdgeChange = {
  sumo_edge_id: string
  road_name: string
  baseline_mean_active_vehicles: number
  scenario_mean_active_vehicles: number
  change_mean_active_vehicles: number
  baseline_mean_speed_mps: number | null
  scenario_mean_speed_mps: number | null
  geometry: { type: 'LineString'; coordinates: [number, number][] }
}

export type PhysicalRunMetrics = {
  selected_trip: PhysicalTripInfo | null
  selected_trip_rerouted: boolean
  departed_vehicle_count: number
  arrived_vehicle_count: number
  teleport_count: number
}

export type RegionalSimulationSummary = {
  status: 'completed'
  evidence_level: 'modeled_uncalibrated'
  seed: number
  demand_version: string
  demand_model_version: string
  real_vehicles_per_simulated_vehicle: number
  represented_real_vehicle_trips: number
  baseline: PhysicalRunMetrics
  scenario: PhysicalRunMetrics
  comparison: {
    selected_trip_delta_seconds: number | null
    teleport_delta: number
    arrived_vehicle_delta: number
    edge_changes: PhysicalEdgeChange[]
  }
  free_flow_validation: {
    network_free_flow_seconds: number
    minimum_allowed_seconds: number
    baseline_valid: boolean
    scenario_valid: boolean
  }
  playback_available: boolean
  assumptions: string[]
}

export type SimulationRunStatus = {
  id: string
  run_kind: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancel_requested' | 'cancelled'
  seed: number
  progress: number
  current_sim_second: number | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  summary: RegionalSimulationSummary | null
  error_message: string | null
}

export type PhysicalPlaybackFrame = {
  elapsed_seconds: number
  agents: Array<{
    id: string
    coordinate: [number, number]
    congestion: 'free' | 'slow' | 'heavy'
    opacity: number
  }>
  trip_coordinate: [number, number] | null
  traveled_route: [number, number][]
  projected_route: [number, number][]
  selected_route_edges: string[]
}

export type PhysicalSimulationPlayback = {
  schema_version: number
  duration_seconds: number
  frame_interval_seconds: number
  seed: number
  real_vehicles_per_simulated_vehicle: number
  displayed_vehicle_limit: number
  frames: PhysicalPlaybackFrame[]
}

export async function startPhysicalSimulation(
  origin: SelectedLocation,
  destination: SelectedLocation,
  closure: ClosureComparisonInput,
  settings: PhysicalSimulationSettings,
): Promise<{ id: string; status: 'queued' | 'running' }> {
  return request('/api/simulation/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      run_kind: 'regional_comparison',
      seed: settings.seed,
      departure_time: closure.departureTime,
      origin_app_edge_id: origin.edge.edge_id,
      destination_app_edge_id: destination.edge.edge_id,
      origin_node_id: origin.node_id,
      destination_node_id: destination.node_id,
      warmup_minutes: settings.warmupMinutes,
      analysis_minutes: settings.analysisMinutes,
      real_vehicles_per_simulated_vehicle: settings.realVehiclesPerSimulatedVehicle,
      closures: closure.sections.map(({ directions, restriction, schedule }) => ({
        app_edge_ids: directions.map(({ edge }) => edge.edge_id),
        restriction_type: restriction.type,
        ...(restriction.type === 'lane' ? { remaining_lanes: restriction.remaining_lanes } : {}),
        ...(restriction.type === 'speed' ? { speed_limit_kph: restriction.speed_limit_kph } : {}),
        ...(schedule.type === 'scheduled'
          ? { starts_at: new Date(schedule.starts_at).toISOString(), ends_at: new Date(schedule.ends_at).toISOString() }
          : {}),
      })),
    }),
  })
}

export function getPhysicalSimulation(id: string, signal?: AbortSignal): Promise<SimulationRunStatus> {
  return request(`/api/simulation/runs/${id}`, { signal })
}

export function getPhysicalPlayback(id: string, signal?: AbortSignal): Promise<PhysicalSimulationPlayback> {
  return request(`/api/simulation/runs/${id}/playback`, { signal })
}

export function cancelPhysicalSimulation(id: string): Promise<SimulationRunStatus> {
  return request(`/api/simulation/runs/${id}/cancel`, { method: 'POST' })
}

async function request<Response>(url: string, init: RequestInit): Promise<Response> {
  const response = await fetch(url, init)
  if (response.ok) return response.json() as Promise<Response>
  const payload = (await response.json().catch(() => null)) as {
    detail?: string | { code?: string; message?: string }
  } | null
  const detail = payload?.detail
  throw new ApiError(
    typeof detail === 'object' ? detail.code ?? 'simulation_failed' : 'simulation_failed',
    typeof detail === 'string' ? detail : detail?.message ?? 'The physical simulation request failed.',
  )
}
