import type { Role } from '../doctype'
import type { SessionProvider, SessionUser } from './types'

const rolePermissions: Record<string, string[]> = {
  admin: ['*'],
  planner: ['plan.run', 'plan.reconcile', 'production.propose'],
  buyer: ['purchase.export_1c', 'purchase.sync_1c'],
  shopfloor: ['material_issue.assemble_post_1c'],
  viewer: [],
}

export function mockUser(role: Role = 'admin'): SessionUser {
  return {
    id: `mock-${role}`,
    name: `Демо · ${role}`,
    roles: [role],
    permissions: rolePermissions[role] ?? [],
  }
}

export function createMockSessionProvider(initialUser: SessionUser | null = mockUser()): SessionProvider {
  let current = initialUser
  return {
    async load() {
      return current
    },
    async login(login) {
      const role = login in rolePermissions ? login as Role : 'viewer'
      current = mockUser(role)
      return current
    },
    async logout() {
      current = null
    },
  }
}

export const mockSessionProvider = createMockSessionProvider()
