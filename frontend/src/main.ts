import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { Quasar } from 'quasar'
import quasarIconSet from 'quasar/icon-set/material-icons'
// Оставляем только один CSS импорт для предотвращения дублирования
import 'quasar/src/css/index.sass'

import App from './App.vue'
import router from './router'
import { i18n } from './i18n'
import { I18nInjectionKey } from 'vue-i18n'

const app = createApp(App)

// Важно: подключаем i18n до router, чтобы useI18n был доступен при монтировании route-компонентов
app.use(createPinia())
app.use(i18n)
// Дополнительно принудительно публикуем i18n в provide, чтобы useI18n() не падал даже до установки плагина
try {
  app.provide(I18nInjectionKey as any, (i18n as any).global)
} catch {}
app.use(router)
app.use(Quasar, {
  plugins: {}, // import Quasar plugins and add here
  iconSet: quasarIconSet,
})

app.mount('#q-app')