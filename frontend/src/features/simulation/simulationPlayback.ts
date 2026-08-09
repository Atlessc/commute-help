import type { DiversionPlaybackEdge, DiversionResult } from '../../api/diversion'
import type { RouteSummary } from '../../api/routing'
import type { PhysicalSimulationPlayback } from '../../api/simulation'

export type TrafficAgentPoint = {
  id: string
  coordinate: [number, number]
  congestion: 'free' | 'slow' | 'heavy'
  opacity: number
}

export type SimulationMapFrame = {
  agents: TrafficAgentPoint[]
  traveledRoute: [number, number][]
  projectedRoute: [number, number][]
  tripCoordinate: [number, number] | null
  progress: number
  rerouted: boolean
}

export function physicalSimulationMapFrame(
  playback: PhysicalSimulationPlayback,
  elapsedSeconds: number,
  rerouted: boolean,
): SimulationMapFrame {
  const frames = playback.frames
  if (frames.length === 0) {
    return { agents: [], traveledRoute: [], projectedRoute: [], tripCoordinate: null, progress: 0, rerouted }
  }
  const elapsed = clamp(elapsedSeconds, 0, playback.duration_seconds)
  const nextIndex = frames.findIndex((frame) => frame.elapsed_seconds >= elapsed)
  const right = frames[nextIndex < 0 ? frames.length - 1 : nextIndex]
  const left = frames[Math.max(0, (nextIndex < 0 ? frames.length : nextIndex) - 1)]
  const span = Math.max(right.elapsed_seconds - left.elapsed_seconds, 1)
  const amount = clamp((elapsed - left.elapsed_seconds) / span, 0, 1)
  const rightAgents = new Map(right.agents.map((agent) => [agent.id, agent]))
  const agents = left.agents.map((agent) => {
    const target = rightAgents.get(agent.id)
    return target ? {
      ...target,
      coordinate: [
        lerp(agent.coordinate[0], target.coordinate[0], amount),
        lerp(agent.coordinate[1], target.coordinate[1], amount),
      ] as [number, number],
    } : agent
  })
  const tripCoordinate = interpolateCoordinate(left.trip_coordinate, right.trip_coordinate, amount)
  return {
    agents,
    traveledRoute: amount < 0.5 ? left.traveled_route : right.traveled_route,
    projectedRoute: amount < 0.5 ? left.projected_route : right.projected_route,
    tripCoordinate,
    progress: playback.duration_seconds ? elapsed / playback.duration_seconds : 0,
    rerouted,
  }
}

function interpolateCoordinate(
  left: [number, number] | null,
  right: [number, number] | null,
  amount: number,
): [number, number] | null {
  if (!left) return right
  if (!right) return left
  return [lerp(left[0], right[0], amount), lerp(left[1], right[1], amount)]
}

type AgentSeed = {
  id: string
  edge: DiversionPlaybackEdge
  offset: number
  speed: number
  reactionAt: number
  visibilityRank: number
}

const MAX_VISIBLE_AGENTS = 900
const MINUTES = 60

export function buildSimulationSeeds(result: DiversionResult): AgentSeed[] {
  const candidates = (result.playback_edges?.length
    ? result.playback_edges
    : result.edge_changes).filter(
    (edge) => edge.geometry.coordinates.length >= 2,
  )
  const totalFlow = candidates.reduce(
    (sum, edge) => sum + Math.max(edge.baseline_vph, edge.scenario_vph),
    0,
  )
  if (totalFlow <= 0) return []

  const seeds: AgentSeed[] = []
  for (const edge of candidates) {
    const share = Math.max(edge.baseline_vph, edge.scenario_vph) / totalFlow
    const count = Math.max(1, Math.round(share * MAX_VISIBLE_AGENTS))
    for (let index = 0; index < count; index += 1) {
      const random = seededNumbers(`${edge.edge_id}:${index}`)
      seeds.push({
        id: `${edge.edge_id}:${index}`,
        edge,
        offset: random[0],
        speed: 0.45 + random[1] * 0.9,
        reactionAt: 0.05 + random[2] * 0.72,
        visibilityRank: random[3],
      })
    }
  }
  return seeds.slice(0, MAX_VISIBLE_AGENTS)
}

