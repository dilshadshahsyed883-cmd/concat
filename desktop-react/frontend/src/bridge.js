export async function call(method, ...args) {
  if (typeof window.pywebview?.api?.[method] !== "function")
    await new Promise((resolve, reject) => {
      const done = () => {
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(() => {
        window.removeEventListener("pywebviewready", done);
        reject(
          new Error("Desktop bridge is unavailable. Launch Concat-React.exe."),
        );
      }, 15000);
      window.addEventListener("pywebviewready", done, { once: true });
    });
  const result = await window.pywebview.api[method](...args);
  if (!result.ok) throw new Error(result.error || "Operation failed");
  return result.data;
}
export const duration = (timeline) =>
  Math.max(0, ...(timeline?.clips || []).map((c) => c.start + c.duration));
export const stateFromReply = (reply) =>
  reply?.state?.view !== undefined ? reply.state : reply;
export const clipsAt = (timeline, time) =>
  (timeline?.clips || []).filter(
    (c) => time >= c.start && time < c.start + c.duration,
  );
export function snappedTime(seconds, rateNum = 30, rateDen = 1) {
  if (!Number.isFinite(seconds) || rateNum <= 0 || rateDen <= 0)
    throw new Error("Invalid timeline time or frame rate");
  return Math.max(
    0,
    (Math.round((seconds * rateNum) / rateDen) * rateDen) / rateNum,
  );
}
export const clock = (t) =>
  `${Math.floor(Math.max(0, t) / 60)
    .toString()
    .padStart(2, "0")}:${(Math.max(0, t) % 60).toFixed(2).padStart(5, "0")}`;
