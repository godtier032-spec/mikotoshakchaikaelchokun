// GET /api/stats[?full=1] — ให้หน้าเว็บอ่านสถิติของ "รหัสส่วนตัว" นั้นๆ (Authorization: Bearer <key>)
// ทุกคนใช้เว็บเดียวกันได้ แต่ละรหัสเห็นเฉพาะข้อมูลของตัวเอง
import { getStore } from "@netlify/blobs";
import { createHash } from "node:crypto";

export const config = { path: "/api/stats" };

const KEY = /^[A-Za-z0-9_-]{24,128}$/;

const json = (o, status = 200) =>
  new Response(JSON.stringify(o), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });

const idOf = (key) => createHash("sha256").update(String(key)).digest("hex").slice(0, 32);

export default async (req) => {
  if (req.method !== "GET") return json({ error: "method not allowed" }, 405);

  const key = (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, "").trim();
  if (!KEY.test(key)) return json({ error: "unauthorized" }, 401);

  const store = getStore({ name: "posture", consistency: "strong" });
  const dev = await store.get(`device/${idOf(key)}`, { type: "json" });
  if (!dev || !dev.live) return json({ empty: true, server_time: Date.now() });

  const out = { server_time: Date.now(), live: dev.live };
  if (new URL(req.url).searchParams.get("full")) out.days = dev.days;
  return json(out);
};
