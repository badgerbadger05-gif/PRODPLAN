import { boot } from 'quasar/wrappers'
import axios, { AxiosInstance } from 'axios'

declare module '@vue/runtime-core' {
  interface ComponentCustomProperties {
    $axios: AxiosInstance
  }
}

// Be careful when using SSR for cross-request state pollution
// due to creating a Singleton instance here;
// If any client changes this (global) instance, it might be a
// good idea to move this instance creation inside of the
// "export default () => {}" function below (which runs individually
// for each client)
/**
 * Определение базового URL API:
 * - В DEV (локальный фронтенд на 9000/localhost) направляем запросы на локальный backend: http://localhost:8000/api
 * - В PROD (через nginx) используем относительный путь '/api'
 * - Возможна переопределение через window.__API_URL__ (например, при нестандартной среде)
 * Без import.meta.env — чтобы не зависеть от настроек tsconfig/module.
 */
const dev =
  typeof window !== 'undefined' &&
  (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1')

const apiBase: string =
  (dev ? (((window as any).__API_URL__ as string) || 'http://localhost:8000/api') : '/api')

const api = axios.create({ baseURL: apiBase, timeout: 900000 })

export default boot(({ app }) => {
  // for use inside Vue files (Options API) through this.$axios and this.$api

  app.config.globalProperties.$axios = axios
  // ^ ^ ^ this will allow you to use this.$axios (for Vue Options API form)
  //       so you won't necessarily have to import axios in each vue file

  app.config.globalProperties.$api = api
  // ^ ^ ^ this will allow you to use this.$api (for Vue Options API form)
  //       so you can easily perform requests against your app's API
})

export { api }