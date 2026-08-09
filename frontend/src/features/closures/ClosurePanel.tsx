import {
  CalendarClock,
  Check,
  Crosshair,
  ExternalLink,
  LoaderCircle,
  MapPinned,
  Save,
  SlidersHorizontal,
  Trash2,
  TriangleAlert,
} from 'lucide-react'
import type { ClosurePreset } from '../../api/closurePresets'
import type {
  ClosureRoadSelectionResponse,
  ClosureSectionDraft,
  RoadRestriction,
  RestrictionSchedule,
} from '../../api/routing'

type ClosurePanelProps = {
  presets: ClosurePreset[]
  presetsLoading: boolean
  presetsError: string | null
  onApplyPreset: (preset: ClosurePreset) => void
  sections: ClosureSectionDraft[]
  selectedEdgeIds: string[]
  picking: boolean
  selecting: boolean
  comparing: boolean
  error: string | null
  departureTime: string
  onDepartureTimeChange: (value: string) => void
  onStartPicking: () => void
  onStopPicking: () => void
  onToggleDirection: (edgeId: string) => void
  onSelectAllDirections: (sectionIndex: number) => void
  onRestrictionChange: (
    sectionIndex: number,
    restriction: RoadRestriction,
  ) => void
  onScheduleChange: (
    sectionIndex: number,
    schedule: RestrictionSchedule,
  ) => void
  onRemoveSection: (sectionIndex: number) => void
  onClearAll: () => void
  onCompare: () => void
  scenarioName: string
  currentScenarioRevision: number | null
  scenarioDirty: boolean
  canSave: boolean
  saving: boolean
  saveError: string | null
  onScenarioNameChange: (value: string) => void
  onSaveScenario: () => void
}

