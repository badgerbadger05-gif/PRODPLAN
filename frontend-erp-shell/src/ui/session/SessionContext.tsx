/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { mockSessionProvider } from './mockSessionProvider'
import type { SessionProvider, SessionUser } from './types'
import { onApiUnauthorized } from '../../lib/api'

type SessionState = {
  user: SessionUser | null
  loading: boolean
  reason: string
  login(login: string, password: string): Promise<void>
  logout(): Promise<void>
}

const SessionContext = createContext<SessionState | null>(null)

export function SessionRoot({
  children,
  provider = mockSessionProvider,
}: {
  children: ReactNode
  provider?: SessionProvider
}) {
  const [user, setUser] = useState<SessionUser | null>(null)
  const [loading, setLoading] = useState(true)
  const [reason, setReason] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    void provider.load(controller.signal)
      .then(setUser)
      .catch(() => {
        if (!controller.signal.aborted) setUser(null)
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [provider])

  useEffect(() => onApiUnauthorized(() => {
    setReason('Сессия истекла. Войдите снова.')
    setUser(null)
  }), [])

  const value = useMemo<SessionState>(() => ({
    user,
    loading,
    reason,
    async login(login, password) {
      setLoading(true)
      setReason('')
      try {
        setUser(await provider.login(login, password))
      } finally {
        setLoading(false)
      }
    },
    async logout() {
      await provider.logout()
      setReason('')
      setUser(null)
    },
  }), [loading, provider, reason, user])

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
}

export function useSession() {
  const session = useContext(SessionContext)
  if (!session) throw new Error('useSession must be used inside SessionRoot')
  return session
}

export function useOptionalSession() {
  return useContext(SessionContext)
}
