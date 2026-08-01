import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Route, ShieldCheck } from 'lucide-react'
import {
  compareRoute,
  createGoogleMapsUrl,
  findAlternativeRoutes,
  selectClosureRoad,
  selectRoad,
  type ClosureComparisonInput,
  type ClosureSectionDraft,
  type CoordinateInput,
  type RoadRestriction,
  type RestrictionSchedule,
  type SelectedLocation,
} from './api/routing'
import { getStatus } from './api/system'
import {
  archiveScenario,
  createScenario,
  duplicateScenario,
  exportScenario,
  getScenario,
  importScenario,
  listScenarios,
  updateScenario,
  type ScenarioContent,
  type ScenarioMapState,
  type ScenarioRecord,
} from './api/scenarios'
import { ClosurePanel } from './features/closures/ClosurePanel'
import { TripPanel } from './features/locations/TripPanel'
import { RouteResultCard } from './features/results/RouteResultCard'
import { RouteAlternativesPanel } from './features/results/RouteAlternativesPanel'
import { ReliabilityPanel } from './features/traffic/ReliabilityPanel'
import type { ReliabilitySettings } from './api/traffic'
import {
  DEFAULT_DIVERSION_SETTINGS,
  type DiversionResult,
  type DiversionSettings,
} from './api/diversion'
import { DiversionPanel } from './features/diversion/DiversionPanel'
import { DraftRecovery, ScenarioPanel } from './features/scenarios/ScenarioPanel'
import {
  clearPlannerDraft,
  readPlannerDraft,
  writePlannerDraft,
  type BrowserDraft,
} from './stores/draftDatabase'
import {
  useTripStore,
  type SelectionMode,
} from './stores/tripStore'
import './App.css'

const TripMap = lazy(() => import('./features/map/TripMap'))

const WORKFLOW_STEPS = ['Trip', 'Closures', 'Conditions', 'Run', 'Compare', 'Save']

type SelectionVariables = {
  mode: Exclude<SelectionMode, null>
  coordinate: CoordinateInput
}

type RouteVariables = {
  origin: SelectedLocation
  destination: SelectedLocation
  closure?: ClosureComparisonInput
}

type ScenarioAction =
  | { type: 'save'; name: string; content: ScenarioContent; current: ScenarioRecord | null }
  | { type: 'duplicate'; id: string }
  | { type: 'import'; value: unknown }

