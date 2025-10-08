import { createI18n } from 'vue-i18n'
import { ru } from './ru'

export const i18n = createI18n({
  legacy: false,
  globalInjection: true,
  locale: 'ru',
  fallbackLocale: 'ru',
  messages: { ru }
})

export type AppMessageSchema = typeof ru