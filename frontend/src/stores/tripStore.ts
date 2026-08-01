import { create } from 'zustand'
import type { SelectedLocation } from '../api/routing'

export type SelectionMode = 'origin' | 'destination' | null

type TripState = {
  origin: SelectedLocation | null
  destination: SelectedLocation | null
  selectionMode: SelectionMode
  setSelectionMode: (mode: SelectionMode) => void
  setLocation: (
    mode: Exclude<SelectionMode, null>,
    location: SelectedLocation,
  ) => void
  clearLocation: (mode: Exclude<SelectionMode, null>) => void
  restoreTrip: (origin: SelectedLocation, destination: SelectedLocation) => void
  replaceTrip: (
    origin: SelectedLocation | null,
    destination: SelectedLocation | null,
  ) => void
  resetTrip: () => void
}

export const useTripStore = create<TripState>((set) => ({
  origin: null,
  destination: null,
  selectionMode: 'origin',
  setSelectionMode: (selectionMode) => set({ selectionMode }),
  setLocation: (mode, location) =>
    set((state) => ({
      [mode]: location,
      selectionMode:
        mode === 'origin' && state.destination === null ? 'destination' : null,
    })),
  clearLocation: (mode) =>
    set({
      [mode]: null,
      selectionMode: mode,
    }),
  restoreTrip: (origin, destination) =>
    set({ origin, destination, selectionMode: null }),
  replaceTrip: (origin, destination) =>
    set({
      origin,
      destination,
      selectionMode: origin === null ? 'origin' : destination === null ? 'destination' : null,
    }),
  resetTrip: () =>
    set({ origin: null, destination: null, selectionMode: 'origin' }),
}))
