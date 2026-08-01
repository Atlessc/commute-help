import { z } from 'zod'
import { ApiError } from './routing'
import type {
  ClosureRoadSelectionResponse,
  RoadRestriction,
  SelectedLocation,
} from './routing'

export type SavedScenarioRestriction = RoadRestriction & {
  starts_at?: string
  ends_at?: string
}

export type SavedClosureSection = {
  selection: ClosureRoadSelectionResponse
  selected_edge_ids: string[]
  restriction: SavedScenarioRestriction
}

export type ScenarioContent = {
  schema_version: 1
  graph_version: string
  origin: SelectedLocation
  destination: SelectedLocation
  departure_time: string
  closures: SavedClosureSection[]
  selected_route_id: string | null
  map_state: ScenarioMapState | null
  reliability_conditions: ScenarioReliabilityConditions | null
  diversion_conditions: ScenarioDiversionConditions | null
}

export type ScenarioMapState = {
  center_lng: number
  center_lat: number
  zoom: number
}

export type ScenarioReliabilityConditions = {
  planning_mode: 'arrive_by' | 'depart_at'
  arrival_deadline: string | null
  buffer_minutes: number
  confidence_target: number
  sample_count: number
  profile_id: string | null
}

export type ScenarioDiversionConditions = {
  demand_vph: number
  demand_pair_count: number
  iterations: number
  dispersion_radius_m: number
  traffic_profile_id: string | null
}

export type ScenarioListItem = {
  id: string
  name: string
  revision: number
  graph_version: string
  archived: boolean
  created_at: string
  updated_at: string
}

export type ScenarioRecord = ScenarioListItem & {
  schema_version: 1
  content: ScenarioContent
  graph_status: 'current' | 'rematched' | 'review_required'
  warnings: string[]
}

const coordinateSchema = z.tuple([z.number(), z.number()])
const edgeSchema = z.object({
  edge_id: z.string(),
  u: z.string(),
  v: z.string(),
  key: z.number().int(),
  road_name: z.string(),
  road_class: z.string(),
  osm_way_ids: z.array(z.string()),
  geometry_fingerprint: z.string(),
  lanes: z.number().positive(),
  maxspeed_kph: z.number().positive(),
})
const locationSchema = z.object({
  lat: z.number(),
  lng: z.number(),
  node_id: z.string(),
  distance_m: z.number().nonnegative(),
  label: z.string(),
  edge: edgeSchema,
})
const directionSchema = z.object({
  edge: edgeSchema,
  direction_label: z.string(),
  geometry: z.object({
    type: z.literal('LineString'),
    coordinates: z.array(coordinateSchema).min(2),
  }),
})
const restrictionSchema = z.discriminatedUnion('type', [
  z.object({
    type: z.literal('full'),
    starts_at: z.string().nullish().transform((value) => value ?? undefined),
    ends_at: z.string().nullish().transform((value) => value ?? undefined),
  }),
  z.object({
    type: z.literal('lane'),
    remaining_lanes: z.number().positive(),
    starts_at: z.string().nullish().transform((value) => value ?? undefined),
    ends_at: z.string().nullish().transform((value) => value ?? undefined),
  }),
  z.object({
    type: z.literal('speed'),
    speed_limit_kph: z.number().min(5).max(130),
    starts_at: z.string().nullish().transform((value) => value ?? undefined),
    ends_at: z.string().nullish().transform((value) => value ?? undefined),
  }),
])
const contentSchema = z.object({
  schema_version: z.literal(1),
  graph_version: z.string(),
  origin: locationSchema,
  destination: locationSchema,
  departure_time: z.string(),
  closures: z.array(
    z.object({
      selection: z.object({
        graph_version: z.string(),
        road_name: z.string(),
        distance_m: z.number().nonnegative(),
        selected_edge_id: z.string(),
        directions: z.array(directionSchema).min(1),
      }),
      selected_edge_ids: z.array(z.string()).min(1),
      restriction: restrictionSchema,
    }),
  ),
  selected_route_id: z.string().nullable(),
  map_state: z
    .object({
      center_lng: z.number().min(-180).max(180),
      center_lat: z.number().min(-90).max(90),
      zoom: z.number().min(0).max(24),
    })
    .nullable(),
  reliability_conditions: z
    .object({
      planning_mode: z.enum(['arrive_by', 'depart_at']).default('arrive_by'),
      arrival_deadline: z.string().nullable(),
      buffer_minutes: z.number().int().min(0).max(180),
      confidence_target: z.number().min(0.5).max(0.99),
      sample_count: z.number().int().min(100).max(10000),
      profile_id: z.string().uuid().nullable(),
    })
    .nullable()
    .default(null),
  diversion_conditions: z
    .object({
      demand_vph: z.number().int().min(50).max(5000),
      demand_pair_count: z.number().int().min(5).max(100),
      iterations: z.number().int().min(2).max(10),
      dispersion_radius_m: z.number().int().min(0).max(10000),
      traffic_profile_id: z.string().uuid().nullable().default(null),
    })
    .nullable()
    .default(null),
})

