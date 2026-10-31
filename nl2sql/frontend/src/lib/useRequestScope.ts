import { useCallback, useEffect, useRef } from "react";

type ScopedRequest<T> = (signal: AbortSignal) => Promise<T>;

export function useRequestScope() {
  const controllers = useRef<Set<AbortController>>(new Set());

  const release = useCallback((controller: AbortController) => {
    controllers.current.delete(controller);
  }, []);

  const createSignal = useCallback(() => {
    const controller = new AbortController();
    controllers.current.add(controller);
    controller.signal.addEventListener("abort", () => release(controller), {
      once: true,
    });
    return controller.signal;
  }, [release]);

  const run = useCallback(
    async <T,>(request: ScopedRequest<T>): Promise<T> => {
      const controller = new AbortController();
      controllers.current.add(controller);
      try {
        return await request(controller.signal);
      } finally {
        release(controller);
      }
    },
    [release]
  );

  const abortAll = useCallback(() => {
    for (const controller of controllers.current) {
      controller.abort();
    }
    controllers.current.clear();
  }, []);

  useEffect(() => abortAll, [abortAll]);

  return { abortAll, createSignal, run };
}
