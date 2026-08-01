import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Ban, LoaderCircle, Network, X } from 'lucide-react'
import {
  cancelDiversion,
  getDiversion,
  startDiversion,
  type DiversionResult,
  type DiversionSettings,
} from '../../api/diversion'
import type {
  ClosureComparisonInput,
  SelectedLocation,
} from '../../api/routing'
import { listTrafficProfiles } from '../../api/traffic'

type DiversionPanelProps = {
  origin: SelectedLocation
  destination: SelectedLocation
  closure: ClosureComparisonInput
  settings: DiversionSettings
  onSettingsChange: (settings: DiversionSettings) => void
  onResult: (result: DiversionResult | null) => void
}

export function DiversionPanel({
  origin,
  destination,
  closure,
  settings,
  onSettingsChange,
  onResult,
}: DiversionPanelProps) {
  const [jobId, setJobId] = useState<string | null>(null)
  const deliveredHash = useRef<string | null>(null)
  const starter = useMutation({
    mutationFn: () => startDiversion(origin, destination, closure, settings),
    onSuccess: (job) => {
      deliveredHash.current = null
      onResult(null)
      setJobId(job.id)
    },
  })
  const profiles = useQuery({
    queryKey: ['traffic-profiles'],
    queryFn: ({ signal }) => listTrafficProfiles(signal),
    refetchOnWindowFocus: true,
  })
  const job = useQuery({
    queryKey: ['diversion-job', jobId],
    queryFn: ({ signal }) => {
      if (!jobId) throw new Error('No diversion job is active.')
      return getDiversion(jobId, signal)
    },
    enabled: jobId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === 'queued' || status === 'running' ? 400 : false
    },
    refetchOnWindowFocus: false,
  })
  const canceller = useMutation({
    mutationFn: () => {
      if (!jobId) throw new Error('No diversion job is active.')
      return cancelDiversion(jobId)
    },
  })

  useEffect(() => {
    const result = job.data?.result
    if (
      job.data?.status === 'completed' &&
      result &&
      deliveredHash.current !== result.input_hash
    ) {
      deliveredHash.current = result.input_hash
      onResult(result)
    }
  }, [job.data, onResult])

  const active = job.data?.status === 'queued' || job.data?.status === 'running'
  const result = job.data?.result
  const error = starter.error?.message ?? job.error?.message ?? job.data?.error ?? null

  function updateSettings(next: DiversionSettings) {
    setJobId(null)
    onResult(null)
    onSettingsChange(next)
  }

  return (
    <section className="diversion-panel" aria-labelledby="diversion-title">
      <div className="diversion-heading">
        <div>
          <span className="result-kicker">Compare · network spillover</span>
          <h2 id="diversion-title">Where might traffic divert?</h2>
        </div>
        <span className="evidence-badge evidence-badge--modeled_uncalibrated">
          Modeled · uncalibrated
        </span>
      </div>
      <p className="panel-intro">
        Reassign traffic around the closure, then route your trip across the resulting network. Select observed background traffic when available; uncovered roads remain modeled estimates.
      </p>

      <div className="diversion-controls">
        <label>
          Background traffic
          <select
            value={settings.trafficProfileId ?? ''}
            disabled={active}
            onChange={(event) => updateSettings({ ...settings, trafficProfileId: event.target.value || null })}
          >
            <option value="">Synthetic only · no observed background</option>
            {(profiles.data ?? []).map((profile) => (
              <option key={profile.id} value={profile.id}>
                {profile.period === 'weekday_morning' ? 'Weekday AM' : 'Weekday PM'} · {profile.volume_observation_count.toLocaleString()} volume rows · {profile.source_name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Additional corridor demand
          <select
            value={settings.demandVph}
            disabled={active}
            onChange={(event) => updateSettings({ ...settings, demandVph: Number(event.target.value) })}
          >
            {[300, 600, 1200, 2400].map((value) => <option key={value} value={value}>{value.toLocaleString()} vehicles/hour</option>)}
          </select>
        </label>
        <label>
          Demand pairs
          <select
            value={settings.demandPairCount}
            disabled={active}
            onChange={(event) => updateSettings({ ...settings, demandPairCount: Number(event.target.value) })}
          >
            {[10, 20, 40, 80].map((value) => <option key={value} value={value}>{value} dispersed pairs</option>)}
          </select>
        </label>
        <label>
          MSA iterations
          <select
            value={settings.iterations}
            disabled={active}
            onChange={(event) => updateSettings({ ...settings, iterations: Number(event.target.value) })}
          >
            {[2, 3, 4, 6, 8].map((value) => <option key={value} value={value}>{value} iterations</option>)}
          </select>
        </label>
        <label>
          Endpoint radius
          <select
            value={settings.dispersionRadiusM}
            disabled={active}
            onChange={(event) => updateSettings({ ...settings, dispersionRadiusM: Number(event.target.value) })}
          >
            <option value={0}>Exact endpoints</option>
            <option value={500}>0.3 miles</option>
            <option value={1000}>0.6 miles</option>
            <option value={2500}>1.6 miles</option>
            <option value={5000}>3.1 miles</option>
          </select>
        </label>
      </div>

      {active && job.data ? (
        <div className="diversion-progress" role="status">
          <div><strong>{job.data.progress_percent}%</strong><span>{job.data.message}</span></div>
          <progress max={100} value={job.data.progress_percent} />
          <button type="button" disabled={canceller.isPending} onClick={() => canceller.mutate()}>
            <X size={14} /> Cancel
          </button>
        </div>
      ) : null}

      <button
        className="primary-action diversion-run"
        type="button"
        disabled={active || starter.isPending}
        onClick={() => starter.mutate()}
      >
        {active || starter.isPending ? <LoaderCircle className="spin" size={17} /> : <Network size={17} />}
        {active || starter.isPending ? 'Modeling diversion…' : result ? 'Run again' : 'Model network diversion'}
      </button>

      {job.data?.status === 'cancelled' ? (
        <div className="diversion-notice"><Ban size={16} /> The diversion run was cancelled.</div>
      ) : null}
      {error ? <div className="inline-error" role="alert">{error}</div> : null}

      {result ? (
        <div className="diversion-results">
          <div className="diversion-summary-grid">
            <Metric label="Largest increase" value={`+${formatFlow(result.max_increase_vph)}`} />
            <Metric label="Largest decrease" value={formatFlow(result.max_decrease_vph)} />
            <Metric label="Changed directed edges" value={result.changed_edge_count.toLocaleString()} />
            <Metric label="Residential increase" value={`+${formatFlow(result.residential_increase_vph)}`} />
            <Metric label="Observed edges" value={result.background_matched_edge_count.toLocaleString()} />
            <Metric label="Network coverage" value={`${result.background_network_coverage_percent.toFixed(2)}%`} />
          </div>
          {result.recommended_route ? (
            <div className="diversion-route-recommendation">
              <span>Closure and traffic-aware route</span>
              <strong>{formatDuration(result.recommended_route.travel_time_seconds)}</strong>
              <small>{formatDistance(result.recommended_route.distance_m)} · now drawn on the map and used for navigation</small>
            </div>
          ) : null}
          <p className="result-note">
            Background: {result.background_source} · {result.background_bucket} · {result.background_observation_count.toLocaleString()} observations
            {result.displaced_background_vph > 0 ? ` · ${formatFlow(result.displaced_background_vph)} displaced from closed edges` : ''}
          </p>
          <div className="spillover-legend" aria-label="Spillover map legend">
            <span><i className="spillover-key spillover-key--increase" /> Increase</span>
            <span><i className="spillover-key spillover-key--decrease" /> Decrease · dashed</span>
          </div>
          {result.unassigned_demand_vph > 0 ? (
            <p className="diversion-warning">
              {formatFlow(result.unassigned_demand_vph)} could not be assigned under the selected restrictions.
            </p>
          ) : null}
          <div className="spillover-roads">
            <strong>Largest modeled changes</strong>
            {result.edge_changes.slice(0, 8).map((change) => (
              <div key={change.edge_id}>
                <span>{change.road_name}</span>
                <small>{change.road_class} · {change.u} → {change.v}</small>
                <b className={change.change_vph >= 0 ? 'flow-increase' : 'flow-decrease'}>
                  {change.change_vph >= 0 ? '+' : ''}{formatFlow(change.change_vph)}
                </b>
              </div>
            ))}
          </div>
          <p className="result-note">
            {result.demand_pair_count} pairs · {result.iterations} MSA iterations · model {result.model_version} · input {result.input_hash.slice(0, 12)}{job.data?.cached ? ' · local cache hit' : ''}
          </p>
          <ul className="assumption-list">
            {result.assumptions.map((assumption) => <li key={assumption}>{assumption}</li>)}
          </ul>
        </div>
      ) : null}
    </section>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div><strong>{value}</strong><span>{label}</span></div>
}

function formatFlow(value: number): string {
  return `${Math.round(value).toLocaleString()} veh/hr`
}

function formatDuration(seconds: number): string {
  const minutes = Math.max(1, Math.round(seconds / 60))
  return `${minutes} min`
}

function formatDistance(meters: number): string {
  return `${(meters / 1609.344).toFixed(1)} mi`
}