function App() {
  const queryClient = useQueryClient()
  const [closurePicking, setClosurePicking] = useState(false)
  const [closureSections, setClosureSections] = useState<ClosureSectionDraft[]>([])
  const [selectedClosureEdgeIds, setSelectedClosureEdgeIds] = useState<string[]>([])
  const [lastComparisonKey, setLastComparisonKey] = useState('')
  const [lastAlternativesKey, setLastAlternativesKey] = useState('')
  const [selectedAlternativeRouteId, setSelectedAlternativeRouteId] = useState<string | null>(null)
  const [departureTime, setDepartureTime] = useState(defaultDepartureTime)
  const [reliabilitySettings, setReliabilitySettings] = useState<ReliabilitySettings>(
    () => defaultReliabilitySettings(defaultDepartureTime()),
  )
  const [diversionSnapshot, setDiversionSnapshot] = useState<{
    comparisonKey: string
    result: DiversionResult
  } | null>(null)
  const [diversionSettings, setDiversionSettings] = useState<DiversionSettings>(
    DEFAULT_DIVERSION_SETTINGS,
  )
  const [scenarioName, setScenarioName] = useState('')
  const [currentScenario, setCurrentScenario] = useState<ScenarioRecord | null>(null)
  const [pendingDraft, setPendingDraft] = useState<BrowserDraft | null>(null)
  const [mapState, setMapState] = useState<ScenarioMapState | null>(null)
  const [draftInitialized, setDraftInitialized] = useState(false)
  const lastScenarioRestoreAttempted = useRef(false)
  const origin = useTripStore((state) => state.origin)
  const destination = useTripStore((state) => state.destination)
  const selectionMode = useTripStore((state) => state.selectionMode)
  const setSelectionMode = useTripStore((state) => state.setSelectionMode)
  const setLocation = useTripStore((state) => state.setLocation)
  const clearLocation = useTripStore((state) => state.clearLocation)
  const restoreTrip = useTripStore((state) => state.restoreTrip)
  const replaceTrip = useTripStore((state) => state.replaceTrip)

  const system = useQuery({
    queryKey: ['system', 'status'],
    queryFn: ({ signal }) => getStatus(signal),
    refetchOnWindowFocus: false,
  })

  const scenariosQuery = useQuery({
    queryKey: ['scenarios'],
    queryFn: ({ signal }) => listScenarios(signal),
    refetchOnWindowFocus: true,
  })

  const routeMutation = useMutation({
    mutationFn: ({ origin: routeOrigin, destination: routeDestination, closure }: RouteVariables) =>
      compareRoute(routeOrigin, routeDestination, closure),
    onSuccess: (_response, variables) => {
      setLastComparisonKey(closureComparisonKey(variables.closure))
    },
  })

  const alternativesMutation = useMutation({
    mutationFn: ({ origin: routeOrigin, destination: routeDestination, closure }: RouteVariables) =>
      findAlternativeRoutes(routeOrigin, routeDestination, closure),
    onSuccess: (response, variables) => {
      setLastAlternativesKey(closureComparisonKey(variables.closure))
      setSelectedAlternativeRouteId(response.routes[0]?.route.route_id ?? null)
    },
  })

  const scenarioActionMutation = useMutation({
    mutationFn: (action: ScenarioAction) => {
      if (action.type === 'duplicate') return duplicateScenario(action.id)
      if (action.type === 'import') return importScenario(action.value)
      return action.current
        ? updateScenario(action.current, action.name, action.content)
        : createScenario(action.name, action.content)
    },
    onSuccess: (scenario) => {
      restoreScenario(scenario)
      localStorage.setItem('commute-help-last-scenario', scenario.id)
      void clearPlannerDraft()
      void queryClient.invalidateQueries({ queryKey: ['scenarios'] })
    },
  })

  const scenarioLoadMutation = useMutation({
    mutationFn: (id: string) => getScenario(id),
    onSuccess: (scenario) => {
      restoreScenario(scenario)
      localStorage.setItem('commute-help-last-scenario', scenario.id)
    },
  })

  const scenarioArchiveMutation = useMutation({
    mutationFn: (id: string) => archiveScenario(id),
    onSuccess: () => {
      setCurrentScenario(null)
      setScenarioName('')
      localStorage.removeItem('commute-help-last-scenario')
      void queryClient.invalidateQueries({ queryKey: ['scenarios'] })
    },
  })

  const selectionMutation = useMutation({
    mutationFn: ({ coordinate }: SelectionVariables) => selectRoad(coordinate),
    onSuccess: (response, variables) => {
      setLocation(variables.mode, response.location)
      routeMutation.reset()
      alternativesMutation.reset()
      setSelectedAlternativeRouteId(null)
    },
  })
  const selectionPending = selectionMutation.isPending
  const mutateSelection = selectionMutation.mutate

  const pickCoordinate = useCallback(
    (coordinate: CoordinateInput) => {
      if (!selectionMode || selectionPending) return
      mutateSelection({ mode: selectionMode, coordinate })
    },
    [mutateSelection, selectionMode, selectionPending],
  )

  const closureSelectionMutation = useMutation({
    mutationFn: (coordinate: CoordinateInput) => selectClosureRoad(coordinate),
    onSuccess: (response) => {
      setClosureSections((current) => {
        const alreadySelected = current.some((section) =>
          section.selection.directions.some(
            (direction) => direction.edge.edge_id === response.selected_edge_id,
          ),
        )
        return alreadySelected
          ? current
          : [
              ...current,
              {
                selection: response,
                restriction: { type: 'full' },
                schedule: { type: 'always' },
              },
            ]
      })
      setSelectedClosureEdgeIds((current) => [
        ...new Set([
          ...current,
          ...response.directions.map((direction) => direction.edge.edge_id),
        ]),
      ])
      setClosurePicking(true)
    },
  })
  const mutateClosureSelection = closureSelectionMutation.mutate

  const currentScenarioContent = buildScenarioContent()
  const scenarioDirty = currentScenario
    ? scenarioName.trim() !== currentScenario.name ||
      scenarioContentKey(
        currentScenarioContent,
        currentScenario.content.map_state === null,
      ) !==
        scenarioContentKey(
          currentScenario.content,
          currentScenario.content.map_state === null,
        )
    : origin !== null || destination !== null || closureSections.length > 0

  useEffect(() => {
    let active = true
    void readPlannerDraft().then((draft) => {
      if (!active) return
      setPendingDraft(draft ?? null)
      setDraftInitialized(true)
    })
    return () => {
      active = false
    }
  }, [])

  useEffect(() => {
    if (
      !draftInitialized ||
      pendingDraft !== null ||
      lastScenarioRestoreAttempted.current
    ) {
      return
    }
    lastScenarioRestoreAttempted.current = true
    const lastScenarioId = localStorage.getItem('commute-help-last-scenario')
    if (lastScenarioId) scenarioLoadMutation.mutate(lastScenarioId)
  }, [
    draftInitialized,
    pendingDraft,
    scenarioLoadMutation,
  ])

  useEffect(() => {
    if (!draftInitialized || pendingDraft !== null) return
    const timer = window.setTimeout(() => {
      if (!scenarioDirty) {
        void clearPlannerDraft()
        return
      }
      void writePlannerDraft({
        name: scenarioName,
        scenarioId: currentScenario?.id ?? null,
        scenarioRevision: currentScenario?.revision ?? null,
        origin,
        destination,
        departureTime,
        closureSections,
        selectedClosureEdgeIds,
        selectedRouteId: selectedAlternativeRouteId,
        mapState,
        reliabilitySettings,
        diversionSettings,
      })
    }, 500)
    return () => window.clearTimeout(timer)
  }, [
    closureSections,
    currentScenario,
    departureTime,
    destination,
    draftInitialized,
    origin,
    pendingDraft,
    scenarioDirty,
    scenarioName,
    selectedAlternativeRouteId,
    selectedClosureEdgeIds,
    mapState,
    reliabilitySettings,
    diversionSettings,
  ])

  const pickClosureCoordinate = useCallback(
    (coordinate: CoordinateInput) => {
      if (!closurePicking || closureSelectionMutation.isPending) return
      mutateClosureSelection(coordinate)
    },
    [closurePicking, closureSelectionMutation.isPending, mutateClosureSelection],
  )

  function calculateRoute() {
    if (!origin || !destination) return
    clearClosure()
    alternativesMutation.reset()
    setSelectedAlternativeRouteId(null)
    routeMutation.mutate({ origin, destination })
  }

  function compareClosureRoute() {
    if (!origin || !destination) return
    const closure = buildClosureComparison()
    if (!closure) return
    alternativesMutation.reset()
    setSelectedAlternativeRouteId(null)
    routeMutation.mutate({
      origin,
      destination,
      closure,
    })
  }

  function findAlternatives() {
    if (!origin || !destination) return
    const closure = closureSections.length ? buildClosureComparison() : undefined
    if (closureSections.length && !closure) return
    alternativesMutation.mutate({ origin, destination, closure })
  }

  function buildClosureComparison(): ClosureComparisonInput | undefined {
    if (closureSections.length === 0) return undefined
    const sections = closureSections
      .map((section) => ({
        restriction: section.restriction,
        schedule: section.schedule,
        directions: section.selection.directions.filter((direction) =>
          selectedClosureEdgeIds.includes(direction.edge.edge_id),
        ),
      }))
      .filter((section) => section.directions.length > 0)
    if (
      sections.length === 0 ||
      !departureTime ||
      sections.some(
        ({ schedule }) =>
          schedule.type === 'scheduled' &&
          (!schedule.starts_at ||
            !schedule.ends_at ||
            new Date(schedule.ends_at) <= new Date(schedule.starts_at)),
      )
    ) {
      return undefined
    }
    return {
      graphVersion: closureSections[0].selection.graph_version,
      departureTime: new Date(departureTime).toISOString(),
      sections,
    }
  }

  function buildScenarioContent(): ScenarioContent | undefined {
    if (!origin || !destination || !departureTime) return undefined
    const departure = new Date(departureTime)
    const arrivalDeadline = reliabilitySettings.arrivalDeadline
      ? new Date(reliabilitySettings.arrivalDeadline)
      : null
    if (
      Number.isNaN(departure.getTime()) ||
      (reliabilitySettings.planningMode === 'arrive_by' &&
        (!arrivalDeadline ||
          Number.isNaN(arrivalDeadline.getTime()) ||
          arrivalDeadline <= departure))
    ) return undefined
    const graphVersion =
      closureSections[0]?.selection.graph_version ?? system.data?.graph.version
    if (!graphVersion) return undefined
    return {
      schema_version: 1,
      graph_version: graphVersion,
      origin,
      destination,
      departure_time: departure.toISOString(),
      closures: closureSections
        .map((section) => ({
          selection: section.selection,
          selected_edge_ids: section.selection.directions
            .map((direction) => direction.edge.edge_id)
            .filter((edgeId) => selectedClosureEdgeIds.includes(edgeId)),
          restriction: {
            ...section.restriction,
            ...(section.schedule.type === 'scheduled'
              ? {
                  starts_at: new Date(section.schedule.starts_at).toISOString(),
                  ends_at: new Date(section.schedule.ends_at).toISOString(),
                }
              : {}),
          },
        }))
        .filter((section) => section.selected_edge_ids.length > 0),
      selected_route_id: selectedAlternativeRouteId,
      map_state: mapState,
      reliability_conditions: {
        planning_mode: reliabilitySettings.planningMode,
        arrival_deadline:
          reliabilitySettings.planningMode === 'arrive_by' && arrivalDeadline
            ? arrivalDeadline.toISOString()
            : null,
        buffer_minutes: reliabilitySettings.bufferMinutes,
        confidence_target: reliabilitySettings.confidenceTarget,
        sample_count: reliabilitySettings.sampleCount,
        profile_id: reliabilitySettings.profileId,
      },
      diversion_conditions: {
        demand_vph: diversionSettings.demandVph,
        demand_pair_count: diversionSettings.demandPairCount,
        iterations: diversionSettings.iterations,
        dispersion_radius_m: diversionSettings.dispersionRadiusM,
      },
    }
  }

  function restoreScenario(scenario: ScenarioRecord) {
    const content = scenario.content
    const workflow = scenarioWorkflow(content)
    restoreTrip(content.origin, content.destination)
    setCurrentScenario(scenario)
    setScenarioName(scenario.name)
    setDepartureTime(localDateTimeValue(new Date(content.departure_time)))
    setClosureSections(
      scenario.graph_status === 'review_required' ? [] : workflow.sections,
    )
    setSelectedClosureEdgeIds(
      scenario.graph_status === 'review_required' ? [] : workflow.selectedEdgeIds,
    )
    setSelectedAlternativeRouteId(content.selected_route_id)
    setMapState(content.map_state)
    setReliabilitySettings(
      content.reliability_conditions
        ? {
            planningMode: content.reliability_conditions.planning_mode,
            arrivalDeadline: content.reliability_conditions.arrival_deadline
              ? localDateTimeValue(
                  new Date(content.reliability_conditions.arrival_deadline),
                )
              : defaultReliabilitySettings(
                  localDateTimeValue(new Date(content.departure_time)),
                ).arrivalDeadline,
            bufferMinutes: content.reliability_conditions.buffer_minutes,
            confidenceTarget: content.reliability_conditions.confidence_target,
            sampleCount: content.reliability_conditions.sample_count,
            profileId: content.reliability_conditions.profile_id,
          }
        : defaultReliabilitySettings(localDateTimeValue(new Date(content.departure_time))),
    )
    setDiversionSettings(
      content.diversion_conditions
        ? {
            demandVph: content.diversion_conditions.demand_vph,
            demandPairCount: content.diversion_conditions.demand_pair_count,
            iterations: content.diversion_conditions.iterations,
            dispersionRadiusM: content.diversion_conditions.dispersion_radius_m,
          }
        : DEFAULT_DIVERSION_SETTINGS,
    )
    alternativesMutation.reset()
    routeMutation.reset()
    if (scenario.graph_status !== 'review_required') {
      routeMutation.mutate({
        origin: content.origin,
        destination: content.destination,
        closure: workflow.comparison,
      })
    }
  }

  function saveCurrentScenario() {
    const content = buildScenarioContent()
    if (!content || !scenarioName.trim()) return
    scenarioActionMutation.mutate({
      type: 'save',
      name: scenarioName,
      content,
      current: currentScenario,
    })
  }

  function loadSavedScenario(id: string) {
    if (scenarioDirty && !window.confirm('Discard unsaved changes and load this scenario?')) {
      return
    }
    scenarioLoadMutation.mutate(id)
  }

  function duplicateCurrentScenario() {
    if (!currentScenario) return
    scenarioActionMutation.mutate({ type: 'duplicate', id: currentScenario.id })
  }

  function archiveCurrentScenario() {
    if (!currentScenario) return
    if (!window.confirm(`Archive “${currentScenario.name}”?`)) return
    scenarioArchiveMutation.mutate(currentScenario.id)
  }

  async function exportCurrentScenario() {
    if (!currentScenario) return
    try {
      const exported = await exportScenario(currentScenario.id)
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(exported, null, 2)], {
          type: 'application/json',
        }),
      )
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `commute-help-${currentScenario.id}.json`
      anchor.click()
      URL.revokeObjectURL(url)
    } catch {
      // The panel surfaces API mutation errors; export remains a direct download.
    }
  }

  function recoverDraft() {
    if (!pendingDraft) return
    const draft = pendingDraft
    replaceTrip(draft.origin, draft.destination)
    setScenarioName(draft.name)
    setCurrentScenario(null)
    setDepartureTime(draft.departureTime)
    setClosureSections(draft.closureSections)
    setSelectedClosureEdgeIds(draft.selectedClosureEdgeIds)
    setSelectedAlternativeRouteId(draft.selectedRouteId)
    setMapState(draft.mapState ?? null)
    setReliabilitySettings(
      normalizeReliabilitySettings(
        draft.reliabilitySettings,
        draft.departureTime,
      ),
    )
    setDiversionSettings(
      draft.diversionSettings ?? DEFAULT_DIVERSION_SETTINGS,
    )
    routeMutation.reset()
    alternativesMutation.reset()
    setPendingDraft(null)
    lastScenarioRestoreAttempted.current = true
    if (draft.scenarioId && draft.scenarioRevision) {
      void getScenario(draft.scenarioId).then((scenario) => {
        if (scenario.revision === draft.scenarioRevision) {
          setCurrentScenario(scenario)
        }
      })
    }
  }

  function discardDraft() {
    void clearPlannerDraft()
    setPendingDraft(null)
  }

  function startClosurePicking() {
    selectionMutation.reset()
    closureSelectionMutation.reset()
    setSelectionMode(null)
    setClosurePicking(true)
  }

  function stopClosurePicking() {
    setClosurePicking(false)
  }

  function toggleClosureDirection(edgeId: string) {
    setSelectedClosureEdgeIds((current) =>
      current.includes(edgeId)
        ? current.filter((candidate) => candidate !== edgeId)
        : [...current, edgeId],
    )
  }

  function clearClosure() {
    setClosurePicking(false)
    setClosureSections([])
    setSelectedClosureEdgeIds([])
    closureSelectionMutation.reset()
  }

  function removeClosureSection(sectionIndex: number) {
    const removed = closureSections[sectionIndex]
    if (!removed) return
    const removedEdgeIds = new Set(
      removed.selection.directions.map((direction) => direction.edge.edge_id),
    )
    setClosureSections((current) =>
      current.filter((_, index) => index !== sectionIndex),
    )
    setSelectedClosureEdgeIds((current) =>
      current.filter((edgeId) => !removedEdgeIds.has(edgeId)),
    )
  }

  function selectAllSectionDirections(sectionIndex: number) {
    const section = closureSections[sectionIndex]
    if (!section) return
    setSelectedClosureEdgeIds((current) => [
      ...new Set([
        ...current,
        ...section.selection.directions.map((direction) => direction.edge.edge_id),
      ]),
    ])
  }

  function updateSectionRestriction(
    sectionIndex: number,
    restriction: RoadRestriction,
  ) {
    setClosureSections((current) =>
      current.map((section, index) =>
        index === sectionIndex ? { ...section, restriction } : section,
      ),
    )
  }

  function updateSectionSchedule(
    sectionIndex: number,
    schedule: RestrictionSchedule,
  ) {
    setClosureSections((current) =>
      current.map((section, index) =>
        index === sectionIndex ? { ...section, schedule } : section,
      ),
    )
  }

  function chooseSelectionMode(mode: Exclude<SelectionMode, null>) {
    clearClosure()
    selectionMutation.reset()
    setSelectionMode(mode)
  }

  function clearSelection(mode: Exclude<SelectionMode, null>) {
    clearClosure()
    routeMutation.reset()
    selectionMutation.reset()
    alternativesMutation.reset()
    setSelectedAlternativeRouteId(null)
    clearLocation(mode)
  }

  const workflowError =
    selectionMutation.error?.message ??
    (closureSections.length === 0 ? routeMutation.error?.message : null) ??
    null
  const closureError =
    closureSelectionMutation.error?.message ??
    (closureSections.length > 0 ? routeMutation.error?.message : null) ??
    null
  const graphReady = system.data?.graph.status === 'ready'
  const currentClosureComparison = buildClosureComparison()
  const currentClosureKey = closureComparisonKey(currentClosureComparison)
  const comparisonIsCurrent =
    routeMutation.data !== undefined &&
    lastComparisonKey === currentClosureKey
  const displayedResult = routeMutation.data
    ? comparisonIsCurrent
      ? routeMutation.data
      : {
          ...routeMutation.data,
          evidence_level: 'free_flow' as const,
          scenario: null,
          scenario_status: 'not_requested' as const,
          applied_restriction_edge_ids: [],
          inactive_restriction_edge_ids: [],
        }
    : undefined
  const alternativesAreCurrent =
    alternativesMutation.data !== undefined &&
    comparisonIsCurrent &&
    lastAlternativesKey === currentClosureKey
  const alternatives = alternativesAreCurrent
    ? alternativesMutation.data.routes
    : []
  const activeStep = currentScenario
    ? 5
    : alternatives.length > 0
      ? 4
      : routeMutation.data
        ? closureSections.length > 0
          ? 2
          : 1
        : 0
  const selectedAlternative = alternatives.find(
    (candidate) => candidate.route.route_id === selectedAlternativeRouteId,
  )
  const selectedNavigationRoute =
    selectedAlternative?.route ?? displayedResult?.scenario ?? displayedResult?.baseline
  const currentDiversionResult =
    diversionSnapshot?.comparisonKey === currentClosureKey
      ? diversionSnapshot.result
      : null
  const googleHandoff = useQuery({
    queryKey: [
      'google-maps-url',
      origin?.node_id,
      destination?.node_id,
      selectedNavigationRoute?.route_id,
    ],
    queryFn: ({ signal }) => {
      if (!origin || !destination || !selectedNavigationRoute) {
        throw new Error('Choose a route before preparing Google Maps.')
      }
      return createGoogleMapsUrl(
        origin,
        destination,
        selectedNavigationRoute,
        signal,
      )
    },
    enabled:
      alternatives.length > 0 &&
      origin !== null &&
      destination !== null &&
      selectedNavigationRoute !== undefined,
    refetchOnWindowFocus: false,
  })

  return (
    <div className="app-shell">
      <header className="app-header">
        <a className="brand" href="/" aria-label="Commute Help home">
          <span className="brand-mark" aria-hidden="true">
            <Route size={20} strokeWidth={2.4} />
          </span>
          <span>
            Commute Help
            <small>Plan around the road ahead</small>
          </span>
        </a>

        <nav className="workflow-nav" aria-label="Planning steps">
          {WORKFLOW_STEPS.map((step, index) => (
            <span
              className={
                index === activeStep
                  ? 'workflow-step workflow-step--active'
                  : index < activeStep
                    ? 'workflow-step workflow-step--complete'
                    : 'workflow-step'
              }
              key={step}
              aria-current={index === activeStep ? 'step' : undefined}
            >
              <span>{index + 1}</span>
              {step}
            </span>
          ))}
        </nav>

        <span
          className={`graph-badge ${graphReady ? 'graph-badge--ready' : ''}`}
          title={system.data?.graph.message ?? undefined}
        >
          <ShieldCheck size={15} aria-hidden="true" />
          {system.isPending
            ? 'Connecting'
            : graphReady
              ? 'Local graph ready'
              : 'Graph unavailable'}
        </span>
      </header>

      {pendingDraft ? (
        <DraftRecovery
          updatedAt={pendingDraft.updatedAt}
          onRecover={recoverDraft}
          onDiscard={discardDraft}
        />
      ) : null}

      <main className="planner-layout">
        <aside className="planner-sidebar">
          <TripPanel
            origin={origin}
            destination={destination}
            selectionMode={selectionMode}
            selectionPending={selectionMutation.isPending}
            routePending={routeMutation.isPending}
            error={workflowError}
            onSelectMode={chooseSelectionMode}
            onClear={clearSelection}
            onCoordinateSubmit={pickCoordinate}
            onCalculate={calculateRoute}
          />
          {routeMutation.data ? (
            <ClosurePanel
              sections={closureSections}
              selectedEdgeIds={selectedClosureEdgeIds}
              picking={closurePicking}
              selecting={closureSelectionMutation.isPending}
              comparing={routeMutation.isPending}
              error={closureError}
              departureTime={departureTime}
              onDepartureTimeChange={setDepartureTime}
              onStartPicking={startClosurePicking}
              onStopPicking={stopClosurePicking}
              onToggleDirection={toggleClosureDirection}
              onSelectAllDirections={selectAllSectionDirections}
              onRestrictionChange={updateSectionRestriction}
              onScheduleChange={updateSectionSchedule}
              onRemoveSection={removeClosureSection}
              onClearAll={clearClosure}
              onCompare={compareClosureRoute}
              scenarioName={scenarioName}
              currentScenarioRevision={currentScenario?.revision ?? null}
              scenarioDirty={scenarioDirty}
              canSave={currentScenarioContent !== undefined}
              saving={scenarioActionMutation.isPending}
              onScenarioNameChange={setScenarioName}
              onSaveScenario={saveCurrentScenario}
            />
          ) : null}
          <ScenarioPanel
              scenarios={scenariosQuery.data ?? []}
              current={currentScenario}
              name={scenarioName}
              dirty={scenarioDirty}
              canSave={currentScenarioContent !== undefined}
              busy={
                scenarioActionMutation.isPending ||
                scenarioLoadMutation.isPending ||
                scenarioArchiveMutation.isPending
              }
              error={
                formatScenarioError(
                  scenarioActionMutation.error ??
                    scenarioLoadMutation.error ??
                    scenarioArchiveMutation.error ??
                    scenariosQuery.error,
                )
              }
              onNameChange={setScenarioName}
              onSave={saveCurrentScenario}
              onLoad={loadSavedScenario}
              onDuplicate={duplicateCurrentScenario}
              onArchive={archiveCurrentScenario}
              onExport={() => void exportCurrentScenario()}
              onImport={(value) =>
                scenarioActionMutation.mutate({ type: 'import', value })
              }
            />
        </aside>

        <div className="map-column">
          <Suspense fallback={<div className="map-loading">Loading local trip map…</div>}>
            <TripMap
              origin={origin}
              destination={destination}
              route={displayedResult?.baseline ?? null}
              scenarioRoute={selectedAlternative?.route ?? displayedResult?.scenario ?? null}
              closureDirections={
                closureSections.flatMap((section) =>
                  section.selection.directions.filter((direction) =>
                    selectedClosureEdgeIds.includes(direction.edge.edge_id),
                  ),
                )
              }
              spilloverEdges={currentDiversionResult?.edge_changes ?? []}
              selectionMode={selectionMode}
              selectionPending={selectionMutation.isPending}
              closurePicking={closurePicking}
              closureSelectionPending={closureSelectionMutation.isPending}
              onCoordinatePick={pickCoordinate}
              onClosurePick={pickClosureCoordinate}
              mapState={mapState}
              onMapStateChange={setMapState}
            />
          </Suspense>
          {displayedResult ? (
            <>
              <RouteResultCard result={displayedResult} />
              {selectedNavigationRoute ? (
                <ReliabilityPanel
                  key={selectedNavigationRoute.route_id}
                  route={selectedNavigationRoute}
                  departureTime={departureTime}
                  settings={reliabilitySettings}
                  onSettingsChange={setReliabilitySettings}
                  onDepartureTimeChange={setDepartureTime}
                />
              ) : null}
              <RouteAlternativesPanel
                alternatives={alternatives}
                selectedRouteId={selectedAlternativeRouteId}
                loading={alternativesMutation.isPending}
                error={alternativesMutation.error?.message ?? null}
                handoff={googleHandoff.data}
                handoffLoading={googleHandoff.isPending && googleHandoff.fetchStatus === 'fetching'}
                handoffError={googleHandoff.error?.message ?? null}
                onFind={findAlternatives}
                onSelect={setSelectedAlternativeRouteId}
              />
              {comparisonIsCurrent &&
              currentClosureComparison &&
              routeMutation.data.applied_restriction_edge_ids.length > 0 &&
              origin &&
              destination ? (
                <DiversionPanel
                  key={currentClosureKey}
                  origin={origin}
                  destination={destination}
                  closure={currentClosureComparison}
                  settings={diversionSettings}
                  onSettingsChange={setDiversionSettings}
                  onResult={(result) => {
                    if (result) {
                      setDiversionSnapshot({
                        comparisonKey: currentClosureKey,
                        result,
                      })
                    } else {
                      setDiversionSnapshot(null)
                    }
                  }}
                />
              ) : null}
            </>
          ) : (
            <div className="route-placeholder">
              <Route size={20} aria-hidden="true" />
              Your normal route and honest free-flow estimate will appear here.
            </div>
          )}
        </div>
      </main>
    </div>
  )
}

