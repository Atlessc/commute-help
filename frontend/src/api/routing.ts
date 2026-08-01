export type CoordinateInput = {
  lat: number
  lng: number
}

export type DirectedEdgeReference = {
  edge_id: string
  u: string
  v: string
  key: number
  road_name: string
  road_class: string
  osm_way_ids: string[]
  geometry_fingerprint: string
  lanes: number
  maxspeed_kph: number
}

export type SelectedLocation = CoordinateInput & {
  node_id: string
  distance_m: number
  label: string
  edge: DirectedEdgeReference
}

export type RoadSelectionResponse = {
  graph_version: string
  location: SelectedLocation
}

export type ClosureDirectionCandidate = {
  edge: DirectedEdgeReference
  direction_label: string
  geometry: {
    type: 'LineString'
    coordinates: [number, number][]
  }
}

export type ClosureRoadSelectionResponse = {
  graph_version: string
  road_name: string
  distance_m: number
  selected_edge_id: string
  directions: ClosureDirectionCandidate[]
}

export type RouteSummary = {
  route_id: string
  travel_time_seconds: number
  distance_m: number
  geometry: {
    type: 'LineString'
    coordinates: [number, number][]
  }
}

export type RouteCompareResponse = {
  graph_version: string
  evidence_level: 'free_flow' | 'modeled_uncalibrated'
  baseline: RouteSummary
  scenario: RouteSummary | null
  scenario_status: 'not_requested' | 'available' | 'no_route' | 'inactive'
  evaluated_departure_time: string
  applied_restriction_edge_ids: string[]
  inactive_restriction_edge_ids: string[]
  assumptions: string[]
}

export type AlternativeRoute = {
  ranking: 'fastest' | 'reliable' | 'balanced'
  route: RouteSummary
  overlap_with_fastest_percent: number
  residential_distance_percent: number
  description: string
}

export type RouteAlternativesResponse = {
  graph_version: string
  evidence_level: 'modeled_uncalibrated'
  routes: AlternativeRoute[]
  evaluated_departure_time: string
  assumptions: string[]
}

export type GoogleMapsUrlResponse = {
  url: string
  waypoint_count: number
  warning: string
}

export type RoadRestriction =
  | { type: 'full' }
  | { type: 'lane'; remaining_lanes: number }
  | { type: 'speed'; speed_limit_kph: number }

export type RestrictionSchedule =
  | { type: 'always' }
  | { type: 'scheduled'; starts_at: string; ends_at: string }

export type ClosureSectionDraft = {
  selection: ClosureRoadSelectionResponse
  restriction: RoadRestriction
  schedule: RestrictionSchedule
}

export type ClosureComparisonSection = {
  directions: ClosureDirectionCandidate[]
  restriction: RoadRestriction
  schedule: RestrictionSchedule
}

export type ClosureComparisonInput = {
  graphVersion: string
  departureTime: string
  sections: ClosureComparisonSection[]
}

export class ApiError extends Error {
  code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = 'ApiError'
    this.code = code
  }
}

export async function selectRoad(
  coordinate: CoordinateInput,
  signal?: AbortSignal,
): Promise<RoadSelectionResponse> {
  return requestJson('/api/roads/select', coordinate, signal)
}

export async function selectClosureRoad(
  coordinate: CoordinateInput,
  signal?: AbortSignal,
): Promise<ClosureRoadSelectionResponse> {
  return requestJson('/api/roads/select-closure', coordinate, signal)
}

export async function compareRoute(
  origin: SelectedLocation,
  destination: SelectedLocation,
  closure?: ClosureComparisonInput,
  signal?: AbortSignal,
): Promise<RouteCompareResponse> {
  return requestJson(
    '/api/routes/compare',
    buildRouteRequestBody(origin, destination, closure),
    signal,
  )
}

export async function findAlternativeRoutes(
  origin: SelectedLocation,
  destination: SelectedLocation,
  closure?: ClosureComparisonInput,
  signal?: AbortSignal,
): Promise<RouteAlternativesResponse> {
  return requestJson(
    '/api/routes/alternatives',
    buildRouteRequestBody(origin, destination, closure),
    signal,
  )
}

export async function createGoogleMapsUrl(
  origin: SelectedLocation,
  destination: SelectedLocation,
  route: RouteSummary,
  signal?: AbortSignal,
): Promise<GoogleMapsUrlResponse> {
  return requestJson(
    '/api/routes/google-maps-url',
    {
      origin: { lat: origin.lat, lng: origin.lng },
      destination: { lat: destination.lat, lng: destination.lng },
      route,
    },
    signal,
  )
}

export function buildRouteRequestBody(
  origin: SelectedLocation,
  destination: SelectedLocation,
  closure?: ClosureComparisonInput,
) {
  return {
    origin: {
      lat: origin.lat,
      lng: origin.lng,
      node_id: origin.node_id,
    },
    destination: {
      lat: destination.lat,
      lng: destination.lng,
      node_id: destination.node_id,
    },
    graph_version: closure?.graphVersion,
    departure_time: closure?.departureTime,
    closures: closure
      ? closure.sections.map(({ directions, restriction, schedule }) => ({
          ...restriction,
          ...(schedule.type === 'scheduled'
            ? {
                starts_at: new Date(schedule.starts_at).toISOString(),
                ends_at: new Date(schedule.ends_at).toISOString(),
              }
            : {}),
          edges: directions.map(({ edge }) => ({
            edge_id: edge.edge_id,
            u: edge.u,
            v: edge.v,
            key: edge.key,
          })),
        }))
      : [],
  }
}

async function requestJson<Response>(
  url: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<Response> {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })

  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as {
      detail?: { code?: string; message?: string }
    } | null
    throw new ApiError(
      payload?.detail?.code ?? 'request_failed',
      payload?.detail?.message ?? 'The local routing request failed.',
    )
  }

  return response.json() as Promise<Response>
}