export function ClosurePanel({
  presets,
  presetsLoading,
  presetsError,
  onApplyPreset,
  sections,
  selectedEdgeIds,
  picking,
  selecting,
  comparing,
  error,
  departureTime,
  onDepartureTimeChange,
  onStartPicking,
  onStopPicking,
  onToggleDirection,
  onSelectAllDirections,
  onRestrictionChange,
  onScheduleChange,
  onRemoveSection,
  onClearAll,
  onCompare,
  scenarioName,
  currentScenarioRevision,
  scenarioDirty,
  canSave,
  saving,
  saveError,
  onScenarioNameChange,
  onSaveScenario,
}: ClosurePanelProps) {
  const invalidSchedule = sections.some(
    ({ schedule }) =>
      schedule.type === 'scheduled' &&
      (!schedule.starts_at ||
        !schedule.ends_at ||
        new Date(schedule.ends_at) <= new Date(schedule.starts_at)),
  )

  return (
    <section className="closure-panel" aria-labelledby="closure-step-title">
      <div className="step-kicker">Step 2 of 6</div>
      <h2 id="closure-step-title">Add a road closure</h2>
      <p className="panel-intro">
        Pick as many road sections as needed, choose the affected direction,
        then define a full closure, lane restriction, or temporary speed.
      </p>

      <div className="closure-presets" aria-label="Premarked closure plans">
        <div className="closure-presets__heading">
          <MapPinned size={17} aria-hidden="true" />
          <div>
            <strong>Premarked closure</strong>
            <small>Load an official project layout without clicking every road.</small>
          </div>
        </div>
        {presetsLoading ? (
          <div className="closure-preset-status" role="status">
            <LoaderCircle className="spin" size={16} aria-hidden="true" />
            Loading closure plans…
          </div>
        ) : null}
        {presetsError ? (
          <small className="schedule-error" role="alert">{presetsError}</small>
        ) : null}
        {presets.map((preset) => (
          <ClosurePresetCard
            key={preset.id}
            preset={preset}
            onApply={() => onApplyPreset(preset)}
          />
        ))}
      </div>

      <div className="departure-condition">
        <div>
          <span className="condition-kicker">Step 3 · Conditions</span>
          <strong>Trip departure</strong>
          <small>Road schedules are evaluated at this local time.</small>
        </div>
        <input
          type="datetime-local"
          required
          value={departureTime}
          onChange={(event) => onDepartureTimeChange(event.target.value)}
          aria-label="Trip departure time"
        />
      </div>

      {sections.length > 0 ? (
        <>
          <div className="closure-selection-summary" role="status">
            <Check size={16} aria-hidden="true" />
            <span>
              {sections.length} road {sections.length === 1 ? 'section' : 'sections'} selected.
              Both directions are selected automatically when available.
            </span>
          </div>
          <div className="closure-list" aria-label="Selected road sections">
            {sections.map((section, sectionIndex) => (
              <ClosureSectionCard
                key={section.selection.directions
                  .map((direction) => direction.edge.edge_id)
                  .join(':')}
                section={section}
                sectionIndex={sectionIndex}
                selectedEdgeIds={selectedEdgeIds}
                onToggleDirection={onToggleDirection}
                onSelectAllDirections={onSelectAllDirections}
                onRestrictionChange={onRestrictionChange}
                departureTime={departureTime}
                onScheduleChange={onScheduleChange}
                onRemoveSection={onRemoveSection}
              />
            ))}
          </div>
        </>
      ) : null}

      <button
        className={`secondary-action ${picking ? 'secondary-action--active' : ''}`}
        type="button"
        disabled={selecting}
        onClick={picking ? onStopPicking : onStartPicking}
      >
        {selecting ? (
          <LoaderCircle className="spin" size={18} aria-hidden="true" />
        ) : (
          <Crosshair size={18} aria-hidden="true" />
        )}
        {selecting
          ? 'Finding road directions…'
          : picking
            ? 'Done selecting roads'
            : sections.length > 0
              ? 'Add another road section'
              : 'Choose closed road on map'}
      </button>

      {sections.length > 1 ? (
        <button className="text-action clear-all-action" type="button" onClick={onClearAll}>
          Clear all {sections.length} sections
        </button>
      ) : null}

      {error ? (
        <div className="inline-error" role="alert">
          {error}
        </div>
      ) : null}

      {sections.length > 0 ? (
        <div className="closure-save-card">
          <div>
            <strong>Save these closures</strong>
            <small>Stores the trip, every selected road, direction, impact, and schedule.</small>
          </div>
          <label>
            Plan name
            <input
              value={scenarioName}
              maxLength={120}
              placeholder="Downtown construction closures"
              onChange={(event) => onScenarioNameChange(event.target.value)}
            />
          </label>
          <button
            type="button"
            disabled={saving || !canSave || !scenarioName.trim()}
            onClick={onSaveScenario}
          >
            {saving ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />}
            {currentScenarioRevision
              ? `Save revision ${currentScenarioRevision + 1}`
              : 'Save closure plan'}
          </button>
          {currentScenarioRevision ? (
            <small className="closure-save-status">
              Revision {currentScenarioRevision} · {scenarioDirty ? 'Unsaved changes' : 'Saved'}
            </small>
          ) : null}
          {saveError ? <small className="schedule-error" role="alert">{saveError}</small> : null}
        </div>
      ) : null}

      <button
        className="primary-action primary-action--closure"
        type="button"
        disabled={
          sections.length === 0 ||
          selectedEdgeIds.length === 0 ||
          !departureTime ||
          invalidSchedule ||
          comparing
        }
        onClick={onCompare}
      >
        {comparing ? (
          <LoaderCircle className="spin" size={18} aria-hidden="true" />
        ) : (
          <SlidersHorizontal size={18} aria-hidden="true" />
        )}
        {comparing
          ? 'Comparing routes…'
          : sections.length === 0
            ? 'Compare road impacts'
            : `Compare ${sections.length} road ${sections.length === 1 ? 'section' : 'sections'}`}
      </button>
    </section>
  )
}

type ClosurePresetCardProps = {
  preset: ClosurePreset
  onApply: () => void
}

function ClosurePresetCard({ preset, onApply }: ClosurePresetCardProps) {
  const fullClosures = preset.sections.filter(
    (section) => section.restriction.type === 'full',
  ).length
  const laneRestrictions = preset.sections.length - fullClosures
  const start = new Date(preset.timing.mainline_starts_at)
  const rampStart = preset.timing.ramp_closures_may_start_at
    ? new Date(preset.timing.ramp_closures_may_start_at)
    : null
  const canApply = preset.graph_status === 'current'

  return (
    <article className="closure-preset-card">
      <div className="closure-preset-card__title">
        <div>
          <strong>{preset.name}</strong>
          <small>{preset.summary}</small>
        </div>
        <span>{preset.sections.length} sections</span>
      </div>
      <div className="closure-preset-card__facts">
        <span>{fullClosures} fully closed</span>
        <span>{laneRestrictions} one-lane sections</span>
      </div>
      <div className="closure-preset-card__timing">
        <CalendarClock size={15} aria-hidden="true" />
        <span>
          Mainline starts {formatPresetDate(start)}.
          {rampStart ? ` Ramps may close at ${formatPresetTime(rampStart)}.` : ''}{' '}
          {preset.timing.duration_note}
        </span>
      </div>
      {!preset.timing.exact_end_confirmed ? (
        <div className="closure-preset-card__warning">
          <TriangleAlert size={15} aria-hidden="true" />
          <span>
            Exact reopening is not confirmed. Loaded sections stay active for the
            trip time you choose; add schedules only after ODOT publishes the end.
          </span>
        </div>
      ) : null}
      {preset.warnings.map((warning) => (
        <small className="schedule-error" key={warning}>{warning}</small>
      ))}
      <div className="closure-preset-card__actions">
        <button type="button" disabled={!canApply} onClick={onApply}>
          <MapPinned size={16} aria-hidden="true" />
          {canApply ? 'Apply marked closure' : 'Graph review required'}
        </button>
        {preset.sources[0] ? (
          <a href={preset.sources[0].url} target="_blank" rel="noreferrer">
            Project details <ExternalLink size={13} aria-hidden="true" />
          </a>
        ) : null}
      </div>
    </article>
  )
}

