import { useState, type FormEvent } from 'react'
import { Crosshair, LoaderCircle, MapPin, Navigation, X } from 'lucide-react'
import type { SelectedLocation } from '../../api/routing'
import type { SelectionMode } from '../../stores/tripStore'

type TripPanelProps = {
  origin: SelectedLocation | null
  destination: SelectedLocation | null
  selectionMode: SelectionMode
  selectionPending: boolean
  routePending: boolean
  error: string | null
  onSelectMode: (mode: Exclude<SelectionMode, null>) => void
  onClear: (mode: Exclude<SelectionMode, null>) => void
  onCoordinateSubmit: (coordinate: { lat: number; lng: number }) => void
  onCalculate: () => void
}

export function TripPanel({
  origin,
  destination,
  selectionMode,
  selectionPending,
  routePending,
  error,
  onSelectMode,
  onClear,
  onCoordinateSubmit,
  onCalculate,
}: TripPanelProps) {
  const canCalculate = origin !== null && destination !== null

  return (
    <section className="trip-panel" aria-labelledby="trip-step-title">
      <div className="step-kicker">Step 1 of 6</div>
      <h1 id="trip-step-title">Choose your trip</h1>
      <p className="panel-intro">
        Select Point A and Point B on the map. Each point is snapped to the
        nearest road in the local routing graph.
      </p>

      <div className="location-list">
        <LocationField
          point="A"
          title="Origin"
          location={origin}
          active={selectionMode === 'origin'}
          disabled={selectionPending}
          onSelect={() => onSelectMode('origin')}
          onClear={() => onClear('origin')}
        />
        <div className="location-connector" aria-hidden="true" />
        <LocationField
          point="B"
          title="Destination"
          location={destination}
          active={selectionMode === 'destination'}
          disabled={selectionPending}
          onSelect={() => onSelectMode('destination')}
          onClear={() => onClear('destination')}
        />
      </div>

      {selectionMode ? (
        <CoordinateForm
          key={selectionMode}
          selectionMode={selectionMode}
          disabled={selectionPending}
          onSubmit={onCoordinateSubmit}
        />
      ) : null}

      {error ? (
        <div className="inline-error" role="alert">
          {error}
        </div>
      ) : null}

      <button
        className="primary-action"
        type="button"
        disabled={!canCalculate || routePending || selectionPending}
        onClick={onCalculate}
      >
        {routePending ? (
          <LoaderCircle className="spin" size={18} aria-hidden="true" />
        ) : (
          <Navigation size={18} aria-hidden="true" />
        )}
        {routePending ? 'Calculating route…' : 'Calculate normal route'}
      </button>
    </section>
  )
}

type LocationFieldProps = {
  point: 'A' | 'B'
  title: string
  location: SelectedLocation | null
  active: boolean
  disabled: boolean
  onSelect: () => void
  onClear: () => void
}

function LocationField({
  point,
  title,
  location,
  active,
  disabled,
  onSelect,
  onClear,
}: LocationFieldProps) {
  return (
    <div className={`location-field ${active ? 'location-field--active' : ''}`}>
      <span className={`point-marker point-marker--${point.toLowerCase()}`}>{point}</span>
      <div className="location-copy">
        <span className="location-title">{title}</span>
        <strong>{location?.label ?? `Set Point ${point}`}</strong>
        {location ? (
          <small>
            {location.distance_m.toFixed(0)} m from click · {location.edge.road_class}
          </small>
        ) : (
          <small>Not selected</small>
        )}
      </div>
      {location ? (
        <button
          className="icon-button"
          type="button"
          aria-label={`Clear ${title.toLowerCase()}`}
          onClick={onClear}
        >
          <X size={17} aria-hidden="true" />
        </button>
      ) : (
        <button
          className="map-pick-button"
          type="button"
          disabled={disabled}
          onClick={onSelect}
        >
          <Crosshair size={16} aria-hidden="true" />
          Map
        </button>
      )}
    </div>
  )
}

type CoordinateFormProps = {
  selectionMode: Exclude<SelectionMode, null>
  disabled: boolean
  onSubmit: (coordinate: { lat: number; lng: number }) => void
}

function CoordinateForm({
  selectionMode,
  disabled,
  onSubmit,
}: CoordinateFormProps) {
  const [latitude, setLatitude] = useState('')
  const [longitude, setLongitude] = useState('')

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const lat = Number(latitude)
    const lng = Number(longitude)
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return
    onSubmit({ lat, lng })
  }

  return (
    <details className="coordinate-entry">
      <summary>
        <MapPin size={15} aria-hidden="true" /> Enter coordinates instead
      </summary>
      <form onSubmit={handleSubmit}>
        <label>
          Latitude for {selectionMode === 'origin' ? 'Point A' : 'Point B'}
          <input
            type="number"
            min="45.25"
            max="45.85"
            step="any"
            required
            value={latitude}
            onChange={(event) => setLatitude(event.target.value)}
            placeholder="45.52"
          />
        </label>
        <label>
          Longitude
          <input
            type="number"
            min="-123.05"
            max="-122.25"
            step="any"
            required
            value={longitude}
            onChange={(event) => setLongitude(event.target.value)}
            placeholder="-122.68"
          />
        </label>
        <button type="submit" disabled={disabled}>
          Use coordinates
        </button>
      </form>
    </details>
  )
}
