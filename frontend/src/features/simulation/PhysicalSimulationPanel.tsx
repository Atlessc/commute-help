import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Ban, LoaderCircle, Play, X } from 'lucide-react'
import type { ClosureComparisonInput, SelectedLocation } from '../../api/routing'
import {
  cancelPhysicalSimulation,
  DEFAULT_PHYSICAL_SIMULATION_SETTINGS,
  getPhysicalPlayback,
  getPhysicalSimulation,
  startPhysicalSimulation,
  type PhysicalSimulationPlayback,
  type SimulationRunStatus,
} from '../../api/simulation'

type Props = {
  origin: SelectedLocation
  destination: SelectedLocation
  closure: ClosureComparisonInput
  onResult: (status: SimulationRunStatus | null, playback: PhysicalSimulationPlayback | null) => void
}

export function PhysicalSimulationPanel({ origin, destination, closure, onResult }: Props) {
  const [settings, setSettings] = useState(DEFAULT_PHYSICAL_SIMULATION_SETTINGS)
  const [runId, setRunId] = useState<string | null>(null)
  const [playbackError, setPlaybackError] = useState<string | null>(null)
  const delivered = useRef<string | null>(null)
  const starter = useMutation({
    mutationFn: () => startPhysicalSimulation(origin, destination, closure, settings),
    onSuccess: (created) => {
      delivered.current = null
      setPlaybackError(null)
      onResult(null, null)
      setRunId(created.id)
    },
  })
  const run = useQuery({
    queryKey: ['physical-simulation', runId],
    queryFn: ({ signal }) => getPhysicalSimulation(runId as string, signal),
    enabled: runId !== null,
    refetchInterval: (query) => ['queued', 'running', 'cancel_requested'].includes(query.state.data?.status ?? '') ? 500 : false,
    refetchOnWindowFocus: false,
  })
  const canceller = useMutation({
    mutationFn: () => cancelPhysicalSimulation(runId as string),
  })

  useEffect(() => {
    if (run.data?.status !== 'completed' || delivered.current === run.data.id) return
    delivered.current = run.data.id
    void getPhysicalPlayback(run.data.id)
      .then((playback) => onResult(run.data ?? null, playback))
      .catch((error: Error) => {
        delivered.current = null
        setPlaybackError(error.message)
      })
  }, [onResult, run.data])

  const active = ['queued', 'running', 'cancel_requested'].includes(run.data?.status ?? '')
  const error = starter.error?.message ?? run.error?.message ?? run.data?.error_message ?? playbackError

  return (
    <section className="diversion-panel" aria-labelledby="physical-simulation-title">
      <div className="diversion-heading">
        <div>
          <span className="result-kicker">Physical regional comparison</span>
          <h2 id="physical-simulation-title">Run Portland traffic in SUMO</h2>
        </div>
        <span className="evidence-badge evidence-badge--modeled_uncalibrated">Modeled · uncalibrated</span>
      </div>
      <p className="panel-intro">
        Runs the same local background demand twice—first normally, then with every selected closure—and tracks your trip through both populations.
      </p>
      <div className="diversion-controls">
        <label>Traffic warmup
          <select value={settings.warmupMinutes} disabled={active} onChange={(event) => setSettings({ ...settings, warmupMinutes: Number(event.target.value) })}>
            {[15, 30, 45, 60].map((value) => <option key={value} value={value}>{value} minutes</option>)}
          </select>
        </label>
        <label>Playback window
          <select value={settings.analysisMinutes} disabled={active} onChange={(event) => setSettings({ ...settings, analysisMinutes: Number(event.target.value) })}>
            {[30, 60, 90, 120].map((value) => <option key={value} value={value}>{value} minutes</option>)}
          </select>
        </label>
        <label>Physical vehicle scale
          <select value={settings.realVehiclesPerSimulatedVehicle} disabled={active} onChange={(event) => setSettings({ ...settings, realVehiclesPerSimulatedVehicle: Number(event.target.value) })}>
            <option value={1}>1:1 · full density</option>
            <option value={5}>5:1 · faster preview</option>
            <option value={10}>10:1 · rough preview</option>
            <option value={25}>25:1 · pipeline check only</option>
          </select>
        </label>
        <label>Deterministic seed
          <input type="number" min={0} max={2147483647} value={settings.seed} disabled={active} onChange={(event) => setSettings({ ...settings, seed: Number(event.target.value) })} />
        </label>
      </div>
      {settings.realVehiclesPerSimulatedVehicle > 1 ? (
        <p className="diversion-warning">Scaled traffic runs faster but reduces physical density. Use 1:1 for congestion decisions.</p>
      ) : null}
      {active && run.data ? (
        <div className="diversion-progress" role="status">
          <div><strong>{Math.round(run.data.progress * 100)}%</strong><span>{run.data.current_sim_second === null ? 'Preparing demand' : 'Computing baseline and closure traffic'}</span></div>
          <progress max={1} value={run.data.progress} />
          <button type="button" disabled={canceller.isPending} onClick={() => canceller.mutate()}><X size={14} /> Cancel</button>
        </div>
      ) : null}
      <button className="primary-action diversion-run" type="button" disabled={active || starter.isPending} onClick={() => starter.mutate()}>
        {active || starter.isPending ? <LoaderCircle className="spin" size={17} /> : <Play size={17} />}
        {active || starter.isPending ? 'Running physical traffic…' : run.data?.status === 'completed' ? 'Run again' : 'Run physical simulation'}
      </button>
      {run.data?.status === 'cancelled' ? <div className="diversion-notice"><Ban size={16} /> The physical run was cancelled safely.</div> : null}
      {error ? <div className="inline-error" role="alert">{error}</div> : null}
      {run.data?.summary ? <SimulationMetrics status={run.data} /> : null}
    </section>
  )
}

function SimulationMetrics({ status }: { status: SimulationRunStatus }) {
  const summary = status.summary
  if (!summary) return null
  const baseline = summary.baseline.selected_trip
  const scenario = summary.scenario.selected_trip
  return (
    <div className="diversion-results">
      <div className="diversion-summary-grid">
        <Metric label="Normal trip" value={duration(baseline?.duration)} />
        <Metric label="Closure trip" value={duration(scenario?.duration)} />
        <Metric label="Closure difference" value={signedDuration(summary.comparison.selected_trip_delta_seconds)} />
        <Metric label="Scenario teleports" value={summary.scenario.teleport_count.toLocaleString()} />
        <Metric label="Simulated vehicles" value={summary.scenario.departed_vehicle_count.toLocaleString()} />
        <Metric label="Changed roads" value={summary.comparison.edge_changes.length.toLocaleString()} />
        <Metric label="Free-flow floor" value={duration(summary.free_flow_validation.network_free_flow_seconds)} />
      </div>
      <p className="result-note">Local proxy {summary.demand_model_version} · seed {summary.seed} · physical scale {summary.real_vehicles_per_simulated_vehicle}:1</p>
      <ul className="assumption-list">{summary.assumptions.map((item) => <li key={item}>{item}</li>)}</ul>
    </div>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div><strong>{value}</strong><span>{label}</span></div>
}

function duration(seconds: number | undefined): string {
  return seconds === undefined ? 'Did not finish' : `${Math.round(seconds / 60)} min`
}

function signedDuration(seconds: number | null): string {
  if (seconds === null) return 'Unavailable'
  const minutes = Math.round(seconds / 60)
  return `${minutes >= 0 ? '+' : ''}${minutes} min`
}
