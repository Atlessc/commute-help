import { Check, Copy, ExternalLink, Route } from 'lucide-react'
import { useState } from 'react'
import type {
  AlternativeRoute,
  GoogleMapsUrlResponse,
} from '../../api/routing'

type RouteAlternativesPanelProps = {
  alternatives: AlternativeRoute[]
  selectedRouteId: string | null
  loading: boolean
  error: string | null
  handoff: GoogleMapsUrlResponse | undefined
  handoffLoading: boolean
  handoffError: string | null
  onFind: () => void
  onSelect: (routeId: string) => void
}

const RANKING_LABELS = {
  fastest: 'Fastest',
  reliable: 'Reliable proxy',
  balanced: 'Balanced',
}

export function RouteAlternativesPanel({
  alternatives,
  selectedRouteId,
  loading,
  error,
  handoff,
  handoffLoading,
  handoffError,
  onFind,
  onSelect,
}: RouteAlternativesPanelProps) {
  const [copied, setCopied] = useState(false)

  async function copyLink() {
    if (!handoff) return
    try {
      await navigator.clipboard.writeText(handoff.url)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1800)
    } catch {
      setCopied(false)
    }
  }

  return (
    <section className="alternatives-panel" aria-labelledby="alternatives-title">
      <div className="alternatives-heading">
        <div>
          <span className="result-kicker">Step 4 · Run and compare</span>
          <h2 id="alternatives-title">Choose a route corridor</h2>
        </div>
        <button type="button" onClick={onFind} disabled={loading}>
          <Route size={16} aria-hidden="true" />
          {loading
            ? 'Finding distinct routes…'
            : alternatives.length
              ? 'Refresh alternatives'
              : 'Find alternative routes'}
        </button>
      </div>

      {error ? <div className="panel-error" role="alert">{error}</div> : null}

      {alternatives.length ? (
        <>
          <div className="alternative-grid" aria-label="Available route corridors">
            {alternatives.map((alternative) => (
              <AlternativeCard
                key={alternative.route.route_id}
                alternative={alternative}
                selected={alternative.route.route_id === selectedRouteId}
                onSelect={onSelect}
              />
            ))}
          </div>
          {alternatives.length === 1 ? (
            <p className="alternatives-limit">
              No other route passed the meaningful-difference and travel-time limits.
            </p>
          ) : null}

          <div className="google-handoff">
            <div>
              <span className="result-kicker">Step 6 · Navigate</span>
              <strong>Continue in Google Maps</strong>
              <small>
                {handoffLoading
                  ? 'Preparing strategic waypoints…'
                  : handoff
                    ? `${handoff.waypoint_count} strategic waypoint${handoff.waypoint_count === 1 ? '' : 's'} will guide the selected corridor.`
                    : 'Preparing the selected corridor…'}
              </small>
            </div>
            <div className="handoff-actions">
              {handoff ? (
                <a href={handoff.url} target="_blank" rel="noreferrer">
                  <ExternalLink size={15} aria-hidden="true" />
                  Open in Google Maps
                </a>
              ) : null}
              <button type="button" onClick={copyLink} disabled={!handoff}>
                {copied ? <Check size={15} aria-hidden="true" /> : <Copy size={15} aria-hidden="true" />}
                {copied ? 'Copied' : 'Copy link'}
              </button>
            </div>
          </div>
          {handoffError ? <div className="panel-error" role="alert">{handoffError}</div> : null}
          {handoff ? <p className="handoff-warning">{handoff.warning}</p> : null}
        </>
      ) : (
        <p className="alternatives-empty">
          Compare meaningfully different corridors, then select one to preview it on the map.
        </p>
      )}
    </section>
  )
}

function AlternativeCard({
  alternative,
  selected,
  onSelect,
}: {
  alternative: AlternativeRoute
  selected: boolean
  onSelect: (routeId: string) => void
}) {
  return (
    <button
      type="button"
      className={`alternative-card ${selected ? 'alternative-card--selected' : ''}`}
      aria-pressed={selected}
      onClick={() => onSelect(alternative.route.route_id)}
    >
      <span className="alternative-rank">{RANKING_LABELS[alternative.ranking]}</span>
      <strong>
        {formatDuration(alternative.route.travel_time_seconds)} ·{' '}
        {formatDistance(alternative.route.distance_m)}
      </strong>
      <span>
        {alternative.overlap_with_fastest_percent.toFixed(0)}% fastest-route overlap
      </span>
      <span>
        {alternative.residential_distance_percent.toFixed(0)}% residential roads
      </span>
      <small>{alternative.description}</small>
    </button>
  )
}

function formatDuration(seconds: number): string {
  return `${Math.max(1, Math.round(seconds / 60))} min`
}

function formatDistance(meters: number): string {
  const miles = meters / 1609.344
  return `${miles.toFixed(miles < 10 ? 1 : 0)} mi`
}
