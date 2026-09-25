import { useCallback, useRef } from 'react';

/**
 * Abortable fetch wrapper.
 *
 * Returns a memoized `fetchWithAbort` function that creates an `AbortController`
 * per call, forwards the signal through `RequestInit`, and exposes a
 * `cancel()` method so callers can abort in-flight requests during effect
 * cleanup or unmount.
 *
 * Usage:
 *   const fetchWithAbort = useAbortableFetch();
 *   useEffect(() => {
 *     let cancelled = false;
 *     fetchWithAbort('/api/v1/foo')
 *       .then(data => { if (!cancelled) setState(data); })
 *       .catch(() => {});
 *     return () => { fetchWithAbort.cancel(); cancelled = true; };
 *   }, [fetchWithAbort]);
 */

export function useAbortableFetch() {
  const controllerRef = useRef<AbortController | null>(null);
  const fetchWithAbort = useCallback(
    async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      return fetch(input, { ...init, signal: controller.signal });
    },
    []
  );
  const cancel = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
  }, []);
  return { fetchWithAbort, cancel };
}