export default App

function scenarioWorkflow(content: ScenarioContent): {
  sections: ClosureSectionDraft[]
  selectedEdgeIds: string[]
  comparison: ClosureComparisonInput | undefined
} {
  const sections = content.closures.map((saved) => ({
    selection: saved.selection,
    restriction: restrictionForWorkflow(saved.restriction),
    schedule:
      saved.restriction.starts_at && saved.restriction.ends_at
        ? {
            type: 'scheduled' as const,
            starts_at: localDateTimeValue(new Date(saved.restriction.starts_at)),
            ends_at: localDateTimeValue(new Date(saved.restriction.ends_at)),
          }
        : { type: 'always' as const },
  }))
  const selectedEdgeIds = content.closures.flatMap(
    (section) => section.selected_edge_ids,
  )
  const comparison = sections.length
    ? {
        graphVersion: content.graph_version,
        departureTime: content.departure_time,
        sections: sections.map((section, index) => ({
          directions: section.selection.directions.filter((direction) =>
            content.closures[index].selected_edge_ids.includes(
              direction.edge.edge_id,
            ),
          ),
          restriction: section.restriction,
          schedule: section.schedule,
        })),
      }
    : undefined
  return { sections, selectedEdgeIds, comparison }
}

function restrictionForWorkflow(
  restriction: ScenarioContent['closures'][number]['restriction'],
): RoadRestriction {
  if (restriction.type === 'lane') {
    return { type: 'lane', remaining_lanes: restriction.remaining_lanes }
  }
  if (restriction.type === 'speed') {
    return { type: 'speed', speed_limit_kph: restriction.speed_limit_kph }
  }
  return { type: 'full' }
}

