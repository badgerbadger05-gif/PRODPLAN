import type { DrumResponse } from '../domain/drum'
import { api } from '../lib/api'

export function listDrum() {
  return api<DrumResponse>('/v1/production-control/drum')
}
