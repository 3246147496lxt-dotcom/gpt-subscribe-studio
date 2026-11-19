import type { Order } from "./client";

// A tab-local slot, not a cache of requests or receipts. No account-derived keys.
const SLOT = "gss.nonteam-purchase.v1";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
type Slot = Pick<Storage, "getItem" | "setItem" | "removeItem">;
type Intent = { kind: "pending" | "order"; id: string };
const unavailable = "当前标签页不能可靠地保存或读取购买标记，请先核对原单。";

export function readPurchaseIntent(storage: Slot): Intent | null {
  const value = storage.getItem(SLOT);
  if (value === null) return null;
  const [kind, id, ...rest] = value.split(":");
  if ((kind !== "pending" && kind !== "order") || !UUID.test(id) || rest.length) {
    throw new Error(unavailable);
  }
  return { kind, id };
}

export function forgetPurchase(storage: Slot): void {
  storage.removeItem(SLOT);
}

export async function openNonTeamPurchase(
  storage: Slot, create: (key: string) => Promise<Order>, lookup: (id: string) => Promise<Order>,
): Promise<Order> {
  const intent = readPurchaseIntent(storage);
  if (intent?.kind === "order") {
    // A missing or unreachable order is not permission to create a replacement.
    const order = await lookup(intent.id);
    if (order.id !== intent.id || order.plan === "team") throw new Error("查询结果与原订单不符。");
    return order;
  }
  const key = intent?.id ?? crypto.randomUUID();
  if (!intent) storage.setItem(SLOT, `pending:${key}`); // must succeed before POST
  const order = await create(key);
  if (!UUID.test(order.id) || order.plan === "team") throw new Error("无法识别建单回执，请用原购买标记核对。");

  // A late response must not overwrite another purchase selected in the meantime.
  const current = readPurchaseIntent(storage);
  if (current?.kind === "order" && current.id === order.id) return order;
  if (current?.kind !== "pending" || current.id !== key) throw new Error("购买标记已更改，请核对原单。");
  storage.setItem(SLOT, `order:${order.id}`);
  return order;
}
