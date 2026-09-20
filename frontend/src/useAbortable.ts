import React from 'react';

/**
 * AbortController-based request helper.
 *
 * Each call receives a unique signal that is automatically aborted
 * when the component unmounts or the deps change.
 */

export function useAbortable<T extends (...args: any[]) => Promise<any>>(
  fn: T
): [T, () => void] {
  const controllerRef = React.useRef<AbortController | null>(null);

  const wrapped = React.useCallback(
    async (...args: Parameters<T>) => {
      // Abort any in-flight request
      controllerRef.current?.abort();
      const ctrl = new AbortController();
      controllerRef.current = ctrl;
      return fn(...args, ctrl.signal);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [fn]
  );

  const cleanup = React.useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
  }, []);

  return [wrapped as T, cleanup];
}

/** Cleanup helper for useEffect: call with the returned cleanup function. */
export function useRequestCleanup(cleanup: (() => void) | undefined) {
  React.useEffect(() => cleanup, [cleanup]);
}
