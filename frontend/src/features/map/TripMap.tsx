import { useEffect, useRef, useState } from 'react'
import {
  GeoJSONSource,
  LngLatBounds,
  Map as MapLibreMap,
  type MapMouseEvent,
  Marker,
  NavigationControl,
  setWorkerUrl,
  type StyleSpecification,
} from 'maplibre-gl'
import mapLibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url'
import 'maplibre-gl/dist/maplibre-gl.css'
import type {
  ClosureDirectionCandidate,
  RoadRestriction,
  RouteSummary,
  SelectedLocation,
} from '../../api/routing'
import type { SelectionMode } from '../../stores/tripStore'
import type { ScenarioMapState } from '../../api/scenarios'
import type { DiversionEdgeChange } from '../../api/diversion'
import type { SimulationMapFrame } from '../simulation/simulationPlayback'
import './TripMap.css'

setWorkerUrl(mapLibreWorkerUrl)

const EMPTY_ROUTE = {
  type: 'FeatureCollection' as const,
  features: [],
}

const MAP_STYLE: StyleSpecification = {
  version: 8,
  sources: {
    openStreetMap: {
      type: 'raster',
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      maxzoom: 19,
      attribution:
        '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>',
    },
  },
  layers: [
    {
      id: 'openStreetMap',
      type: 'raster',
      source: 'openStreetMap',
    },
  ],
}

type TripMapProps = {
  origin: SelectedLocation | null
  destination: SelectedLocation | null
  route: RouteSummary | null
  scenarioRoute: RouteSummary | null
  closureDirections: ClosureMapDirection[]
  spilloverEdges: DiversionEdgeChange[]
  simulationFrame: SimulationMapFrame | null
  simulationPlaying: boolean
  selectionMode: SelectionMode
  selectionPending: boolean
  closurePicking: boolean
  closureSelectionPending: boolean
  onCoordinatePick: (coordinate: { lat: number; lng: number }) => void
  onClosurePick: (coordinate: { lat: number; lng: number }) => void
  mapState: ScenarioMapState | null
  onMapStateChange: (state: ScenarioMapState) => void
}

export type ClosureMapDirection = ClosureDirectionCandidate & {
  restrictionType: RoadRestriction['type']
}