function formatPresetDate(value: Date): string {
  return new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/Los_Angeles',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
  }).format(value)
}

function formatPresetTime(value: Date): string {
  return new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/Los_Angeles',
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
  }).format(value)
}

type ClosureSectionCardProps = {
  section: ClosureSectionDraft
  sectionIndex: number
  selectedEdgeIds: string[]
  onToggleDirection: (edgeId: string) => void
  onSelectAllDirections: (sectionIndex: number) => void
  onRestrictionChange: (
    sectionIndex: number,
    restriction: RoadRestriction,
  ) => void
  departureTime: string
  onScheduleChange: (
    sectionIndex: number,
    schedule: RestrictionSchedule,
  ) => void
  onRemoveSection: (sectionIndex: number) => void
}

function ClosureSectionCard({
  section,
  sectionIndex,
  selectedEdgeIds,
  onToggleDirection,
  onSelectAllDirections,
  onRestrictionChange,
  departureTime,
  onScheduleChange,
  onRemoveSection,
}: ClosureSectionCardProps) {
  const { selection, restriction } = section
  const allSelected = selection.directions.every((direction) =>
    selectedEdgeIds.includes(direction.edge.edge_id),
  )

  return (
    <div className="closure-card">
      <div className="closure-card__heading">
        <span className="closure-icon" aria-hidden="true">
          {sectionIndex + 1}
        </span>
        <div>
          <strong>{selection.road_name}</strong>
          <small>
            {selection.distance_m.toFixed(0)} m from click ·{' '}
            {restrictionLabel(restriction)}
          </small>
        </div>
        <button
          className="icon-button"
          type="button"
          onClick={() => onRemoveSection(sectionIndex)}
        >
          <Trash2 size={16} aria-hidden="true" />
          <span className="sr-only">Remove road section {sectionIndex + 1}</span>
        </button>
      </div>

      <RestrictionControls
        selection={selection}
        restriction={restriction}
        onChange={(nextRestriction) =>
          onRestrictionChange(sectionIndex, nextRestriction)
        }
      />

      <ScheduleControls
        schedule={section.schedule}
        departureTime={departureTime}
        onChange={(schedule) => onScheduleChange(sectionIndex, schedule)}
      />

      <fieldset className="direction-options">
        <legend>Affected direction</legend>
        {selection.directions.map((direction) => {
          const checked = selectedEdgeIds.includes(direction.edge.edge_id)
          return (
            <label key={direction.edge.edge_id}>
              <input
                type="checkbox"
                checked={checked}
                onChange={() => onToggleDirection(direction.edge.edge_id)}
              />
              <span className="direction-check" aria-hidden="true">
                {checked ? <Check size={13} /> : null}
              </span>
              <span>
                <strong>{direction.direction_label}</strong>
                <small>
                  {direction.edge.u} → {direction.edge.v}
                </small>
              </span>
            </label>
          )
        })}
      </fieldset>

      {selection.directions.length > 1 && !allSelected ? (
        <button
          className="text-action"
          type="button"
          onClick={() => onSelectAllDirections(sectionIndex)}
        >
          Apply to both directions
        </button>
      ) : null}
    </div>
  )
}

type ScheduleControlsProps = {
  schedule: RestrictionSchedule
  departureTime: string
  onChange: (schedule: RestrictionSchedule) => void
}

