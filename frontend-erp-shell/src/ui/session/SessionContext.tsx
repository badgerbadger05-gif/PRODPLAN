/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { mockSessionProvider } from './mockSessionProvider'
import type { SessionProvider, SessionUser } from './types'

type SessionState = {
  user: SessionUser | null
  loading: boolean
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

  const value = useMemo<SessionState>(() => ({
    user,
    loading,
    async login(login, password) {
      setLoading(true)
      try {
        setUser(await provider.login(login, password))
      } finally {
        setLoading(false)
      }
    },
    async logout() {
      await provider.logout()
      setUser(null)
    },
  }), [loading, provider, user])

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
