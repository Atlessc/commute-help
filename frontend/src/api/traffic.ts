import { ApiError, type RouteSummary } from './routing'

export type TrafficProfile = {
  id: string
  name: string
  version: string
  source_name: string
  source_window: string
  period: 'weekday_morning' | 'weekday_afternoon'
  graph_version: string
  observation_count: number
  volume_observation_count: number
  matched_edge_count: number
  median_multiplier: number
  p85_multiplier: number
  p90_multiplier: number
  p95_multiplier: number
  created_at: string
}

export type TrafficDataQuality = {
  row_count: number
  accepted_count: number
  rejected_count: number
  matched_station_count: number
  unmatched_station_count: number
  missing_speed_percent: number
  missing_volume_percent: number
  quality_flags: string[]
}

export type TrafficImportResponse = {
  import_id: string
  source_name: string
  raw_sha256: string
  normalized_path: string
  quality: TrafficDataQuality
  profiles: TrafficProfile[]
}

export type PortalHighway = {
  id: number
  name: string
  direction: string
  station_count: number
}

export type PortalAcquireSettings = {
  startDate: string
  endDate: string
  highwayIds: number[]
  resolution: '00:15:00' | '01:00:00'
}

export type PortalAcquireResponse = {
  requested_highways: PortalHighway[]
  downloaded_observation_count: number
  normalized_station_count: number
  import_result: TrafficImportResponse
}

export type ReliabilitySettings = {
  planningMode: 'arrive_by' | 'depart_at'
  arrivalDeadline: string
  bufferMinutes: number
  confidenceTarget: number
  sampleCount: number
  profileId: string | null
}

export type ReliabilityResponse = {
  planning_mode: 'arrive_by' | 'depart_at'
  evidence_level: 'modeled_uncalibrated' | 'historically_calibrated'
  mean_seconds: number
  median_seconds: number
  p85_seconds: number
  p90_seconds: number
  p95_seconds: number
  likely_low_seconds: number
  likely_high_seconds: number
  on_time_probability: number | null
  latest_safe_departure: string | null
  planned_departure_time: string
  median_arrival_time: string
  p85_arrival_time: string
  p90_arrival_time: string
  p95_arrival_time: string
  confidence_arrival_time: string
  confidence_target: number
  sample_count: number
  source_name: string
  source_window: string
  profile_version: string
  early_departure_benefits: {
    minutes_earlier: 5 | 10 | 15
    on_time_probability: number
    improvement: number
  }[]
  assumptions: string[]
}

export async function listTrafficProfiles(signal?: AbortSignal): Promise<TrafficProfile[]> {
  const response = await fetch('/api/traffic/profiles', { signal })
  const payload = await parseResponse<{ profiles: TrafficProfile[] }>(response)
  return payload.profiles
}

export async function importTraffic(
  file: File,
  sourceName: string,
): Promise<TrafficImportResponse> {
  const form = new FormData()
  form.append('file', file)
  form.append('source_name', sourceName)
  return parseResponse(
    await fetch('/api/traffic/import', { method: 'POST', body: form }),
  )
}

export async function listPortalHighways(signal?: AbortSignal): Promise<PortalHighway[]> {
  const response = await fetch('/api/traffic/portal/highways', { signal })
  const payload = await parseResponse<{ highways: PortalHighway[] }>(response)
  return payload.highways
}

export async function acquirePortalTraffic(
  settings: PortalAcquireSettings,
): Promise<PortalAcquireResponse> {
  return parseResponse(
    await fetch('/api/traffic/portal/acquire', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start_date: settings.startDate,
        end_date: settings.endDate,
        highway_ids: settings.highwayIds,
        resolution: settings.resolution,
        days_of_week: [2, 3, 4, 5, 6],
      }),
    }),
  )
}

export async function simulateReliability(
  route: RouteSummary,
  departureTime: string,
  settings: ReliabilitySettings,
): Promise<ReliabilityResponse> {
  return parseResponse(
    await fetch('/api/simulations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        route,
        planning_mode: settings.planningMode,
        departure_time: new Date(departureTime).toISOString(),
        arrival_deadline:
          settings.planningMode === 'arrive_by'
            ? new Date(settings.arrivalDeadline).toISOString()
            : null,
        buffer_minutes: settings.bufferMinutes,
        confidence_target: settings.confidenceTarget,
        sample_count: settings.sampleCount,
        profile_id: settings.profileId,
      }),
    }),
  )
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (response.ok) return response.json() as Promise<T>
  const payload = (await response.json().catch(() => null)) as {
    detail?: { code?: string; message?: string }
  } | null
  throw new ApiError(
    payload?.detail?.code ?? 'request_failed',
    payload?.detail?.message ?? 'The traffic request failed.',
  )
}
