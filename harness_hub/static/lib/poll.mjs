// Page poll timers that pause in a hidden tab. A poll that comes due while the tab is hidden does not run; when the
// tab becomes visible again the page loads once at once, and that load schedules the page's normal interval.
// A schedule made with whileHidden (a claim or restart the owner is waiting on elsewhere) keeps running when hidden.

export function poller(run) {
  let timer = null, paused = false, stopped = false;
  const hidden = () => document.visibilityState === "hidden";

  function cancel() {
    clearTimeout(timer);
    timer = null;
    paused = false;
  }
  function schedule(delay, { whileHidden = false } = {}) {
    cancel();
    if (stopped) return;
    timer = setTimeout(() => {
      timer = null;
      if (stopped) return;
      if (!whileHidden && hidden()) {
        paused = true;
        return;
      }
      return run();
    }, delay);
  }
  // Only a pending or paused poll resumes, so a load already in flight is never doubled.
  function resume() {
    if (stopped || hidden() || (timer === null && !paused)) return;
    cancel();
    run();
  }
  function stop() {
    stopped = true;
    cancel();
    document.removeEventListener("visibilitychange", resume);
  }
  document.addEventListener("visibilitychange", resume);
  return { schedule, cancel, stop };
}