export default function TripMap({
  origin,
  destination,
  route,
  scenarioRoute,
  closureDirections,
  spilloverEdges,
  simulationFrame,
  simulationPlaying,
  selectionMode,
  selectionPending,
  closurePicking,
  closureSelectionPending,
  onCoordinatePick,
  onClosurePick,
  mapState,
  onMapStateChange,
}: TripMapProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<MapLibreMap | null>(null)
  const markersRef = useRef<Marker[]>([])
  const pickHandlerRef = useRef(onCoordinatePick)
  const closurePickHandlerRef = useRef(onClosurePick)
  const closurePickingRef = useRef(closurePicking)
  const routeRef = useRef(route)
  const scenarioRouteRef = useRef(scenarioRoute)
  const closureDirectionsRef = useRef(closureDirections)
  const spilloverEdgesRef = useRef(spilloverEdges)
  const simulationFrameRef = useRef(simulationFrame)
  const lastFollowProgressRef = useRef(-1)
  const mapStateChangeRef = useRef(onMapStateChange)
  const mapStateRef = useRef(mapState)
  const [mapError, setMapError] = useState<string | null>(null)

  useEffect(() => {
    pickHandlerRef.current = onCoordinatePick
  }, [onCoordinatePick])

  useEffect(() => {
    closurePickHandlerRef.current = onClosurePick
  }, [onClosurePick])

  useEffect(() => {
    closurePickingRef.current = closurePicking
  }, [closurePicking])

  useEffect(() => {
    routeRef.current = route
  }, [route])

  useEffect(() => {
    scenarioRouteRef.current = scenarioRoute
  }, [scenarioRoute])

  useEffect(() => {
    closureDirectionsRef.current = closureDirections
  }, [closureDirections])

  useEffect(() => {
    spilloverEdgesRef.current = spilloverEdges
  }, [spilloverEdges])

  useEffect(() => {
    simulationFrameRef.current = simulationFrame
  }, [simulationFrame])

  useEffect(() => {
    mapStateChangeRef.current = onMapStateChange
  }, [onMapStateChange])

  useEffect(() => {
    mapStateRef.current = mapState
  }, [mapState])

  useEffect(() => {
    if (!containerRef.current) return

    const map = new MapLibreMap({
      container: containerRef.current,
      style: MAP_STYLE,
      center: mapStateRef.current
        ? [mapStateRef.current.center_lng, mapStateRef.current.center_lat]
        : [-122.675, 45.55],
      zoom: mapStateRef.current?.zoom ?? 9.4,
      minZoom: 7,
      maxZoom: 19,
      maxBounds: [
        [-123.2, 45.12],
        [-122.08, 45.98],
      ],
      attributionControl: { compact: true },
    })
    mapRef.current = map
    map.addControl(new NavigationControl(), 'top-right')
    map.getCanvas().setAttribute('aria-label', 'Portland–Vancouver trip map')

    map.on('load', () => {
      map.addSource('baseline-route', {
        type: 'geojson',
        data: routeFeature(routeRef.current),
      })
      map.addLayer({
        id: 'baseline-route-casing',
        type: 'line',
        source: 'baseline-route',
        paint: {
          'line-color': '#f8f7f0',
          'line-width': 13,
          'line-opacity': 0.98,
        },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
      })
      map.addLayer({
        id: 'baseline-route-line',
        type: 'line',
        source: 'baseline-route',
        paint: {
          'line-color': '#146b8c',
          'line-width': 7,
          'line-opacity': 1,
        },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
      })
      map.addSource('closure-selection', {
        type: 'geojson',
        data: closureFeature(closureDirectionsRef.current),
      })
      map.addLayer({
        id: 'closure-full-line',
        type: 'line',
        source: 'closure-selection',
        filter: ['==', ['get', 'restrictionType'], 'full'],
        paint: {
          'line-color': '#c3462d',
          'line-width': 11,
          'line-opacity': 0.9,
        },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
      })
      map.addLayer({
        id: 'closure-lane-line',
        type: 'line',
        source: 'closure-selection',
        filter: ['==', ['get', 'restrictionType'], 'lane'],
        paint: {
          'line-color': '#4d963d',
          'line-width': 9,
          'line-opacity': 0.95,
          'line-dasharray': [0.1, 1.35],
        },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
      })
      map.addLayer({
        id: 'closure-speed-line',
        type: 'line',
        source: 'closure-selection',
        filter: ['==', ['get', 'restrictionType'], 'speed'],
        paint: {
          'line-color': '#c48320',
          'line-width': 8,
          'line-opacity': 0.95,
          'line-dasharray': [1.2, 1.2],
        },
        layout: { 'line-cap': 'butt', 'line-join': 'round' },
      })
      map.addSource('scenario-route', {
        type: 'geojson',
        data: routeFeature(scenarioRouteRef.current),
      })
      map.addLayer({
        id: 'scenario-route-casing',
        type: 'line',
        source: 'scenario-route',
        paint: {
          'line-color': '#f8f7f0',
          'line-width': 11,
          'line-opacity': 0.98,
        },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
      })
      map.addLayer({
        id: 'scenario-route-line',
        type: 'line',
        source: 'scenario-route',
        paint: {
          'line-color': '#c3462d',
          'line-width': 6,
          'line-opacity': 1,
        },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
      })
      map.addSource('spillover-edges', {
        type: 'geojson',
        data: spilloverFeature(spilloverEdgesRef.current),
      })
      map.addSource('simulation-agents', {
        type: 'geojson',
        data: simulationAgentsFeature(simulationFrameRef.current),
      })
      map.addLayer({
        id: 'simulation-agents-points',
        type: 'circle',
        source: 'simulation-agents',
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 8, 2.5, 14, 4.2],
          'circle-color': [
            'match',
            ['get', 'congestion'],
            'heavy', '#d52f2f',
            'slow', '#e9a126',
            '#39a852',
          ],
          'circle-opacity': ['get', 'opacity'],
          'circle-stroke-color': 'rgba(255,255,255,0.72)',
          'circle-stroke-width': 0.35,
        },
      })
      map.addSource('simulation-projected-route', {
        type: 'geojson',
        data: simulationRouteFeature(simulationFrameRef.current?.projectedRoute ?? []),
      })
      map.addLayer({
        id: 'simulation-projected-route-casing',
        type: 'line',
        source: 'simulation-projected-route',
        paint: { 'line-color': '#f8f7f0', 'line-width': 10, 'line-opacity': 0.92 },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
      })
      map.addLayer({
        id: 'simulation-projected-route-line',
        type: 'line',
        source: 'simulation-projected-route',
        paint: { 'line-color': '#22a3aa', 'line-width': 5, 'line-opacity': 0.9, 'line-dasharray': [2, 1.2] },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
      })
      map.addSource('simulation-traveled-route', {
        type: 'geojson',
        data: simulationRouteFeature(simulationFrameRef.current?.traveledRoute ?? []),
      })
      map.addLayer({
        id: 'simulation-traveled-route-casing',
        type: 'line',
        source: 'simulation-traveled-route',
        paint: { 'line-color': '#f8f7f0', 'line-width': 11, 'line-opacity': 0.96 },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
      })
      map.addLayer({
        id: 'simulation-traveled-route-line',
        type: 'line',
        source: 'simulation-traveled-route',
        paint: { 'line-color': '#156bd1', 'line-width': 6, 'line-opacity': 1 },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
      })
      map.addSource('simulation-trip-agent', {
        type: 'geojson',
        data: simulationTripFeature(simulationFrameRef.current?.tripCoordinate ?? null),
      })
      map.addLayer({
        id: 'simulation-trip-agent-halo',
        type: 'circle',
        source: 'simulation-trip-agent',
        paint: { 'circle-radius': 12, 'circle-color': '#25aab0', 'circle-opacity': 0.2 },
      })
      map.addLayer({
        id: 'simulation-trip-agent-dot',
        type: 'circle',
        source: 'simulation-trip-agent',
        paint: {
          'circle-radius': 6,
          'circle-color': '#ffffff',
          'circle-stroke-color': '#15959e',
          'circle-stroke-width': 3,
        },
      })
      map.addLayer(
        {
          id: 'spillover-increase',
          type: 'line',
          source: 'spillover-edges',
          filter: ['>', ['get', 'changeVph'], 0],
          paint: {
            'line-color': '#b34f2d',
            'line-width': [
              'interpolate',
              ['linear'],
              ['abs', ['get', 'changeVph']],
              0,
              2,
              1200,
              10,
            ],
            'line-opacity': 0.82,
          },
          layout: { 'line-cap': 'round', 'line-join': 'round' },
        },
        'baseline-route-casing',
      )
      map.addLayer(
        {
          id: 'spillover-decrease',
          type: 'line',
          source: 'spillover-edges',
          filter: ['<', ['get', 'changeVph'], 0],
          paint: {
            'line-color': '#356f91',
            'line-width': [
              'interpolate',
              ['linear'],
              ['abs', ['get', 'changeVph']],
              0,
              2,
              1200,
              9,
            ],
            'line-opacity': 0.78,
            'line-dasharray': [2, 2],
          },
          layout: { 'line-cap': 'butt', 'line-join': 'round' },
        },
        'baseline-route-casing',
      )
      syncRouteSource(map, routeRef.current)
      syncSpilloverSource(map, spilloverEdgesRef.current)
      syncSimulationSources(map, simulationFrameRef.current)
      fitRoute(map, routeRef.current)
    })
    map.on('click', (event: MapMouseEvent) => {
      const coordinate = { lat: event.lngLat.lat, lng: event.lngLat.lng }
      if (closurePickingRef.current) {
        closurePickHandlerRef.current(coordinate)
      } else {
        pickHandlerRef.current(coordinate)
      }
    })
    map.on('error', () => {
      setMapError('Some background map tiles could not be loaded.')
    })
    map.on('moveend', (event) => {
      if (!event.originalEvent) return
      const center = map.getCenter()
      mapStateChangeRef.current({
        center_lng: center.lng,
        center_lat: center.lat,
        zoom: map.getZoom(),
      })
    })

    return () => {
      markersRef.current.forEach((marker) => marker.remove())
      markersRef.current = []
      map.remove()
      mapRef.current = null
    }
  }, [])

  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    map.getCanvas().style.cursor =
      (selectionMode && !selectionPending) ||
      (closurePicking && !closureSelectionPending)
        ? 'crosshair'
        : 'grab'
  }, [
    selectionMode,
    selectionPending,
    closurePicking,
    closureSelectionPending,
  ])

  useEffect(() => {
    const map = mapRef.current
    if (!map) return

    markersRef.current.forEach((marker) => marker.remove())
    markersRef.current = [
      createMarker(map, origin, '#23563f', 'Trip origin'),
      createMarker(map, destination, '#a4472b', 'Trip destination'),
    ].filter((marker): marker is Marker => marker !== null)
  }, [origin, destination])

  useEffect(() => {
    const map = mapRef.current
    if (!map) return

    const sync = () => {
      if (!syncRouteSource(map, route)) return
      fitRoute(map, route)
    }
    if (!syncRouteSource(map, route)) {
      map.once('styledata', sync)
      return () => {
        map.off('styledata', sync)
      }
    }
    fitRoute(map, route)
  }, [route])

  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const sync = () => {
      if (!syncRouteSource(map, scenarioRoute, 'scenario-route')) return
      fitRoute(map, scenarioRoute)
    }
    if (!syncRouteSource(map, scenarioRoute, 'scenario-route')) {
      map.once('styledata', sync)
      return () => {
        map.off('styledata', sync)
      }
    }
    fitRoute(map, scenarioRoute)
  }, [scenarioRoute])

  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const sync = () => syncClosureSource(map, closureDirections)
    if (!syncClosureSource(map, closureDirections)) {
      map.once('styledata', sync)
      return () => {
        map.off('styledata', sync)
      }
    }
  }, [closureDirections])

  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const sync = () => syncSpilloverSource(map, spilloverEdges)
    if (!syncSpilloverSource(map, spilloverEdges)) {
      map.once('styledata', sync)
      return () => {
        map.off('styledata', sync)
      }
    }
  }, [spilloverEdges])

  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const sync = () => syncSimulationSources(map, simulationFrame)
    if (!syncSimulationSources(map, simulationFrame)) {
      map.once('styledata', sync)
      return () => {
        map.off('styledata', sync)
      }
    }
  }, [simulationFrame])

  useEffect(() => {
    const map = mapRef.current
    const coordinate = simulationFrame?.tripCoordinate
    if (!map || !simulationPlaying || !coordinate) return
    if (Math.abs(simulationFrame.progress - lastFollowProgressRef.current) < 0.01) return
    lastFollowProgressRef.current = simulationFrame.progress
    map.easeTo({
      center: coordinate,
      zoom: Math.min(map.getZoom(), 11.6),
      duration: 280,
    })
  }, [simulationFrame, simulationPlaying])

  useEffect(() => {
    if (!simulationPlaying) lastFollowProgressRef.current = -1
  }, [simulationPlaying])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !mapState) return
    const center = map.getCenter()
    if (
      Math.abs(center.lng - mapState.center_lng) < 0.000001 &&
      Math.abs(center.lat - mapState.center_lat) < 0.000001 &&
      Math.abs(map.getZoom() - mapState.zoom) < 0.001
    ) {
      return
    }
    map.jumpTo({
      center: [mapState.center_lng, mapState.center_lat],
      zoom: mapState.zoom,
    })
  }, [mapState])

  return (
    <section className="map-shell" aria-label="Trip map">
      <div ref={containerRef} className="trip-map" />
      {selectionMode || closurePicking ? (
        <div className="map-instruction" role="status">
          {closurePicking
            ? closureSelectionPending
              ? 'Finding road directions…'
              : 'Click each road section to add it, then choose Done selecting roads'
            : selectionPending
              ? 'Finding the nearest routable road…'
              : `Click the map to set ${selectionMode === 'origin' ? 'Point A' : 'Point B'}`}
        </div>
      ) : null}
      {closureDirections.length > 0 ? (
        <div className="map-closure-legend" aria-label="Road impact map legend">
          {closureDirections.some(({ restrictionType }) => restrictionType === 'full') ? (
            <span><i className="map-legend-line map-legend-line--full" /> Full closure</span>
          ) : null}
          {closureDirections.some(({ restrictionType }) => restrictionType === 'lane') ? (
            <span><i className="map-legend-line map-legend-line--lane" /> Reduced lanes</span>
          ) : null}
          {closureDirections.some(({ restrictionType }) => restrictionType === 'speed') ? (
            <span><i className="map-legend-line map-legend-line--speed" /> Reduced speed</span>
          ) : null}
        </div>
      ) : null}
      {simulationFrame?.tripCoordinate ? (
        <div className="sim-trip-callout" role="status">
          <strong>{simulationFrame.rerouted ? 'Rerouted · traffic-aware path' : 'SIM · your trip'}</strong>
          <span>{simulationFrame.rerouted ? 'Future route updated; blue history is preserved.' : 'Blue is traveled · teal is ahead.'}</span>
        </div>
      ) : null}
      {simulationFrame ? (
        <div className="simulation-map-legend" aria-label="Traffic simulation legend">
          <span>Fast</span><i className="agent-key agent-key--free" />
          <i className="agent-key agent-key--slow" />
          <i className="agent-key agent-key--heavy" /><span>Slow</span>
        </div>
      ) : null}
      {mapError ? <div className="map-error">{mapError}</div> : null}
    </section>
  )
}

