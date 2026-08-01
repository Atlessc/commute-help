import {
  Archive,
  Copy,
  Download,
  FileUp,
  LoaderCircle,
  Save,
} from 'lucide-react'
import type {
  ScenarioListItem,
  ScenarioRecord,
} from '../../api/scenarios'

type ScenarioPanelProps = {
  scenarios: ScenarioListItem[]
  current: ScenarioRecord | null
  name: string
  dirty: boolean
  canSave: boolean
  busy: boolean
  error: string | null
  onNameChange: (name: string) => void
  onSave: () => void
  onLoad: (id: string) => void
  onDuplicate: () => void
  onArchive: () => void
  onExport: () => void
  onImport: (value: unknown) => void
}

export function ScenarioPanel({
  scenarios,
  current,
  name,
  dirty,
  canSave,
  busy,
  error,
  onNameChange,
  onSave,
  onLoad,
  onDuplicate,
  onArchive,
  onExport,
  onImport,
}: ScenarioPanelProps) {
  async function readImport(file: File | undefined) {
    if (!file) return
    try {
      onImport(JSON.parse(await file.text()) as unknown)
    } catch {
      onImport(null)
    }
  }

  return (
    <section className="scenario-panel" aria-labelledby="scenario-title">
      <div className="step-kicker">Step 6 of 6</div>
      <h2 id="scenario-title">Save and share this plan</h2>
      <p className="panel-intro">
        Saved scenarios live in the Mac's shared SQLite database and are visible
        to other trusted-LAN browsers.
      </p>

      <label className="scenario-name-field">
        Scenario name
        <input
          value={name}
          maxLength={120}
          onChange={(event) => onNameChange(event.target.value)}
          placeholder="Morning bridge closure"
        />
      </label>
      <button
        className="primary-action"
        type="button"
        disabled={busy || !canSave || !name.trim()}
        onClick={onSave}
      >
        {busy ? <LoaderCircle className="spin" size={17} /> : <Save size={17} />}
        {current ? `Save revision ${current.revision + 1}` : 'Save new scenario'}
      </button>
      {current ? (
        <div className="current-scenario-meta">
          <strong>{current.name}</strong>
          <span>
            Revision {current.revision}{dirty ? ' · Unsaved changes' : ' · Saved'}
          </span>
          {current.warnings.map((warning) => (
            <small key={warning} className="scenario-warning">{warning}</small>
          ))}
        </div>
      ) : null}

      <div className="scenario-actions">
        <button type="button" disabled={!current || busy} onClick={onDuplicate}>
          <Copy size={15} /> Duplicate
        </button>
        <button type="button" disabled={!current || busy} onClick={onExport}>
          <Download size={15} /> Export
        </button>
        <button type="button" disabled={!current || busy} onClick={onArchive}>
          <Archive size={15} /> Archive
        </button>
        <label className="scenario-import">
          <FileUp size={15} /> Import
          <input
            type="file"
            accept="application/json,.json"
            onChange={(event) => {
              void readImport(event.target.files?.[0])
              event.target.value = ''
            }}
          />
        </label>
      </div>

      {error ? <div className="inline-error" role="alert">{error}</div> : null}

      <div className="saved-scenarios">
        <strong>Shared scenarios</strong>
        {scenarios.length ? (
          scenarios.map((scenario) => (
            <button
              type="button"
              key={scenario.id}
              className={scenario.id === current?.id ? 'saved-scenario--active' : ''}
              onClick={() => onLoad(scenario.id)}
              disabled={busy}
            >
              <span>{scenario.name}</span>
              <small>
                Revision {scenario.revision} · {formatUpdated(scenario.updated_at)}
              </small>
            </button>
          ))
        ) : (
          <small>No shared scenarios saved yet.</small>
        )}
      </div>
    </section>
  )
}

export function DraftRecovery({
  updatedAt,
  onRecover,
  onDiscard,
}: {
  updatedAt: string
  onRecover: () => void
  onDiscard: () => void
}) {
  return (
    <div className="draft-recovery" role="status">
      <div>
        <strong>Recover unsaved browser draft?</strong>
        <span>Last changed {formatUpdated(updatedAt)}</span>
      </div>
      <button type="button" onClick={onRecover}>Recover</button>
      <button type="button" onClick={onDiscard}>Discard</button>
    </div>
  )
}

function formatUpdated(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}
