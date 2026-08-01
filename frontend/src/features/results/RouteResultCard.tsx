import { Clock3, Gauge, Route } from 'lucide-react'
import type { RouteCompareResponse } from '../../api/routing'

type RouteResultCardProps = {
  result: RouteCompareResponse
}

export function RouteResultCard({ result }: RouteResultCardProps) {
  const hasActiveRestriction = result.applied_restriction_edge_ids.length > 0
  const hasPlan =
    hasActiveRestriction || result.inactive_restriction_edge_ids.length > 0
  const modeled = result.evidence_level === 'modeled_uncalibrated'
  const scenario = result.scenario

  return (
    <section className="route-result" aria-labelledby="route-result-title">
      <div className="result-heading">
        <div>
          <span className="result-kicker">
            {hasPlan ? 'Route comparison' : 'Normal route'}
          </span>
          <h2 id="route-result-title">
            {hasPlan ? 'Normal vs. road impacts' : 'Free-flow baseline'}
          </h2>
        </div>
        <span className="evidence-badge">
          {result.scenario_status === 'inactive'
            ? 'No active restrictions'
            : modeled
              ? 'Modeled · uncalibrated'
              : 'Free-flow'}
        </span>
      </div>
      <div className="result-metrics">
        <div>
          <Clock3 size={18} aria-hidden="true" />
          <strong>{formatDuration(result.baseline.travel_time_seconds)}</strong>
          <span>Travel time</span>
        </div>
        <div>
          <Route size={18} aria-hidden="true" />
          <strong>{formatDistance(result.baseline.distance_m)}</strong>
          <span>Distance</span>
        </div>
        <div>
          <Gauge size={18} aria-hidden="true" />
          <strong>Uncalibrated</strong>
          <span>Traffic evidence</span>
        </div>
      </div>
      {hasPlan ? (
        result.scenario_status === 'inactive' ? (
          <div className="inactive-result" role="status">
            <Clock3 size={19} aria-hidden="true" />
            <div>
              <strong>No restrictions active at departure</strong>
              <span>{formatDeparture(result.evaluated_departure_time)}</span>
            </div>
          </div>
        ) : result.scenario_status === 'no_route' ? (
          <div className="no-route-result" role="status">
            <BanIcon />
            <div>
              <strong>No legal closure-aware route</strong>
              <span>
                The selected directed restrictions disconnect this trip in the
                local road graph.
              </span>
            </div>
          </div>
        ) : scenario ? (
          <div className="scenario-comparison">
            <div>
              <span>Impact-aware route</span>
              <strong>{formatDuration(scenario.travel_time_seconds)}</strong>
              <small>{formatDistance(scenario.distance_m)}</small>
            </div>
            <div>
              <span>Change</span>
              <strong>
                {formatDelta(
                  scenario.travel_time_seconds - result.baseline.travel_time_seconds,
                )}
              </strong>
              <small>
                {formatDistanceDelta(scenario.distance_m - result.baseline.distance_m)}
              </small>
            </div>
          </div>
        ) : null
      ) : null}
      <p className="result-note">
        {hasPlan
          ? result.scenario_status === 'inactive'
            ? 'The saved road-impact windows do not include this trip departure, so the baseline remains in effect.'
            : modeled
            ? 'This uncalibrated model applies the selected lane or speed costs to only the chosen directed road sections. It is not a live or historical traffic prediction.'
            : 'Both routes use free-flow costs. Only the selected fully closed directed road sections are removed; this is not a live traffic prediction.'
          : 'This is a road-network baseline, not a live or historical traffic prediction.'}
      </p>
    </section>
  )
}

function BanIcon() {
  return (
    <span className="no-route-icon" aria-hidden="true">
      ×
    </span>
  )
}

function formatDuration(seconds: number): string {
  const minutes = Math.max(1, Math.round(seconds / 60))
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const remainder = minutes % 60
  return `${hours} hr ${remainder} min`
}

function formatDistance(meters: number): string {
  const miles = meters / 1609.344
  return `${miles.toFixed(miles < 10 ? 1 : 0)} mi`
}

function formatDelta(seconds: number): string {
  const minutes = Math.round(seconds / 60)
  if (minutes === 0) return 'No time change'
  return `${minutes > 0 ? '+' : '−'}${Math.abs(minutes)} min`
}

function formatDistanceDelta(meters: number): string {
  const miles = Math.abs(meters / 1609.344)
  if (miles < 0.05) return 'No distance change'
  return `${meters > 0 ? '+' : '−'}${miles.toFixed(1)} mi`
}

function formatDeparture(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}