function routeFeature(route: RouteSummary | null) {
  if (!route) return EMPTY_ROUTE
  return {
    type: 'FeatureCollection' as const,
    features: [
      {
        type: 'Feature' as const,
        properties: { routeId: route.route_id },
        geometry: route.geometry,
      },
    ],
  }
}

function closureFeature(directions: ClosureMapDirection[]) {
  return {
    type: 'FeatureCollection' as const,
    features: directions.map((direction) => ({
      type: 'Feature' as const,
      properties: {
        edgeId: direction.edge.edge_id,
        direction: direction.direction_label,
        restrictionType: direction.restrictionType,
      },
      geometry: direction.geometry,
    })),
  }
}

function spilloverFeature(edges: DiversionEdgeChange[]) {
  return {
    type: 'FeatureCollection' as const,
    features: edges.map((edge) => ({
      type: 'Feature' as const,
      properties: {
        edgeId: edge.edge_id,
        roadName: edge.road_name,
        changeVph: edge.change_vph,
      },
      geometry: edge.geometry,
    })),
  }
}

function simulationAgentsFeature(frame: SimulationMapFrame | null) {
  return {
    type: 'FeatureCollection' as const,
    features: (frame?.agents ?? []).map((agent) => ({
      type: 'Feature' as const,
      properties: {
        id: agent.id,
        congestion: agent.congestion,
        opacity: agent.opacity,
      },
      geometry: { type: 'Point' as const, coordinates: agent.coordinate },
    })),
  }
}

