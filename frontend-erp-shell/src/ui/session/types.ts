import type { AccessSubject } from '../doctype/permissions'

export type SessionUser = AccessSubject & {
  id: string
  name: string
}

export interface SessionProvider {
  load(signal?: AbortSignal): Promise<SessionUser | null>
  login(login: string, password: string, signal?: AbortSignal): Promise<SessionUser>
  logout(signal?: AbortSignal): Promise<void>
}
