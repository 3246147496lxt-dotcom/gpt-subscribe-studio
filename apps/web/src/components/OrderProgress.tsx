import { useEffect, useState } from "react";

import type { OrderFlow } from "../orderFlow";
import "./order-progress.css";

export interface CheckoutClock {
  startedAt: number;
  finishedAt: number | null;
}

export interface ProgressActivity {
  label: string;
  step?: OrderFlow["focus"];
}

function ElapsedTime({ clock }: { clock: CheckoutClock | null }) {
  const [now, setNow] = useState(Date.now);
  const startedAt = clock?.startedAt;
  const finishedAt = clock?.finishedAt;

  useEffect(() => {
    if (startedAt === undefined || finishedAt !== null) return;
    let timeout: number | undefined;
    const tick = () => {
      window.clearTimeout(timeout);
      if (document.hidden) return;
      const time = Date.now();
      setNow(time);
      const elapsed = Math.max(0, time - startedAt);
      timeout = window.setTimeout(tick, 1000 - (elapsed % 1000));
    };
    tick();
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearTimeout(timeout);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [startedAt, finishedAt]);

  const seconds = clock ? Math.max(0, Math.floor(((clock.finishedAt ?? now) - clock.startedAt) / 1000)) : 0;
  return (
    <span className="order-progress__elapsed" role="timer" aria-live="off">
      自创建结账起 <strong>{seconds} 秒</strong>
    </span>
  );
}

interface Props {
  progress: OrderFlow;
  clock: CheckoutClock | null;
  activity: ProgressActivity | null;
}

export function OrderProgress({ progress, clock, activity }: Props) {
  const currentStep = activity ? activity.step : progress.focus;
  return (
    <section id="order-progress" className="order-progress" aria-label="订阅流程">
      <header className="order-progress__heading">
        <h2>后端处理流程</h2>
        <ElapsedTime clock={clock} />
        <div className="order-progress__status" role="status" aria-live="polite" aria-atomic="true" data-tone={progress.tone}>
          <strong>
            {activity && <span className="order-progress__spinner" aria-hidden="true" />}
            {activity?.label ?? progress.label}
          </strong>
          {progress.detail && <p>{progress.detail}</p>}
        </div>
      </header>
      <ol className="order-progress__steps" role="list">
        {progress.steps.map((step, index) => {
          const working = activity?.step === step.id;
          return (
            <li key={step.id} data-state={working ? "active" : step.state} aria-current={currentStep === step.id ? "step" : undefined}>
              <div className="order-progress__step-heading">
                <span className="order-progress__marker" aria-hidden="true">
                  {working ? <span className="order-progress__spinner" /> : index + 1}
                </span>
                <h3>{step.title}</h3>
                <span className="order-progress__step-status">{working ? activity.label : step.status}</span>
              </div>
              <p className="order-progress__description">{step.description}</p>
              {step.evidence && <p className="order-progress__evidence">{step.evidence}</p>}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
