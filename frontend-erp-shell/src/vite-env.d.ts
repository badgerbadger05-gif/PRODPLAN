/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DEPLOYMENT_CONTOUR?: string
  readonly VITE_STABLE_PRODPLAN_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
