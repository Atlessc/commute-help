import Dexie, { type EntityTable } from 'dexie'
import type {
  ClosureSectionDraft,
  SelectedLocation,
} from '../api/routing'
import type { ScenarioMapState } from '../api/scenarios'
import type { ReliabilitySettings } from '../api/traffic'
import type { DiversionSettings } from '../api/diversion'

export type BrowserDraft = {
  key: 'planner'
  updatedAt: string
  name: string
  scenarioId: string | null
  scenarioRevision: number | null
  origin: SelectedLocation | null
  destination: SelectedLocation | null
  departureTime: string
  closureSections: ClosureSectionDraft[]
  selectedClosureEdgeIds: string[]
  selectedRouteId: string | null
  mapState: ScenarioMapState | null
  reliabilitySettings?: ReliabilitySettings
  diversionSettings?: DiversionSettings
}

const draftDatabase = new Dexie('commute-help-browser') as Dexie & {
  drafts: EntityTable<BrowserDraft, 'key'>
}

draftDatabase.version(1).stores({ drafts: 'key,updatedAt' })

export async function readPlannerDraft(): Promise<BrowserDraft | undefined> {
  return draftDatabase.drafts.get('planner')
}

export async function writePlannerDraft(
  draft: Omit<BrowserDraft, 'key' | 'updatedAt'>,
): Promise<void> {
  await draftDatabase.drafts.put({
    ...draft,
    key: 'planner',
    updatedAt: new Date().toISOString(),
  })
}

export async function clearPlannerDraft(): Promise<void> {
  await draftDatabase.drafts.delete('planner')
}
