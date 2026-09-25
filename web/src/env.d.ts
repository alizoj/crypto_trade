/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "1" — mock-режим: API и WebSocket эмулируются в браузере */
  readonly VITE_MOCK?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
