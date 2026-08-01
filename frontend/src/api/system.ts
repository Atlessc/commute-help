export type HealthResponse = {
  status: 'ok'
  application: string
  database: 'ok'
}

export async function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const response = await fetch('/api/health', { signal })

  if (!response.ok) {
    throw new Error('The local API did not return a healthy response.')
  }

  return response.json() as Promise<HealthResponse>
}