function ScheduleControls({
  schedule,
  departureTime,
  onChange,
}: ScheduleControlsProps) {
  const invalid =
    schedule.type === 'scheduled' &&
    (!schedule.starts_at ||
      !schedule.ends_at ||
      new Date(schedule.ends_at) <= new Date(schedule.starts_at))

  function toggleScheduled(scheduled: boolean) {
    if (!scheduled) {
      onChange({ type: 'always' })
      return
    }
    const departure = departureTime ? new Date(departureTime) : new Date()
    const start = new Date(departure.getTime() - 60 * 60 * 1000)
    const end = new Date(departure.getTime() + 4 * 60 * 60 * 1000)
    onChange({
      type: 'scheduled',
      starts_at: localDateTimeValue(start),
      ends_at: localDateTimeValue(end),
    })
  }

  return (
    <div className="schedule-controls">
      <label className="schedule-toggle">
        <input
          type="checkbox"
          checked={schedule.type === 'scheduled'}
          onChange={(event) => toggleScheduled(event.target.checked)}
        />
        Only active during a time window
      </label>
      {schedule.type === 'scheduled' ? (
        <div className="schedule-window">
          <label>
            Starts
            <input
              type="datetime-local"
              required
              value={schedule.starts_at}
              onChange={(event) =>
                onChange({ ...schedule, starts_at: event.target.value })
              }
            />
          </label>
          <label>
            Ends
            <input
              type="datetime-local"
              required
              value={schedule.ends_at}
              onChange={(event) =>
                onChange({ ...schedule, ends_at: event.target.value })
              }
            />
          </label>
          {invalid ? (
            <small className="schedule-error" role="alert">
              End time must be after start time.
            </small>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

type RestrictionControlsProps = {
  selection: ClosureRoadSelectionResponse
  restriction: RoadRestriction
  onChange: (restriction: RoadRestriction) => void
}

function RestrictionControls({
  selection,
  restriction,
  onChange,
}: RestrictionControlsProps) {
  const availableLanes = Math.min(
    ...selection.directions.map((direction) => direction.edge.lanes),
  )
  const currentSpeedKph = Math.min(
    ...selection.directions.map((direction) => direction.edge.maxspeed_kph),
  )
  const currentSpeedMph = Math.round(currentSpeedKph / 1.609344)

  function changeType(type: RoadRestriction['type']) {
    if (type === 'full') onChange({ type: 'full' })
    if (type === 'lane') {
      onChange({
        type: 'lane',
        remaining_lanes: Math.max(1, Math.floor(availableLanes) - 1),
      })
    }
    if (type === 'speed') {
      const speedMph = Math.max(5, Math.min(25, currentSpeedMph - 5))
      onChange({ type: 'speed', speed_limit_kph: speedMph * 1.609344 })
    }
  }

  return (
    <div className="restriction-controls">
      <label>
        Road impact
        <select
          value={restriction.type}
          onChange={(event) =>
            changeType(event.target.value as RoadRestriction['type'])
          }
        >
          <option value="full">Full closure</option>
          <option value="lane" disabled={availableLanes <= 1}>
            Lane restriction{availableLanes <= 1 ? ' (one-lane road)' : ''}
          </option>
          <option value="speed">Temporary speed</option>
        </select>
      </label>

      {restriction.type === 'lane' ? (
        <label>
          Lanes remaining
          <input
            type="number"
            min="1"
            max={Math.max(1, Math.ceil(availableLanes) - 1)}
            step="1"
            value={restriction.remaining_lanes}
            onChange={(event) => {
              const maximum = Math.max(1, Math.ceil(availableLanes) - 1)
              onChange({
                type: 'lane',
                remaining_lanes: Math.min(
                  maximum,
                  Math.max(1, Number(event.target.value)),
                ),
              })
            }}
          />
          <small>{formatLanes(availableLanes)} normally</small>
        </label>
      ) : null}

      {restriction.type === 'speed' ? (
        <label>
          Temporary speed
          <span className="restriction-input-unit">
            <input
              type="number"
              min="5"
              max={Math.max(5, currentSpeedMph - 5)}
              step="5"
              value={Math.round(restriction.speed_limit_kph / 1.609344)}
              onChange={(event) => {
                const maximum = Math.max(5, currentSpeedMph - 5)
                onChange({
                  type: 'speed',
                  speed_limit_kph:
                    Math.min(maximum, Math.max(5, Number(event.target.value))) *
                    1.609344,
                })
              }}
            />
            mph
          </span>
          <small>{currentSpeedMph} mph normally</small>
        </label>
      ) : null}
    </div>
  )
}

function restrictionLabel(restriction: RoadRestriction): string {
  if (restriction.type === 'full') return 'Full closure'
  if (restriction.type === 'lane') {
    return `${formatLanes(restriction.remaining_lanes)} remaining`
  }
  return `${Math.round(restriction.speed_limit_kph / 1.609344)} mph limit`
}

function formatLanes(lanes: number): string {
  return `${Number.isInteger(lanes) ? lanes : lanes.toFixed(1)} ${lanes === 1 ? 'lane' : 'lanes'}`
}

function localDateTimeValue(value: Date): string {
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(
    value.getDate(),
  )}T${pad(value.getHours())}:${pad(value.getMinutes())}`
}
