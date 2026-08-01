import { useState, type ChangeEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clock3, Database, DownloadCloud, LoaderCircle, Upload } from 'lucide-react'
import type { RouteSummary } from '../../api/routing'
import {
  importTraffic,
  acquirePortalTraffic,
  listTrafficProfiles,
  listPortalHighways,
  simulateReliability,
  type ReliabilitySettings,
  type TrafficImportResponse,
  type PortalAcquireResponse,
  type PortalAcquireSettings,
} from '../../api/traffic'

type ReliabilityPanelProps = {
  route: RouteSummary
  departureTime: string
  settings: ReliabilitySettings
  onSettingsChange: (settings: ReliabilitySettings) => void
  onDepartureTimeChange: (value: string) => void
}

type ImportVariables = { file: File; sourceName: string }

export function ReliabilityPanel({
  route,
  departureTime,
  settings,
  onSettingsChange,
  onDepartureTimeChange,
}: ReliabilityPanelProps) {
  const queryClient = useQueryClient()
  const [sourceName, setSourceName] = useState('')
  const [importResult, setImportResult] = useState<TrafficImportResponse | null>(null)
  const [portalResult, setPortalResult] = useState<PortalAcquireResponse | null>(null)
  const [portalWindow, setPortalWindow] = useState<PortalAcquireSettings>(defaultPortalWindow)
  const [lastSimulationKey, setLastSimulationKey] = useState('')
  const profiles = useQuery({
    queryKey: ['traffic-profiles'],
    queryFn: ({ signal }) => listTrafficProfiles(signal),
    refetchOnWindowFocus: true,
  })
  const portalHighways = useQuery({
    queryKey: ['portal-highways'],
    queryFn: ({ signal }) => listPortalHighways(signal),
    refetchOnWindowFocus: false,
  })
  const simulation = useMutation({
    mutationFn: () => simulateReliability(route, departureTime, settings),
    onSuccess: () => setLastSimulationKey(simulationKey(route.route_id, departureTime, settings)),
  })
  const importer = useMutation({
    mutationFn: ({ file, sourceName: name }: ImportVariables) => importTraffic(file, name),
    onSuccess: (result) => {
      setImportResult(result)
      void queryClient.invalidateQueries({ queryKey: ['traffic-profiles'] })
      if (result.profiles[0]) {
        onSettingsChange({ ...settings, profileId: result.profiles[0].id })
      }
    },
  })
  const portalAcquisition = useMutation({
    mutationFn: () => acquirePortalTraffic(portalWindow),
    onSuccess: (result) => {
      setPortalResult(result)
      setImportResult(result.import_result)
      void queryClient.invalidateQueries({ queryKey: ['traffic-profiles'] })
      if (result.import_result.profiles[0]) {
        onSettingsChange({ ...settings, profileId: result.import_result.profiles[0].id })
      }
    },
  })

  function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    if (!file || !sourceName.trim()) return
    importer.mutate({ file, sourceName: sourceName.trim() })
    event.target.value = ''
  }

  const timeIsValid = settings.planningMode === 'depart_at'
    ? departureTime !== '' && !Number.isNaN(new Date(departureTime).getTime())
    : settings.arrivalDeadline !== '' &&
      new Date(settings.arrivalDeadline) > new Date(departureTime)
  const currentSimulationKey = simulationKey(route.route_id, departureTime, settings)
  const result = lastSimulationKey === currentSimulationKey ? simulation.data : undefined

  return (
    <section className="reliability-panel" aria-labelledby="reliability-title">
      <div className="reliability-heading">
        <div>
          <span className="result-kicker">Conditions · trip timing</span>
          <h2 id="reliability-title">
            {settings.planningMode === 'arrive_by'
              ? 'When should you leave?'
              : 'When should you expect to arrive?'}
          </h2>
        </div>
        {result ? (
          <span className={`evidence-badge evidence-badge--${result.evidence_level}`}>
            {result.evidence_level === 'historically_calibrated'
              ? 'Historically calibrated'
              : 'Modeled · uncalibrated'}
          </span>
        ) : null}
      </div>

      <div className="planning-mode-toggle" role="group" aria-label="Choose a time-planning mode">
        <button
          type="button"
          className={settings.planningMode === 'arrive_by' ? 'planning-mode--active' : ''}
          onClick={() => onSettingsChange({ ...settings, planningMode: 'arrive_by' })}
        >
          I need to arrive by
        </button>
        <button
          type="button"
          className={settings.planningMode === 'depart_at' ? 'planning-mode--active' : ''}
          onClick={() => onSettingsChange({ ...settings, planningMode: 'depart_at' })}
        >
          I want to leave at
        </button>
      </div>

      <div className={`reliability-controls reliability-controls--${settings.planningMode}`}>
        <label>
          {settings.planningMode === 'arrive_by' ? 'Arrive by' : 'Leave at'}
          <input
            type="datetime-local"
            value={settings.planningMode === 'arrive_by' ? settings.arrivalDeadline : departureTime}
            min={settings.planningMode === 'arrive_by' ? departureTime : undefined}
            onChange={(event) => {
              if (settings.planningMode === 'arrive_by') {
                onSettingsChange({ ...settings, arrivalDeadline: event.target.value })
              } else {
                onDepartureTimeChange(event.target.value)
              }
            }}
          />
        </label>
        {settings.planningMode === 'arrive_by' ? (
          <label>
            Arrival buffer
            <select
              value={settings.bufferMinutes}
              onChange={(event) =>
                onSettingsChange({ ...settings, bufferMinutes: Number(event.target.value) })
              }
            >
              {[0, 5, 8, 10, 15, 20].map((minutes) => (
                <option key={minutes} value={minutes}>{minutes} minutes</option>
              ))}
            </select>
          </label>
        ) : null}
        <label>
          {settings.planningMode === 'arrive_by' ? 'Confidence target' : 'Plan for'}
          <select
            value={settings.confidenceTarget}
            onChange={(event) =>
              onSettingsChange({ ...settings, confidenceTarget: Number(event.target.value) })
            }
          >
            <option value={0.85}>85%</option>
            <option value={0.9}>90%</option>
            <option value={0.95}>95%</option>
          </select>
        </label>
        <label>
          Traffic evidence
          <select
            value={settings.profileId ?? ''}
            onChange={(event) =>
              onSettingsChange({ ...settings, profileId: event.target.value || null })
            }
          >
            <option value="">Modeled estimate · no history</option>
            {(profiles.data ?? []).map((profile) => (
              <option key={profile.id} value={profile.id}>
                {profile.period === 'weekday_morning' ? 'Sep/Oct weekday AM' : 'Sep/Oct weekday PM'} · {profile.observation_count} samples
              </option>
            ))}
          </select>
        </label>
      </div>

      {!timeIsValid ? (
        <p className="inline-error">
          {settings.planningMode === 'arrive_by'
            ? 'Arrival time must be later than departure time.'
            : 'Choose a valid departure time.'}
        </p>
      ) : null}
      <button
        className="primary-action reliability-run"
        type="button"
        disabled={!timeIsValid || simulation.isPending}
        onClick={() => simulation.mutate()}
      >
        {simulation.isPending ? <LoaderCircle className="spin" size={16} /> : <Clock3 size={16} />}
        {simulation.isPending
          ? 'Sampling travel times…'
          : settings.planningMode === 'arrive_by'
            ? 'Find my safe departure time'
            : 'Estimate my arrival time'}
      </button>
      {simulation.error ? <p className="inline-error">{simulation.error.message}</p> : null}

      {result ? (
        <div className="reliability-results">
          <div className="reliability-hero">
            <span>
              {result.planning_mode === 'arrive_by'
                ? 'Latest safe departure'
                : `${Math.round(result.confidence_target * 100)}% arrival estimate`}
            </span>
            <strong>
              {formatClock(
                result.planning_mode === 'arrive_by'
                  ? result.latest_safe_departure
                  : result.confidence_arrival_time,
              )}
            </strong>
            <small>
              {result.planning_mode === 'arrive_by'
                ? `for ${Math.round(result.confidence_target * 100)}% confidence + ${settings.bufferMinutes} min buffer`
                : `when leaving at ${formatClock(result.planned_departure_time)}`}
            </small>
          </div>
          <div className="reliability-metrics">
            <Metric
              label={result.planning_mode === 'depart_at' ? 'Median arrival' : 'Median'}
              value={result.planning_mode === 'depart_at' ? formatClock(result.median_arrival_time) : formatDuration(result.median_seconds)}
            />
            <Metric label={result.planning_mode === 'depart_at' ? 'P85 arrival' : 'P85'} value={result.planning_mode === 'depart_at' ? formatClock(result.p85_arrival_time) : formatDuration(result.p85_seconds)} />
            <Metric label={result.planning_mode === 'depart_at' ? 'P90 arrival' : 'P90'} value={result.planning_mode === 'depart_at' ? formatClock(result.p90_arrival_time) : formatDuration(result.p90_seconds)} />
            <Metric label={result.planning_mode === 'depart_at' ? 'P95 arrival' : 'P95'} value={result.planning_mode === 'depart_at' ? formatClock(result.p95_arrival_time) : formatDuration(result.p95_seconds)} />
            {result.on_time_probability !== null ? (
              <Metric label="On-time chance" value={formatPercent(result.on_time_probability)} />
            ) : (
              <Metric label="Median travel" value={formatDuration(result.median_seconds)} />
            )}
            <Metric
              label="Likely range"
              value={`${formatDuration(result.likely_low_seconds)}–${formatDuration(result.likely_high_seconds)}`}
            />
          </div>
          {result.early_departure_benefits.length ? (
            <div className="early-benefits">
              <strong>Benefit of leaving earlier</strong>
              {result.early_departure_benefits.map((benefit) => (
                <span key={benefit.minutes_earlier}>
                  {benefit.minutes_earlier} min: {formatPercent(benefit.on_time_probability)} on time
                  {' '}(+{Math.round(benefit.improvement * 100)} points)
                </span>
              ))}
            </div>
          ) : null}
          <p className="result-note">
            {result.sample_count.toLocaleString()} deterministic samples · {result.source_name} · {result.source_window} · profile {result.profile_version}
          </p>
          <ul className="assumption-list">
            {result.assumptions.map((assumption) => <li key={assumption}>{assumption}</li>)}
          </ul>
        </div>
      ) : null}

      <details className="traffic-import">
        <summary><Database size={14} /> Get historical traffic data</summary>
        <div className="portal-acquisition">
          <strong>Download directly from PORTAL</strong>
          <p>
            Commute Help fetches public Portland–Vancouver detector observations, joins station and direction metadata, combines lane detectors, converts mph to km/h and interval counts to vehicles/hour, then matches the result to the local road graph.
          </p>
          <div className="portal-acquisition-controls">
            <label>
              Start date
              <input
                type="date"
                value={portalWindow.startDate}
                onChange={(event) => setPortalWindow({ ...portalWindow, startDate: event.target.value })}
              />
            </label>
            <label>
              End date
              <input
                type="date"
                value={portalWindow.endDate}
                onChange={(event) => setPortalWindow({ ...portalWindow, endDate: event.target.value })}
              />
            </label>
            <label>
              Resolution
              <select
                value={portalWindow.resolution}
                onChange={(event) => setPortalWindow({
                  ...portalWindow,
                  resolution: event.target.value as '00:15:00' | '01:00:00',
                })}
              >
                <option value="00:15:00">15 minutes · recommended</option>
                <option value="01:00:00">1 hour · smaller download</option>
              </select>
            </label>
          </div>
          <label className="portal-highway-picker">
            Highway directions—select up to eight
            <select
              multiple
              size={6}
              value={portalWindow.highwayIds.map(String)}
              disabled={portalHighways.isPending || portalAcquisition.isPending}
              onChange={(event) => {
                const highwayIds = Array.from(event.target.selectedOptions)
                  .map((option) => Number(option.value))
                  .slice(0, 8)
                setPortalWindow({ ...portalWindow, highwayIds })
              }}
            >
              {(portalHighways.data ?? []).map((highway) => (
                <option key={highway.id} value={highway.id}>
                  {highway.name} {highway.direction} · {highway.station_count} stations
                </option>
              ))}
            </select>
          </label>
          <button
            className="portal-download"
            type="button"
            disabled={portalWindow.highwayIds.length === 0 || portalAcquisition.isPending}
            onClick={() => portalAcquisition.mutate()}
          >
            {portalAcquisition.isPending
              ? <LoaderCircle className="spin" size={15} />
              : <DownloadCloud size={15} />}
            {portalAcquisition.isPending ? 'Downloading and matching…' : 'Get traffic from PORTAL'}
          </button>
          {portalHighways.error ? <p className="inline-error">{portalHighways.error.message}</p> : null}
          {portalAcquisition.error ? <p className="inline-error">{portalAcquisition.error.message}</p> : null}
          {portalResult ? (
            <div className="import-quality" role="status">
              <strong>
                Downloaded {portalResult.downloaded_observation_count.toLocaleString()} detector rows and normalized {portalResult.normalized_station_count.toLocaleString()} stations
              </strong>
              <span>{portalResult.requested_highways.map((highway) => `${highway.name} ${highway.direction}`).join(', ')}</span>
            </div>
          ) : null}
        </div>
        <div className="manual-traffic-import">
        <strong>Or import an existing file</strong>
        <p>
          CSV or Parquet, up to 50 MB. Raw files stay unchanged; accepted rows are matched to the local graph and normalized separately.
        </p>
        <div className="traffic-import-controls">
          <label>
            Source name
            <input
              value={sourceName}
              placeholder="Example: local DOT detector export"
              onChange={(event) => setSourceName(event.target.value)}
            />
          </label>
          <label className={sourceName.trim() ? 'traffic-file' : 'traffic-file traffic-file--disabled'}>
            <Upload size={14} />
            {importer.isPending ? 'Importing…' : 'Choose CSV or Parquet'}
            <input
              type="file"
              accept=".csv,.parquet,text/csv,application/vnd.apache.parquet"
              disabled={!sourceName.trim() || importer.isPending}
              onChange={chooseFile}
            />
          </label>
        </div>
        {importer.error ? <p className="inline-error">{importer.error.message}</p> : null}
        {importResult ? (
          <div className="import-quality" role="status">
            <strong>
              Accepted {importResult.quality.accepted_count} of {importResult.quality.row_count} rows · built {importResult.profiles.length} profile(s)
            </strong>
            <span>
              {importResult.quality.matched_station_count} matched and {importResult.quality.unmatched_station_count} unmatched station/segment IDs · missing speed {importResult.quality.missing_speed_percent}% · missing volume {importResult.quality.missing_volume_percent}%
            </span>
            {importResult.quality.quality_flags.length ? (
              <span>Source flags: {importResult.quality.quality_flags.join(', ')}</span>
            ) : null}
          </div>
        ) : null}
        <small className="traffic-schema-note">
          Required: station_or_segment_id, timestamp_local, and speed_kph or travel_time_seconds. Coordinates are required unless the ID exactly matches a graph edge.
        </small>
        </div>
      </details>
    </section>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div><strong>{value}</strong><span>{label}</span></div>
}

function formatDuration(seconds: number): string {
  return `${Math.round(seconds / 60)} min`
}

function formatPercent(value: number): string {
  return `${Math.round(value * 100)}%`
}

function formatClock(value: string | null): string {
  if (!value) return 'Not available'
  return new Intl.DateTimeFormat(undefined, {
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value))
}

function defaultPortalWindow(): PortalAcquireSettings {
  const now = new Date()
  const year = now.getMonth() >= 10 ? now.getFullYear() : now.getFullYear() - 1
  return {
    startDate: `${year}-09-01`,
    endDate: `${year}-10-31`,
    highwayIds: [],
    resolution: '00:15:00',
  }
}

function simulationKey(
  routeId: string,
  departureTime: string,
  settings: ReliabilitySettings,
): string {
  return JSON.stringify({ routeId, departureTime, settings })
}
