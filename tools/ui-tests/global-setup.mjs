// One disposable instance for the run. Workers inherit the env set here; the returned function is the teardown,
// which also runs after failed tests. CI repeats `ui_instance.py stop` for every run directory with if: always().
import { startInstance } from "./instance.mjs";

export default async function globalSetup() {
  const instance = await startInstance();
  process.env.HUB_UI_DIR = instance.dir;
  process.env.HUB_UI_URL = instance.hub;
  process.env.HUB_UI_DAEMON_URL = instance.daemon;
  return async () => {
    console.log((await instance.stop()).trim());
  };
}