function simulationRouteFeature(coordinates: [number, number][]) {
  return coordinates.length >= 2
    ? {
        type: 'FeatureCollection' as const,
        features: [{
          type: 'Feature' as const,
          properties: {},
          geometry: { type: 'LineString' as const, coordinates },
        }],
      }
    : EMPTY_ROUTE
}

function simulationTripFeature(coordinate: [number, number] | null) {
  return coordinate
    ? {
        type: 'FeatureCollection' as const,
        features: [{
          type: 'Feature' as const,
          properties: {},
          geometry: { type: 'Point' as const, coordinates: coordinate },
        }],
      }
    : EMPTY_ROUTE
}

function syncRouteSource(
  map: MapLibreMap,
  route: RouteSummary | null,
  sourceId = 'baseline-route',
): boolean {
  const source = map.getSource(sourceId) as GeoJSONSource | undefined
  if (!source) return false
  source.setData(routeFeature(route))
  return true
}

function syncClosureSource(
  map: MapLibreMap,
  directions: ClosureMapDirection[],
): boolean {
  const source = map.getSource('closure-selection') as GeoJSONSource | undefined
  if (!source) return false
  source.setData(closureFeature(directions))
  return true
}

function syncSpilloverSource(
  map: MapLibreMap,
  edges: DiversionEdgeChange[],
): boolean {
  const source = map.getSource('spillover-edges') as GeoJSONSource | undefined
  if (!source) return false
  source.setData(spilloverFeature(edges))
  return true
}

