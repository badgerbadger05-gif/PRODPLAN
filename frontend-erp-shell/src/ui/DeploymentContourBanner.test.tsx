import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

describe('DeploymentContourBanner', () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllEnvs()
    vi.resetModules()
  })

  it('is absent from normal builds without a contour', async () => {
    vi.stubEnv('VITE_DEPLOYMENT_CONTOUR', '')
    const { DeploymentContourBanner } = await import('./DeploymentContourBanner')
    render(<DeploymentContourBanner />)
    expect(screen.queryByText('ПАРАЛЛЕЛЬНЫЙ КОНТУР')).not.toBeInTheDocument()
  })

  it('marks parallel builds and links directly to stable PRODPLAN', async () => {
    vi.stubEnv('VITE_DEPLOYMENT_CONTOUR', 'SHADOW')
    vi.stubEnv('VITE_STABLE_PRODPLAN_URL', 'https://stable.example/')
    const { DeploymentContourBanner } = await import('./DeploymentContourBanner')
    render(<DeploymentContourBanner />)

    expect(screen.getByText('ПАРАЛЛЕЛЬНЫЙ КОНТУР')).toBeInTheDocument()
    expect(screen.getByText('SHADOW')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Открыть стабильную PRODPLAN' })).toHaveAttribute('href', 'https://stable.example/')
  })
})
