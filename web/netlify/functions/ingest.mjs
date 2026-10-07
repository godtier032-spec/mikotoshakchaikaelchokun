// POST /api/ingest — รับสถิติจากแอป แล้วเก็บแยกตามผู้ใช้ลง Netlify Blobs
// ผู้ใช้แต่ละคนมี "รหัสส่วนตัว" (สุ่มโดยแอป) ส่งมาใน Authorization: Bearer <key>
// เซิร์ฟเวอร์เก็บข้อมูลที่คีย์ device/<sha256(key)> เท่านั้น ไม่เก็บตัวรหัสจริง
import { getStore } from "@netlify/blobs";
import { createHash, timingSafeEqual } from "node:crypto";

export const config = { path: "/api/ingest" };

const MAX_BODY = 600_000;   // ไบต์
const MAX_DAY = 30_000;     // ขนาดข้อมูลต่อวัน (ตัวอักษร)
const MIN_GAP_MS = 1500;    // ส่งถี่กว่านี้ต่อรหัสเดียวกัน -> 429
const KEEP_DAYS = 120;
const STATUSES = ["normal", "warning", "bad", "no_person"];
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const KEY = /^[A-Za-z0-9_-]{24,128}$/;

const json = (o, status = 200) =>
  new Response(JSON.stringify(o), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });

const sha = (s) => createHash("sha256").update(String(s)).digest();
const idOf = (key) => sha(key).toString("hex").slice(0, 32);

export default async (req) => {
  if (req.method !== "POST") return json({ error: "method not allowed" }, 405);

  // (ทางเลือก) รหัสเชิญ: ถ้าตั้ง INVITE_CODE ไว้ใน Netlify ผู้ส่งต้องมีรหัสนี้ด้วย กันคนแปลกหน้าสร้างข้อมูลขยะ
  const invite = Netlify.env.get("INVITE_CODE");
  if (invite && !timingSafeEqual(sha(req.headers.get("x-invite") || ""), sha(invite)))
    return json({ error: "invite code required" }, 403);

  const key = (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, "").trim();
  if (!KEY.test(key)) return json({ error: "bad key" }, 401);

  const raw = await req.text();
  if (raw.length > MAX_BODY) return json({ error: "payload too large" }, 413);

  let p;
  try { p = JSON.parse(raw); } catch { return json({ error: "bad json" }, 400); }
  if (!p || typeof p !== "object" || Array.isArray(p)) return json({ error: "bad json" }, 400);
  if (!STATUSES.includes(p.status)) return json({ error: "bad status" }, 400);

  const store = getStore({ name: "posture", consistency: "strong" });
  const rec = `device/${idOf(key)}`;
  const cur = (await store.get(rec, { type: "json" })) || { days: {} };

  if (cur.updated && Date.now() - cur.updated < MIN_GAP_MS)
    return json({ error: "too many requests" }, 429);

  const days = cur.days || {};
  const put = (d) => {
    if (d && typeof d === "object" && DATE.test(String(d.date)) && JSON.stringify(d).length <= MAX_DAY)
      days[d.date] = d;
  };
  (Array.isArray(p.history) ? p.history : []).forEach(put);
  put(p.day);
  const keep = Object.keys(days).sort().slice(-KEEP_DAYS);

  const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : 0);
  const okDay = p.day && DATE.test(String(p.day.date)) && JSON.stringify(p.day).length <= MAX_DAY;
  const live = {
    status: p.status,
    level: num(p.level),
    score: p.score == null ? null : num(p.score),
    streak: num(p.streak),
    goal: num(p.goal) || 0.7,
    date: okDay ? p.day.date : null,
    day: okDay ? p.day : null,
    sent_at: num(p.sent_at),
    received_at: Date.now(),
    app_version: String(p.app_version || "").slice(0, 16),
    events: (Array.isArray(p.events) ? p.events : []).slice(0, 12).map((e) => ({
      t: num(e && e.t), s: String((e && e.s) || "").slice(0, 16),
      from: String((e && e.from) || "").slice(0, 16),
      cause: e && e.cause ? String(e.cause).slice(0, 16) : undefined,
    })),
  };

  await store.setJSON(rec, {
    days: Object.fromEntries(keep.map((k) => [k, days[k]])),
    live,
    updated: Date.now(),
  });
  return json({ ok: true });
};
