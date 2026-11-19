import assert from "node:assert/strict";
import test from "node:test";
import * as purchase from "../src/api/purchaseIntent";
import { createOrder, type Order } from "../src/api/client";

test("建单客户端原样传递调用方购买键，不为第二次点击换键", async () => {
  const body = { plan: "plus" as const, billing_country: "US", session: "synthetic-only" };
  const seen: string[] = [];
  const old = globalThis.fetch;
  try {
    globalThis.fetch = async (_, init) => {
      seen.push(new Headers(init?.headers).get("Idempotency-Key")!);
      return Response.json({ id: "fixture" } as Order);
    };
    const key = crypto.randomUUID();
    await createOrder(body, key);
    await createOrder(body, key);
    assert.deepEqual(seen, [key, key]);
  } finally { globalThis.fetch = old; }
});

function tab() {
  const values = new Map<string, string>();
  return {
    values,
    getItem: (k: string) => values.get(k) ?? null,
    setItem: (k: string, v: string) => { values.set(k, v); },
    removeItem: (k: string) => { values.delete(k); },
  };
}
const makeOrder = (): Order => ({
  id: crypto.randomUUID(), status: "awaiting_payment", plan: "plus", billing_country: "US",
});
const noLookup = async (): Promise<Order> => assert.fail("unexpected order lookup");
const noCreate = async (): Promise<Order> => assert.fail("unexpected create");

test("丢失首次成功回包；重载、冲突和重试沿用原键；知道单号后只读", async () => {
  const first = tab(); const order = makeOrder(); const keys: string[] = [];
  const open = (store: ReturnType<typeof tab>, cb: (key: string) => Promise<Order>) =>
    purchase.openNonTeamPurchase(store, async key => { keys.push(key); return cb(key); }, noLookup);
  await assert.rejects(open(first, async () => { throw Error("lost after create"); }), /lost/);
  // Reload: only sessionStorage content survives. No request body or account digest.
  const restored = tab();
  for (const [k,v] of first.values) restored.setItem(k,v);
  assert.deepEqual([...restored.values.values()], [`pending:${keys[0]}`]);
  await assert.rejects(open(restored, async () => { throw Error("409 different inputs"); }), /409/);
  assert.equal((await open(restored, async () => order)).id, order.id);
  assert.equal(new Set(keys).size, 1);
  assert.deepEqual([...restored.values.values()], [`order:${order.id}`]);
  await assert.rejects(purchase.openNonTeamPurchase(restored, noCreate, async () => { throw Error("404"); }), /404/);
  assert.equal((await purchase.openNonTeamPurchase(restored, noCreate, async id => {
    assert.equal(id, order.id); return order;
  })).id, order.id);

  purchase.forgetPurchase(restored); // user-confirmed new purchase
  const next = makeOrder();
  assert.equal((await open(restored, async () => next)).id, next.id);
  assert.notEqual(keys.at(-1), keys[0]);
});

test("重叠建单共用键；先后返回的相同订单都能恢复", async () => {
  const storage = tab(); const keys: string[] = []; const order = makeOrder();
  let release!: (o: Order) => void;
  const delayed = new Promise<Order>(resolve => { release = resolve; });
  const create = async (k: string) => { keys.push(k); return delayed; };
  const first = purchase.openNonTeamPurchase(storage, create, noLookup);
  const second = purchase.openNonTeamPurchase(storage, create, noLookup);
  release(order);
  assert.deepEqual(await Promise.all([first, second]), [order, order]);
  assert.equal(new Set(keys).size, 1);
});

test("存储不可写或标记损坏不提交，不自行生成替代键", async () => {
  const blocked = tab();
  blocked.setItem = () => { throw Error("quota"); };
  await assert.rejects(purchase.openNonTeamPurchase(blocked, noCreate, noLookup), /quota/);
  const corrupt = tab();
  corrupt.getItem = () => "invalid";
  await assert.rejects(purchase.openNonTeamPurchase(corrupt, noCreate, noLookup), /购买标记/);
});

test("回执写入失败仍保留原键；查回错误单号也不创建新单", async () => {
  const storage = tab(); const order = makeOrder(); const keys = new Set<string>();
  const write = storage.setItem;
  storage.setItem = (k, v) => {
    if (v.startsWith("order:")) throw Error("quota");
    write(k,v);
  };
  const create = async (k: string) => { keys.add(k); return order; };
  await assert.rejects(purchase.openNonTeamPurchase(storage, create, noLookup), /quota/);
  assert.equal(purchase.readPurchaseIntent(storage)?.kind, "pending");
  storage.setItem = write;
  await purchase.openNonTeamPurchase(storage, create, noLookup);
  assert.equal(keys.size, 1);
  await assert.rejects(purchase.openNonTeamPurchase(storage, noCreate, async () => makeOrder()), /原订单/);
  assert.equal(purchase.readPurchaseIntent(storage)?.id, order.id);
});

test("上一笔迟到的响应不覆盖显式新开的购买", async () => {
  const storage = tab();
  let release!: (o: Order) => void;
  const pending = new Promise<Order>(resolve => {release = resolve;});
  const old = purchase.openNonTeamPurchase(storage, () => pending, noLookup);
  purchase.forgetPurchase(storage);
  const next = makeOrder();
  await purchase.openNonTeamPurchase(storage, async () => next, noLookup);
  release(makeOrder());
  await assert.rejects(old, /标记已更改/);
  assert.equal(purchase.readPurchaseIntent(storage)?.id, next.id);
});
