import type { DoctypePermissions, Permission, Role } from './types'

export type AccessSubject = {
  roles: Role[]
  permissions: Permission[]
}

function hasGrant(subject: AccessSubject, grant: Role | Permission) {
  return subject.roles.includes(grant as Role) || subject.permissions.includes(grant)
}

export function canView(permissions: DoctypePermissions, subject: AccessSubject) {
  if (!permissions.view?.length) return true
  return permissions.view.some((grant) => hasGrant(subject, grant))
}

export function canRunAction(
  permissions: DoctypePermissions,
  actionKey: string,
  subject: AccessSubject,
) {
  const required = permissions.actions?.[actionKey]
  if (!required) return true
  const grants = Array.isArray(required) ? required : [required]
  return grants.some((grant) => hasGrant(subject, grant))
}