export function simulationMapFrame(
  seeds: AgentSeed[],
  result: DiversionResult,
  route: RouteSummary | null,
  elapsedSeconds: number,
): SimulationMapFrame {
  const progress = clamp(elapsedSeconds / (MINUTES * 60), 0, 1)
  const agents = seeds.flatMap((seed) => {
    const transition = smoothResponse(progress, seed.reactionAt)
    const currentFlow = lerp(
      seed.edge.baseline_vph,
      seed.edge.scenario_vph,
      transition,
    )
    const maximum = Math.max(seed.edge.baseline_vph, seed.edge.scenario_vph, 1)
    const visibleShare = clamp(currentFlow / maximum, 0, 1)
    if (seed.visibilityRank > visibleShare) return []
    const lineProgress = (seed.offset + elapsedSeconds / 75 * seed.speed) % 1
    const coordinate = pointAlongLine(seed.edge.geometry.coordinates, lineProgress)
    const ratio = seed.edge.volume_capacity_ratio * (0.65 + transition * 0.35)
    return [{
      id: seed.id,
      coordinate,
      congestion: ratio >= 1 ? 'heavy' as const : ratio >= 0.72 ? 'slow' as const : 'free' as const,
      opacity: clamp(0.38 + visibleShare * 0.62, 0, 1),
    }]
  })

  const routeCoordinates = route?.geometry.coordinates ?? []
  const tripStart = 0
  const tripDuration = Math.max(route?.travel_time_seconds ?? 1, 1)
  const tripProgress = clamp((elapsedSeconds - tripStart) / tripDuration, 0, 1)
  const split = splitLine(routeCoordinates, tripProgress)
  return {
    agents,
    traveledRoute: split.traveled,
    projectedRoute: split.projected,
    tripCoordinate: split.coordinate,
    progress,
    rerouted: result.changed_edge_count > 0 && progress >= 0.18,
  }
}

function smoothResponse(progress: number, reactionAt: number): number {
  const width = 0.16
  return smoothstep(
    clamp((progress - reactionAt + width / 2) / width, 0, 1),
  )
}

function smoothstep(value: number): number {
  return value * value * (3 - 2 * value)
}

function pointAlongLine(
  coordinates: [number, number][],
  progress: number,
): [number, number] {
  if (coordinates.length === 0) return [0, 0]
  if (coordinates.length === 1) return coordinates[0]
  const lengths = segmentLengths(coordinates)
  const total = lengths.reduce((sum, length) => sum + length, 0)
  let remaining = total * clamp(progress, 0, 1)
  for (let index = 0; index < lengths.length; index += 1) {
    if (remaining <= lengths[index]) {
      const portion = lengths[index] === 0 ? 0 : remaining / lengths[index]
      return [
        lerp(coordinates[index][0], coordinates[index + 1][0], portion),
        lerp(coordinates[index][1], coordinates[index + 1][1], portion),
      ]
    }
    remaining -= lengths[index]
  }
  return coordinates.at(-1) ?? [0, 0]
}

function splitLine(
  coordinates: [number, number][],
  progress: number,
): {
  traveled: [number, number][]
  projected: [number, number][]
  coordinate: [number, number] | null
} {
  if (coordinates.length < 2) {
    return { traveled: [], projected: coordinates, coordinate: coordinates[0] ?? null }
  }
  const lengths = segmentLengths(coordinates)
  const total = lengths.reduce((sum, length) => sum + length, 0)
  let remaining = total * clamp(progress, 0, 1)
  const traveled: [number, number][] = [coordinates[0]]
  for (let index = 0; index < lengths.length; index += 1) {
    if (remaining <= lengths[index]) {
      const portion = lengths[index] === 0 ? 0 : remaining / lengths[index]
      const coordinate: [number, number] = [
        lerp(coordinates[index][0], coordinates[index + 1][0], portion),
        lerp(coordinates[index][1], coordinates[index + 1][1], portion),
      ]
      traveled.push(coordinate)
      return {
        traveled,
        projected: [coordinate, ...coordinates.slice(index + 1)],
        coordinate,
      }
    }
    traveled.push(coordinates[index + 1])
    remaining -= lengths[index]
  }
  return {
    traveled: coordinates,
    projected: [coordinates.at(-1) ?? coordinates[0]],
    coordinate: coordinates.at(-1) ?? null,
  }
}

function segmentLengths(coordinates: [number, number][]): number[] {
  return coordinates.slice(0, -1).map((coordinate, index) => {
    const next = coordinates[index + 1]
    const averageLatitude = (coordinate[1] + next[1]) / 2 * Math.PI / 180
    const x = (next[0] - coordinate[0]) * Math.cos(averageLatitude)
    const y = next[1] - coordinate[1]
    return Math.hypot(x, y)
  })
}

function seededNumbers(value: string): [number, number, number, number] {
  let state = 2166136261
  for (let index = 0; index < value.length; index += 1) {
    state ^= value.charCodeAt(index)
    state = Math.imul(state, 16777619)
  }
  const next = () => {
    state += 0x6d2b79f5
    let result = state
    result = Math.imul(result ^ result >>> 15, result | 1)
    result ^= result + Math.imul(result ^ result >>> 7, result | 61)
    return ((result ^ result >>> 14) >>> 0) / 4294967296
  }
  return [next(), next(), next(), next()]
}

function lerp(start: number, end: number, amount: number): number {
  return start + (end - start) * amount
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value))
}