function formatScenarioError(error: Error | null): string | null {
  if (!error) return null
  if (error.name === 'ZodError') {
    return 'That file is not a valid Commute Help scenario export.'
  }
  return error.message
}

function scenarioContentKey(
  content: ScenarioContent | undefined,
  ignoreMapState = false,
): string {
  if (!content) return ''
  return JSON.stringify({
    ...content,
    map_state: ignoreMapState ? null : content.map_state,
    departure_time: new Date(content.departure_time).getTime(),
    reliability_conditions: content.reliability_conditions
      ? {
          ...content.reliability_conditions,
          arrival_deadline: content.reliability_conditions.arrival_deadline
            ? new Date(
                content.reliability_conditions.arrival_deadline,
              ).getTime()
            : null,
        }
      : null,
    closures: content.closures.map((section) => ({
      ...section,
      restriction: {
        ...section.restriction,
        starts_at: section.restriction.starts_at
          ? new Date(section.restriction.starts_at).getTime()
          : null,
        ends_at: section.restriction.ends_at
          ? new Date(section.restriction.ends_at).getTime()
          : null,
      },
    })),
  })
}

function closureComparisonKey(closure?: ClosureComparisonInput): string {
  if (!closure) return ''
  return JSON.stringify(
    {
      departureTime: closure.departureTime,
      restrictions: closure.sections
        .flatMap(({ directions, restriction, schedule }) =>
          directions.map((direction) => ({
            edgeId: direction.edge.edge_id,
            restriction,
            schedule,
          })),
        )
        .sort((first, second) => first.edgeId.localeCompare(second.edgeId)),
    },
  )
}

function defaultDepartureTime(): string {
  const departure = new Date()
  departure.setMinutes(Math.ceil(departure.getMinutes() / 15) * 15, 0, 0)
  return localDateTimeValue(departure)
}

function defaultReliabilitySettings(departureTime: string): ReliabilitySettings {
  const deadline = new Date(departureTime)
  deadline.setMinutes(deadline.getMinutes() + 45)
  return {
    planningMode: 'arrive_by',
    arrivalDeadline: localDateTimeValue(deadline),
    bufferMinutes: 8,
    confidenceTarget: 0.9,
    sampleCount: 1000,
    profileId: null,
  }
}

function normalizeReliabilitySettings(
  settings: ReliabilitySettings | undefined,
  departureTime: string,
): ReliabilitySettings {
  const defaults = defaultReliabilitySettings(departureTime)
  if (!settings) return defaults
  return {
    ...defaults,
    ...settings,
    planningMode: settings.planningMode ?? 'arrive_by',
  }
}

function localDateTimeValue(value: Date): string {
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(
    value.getDate(),
  )}T${pad(value.getHours())}:${pad(value.getMinutes())}`
}
