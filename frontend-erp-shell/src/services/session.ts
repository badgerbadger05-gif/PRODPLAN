import { onApiUnauthorized } from '../lib/api'

export function subscribeToSessionExpiry(listener: () => void) {
  return onApiUnauthorized(listener)
}