export const scenarioRecordSchema = z.object({
  schema_version: z.literal(1),
  id: z.string().uuid(),
  name: z.string(),
  revision: z.number().int().positive(),
  graph_version: z.string(),
  archived: z.boolean(),
  created_at: z.string(),
  updated_at: z.string(),
  content: contentSchema,
  graph_status: z.enum(['current', 'rematched', 'review_required']),
  warnings: z.array(z.string()),
})

export async function listScenarios(signal?: AbortSignal): Promise<ScenarioListItem[]> {
  const response = await fetch('/api/scenarios', { signal })
  const payload = await parseResponse<{ scenarios: ScenarioListItem[] }>(response)
  return payload.scenarios
}

export async function getScenario(
  id: string,
  signal?: AbortSignal,
): Promise<ScenarioRecord> {
  const response = await fetch(`/api/scenarios/${id}`, { signal })
  return scenarioRecordSchema.parse(await parseResponse<unknown>(response))
}

export async function createScenario(
  name: string,
  content: ScenarioContent,
): Promise<ScenarioRecord> {
  return sendScenario('/api/scenarios', 'POST', { name, content })
}

export async function updateScenario(
  scenario: ScenarioRecord,
  name: string,
  content: ScenarioContent,
): Promise<ScenarioRecord> {
  return sendScenario(`/api/scenarios/${scenario.id}`, 'PUT', {
    name,
    content,
    expected_revision: scenario.revision,
  })
}

export async function duplicateScenario(id: string): Promise<ScenarioRecord> {
  return sendScenario(`/api/scenarios/${id}/duplicate`, 'POST', {})
}

export async function archiveScenario(id: string): Promise<void> {
  const response = await fetch(`/api/scenarios/${id}`, { method: 'DELETE' })
  if (!response.ok) await throwApiError(response)
}

export async function exportScenario(id: string): Promise<ScenarioRecord> {
  const response = await fetch(`/api/scenarios/${id}/export`)
  return scenarioRecordSchema.parse(await parseResponse<unknown>(response))
}

export async function importScenario(value: unknown): Promise<ScenarioRecord> {
  const scenario = scenarioRecordSchema.parse(value)
  return sendScenario('/api/scenarios/import', 'POST', { scenario })
}

async function sendScenario(
  url: string,
  method: 'POST' | 'PUT',
  body: unknown,
): Promise<ScenarioRecord> {
  const response = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return scenarioRecordSchema.parse(await parseResponse<unknown>(response))
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) await throwApiError(response)
  return response.json() as Promise<T>
}

async function throwApiError(response: Response): Promise<never> {
  const payload = (await response.json().catch(() => null)) as {
    detail?: { code?: string; message?: string }
  } | null
  throw new ApiError(
    payload?.detail?.code ?? 'request_failed',
    payload?.detail?.message ?? 'The scenario request failed.',
  )
}
