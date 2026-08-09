import { Pause, Play, RotateCcw } from 'lucide-react'
import type { DiversionResult } from '../../api/diversion'

type SimulationPlaybackProps = {
  result: DiversionResult
  elapsedSeconds: number
  playing: boolean
  speed: number
  departureTime: string
  onElapsedChange: (seconds: number) => void
  onPlayingChange: (playing: boolean) => void
  onSpeedChange: (speed: number) => void
}

const DURATION_SECONDS = 60 * 60

export function SimulationPlayback({
  result,
  elapsedSeconds,
  playing,
  speed,
  departureTime,
  onElapsedChange,
  onPlayingChange,
  onSpeedChange,
}: SimulationPlaybackProps) {
  const simulatedTime = new Date(new Date(departureTime).getTime() + elapsedSeconds * 1000)
  return (
    <section className="simulation-playback" aria-label="Simulation playback controls">
      <div className="simulation-transport">
        <button
          type="button"
          className="transport-button transport-button--secondary"
          onClick={() => {
            onPlayingChange(false)
            onElapsedChange(0)
          }}
        >
          <RotateCcw size={19} aria-hidden="true" />
          <span>Restart</span>
        </button>
        <button
          type="button"
          className="transport-button transport-button--primary"
          onClick={() => onPlayingChange(!playing)}
        >
          {playing ? <Pause size={23} fill="currentColor" /> : <Play size={23} fill="currentColor" />}
          <span>{playing ? 'Pause' : 'Play'}</span>
        </button>
      </div>

      <div className="simulation-clock">
        <small>Current time (simulated)</small>
        <strong>{simulatedTime.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</strong>
        <span>{simulatedTime.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' })}</span>
      </div>

      <div className="simulation-timeline">
        <div className="timeline-event"><i />Closure model active</div>
        <input
          aria-label="Simulation timeline"
          type="range"
          min={0}
          max={DURATION_SECONDS}
          step={1}
          value={elapsedSeconds}
          onChange={(event) => onElapsedChange(Number(event.target.value))}
        />
        <div className="timeline-labels" aria-label="Jump to simulated time">
          {[0, 15, 30, 45, 60].map((minutes) => (
            <button
              type="button"
              key={minutes}
              onClick={() => onElapsedChange(minutes * 60)}
            >
              {minutes}m
            </button>
          ))}
        </div>
      </div>

      <div className="simulation-elapsed">
        <small>Elapsed</small>
        <strong>{Math.floor(elapsedSeconds / 60)}m</strong>
        <span>of 60m</span>
      </div>

      <label className="simulation-speed">
        <span>Playback speed <strong>{Math.round(speed)}×</strong></span>
        <input
          aria-label="Playback speed"
          type="range"
          min={1}
          max={50}
          step={1}
          value={speed}
          onChange={(event) => onSpeedChange(Number(event.target.value))}
        />
        <small><span>1×</span><span>10×</span><span>20×</span><span>50×</span></small>
        <span className="simulation-speed-presets" aria-label="Playback speed presets">
          {[1, 5, 10, 20, 50].map((preset) => (
            <button
              type="button"
              key={preset}
              className={Math.round(speed) === preset ? 'is-active' : ''}
              aria-pressed={Math.round(speed) === preset}
              onClick={() => onSpeedChange(preset)}
            >
              {preset}×
            </button>
          ))}
        </span>
      </label>

      <div className="simulation-status">
        <i />
        {playing ? `Playback running · ${Math.round(speed)}×` : 'Simulation computed · ready to play'}
        <span>Modeled · uncalibrated · {result.input_hash.slice(0, 8)}</span>
      </div>
    </section>
  )
}
