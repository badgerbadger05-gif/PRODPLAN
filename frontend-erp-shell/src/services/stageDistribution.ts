import type { ResourceDistributionResponse } from '../domain/stageDistribution'
import { api } from '../lib/api'

export function calculateResourceDistribution() {
  return api<ResourceDistributionResponse>('/v1/resources/calculate_distribution', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}
