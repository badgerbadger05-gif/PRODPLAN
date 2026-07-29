import type { AccessSubject, DoctypePermissions, Permission, Role } from './types'

export type { AccessSubject } from './types'

function hasGrant(subject: AccessSubject, grant: Role | Permission) {
  return subject.roles.includes(grant as Role)
    || subject.permissions.includes('*')
    || subject.permissions.includes(grant)
}

export function canViewRecord<Row>(
  permissions: DoctypePermissions<Row>,
  row: Row,
  subject: AccessSubject,
) {
  return permissions.recordView?.(row, subject) ?? true
}

export function canViewField(
  permissions: Pick<DoctypePermissions, 'fields'>,
  field: string,
  subject: AccessSubject,
) {
  const required = permissions.fields?.[field]
  if (!required) return true
  const grants = Array.isArray(required) ? required : [required]
  return grants.some((grant) => hasGrant(subject, grant))
}

export function canView(
  permissions: Pick<DoctypePermissions, 'view'>,
  subject: AccessSubject,
) {
  if (!permissions.view?.length) return true
  return permissions.view.some((grant) => hasGrant(subject, grant))
}

export function canRunAction(
  permissions: Pick<DoctypePermissions, 'actions'>,
  actionKey: string,
  subject: AccessSubject,
) {
  const required = permissions.actions?.[actionKey]
  if (!required) return true
  const grants = Array.isArray(required) ? required : [required]
  return grants.some((grant) => hasGrant(subject, grant))
}