function syncSimulationSources(
  map: MapLibreMap,
  frame: SimulationMapFrame | null,
): boolean {
  const agents = map.getSource('simulation-agents') as GeoJSONSource | undefined
  const traveled = map.getSource('simulation-traveled-route') as GeoJSONSource | undefined
  const projected = map.getSource('simulation-projected-route') as GeoJSONSource | undefined
  const trip = map.getSource('simulation-trip-agent') as GeoJSONSource | undefined
  if (!agents || !traveled || !projected || !trip) return false
  agents.setData(simulationAgentsFeature(frame))
  traveled.setData(simulationRouteFeature(frame?.traveledRoute ?? []))
  projected.setData(simulationRouteFeature(frame?.projectedRoute ?? []))
  trip.setData(simulationTripFeature(frame?.tripCoordinate ?? null))
  return true
}

function fitRoute(map: MapLibreMap, route: RouteSummary | null) {
  if (!route || route.geometry.coordinates.length < 2) return
  const bounds = route.geometry.coordinates.reduce(
    (current, coordinate) => current.extend(coordinate),
    new LngLatBounds(
      route.geometry.coordinates[0],
      route.geometry.coordinates[0],
    ),
  )
  map.fitBounds(bounds, { padding: 72, duration: 650, maxZoom: 14 })
}

function createMarker(
  map: MapLibreMap,
  location: SelectedLocation | null,
  color: string,
  label: string,
): Marker | null {
  if (!location) return null
  const marker = new Marker({ color })
    .setLngLat([location.lng, location.lat])
    .addTo(map)
  marker.getElement().setAttribute('aria-label', `${label}: ${location.label}`)
  marker.getElement().setAttribute('title', `${label}: ${location.label}`)
  return marker
}
