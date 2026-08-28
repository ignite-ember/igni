/**
 * Real-time retry banner that appears when HTTP requests are being retried
 * and automatically dismisses on success.
 *
 * Listens for "ember:http_retry_attempt" and "ember:http_retry_succeeded"
 * window events and mounts a transient banner.
 *
 * The banner shows:
 * - Current attempt number and retry status
 * - Exponential backoff countdown (visual feedback)
 *
 * Stacks vertically if multiple retries occur simultaneously (rare but possible).
 */

import { useEffect, useState } from "react";
import "./RetryBanner.css";

interface RetryEvent {
  id: string;
  attempt: number;
  delay_seconds: number;
  url?: string;
  timestamp: number;
}

export function RetryBanner() {
  const [retries, setRetries] = useState<Map<string, RetryEvent>>(new Map());

  // Demo mode: trigger with ?demo=retry in URL
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("demo") === "retry") {
      // Simulate a retry sequence
      const retryEvent: RetryEvent = {
        id: `demo-retry-${Date.now()}`,
        attempt: 2,
        delay_seconds: 1.5,
        url: "https://api.example.com/data",
        timestamp: Date.now(),
      };
      setRetries(new Map([["demo", retryEvent]]));

      setTimeout(() => {
        setRetries(new Map());
      }, 3000);
    }
  }, []);

  useEffect(() => {
    const handleRetryAttempt = (e: Event) => {
      const event = e as CustomEvent<{ attempt: number; delay_seconds: number; url?: string }>;
      const { attempt, delay_seconds, url } = event.detail || {};

      const retryEvent: RetryEvent = {
        id: `retry-${Date.now()}-${Math.random()}`,
        attempt: attempt || 2,
        delay_seconds: delay_seconds || 0.1,
        url,
        timestamp: Date.now(),
      };

      setRetries((prev) => new Map(prev).set(retryEvent.id, retryEvent));

      // Auto-dismiss after the delay + a small buffer for the request to complete
      const dismissDelay = (delay_seconds + 1) * 1000;
      const timeout = setTimeout(() => {
        setRetries((prev) => {
          const next = new Map(prev);
          next.delete(retryEvent.id);
          return next;
        });
      }, dismissDelay);

      // Store timeout ID for cleanup if needed
      (retryEvent as any)._timeout = timeout;
    };

    const handleRetrySucceeded = () => {
      setRetries(new Map());
    };

    window.addEventListener("ember:http_retry_attempt", handleRetryAttempt as EventListener);
    window.addEventListener("ember:http_retry_succeeded", handleRetrySucceeded);

    return () => {
      window.removeEventListener("ember:http_retry_attempt", handleRetryAttempt as EventListener);
      window.removeEventListener("ember:http_retry_succeeded", handleRetrySucceeded);
    };
  }, []);

  if (retries.size === 0) {
    return null;
  }

  const retryArray = Array.from(retries.values()).sort(
    (a, b) => a.timestamp - b.timestamp,
  );

  return (
    <div className="retry-banner-container">
      {retryArray.map((retry) => (
        <RetryBannerItem key={retry.id} retry={retry} />
      ))}
    </div>
  );
}

function RetryBannerItem({ retry }: { retry: RetryEvent }) {
  const [countdownPercent, setCountdownPercent] = useState(100);

  useEffect(() => {
    const startTime = retry.timestamp;
    const delayMs = retry.delay_seconds * 1000;

    const interval = setInterval(() => {
      const elapsed = Date.now() - startTime;
      const remaining = Math.max(0, delayMs - elapsed);
      const percent = (remaining / delayMs) * 100;
      setCountdownPercent(percent);

      if (percent === 0) {
        clearInterval(interval);
      }
    }, 50);

    return () => clearInterval(interval);
  }, [retry]);

  return (
    <div className="retry-banner">
      <div className="retry-banner-content">
        <span className="retry-banner-icon">⟳</span>
        <div className="retry-banner-text">
          <span className="retry-banner-label">
            Retrying request (attempt {retry.attempt}...)
          </span>
          {retry.url && <span className="retry-banner-url">{retry.url}</span>}
        </div>
      </div>
      <div className="retry-banner-progress">
        <div
          className="retry-banner-progress-bar"
          style={{ width: `${countdownPercent}%` }}
        />
      </div>
    </div>
  );
}
