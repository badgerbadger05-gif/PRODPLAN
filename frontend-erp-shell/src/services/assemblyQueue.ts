import type { AssemblyQueueResponse } from '../domain/assemblyQueue'
import { api } from '../lib/api'

export function listAssemblyQueue() {
  return api<AssemblyQueueResponse>('/v1/production-control/assembly-queue')
}
