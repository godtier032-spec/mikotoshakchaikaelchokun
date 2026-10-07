# -*- coding: utf-8 -*-
"""ส่งสถิติท่านั่งขึ้นเว็บ (ปิดไว้เป็นค่าเริ่มต้น เปิดเองได้ที่ปุ่ม "ส่งสถิติขึ้นเว็บ")

ส่งเฉพาะตัวเลขสรุป (เวลาดี/เตือน/ค่อม รายชั่วโมง สาเหตุ ฯลฯ) ไม่ส่งภาพจากกล้อง
แต่ละเครื่องมี "รหัสส่วนตัว" (web_key) ที่สุ่มเอง ส่งใน Authorization: Bearer <key> ผ่าน HTTPS
เซิร์ฟเวอร์เก็บข้อมูลแยกตาม sha256(key) จึงใช้เว็บเดียวกันได้หลายคนโดยไม่เห็นข้อมูลของกันและกัน
"""
import datetime
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import deque

APP_VERSION = "12"
SEND_EVERY = 10.0          # ส่งสถานะสดทุกกี่วินาที
HISTORY_EVERY = 300.0      # ส่งประวัติย้อนหลังทุกกี่วินาที (และตอนส่งครั้งแรก)
HISTORY_DAYS = 60
CAUSES = ("head", "fwd", "scale", "tilt")


def normalize_url(url):
    """รับได้ทั้ง https://xxx.netlify.app หรือ URL เต็ม แล้วต่อ /api/ingest ให้"""
    u = (url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "https://" + u
    if "/api/ingest" not in u:
        u = u.rstrip("/") + "/api/ingest"
    return u


class WebSync:
    def __init__(self, cfg, stats, log, goal=0.7):
        self.cfg, self.stats, self.log, self.goal = cfg, stats, log, goal
        self.events = deque(maxlen=12)
        self.status = None
        self.level = 0.0
        self.last_ok = 0.0
        self.last_error = ""
        self._last_try = 0.0
        self._hist_t = 0.0
        self._soon = False
        self._busy = False

    # ----- ตั้งค่า -----
    def enabled(self):
        return bool(self.cfg["web_on"]) and bool(normalize_url(self.cfg["web_url"]))

    def key(self):
        """รหัสส่วนตัวของเครื่องนี้ (สร้างครั้งแรกอัตโนมัติ)"""
        k = str(self.cfg["web_key"] or "")
        if len(k) < 24:
            k = secrets.token_urlsafe(24)
            self.cfg["web_key"] = k
        return k

    def new_key(self):
        self.cfg["web_key"] = ""
        return self.key()

    def device_id(self):
        """เลขอ้างอิงเครื่อง (แสดงในแอปเท่านั้น เซิร์ฟเวอร์คำนวณจากรหัสเอง)"""
        return hashlib.sha256(self.key().encode()).hexdigest()[:8]

    def dashboard_url(self):
        """ลิงก์เปิดแดชบอร์ดพร้อมรหัส (รหัสอยู่หลัง # จึงไม่ถูกส่งไปเซิร์ฟเวอร์)"""
        u = normalize_url(self.cfg["web_url"])
        if not u:
            return ""
        return u.split("/api/ingest")[0].rstrip("/") + "/#k=" + self.key()

    # ----- เหตุการณ์ (เรียกจาก main thread) -----
    def note(self, status, parts=None):
        if status == self.status:
            return
        prev, self.status = self.status, status
        if prev is None:
            return
        ev = {"t": int(time.time()), "s": status, "from": prev}
        if parts and status in ("warning", "bad"):
            ev["cause"] = max(CAUSES, key=lambda k: float(parts.get(k, 0.0)))
        self.events.appendleft(ev)
        self._soon = True

    # ----- สร้างข้อมูล (main thread เท่านั้น เพราะอ่าน stats.days) -----
    def build(self, with_history):
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        day = self.stats.day_summary(today)
        tracked = day["good_s"] + day["warning_s"] + day["bad_s"]
        p = {
            "device_id": self.device_id(),
            "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "sent_at": int(now),
            "app_version": APP_VERSION,
            "status": self.status or "no_person",
            "level": round(float(self.level), 3),
            "score": round(day["good_s"] / tracked, 4) if tracked >= 60 else None,
            "today": {"good_sec": day["good_s"], "warn_sec": day["warning_s"],
                      "bad_sec": day["bad_s"], "away_sec": day["away_s"]},
            "streak": self.stats.goal_streak(),
            "goal": self.goal,
            "day": day,
            "events": list(self.events),
        }
        if with_history:
            p["history"] = [self.stats.day_summary(k)
                            for k in sorted(self.stats.days)[-HISTORY_DAYS:] if k != today]
        return p

    def tick(self):
        """เรียกจาก QTimer ทุก ~2 วินาที"""
        if not self.enabled() or self._busy:
            return
        now = time.time()
        if not (self._soon or now - self._last_try >= SEND_EVERY):
            return
        self._last_try = now
        self._soon = False
        with_hist = now - self._hist_t >= HISTORY_EVERY
        try:
            body = self.build(with_hist)
        except Exception as e:
            self.last_error = f"build: {e}"
            self.log(f"web sync build error: {e}")
            return
        self._busy = True
        threading.Thread(target=self._send_bg, args=(body, with_hist), daemon=True).start()

    # ----- ส่ง -----
    def _post(self, payload):
        url = normalize_url(self.cfg["web_url"])
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer " + self.key(),
                   "User-Agent": "PostureShimeji/" + APP_VERSION}
        invite = str(self.cfg["web_invite"] or "").strip()
        if invite:
            headers["X-Invite"] = invite
        req = urllib.request.Request(url, data=raw, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, ""
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8", "replace")).get("error", "")
            except Exception:
                msg = ""
            return e.code, msg
        except Exception as e:
            return 0, str(e)

    def _send_bg(self, body, with_hist):
        try:
            code, msg = self._post(body)
            if 200 <= code < 300:
                self.last_ok, self.last_error = time.time(), ""
                if with_hist:
                    self._hist_t = time.time()
            else:
                self.last_error = f"HTTP {code} {msg}".strip() if code else msg
                self.log(f"web sync failed: {self.last_error}")
        finally:
            self._busy = False

    def test(self):
        """ทดสอบการเชื่อมต่อ (บล็อกไม่เกิน ~8 วินาที) -> (ok, ข้อความ)"""
        try:
            code, msg = self._post(self.build(True))
        except Exception as e:
            return False, str(e)
        if 200 <= code < 300:
            self.last_ok, self.last_error, self._hist_t = time.time(), "", time.time()
            return True, "OK"
        hint = {401: "รหัสส่วนตัวไม่ถูกต้อง ลองกด \"สุ่มรหัสใหม่\"",
                403: "ไซต์นี้ต้องใส่รหัสเชิญ (INVITE_CODE) ให้ตรงกับเจ้าของไซต์",
                404: "ไม่พบ /api/ingest ตรวจ URL และว่า deploy แบบมี Functions แล้ว",
                429: "ส่งถี่เกินไป ลองใหม่อีกครั้งใน 2-3 วินาที"}.get(code, "")
        return False, (f"HTTP {code} {msg} {hint}".strip() if code else msg)
