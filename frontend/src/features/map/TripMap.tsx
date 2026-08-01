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
  RouteSummary,
  SelectedLocation,
} from '../../api/routing'
import type { SelectionMode } from '../../stores/tripStore'
import type { ScenarioMapState } from '../../api/scenarios'
import type { DiversionEdgeChange } from '../../api/diversion'
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
  closureDirections: ClosureDirectionCandidate[]
  spilloverEdges: DiversionEdgeChange[]
  selectionMode: SelectionMode
  selectionPending: boolean
  closurePicking: boolean
  closureSelectionPending: boolean
  onCoordinatePick: (coordinate: { lat: number; lng: number }) => void
  onClosurePick: (coordinate: { lat: number; lng: number }) => void
  mapState: ScenarioMapState | null
  onMapStateChange: (state: ScenarioMapState) => void
}

export default function TripMap({
  origin,
  destination,
  route,
  scenarioRoute,
  closureDirections,
  spilloverEdges,
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
        id: 'closure-selection-line',
        type: 'line',
        source: 'closure-selection',
        paint: {
          'line-color': '#c3462d',
          'line-width': 11,
          'line-opacity': 0.9,
        },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
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

function closureFeature(directions: ClosureDirectionCandidate[]) {
  return {
    type: 'FeatureCollection' as const,
    features: directions.map((direction) => ({
      type: 'Feature' as const,
      properties: {
        edgeId: direction.edge.edge_id,
        direction: direction.direction_label,
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
  directions: ClosureDirectionCandidate[],
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
