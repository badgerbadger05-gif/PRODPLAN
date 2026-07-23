const contour = import.meta.env.VITE_DEPLOYMENT_CONTOUR?.trim()
const stableUrl = import.meta.env.VITE_STABLE_PRODPLAN_URL?.trim()

export function DeploymentContourBanner() {
  if (!contour) return null

  return (
    <div className="deploymentContourBanner" role="status">
      <strong>ПАРАЛЛЕЛЬНЫЙ КОНТУР</strong>
      <span>{contour}</span>
      {stableUrl && (
        <a href={stableUrl} target="_blank" rel="noreferrer">
          Открыть стабильную PRODPLAN
        </a>
      )}
    </div>
  )
}
