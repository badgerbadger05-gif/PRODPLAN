/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DEPLOYMENT_CONTOUR?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
