import { z } from 'zod'
import { ApiError } from './routing'
import type {
  ClosureRoadSelectionResponse,
  RoadRestriction,
} from './routing'

export type ClosurePresetSection = {
  label: string
  selection: ClosureRoadSelectionResponse
  selected_edge_ids: string[]
  restriction: RoadRestriction
}

export type ClosurePreset = {
  id: string
  name: string
  summary: string
  graph_version: string
  timing: {
    mainline_starts_at: string
    ramp_closures_may_start_at: string | null
    duration_note: string
    exact_end_confirmed: boolean
  }
  notices: string[]
  sources: Array<{
    title: string
    url: string
    checked_on: string
  }>
  sections: ClosurePresetSection[]
  graph_status: 'current' | 'review_required'
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
const selectionSchema = z.object({
  graph_version: z.string(),
  road_name: z.string(),
  distance_m: z.number().nonnegative(),
  selected_edge_id: z.string(),
  directions: z.array(
    z.object({
      edge: edgeSchema,
      direction_label: z.string(),
      geometry: z.object({
        type: z.literal('LineString'),
        coordinates: z.array(coordinateSchema).min(2),
      }),
    }),
  ).min(1),
})
const restrictionSchema = z.discriminatedUnion('type', [
  z.object({ type: z.literal('full') }),
  z.object({
    type: z.literal('lane'),
    remaining_lanes: z.number().positive(),
  }),
  z.object({
    type: z.literal('speed'),
    speed_limit_kph: z.number().min(5).max(130),
  }),
])
const presetSchema = z.object({
  id: z.string(),
  name: z.string(),
  summary: z.string(),
  graph_version: z.string(),
  timing: z.object({
    mainline_starts_at: z.string(),
    ramp_closures_may_start_at: z.string().nullable(),
    duration_note: z.string(),
    exact_end_confirmed: z.boolean(),
  }),
  notices: z.array(z.string()),
  sources: z.array(z.object({
    title: z.string(),
    url: z.string().url(),
    checked_on: z.string(),
  })),
  sections: z.array(z.object({
    label: z.string(),
    selection: selectionSchema,
    selected_edge_ids: z.array(z.string()).min(1),
    restriction: restrictionSchema,
  })).min(1),
  graph_status: z.enum(['current', 'review_required']),
  warnings: z.array(z.string()),
})

const presetListSchema = z.object({ presets: z.array(presetSchema) })

export async function listClosurePresets(
  signal?: AbortSignal,
): Promise<ClosurePreset[]> {
  const response = await fetch('/api/closure-presets', { signal })
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as {
      detail?: { code?: string; message?: string }
    } | null
    throw new ApiError(
      payload?.detail?.code ?? 'closure_presets_failed',
      payload?.detail?.message ?? 'Closure presets could not be loaded.',
    )
  }
  return presetListSchema.parse(await response.json()).presets
}
