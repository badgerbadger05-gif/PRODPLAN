import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { HashRouter } from 'react-router-dom'
import { App } from './ui/App'
import { SessionRoot } from './ui/session'
import './styles.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <HashRouter>
      <SessionRoot>
        <App />
      </SessionRoot>
    </HashRouter>
  </StrictMode>,
)
