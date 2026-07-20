import { useState } from 'react'
import { useSession } from './SessionContext'

export function LoginPage() {
  const { login, loading } = useSession()
  const [name, setName] = useState('viewer')
  const [password, setPassword] = useState('')

  return (
    <main className="loginPage">
      <form
        className="loginCard"
        onSubmit={(event) => {
          event.preventDefault()
          void login(name, password)
        }}
      >
        <div className="brandMark">P</div>
        <h1>PRODPLAN</h1>
        <p>Вход в ERP shell</p>
        <label>Логин<input value={name} onChange={(event) => setName(event.target.value)} autoFocus /></label>
        <label>Пароль<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        <button className="primary" type="submit" disabled={loading}>{loading ? 'Вход...' : 'Войти'}</button>
        <small>Mock-режим: admin, planner, buyer, shopfloor или viewer</small>
      </form>
    </main>
  )
}
