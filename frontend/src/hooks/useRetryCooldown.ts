import { useCallback, useEffect, useState } from 'react';

export function useRetryCooldown() {
  const [retryAt, setRetryAt] = useState(0);
  const [now, setNow] = useState(Date.now);
  const startCooldown = useCallback((seconds: number) => {
    const time = Date.now();
    setNow(time);
    setRetryAt(time + seconds * 1000);
  }, []);
  const remainingSeconds = Math.max(0, Math.ceil((retryAt - now) / 1000));

  useEffect(() => {
    if (remainingSeconds === 0) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [remainingSeconds]);

  return { remainingSeconds, startCooldown };
}
