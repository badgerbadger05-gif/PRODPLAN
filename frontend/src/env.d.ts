/// <reference types="vite/client" />

// Fix TS: ImportMeta.env typing (some TS configs may miss vite/client)
interface ImportMetaEnv {
  readonly BASE_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

