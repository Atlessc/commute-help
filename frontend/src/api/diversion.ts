import {
  ApiError,
  buildRouteRequestBody,
  type ClosureComparisonInput,
  type SelectedLocation,
} from './routing'
import type { RouteSummary } from './routing'

export type DiversionSettings = {
  demandVph: number
  demandPairCount: number
  iterations: number
  dispersionRadiusM: number
  trafficProfileId: string | null
}

export const DEFAULT_DIVERSION_SETTINGS: DiversionSettings = {
  demandVph: 600,
  demandPairCount: 10,
  iterations: 3,
  dispersionRadiusM: 1000,
  trafficProfileId: null,
}

export type DiversionEdgeChange = {
  edge_id: string
  u: string
  v: string
  key: number
  road_name: string
  road_class: string
  baseline_vph: number
  scenario_vph: number
  change_vph: number
  change_percent: number | null
  volume_capacity_ratio: number
  geometry: {
    type: 'LineString'
    coordinates: [number, number][]
  }
}

export type DiversionPlaybackEdge = {
  edge_id: string
  baseline_vph: number
  scenario_vph: number
  volume_capacity_ratio: number
  geometry: {
    type: 'LineString'
    coordinates: [number, number][]
  }
}

export type DiversionResult = {
  evidence_level: 'modeled_uncalibrated'
  graph_version: string
  model_version: string
  input_hash: string
  demand_vph: number
  demand_pair_count: number
  iterations: number
  dispersion_radius_m: number
  traffic_profile_id: string | null
  background_source: string
  background_bucket: string
  background_observation_count: number
  background_matched_edge_count: number
  background_network_coverage_percent: number
  displaced_background_vph: number
  assigned_demand_vph: number
  unassigned_demand_vph: number
  changed_edge_count: number
  max_increase_vph: number
  max_decrease_vph: number
  residential_increase_vph: number
  recommended_route: RouteSummary | null
  edge_changes: DiversionEdgeChange[]
  playback_edges?: DiversionPlaybackEdge[]
  assumptions: string[]
}

export type DiversionJob = {
  id: string
  status: 'queued' | 'running' | 'completed' | 'cancelled' | 'failed'
  progress_percent: number
  message: string
  cached: boolean
  created_at: string
  updated_at: string
  result: DiversionResult | null
  error: string | null
}

export async function startDiversion(
  origin: SelectedLocation,
  destination: SelectedLocation,
  closure: ClosureComparisonInput,
  settings: DiversionSettings,
): Promise<DiversionJob> {
  const route = buildRouteRequestBody(origin, destination, closure)
  return request<DiversionJob>('/api/diversions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ...route,
      demand_vph: settings.demandVph,
      demand_pair_count: settings.demandPairCount,
      iterations: settings.iterations,
      dispersion_radius_m: settings.dispersionRadiusM,
      traffic_profile_id: settings.trafficProfileId,
      max_result_edges: 200,
    }),
  })
}

export function getDiversion(id: string, signal?: AbortSignal): Promise<DiversionJob> {
  return request(`/api/diversions/${id}`, { signal })
}

export function cancelDiversion(id: string): Promise<DiversionJob> {
  return request(`/api/diversions/${id}`, { method: 'DELETE' })
}

async function request<Response>(
  url: string,
  init: RequestInit,
): Promise<Response> {
  const response = await fetch(url, init)
  if (response.ok) return response.json() as Promise<Response>
  const payload = (await response.json().catch(() => null)) as {
    detail?: { code?: string; message?: string }
  } | null
  throw new ApiError(
    payload?.detail?.code ?? 'request_failed',
    payload?.detail?.message ?? 'The diversion request failed.',
  )
}
