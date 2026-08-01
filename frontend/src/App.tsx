import { useQuery } from '@tanstack/react-query'
import { Check, Map, Route, ShieldCheck } from 'lucide-react'
import { getStatus } from './api/system'
import './App.css'

const workflow = [
  'Choose your trip',
  'Mark planned closures',
  'Compare reliable routes',
  'Save or open in Google Maps',
]

function App() {
  const system = useQuery({
    queryKey: ['system', 'status'],
    queryFn: ({ signal }) => getStatus(signal),
  })

  const statusLabel = system.isPending
    ? 'Connecting to the local service…'
    : system.isError
      ? 'Local service unavailable'
      : system.data.graph.status === 'ready'
        ? 'Regional road graph ready'
        : system.data.graph.status === 'error'
          ? 'Road graph needs attention'
          : 'Local foundation ready'

  const statusDetail = system.isError
    ? 'Start both services from the repository with npm run dev.'
    : system.data?.graph.status === 'ready'
      ? `${system.data.graph.nodes?.toLocaleString()} nodes loaded once for routing.`
      : system.data?.graph.status === 'error'
        ? system.data.graph.message
        : 'Run npm run graph:build to prepare the Phase 1 road network.'

  return (
    <main>
      <header className="site-header">
        <a className="brand" href="/" aria-label="Commute Help home">
          <span className="brand-mark" aria-hidden="true">
            <Route size={21} strokeWidth={2.4} />
          </span>
          Commute Help
        </a>
        <span className="local-badge">
          <ShieldCheck size={15} aria-hidden="true" /> Local-first
        </span>
      </header>

      <section className="hero" aria-labelledby="page-title">
        <div className="eyebrow">Portland–Vancouver trip planning</div>
        <h1 id="page-title">Plan around the road ahead.</h1>
        <p className="lede">
          Compare an everyday trip with closure-aware routes, understand arrival
          risk, and keep reusable plans on your own Mac.
        </p>

        <div
          className={`service-status ${system.isError || system.data?.graph.status === 'error' ? 'service-status--error' : ''}`}
          role="status"
        >
          <span className="status-dot" aria-hidden="true" />
          <span>
            <strong>{statusLabel}</strong>
            <small>
              {statusDetail}
            </small>
          </span>
        </div>
      </section>

      <section className="workflow" aria-labelledby="workflow-title">
        <div className="section-heading">
          <Map size={22} aria-hidden="true" />
          <h2 id="workflow-title">The planned workflow</h2>
        </div>
        <ol>
          {workflow.map((step, index) => (
            <li key={step}>
              <span className="step-number">{index + 1}</span>
              <span>{step}</span>
              <Check size={17} aria-hidden="true" />
            </li>
          ))}
        </ol>
      </section>
    </main>
  )
}

export default App
