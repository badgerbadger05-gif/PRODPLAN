import { boot } from 'quasar/wrappers';
import { i18n } from 'src/i18n';

/**
 * Quasar boot file for Vue I18n.
 * Ensures i18n plugin is installed on the app instance before any route components mount.
 */
export default boot(({ app }) => {
  app.use(i18n);
});