export type HealthResponse = {
  status: 'ok'
  application: string
  database: 'ok'
}

export type GraphRuntimeStatus = {
  status: 'not_configured' | 'ready' | 'error'
  version: string | null
  nodes: number | null
  directed_edges: number | null
  message: string | null
}

export type StatusResponse = {
  status: 'ok'
  application: string
  version: string
  environment: string
  database: 'ok'
  graph: GraphRuntimeStatus
}

export async function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const response = await fetch('/api/health', { signal })

  if (!response.ok) {
    throw new Error('The local API did not return a healthy response.')
  }

  return response.json() as Promise<HealthResponse>
}

export async function getStatus(signal?: AbortSignal): Promise<StatusResponse> {
  const response = await fetch('/api/status', { signal })

  if (!response.ok) {
    throw new Error('The local API did not return its readiness status.')
  }

  return response.json() as Promise<StatusResponse>
}
