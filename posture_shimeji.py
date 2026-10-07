# -*- coding: utf-8 -*-
"""
Posture Shimeji  -  มาสคอตเตือนนั่งหลังค่อม (Windows)

  * ใช้ MediaPipe Pose (33 keypoints) ตัวเดียวกับโปรเจกต์ MindDock/motion-tracker
  * วิเคราะห์ท่านั่งจากเว็บแคม แล้วแปลงเป็น 4 สถานะ
        STATUS: normal | warning | bad | no_person
  * มาสคอตวิ่งเล่นบนหน้าจอ (สไตล์ Shimeji) และเปลี่ยนพฤติกรรมตามสถานะ
  * ทุกอย่างอยู่ในไฟล์เดียว  ->  build เป็น PostureShimeji.exe ไฟล์เดียวได้
"""
import datetime
import json
import math
import os
import random
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from string import Template

import numpy as np
from web_sync import WebSync, normalize_url
from PySide6.QtCore import (QLockFile, QObject, QPoint, QPointF, QRect, QRectF,
                            QSize, Qt, QThread, QTimer, QUrl, Signal)
from PySide6.QtGui import (QAction, QActionGroup, QColor, QCursor,
                           QDesktopServices, QFont, QFontMetrics, QGuiApplication,
                           QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap,
                           QPolygonF)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QScrollArea, QSlider, QSystemTrayIcon,
                               QVBoxLayout, QWidget)

APP_NAME = "PostureShimeji"
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
BASE_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent

STATUSES = ("normal", "warning", "bad", "no_person")

# ----------------------------------------------------------------------------
#  ตั้งค่าความไว (ปรับได้)
# ----------------------------------------------------------------------------
TARGET_FPS = 10            # ประมวลผลกล้องกี่เฟรมต่อวินาที (พอสำหรับท่านั่ง และประหยัดเครื่อง)
WARN_TH = 0.25             # คะแนนเริ่ม warning
BAD_TH = 0.50              # คะแนนเริ่ม bad
HYST = 0.06                # hysteresis กันสถานะกระพริบ
ENTER_DELAY = {            # ต้องอยู่ในท่านั้นต่อเนื่องกี่วินาทีถึงเปลี่ยนสถานะ
    "normal": 2.0,
    "warning": 4.0,
    "bad": 5.0,
    "no_person": 3.0,
}
SENS = {"low": 1.25, "normal": 1.0, "high": 0.8}

# ----------------------------------------------------------------------------
#  Utility
# ----------------------------------------------------------------------------
APP_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = APP_DIR / "log.txt"
STATUS_FILE = APP_DIR / "status.txt"


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512 * 1024:
            LOG_FILE.unlink()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def clamp01(v):
    return max(0.0, min(1.0, float(v)))


class Config:
    DEFAULTS = {"baseline": None, "sound": True, "camera": 0, "sensitivity": "normal",
                "tip_shown": False, "volume": "high", "use_custom": True,
                "character": "cat", "lang": "th", "theme": "light", "roam": True,
                "time_greet": True, "companion": "none", "greeted": {}, "size": 1.0,
                "web_on": False, "web_url": "", "web_key": "", "web_invite": ""}

    def __init__(self):
        self.path = APP_DIR / "config.json"
        self.data = dict(self.DEFAULTS)
        try:
            if self.path.exists():
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception as e:
            log(f"config load error: {e}")

    def __getitem__(self, k):
        return self.data.get(k, self.DEFAULTS.get(k))

    def __setitem__(self, k, v):
        self.data[k] = v
        self.save()

    def save(self):
        try:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        except Exception as e:
            log(f"config save error: {e}")


EN = {
    "🌐  ส่งสถิติขึ้นเว็บ…": "🌐  Send stats to web…", "🌐  ส่งสถิติขึ้นเว็บ": "🌐  Send stats to web",
    "ส่งเฉพาะตัวเลขสรุป (เวลานั่งดี/เตือน/ค่อม รายชั่วโมง สาเหตุ) ไม่ส่งภาพจากกล้อง แต่ละเครื่องใช้รหัสส่วนตัวของตัวเอง คนอื่นที่ใช้เว็บเดียวกันจะไม่เห็นข้อมูลของคุณ ปิดไว้เป็นค่าเริ่มต้น":
        "Only summary numbers are sent (good/warning/bad time per hour, causes) - never camera images. Each device has its own personal key, so others using the same site cannot see your data. Off by default.",
    "เปิดการส่งสถิติขึ้นเว็บ": "Enable sending stats to the web",
    "URL เว็บ (เช่น https://ชื่อไซต์.netlify.app)": "Site URL (e.g. https://your-site.netlify.app)",
    "รหัสเชิญ (ถ้าเจ้าของไซต์ตั้งไว้ — เว้นว่างได้)": "Invite code (only if the site owner set one - may be empty)",
    "รหัสส่วนตัวของคุณ (ใช้ดูข้อมูลบนเว็บ อย่าแชร์ให้ใคร)": "Your personal key (used to view your data on the web - don't share it)",
    "สุ่มรหัสใหม่": "New key", "รหัสเครื่องนี้: ": "This device ID: ",
    "สุ่มรหัสใหม่แล้ว ต้องเปิดแดชบอร์ดผ่านปุ่มด้านล่างอีกครั้ง (ข้อมูลเก่าจะไม่แสดงภายใต้รหัสใหม่)":
        "New key generated. Open the dashboard again with the button below (old data won't show under the new key).",
    "🌍  เปิดแดชบอร์ดในเบราว์เซอร์": "🌍  Open dashboard in browser",
    "📋  คัดลอกรหัส": "📋  Copy key", "คัดลอกรหัสแล้ว": "Key copied",
    "กรอก URL เว็บก่อน": "Enter the site URL first",
    "🔌  ทดสอบการเชื่อมต่อ": "🔌  Test connection", "บันทึก": "Save",
    "กำลังทดสอบ…": "Testing…",
    "เชื่อมต่อสำเร็จ": "Connected",
    "ขนาดตัวละคร": "Mascot size", "↺  กลับเป็นขนาดเดิม (100%)": "↺  Reset to default size (100%)",
    "ค้นหากล้องใหม่": "Rescan cameras",
    # ---- ข้อความทั่วไป / ตัวละคร ----
    "เปิดกล้องไม่ได้ (ถูกโปรแกรมอื่นใช้อยู่หรือยังไม่ได้อนุญาต)": "Can't open the camera (in use by another app, or not allowed yet)",
    "แมวส้ม": "Orange Cat", "กระต่ายชมพู": "Pink Bunny", "ชิบะ": "Shiba", "แรคคูน": "Raccoon",
    "เมี้ยว~ แมวส้มมาแล้ว!": "Meow~ Orange Cat is here!",
    "สวัสดีค่ะ~ กระต่ายชมพูมาแล้ว!": "Hello~ Pink Bunny is here!",
    "โฮ่ง! ชิบะมารายงานตัวแล้ว!": "Woof! Shiba reporting for duty!",
    "หวัดดีจ้า~ แรคคูนจอมซนมาแล้ว!": "Hiya~ the mischievous Raccoon is here!",
    # ---- ท่าทาง ----
    "เลียๆ~ ล้างหน้าก่อน": "Lick lick~ washing my face",
    "ยืดดดด~ หาววว": "Streeetch~ *yawn*",
    "สวัสดี~ เมี้ยว!": "Hello~ meow!",
    "♪ ลา~ลา~ลา": "♪ la~la~la",
    "ขอแอบงีบแป๊บ… zZ": "Just a tiny nap… zZ",
    "มีอะไรน่ะ?": "What's that?",
    "ผีเสื้อ! ผีเสื้อ!": "A butterfly! A butterfly!",
    "ไล่หางตัวเอง!": "Chasing my own tail!", "ไล่หางตัวเอง": "Chase own tail",
    "ดีใจจัง~": "So happy~",
    "ตุบๆ แครอทอร่อยจัง~": "Crunch crunch~ yummy carrot!",
    "หูกระดิกๆ~": "Wiggle wiggle~ my ears",
    "โฮ่ง! โฮ่ง!": "Woof! Woof!",
    "ดีใจจนหางจะหลุด~": "So happy my tail might fall off~",
    "เล่นไหมพรมๆ~": "Playing with yarn~",
    "ล้างก่อนกินนะ~ ซ่าๆ": "Wash before eating~ splish splash",
    "ขโมยคุกกี้มาได้~ แฮ่!": "Got a stolen cookie~ heh!",
    "จ๊ะเอ๋!": "Peekaboo!",
    "กอดหางตัวเองอุ่นจัง~": "Hugging my tail, so cozy~",
    "ว้าว! ของแวววาว~": "Wow! Something shiny~",
    "หึหึ… ไม่มีใครเห็นนะ": "Hehe… nobody's watching",
    "สวัสดี~ ปิ๊บๆ!": "Hello~ squeak squeak!",
    "สวัสดี~ โฮ่ง!": "Hello~ woof!",
    "หมุนติ้วๆ~": "Spinning around~",
    "ไล่หางตัวเอง! โฮ่งๆ": "Chasing my tail! Woof woof",
    "ผีเสื้อ! ปิ๊บ!": "A butterfly! Squeak!",
    "♪ โฮ่ง~ ลา~ลา": "♪ woof~ la~la",
    "ดีใจจัง ปิ๊บ~": "So happy, squeak~",
    "ดีใจจังโฮ่ง~": "So happy, woof~",
    "หวัดดี~ จี๊ดๆ!": "Hiya~ chitter chitter!",
    "ไล่หางลายตัวเอง!": "Chasing my striped tail!",
    "ดีใจจัง~ จี๊ด!": "So happy~ chit!",
    "♪ ชะชะช่า~": "♪ cha-cha-cha~",
    "ผีเสื้อ! จะจับให้ได้!": "A butterfly! I'll catch it!",
    "ล้างหน้าล้างตา~": "Washing up~",
    "มีของแวววาวไหมนะ?": "Anything shiny around?",
    "นั่งพัก": "Sit & rest", "บิดขี้เกียจ": "Stretch", "ล้างหน้า": "Wash face", "โบกมือทักทาย": "Wave hello",
    "มองซ้ายขวา": "Look around", "เต้น": "Dance", "แอบงีบ": "Sneaky nap", "ไล่ดูผีเสื้อ": "Watch a butterfly",
    "กระโดดดีใจ": "Happy hop", "กินแครอท": "Eat a carrot", "หูกระดิก": "Wiggle ears",
    "กระดิกหางรัว": "Wag tail", "เล่นไหมพรม": "Play with yarn", "เห่า": "Bark", "จ๊ะเอ๋": "Peekaboo",
    "ล้างผลไม้ (ซ่าๆ)": "Wash fruit (splish!)", "แทะคุกกี้": "Nibble cookie", "กอดหางตัวเอง": "Hug own tail",
    "ย่องหัวเราะหึหึ": "Sneaky giggle", "เจอของแวววาว": "Find something shiny",
    # ---- ข้อความเตือน ----
    "หลังค่อมแล้ว!\nนั่งตัวตรงๆ เดี๋ยวนี้!": "Slouching!\nSit up straight now!",
    "โอ๊ย ปวดหลังแทนเลย\nยืดตัวขึ้นหน่อย!": "Ouch, my back hurts for you\nStraighten up!",
    "ไหล่ห่อแล้วนะ!\nเงยหน้า ยืดอกหน่อย!": "Shoulders hunched!\nChin up, chest out!",
    "เริ่มค่อมแล้วนะ\nยืดหลังหน่อย~": "You're starting to slouch\nStretch your back~",
    "ระวังหลังงอ!\nนั่งให้ตรงอีกนิด": "Careful, your back is curving!\nSit a bit straighter",
    "คอเริ่มยื่นแล้วนะ\nถอยหลังมาหน่อย": "Your neck is sticking out\nPull back a little",
    "สุดยอด! หลังตรงเลย": "Awesome! Back is straight",
    "เยี่ยม! ท่านั่งดีแล้ว\nรักษาไว้นะ": "Great! Good posture\nKeep it up",
    "จิ้มทำไมเนี่ย~\nนั่งหลังตรงๆ ด้วยนะ": "Why the poke~\nSit up straight, okay?",
    "เราคอยเฝ้าดูท่านั่งอยู่นะ!": "I'm watching your posture!",
    "ดื่มน้ำแล้วหรือยัง?": "Have you had some water?",
    "ลุกเดินสักนิดไหม?": "Want to walk around a bit?",
    "ตอนนี้ต้องตั้งใจเฝ้าท่านั่งก่อน\nไว้นั่งดีๆ แล้วค่อยเล่นนะ": "I need to focus on your posture\nLet's play once you sit well",
    "เดี๋ยวนะ ขอลงมาก่อน~": "Hold on, let me come down first~",
    "กำลังเปิดกล้อง...": "Opening camera...",
    # ---- ข้อความใหม่ของมาสคอต ----
    "ตามมาแล้ว~": "Coming to you~", "เมาส์อยู่นี่เอง!": "There's the mouse!", "ขอตามไปด้วยคน~": "Mind if I tag along~",
    "หืม? มีอะไรเหรอ": "Hm? What's up?", "มองอะไรอยู่~": "What are you looking at~",
    "เมาส์มาใกล้แล้วว": "The mouse is so close!",
    "อุ๊ย~ ชอบจัง ♥": "Aww~ I love it ♥", "ลูบอีกๆ~": "More pats~", "หัวฟูหมดแล้ว~": "My head is all fluffy~",
    "ขอบคุณที่ดูแลนะ ♥": "Thanks for taking care of me ♥",
    "งั่มๆ~ อร่อยจัง!": "Nom nom~ so tasty!", "ขอบคุณสำหรับขนมนะ ♥": "Thanks for the snack ♥",
    "อร่อยที่สุดเลย~": "The best ever~",
    "นั่งหลังตรงก่อนนะ\nแล้วค่อยกินขนม!": "Sit up straight first\nThen snack time!",
    "เก็บขนมไว้ก่อน\nนั่งหลังตรงๆ แล้วค่อยกินนะ": "I'll save the snack\nI'll eat once you sit straight",
    "ขนม! ขนมมาแล้ว~": "Snack! A snack!~", "ได้กลิ่นขนมแล้ว!": "I smell a snack!",
    "สูงดีจัง~ วิวสวย": "So high up~ nice view", "ขอนั่งตรงนี้นะ": "I'll sit right here",
    "เห็นทั้งจอเลย!": "I can see the whole screen!",
    "ลงละนะ~": "Coming down~", "ไปล่ะ ฮึบ!": "Here I go! Hup!",
    "อ้าว! หน้าต่างหายไปไหน": "Oh! Where did the window go?", "ว้าย! พื้นหายไปแล้ว": "Whoa! The floor vanished!",
    "จับให้ได้สิ~": "Catch me if you can~", "ว้าา! ตามมาแล้ว!": "Eek! It's coming!",
    "รอเดี๋ยว! อย่าหนีนะ": "Wait! Don't run!", "เดี๋ยวจับได้แน่~": "I'll catch you for sure~",
    "จับได้แล้ว!": "Got you!", "ฮ่าๆ เล่นอีกรอบไหม~": "Haha, one more round?~",
    "อรุณสวัสดิ์~ วันนี้นั่งหลังตรงๆ กันนะ": "Good morning~ let's sit up straight today",
    "เช้าแล้ว! ยืดเส้นยืดสายก่อนเริ่มงานไหม?": "Morning! Stretch before you start work?",
    "เที่ยงแล้ว! ไปกินข้าวกันเถอะ": "It's noon! Let's go eat",
    "พักกินข้าวก่อนไหม? ลุกจากเก้าอี้หน่อย~": "Lunch break? Get up from the chair a bit~",
    "เย็นแล้ว~ วันนี้เหนื่อยไหม?": "Evening~ tired today?",
    "ใกล้เลิกงานแล้ว ยืดตัวหน่อยนะ": "Almost done for the day, stretch a little",
    "ดึกแล้วนะ… ไปนอนได้แล้ว": "It's late… time for bed",
    "หาววว~ ง่วงแล้ว พักสายตาหน่อยนะ": "*yawn*~ sleepy… rest your eyes a bit",
    "นอนดึกระวังปวดหลังนะ!": "Staying up late hurts your back!",
    "ยินดีต้อนรับกลับ!\nพักไป {n} นาทีเลยนะ": "Welcome back!\nYou were away for {n} min",
    # ---- สถานะ / หน้าต่างควบคุม ----
    "นั่งท่าดีมาก": "Great posture", "รักษาท่านี้ไว้นะ": "Keep it up",
    "ลองยืดหลัง ยกหัวขึ้นนิดนึง": "Straighten your back, lift your head a bit",
    "เริ่มค่อมแล้วนะ": "Starting to slouch",
    "ยืดหลัง ดึงคางเข้า ผ่อนคลายไหล่": "Straighten up, tuck your chin, relax your shoulders",
    "หลังค่อมแล้ว!": "Slouching!",
    "นั่งให้เห็นใบหน้าและไหล่ทั้งสองข้าง": "Sit so your face and both shoulders are visible",
    "ไม่พบคนหน้ากล้อง": "No one in front of the camera",
    "กด \"เริ่มตรวจจับ\" เพื่อเปิดกล้องอีกครั้ง": "Press \"Start detection\" to turn the camera on again",
    "พักการตรวจจับอยู่": "Detection paused",
    "ระดับความหลังค่อม": "Slouch level",
    "👉 ยังไม่ได้ตั้งท่านั่งมาตรฐาน กดปุ่ม \"ปรับท่านั่งมาตรฐาน\" ด้านล่าง\nแล้วนั่งหลังตรงๆ มองกล้อง 5 วินาที":
        "👉 Your reference posture isn't set yet. Press \"Calibrate posture\" below,\nthen sit up straight and look at the camera for 5 seconds",
    "⏸  หยุดตรวจจับชั่วคราว": "⏸  Pause detection", "▶  เริ่มตรวจจับ": "▶  Start detection",
    "🎯  ปรับท่านั่งมาตรฐาน": "🎯  Calibrate posture", "📷  ดูภาพกล้อง": "📷  Camera view",
    "📊  ดูสถิติ": "📊  Statistics", "◂  ซ่อนสถิติ": "◂  Hide statistics",
    "ตั้งค่า": "Settings", "ความไวในการเตือน": "Alert sensitivity",
    "ปกติ": "Normal", "ผ่อนปรน": "Relaxed", "เข้มงวด": "Strict",
    "เสียงเตือนเมื่อหลังค่อม": "Sound alert when slouching", "ความดังเสียงเตือน": "Alert volume",
    "เบา": "Soft", "กลาง": "Medium", "ดัง": "Loud", "🔊  ลองฟังเสียงเตือน": "🔊  Test the alert sound",
    "ตอนหลังค่อมชัดเจน เสียงจะดังต่อเนื่องจนกว่าจะนั่งตรง (คลิกที่มาสคอตเพื่อพักเสียง 1 นาที)":
        "When clearly slouching, the alarm keeps sounding until you sit up straight (click the mascot to snooze for 1 minute)",
    "เปิดโปรแกรมอัตโนมัติเมื่อเปิดเครื่อง": "Start automatically with Windows",
    "กล้องที่ใช้": "Camera", "กล้องตัวที่ ": "Camera ", " (ค่าเริ่มต้น)": " (default)",
    "ไม่เห็นภาพ? ลองเลือกกล้องตัวอื่น หรือปิดโปรแกรมที่ใช้กล้องอยู่ (Zoom/Teams)":
        "No image? Try another camera or close apps using the camera (Zoom/Teams)",
    "ธีมสี": "Theme", "สว่าง": "Light", "มืด": "Dark", "ซากุระ": "Sakura",
    "ภาษา / Language": "Language / ภาษา",
    "ทักทายตามเวลา (เช้า/เที่ยง/เย็น/ดึก)": "Time-of-day greetings (morning / noon / evening / night)",
    "ให้มาสคอตกระโดดขึ้นนั่งบนขอบหน้าต่าง และตามเมาส์ (Windows)":
        "Let the mascot perch on window edges and follow the mouse (Windows)",
    "มาสคอต": "Mascot", "🖼  รูปของฉัน": "🖼  My image", "📂  เลือกไฟล์รูปใหม่ (PNG)…": "📂  Choose a new image (PNG)…",
    "🎬  ดูท่าทางน่ารักๆ (สุ่ม)": "🎬  Watch cute moves (random)", "▶ เล่นท่านี้": "▶ Play this move",
    "🍪  ให้ขนมมาสคอต": "🍪  Give the mascot a snack",
    "ลากขนมไปวางให้กิน หรือปล่อยให้ตกพื้นแล้วมาสคอตจะเดินมากินเอง (ต้องนั่งท่าดีก่อนถึงจะกิน) / กดค้างที่ตัวมาสคอตเพื่อลูบหัว":
        "Drag the snack onto the mascot, or let it drop and the mascot walks over to eat it (only when your posture is good). Press and hold the mascot to pet it.",
    "ตัวละครคู่หู (อยู่ด้วยกัน 2 ตัว)": "Companion (two mascots at once)", "ไม่มี": "None",
    "ลองดูท่าทางมาสคอต (ไม่ต้องนั่งค่อมจริง)": "Preview mascot states (no need to really slouch)",
    "😊 ปกติ": "😊 Normal", "😟 เตือน": "😟 Warning", "😣 ค่อม": "😣 Slouch", "หยุดดู": "Stop preview",
    "ปิดหน้าต่างนี้แล้วโปรแกรมยังทำงานต่อที่ไอคอนข้างนาฬิกา (system tray)\nคลิกไอคอนเพื่อเปิดหน้านี้อีกครั้ง":
        "Closing this window keeps the app running in the system tray\nClick the tray icon to open it again",
    "ออกจากโปรแกรม": "Quit",
    "เตือน": "Warning", "หลังค่อม!": "Slouching!", "ไม่พบคน": "No one",
    # ---- Controller ----
    "สวัสดี! ฉันจะช่วยเฝ้าท่านั่งให้\nนั่งหลังตรงๆ แล้วรอสักครู่นะ": "Hi! I'll watch your posture\nSit up straight and wait a moment",
    "หยุดตรวจจับชั่วคราว (ปิดกล้อง)": "Pause detection (camera off)", "แสดงภาพกล้อง": "Show camera view",
    "เปิดอัตโนมัติเมื่อเริ่ม Windows": "Start automatically with Windows",
    "⚙  เปิดหน้าต่างควบคุม": "⚙  Open control panel", "📊  สถิติท่านั่ง": "📊  Posture statistics",
    "🍪  ให้ขนม": "🍪  Give a snack",
    "ปรับท่านั่งมาตรฐาน (Calibrate)": "Calibrate posture",
    "โปรแกรมยังทำงานอยู่ที่ไอคอนข้างนาฬิกา คลิกไอคอนเพื่อเปิดหน้าควบคุมอีกครั้ง":
        "The app is still running in the system tray. Click the icon to reopen the control panel.",
    "นั่งหลังตรงๆ มองกล้อง\nจะเริ่มจดจำใน 5 วินาที": "Sit up straight and look at the camera\nLearning your posture in 5 seconds",
    "เตรียมตัว... นั่งหลังตรงๆ\nเริ่มใน {n} วิ": "Get ready... sit up straight\nStarting in {n}s",
    "นิ่งๆ นะ กำลังจดจำท่าที่ดี\nเหลือ {n} วิ": "Hold still, learning your good posture\n{n}s left",
    "เรียบร้อย! จำท่านั่งที่ดีของคุณแล้ว\nต่อไปฉันจะคอยเตือนนะ": "All set! I've learned your good posture\nI'll remind you from now on",
    "ไม่เห็นตัวคุณชัดเลย\nลองใหม่จากเมนู Calibrate นะ": "I can't see you clearly\nTry again from the Calibrate menu",
    "พักก่อนนะ (ปิดกล้องแล้ว)": "Taking a break (camera off)",
    "ตั้งค่าเปิดอัตโนมัติไม่สำเร็จ: ": "Could not set auto-start: ",
    "เสียงถูกปิดอยู่นะ\nติ๊กเปิดเสียงก่อน": "Sound is off\nTurn it on first",
    "เปลี่ยนเป็นรูปของคุณแล้ว": "Switched to your image",
    "เลือกรูปมาสคอต (PNG พื้นหลังโปร่งใส หันหน้าไปทางขวา)": "Choose mascot image (transparent PNG, facing right)",
    "รูปภาพ (*.png)": "Images (*.png)",
    "เปิดไฟล์รูปนี้ไม่ได้ ลองเลือกไฟล์ PNG อื่น": "Can't open this image. Try another PNG.",
    "ใช้รูปนี้ทุกสถานะ (ง่ายที่สุด)": "Use for every state (easiest)", "เฉพาะสถานะปกติ": "Normal state only",
    "เฉพาะสถานะเตือน": "Warning state only", "เฉพาะสถานะหลังค่อม": "Slouching state only",
    "เฉพาะสถานะไม่พบคน": "No-person state only", "ใช้รูปนี้กับสถานะไหน?": "Use this image for which state?",
    "บันทึกรูปไม่สำเร็จ: ": "Could not save the image: ",
    "โอเค พักเสียง 1 นาที\nแต่ต้องนั่งตรงๆ นะ!": "OK, sound snoozed for 1 minute\nBut sit up straight!",
    "เปิดกล้องไม่ได้ T_T\nเช็คว่ามีโปรแกรมอื่นใช้อยู่ไหม": "Can't open the camera T_T\nIs another app using it?",
    "โปรแกรมเปิดอยู่แล้ว (ดูไอคอนที่ system tray)": "The app is already running (see the system tray icon)",
    # ---- สถิติ ----
    "สถิติท่านั่ง": "Posture statistics", "วันนี้": "Today", "7 วัน": "7 days", "30 วัน": "30 days",
    "ยังไม่มีข้อมูล": "No data yet",
    "ยังไม่มีข้อมูลพอ (ต้องตรวจจับอย่างน้อย 1 นาที)": "Not enough data yet (needs at least 1 minute of tracking)",
    "ของเวลาที่ตรวจจับ นั่งท่าดี": "of tracked time in good posture",
    "เทียบกับ": "vs. ", "เมื่อวาน": "yesterday", "7 วันก่อนหน้า": "the previous 7 days", "30 วันก่อนหน้า": "the previous 30 days",
    "นั่งดี": "Good", "ค่อม": "Slouch",
    "{h} ชม. {m} น.": "{h}h {m}m", "{m} น.": "{m}m",
    "{a:02d}:00 - {b:02d}:00   นั่งดี {n}  เตือน {w}  ค่อม {c}": "{a:02d}:00 - {b:02d}:00   Good {n}  Warn {w}  Slouch {c}",
    "{d}   นั่งดี {n}  เตือน {w}  ค่อม {c}": "{d}   Good {n}  Warn {w}  Slouch {c}",
    "เวลาที่นั่งแต่ละชั่วโมง (นาที)": "Time per hour (minutes)", "เวลาที่นั่งแต่ละวัน (ชั่วโมง)": "Time per day (hours)",
    "ข้อสังเกต": "Insights",
    "เปิดโปรแกรมทิ้งไว้สักพัก แล้วกลับมาดูใหม่นะ": "Leave the app running for a while, then check back",
    "ช่วงที่ค่อมบ่อยสุด: {a:02d}:00 - {b:02d}:00 (ค่อมหรือเริ่มค่อม {p:.0f}% ของช่วงนั้น)":
        "Worst time of day: {a:02d}:00 - {b:02d}:00 (slouching or warned {p:.0f}% of that hour)",
    "ท่านั่งสม่ำเสมอดีมาก ไม่มีช่วงไหนที่ค่อมเด่นชัด 👏": "Very consistent posture, no standout bad hour 👏",
    "สาเหตุหลักที่ทำให้ค่อม: {c} ({p:.0f}%)": "Main cause of slouching: {c} ({p:.0f}%)",
    "หัวก้มต่ำกว่าไหล่": "Head dropping below the shoulders", "คอยื่นไปข้างหน้า": "Neck jutting forward",
    "โน้มตัวเข้าหาจอ": "Leaning toward the screen", "ไหล่เอียง": "Tilted shoulders",
    "ยกจอให้สูงขึ้น ให้ขอบบนจออยู่ระดับสายตา": "Raise the monitor so its top edge is at eye level",
    "ดึงคางเข้า แล้วให้หลังชิดพนักเก้าอี้": "Tuck your chin and rest your back against the chair",
    "ขยับจอให้ไกลขึ้นหรือขยายตัวอักษร จะได้ไม่ต้องโน้มตัว": "Move the screen farther or enlarge the text so you needn't lean",
    "เช็กความสูงของที่วางแขนและตำแหน่งเมาส์": "Check your armrest height and mouse position",
    "วันที่นั่งดีที่สุด: {d} ({p:.0f}%)": "Best day: {d} ({p:.0f}%)",
    "ถูกเตือนหลังค่อมชัดเจน {n} ครั้ง": "Clear-slouch alerts: {n}",
    "ลูบหัวมาสคอต {a} ครั้ง ป้อนขนม {b} ชิ้น 🍪": "Petted the mascot {a} times, fed {b} snacks 🍪",
    "ไม่อยู่หน้ากล้องรวม {t}": "Away from the camera: {t}",
    "📤  ส่งออก CSV": "📤  Export CSV", "🗑  ล้างสถิติ": "🗑  Clear statistics",
    "ข้อมูลเก็บในเครื่องคุณ (stats.json) จะส่งออกเฉพาะเมื่อคุณเปิด \"ส่งสถิติขึ้นเว็บ\" เอง":
        "Data stays on your computer (stats.json) and is only sent online if you turn on \"Send stats to web\"",
    "ส่งออกสถิติ": "Export statistics", "บันทึกแล้ว": "Saved", "บันทึกไม่สำเร็จ: ": "Could not save: ",
    "ล้างสถิติทั้งหมดใช่ไหม? (กู้คืนไม่ได้)": "Clear all statistics? This can't be undone.",
}

EN.update({
    "เวลาที่ตรวจจับรวม": "Total tracked time", "ใน {n} วันที่มีข้อมูล": "across {n} days with data",
    "เฉลี่ยต่อวัน": "Average per day", "ไม่อยู่หน้ากล้อง": "Away from camera",
    "คะแนนความค่อมเฉลี่ย": "Average slouch score", "ยิ่งต่ำยิ่งดี (0 = ตรงเท่าท่ามาตรฐาน)": "Lower is better (0 = same as your baseline)",
    "เฉลี่ยจากคะแนนทุกวินาทีที่ตรวจจับ": "Averaged over every tracked second",
    "นั่งดีต่อเนื่องนานสุด": "Longest good-posture streak", "ไม่ค่อม ไม่เตือน ต่อเนื่องไม่ขาด": "No warnings or slouching, unbroken",
    "นั่งนานสุดโดยไม่ลุก": "Longest sitting without a break", "ควรลุกยืดเส้นทุก 45-60 นาที": "Try to stand and stretch every 45-60 min",
    "ลุกพัก (ครั้ง)": "Breaks taken", "นั่งเฉลี่ยรอบละ {t}": "Average sitting round: {t}",
    "นับเมื่อออกจากหน้ากล้องเกิน 2 นาที": "Counted when you leave the camera for over 2 minutes",
    "ค่อมชัดเจน (ครั้ง)": "Clear slouches", "แก้ท่าเฉลี่ยครั้งละ {t}": "Fixed in {t} on average",
    "ทำเป้าติดต่อกัน (วัน)": "Goal streak (days)", "วันที่นั่งดีถึง {g:.0f}%": "Days with at least {g:.0f}% good posture",
    "{s} วิ": "{s}s",
    "ถึงเป้าหมาย {g:.0f}% แล้ว!": "Goal of {g:.0f}% reached!", "🎯 เป้าหมาย {g:.0f}%  อีก {d:.0f}% จะถึงเป้า": "🎯 Goal {g:.0f}%  -  {d:.0f}% to go",
    "ทำเป้าติดต่อกัน {n} วัน": "{n}-day goal streak",
    "แนวโน้มคะแนนท่านั่งรายวัน": "Daily posture score trend",
    "{d}   นั่งดี {p:.0f}%  (ตรวจจับ {t})": "{d}   Good {p:.0f}%  (tracked {t})",
    "เส้นประสีเขียว = เป้าหมาย {g:.0f}%   ·   ถึงเป้า {a} จาก {b} วันที่มีข้อมูล":
        "Dashed green line = goal {g:.0f}%   ·   goal reached on {a} of {b} days with data",
    "ช่วงเวลาที่นั่งดี/แย่ (วัน × ชั่วโมง)": "Good / bad hours (day × hour)",
    "เขียว = นั่งดี  เหลือง = เริ่มค่อม  แดง = ค่อมบ่อย  เทา = ไม่มีข้อมูล (เอาเมาส์ชี้ดูรายละเอียด)":
        "Green = good  Yellow = warming up  Red = often slouching  Gray = no data (hover for details)",
    "{d} {a:02d}:00   นั่งดี {p:.0f}%  (ตรวจจับรวม {t})": "{d} {a:02d}:00   Good {p:.0f}%  (tracked {t})",
    "0-30 น.": "0-30 min", "30-60 น.": "30-60 min", "60-90 น.": "60-90 min", "90+ น.": "90+ min",
    "นั่งต่อเนื่อง {n}   นั่งดี {p:.0f}%  (ตรวจจับ {t})": "Sitting {n}   Good {p:.0f}%  (tracked {t})",
    "ยังไม่มีข้อมูลพอ": "Not enough data yet",
    "ยิ่งนั่งนาน ท่ายิ่งแย่ไหม?": "Does posture get worse the longer you sit?",
    "คะแนนท่าดีแยกตามเวลาที่นั่งมาแล้วในแต่ละรอบ (นับใหม่ทุกครั้งที่ลุกพักเกิน 2 นาที)":
        "Good-posture score by how long you've been sitting (resets after a break of over 2 minutes)",
    "สาเหตุที่ทำให้ค่อม (สัดส่วน)": "What causes the slouching (share)",
    "ยังไม่มีช่วงที่ค่อม/เตือนมากพอ จึงยังวิเคราะห์สาเหตุไม่ได้ 👍": "Not enough slouching yet to analyze the causes 👍",
    "นั่งต่อเนื่องนานที่สุด {t} โดยไม่ลุก ลองตั้งใจลุกยืดเส้นทุก 45-60 นาที 🚶":
        "Longest unbroken sitting: {t}. Try to stand and stretch every 45-60 minutes 🚶",
    "ยิ่งนั่งนานท่ายิ่งแย่: ช่วงแรกนั่งดี {a:.0f}% แต่พอนั่งนานเหลือ {b:.0f}%":
        "Posture worsens the longer you sit: {a:.0f}% good at first, {b:.0f}% after sitting long",
    "นั่งนานแล้วท่ายังนิ่งเหมือนเดิม ทำได้ดีมาก 👏": "Posture holds steady even after long sitting. Great job 👏",
    "ค่อมชัดเจน {n} ครั้ง ใช้เวลาแก้ท่าเฉลี่ยครั้งละ {t}": "Clear slouches: {n}, fixed in {t} on average",
    "🧾  ส่งออก JSON": "🧾  Export JSON",
    "ข้อมูลรายวันแบบละเอียด (ไว้ต่อยอดไปทำ dashboard)": "Detailed daily data (ready for building a dashboard)",
})


# ----------------------------------------------------------------------------
#  ภาษา (TH/EN) + ธีมสี  (ข้อความในโค้ดเขียนเป็นภาษาไทย แล้วแปลตอนแสดงผลด้วย tr())
# ----------------------------------------------------------------------------
LANG = "th"


def set_lang(code):
    global LANG
    LANG = code if code in ("th", "en") else "th"


def tr(s):
    """แปลข้อความไทย -> อังกฤษ ถ้าใช้ภาษาไทยอยู่หรือไม่มีคำแปล จะคืนข้อความเดิม"""
    if LANG != "en" or not s:
        return s
    return EN.get(s, s)


THEMES = {
    "light": dict(name="สว่าง", dark=False, bg="#f6f7fb", card="#ffffff", border="#e3e6ee", text="#23272f",
                  head="#4a5160", hint="#7b8190", btn="#ffffff", btn_b="#cfd4e0", btn_h="#eef1f9",
                  btn_p="#e1e6f4", primary="#4a6cf7", primary_h="#3d5ce0", chk_b="#9aa3b5", prog="#e6e9f1"),
    "dark": dict(name="มืด", dark=True, bg="#1b1e25", card="#262a33", border="#363b47", text="#e6e8ee",
                 head="#b9c0d0", hint="#8d96a8", btn="#2d323d", btn_b="#40475a", btn_h="#363c4a",
                 btn_p="#414859", primary="#6b8cff", primary_h="#5a7cf0", chk_b="#6c7488", prog="#383d4a"),
    "sakura": dict(name="ซากุระ", dark=False, bg="#fff4f7", card="#ffffff", border="#f6d5e0", text="#4a2f3a",
                   head="#7a4a5c", hint="#a8808f", btn="#ffffff", btn_b="#efc4d3", btn_h="#ffe8ef",
                   btn_p="#ffd6e3", primary="#ec5f8c", primary_h="#d94f7c", chk_b="#d9a5b8", prog="#f8dfe8"),
}
THEME_KEY = "light"


def set_theme(key):
    global THEME_KEY
    THEME_KEY = key if key in THEMES else "light"


def theme():
    return THEMES[THEME_KEY]


def blend(c1, c2, t):
    """ผสมสี c1 เข้า c2 ด้วยสัดส่วน t (0..1) -> '#rrggbb'"""
    a, b = QColor(c1), QColor(c2)
    mix = lambda x, y: int(round(y + (x - y) * t))
    return QColor(mix(a.red(), b.red()), mix(a.green(), b.green()), mix(a.blue(), b.blue())).name()


_QSS = Template("""
QWidget { background: $bg; color: $text; }
QFrame#card { background: $card; border: 1px solid $border; border-radius: 14px; }
QLabel { background: transparent; }
QLabel#h { font-size: 11pt; font-weight: bold; color: $head; }
QLabel#hint { color: $hint; font-size: 9pt; }
QPushButton { background: $btn; border: 1px solid $btn_b; border-radius: 10px; padding: 9px 12px; }
QPushButton:hover { background: $btn_h; }
QPushButton:pressed { background: $btn_p; }
QPushButton#primary { background: $primary; color: white; border: none;
                      font-size: 12pt; font-weight: bold; padding: 13px; }
QPushButton#primary:hover { background: $primary_h; }
QPushButton[seg="true"] { padding: 8px 6px; }
QPushButton[seg="true"]:checked { background: $primary; color: white; border-color: $primary; }
QCheckBox { spacing: 10px; padding: 3px 0; background: transparent; }
QCheckBox::indicator { width: 18px; height: 18px; border: 2px solid $chk_b;
                       border-radius: 5px; background: $card; }
QCheckBox::indicator:checked { background: $primary; border-color: $primary; }
QScrollArea { background: transparent; }
QComboBox { background: $card; border: 1px solid $btn_b; border-radius: 8px; padding: 6px 10px; }
QComboBox QAbstractItemView { background: $card; color: $text; selection-background-color: $primary;
                              selection-color: white; }
QProgressBar { background: $prog; border: none; border-radius: 5px; height: 10px; }
QProgressBar::chunk { border-radius: 5px; }
QToolTip { background: $card; color: $text; border: 1px solid $border; padding: 4px; }
""")


def make_qss():
    return _QSS.substitute(theme())


# ----------------------------------------------------------------------------
#  สถิติท่านั่ง (เก็บเป็น %APPDATA%\PostureShimeji\stats.json  ไม่ผูกกับ Qt -> ทดสอบง่าย)
# ----------------------------------------------------------------------------
STAT_IDX = {"normal": 0, "warning": 1, "bad": 2, "no_person": 3}
CAUSE_KEYS = ("head", "fwd", "scale", "tilt")
GOAL = 0.70               # เป้าหมาย: นั่งท่าดีอย่างน้อย 70% ของเวลาที่ตรวจจับ
SESSION_BREAK = 120.0     # ไม่อยู่หน้ากล้องนานเท่านี้ (วินาที) = ลุกพัก / จบรอบการนั่ง 1 รอบ
FATIGUE_STEP = 1800.0     # แบ่งรอบการนั่งเป็นช่วงละ 30 นาที (ดูว่านั่งนานแล้วท่าแย่ลงไหม)


class Stats:
    """days[YYYY-MM-DD] = {"h": {"13": [ดี, เตือน, ค่อม, ไม่อยู่ (วินาที)]}, "c": [สาเหตุ x4], "n": {นับครั้ง},
                          "m": {ค่าสูงสุด/ผลรวม เช่น good_max, sess_max, sc, scs},
                          "f": [[ดี, ทั้งหมด] x4 ช่วงของรอบการนั่ง]}   (m, f เพิ่มใน v10 ไฟล์เก่าอ่านได้ปกติ)"""

    KEEP_DAYS = 120

    def __init__(self, path=None):
        self.path = Path(path) if path else APP_DIR / "stats.json"
        self.days = {}
        self.dirty = False
        self._good_run = self._sess = self._away = 0.0     # ตัวนับชั่วคราว (ไม่เก็บลงไฟล์)
        self._prev = None
        try:
            if self.path.exists():
                self.days = json.loads(self.path.read_text(encoding="utf-8")).get("days", {})
        except Exception as e:
            log(f"stats load error: {e}")
            self.days = {}

    # ----- เขียน -----
    def _day(self, key):
        d = self.days.setdefault(key, {"h": {}, "c": [0.0, 0.0, 0.0, 0.0], "n": {}})
        d.setdefault("m", {})
        d.setdefault("f", [[0.0, 0.0] for _ in range(4)])
        return d

    def add(self, status, dt, parts=None, now=None, score=None):
        i = STAT_IDX.get(status)
        if i is None or dt <= 0:
            return
        t = time.localtime(now if now is not None else time.time())
        d = self._day(time.strftime("%Y-%m-%d", t))
        h = d["h"].setdefault(str(t.tm_hour), [0.0, 0.0, 0.0, 0.0])
        h[i] += dt
        if parts and status in ("warning", "bad"):
            for j, k in enumerate(CAUSE_KEYS):
                d["c"][j] += float(parts.get(k, 0.0)) * dt
        self._track(d, status, dt, score)
        self.dirty = True

    def _track(self, d, status, dt, score):
        """ตามดู 'รอบการนั่ง' / 'ช่วงนั่งดีต่อเนื่อง' / 'ครั้งที่ค่อม' แบบเรียลไทม์"""
        m, n = d["m"], d["n"]
        if status == "no_person":
            self._away += dt
            self._good_run = 0.0
            if self._sess > 0 and self._away >= SESSION_BREAK:
                self._end_session(d, broke=True)
        else:
            self._away = 0.0
            self._sess += dt
            m["sess_max"] = max(m.get("sess_max", 0.0), self._sess)
            fb = d["f"][min(3, int(self._sess // FATIGUE_STEP))]
            fb[1] += dt
            if status == "normal":
                fb[0] += dt
                self._good_run += dt
                m["good_max"] = max(m.get("good_max", 0.0), self._good_run)
            else:
                self._good_run = 0.0
            if score is not None:
                m["sc"] = m.get("sc", 0.0) + float(score) * dt
                m["scs"] = m.get("scs", 0.0) + dt
            if status == "bad":
                if self._prev != "bad":
                    n["bad_ep"] = n.get("bad_ep", 0) + 1
                n["bad_ep_s"] = n.get("bad_ep_s", 0.0) + dt
        self._prev = status

    def _end_session(self, d, broke):
        if self._sess >= 60:
            d["n"]["sessions"] = d["n"].get("sessions", 0) + 1
            if broke:
                d["n"]["breaks"] = d["n"].get("breaks", 0) + 1
        self._sess = 0.0
        self.dirty = True

    def close_session(self):
        """เรียกตอนปิดโปรแกรม: นับรอบการนั่งที่ค้างอยู่ (ไม่นับเป็นการลุกพัก)"""
        if self._sess >= 60:
            self._end_session(self._day(time.strftime("%Y-%m-%d")), broke=False)

    def bump(self, name, n=1, now=None):
        t = time.localtime(now if now is not None else time.time())
        d = self._day(time.strftime("%Y-%m-%d", t))
        d["n"][name] = d["n"].get(name, 0) + n
        self.dirty = True

    def save(self, force=False):
        if not (self.dirty or force):
            return
        try:
            keep = sorted(self.days)[-self.KEEP_DAYS:]
            self.days = {k: self.days[k] for k in keep}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"days": self.days}, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(self.path))
            self.dirty = False
        except Exception as e:
            log(f"stats save error: {e}")

    def clear(self):
        self.days = {}
        self.dirty = True
        self.save(force=True)

    # ----- อ่าน -----
    @staticmethod
    def date_keys(n, offset=0):
        """รายการวันที่ n วัน (เก่า -> ใหม่) โดยวันสุดท้ายคือ (วันนี้ - offset)"""
        end = datetime.date.today() - datetime.timedelta(days=offset)
        return [(end - datetime.timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]

    def totals(self, keys):
        t = {"n": 0.0, "w": 0.0, "b": 0.0, "x": 0.0, "alerts": 0, "pet": 0, "snack": 0,
             "cause": [0.0, 0.0, 0.0, 0.0]}
        for k in keys:
            d = self.days.get(k)
            if not d:
                continue
            for v in d["h"].values():
                t["n"] += v[0]
                t["w"] += v[1]
                t["b"] += v[2]
                t["x"] += v[3]
            for j in range(4):
                t["cause"][j] += d["c"][j]
            for name in ("alerts", "pet", "snack"):
                t[name] += d["n"].get(name, 0)
        return t

    def hourly(self, keys):
        out = [[0.0, 0.0, 0.0, 0.0] for _ in range(24)]
        for k in keys:
            d = self.days.get(k)
            if not d:
                continue
            for hh, v in d["h"].items():
                for j in range(4):
                    out[int(hh)][j] += v[j]
        return out

    def daily(self, keys):
        out = []
        for k in keys:
            v = [0.0, 0.0, 0.0, 0.0]
            for hv in (self.days.get(k) or {"h": {}})["h"].values():
                for j in range(4):
                    v[j] += hv[j]
            out.append((k, v))
        return out

    def summary(self, keys):
        """ตัวเลขรวมของช่วงวันที่เลือก (ใช้ทั้งหน้าสถิติและส่งออก JSON)"""
        t = self.totals(keys)
        tracked = t["n"] + t["w"] + t["b"]
        act = 0
        sc = scs = good_max = sess_max = 0.0
        sessions = breaks = bad_ep = 0
        bad_ep_s = 0.0
        for k in keys:
            d = self.days.get(k)
            if not d:
                continue
            if sum(sum(v[:3]) for v in d["h"].values()) >= 60:
                act += 1
            m, n = d.get("m", {}), d.get("n", {})
            sc += m.get("sc", 0.0)
            scs += m.get("scs", 0.0)
            good_max = max(good_max, m.get("good_max", 0.0))
            sess_max = max(sess_max, m.get("sess_max", 0.0))
            sessions += n.get("sessions", 0)
            breaks += n.get("breaks", 0)
            bad_ep += n.get("bad_ep", 0)
            bad_ep_s += n.get("bad_ep_s", 0.0)
        return {
            "tracked": tracked, "away": t["x"], "good": t["n"], "warn": t["w"], "bad": t["b"],
            "score": Stats.score([t["n"], t["w"], t["b"]]),
            "days_active": act, "avg_day": tracked / act if act else 0.0,
            "avg_penalty": sc / scs * 100.0 if scs >= 60 else None,       # 0..100 ยิ่งต่ำยิ่งดี
            "good_max": good_max, "sess_max": sess_max,
            "sessions": sessions, "breaks": breaks,
            "avg_sess": tracked / sessions if sessions else None,
            "bad_ep": bad_ep, "bad_avg": bad_ep_s / bad_ep if bad_ep else None,
            "alerts": t["alerts"], "pet": t["pet"], "snack": t["snack"], "cause": t["cause"],
        }

    def fatigue(self, keys, min_seconds=120.0):
        """คะแนนท่าดีแยกตามช่วงของรอบการนั่ง [0-30, 30-60, 60-90, 90+ นาที] -> [ (สัดส่วน|None, วินาที) x4 ]"""
        acc = [[0.0, 0.0] for _ in range(4)]
        for k in keys:
            for i, v in enumerate((self.days.get(k) or {}).get("f", [])[:4]):
                acc[i][0] += v[0]
                acc[i][1] += v[1]
        return [((g / tt) if tt >= min_seconds else None, tt) for g, tt in acc]

    def heatmap(self, keys):
        """ตาราง 7 วัน x 24 ชั่วโมง = [ดี(วินาที), ที่ตรวจจับ(วินาที)]"""
        grid = [[[0.0, 0.0] for _ in range(24)] for _ in range(7)]
        for k in keys:
            d = self.days.get(k)
            if not d:
                continue
            wd = datetime.date.fromisoformat(k).weekday()
            for hh, v in d["h"].items():
                c = grid[wd][int(hh)]
                c[0] += v[0]
                c[1] += v[0] + v[1] + v[2]
        return grid

    def goal_streak(self, goal=GOAL, min_seconds=600.0):
        """จำนวนวันติดต่อกัน (นับถึงวันนี้/เมื่อวาน) ที่นั่งดีถึงเป้า"""
        n, today = 0, datetime.date.today()
        for off in range(0, 120):
            k = (today - datetime.timedelta(days=off)).isoformat()
            v = [0.0, 0.0, 0.0]
            for hv in (self.days.get(k) or {"h": {}})["h"].values():
                for j in range(3):
                    v[j] += hv[j]
            tracked = sum(v)
            if tracked < min_seconds:
                if off == 0:
                    continue                       # วันนี้ยังไม่เริ่ม -> ไม่ตัดสาย
                break
            if v[0] / tracked >= goal:
                n += 1
            elif off == 0:
                continue                           # วันนี้ยังไม่ถึงเป้า แต่ยังไม่จบวัน
            else:
                break
        return n

    def day_summary(self, k):
        d = self.days.get(k) or {"h": {}, "c": [0, 0, 0, 0], "n": {}}
        s = self.summary([k])
        hourly = [[0.0, 0.0, 0.0, 0.0] for _ in range(24)]
        for hh, v in d["h"].items():
            hourly[int(hh)] = [round(x, 1) for x in v]
        return {
            "date": k, "tracked_s": round(s["tracked"]), "good_s": round(s["good"]),
            "warning_s": round(s["warn"]), "bad_s": round(s["bad"]), "away_s": round(s["away"]),
            "good_ratio": None if s["score"] is None else round(s["score"], 4),
            "avg_penalty": None if s["avg_penalty"] is None else round(s["avg_penalty"], 1),
            "longest_good_streak_s": round(s["good_max"]), "longest_sitting_s": round(s["sess_max"]),
            "sessions": s["sessions"], "breaks": s["breaks"], "bad_episodes": s["bad_ep"],
            "avg_bad_episode_s": None if s["bad_avg"] is None else round(s["bad_avg"], 1),
            "alerts": s["alerts"], "pet": s["pet"], "snack": s["snack"],
            "causes": dict(zip(CAUSE_KEYS, [round(x, 1) for x in s["cause"]])),
            "hourly": hourly,
        }

    def export_json(self, path, keys=None):
        """ส่งออกข้อมูลรายวันเป็น JSON (โครงสร้างพร้อมต่อยอดไปทำ dashboard บนเว็บ)"""
        keys = keys or sorted(self.days)
        doc = {"app": APP_NAME, "schema": 1,
               "generated": datetime.datetime.now().isoformat(timespec="seconds"),
               "goal": GOAL, "days": [self.day_summary(k) for k in keys]}
        Path(path).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def score(v, min_seconds=60.0):
        """สัดส่วนเวลาที่นั่งดี (0..1) จาก [ดี, เตือน, ค่อม, ...] หรือ None ถ้าข้อมูลน้อยเกินไป"""
        tracked = v[0] + v[1] + v[2]
        return None if tracked < min_seconds else v[0] / tracked

    def export_csv(self, path):
        rows = ["date,hour,good_s,warning_s,bad_s,away_s"]
        for k in sorted(self.days):
            for hh in sorted(self.days[k]["h"], key=int):
                v = self.days[k]["h"][hh]
                rows.append(f"{k},{int(hh)},{v[0]:.0f},{v[1]:.0f},{v[2]:.0f},{v[3]:.0f}")
        Path(path).write_text("\n".join(rows) + "\n", encoding="utf-8-sig")


def fmt_dur(sec):
    """วินาที -> '2 ชม. 5 น.' / '2h 5m'"""
    m = int(round(sec / 60.0))
    h, m = divmod(m, 60)
    if h:
        return tr("{h} ชม. {m} น.").format(h=h, m=m)
    return tr("{m} น.").format(m=m)


# ----------------------------------------------------------------------------
#  หาช่วงขอบบนของหน้าต่างที่ไม่ถูกหน้าต่างอื่นบัง (ฟังก์ชันล้วน ทดสอบได้)
# ----------------------------------------------------------------------------
def free_span(left, right, y, blockers, min_w=200):
    """ช่วง x (l, r) ที่ยาวที่สุดบนเส้น y ของ [left,right] ที่ไม่ถูก blockers (l,t,r,b) บัง"""
    segs = [(left, right)]
    for bl, bt, br, bb in blockers:
        if bt <= y <= bb:
            nxt = []
            for a, b in segs:
                if br <= a or bl >= b:
                    nxt.append((a, b))
                    continue
                if bl > a:
                    nxt.append((a, bl))
                if br < b:
                    nxt.append((br, b))
            segs = nxt
    segs = [s for s in segs if s[1] - s[0] >= min_w]
    return max(segs, key=lambda s: s[1] - s[0]) if segs else None


# ----------------------------------------------------------------------------
#  1) วิเคราะห์ท่านั่ง  (ไม่ผูกกับ Qt / mediapipe -> ทดสอบง่าย)
# ----------------------------------------------------------------------------
NOSE, L_EAR, R_EAR, L_SH, R_SH = 0, 7, 8, 11, 12
DEFAULT_BASE = {"ratio": 0.62, "angle": 15.0, "scale": None, "tilt": 0.0}


def compute_metrics(img, world, w, h):
    """
    img   : ndarray (33,4)  x,y (0-1), z, visibility   -- pose_landmarks
    world : ndarray (33,4)  x,y,z (เมตร) หรือ None       -- pose_world_landmarks
    คืน dict ของ metric หรือ None ถ้าเห็นตัวไม่ชัดพอ
    """
    if img[L_SH, 3] < 0.45 or img[R_SH, 3] < 0.45:
        return None
    heads = [i for i in (L_EAR, R_EAR) if img[i, 3] >= 0.4]
    if not heads:
        if img[NOSE, 3] >= 0.5:
            heads = [NOSE]
        else:
            return None

    P = img[:, :2] * np.array([w, h], dtype=float)
    sh_vec = P[L_SH] - P[R_SH]
    sh_w = float(np.hypot(sh_vec[0], sh_vec[1]))
    if sh_w < 0.08 * w:          # ไกลเกินไป / เล็กเกินไป
        return None
    sh_mid = (P[L_SH] + P[R_SH]) / 2.0
    head = P[heads].mean(axis=0)

    ratio = float((sh_mid[1] - head[1]) / sh_w)       # หัวสูงกว่าไหล่กี่เท่าของความกว้างไหล่
    scale = sh_w / float(h)                            # ไหล่ใหญ่ขึ้น = โน้มตัวเข้าหาจอ
    tilt = float(math.degrees(math.atan2(sh_vec[1], sh_vec[0])))   # ไหล่เอียง

    angle = 0.0                                        # มุมคอยื่นไปข้างหน้า (องศา)
    if world is not None:
        wp = world[:, :3]
        d = wp[heads].mean(axis=0) - (wp[L_SH] + wp[R_SH]) / 2.0
        up = -d[1]
        forward = -d[2]                                # z น้อย = ใกล้กล้อง
        angle = float(math.degrees(math.atan2(forward, max(up, 1e-4))))

    return {"ratio": ratio, "angle": angle, "scale": float(scale), "tilt": tilt}


def score_posture(m, base=None):
    """คะแนน 0 (ท่าดีเท่าตอน calibrate) -> 1 (แย่มาก)"""
    b = dict(DEFAULT_BASE)
    if base:
        b.update({k: v for k, v in base.items() if v is not None})
    drop = (b["ratio"] - m["ratio"]) / max(b["ratio"], 1e-3)
    p_head = clamp01((drop - 0.04) / 0.18)                     # หัวจมลงเทียบไหล่
    p_fwd = clamp01((m["angle"] - b["angle"] - 6.0) / 20.0)    # คอยื่น
    p_scale = 0.0
    if b["scale"]:
        p_scale = clamp01((m["scale"] / b["scale"] - 1.04) / 0.20)   # โน้มเข้าหาจอ
    p_tilt = clamp01((abs(m["tilt"] - b["tilt"]) - 5.0) / 10.0)      # ไหล่เอียง
    score = clamp01(0.45 * p_head + 0.25 * p_fwd + 0.25 * p_scale + 0.15 * p_tilt)
    return score, {"head": p_head, "fwd": p_fwd, "scale": p_scale, "tilt": p_tilt}


def classify(score, current, k=1.0):
    w, b, hy = WARN_TH * k, BAD_TH * k, HYST * k
    if score >= (b - hy if current == "bad" else b):
        return "bad"
    if score >= (w - hy if current in ("warning", "bad") else w):
        return "warning"
    return "normal"


class StatusMachine:
    """กันสถานะกระพริบ: ต้องอยู่ในท่าใหม่ต่อเนื่องครบเวลาถึงจะเปลี่ยน"""

    def __init__(self, initial="no_person"):
        self.status = initial
        self.cand = None
        self.cand_t = 0.0

    def force(self, s):
        self.status, self.cand = s, None

    def update(self, raw, now):
        if raw == self.status:
            self.cand = None
            return self.status
        if raw != self.cand:
            self.cand, self.cand_t = raw, now
        el = now - self.cand_t
        delay = ENTER_DELAY[raw]
        if self.status == "no_person":
            delay = 1.0
        if raw == "bad" and self.status == "normal" and el >= ENTER_DELAY["warning"]:
            self.status = "warning"            # ไต่ระดับ: เตือนก่อน แล้วค่อย bad
            return self.status
        if el >= delay:
            self.status, self.cand = raw, None
        return self.status


# ----------------------------------------------------------------------------
#  2) Thread กล้อง + MediaPipe
# ----------------------------------------------------------------------------
def open_camera(idx):
    import cv2
    backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY] if sys.platform == "win32" \
        else [cv2.CAP_ANY]
    for be in backends:
        cap = cv2.VideoCapture(idx, be)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            ok, _ = cap.read()
            if ok:
                return cap
        cap.release()
    return None


def list_cameras():
    """ชื่อกล้องที่ DirectShow เห็น เรียงเหมือนดัชนีของ OpenCV (CAP_DSHOW) คืน [] ถ้าอ่านไม่ได้"""
    if sys.platform != "win32":
        return []
    try:                                           # pragma: no cover  (Windows เท่านั้น)
        import ctypes
        from ctypes import POINTER, WINFUNCTYPE, byref, c_long, c_ulong, c_void_p, wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                        ("d", ctypes.c_ubyte * 8)]

        class VARIANT(ctypes.Structure):
            _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                        ("r3", ctypes.c_ushort), ("ptr", c_void_p), ("pad", c_void_p)]

        ole32 = ctypes.windll.ole32
        oleaut = ctypes.windll.oleaut32

        def guid(txt):
            g = GUID()
            ole32.CLSIDFromString(txt, byref(g))
            return g

        def vcall(obj, idx, restype, argtypes, *args):
            vt = ctypes.cast(obj, POINTER(c_void_p))[0]
            fn = ctypes.cast(vt, POINTER(c_void_p))[idx]
            return WINFUNCTYPE(restype, c_void_p, *argtypes)(fn)(obj, *args)

        def release(obj):
            if obj:
                vcall(obj, 2, c_ulong, ())

        ole32.CoInitialize(None)
        dev = c_void_p()
        if ole32.CoCreateInstance(byref(guid("{62BE5D10-60EB-11d0-BD3B-00A0C911CE86}")), None, 1,
                                  byref(guid("{29840822-5B84-11D0-BD3B-00A0C911CE86}")), byref(dev)) != 0:
            return []
        en = c_void_p()
        hr = vcall(dev, 3, c_long, (POINTER(GUID), POINTER(c_void_p), wintypes.DWORD),
                   byref(guid("{860BB310-5D01-11d0-BD3B-00A0C911CE86}")), byref(en), 0)
        names = []
        if hr == 0 and en:
            iid_bag = guid("{55272A00-42CB-11CE-8135-00AA004BB851}")
            while True:
                mon, got = c_void_p(), wintypes.ULONG(0)
                if vcall(en, 3, c_long, (wintypes.ULONG, POINTER(c_void_p), POINTER(wintypes.ULONG)),
                         1, byref(mon), byref(got)) != 0 or got.value == 0:
                    break
                name, bag = "", c_void_p()
                if vcall(mon, 9, c_long, (c_void_p, c_void_p, POINTER(GUID), POINTER(c_void_p)),
                         None, None, byref(iid_bag), byref(bag)) == 0 and bag:
                    var = VARIANT()
                    if vcall(bag, 3, c_long, (wintypes.LPCWSTR, c_void_p, c_void_p),
                             "FriendlyName", byref(var), None) == 0 and var.vt == 8 and var.ptr:
                        name = ctypes.wstring_at(var.ptr)
                        oleaut.VariantClear(byref(var))
                    release(bag)
                release(mon)
                names.append(name or f"Camera {len(names) + 1}")
        release(en)
        release(dev)
        return names
    except Exception as e:
        log(f"list_cameras failed: {e}")
        return []


class Tracker(QThread):
    result = Signal(object)     # dict(valid, metrics)
    preview = Signal(QImage)
    calib = Signal(object)      # dict(phase, remaining, baseline)
    error = Signal(str)

    def __init__(self, cam_index=0):
        super().__init__()
        self.cam_index = cam_index
        self.paused = False
        self.want_preview = False
        self._stop_flag = False
        self._reopen = False
        self._calib_req = None

    def stop(self):
        self._stop_flag = True

    def reopen_camera(self, idx):
        self.cam_index = idx
        self._reopen = True

    def request_calibration(self, delay=5.0, duration=5.0):
        self._calib_req = (delay, duration)

    def run(self):
        try:
            import cv2
            import mediapipe as mp
        except Exception as e:                       # pragma: no cover
            self.error.emit(f"โหลด mediapipe/opencv ไม่ได้: {e}")
            return
        pose = mp.solutions.pose.Pose(
            static_image_mode=False, model_complexity=1, smooth_landmarks=True,
            enable_segmentation=False, min_detection_confidence=0.5,
            min_tracking_confidence=0.5)
        cap, last_try, fails, last_err = None, 0.0, 0, 0.0
        calib = None
        interval = 1.0 / TARGET_FPS
        conns = [(L_SH, R_SH), (L_EAR, L_SH), (R_EAR, R_SH), (L_EAR, R_EAR),
                 (11, 23), (12, 24), (23, 24)]

        while not self._stop_flag:
            t0 = time.time()
            if self.paused or self._reopen:
                if cap is not None:
                    cap.release()
                    cap = None
                self._reopen = False
                if self.paused:
                    time.sleep(0.2)
                    continue
            if cap is None:
                if time.time() - last_try < 3.0:
                    self.result.emit({"valid": False, "metrics": None})
                    time.sleep(0.3)
                    continue
                last_try = time.time()
                cap = open_camera(self.cam_index)
                if cap is None:
                    if time.time() - last_err > 30:
                        self.error.emit("เปิดกล้องไม่ได้ (ถูกโปรแกรมอื่นใช้อยู่หรือยังไม่ได้อนุญาต)")
                        last_err = time.time()
                    self.result.emit({"valid": False, "metrics": None})
                    continue
                log("camera opened")
            ok, frame = cap.read()
            if not ok:
                fails += 1
                if fails > 10:
                    cap.release()
                    cap = None
                    fails = 0
                time.sleep(0.05)
                continue
            fails = 0
            h, w = frame.shape[:2]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            rgb.flags.writeable = False
            res = pose.process(rgb)

            metrics, img = None, None
            if res.pose_landmarks:
                img = np.array([[l.x, l.y, l.z, l.visibility]
                                for l in res.pose_landmarks.landmark])
                world = None
                if res.pose_world_landmarks:
                    world = np.array([[l.x, l.y, l.z, l.visibility]
                                      for l in res.pose_world_landmarks.landmark])
                metrics = compute_metrics(img, world, w, h)
            valid = metrics is not None

            # ---- calibration ----
            now = time.time()
            if self._calib_req:
                d, dur = self._calib_req
                calib = {"t0": now, "delay": d, "dur": dur, "samples": []}
                self._calib_req = None
            if calib:
                el = now - calib["t0"]
                if el < calib["delay"]:
                    self.calib.emit({"phase": "wait", "remaining": calib["delay"] - el})
                elif el < calib["delay"] + calib["dur"]:
                    if valid:
                        calib["samples"].append(metrics)
                    self.calib.emit({"phase": "hold",
                                     "remaining": calib["delay"] + calib["dur"] - el})
                else:
                    sm = calib["samples"]
                    if len(sm) >= 8:
                        base = {k: float(np.median([s[k] for s in sm]))
                                for k in ("ratio", "angle", "scale", "tilt")}
                        self.calib.emit({"phase": "done", "baseline": base})
                    else:
                        self.calib.emit({"phase": "fail"})
                    calib = None

            self.result.emit({"valid": valid, "metrics": metrics})

            if self.want_preview:
                vis = frame.copy()
                if img is not None:
                    pts = (img[:, :2] * np.array([w, h])).astype(int)
                    for a, b in conns:
                        cv2.line(vis, tuple(pts[a]), tuple(pts[b]), (80, 220, 120), 2)
                    for i in (NOSE, L_EAR, R_EAR, L_SH, R_SH):
                        cv2.circle(vis, tuple(pts[i]), 5, (60, 140, 255), -1)
                vis = cv2.flip(vis, 1)
                vis = cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)
                qi = QImage(vis.data, w, h, 3 * w, QImage.Format_RGB888).copy()
                self.preview.emit(qi)

            time.sleep(max(0.0, interval - (time.time() - t0)))

        if cap is not None:
            cap.release()
        pose.close()


# ----------------------------------------------------------------------------
#  อ่านตำแหน่งหน้าต่างบน Windows (ctypes ล้วน ไม่ต้องติดตั้งเพิ่ม)
#  ใช้ให้มาสคอตกระโดดขึ้นไปนั่งบนขอบบนของหน้าต่างโปรแกรมอื่น
# ----------------------------------------------------------------------------
class WindowTracker:
    SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                    "Windows.UI.Core.CoreWindow", "ApplicationFrameInputSinkWindow",
                    "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland"}

    def __init__(self):
        self.enabled = True
        self.rects = []           # [(hwnd, l, t, r, b)] เรียงจากบนสุดลงล่าง (l..r = ช่วงขอบบนที่มองเห็น)
        self._by = {}
        self._stamp = 0.0
        self.ok = sys.platform == "win32"
        self._api = None
        if self.ok:
            try:
                self._init_api()
            except Exception as e:                 # pragma: no cover
                log(f"window tracker disabled: {e}")
                self.ok = False

    def _init_api(self):                           # pragma: no cover  (Windows เท่านั้น)
        import ctypes
        from ctypes import wintypes
        u32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u32.EnumWindows.argtypes = [proc, wintypes.LPARAM]
        u32.IsWindowVisible.argtypes = [wintypes.HWND]
        u32.IsIconic.argtypes = [wintypes.HWND]
        u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        self._api = (ctypes, wintypes, u32, dwm, proc)

    def refresh(self, force=False):
        if not (self.ok and self.enabled):
            self.rects, self._by = [], {}
            return
        now = time.time()
        if not force and now - self._stamp < 0.5:
            return
        self._stamp = now
        try:
            self.rects = self._scan()
        except Exception as e:                     # pragma: no cover
            log(f"window scan error: {e}")
            self.rects = []
        self._by = {r[0]: r[1:] for r in self.rects}

    def rect_of(self, hwnd):
        """(l, t, r, b) ของขอบบนที่ยังมองเห็น หรือ None ถ้าหน้าต่างปิด/ย่อ/ถูกบังหมด"""
        self.refresh()
        return self._by.get(hwnd)

    def _scan(self):                               # pragma: no cover  (Windows เท่านั้น)
        ctypes, wintypes, u32, dwm, proc = self._api
        scr = QGuiApplication.primaryScreen()
        dpr = max(0.5, float(scr.devicePixelRatio())) if scr else 1.0
        pid = os.getpid()
        out, above = [], []
        buf = ctypes.create_unicode_buffer(80)
        ext, cloaked, owner = wintypes.RECT(), wintypes.DWORD(), wintypes.DWORD()

        def cb(hwnd, _lp):
            try:
                if not u32.IsWindowVisible(hwnd) or u32.IsIconic(hwnd):
                    return True
                u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if owner.value == pid:
                    return True
                ex = u32.GetWindowLongW(hwnd, -20)
                if ex & 0x80 or ex & 0x08000000 or ex & 0x20:    # TOOLWINDOW / NOACTIVATE / TRANSPARENT
                    return True
                if u32.GetWindowTextLengthW(hwnd) == 0:
                    return True
                u32.GetClassNameW(hwnd, buf, 80)
                if buf.value in self.SKIP_CLASSES:
                    return True
                dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)     # DWMWA_CLOAKED
                if cloaked.value:
                    return True
                if dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(ext), ctypes.sizeof(ext)) != 0:
                    u32.GetWindowRect(hwnd, ctypes.byref(ext))                     # ไม่มี DWM -> ใช้ขอบปกติ
                l, t, r, b = ext.left / dpr, ext.top / dpr, ext.right / dpr, ext.bottom / dpr
                if r - l < 200 or b - t < 100:
                    return True
                span = free_span(l, r, t + 3, above)
                above.append((l, t, r, b))
                if span:
                    out.append((int(hwnd), int(span[0]), int(t), int(span[1]), int(b)))
            except Exception:
                pass
            return True

        u32.EnumWindows(proc(cb), 0)
        return out


# ----------------------------------------------------------------------------
#  3) วาดมาสคอต (วาดด้วยโค้ด ไม่ต้องมีไฟล์รูป) + รองรับรูป sprite ของคุณเอง
# ----------------------------------------------------------------------------
PALETTE = {
    "normal": ("#FFF6E5", "#5B4636"),
    "warning": ("#FFE7A0", "#6B4E16"),
    "bad": ("#FFB0A6", "#7A1F1A"),
    "no_person": ("#D5DFF2", "#3F4D6B"),
    "calibrating": ("#D4F1E1", "#2F5E49"),
}
CAT_FILL = {"normal": "#F9A97A", "warning": "#F9B98A", "bad": "#F27B5E",
            "no_person": "#E2B9A4", "calibrating": "#F9A97A"}
CAT_INK = "#2B1B17"

# ตัวละครที่มีให้เลือก (เพิ่มตัวใหม่ได้ที่นี่ + ใน draw_mascot)
SPECIES = {
    "cat": {"name": "แมวส้ม", "ink": "#2B1B17", "ow": 6.5, "head": 106,
            "hello": "เมี้ยว~ แมวส้มมาแล้ว!", "fill": CAT_FILL},
    "bunny": {"name": "กระต่ายชมพู", "ink": "#C0627F", "ow": 4.8, "head": 148,
              "hello": "สวัสดีค่ะ~ กระต่ายชมพูมาแล้ว!",
              "fill": {"normal": "#FFBBD0", "warning": "#FFC9D6", "bad": "#FF8DAB",
                       "no_person": "#E8CAD6", "calibrating": "#FFBBD0"}},
    "shiba": {"name": "ชิบะ", "ink": "#8F5524", "ow": 4.8, "head": 112,
              "hello": "โฮ่ง! ชิบะมารายงานตัวแล้ว!",
              "fill": {"normal": "#F3A64B", "warning": "#F6B766", "bad": "#EE7C4B",
                       "no_person": "#DDBB93", "calibrating": "#F3A64B"}},
    "raccoon": {"name": "แรคคูน", "ink": "#16161C", "ow": 6.5, "head": 132,
                "hello": "หวัดดีจ้า~ แรคคูนจอมซนมาแล้ว!",
                "fill": {"normal": "#A8A8AF", "warning": "#B8B09E", "bad": "#BC9899",
                         "no_person": "#98A2B8", "calibrating": "#A5B7AC"}},
}
FONT_FAMILIES = ["Leelawadee UI", "Tahoma", "Segoe UI", "Noto Sans Thai", "Arial"]


def _pen(c, w):
    return QPen(c, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)


def draw_mascot(p, status, frame, mode, direction=1, vy=0.0, sprite=None, species="cat"):
    """วาดตัวละคร โดย origin (0,0) = กึ่งกลางเท้า, แกน y ลบ = ขึ้นบน"""
    p.save()
    p.scale(direction, 1)
    fill_hex, line_hex = PALETTE.get(status, PALETTE["normal"])
    fill, line = QColor(fill_hex), QColor(line_hex)
    pen = _pen(line, 3)

    sleeping = (mode in ("sleep", "nap")) or status == "no_person"
    airborne = mode in ("fall", "drag")
    walking = mode in ("walk", "run")
    spd = 0.5 if mode == "walk" else 0.85
    ph = math.sin(frame * spd) if walking else 0.0
    ph2 = math.sin(frame * 0.35)

    bob = abs(ph) * 3.0 if walking else math.sin(frame * 0.09) * 1.3
    sy = 1.0
    if mode in ("fall", "jump"):
        sy = 1.0 + max(-0.10, min(0.18, -vy * 0.012))
    elif sleeping:
        sy = 1.0 + 0.03 * math.sin(frame * 0.08)
        bob = 0
    if status == "bad" and mode != "drag":
        p.translate(random.uniform(-1.5, 1.5), 0)
    if status == "normal":                       # ท่าทางสบายๆ ตอนนั่งดี
        if mode == "sit":
            sy, bob = 0.93, math.sin(frame * 0.05) * 0.8
        elif mode == "stretch":
            sy = 1.0 + 0.14 * abs(math.sin(frame * 0.06))
        elif mode == "dance":
            p.translate(math.sin(frame * 0.25) * 6, 0)
            p.rotate(math.sin(frame * 0.25) * 5)
            bob = abs(math.sin(frame * 0.5)) * 3
        elif mode == "look":
            p.rotate(math.sin(frame * 0.07) * 4)
        elif mode == "spin":
            bob = abs(math.sin(frame * 0.6)) * 4
        elif mode == "wave":
            bob = abs(math.sin(frame * 0.35)) * 1.5
        elif mode == "bark":
            bob = abs(math.sin(frame * 0.6)) * 3
    p.scale(1.0 / sy, sy)
    p.translate(0, -bob)

    # ---------- sprite ของผู้ใช้ ----------
    if sprite is not None:
        h = 110.0
        w = h * sprite.width() / max(1, sprite.height())
        p.drawPixmap(QRectF(-w / 2, -h, w, h), sprite, QRectF(sprite.rect()))
        if sleeping:
            _draw_zzz(p, frame, line)
        p.restore()
        return

    p.setRenderHint(QPainter.Antialiasing)
    if species not in SPECIES:
        species = "cat"
    if species == "raccoon":                     # แรคคูนวาดแยกฟังก์ชัน (ท่าเฉพาะตัวเยอะ)
        _draw_raccoon(p, status, frame, mode, vy, sleeping, airborne, walking, spd, ph, ph2)
        p.restore()
        return
    cfg_s = SPECIES[species]
    cat, bunny, shiba = species == "cat", species == "bunny", species == "shiba"
    fill = QColor(cfg_s["fill"].get(status, cfg_s["fill"]["normal"]))
    shade = fill.darker(125)
    light = fill.lighter(115)
    ink = QColor(cfg_s["ink"])
    dark = ink if cat else QColor("#3B2622")     # สีเส้นหน้า (คิ้ว/ปาก/ตาหลับ)
    ow = cfg_s["ow"]                              # ความหนาเส้นขอบ (วาดสองรอบ เห็นจริงครึ่งนึง)
    bad = status == "bad"
    warn = status == "warning"
    HY = -67                                   # กึ่งกลางหัว

    # ---------- หาง ----------
    fast = mode in ("spin", "dance", "wave", "tailwag")
    slow = mode in ("sit", "nap", "sleep")
    wag = math.sin(frame * (0.55 if mode == "tailwag" else (0.5 if fast else (0.07 if slow else 0.18)))) \
        * (14 if fast else (10 if warn else 6))
    if cat:
        tail = QPainterPath(QPointF(-32, -18))
        tail.cubicTo(QPointF(-78, -14), QPointF(-84, -58 + wag), QPointF(-62, -84 + wag))
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(ink, 11 + ow))
        p.drawPath(tail)
        p.setPen(_pen(fill, 11))
        p.drawPath(tail)
        for t in (0.50, 0.66, 0.82):
            a_, b_, c_ = tail.pointAtPercent(t - 0.01), tail.pointAtPercent(t + 0.01), tail.pointAtPercent(t)
            dx, dy = b_.x() - a_.x(), b_.y() - a_.y()
            n = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / n * 5.2, dx / n * 5.2
            p.setPen(QPen(shade, 3.6, Qt.SolidLine, Qt.FlatCap))
            p.drawLine(QPointF(c_.x() - nx, c_.y() - ny), QPointF(c_.x() + nx, c_.y() + ny))
    elif bunny:                                  # หางปุยกลมสีขาว
        p.setPen(_pen(ink, ow))
        p.setBrush(QColor("#FFF7F9"))
        p.drawEllipse(QPointF(-42 + wag * 0.35, -15), 15, 14)
    else:                                        # ชิบะ: หางม้วนฟู ปลายสีครีม
        tail = QPainterPath(QPointF(-33, -20))
        tail.cubicTo(QPointF(-72, -14), QPointF(-84, -50 + wag), QPointF(-62, -66 + wag))
        tail.cubicTo(QPointF(-48, -74 + wag), QPointF(-40, -56 + wag * 0.6), QPointF(-52, -50 + wag * 0.4))
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(ink, 15 + ow))
        p.drawPath(tail)
        p.setPen(_pen(fill, 15))
        p.drawPath(tail)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#FFF1DC"))
        p.drawEllipse(tail.pointAtPercent(1.0), 7, 7)

    # ---------- หู + ตัว + หัว (วาดเส้นขอบรวมก่อน แล้วค่อยลงสี ได้เป็นก้อนเดียวกัน) ----------
    droop = 8 if (warn or status == "no_person") else (4 if bad else 0)
    out = 7 if bad else 0

    def bunny_ang(sd):                           # องศาหูกระต่าย (ยิ่งมากยิ่งตก)
        if bad:
            return 62
        if warn:
            return 32
        if sleeping:
            return 42
        if mode == "earflop":
            return 10 + 20 * math.sin(frame * 0.55 + (0 if sd > 0 else 1.8))
        if mode == "dance":
            return 8 + 9 * math.sin(frame * 0.5)
        if mode == "stretch":
            return 0
        return 8 + 3 * math.sin(frame * 0.06 + (0 if sd > 0 else 0.9))

    tipx, tipy = (37, -112) if shiba else (36, -108)
    ears = []
    if not bunny:
        for sd in (-1, 1):
            ears.append(QPolygonF([QPointF(sd * 43, -75), QPointF(sd * (tipx + out), tipy + droop + out),
                                   QPointF(sd * 10, -98)]))

    def silhouette():
        if bunny:
            for sd in (-1, 1):
                p.save()
                p.translate(sd * 19, -92)
                p.rotate(sd * bunny_ang(sd))
                p.drawEllipse(QPointF(0, -25), 12, 28)
                p.restore()
        for e in ears:
            p.drawPolygon(e)
        p.drawEllipse(QPointF(0, -35), 46, 33)
        p.drawEllipse(QPointF(0, HY), 43, 32)

    p.setPen(_pen(ink, ow))
    p.setBrush(ink)
    silhouette()
    p.setPen(Qt.NoPen)
    p.setBrush(fill)
    silhouette()

    # หูด้านใน
    for sd in (-1, 1):
        p.setPen(Qt.NoPen)
        if bunny:
            p.save()
            p.translate(sd * 19, -92)
            p.rotate(sd * bunny_ang(sd))
            p.setBrush(QColor("#FF9DBB"))
            p.drawEllipse(QPointF(0, -26), 6.6, 20)
            p.restore()
        else:
            p.setBrush(QColor("#FFF0DA") if shiba else shade)
            ty = -104 if shiba else -100
            p.drawPolygon(QPolygonF([QPointF(sd * 37, -78), QPointF(sd * (34 + out * 0.8), ty + droop + out),
                                     QPointF(sd * 17, -92)]))
            if cat:
                p.setPen(_pen(ink, 1.6))
                d = droop + out * 0.8
                p.drawLine(QPointF(sd * 31, -92 + d), QPointF(sd * 35, -93 + d))
                p.drawLine(QPointF(sd * 30, -87 + d), QPointF(sd * 34, -88 + d))

    # ท้อง + ลายประจำตัว
    p.setPen(Qt.NoPen)
    if cat:
        belly = QColor(light)
        belly.setAlpha(200)
        p.setBrush(belly)
        p.drawEllipse(QPointF(0, -24), 27, 17)
        p.setPen(_pen(shade, 3.2))
        for sd in (-1, 1):
            p.drawLine(QPointF(sd * 40, -46), QPointF(sd * 45, -43))
            p.drawLine(QPointF(sd * 41, -38), QPointF(sd * 46, -36))
            p.drawLine(QPointF(sd * 40, -30), QPointF(sd * 44, -29))
    elif bunny:
        p.setBrush(QColor("#FFF8FA"))
        p.drawEllipse(QPointF(0, -26), 26, 20)         # ท้องขาว
        p.drawEllipse(QPointF(0, -52), 13, 9)          # ปากขาว
        _flower(p, 31, -94, 5.4, "#FF7FA5", "#FFD95A", "#E85D88")   # ดอกไม้ที่หู
        _flower(p, 42, -84, 3.2, "#FFFFFF", "#FFD95A", "#F2B8C8")
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#7CC66B"))
        p.drawEllipse(QPointF(42, -96), 6, 2.8)
    else:
        p.setBrush(QColor("#FFF3E0"))
        p.drawEllipse(QPointF(0, -27), 27, 22)         # อกสีครีม
        for sd in (-1, 1):
            p.drawEllipse(QPointF(sd * 19, -55), 18, 13)   # แก้มครีม
        p.drawEllipse(QPointF(0, -51), 13, 10)
        # ผ้าพันคอสีเขียวลายจุด
        sc, scd = QColor("#A9D66B"), QColor("#7FB045")
        arc = QPainterPath(QPointF(-36, -42))
        arc.quadTo(QPointF(0, -28), QPointF(36, -42))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(scd, 14, Qt.SolidLine, Qt.RoundCap))
        p.drawPath(arc)
        p.setPen(QPen(sc, 10, Qt.SolidLine, Qt.RoundCap))
        p.drawPath(arc)
        knot = QPolygonF([QPointF(24, -37), QPointF(37, -40), QPointF(42, -23), QPointF(29, -21)])
        p.setPen(_pen(scd, 2))
        p.setBrush(sc)
        p.drawPolygon(knot)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#E7F5C6"))
        for t in (0.12, 0.31, 0.50, 0.69, 0.88):
            p.drawEllipse(arc.pointAtPercent(t), 1.7, 1.7)
        p.drawEllipse(QPointF(33, -31), 1.7, 1.7)
        p.drawEllipse(QPointF(36, -26), 1.5, 1.5)

    # ---------- แขน ----------
    for s in (-1, 1):
        sh = QPointF(s * 37, -44)
        if mode == "climb":
            sh = QPointF(38, -44)
            hand = QPointF(54, -88 + ph2 * 8) if s > 0 else QPointF(52, -60 - ph2 * 8)
        elif airborne:
            hand = QPointF(s * 52, -76 + math.sin(frame * 0.8 + s) * 6)
        elif walking:
            hand = QPointF(s * 44, -26 + s * ph * 7)
        elif mode == "carrot":
            hand = QPointF(s * 11, -44 + math.sin(frame * 0.5 + s) * 1.5)
        elif mode == "yarn" and s > 0:
            hand = QPointF(50, -22 + 10 * abs(math.sin(frame * 0.24)))
        elif mode == "wave" and s > 0:
            hand = QPointF(48 + math.sin(frame * 0.7) * 7, -94)
        elif mode == "wash" and s > 0:
            hand = QPointF(16 + math.sin(frame * 0.8) * 3, -57 + math.cos(frame * 0.8) * 3)
        elif mode == "stretch":
            hand = QPointF(s * 44, -102 + math.sin(frame * 0.12) * 3)
        elif mode == "dance":
            hand = QPointF(s * 48, -88 + math.sin(frame * 0.5 + s * 1.6) * 10)
        elif mode == "sit":
            hand = QPointF(s * 21, -20)
        elif warn and not sleeping:
            hand = QPointF(s * 14, -30 + math.sin(frame * 0.5 + s) * 2)
        elif bad:
            hand = QPointF(s * 46, -28)
        else:
            hand = QPointF(s * 41, -22)
        p.setPen(_pen(ink, 7.5 + ow))
        p.drawLine(sh, hand)
        p.setPen(_pen(fill, 7.5))
        p.drawLine(sh, hand)
        if mode != "climb" and not airborne:
            p.setPen(_pen(ink, 1.8))
            for dx in (-2.4, 2.4):
                p.drawLine(QPointF(hand.x() + dx, hand.y() + 1), QPointF(hand.x() + dx, hand.y() + 5))

    # ---------- เท้า ----------
    p.setPen(_pen(ink, ow * 0.43))
    p.setBrush(fill)
    for s in (-1, 1):
        lift = 0.0
        if walking:
            fx = s * 17 + s * ph * 9
            lift = max(0.0, s * math.cos(frame * spd)) * 5
        elif airborne or mode == "jump":
            fx = s * 15 + math.sin(frame * 0.5 + s) * 3
            lift = 3
        elif mode == "climb":
            fx = s * 15 + 8
            lift = 4 if s * ph2 > 0 else 0
        elif mode == "sit":
            fx = s * 13
        elif mode == "dance":
            fx = s * 17
            lift = max(0.0, math.sin(frame * 0.5 + (0 if s > 0 else math.pi))) * 4
        else:
            fx = s * 17
        p.drawEllipse(QPointF(fx, -6 - lift), 14, 6.5)

    # ---------- หน้า ----------
    ex, ey = (19, HY + 1) if cat else (21, HY + 3)
    lx = 2.4 + 1.6 * math.sin(frame * 0.03)       # สายตาเหลือบไปมาเบาๆ
    ly = -1.6

    def eye(x, y, rx, ry, pdx, pdy, pr):
        if not cat:                              # ตากลมมันวาวสีเข้ม มีประกาย 2 จุด
            ew, eh = rx * 0.62, ry * 0.78
            cx, cy = x + pdx * 0.35, y + pdy * 0.35
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#2B1A17"))
            p.drawEllipse(QPointF(cx, cy), ew, eh)
            p.setBrush(QColor(110, 70, 60, 150))
            p.drawEllipse(QPointF(cx, cy + eh * 0.38), ew * 0.62, eh * 0.38)
            p.setBrush(QColor("white"))
            p.drawEllipse(QPointF(cx - ew * 0.32, cy - eh * 0.36), ew * 0.38, ew * 0.38)
            p.drawEllipse(QPointF(cx + ew * 0.38, cy + eh * 0.36), ew * 0.19, ew * 0.19)
            return
        p.setPen(_pen(dark, 2.4))
        p.setBrush(QColor("#F8ECE9"))
        p.drawEllipse(QPointF(x, y), rx, ry)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(232, 196, 198, 200))
        p.drawEllipse(QPointF(x - 1.5, y - ry * 0.35), rx * 0.62, ry * 0.45)
        p.setBrush(QColor("#34201B"))
        p.drawEllipse(QPointF(x + pdx, y + pdy), pr, pr * 1.1)
        p.setBrush(QColor("white"))
        p.drawEllipse(QPointF(x + pdx - pr * 0.38, y + pdy - pr * 0.42), pr * 0.34, pr * 0.34)
        p.drawEllipse(QPointF(x + pdx + pr * 0.42, y + pdy + pr * 0.4), pr * 0.17, pr * 0.17)

    def closed_eyes(w=8.0):
        p.setPen(_pen(dark, 2.6))
        p.setBrush(Qt.NoBrush)
        for s in (-1, 1):
            p.drawArc(QRectF(s * ex - w, ey - 4, w * 2, 10), 200 * 16, 140 * 16)

    def nose():
        if bunny:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#FF86A5"))
            p.drawPolygon(QPolygonF([QPointF(-3.0, -58), QPointF(3.0, -58), QPointF(0, -54.6)]))
            return
        if shiba:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#3B2622"))
            p.drawEllipse(QPointF(0, -57), 4.6, 3.3)
            p.setBrush(QColor("white"))
            p.drawEllipse(QPointF(-1.3, -58.2), 1.1, 0.8)
            return
        p.setPen(_pen(dark, 1.3))
        p.setBrush(QColor("#E98AA3"))
        p.drawPolygon(QPolygonF([QPointF(-3.6, -58.5), QPointF(3.6, -58.5), QPointF(0, -54.6)]))

    def open_mouth(w=6.0, h=6.0, y=-52.5):
        m = QPainterPath(QPointF(-w, y))
        m.quadTo(QPointF(0, y + 1.5), QPointF(w, y))
        m.cubicTo(QPointF(w - 1, y + h), QPointF(-w + 1, y + h), QPointF(-w, y))
        p.setPen(_pen(dark, 1.6))
        p.setBrush(QColor("#E5607F"))
        p.drawPath(m)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#FFA6BA"))
        p.drawEllipse(QPointF(0, y + h * 0.62), w * 0.5, h * 0.3)
        if bunny:                                # ฟันหน้ากระต่าย
            p.setPen(_pen(dark, 1.1))
            p.setBrush(QColor("white"))
            p.drawRoundedRect(QRectF(-2.6, y + 0.4, 5.2, 3.8), 1.2, 1.2)

    def blush(col):
        p.setPen(Qt.NoPen)
        if not cat and col.green() > 100:        # แก้มชมพูฟูๆ แบบในภาพอ้างอิง
            col = QColor(255, 128, 165, min(255, col.alpha() + 60))
        p.setBrush(col)
        rx_, ry_ = (6.5, 3.8) if cat else (8.0, 5.0)
        for s in (-1, 1):
            p.drawEllipse(QPointF(s * (32 if cat else 36), -56 if cat else -53), rx_, ry_)

    def brows(inner_y, outer_y, w=3.0):
        p.setPen(_pen(dark, w))
        for s in (-1, 1):
            p.drawLine(QPointF(s * 30, outer_y), QPointF(s * 9, inner_y))

    def forehead_dots():
        if bunny:
            return
        if shiba:                                # จุดคิ้วสีครีมของชิบะ
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#FFF3DE"))
            for s in (-1, 1):
                p.drawEllipse(QPointF(s * 19, -83), 4.4, 3.2)
            return
        p.setPen(_pen(dark, 1.3))
        p.setBrush(light)
        p.drawEllipse(QPointF(-3.5, -88), 1.7, 1.7)
        p.drawEllipse(QPointF(4.5, -89), 1.7, 1.7)

    happy = status == "normal" and mode in ("wave", "dance", "spin", "jump", "earflop", "tailwag")
    bfx = bfy = 0.0
    if status == "normal" and mode == "look":
        lx, ly = 7 * math.sin(frame * 0.07), -1.0 + 1.5 * math.sin(frame * 0.11)
    elif status == "normal" and mode == "yarn":
        lx, ly = 4.0 + 1.5 * math.sin(frame * 0.12), 3.0
    elif status == "normal" and mode == "butterfly":
        bfx, bfy = 58 * math.sin(frame * 0.045), -104 + 20 * math.sin(frame * 0.09)
        lx, ly = max(-5.0, min(5.0, bfx / 9)), max(-5.0, min(2.0, (bfy - HY) / 9))

    def happy_eyes():
        p.setPen(_pen(dark, 2.8))
        p.setBrush(Qt.NoBrush)
        for s in (-1, 1):
            p.drawArc(QRectF(s * ex - 8, ey - 5, 16, 11), 20 * 16, 140 * 16)

    if sleeping:
        closed_eyes()
        nose()
        p.setPen(_pen(dark, 1.8))
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(-4, -55, 8, 6), 200 * 16, 140 * 16)
        forehead_dots()
        _draw_zzz(p, frame, dark)
    elif airborne:
        for s in (-1, 1):
            eye(s * ex, ey, 13, 12.5, 0, 0, 4.2)
        nose()
        p.setPen(_pen(dark, 1.6))
        p.setBrush(QColor("#E5607F"))
        p.drawEllipse(QPointF(0, -48.5), 3.6, 4.6)
        forehead_dots()
    elif bad:
        for s in (-1, 1):
            eye(s * ex, ey + 1, 11.5, 8.5, s * -1.5, 1, 4.4)
        brows(-74, -83, 3.4)
        nose()
        p.setPen(_pen(dark, 1.6))
        p.setBrush(QColor("white"))
        p.drawRoundedRect(QRectF(-8, -53.5, 16, 7.5), 2.5, 2.5)
        p.drawLine(QPointF(-4, -53.5), QPointF(-4, -46))
        p.drawLine(QPointF(0, -53.5), QPointF(0, -46))
        p.drawLine(QPointF(4, -53.5), QPointF(4, -46))
        blush(QColor(255, 60, 60, 150))
        k = 1.0 + 0.2 * math.sin(frame * 0.5)          # เครื่องหมายโมโห
        p.setPen(_pen(QColor("#E53935"), 3))
        cx, cy = 40, -92
        p.drawLine(QPointF(cx - 3 * k, cy - 8 * k), QPointF(cx - 3 * k, cy + 8 * k))
        p.drawLine(QPointF(cx + 3 * k, cy - 8 * k), QPointF(cx + 3 * k, cy + 8 * k))
        p.drawLine(QPointF(cx - 8 * k, cy - 3 * k), QPointF(cx + 8 * k, cy - 3 * k))
        p.drawLine(QPointF(cx - 8 * k, cy + 3 * k), QPointF(cx + 8 * k, cy + 3 * k))
    elif warn:
        sx = 2.2 * math.sin(frame * 0.6)                # ตากวาดไปมาแบบกังวล
        for s in (-1, 1):
            eye(s * ex, ey, 12.5, 12, sx, 0, 4.6)
        brows(-82, -75, 2.8)
        nose()
        zz = QPainterPath(QPointF(-7, -50))
        for x, y in ((-3.5, -52.5), (0, -50), (3.5, -52.5), (7, -50)):
            zz.lineTo(x, y)
        p.setPen(_pen(dark, 2.2))
        p.setBrush(Qt.NoBrush)
        p.drawPath(zz)
        blush(QColor(255, 140, 140, 90))
        x, y = 36, -92 + math.sin(frame * 0.2) * 2     # เหงื่อ
        d = QPainterPath(QPointF(x, y - 9))
        d.cubicTo(QPointF(x + 7, y - 1), QPointF(x + 6, y + 6), QPointF(x, y + 6))
        d.cubicTo(QPointF(x - 6, y + 6), QPointF(x - 7, y - 1), QPointF(x, y - 9))
        p.setBrush(QColor("#8FD3FF"))
        p.setPen(_pen(QColor("#3E8FC4"), 1.5))
        p.drawPath(d)
        forehead_dots()
    else:   # normal / calibrating
        calm = status == "calibrating"
        if mode == "wash" and not calm:
            closed_eyes()
            nose()
            p.setPen(_pen(dark, 1.4))
            p.setBrush(QColor("#FFA6BA"))
            p.drawEllipse(QPointF(2 + math.sin(frame * 0.8) * 2, -50), 3.2, 3.8)
        elif mode == "stretch" and not calm:
            closed_eyes()
            nose()
            open_mouth(8, 12, -54)
        elif mode == "carrot" and not calm:
            happy_eyes()
            nose()
            ch = 1.5 + 2.5 * abs(math.sin(frame * 0.5))     # ท่าเคี้ยว
            open_mouth(4 + ch * 0.4, 2 + ch, -52.5)
        elif mode == "bark" and not calm:
            for s in (-1, 1):
                eye(s * ex, ey, 12.5, 12, 0, 0, 6.6)
            nose()
            if (frame // 12) % 2 == 0:
                open_mouth(8, 12, -54)
            else:
                open_mouth(5, 4)
        elif happy and not calm:
            happy_eyes()
            nose()
            open_mouth(7, 7)
        else:
            if (frame % 130) < 5:
                closed_eyes()
            else:
                for s in (-1, 1):
                    eye(s * ex, ey, 12.5, 12, lx, ly, 6.6)
            nose()
            if calm:
                p.setPen(_pen(dark, 2.2))
                p.drawLine(QPointF(-4, -51), QPointF(4, -51))
            else:
                open_mouth()
        blush(QColor(255, 120, 130, 90))
        forehead_dots()
        if not calm:
            if mode in ("wave", "sit", "jump", "tailwag"):
                _float_fx(p, frame, "heart")
            elif mode == "dance":
                _float_fx(p, frame, "note")
            elif mode == "butterfly":
                _butterfly(p, bfx, bfy, frame)
            elif mode == "carrot":
                _carrot(p, frame)
            elif mode == "yarn":
                _yarn(p, frame)
            elif mode == "bark":
                _bark_waves(p, frame)
    p.restore()


# ----------------------------------------------------------------------------
#  แรคคูน (ตามภาพอ้างอิง: หน้ากากสีเข้มขอบขาว ปากขาว ท้องขาว หางลายวง อุ้งมือ/เท้าสีดำ)
# ----------------------------------------------------------------------------
def _sparkle(p, x, y, r, color):
    s = QPainterPath(QPointF(x, y - r))
    s.quadTo(QPointF(x, y), QPointF(x + r, y))
    s.quadTo(QPointF(x, y), QPointF(x, y + r))
    s.quadTo(QPointF(x, y), QPointF(x - r, y))
    s.quadTo(QPointF(x, y), QPointF(x, y - r))
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawPath(s)


def _apple(p, x, y, r):
    p.setPen(_pen(QColor("#9E2B25"), 1.8))
    p.setBrush(QColor("#EF5350"))
    p.drawEllipse(QPointF(x, y), r, r * 0.94)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 150))
    p.drawEllipse(QPointF(x - r * 0.4, y - r * 0.38), r * 0.2, r * 0.3)
    p.setPen(_pen(QColor("#6B4A2B"), 1.8))
    p.drawLine(QPointF(x, y - r * 0.85), QPointF(x + 1, y - r * 1.3))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#6CC24A"))
    p.drawEllipse(QPointF(x + r * 0.6, y - r * 1.12), r * 0.5, r * 0.24)


def _cookie(p, x, y, r):
    p.setPen(_pen(QColor("#8A5A2B"), 1.8))
    p.setBrush(QColor("#E3B46F"))
    p.drawEllipse(QPointF(x, y), r, r)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#5B3A1E"))
    for cx, cy in ((-.35, -.3), (.3, -.35), (.05, .1), (-.4, .35), (.4, .28)):
        p.drawEllipse(QPointF(x + cx * r, y + cy * r), r * 0.17, r * 0.17)


def _gem(p, x, y, r, frame):
    rw = r * (0.35 + 0.65 * abs(math.cos(frame * 0.08)))
    d = QPolygonF([QPointF(x - rw * 0.6, y - r * 0.6), QPointF(x + rw * 0.6, y - r * 0.6),
                   QPointF(x + rw, y), QPointF(x, y + r), QPointF(x - rw, y)])
    p.setPen(_pen(QColor("#2B7FB0"), 1.8))
    p.setBrush(QColor("#86DBFF"))
    p.drawPolygon(d)
    p.setPen(_pen(QColor(255, 255, 255, 200), 1.2))
    p.drawLine(QPointF(x - rw * 0.6, y - r * 0.6), QPointF(x, y))
    p.drawLine(QPointF(x + rw * 0.6, y - r * 0.6), QPointF(x, y))
    p.drawLine(QPointF(x, y), QPointF(x, y + r))


def _water_drops(p, frame, cx, cy):
    for i in range(5):
        t = (frame * 0.045 + i / 5.0) % 1.0
        x = cx - 17 + i * 8.5 + math.sin(t * 5 + i) * 2
        y = cy - 4 + t * 20
        c = QColor("#8FD3FF")
        c.setAlphaF(1.0 - t)
        p.setPen(_pen(QColor(62, 143, 196, int(255 * (1 - t))), 1.1))
        p.setBrush(c)
        p.drawEllipse(QPointF(x, y), 1.9, 2.6)
    for i in range(3):                               # ฟองสบู่ลอย
        t = (frame * 0.03 + i / 3.0) % 1.0
        c = QColor(120, 190, 240)
        c.setAlphaF((1.0 - t) * 0.9)
        p.setPen(QPen(c, 1.3))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx - 12 + i * 12 + math.sin(t * 6 + i) * 3, cy - 12 - t * 20), 3 + i, 3 + i)


def _crumbs(p, frame):
    for i in range(4):
        t = (frame * 0.05 + i / 4.0) % 1.0
        c = QColor("#C99A55")
        c.setAlphaF(1.0 - t)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawEllipse(QPointF(-9 + i * 6 + math.sin(t * 7 + i) * 2, -44 + t * 26), 1.5, 1.5)


def _draw_raccoon(p, status, frame, mode, vy, sleeping, airborne, walking, spd, ph, ph2):
    """วาดแรคคูน (origin = กึ่งกลางเท้า, y ลบ = ขึ้นบน) ท่าทั้งหมดอยู่ในฟังก์ชันนี้"""
    cfg_s = SPECIES["raccoon"]
    fill = QColor(cfg_s["fill"].get(status, cfg_s["fill"]["normal"]))
    ink = QColor(cfg_s["ink"])
    ow = cfg_s["ow"]
    dark = fill.darker(205)                      # สีหน้ากาก / ลายหาง
    paw = QColor("#2A2830")
    white = QColor("#FFFFFF")
    soft = QColor("#F3F1F6")
    bad, warn = status == "bad", status == "warning"
    calm = status == "calibrating"
    a = mode if status == "normal" else None     # ท่าน่ารักเล่นได้เฉพาะตอนนั่งดี
    HY = -69

    p.save()
    if a == "sneak":
        p.rotate(math.sin(frame * 0.12) * 3)
    elif a == "tailhug":
        p.rotate(math.sin(frame * 0.06) * 3.5)

    # ---------- หาง (ลายวง ปลายดำ) ----------
    fast = a in ("spin", "dance", "wave")
    slow = mode in ("sit", "nap", "sleep")
    wag = math.sin(frame * (0.5 if fast else (0.07 if slow else 0.18))) * (14 if fast else (10 if warn else 6))

    def draw_tail(path, width):
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(ink, width + ow))
        p.drawPath(path)
        p.setPen(_pen(fill, width))
        p.drawPath(path)
        for t in (0.26, 0.44, 0.62, 0.80):
            a_, b_, c_ = path.pointAtPercent(t - 0.01), path.pointAtPercent(t + 0.01), path.pointAtPercent(t)
            dx, dy = b_.x() - a_.x(), b_.y() - a_.y()
            n = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / n * (width / 2 - 0.5), dx / n * (width / 2 - 0.5)
            p.setPen(QPen(dark, 5.2, Qt.SolidLine, Qt.FlatCap))
            p.drawLine(QPointF(c_.x() - nx, c_.y() - ny), QPointF(c_.x() + nx, c_.y() + ny))
        p.setPen(Qt.NoPen)                       # ปลายหางสีเข้ม
        p.setBrush(dark)
        p.drawEllipse(path.pointAtPercent(1.0), width / 2 - 0.4, width / 2 - 0.4)

    if a != "tailhug":
        tail = QPainterPath(QPointF(-26, -15))
        tail.cubicTo(QPointF(-68, -4), QPointF(-90, -38 + wag * 0.5), QPointF(-72, -78 + wag))
        draw_tail(tail, 22)

    # ---------- หู + ตัว + หัว (ขอบรวมก้อนเดียว) ----------
    droop = 9 if (warn or status == "no_person") else (4 if bad else 0)
    out = 6 if bad else 0
    ears, tufts = [], []
    for sd in (-1, 1):
        ears.append(QPolygonF([QPointF(sd * 13, -96), QPointF(sd * 47, -78),
                               QPointF(sd * (39 + out), -116 + droop + out)]))
        tufts.append(QPolygonF([QPointF(sd * 38, -82), QPointF(sd * 53, -63), QPointF(sd * 39, -55)]))

    def silhouette():
        for e in ears:
            p.drawPolygon(e)
        for t in tufts:
            p.drawPolygon(t)
        p.drawEllipse(QPointF(0, -31), 38, 29)
        p.drawEllipse(QPointF(0, HY), 44, 31)

    p.setPen(_pen(ink, ow))
    p.setBrush(ink)
    silhouette()
    p.setPen(Qt.NoPen)
    p.setBrush(fill)
    silhouette()

    for sd in (-1, 1):                           # ในหูสีเข้ม
        p.setPen(Qt.NoPen)
        p.setBrush(dark)
        p.drawPolygon(QPolygonF([QPointF(sd * 19, -93), QPointF(sd * 41, -80),
                                 QPointF(sd * (37 + out * 0.8), -108 + droop + out)]))

    # ---------- ท้องขาว (มีเส้นขอบตามภาพ) ----------
    p.setPen(_pen(ink, 2.6))
    p.setBrush(white if status != "no_person" else QColor("#EEF2FA"))
    p.drawEllipse(QPointF(0, -25), 20.5, 21)

    # ---------- เท้าสีดำ ----------
    for s in (-1, 1):
        lift = 0.0
        if walking:
            fx = s * 15 + s * ph * 9
            lift = max(0.0, s * math.cos(frame * spd)) * 5
        elif airborne or mode == "jump":
            fx = s * 14 + math.sin(frame * 0.5 + s) * 3
            lift = 3
        elif mode == "climb":
            fx = s * 14 + 8
            lift = 4 if s * ph2 > 0 else 0
        elif mode == "sit":
            fx = s * 12
        elif a == "dance":
            fx = s * 15
            lift = max(0.0, math.sin(frame * 0.5 + (0 if s > 0 else math.pi))) * 4
        elif a == "sneak":
            fx = s * 15
            lift = (1 + math.sin(frame * 0.3 + (0 if s > 0 else math.pi))) * 2.6
        else:
            fx = s * 15
        p.setPen(_pen(ink, 2.4))
        p.setBrush(paw)
        p.drawEllipse(QPointF(fx, -6 - lift), 13.5, 6.5)

    # ---------- หน้ากาก + ปากขาว ----------
    for sd in (-1, 1):
        p.save()
        p.translate(sd * 23, HY - 1)
        p.rotate(sd * 13)
        p.setPen(Qt.NoPen)
        p.setBrush(soft)
        p.drawEllipse(QPointF(0, -2), 23, 16.5)  # ขอบขนสีอ่อนเหนือหน้ากาก
        p.setBrush(dark)
        p.drawEllipse(QPointF(0, 0), 20.5, 14)
        p.restore()
    p.setPen(Qt.NoPen)
    p.setBrush(dark)
    p.drawEllipse(QPointF(0, HY - 1), 9.5, 6)    # สันจมูกเชื่อมหน้ากากสองข้าง
    dk = QColor(dark)
    dk.setAlpha(150)
    p.setBrush(dk)
    p.drawEllipse(QPointF(0, -90), 3.4, 8.5)     # แถบกลางหน้าผาก
    p.setBrush(white if status != "no_person" else QColor("#EEF2FA"))
    p.drawEllipse(QPointF(0, -54), 15.5, 11)     # ปากขาว

    # ---------- ตา / จมูก / ปาก ----------
    ex, ey = 23, HY
    gx = 2.4 + 1.6 * math.sin(frame * 0.03)
    gy = 0.0
    if a == "look":
        gx, gy = 7 * math.sin(frame * 0.07), 1.5 * math.sin(frame * 0.11)
    elif a == "butterfly":
        bfx_, bfy_ = 58 * math.sin(frame * 0.045), -104 + 20 * math.sin(frame * 0.09)
        gx, gy = max(-5.0, min(5.0, bfx_ / 9)), max(-5.0, min(2.0, (bfy_ - HY) / 9))
    elif a == "sneak":
        gx = 7 * math.sin(frame * 0.22)
    elif a == "washfood":
        gx, gy = 0.0, 5.0
    gx = max(-4.5, min(4.5, gx * 0.6))
    gy = max(-3.0, min(3.0, gy * 0.6))

    def eye(x, y, rx, ry, dx=0.0, dy=0.0):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#0F0F14"))
        p.drawEllipse(QPointF(x + dx, y + dy), rx, ry)
        p.setBrush(white)
        p.drawEllipse(QPointF(x + dx - rx * 0.32, y + dy - ry * 0.36), rx * 0.36, rx * 0.36)
        p.drawEllipse(QPointF(x + dx + rx * 0.36, y + dy + ry * 0.36), rx * 0.17, rx * 0.17)

    def closed_eyes():
        p.setPen(_pen(soft, 2.8))
        p.setBrush(Qt.NoBrush)
        for s in (-1, 1):
            p.drawArc(QRectF(s * ex - 7, ey - 4, 14, 9), 200 * 16, 140 * 16)

    def happy_eyes():
        p.setPen(_pen(soft, 2.8))
        p.setBrush(Qt.NoBrush)
        for s in (-1, 1):
            p.drawArc(QRectF(s * ex - 7, ey - 4, 14, 10), 20 * 16, 140 * 16)

    def nose():
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#17171C"))
        p.drawEllipse(QPointF(0, -60), 5.2, 3.7)
        p.setBrush(white)
        p.drawEllipse(QPointF(-1.5, -61.2), 1.3, 0.9)

    def smile(w=6.5, smirk=0.0):                 # ปากรูป ω น่ารักตามภาพ
        p.setPen(_pen(ink, 2.0))
        p.setBrush(Qt.NoBrush)
        m = QPainterPath(QPointF(-w, -54 + smirk))
        m.quadTo(QPointF(-w / 2, -49.5), QPointF(0, -54))
        m.quadTo(QPointF(w / 2, -49.5), QPointF(w, -54 - smirk))
        p.drawPath(m)
        p.drawLine(QPointF(0, -57), QPointF(0, -54))

    def open_mouth(w=5.5, h=6.0, y=-53.0):
        m = QPainterPath(QPointF(-w, y))
        m.quadTo(QPointF(0, y + 1.5), QPointF(w, y))
        m.cubicTo(QPointF(w - 1, y + h), QPointF(-w + 1, y + h), QPointF(-w, y))
        p.setPen(_pen(ink, 1.8))
        p.setBrush(QColor("#E5607F"))
        p.drawPath(m)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#FFA6BA"))
        p.drawEllipse(QPointF(0, y + h * 0.65), w * 0.5, h * 0.28)

    def blush(col):
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        for s in (-1, 1):
            p.drawEllipse(QPointF(s * 33, -53), 6.5, 4.0)

    def brows(inner_y, outer_y, w=3.0):
        p.setPen(_pen(ink, w))
        for s in (-1, 1):
            p.drawLine(QPointF(s * 36, outer_y), QPointF(s * 11, inner_y))

    happy = status == "normal" and mode in ("wave", "dance", "spin", "jump")
    peek_cover = a == "peekaboo" and (frame % 80) < 44

    if sleeping:
        closed_eyes()
        nose()
        p.setPen(_pen(ink, 1.8))
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(-4, -56, 8, 6), 200 * 16, 140 * 16)
        _draw_zzz(p, frame, ink)
    elif airborne:
        for s in (-1, 1):
            eye(s * ex, ey, 8.4, 9.2)
        nose()
        p.setPen(_pen(ink, 1.6))
        p.setBrush(QColor("#E5607F"))
        p.drawEllipse(QPointF(0, -49.5), 3.6, 4.6)
    elif bad:
        for s in (-1, 1):
            eye(s * ex, ey + 1.5, 6.4, 4.8, s * -1.0, 0)
        brows(-82, -93, 3.4)
        nose()
        p.setPen(_pen(ink, 1.6))
        p.setBrush(white)
        p.drawRoundedRect(QRectF(-8, -55, 16, 7.5), 2.5, 2.5)
        for x in (-4, 0, 4):
            p.drawLine(QPointF(x, -55), QPointF(x, -47.5))
        blush(QColor(255, 60, 60, 150))
        k = 1.0 + 0.2 * math.sin(frame * 0.5)
        p.setPen(_pen(QColor("#E53935"), 3))
        cx, cy = 46, -98
        p.drawLine(QPointF(cx - 3 * k, cy - 8 * k), QPointF(cx - 3 * k, cy + 8 * k))
        p.drawLine(QPointF(cx + 3 * k, cy - 8 * k), QPointF(cx + 3 * k, cy + 8 * k))
        p.drawLine(QPointF(cx - 8 * k, cy - 3 * k), QPointF(cx + 8 * k, cy - 3 * k))
        p.drawLine(QPointF(cx - 8 * k, cy + 3 * k), QPointF(cx + 8 * k, cy + 3 * k))
    elif warn:
        sx = 2.0 * math.sin(frame * 0.6)
        for s in (-1, 1):
            eye(s * ex, ey, 8.4, 9.2, sx, 0)
        brows(-93, -86, 2.8)
        nose()
        zz = QPainterPath(QPointF(-7, -51))
        for x, y in ((-3.5, -53.5), (0, -51), (3.5, -53.5), (7, -51)):
            zz.lineTo(x, y)
        p.setPen(_pen(ink, 2.2))
        p.setBrush(Qt.NoBrush)
        p.drawPath(zz)
        blush(QColor(255, 140, 140, 90))
        x, y = 44, -96 + math.sin(frame * 0.2) * 2
        d = QPainterPath(QPointF(x, y - 9))
        d.cubicTo(QPointF(x + 7, y - 1), QPointF(x + 6, y + 6), QPointF(x, y + 6))
        d.cubicTo(QPointF(x - 6, y + 6), QPointF(x - 7, y - 1), QPointF(x, y - 9))
        p.setBrush(QColor("#8FD3FF"))
        p.setPen(_pen(QColor("#3E8FC4"), 1.5))
        p.drawPath(d)
    else:
        if a == "wash":
            closed_eyes()
            nose()
            smile()
        elif a == "stretch":
            closed_eyes()
            nose()
            open_mouth(8, 12, -54)
        elif a == "cookie":
            happy_eyes()
            nose()
        elif a == "tailhug":
            happy_eyes()
            nose()
            smile(7.5)
        elif a == "shiny":
            for s in (-1, 1):
                eye(s * ex, ey, 8.6, 9.4, 0, -1)
                _sparkle(p, s * ex + 3.5, ey - 3.5, 3.4 + 1.2 * math.sin(frame * 0.4 + s), QColor("white"))
            nose()
            open_mouth(4.5, 5.5, -53)
        elif a == "peekaboo" and not peek_cover:
            happy_eyes()
            nose()
            open_mouth(7, 8)
        elif happy:
            happy_eyes()
            nose()
            open_mouth(6.5, 7)
        else:
            if (frame % 130) < 5 and a != "washfood":
                closed_eyes()
            else:
                for s in (-1, 1):
                    eye(s * ex, ey, 8.0, 8.9, gx, gy)
            nose()
            if calm:
                p.setPen(_pen(ink, 2.2))
                p.drawLine(QPointF(-4, -52), QPointF(4, -52))
            elif a == "sneak":
                smile(7, 1.6)
            else:
                smile()
        blush(QColor(255, 120, 130, 95))

    # ---------- ของประกอบฉาก (อยู่ใต้มือ) ----------
    if a == "washfood":
        _apple(p, 0, -33, 12)
    elif a == "cookie":
        r = 10.5 * (1.0 - 0.45 * ((frame % 100) / 100.0))
        _cookie(p, 0, -46, r)
        _crumbs(p, frame)
        ch = 1.2 + 1.6 * abs(math.sin(frame * 0.5))      # แก้มตุ้ยๆ ตอนเคี้ยว
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 130, 140, 120))
        for s in (-1, 1):
            p.drawEllipse(QPointF(s * (35 + ch * 0.5), -53), 7 + ch * 0.3, 4.5 + ch * 0.2)
    elif a == "shiny":
        _gem(p, 0, -118, 10.5, frame)
    elif a == "tailhug":
        ft = QPainterPath(QPointF(-40, -20))
        ft.cubicTo(QPointF(-30, 6), QPointF(26, 8), QPointF(36, -26))
        draw_tail(ft, 17)

    # ---------- แขน + อุ้งมือสีดำ ----------
    for s in (-1, 1):
        sh = QPointF(s * 29, -42)
        if mode == "climb":
            sh = QPointF(30, -42)
            hand = QPointF(54, -88 + ph2 * 8) if s > 0 else QPointF(52, -60 - ph2 * 8)
        elif airborne:
            hand = QPointF(s * 52, -76 + math.sin(frame * 0.8 + s) * 6)
        elif walking:
            hand = QPointF(s * 36, -30 + s * ph * 7)
        elif a == "washfood":
            hand = QPointF(s * 11.5, -33 + math.sin(frame * 0.9 + s * 1.5) * 2.5)
        elif a == "cookie":
            hand = QPointF(s * 13, -44 + math.sin(frame * 0.5 + s) * 1.5)
        elif a == "peekaboo":
            hand = QPointF(s * 21, -69) if peek_cover else QPointF(s * 47, -86 + math.sin(frame * 0.3) * 3)
        elif a == "tailhug":
            hand = QPointF(s * 23, -27)
        elif a == "shiny":
            hand = QPointF(s * 14, -112 + math.sin(frame * 0.2 + s) * 2)
        elif a == "sneak":
            hand = QPointF(8, -52) if s > 0 else QPointF(-30, -30)
        elif a == "wash" and s > 0:
            hand = QPointF(19 + math.sin(frame * 0.8) * 4, -60 + math.cos(frame * 0.8) * 3)
        elif a == "stretch":
            hand = QPointF(s * 44, -104 + math.sin(frame * 0.12) * 3)
        elif a == "dance":
            hand = QPointF(s * 48, -88 + math.sin(frame * 0.5 + s * 1.6) * 10)
        elif a == "wave" and s > 0:
            hand = QPointF(50 + math.sin(frame * 0.7) * 7, -96)
        elif mode == "sit":
            hand = QPointF(s * 17, -27)
        elif warn and not sleeping:
            hand = QPointF(s * 9, -42 + math.sin(frame * 0.5 + s) * 2)
        elif bad:
            hand = QPointF(s * 39, -26)
        elif sleeping:
            hand = QPointF(s * 14, -38)
        else:
            hand = QPointF(s * 13.5, -46)        # ท่าประจำตัว: ยกมือจับคางตามภาพ
        p.setPen(_pen(ink, 8.5 + ow))
        p.drawLine(sh, hand)
        p.setPen(_pen(fill, 8.5))
        p.drawLine(sh, hand)
        p.setPen(_pen(ink, 2.2))
        p.setBrush(paw)
        p.drawEllipse(hand, 6.6, 6.2)
        if mode != "climb" and not airborne:
            p.setPen(_pen(QColor("#6A6672"), 1.3))
            for dx in (-2.2, 2.2):
                p.drawLine(QPointF(hand.x() + dx, hand.y() + 0.5), QPointF(hand.x() + dx, hand.y() + 4.2))

    # ---------- เอฟเฟกต์ลอยๆ ----------
    if status == "normal":
        if a == "washfood":
            _water_drops(p, frame, 0, -33)
        elif a == "shiny":
            for i, (sx_, sy_) in enumerate(((-26, -126), (26, -128), (0, -139), (-17, -108), (17, -110))):
                _sparkle(p, sx_, sy_, 3.0 + 3.0 * abs(math.sin(frame * 0.2 + i * 1.3)),
                         QColor(255, 236, 130, 230))
        elif a == "butterfly":
            _butterfly(p, bfx_, bfy_, frame)
        elif a == "dance":
            _float_fx(p, frame, "note")
        elif a == "peekaboo" and not peek_cover:
            _float_fx(p, frame, "heart")
        elif a in ("wave", "sit", "tailhug") or mode == "jump":
            _float_fx(p, frame, "heart")
    p.restore()


def _flower(p, x, y, r, petal, center, edge):
    p.setPen(_pen(QColor(edge), 1.0))
    p.setBrush(QColor(petal))
    for k in range(5):
        a = math.radians(72 * k - 90)
        p.drawEllipse(QPointF(x + math.cos(a) * r * 1.15, y + math.sin(a) * r * 1.15), r, r)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(center))
    p.drawEllipse(QPointF(x, y), r * 0.72, r * 0.72)


def _carrot(p, frame):
    """แครอทที่ถูกแทะทีละนิด (ความยาวหดลงเป็นรอบๆ)"""
    L = 1.0 - 0.55 * ((frame % 120) / 120.0)
    tipx, hx = -3.0, -3.0 + 34 * L
    body = QPolygonF([QPointF(tipx, -47.5), QPointF(hx, -56), QPointF(hx, -39)])
    p.setPen(QPen(QColor("#D9731E"), 1.3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(QColor("#FF9A3C"))
    p.drawPolygon(body)
    p.drawLine(QPointF(hx - 10 * L, -51), QPointF(hx - 6 * L, -51))
    p.drawLine(QPointF(hx - 16 * L, -45), QPointF(hx - 12 * L, -45))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#6CC24A"))
    p.drawEllipse(QPointF(hx + 5, -52), 7, 3.2)
    p.drawEllipse(QPointF(hx + 6, -44), 7.5, 3.2)


def _yarn(p, frame):
    x, y = 60 + 10 * math.sin(frame * 0.12), -10
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor("#E0577F"), 1.6, Qt.SolidLine, Qt.RoundCap))
    th = QPainterPath(QPointF(x - 9, y + 3))
    th.quadTo(QPointF(x - 22, y + 14), QPointF(x - 30, y + 6))
    p.drawPath(th)
    p.setBrush(QColor("#FF8FB1"))
    p.drawEllipse(QPointF(x, y), 10, 10)
    p.setBrush(Qt.NoBrush)
    a = int((frame * 14) % 360) * 16
    p.drawArc(QRectF(x - 7, y - 7, 14, 14), a, 150 * 16)
    p.drawArc(QRectF(x - 4, y - 9.5, 9, 19), a + 90 * 16, 140 * 16)


def _bark_waves(p, frame):
    t = (frame % 24) / 24.0
    for i in range(3):
        rr = 8 + i * 7 + t * 4
        c = QColor("#6B3A12")
        c.setAlphaF(max(0.0, (1.0 - t) * (1.0 - i * 0.25)))
        p.setPen(QPen(c, 2.8, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(42 - rr, -52 - rr, rr * 2, rr * 2), -40 * 16, 80 * 16)


def _heart(p, x, y, sz, color):
    h = QPainterPath(QPointF(x, y + sz * 0.9))
    h.cubicTo(QPointF(x - sz * 1.6, y - sz * 0.1), QPointF(x - sz * 0.9, y - sz * 1.3), QPointF(x, y - sz * 0.45))
    h.cubicTo(QPointF(x + sz * 0.9, y - sz * 1.3), QPointF(x + sz * 1.6, y - sz * 0.1), QPointF(x, y + sz * 0.9))
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawPath(h)


def _draw_note(p, x, y, color):
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawEllipse(QPointF(x, y), 3.6, 2.7)
    p.setPen(QPen(color, 1.8, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(x + 3.2, y), QPointF(x + 3.2, y - 12))
    p.drawLine(QPointF(x + 3.2, y - 12), QPointF(x + 8, y - 8))


def _float_fx(p, frame, kind):
    """หัวใจ/โน้ตลอยขึ้นเหนือหัว"""
    for i in range(3):
        t = (frame * 0.017 + i / 3.0) % 1.0
        c = QColor("#FF5E8A" if kind == "heart" else "#7A5CFF")
        c.setAlphaF(1.0 - t)
        x = -26 + i * 26 + math.sin(t * 6 + i) * 5
        y = -108 - t * 34
        if kind == "heart":
            _heart(p, x, y, 4.5 + 2 * (1 - t), c)
        else:
            _draw_note(p, x, y, c)


def _butterfly(p, x, y, frame):
    flap = abs(math.cos(frame * 0.5))
    p.setPen(_pen(QColor("#5B3A8C"), 1.3))
    for s in (-1, 1):
        p.setBrush(QColor("#FF9EC8"))
        p.drawEllipse(QPointF(x + s * 5 * flap, y - 3), 6 * flap + 1.5, 7)
        p.setBrush(QColor("#FFC9E0"))
        p.drawEllipse(QPointF(x + s * 4 * flap, y + 4), 4.5 * flap + 1, 5)
    p.setPen(_pen(QColor("#3A2556"), 2.2))
    p.drawLine(QPointF(x, y - 6), QPointF(x, y + 8))
    p.setPen(_pen(QColor("#3A2556"), 1.1))
    p.drawLine(QPointF(x, y - 6), QPointF(x - 3, y - 11))
    p.drawLine(QPointF(x, y - 6), QPointF(x + 3, y - 11))


def _draw_zzz(p, frame, color):
    f = QFont("Arial")
    f.setBold(True)
    for i in range(3):
        t = (frame * 0.015 + i / 3.0) % 1.0
        f.setPointSizeF(8 + t * 8)
        p.setFont(f)
        c = QColor(color)
        c.setAlphaF(1.0 - t)
        p.setPen(c)
        p.drawText(QPointF(26 + t * 16, -90 - t * 34), "z")


def make_icon(status="normal", size=64, species="cat"):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    head = SPECIES.get(species, SPECIES["cat"])["head"]
    k = size / max(120.0, head + 12.0)
    p.translate(size / 2, size * 0.95)
    p.scale(k, k)
    draw_mascot(p, status, 0, "idle", 1, 0.0, None, species)
    p.end()
    return pm


# ----------------------------------------------------------------------------
#  4) ตัวมาสคอตบนเดสก์ท็อป
# ----------------------------------------------------------------------------
# ท่าทางน่ารักตอนนั่งดี: ชื่อ -> (เฟรมต่ำสุด, สูงสุด, น้ำหนักการสุ่ม, ข้อความ)
IDLE_ACTIONS = {
    "sit":       (150, 330, 3, None),
    "wash":      (110, 190, 2, "เลียๆ~ ล้างหน้าก่อน"),
    "stretch":   (80, 120, 2, "ยืดดดด~ หาววว"),
    "wave":      (80, 120, 2, "สวัสดี~ เมี้ยว!"),
    "dance":     (130, 220, 2, "♪ ลา~ลา~ลา"),
    "nap":       (170, 300, 1, "ขอแอบงีบแป๊บ… zZ"),
    "look":      (100, 170, 2, "มีอะไรน่ะ?"),
    "butterfly": (170, 270, 2, "ผีเสื้อ! ผีเสื้อ!"),
    "spin":      (70, 110, 1, "ไล่หางตัวเอง!"),
    "hop":       (1, 1, 2, "ดีใจจัง~"),
    # ท่าเฉพาะตัวละคร (ดู ONLY)
    "carrot":    (170, 260, 3, "ตุบๆ แครอทอร่อยจัง~"),
    "earflop":   (90, 140, 2, "หูกระดิกๆ~"),
    "bark":      (60, 100, 3, "โฮ่ง! โฮ่ง!"),
    "tailwag":   (110, 170, 3, "ดีใจจนหางจะหลุด~"),
    "yarn":      (170, 260, 3, "เล่นไหมพรมๆ~"),
    "washfood":  (170, 260, 3, "ล้างก่อนกินนะ~ ซ่าๆ"),
    "cookie":    (170, 250, 3, "ขโมยคุกกี้มาได้~ แฮ่!"),
    "peekaboo":  (130, 200, 3, "จ๊ะเอ๋!"),
    "tailhug":   (150, 240, 2, "กอดหางตัวเองอุ่นจัง~"),
    "shiny":     (150, 230, 2, "ว้าว! ของแวววาว~"),
    "sneak":     (110, 170, 2, "หึหึ… ไม่มีใครเห็นนะ"),
}
ONLY = {"carrot": "bunny", "earflop": "bunny", "bark": "shiba", "tailwag": "shiba", "yarn": "cat",
        "washfood": "raccoon", "cookie": "raccoon", "peekaboo": "raccoon", "tailhug": "raccoon",
        "shiny": "raccoon", "sneak": "raccoon"}
ACTION_MSG = {("bunny", "wave"): "สวัสดี~ ปิ๊บๆ!", ("shiba", "wave"): "สวัสดี~ โฮ่ง!",
              ("bunny", "spin"): "หมุนติ้วๆ~", ("shiba", "spin"): "ไล่หางตัวเอง! โฮ่งๆ",
              ("shiba", "dance"): "♪ โฮ่ง~ ลา~ลา", ("bunny", "butterfly"): "ผีเสื้อ! ปิ๊บ!",
              ("shiba", "hop"): "ดีใจจังโฮ่ง~", ("bunny", "hop"): "ดีใจจัง ปิ๊บ~",
              ("raccoon", "wave"): "หวัดดี~ จี๊ดๆ!", ("raccoon", "spin"): "ไล่หางลายตัวเอง!",
              ("raccoon", "dance"): "♪ ชะชะช่า~", ("raccoon", "hop"): "ดีใจจัง~ จี๊ด!",
              ("raccoon", "butterfly"): "ผีเสื้อ! จะจับให้ได้!", ("raccoon", "wash"): "ล้างหน้าล้างตา~",
              ("raccoon", "look"): "มีของแวววาวไหมนะ?", ("raccoon", "nap"): "ขอแอบงีบแป๊บ… zZ"}
ACTION_MODES = tuple(k for k in IDLE_ACTIONS if k != "hop")
ACTION_TH = {"sit": "นั่งพัก", "wash": "ล้างหน้า", "stretch": "บิดขี้เกียจ", "wave": "โบกมือทักทาย",
             "dance": "เต้น", "nap": "แอบงีบ", "look": "มองซ้ายขวา", "butterfly": "ไล่ดูผีเสื้อ",
             "spin": "ไล่หางตัวเอง", "hop": "กระโดดดีใจ", "carrot": "กินแครอท", "earflop": "หูกระดิก",
             "bark": "เห่า", "tailwag": "กระดิกหางรัว", "yarn": "เล่นไหมพรม",
             "washfood": "ล้างผลไม้ (ซ่าๆ)", "cookie": "แทะคุกกี้", "peekaboo": "จ๊ะเอ๋",
             "tailhug": "กอดหางตัวเอง", "shiny": "เจอของแวววาว", "sneak": "ย่องหัวเราะหึหึ"}
SPRITE_ACTIONS = ("sit", "dance", "stretch", "look", "spin", "nap", "hop")   # ท่าที่ใช้กับรูป PNG ของผู้ใช้ได้

WIN_W, WIN_H = 300, 330
FOOT_PAD = 6
BW = 46        # ครึ่งความกว้างลำตัว (ใช้คำนวณชนขอบจอ)

BAD_MSGS = ["หลังค่อมแล้ว!\nนั่งตัวตรงๆ เดี๋ยวนี้!",
            "โอ๊ย ปวดหลังแทนเลย\nยืดตัวขึ้นหน่อย!",
            "ไหล่ห่อแล้วนะ!\nเงยหน้า ยืดอกหน่อย!"]
WARN_MSGS = ["เริ่มค่อมแล้วนะ\nยืดหลังหน่อย~",
             "ระวังหลังงอ!\nนั่งให้ตรงอีกนิด",
             "คอเริ่มยื่นแล้วนะ\nถอยหลังมาหน่อย"]
GOOD_MSGS = ["เยี่ยม! ท่านั่งดีแล้ว\nรักษาไว้นะ", "สุดยอด! หลังตรงเลย"]
POKE_MSGS = ["จิ้มทำไมเนี่ย~\nนั่งหลังตรงๆ ด้วยนะ", "เราคอยเฝ้าดูท่านั่งอยู่นะ!",
             "ดื่มน้ำแล้วหรือยัง?", "ลุกเดินสักนิดไหม?"]


FOLLOW_MSGS = ["ตามมาแล้ว~", "เมาส์อยู่นี่เอง!", "ขอตามไปด้วยคน~"]
HOVER_MSGS = ["หืม? มีอะไรเหรอ", "มองอะไรอยู่~", "เมาส์มาใกล้แล้วว"]
PET_MSGS = ["อุ๊ย~ ชอบจัง ♥", "ลูบอีกๆ~", "หัวฟูหมดแล้ว~", "ขอบคุณที่ดูแลนะ ♥"]
EAT_MSGS = ["งั่มๆ~ อร่อยจัง!", "ขอบคุณสำหรับขนมนะ ♥", "อร่อยที่สุดเลย~"]
SNACK_REFUSE = "นั่งหลังตรงก่อนนะ\nแล้วค่อยกินขนม!"
SNACK_WAIT = "เก็บขนมไว้ก่อน\nนั่งหลังตรงๆ แล้วค่อยกินนะ"
SNACK_SEEK = ["ขนม! ขนมมาแล้ว~", "ได้กลิ่นขนมแล้ว!"]
PERCH_MSGS = ["สูงดีจัง~ วิวสวย", "ขอนั่งตรงนี้นะ", "เห็นทั้งจอเลย!"]
PERCH_DOWN = ["ลงละนะ~", "ไปล่ะ ฮึบ!"]
PERCH_GONE = ["อ้าว! หน้าต่างหายไปไหน", "ว้าย! พื้นหายไปแล้ว"]
CHASE_LEAD = ["จับให้ได้สิ~", "ว้าา! ตามมาแล้ว!"]
CHASE_FOLLOW = ["รอเดี๋ยว! อย่าหนีนะ", "เดี๋ยวจับได้แน่~"]
CHASE_CATCH = ["จับได้แล้ว!", "ฮ่าๆ เล่นอีกรอบไหม~"]
GREET = {
    "morning": ["อรุณสวัสดิ์~ วันนี้นั่งหลังตรงๆ กันนะ", "เช้าแล้ว! ยืดเส้นยืดสายก่อนเริ่มงานไหม?"],
    "lunch": ["เที่ยงแล้ว! ไปกินข้าวกันเถอะ", "พักกินข้าวก่อนไหม? ลุกจากเก้าอี้หน่อย~"],
    "evening": ["เย็นแล้ว~ วันนี้เหนื่อยไหม?", "ใกล้เลิกงานแล้ว ยืดตัวหน่อยนะ"],
    "night": ["ดึกแล้วนะ… ไปนอนได้แล้ว", "หาววว~ ง่วงแล้ว พักสายตาหน่อยนะ", "นอนดึกระวังปวดหลังนะ!"],
}
SNACK_OF = {"cat": "fish", "bunny": "carrot", "shiba": "bone", "raccoon": "cookie"}
VOICE_OF = {"cat": "meow", "bunny": "squeak", "shiba": "woof", "raccoon": "chitter"}
NEW_MODES = ("follow", "seek", "eat", "flee", "chase", "leap")


def draw_snack(p, kind, cx, cy, s=1.0):
    """วาดขนม 4 แบบ (ปลา/แครอท/กระดูก/คุกกี้) ที่ตำแหน่ง (cx, cy)"""
    p.save()
    p.translate(cx, cy)
    p.scale(s, s)
    p.setRenderHint(QPainter.Antialiasing)
    ink = QColor("#5B4636")
    if kind == "fish":
        p.setPen(QPen(ink, 2))
        p.setBrush(QColor("#7EB6E8"))
        p.drawPolygon(QPolygonF([QPointF(8, 0), QPointF(19, -8), QPointF(19, 8)]))
        p.drawEllipse(QPointF(-3, 0), 13, 8)
        p.setPen(Qt.NoPen)
        p.setBrush(ink)
        p.drawEllipse(QPointF(-10, -2), 1.8, 1.8)
    elif kind == "carrot":
        p.setPen(QPen(ink, 2))
        p.setBrush(QColor("#5DBB63"))
        for a in (-4, 0, 4):
            p.drawEllipse(QPointF(-16, a), 5, 3)
        p.setBrush(QColor("#FF9A3C"))
        p.drawPolygon(QPolygonF([QPointF(-13, -6), QPointF(-13, 6), QPointF(15, 0)]))
    elif kind == "bone":
        p.setPen(QPen(ink, 2))
        p.setBrush(QColor("#FFF8E8"))
        for sx in (-1, 1):
            for sy in (-1, 1):
                p.drawEllipse(QPointF(sx * 12, sy * 4), 4.2, 4.2)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(-12, -4.5, 24, 9), 3, 3)
    else:                                          # cookie
        p.setPen(QPen(ink, 2))
        p.setBrush(QColor("#D9A066"))
        p.drawEllipse(QPointF(0, 0), 12, 12)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#5A3A22"))
        for dx, dy in ((-4, -4), (5, -2), (-2, 5), (6, 6)):
            p.drawEllipse(QPointF(dx, dy), 2, 2)
    p.restore()


class Mascot(QWidget):
    nag = Signal(str)

    def __init__(self, sprites=None, companion=False):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.NoDropShadowWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.size_k = 1.0                       # ตัวคูณขนาดที่ผู้ใช้เลือก (1.0 = ค่าเดิม)
        self.ww, self.wh = WIN_W, WIN_H
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(False)
        self.resize(self.ww, self.wh)

        self.set_sprites(sprites)

        self.companion = bool(companion)       # ตัวคู่หู: ไม่เตือนท่านั่งเอง แค่ทำตามตัวหลัก
        self.menu = None
        self.status = "no_person"
        self.mode = "idle"
        self.dir = 1
        self.frame = 0
        self.timer = 0
        self.scale = 1.0
        self.target_scale = 1.0
        self.vx = self.vy = 0.0
        self.climb_target = 0.0
        self.next_nag = 0
        self.last_action = None
        self.species = "cat"
        self.head_h = SPECIES["cat"]["head"]
        self.bubble_text, self.bubble_kind, self.bubble_ticks = "", "info", 0
        self.drag_off = QPoint()
        self.drag_hist = []
        self.press_pos = QPoint()

        # ----- ความสามารถใหม่ -----
        self.wins = None                       # WindowTracker (ใช้ร่วมกันทุกตัว)
        self.roam = True                       # เดินบนขอบหน้าต่าง / ตามเมาส์
        self.partner = None                    # คู่เล่นไล่จับ
        self.perch = None                      # {"hwnd", "rect"} ถ้ากำลังยืนบนหน้าต่าง
        self.leap = None                       # ข้อมูลการกระโดดขึ้นหน้าต่าง
        self.no_land = (None, 0)               # (hwnd, เฟรม) กันตกลงมาแล้วติดหน้าต่างเดิมทันที
        self.pressing = self.moved = self.petting = False
        self.pet_t = 0
        self._perch_bak = None
        self.hearts = []                       # [[x, อายุ]]
        self.sleepy = False
        self.snack = None
        self.seek_t = 0
        self.hover_t = 0
        self.hover_cd = 0
        self.eat_kind = "cookie"
        self.eat_total = 1
        self.cursor_fn = QCursor.pos

        geo = self._geo_at(QPoint(0, 0), primary=True)
        self.px = float(geo.left() + 80 if self.companion else geo.right() - self.ww - 80)
        self.py = float(self._ground(geo))
        self.move(int(self.px), int(self.py))

        self.clock = QTimer(self)
        self.clock.timeout.connect(self.tick)
        self.clock.start(33)

    def set_species(self, key):
        self.species = key if key in SPECIES else "cat"
        self.head_h = SPECIES[self.species]["head"]

    def set_sprites(self, sprites):
        self.sprites = sprites or {}
        self.bw = BW
        if self.sprites:
            sp = next(iter(self.sprites.values()))
            self.bw = max(30, min(90, 110.0 * sp.width() / max(1, sp.height()) / 2))

    # ----- geometry helpers -----
    def _geo_at(self, c, primary=False):
        sc = None if primary else QGuiApplication.screenAt(c)
        sc = sc or QGuiApplication.primaryScreen()
        return sc.availableGeometry()

    def screen_geo(self):
        return self._geo_at(QPoint(int(self.px) + self.ww // 2, int(self.py) + self.wh // 2))

    def _ground(self, geo):
        return geo.bottom() + 1 - self.wh + FOOT_PAD

    def x_limits(self, geo):
        bw = self.bw * self.es()
        return geo.left() - (self.ww / 2 - bw), geo.right() + 1 - (self.ww / 2 + bw)

    def es(self):
        """สเกลที่ใช้วาดจริง = สเกลตามสถานะ (ค่อมตัวใหญ่) x ขนาดที่ผู้ใช้เลือก"""
        return self.scale * self.size_k

    def set_size(self, k):
        k = max(0.4, min(2.5, float(k)))
        if abs(k - self.size_k) < 1e-6:
            return
        cx, fy = self.body_cx(), self.feet_y()
        self.size_k = k
        self.ww, self.wh = int(WIN_W * max(1.0, k)), int(WIN_H * max(1.0, k))
        self.resize(self.ww, self.wh)
        self.px = cx - self.ww / 2.0
        self.py = self.py_for_feet(fy)
        self.move(int(self.px), int(self.py))
        self.update()

    def body_cx(self):
        return self.px + self.ww / 2.0

    def feet_y(self):
        return self.py + self.wh - FOOT_PAD

    def py_for_feet(self, fy):
        return fy - self.wh + FOOT_PAD

    def perch_limits(self, r):
        bw = self.bw * self.es()
        return r[0] - self.ww / 2.0 + bw * 0.5, r[2] - self.ww / 2.0 - bw * 0.5

    def hit_test(self, gx, gy):
        """จุดบนหน้าจอ (gx, gy) อยู่ที่ตัวมาสคอตไหม (ใช้ตอนวางขนม)"""
        bw = self.bw * self.es()
        fy = self.feet_y()
        return abs(gx - self.body_cx()) < bw + 24 and fy - 170 * self.es() < gy < fy + 24

    # ----- public API -----
    def say(self, text, kind="info", ticks=150):
        self.bubble_text, self.bubble_kind, self.bubble_ticks = tr(text), kind, ticks
        self.update()

    def clear_bubble(self):
        self.bubble_text, self.bubble_ticks = "", 0

    def burst_hearts(self, n=3):
        for _ in range(n):
            self.hearts.append([random.uniform(-30, 30), 0])

    def set_roam(self, flag):
        self.roam = bool(flag)
        if not self.roam:
            self.cancel_play(keep_partner=True)

    def set_status(self, status):
        if status == self.status:
            return
        prev, self.status = self.status, status
        self.target_scale = 1.4 if status == "bad" else 1.0
        if self.mode == "climb":
            self.start_fall(-self.dir * 2.0, -2.0)
        if status != "normal":
            self.cancel_play(keep_perch=(status == "no_person"))
        self.timer = 0
        self.clear_bubble()
        if self.companion:
            return
        if status == "bad":
            self.say(random.choice(BAD_MSGS), "bad", -1)
            self.next_nag = self.frame + 600
            self.nag.emit("bad")
        elif status == "warning":
            self.say(random.choice(WARN_MSGS), "warn", 120)
            self.next_nag = self.frame + 360
            self.nag.emit("warn")
        elif status == "normal" and prev in ("warning", "bad"):
            self.say(random.choice(GOOD_MSGS), "info", 120)

    def cancel_play(self, keep_partner=False, keep_perch=False):
        """เลิกสิ่งที่กำลังเล่นอยู่ (ไล่จับ/ตามเมาส์/ไปกินขนม/ขึ้นหน้าต่าง) เพื่อกลับมาเฝ้าท่านั่ง"""
        if self.mode == "leap":
            self.leap = None
            self.start_fall(0.0, -1.0)
        elif self.mode in ("follow", "flee", "chase", "seek", "eat"):
            self.mode = "idle"
        if self.perch and not keep_perch:
            self.drop_down(0.0)
        if not keep_partner:
            self.partner = None

    # ----- behaviour -----
    def start_fall(self, vx=0.0, vy=0.0):
        self.mode, self.vx, self.vy = "fall", vx, vy

    def drop_down(self, vx=None):
        """กระโดดลงจากหน้าต่าง"""
        if self.perch:
            self.no_land = (self.perch["hwnd"], self.frame + 25)
            self.perch = None
        if vx is None:
            vx = self.dir * 2.0
        self.start_fall(vx, -3.0)

    def choose_action(self, geo):
        s = self.status
        if s == "no_person":
            self.mode, self.timer = "sleep", 10 ** 9
            return
        if s == "bad":
            lo, hi = self.x_limits(geo)
            target = (lo + hi) / 2
            if abs(self.px - target) > 12:
                self.mode, self.dir, self.timer = "run", (1 if target > self.px else -1), 10 ** 9
            else:
                self.mode, self.vy = "jump", -14.0
            return
        if s == "warning":
            self.mode, self.timer = "run", random.randint(40, 120)
            if random.random() < 0.5:
                self.dir = -self.dir
            return
        if self.roam and s == "normal":
            q = random.random()
            if self.perch is None:
                if q < 0.13 and self.try_leap(geo):
                    return
                if q < 0.22 and self.try_follow(geo):
                    return
            else:
                if q < 0.10 and self.try_leap(geo):
                    return
                if q < 0.20:
                    self.say(random.choice(PERCH_DOWN), "info", 60)
                    self.drop_down()
                    return
        r = random.random()
        if r < 0.40:
            self.mode, self.timer = "walk", random.randint(90, 260)
            self.dir = random.choice((-1, 1))
        elif r < 0.55:
            self.mode, self.timer = "idle", random.randint(60, 180)
        else:
            self.start_action(None)

    def try_leap(self, geo):
        """เลือกหน้าต่างใกล้ๆ แล้วกระโดดขึ้นไปนั่งบนขอบบน คืน True ถ้าเริ่มกระโดด"""
        if self.wins is None or not self.wins.rects:
            return False
        fy, cx = self.feet_y(), self.body_cx()
        bw = self.bw * self.es()
        cur = self.perch["hwnd"] if self.perch else None
        cands = []
        for hwnd, l, t, r, b in self.wins.rects:
            if hwnd == cur or t < geo.top() + 170:
                continue
            if cur is None and t > fy - 60:
                continue                                   # ต่ำ/เสมอพื้นเกินไป ไม่ต้องกระโดด
            if cur is not None and abs(t - fy) > 320:
                continue
            if r < geo.left() + 80 or l > geo.right() - 80:
                continue
            tx = min(max(cx, l + 50), r - 50)
            tx = min(max(tx, geo.left() + bw + 10), geo.right() - bw - 10)
            if not (l + 30 < tx < r - 30):
                continue
            dist = math.hypot(tx - cx, t - fy)
            if dist <= 720:
                cands.append((dist, hwnd, tx, (l, t, r, b)))
        if not cands:
            return False
        cands.sort(key=lambda c: c[0])
        dist, hwnd, tx, rect = random.choice(cands[:3])
        was_perched = self.perch is not None
        self.perch = None
        self.no_land = (cur, self.frame + 25)
        self.leap = {"x0": self.px, "y0": self.py, "x1": tx - self.ww / 2.0,
                     "y1": self.py_for_feet(rect[1]), "t": 0, "n": int(24 + dist / 22.0),
                     "h": min(170.0, 60.0 + max(0.0, fy - rect[1]) * 0.12), "hwnd": hwnd, "rect": rect}
        self.mode, self.timer = "leap", 10 ** 9
        self.dir = 1 if tx >= cx else -1
        if not was_perched or random.random() < 0.3:
            self.nag.emit("meow")
        return True

    def try_follow(self, geo):
        cur = self.cursor_fn()
        d = (cur.x() - self.ww / 2.0) - self.px
        if abs(d) < 220 or not geo.contains(cur):
            return False
        self.mode, self.timer = "follow", random.randint(150, 300)
        self.dir = 1 if d > 0 else -1
        if random.random() < 0.5:
            self.say(random.choice(FOLLOW_MSGS), "info", 70)
        return True

    def start_action(self, name=None):
        """เริ่มท่าทางน่ารักๆ (สุ่มถ้าไม่ระบุชื่อ) คืน True ถ้าเริ่มได้"""
        if self.mode in ("drag", "fall", "climb", "jump", "leap"):
            return False
        names = [n for n in IDLE_ACTIONS if (not self.sprites or n in SPRITE_ACTIONS)
                 and ONLY.get(n, self.species) == self.species]
        if name not in names:
            pool = [n for n in names if n != self.last_action] or names
            weights = [IDLE_ACTIONS[n][2] for n in pool]
            if self.sleepy:                                # ดึกแล้ว: ง่วง หาว งีบบ่อยขึ้น
                weights = [w * (6 if n == "nap" else (3 if n == "stretch" else 1))
                           for w, n in zip(weights, pool)]
            name = random.choices(pool, weights=weights)[0]
        lo, hi, _w, msg = IDLE_ACTIONS[name]
        msg = ACTION_MSG.get((self.species, name), msg)
        self.last_action = name
        self.timer = random.randint(lo, hi)
        if name == "hop":
            self.mode, self.vy = "jump", -11.0
            if random.random() < 0.5:
                self.nag.emit("meow")
        else:
            self.mode = name
            if name in ("wave", "bark"):
                self.nag.emit("meow")             # "meow" = เสียงร้องของตัวละครปัจจุบัน
        if msg and not self.bubble_text and random.random() < 0.8:
            self.say(msg, "info", 90)
        return True

    def demo_action(self, name=None):
        if self.status != "normal":
            self.say("ตอนนี้ต้องตั้งใจเฝ้าท่านั่งก่อน\nไว้นั่งดีๆ แล้วค่อยเล่นนะ", "info", 100)
            return
        if self.mode in ("walk", "run", "idle", "sleep", "follow", "seek", "eat") or self.mode in ACTION_MODES:
            self.mode = "idle"                         # ตัดท่าเดิมทิ้ง เพื่อให้ท่าที่เลือกเริ่มทันที
        if not self.start_action(name):
            self.say("เดี๋ยวนะ ขอลงมาก่อน~", "info", 60)

    # ----- ขนม -----
    def snack_ready(self):
        sn = self.snack
        return sn is not None and sn.alive and sn.landed and self.status == "normal"

    def try_feed(self, sn):
        """ถูกวางขนมใส่ตัว (ลากมาปล่อย) คืน True ถ้ายอมกิน"""
        if self.status != "normal":
            self.say(SNACK_REFUSE, "warn", 100)
            return False
        self.start_eat(sn.kind)
        return True

    def start_eat(self, kind):
        self.eat_kind = kind
        self.snack = None
        self.leap = None
        self.mode = "idle"
        self.say(random.choice(EAT_MSGS), "info", 90)
        self.nag.emit("snack")
        self.burst_hearts(3)
        if not self.sprites and self.species == "bunny" and self.start_action("carrot"):
            return
        if not self.sprites and self.species == "raccoon" and self.start_action("cookie"):
            return
        self.mode = "eat"
        self.eat_total = self.timer = random.randint(150, 200)

    # ----- เฟรมต่อเฟรม -----
    def tick(self):
        self.frame += 1
        self.scale += (self.target_scale - self.scale) * 0.15
        geo = self.screen_geo()
        wins = self.wins if (self.roam and self.wins is not None) else None
        if wins is not None:
            wins.refresh()
        if self.hover_cd > 0:
            self.hover_cd -= 1
        if self.hearts:
            for h in self.hearts:
                h[1] += 1
            self.hearts = [h for h in self.hearts if h[1] < 50]
        m = self.mode

        # ----- ตามหน้าต่างที่ยืนอยู่ (หน้าต่างขยับ/หาย) -----
        if self.perch and m not in ("drag", "fall", "leap"):
            r = wins.rect_of(self.perch["hwnd"]) if wins is not None else None
            if r is None or r[1] < geo.top() + 150:
                self.no_land = (self.perch["hwnd"], self.frame + 25)
                self.perch = None
                if r is None and random.random() < 0.7:
                    self.say(random.choice(PERCH_GONE), "info", 80)
                self.start_fall(0.0, -2.0)
                m = self.mode
            else:
                self.px += r[0] - self.perch["rect"][0]
                self.perch["rect"] = r
                if m != "jump":
                    self.py = self.py_for_feet(r[1])

        ground = self._ground(geo)
        if self.perch:
            ground = self.py_for_feet(self.perch["rect"][1])
        lo, hi = self.x_limits(geo)
        if self.perch:
            plo, phi = self.perch_limits(self.perch["rect"])
            lo, hi = max(lo, plo), min(hi, phi)
            if lo > hi:
                lo = hi = (lo + hi) / 2.0

        if m == "drag":
            self.tick_drag()
        elif m == "leap":
            self.tick_leap(wins)
        elif m in ("fall", "jump"):
            prev_feet = self.feet_y()
            self.vy += 1.3
            self.py += self.vy
            self.px += self.vx
            self.vx *= 0.98
            if self.px < lo or self.px > hi:
                self.px = min(max(self.px, lo), hi)
                self.vx *= -0.5
            landed = self.land_target(prev_feet, self.feet_y()) if (wins is not None and self.vy > 0) else None
            cur_h = self.perch["hwnd"] if self.perch else None
            if landed is not None and landed[0] != cur_h and self.py < ground:
                self.perch = {"hwnd": landed[0], "rect": landed[1]}
                self.py = self.py_for_feet(landed[1][1])
                self.vx = self.vy = 0.0
                self.mode, self.timer = "idle", 15
                if random.random() < 0.5:
                    self.say(random.choice(PERCH_MSGS), "info", 80)
            elif self.py >= ground:
                self.py = ground
                if self.status == "bad" and m == "jump":
                    self.vy = -14.0               # เด้งต่อเนื่อง
                else:
                    self.vx = self.vy = 0.0
                    self.mode, self.timer = "idle", 15
        else:
            if m != "climb" and self.py < ground - 1:
                self.start_fall(self.vx, 0.0)
            elif m == "climb":
                self.py -= 1.5
                if self.py <= self.climb_target:
                    self.start_fall(-self.dir * 2.5, -3.0)
            else:
                if (self.frame % 10 == 0 and self.snack_ready()
                        and (m in ("idle", "walk", "follow", "sit") or m in ACTION_MODES)):
                    self.seek_t = 0
                    if self.perch:
                        self.drop_down()
                    else:
                        self.mode, self.timer = "seek", 10 ** 9
                        self.say(random.choice(SNACK_SEEK), "info", 70)
                    m = self.mode
                if m not in ("fall",):
                    self.timer -= 1
                if m == "spin" and self.frame % 6 == 0:
                    self.dir = -self.dir
                if m == "bark" and self.frame % 24 == 12:
                    self.nag.emit("meow")
                if m in ("walk", "run"):
                    sp = 1.8 if m == "walk" else (7.0 if self.status == "bad" else 5.0)
                    sp *= max(0.7, self.size_k)
                    self.px += self.dir * sp
                    if self.status == "bad" and m == "run":
                        target = (lo + hi) / 2
                        if abs(self.px - target) <= sp + 1:
                            self.mode, self.vy = "jump", -14.0
                    if self.px <= lo or self.px >= hi:
                        self.px = min(max(self.px, lo), hi)
                        if self.perch:
                            if self.status == "normal" and m == "walk" and random.random() < 0.4:
                                self.drop_down(self.dir * 2.5)          # เดินตกขอบหน้าต่าง
                            else:
                                self.dir = -self.dir
                        elif self.status == "normal" and m == "walk" and random.random() < 0.45:
                            self.mode = "climb"
                            self.dir = 1 if self.px >= hi else -1
                            self.climb_target = max(geo.top(), self.py - random.randint(120, 380))
                        else:
                            self.dir = -self.dir
                elif m == "follow":
                    self.tick_follow(lo, hi)
                elif m == "seek":
                    self.tick_seek(lo, hi)
                elif m in ("flee", "chase"):
                    self.tick_chase(m, lo, hi)
                if self.timer <= 0 and self.mode in ("idle", "walk", "run", "sleep", "follow", "eat") + ACTION_MODES:
                    self.choose_action(geo)
                if self.roam and m not in ("drag", "fall"):
                    self.check_hover()

        if self.mode == "sleep" and self.status != "no_person":
            self.timer = 0

        # ----- bubble / nag -----
        if self.bubble_ticks > 0:
            self.bubble_ticks -= 1
            if self.bubble_ticks == 0:
                self.bubble_text = ""
        if not self.companion:
            if self.status == "warning" and self.frame >= self.next_nag:
                self.say(random.choice(WARN_MSGS), "warn", 120)
                self.next_nag = self.frame + 360
                self.nag.emit("warn")
            elif self.status == "bad" and self.frame >= self.next_nag:
                self.say(random.choice(BAD_MSGS), "bad", -1)
                self.next_nag = self.frame + 600
                self.nag.emit("bad")

        self.move(int(self.px), int(self.py))
        self.update()

    def land_target(self, prev_feet, new_feet):
        """หน้าต่างที่เท้าตกลงมาถึงพอดีในเฟรมนี้ (เฉพาะตอนท่านั่งดี)"""
        if not (self.roam and self.wins is not None and self.status == "normal"):
            return None
        cx = self.body_cx()
        for hwnd, l, t, r, b in self.wins.rects:
            if hwnd == self.no_land[0] and self.frame < self.no_land[1]:
                continue
            if prev_feet <= t + 1 and new_feet >= t and l + 24 < cx < r - 24:
                return hwnd, (l, t, r, b)
        return None

    def tick_leap(self, wins):
        L = self.leap
        if L is None:
            self.mode, self.timer = "idle", 10
            return
        L["t"] += 1
        u = min(1.0, L["t"] / float(L["n"]))
        self.px = L["x0"] + (L["x1"] - L["x0"]) * u
        self.py = L["y0"] + (L["y1"] - L["y0"]) * u - 4.0 * L["h"] * u * (1.0 - u)
        if u >= 1.0:
            r = wins.rect_of(L["hwnd"]) if wins is not None else None
            self.leap = None
            if r is None:
                self.start_fall(0.0, 0.0)
                return
            self.px += r[0] - L["rect"][0]                   # หน้าต่างขยับระหว่างที่อยู่กลางอากาศ
            self.perch = {"hwnd": L["hwnd"], "rect": r}
            self.py = self.py_for_feet(r[1])
            self.vx = self.vy = 0.0
            self.mode, self.timer = "idle", 20
            if random.random() < 0.6:
                self.say(random.choice(PERCH_MSGS), "info", 80)

    def tick_follow(self, lo, hi):
        cur = self.cursor_fn()
        d = (cur.x() - self.ww / 2.0) - self.px
        if abs(d) < 60:
            self.mode, self.timer = "idle", random.randint(60, 120)
            self.dir = 1 if d >= 0 else -1
            if random.random() < 0.7:
                self.say(random.choice(FOLLOW_MSGS), "info", 80)
        else:
            self.dir = 1 if d > 0 else -1
            self.px = min(max(self.px + self.dir * 2.8, lo), hi)

    def tick_seek(self, lo, hi):
        sn = self.snack
        self.seek_t += 1
        if not self.snack_ready() or self.seek_t > 700:
            if self.seek_t > 700:
                self.snack = None
            self.mode, self.timer = "idle", 10
            return
        d = (sn.cx() - self.ww / 2.0) - self.px
        if abs(d) < 16:
            kind = sn.kind
            sn.consume()
            self.start_eat(kind)
        else:
            self.dir = 1 if d > 0 else -1
            self.px = min(max(self.px + self.dir * 3.6, lo), hi)

    def free_for_play(self):
        return (self.status == "normal" and self.perch is None and self.partner is None
                and (self.mode in ("idle", "walk") or self.mode in ACTION_MODES)
                and self.snack is None and not self.pressing)

    def tick_chase(self, m, lo, hi):
        o = self.partner
        if o is None or o.mode not in ("flee", "chase") or o.partner is not self or self.timer <= 0:
            self.mode, self.timer, self.partner = "idle", 30, None
            return
        d = o.px - self.px
        if m == "chase":
            self.dir = 1 if d > 0 else -1
            self.px = min(max(self.px + self.dir * 5.8, lo), hi)
            if abs(d) < 48:
                self.catch(o)
        else:
            away = -1 if d > 0 else 1
            if (away < 0 and self.px <= lo + 2) or (away > 0 and self.px >= hi - 2):
                away = -away                                  # จนมุม -> วิ่งสวนกลับ
            self.dir = away
            self.px = min(max(self.px + away * 5.0, lo), hi)

    def catch(self, o):
        for mm in (self, o):
            mm.partner = None
            mm.mode, mm.timer = "idle", 10
            mm.burst_hearts(3)
        self.say(random.choice(CHASE_CATCH), "info", 80)
        o.start_action("hop")
        self.start_action("hop")

    def check_hover(self):
        if self.status != "normal" or self.hover_cd > 0 or self.mode in NEW_MODES or self.pressing:
            self.hover_t = 0
            return
        cur = self.cursor_fn()
        bw = self.bw * self.es()
        fy = self.feet_y()
        if abs(cur.x() - self.body_cx()) < bw + 8 and fy - 130 * self.es() < cur.y() < fy + 6:
            self.hover_t += 1
        else:
            self.hover_t = 0
        if self.hover_t >= 75:
            self.hover_t, self.hover_cd = 0, 900
            self.say(random.choice(HOVER_MSGS), "info", 80)
            self.start_action("hop" if self.sprites else "wave")

    def tick_drag(self):
        if self.pressing and not self.moved and self.status == "normal":
            self.pet_t += 1
            if self.pet_t == 14:
                self.petting = True
                self.say(random.choice(PET_MSGS), "info", 70)
                self.nag.emit("pet")
            if self.petting:
                if self.frame % 5 == 0:
                    self.burst_hearts(1)
                if self.frame % 75 == 0:
                    self.nag.emit("meow")

    # ----- mouse -----
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.press_pos = e.globalPosition().toPoint()
            self.drag_off = self.press_pos - self.pos()
            self.drag_hist = []
            self._prev_mode = self.mode
            self._perch_bak = self.perch
            self.perch = None
            self.leap = None
            self.partner = None
            self.mode = "drag"
            self.pressing, self.moved, self.petting, self.pet_t = True, False, False, 0

    def mouseMoveEvent(self, e):
        if self.mode == "drag" and (e.buttons() & Qt.LeftButton):
            g = e.globalPosition().toPoint()
            if not self.moved:
                if (g - self.press_pos).manhattanLength() < 6:
                    return                                    # ยังเป็นการกดค้าง/ลูบหัวอยู่
                self.moved, self.petting, self.pet_t = True, False, 0
            np_ = g - self.drag_off
            self.px, self.py = float(np_.x()), float(np_.y())
            self.drag_hist.append(g.x())
            self.drag_hist = self.drag_hist[-5:]
            self.move(np_)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.mode == "drag":
            g = e.globalPosition().toPoint()
            was_pet, moved = self.petting, self.moved
            self.pressing = self.petting = False
            if not moved:
                if not was_pet and (g - self.press_pos).manhattanLength() < 5:
                    self.say(random.choice(POKE_MSGS), "info", 90)
                    self.nag.emit("poke")
                self.perch = self._perch_bak                  # ไม่ได้ลาก -> ยืนที่เดิมต่อ
                self.mode, self.timer = "idle", 20
                self.vx = self.vy = 0.0
                return
            vx = 0.0
            if len(self.drag_hist) >= 2:
                vx = (self.drag_hist[-1] - self.drag_hist[0]) / len(self.drag_hist) * 0.6
            self.start_fall(max(-12.0, min(12.0, vx)), 0.0)
            self.timer = 0

    def contextMenuEvent(self, e):
        if self.menu:
            self.menu.exec(e.globalPos())

    # ----- paint -----
    def draw_mode(self):
        m = self.mode
        if m == "drag":
            if self.moved:
                return "drag"
            return "sit" if self.petting else "idle"
        return {"follow": "walk", "seek": "run", "flee": "run", "chase": "run",
                "leap": "jump", "eat": "sit"}.get(m, m)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        if self.bubble_text:
            self.draw_bubble(p)
        p.save()
        p.translate(self.ww / 2, self.wh - FOOT_PAD)
        p.scale(self.es(), self.es())
        sprite = self.sprites.get(self.status) or self.sprites.get("normal")
        draw_mascot(p, self.status, self.frame, self.draw_mode(), self.dir, self.vy, sprite, self.species)
        if self.mode == "eat":
            left = max(0.12, self.timer / float(max(1, self.eat_total)))
            draw_snack(p, self.eat_kind, self.dir * 24, -50 + math.sin(self.frame * 0.9) * 2.0,
                       0.55 + 0.6 * left)
        p.restore()
        if self.hearts:
            p.save()
            p.translate(self.ww / 2, self.wh - FOOT_PAD)
            top = -(110 if self.sprites else self.head_h) * self.es()
            for x, age in self.hearts:
                col = QColor("#FF6B8B")
                col.setAlphaF(max(0.0, 1.0 - age / 50.0))
                _heart(p, x * self.es(), top - age * 1.3, 5.0 + (age % 3), col)
            p.restore()
        p.end()

    def draw_bubble(self, p):
        font = QFont()
        font.setFamilies(FONT_FAMILIES)
        font.setPointSize(10)
        font.setBold(self.bubble_kind != "info")
        p.setFont(font)
        fm = QFontMetrics(font)
        tr = fm.boundingRect(QRect(0, 0, 400, 400), Qt.AlignLeft, self.bubble_text)
        bw_, bh_ = tr.width() + 26, tr.height() + 18
        head_y = self.wh - FOOT_PAD - (110 if self.sprites else self.head_h) * self.es()
        top = head_y - 12 - bh_
        left = self.ww / 2 - bw_ / 2

        geo = self.screen_geo()
        gl, gr = self.px + left, self.px + left + bw_       # global bounds
        shift = max(0.0, geo.left() - gl) - max(0.0, gr - (geo.right() + 1))
        left += shift
        left = max(2.0, min(self.ww - bw_ - 2, left))

        kind = self.bubble_kind
        if kind == "bad":
            flash = (self.frame // 8) % 2 == 0
            bg, edge = QColor("#FFE3E0"), QColor("#E53935" if flash else "#FF8A80")
            fg = QColor("#B71C1C")
        elif kind == "warn":
            bg, edge, fg = QColor("#FFF6D6"), QColor("#E0A800"), QColor("#6B4E16")
        else:
            bg, edge, fg = QColor("#FFFFFF"), QColor("#9AA5B1"), QColor("#2E3A46")
        path = QPainterPath()
        path.addRoundedRect(QRectF(left, top, bw_, bh_), 12, 12)
        cx = self.ww / 2
        tail = QPolygonF([QPointF(cx - 8, top + bh_ - 1), QPointF(cx + 8, top + bh_ - 1),
                          QPointF(cx, top + bh_ + 11)])
        tp = QPainterPath()
        tp.addPolygon(tail)
        tp.closeSubpath()
        path = path.united(tp)
        p.setPen(QPen(edge, 2))
        p.setBrush(bg)
        p.drawPath(path)
        p.setPen(fg)
        p.drawText(QRectF(left, top, bw_, bh_), Qt.AlignCenter, self.bubble_text)


# ----------------------------------------------------------------------------
#  ขนมที่ลากไปวางให้มาสคอตกินได้ (หรือปล่อยให้ตกพื้น มาสคอตจะเดินมากินเอง)
# ----------------------------------------------------------------------------
class Snack(QWidget):
    SIZE = 48

    def __init__(self, ctrl, kind, x, y):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.NoDropShadowWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(self.SIZE, self.SIZE)
        self.ctrl, self.kind = ctrl, kind
        self.fx, self.fy = float(x), float(y)        # (ห้ามใช้ชื่อ x/y เพราะชนกับเมธอดของ QWidget)
        self.vy = 0.0
        self.alive = True
        self.landed = False
        self.dragging = False
        self.age = 0
        self.off = QPoint()
        self.move(int(self.fx), int(self.fy))
        self.clock = QTimer(self)
        self.clock.timeout.connect(self.tick)
        self.clock.start(33)

    def cx(self):
        return self.fx + self.SIZE / 2.0

    def ground(self):
        sc = QGuiApplication.screenAt(QPoint(int(self.cx()), int(self.fy + self.SIZE / 2))) \
            or QGuiApplication.primaryScreen()
        return sc.availableGeometry().bottom() + 1 - self.SIZE

    def tick(self):
        self.age += 1
        if not self.alive:
            return
        if self.age > 33 * 75:                        # ไม่มีใครกินใน 75 วินาที -> หายไป
            self.consume()
            return
        if self.dragging:
            return
        g = self.ground()
        if self.fy < g or self.vy < 0:
            self.vy += 1.1
            self.fy += self.vy
            if self.fy >= g:
                self.fy = g
                if self.vy > 4:
                    self.vy = -self.vy * 0.35         # เด้งนิดนึง
                else:
                    self.vy = 0.0
                    self.landed = True
        else:
            self.landed = True
        self.move(int(self.fx), int(self.fy))
        self.update()

    def consume(self):
        if not self.alive:
            return
        self.alive = False
        self.clock.stop()
        self.hide()
        self.ctrl.snack_gone(self)
        self.deleteLater()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.dragging, self.landed = True, False
            self.off = e.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, e):
        if self.dragging and (e.buttons() & Qt.LeftButton):
            g = e.globalPosition().toPoint() - self.off
            self.fx, self.fy = float(g.x()), float(g.y())
            self.move(g)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.dragging:
            self.dragging = False
            self.vy = 0.0
            self.ctrl.snack_released(self)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        draw_snack(p, self.kind, self.SIZE / 2.0, self.SIZE / 2.0 + 2, 1.15)
        p.end()


def load_sprites(enabled=True):
    out = {}
    if not enabled:
        return out
    d = BASE_DIR / "sprites"
    for name in ("normal", "warning", "bad", "no_person"):
        f = d / f"{name}.png"
        if f.exists():
            pm = QPixmap(str(f))
            if not pm.isNull():
                out[name] = pm
    return out


# ----------------------------------------------------------------------------
#  5) หน้าต่างพรีวิวกล้อง
# ----------------------------------------------------------------------------
class PreviewWindow(QWidget):
    closed = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Posture Shimeji - Camera")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        lay = QVBoxLayout(self)
        self.img = QLabel(tr("กำลังเปิดกล้อง..."))
        self.img.setMinimumSize(480, 360)
        self.img.setAlignment(Qt.AlignCenter)
        self.info = QLabel("")
        f = QFont()
        f.setFamilies(FONT_FAMILIES)
        self.info.setFont(f)
        lay.addWidget(self.img)
        lay.addWidget(self.info)

    def set_frame(self, qi):
        self.img.setPixmap(QPixmap.fromImage(qi).scaled(
            480, 360, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def closeEvent(self, e):
        e.ignore()
        self.hide()
        self.closed.emit()


# ----------------------------------------------------------------------------
#  เสียงประกอบ (สร้างเสียงเองด้วย numpy ไม่ต้องมีไฟล์เสียง) + สัญญาณเตือนวนต่อเนื่อง
# ----------------------------------------------------------------------------
SR = 22050
VOLUMES = {"low": 0.35, "mid": 0.65, "high": 1.0}


def _wav_bytes(x):
    import io
    import wave
    pcm = (np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    return bio.getvalue()


def _note(freq, dur, amp=0.5, harm=(1.0,), decay=6.0):
    t = np.arange(int(SR * dur)) / SR
    ph = 2 * np.pi * freq * t
    y = sum(h * np.sin((i + 1) * ph) for i, h in enumerate(harm)) / sum(harm)
    env = np.minimum(1.0, t / 0.004) * np.exp(-decay * t) * np.minimum(1.0, (dur - t) / 0.01)
    return amp * y * env


def _gap(d):
    return np.zeros(int(SR * d))


def make_sound(name, vol=1.0):
    if name == "meow":                              # เสียงแมวร้อง "เมี้ยว~" (ตอนจิ้มมาสคอต)
        dur = 0.40
        t = np.arange(int(SR * dur)) / SR
        f = 480 + 360 * np.sin(np.pi * t / dur) ** 1.2
        ph = 2 * np.pi * np.cumsum(f) / SR
        y = (np.sin(ph) + 0.5 * np.sin(2 * ph) + 0.25 * np.sin(3 * ph)) / 1.75
        return 0.6 * y * np.sin(np.pi * t / dur) ** 0.7
    if name == "squeak":                            # กระต่าย: ปิ๊บ ปิ๊บ
        parts = []
        for f0 in (1250, 1500):
            dur = 0.09
            t = np.arange(int(SR * dur)) / SR
            ph = 2 * np.pi * np.cumsum(f0 + 700 * t / dur) / SR
            parts += [0.55 * (np.sin(ph) + 0.3 * np.sin(2 * ph)) / 1.3 * np.sin(np.pi * t / dur) ** 0.6, _gap(0.04)]
        return np.concatenate(parts)
    if name == "chitter":                           # แรคคูน: จี๊ดๆๆ (เสียงนกหวีดเล็กๆ รัวขึ้น)
        parts = []
        for i, f0 in enumerate((1500, 1750, 1950)):
            dur = 0.07
            t = np.arange(int(SR * dur)) / SR
            ph = 2 * np.pi * np.cumsum(f0 + 900 * t / dur + 120 * np.sin(60 * t)) / SR
            parts += [0.5 * (np.sin(ph) + 0.35 * np.sin(2 * ph)) / 1.35 * np.sin(np.pi * t / dur) ** 0.6, _gap(0.03)]
        return np.concatenate(parts)
    if name == "woof":                              # ชิบะ: โฮ่ง!
        dur = 0.26
        t = np.arange(int(SR * dur)) / SR
        ph = 2 * np.pi * np.cumsum(150 + 430 * np.exp(-2.2 * t / dur)) / SR
        y = (np.sin(ph) + 0.7 * np.sin(2 * ph) + 0.5 * np.sin(3 * ph) + 0.3 * np.sin(5 * ph)) / 2.5
        env = np.minimum(1.0, t / 0.01) * np.exp(-7 * t) * np.minimum(1.0, (dur - t) / 0.02)
        noise = np.random.default_rng(1).normal(size=t.shape) * 0.15 * np.exp(-30 * t)
        return 0.7 * np.tanh(1.6 * (y * env + noise))
    if name == "good":                              # กลับมานั่งดีแล้ว ติ๊ง-ติ๊ง-ติ๊ง
        return np.concatenate([_note(1047, .10, .45, decay=8), _note(1319, .10, .45, decay=8),
                               _note(1568, .24, .45, decay=5)])
    if name == "warn":                              # เตือนเบาๆ 2 โน้ต
        return np.concatenate([_note(784, .14, .7, (1, .3), 7), _note(622, .26, .7, (1, .3), 5)])
    if name == "done":                              # ปรับท่านั่งเสร็จ
        return np.concatenate([_note(523, .09, .45, decay=7), _note(659, .09, .45, decay=7),
                               _note(784, .09, .45, decay=7), _note(1047, .28, .45, decay=4)])
    if name == "alarm":                             # ไซเรนดังๆ ลูปจนกว่าจะนั่งตรง
        parts = []
        for fq in (1000, 780, 1000, 780, 1000, 780):
            parts += [_note(fq, 0.17, 0.9, (1, .6, .4, .25, .15), 0.0), _gap(0.04)]
        parts.append(_gap(0.30))
        y = np.concatenate(parts)
        y = np.tanh(1.8 * y) / np.tanh(1.8)
        return y * vol
    return _gap(0.1)


class SoundFx:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cache = {}
        self.alarm_on = False
        self._keep = None
        self._beep = QTimer()                       # ใช้เฉพาะระบบที่ไม่ใช่ Windows
        self._beep.timeout.connect(QApplication.beep)

    def enabled(self):
        return bool(self.cfg["sound"])

    def _path(self, name):
        """สร้างไฟล์ WAV ครั้งแรกที่ต้องใช้ แล้วคืน path (winsound เล่น async จากไฟล์ได้เท่านั้น)"""
        vol = VOLUMES.get(self.cfg["volume"], 1.0) if name == "alarm" else 1.0
        key = (name, vol)
        if key not in self.cache:
            d = APP_DIR / "sounds"
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"{name}_{int(vol * 100)}.wav"
            f.write_bytes(_wav_bytes(make_sound(name, vol)))
            self.cache[key] = str(f)
        return self.cache[key]

    @staticmethod
    def _fallback_beep():
        def _run():
            try:
                import winsound
                winsound.Beep(880, 160)
                winsound.Beep(660, 220)
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def play(self, name):
        if not self.enabled() or self.alarm_on or sys.platform != "win32":
            return
        try:
            import winsound
            self._keep = self._path(name)
            winsound.PlaySound(self._keep, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as e:
            log(f"sound error: {e}")
            self._fallback_beep()

    def start_alarm(self, force=False):
        if (not self.enabled()) or (self.alarm_on and not force):
            return
        self.alarm_on = True
        try:
            if sys.platform == "win32":
                import winsound
                self._keep = self._path("alarm")
                winsound.PlaySound(self._keep, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
            else:
                self._beep.start(1200)
        except Exception as e:
            log(f"alarm error: {e}")
            self._beep.start(1200)                  # สำรอง: บี๊บซ้ำๆ

    def stop_alarm(self):
        if not self.alarm_on:
            return
        self.alarm_on = False
        try:
            if sys.platform == "win32":
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            self._beep.stop()
        except Exception:
            pass


# ----------------------------------------------------------------------------
#  5.5) หน้าต่างควบคุมหลัก (หน้าตั้งค่าแบบปุ่มใหญ่ ใช้ง่าย)
# ----------------------------------------------------------------------------
STATUS_UI = {
    "normal":    ("😊", "นั่งท่าดีมาก",       "รักษาท่านี้ไว้นะ",                    "#2e9e5b", "#e6f5ec"),
    "warning":   ("😟", "เริ่มค่อมแล้วนะ",     "ลองยืดหลัง ยกหัวขึ้นนิดนึง",           "#d98a00", "#fff3dc"),
    "bad":       ("😣", "หลังค่อมแล้ว!",       "ยืดหลัง ดึงคางเข้า ผ่อนคลายไหล่",      "#d64545", "#fde8e8"),
    "no_person": ("😴", "ไม่พบคนหน้ากล้อง",    "นั่งให้เห็นใบหน้าและไหล่ทั้งสองข้าง",  "#7b8190", "#eceef2"),
    "paused":    ("⏸️", "พักการตรวจจับอยู่",    "กด \"เริ่มตรวจจับ\" เพื่อเปิดกล้องอีกครั้ง", "#5b6bd6", "#e9ecfb"),
}

def status_ui(key):
    emoji, title, sub, color, bg = STATUS_UI.get(key, STATUS_UI["no_person"])
    if theme()["dark"]:
        bg = blend(color, theme()["card"], 0.16)
    return emoji, tr(title), tr(sub), color, bg


def _bind(action, button):
    """ผูก QAction (ตัวเก็บสถานะจริง) กับปุ่ม/เช็กบ็อกซ์ ให้ซิงก์กันสองทาง"""
    button.setChecked(action.isChecked())
    button.toggled.connect(action.setChecked)

    def back(v):
        try:
            button.blockSignals(True)
            button.setChecked(v)
            button.blockSignals(False)
        except RuntimeError:                       # ปุ่มถูกลบไปแล้ว (หน้าต่างถูกสร้างใหม่ตอนเปลี่ยนภาษา/ธีม)
            pass
    action.toggled.connect(back)


class NoWheelCombo(QComboBox):
    """ไม่เปลี่ยนค่าเวลาเลื่อนลูกกลิ้งเมาส์ผ่าน (ให้หน้าต่างเลื่อนแทน) ต้องคลิกเลือกเท่านั้น"""

    def __init__(self):
        super().__init__()
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, e):
        e.ignore()


class NoWheelSlider(QSlider):
    def wheelEvent(self, e):
        e.ignore()


class ControlPanel(QWidget):
    WIDTH = 440

    def __init__(self, ctrl):
        super().__init__()
        self.ctrl = ctrl
        self._status = "no_person"
        self._paused = False
        self.setWindowTitle("Posture Shimeji")
        self.setWindowFlags(Qt.Window | Qt.WindowTitleHint | Qt.WindowSystemMenuHint
                            | Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint
                            | Qt.WindowCloseButtonHint)
        self.setMinimumWidth(self.WIDTH)
        f = QFont()
        f.setFamilies(FONT_FAMILIES)
        f.setPointSize(10)
        self.setFont(f)
        self.setStyleSheet(make_qss())

        # หน้าต่างเดียว: ซ้าย = ตัวควบคุม (กว้างคงที่ ปุ่มไม่ยืด) / ขวา = สถิติ (โผล่เมื่อขยายหน้าต่างกว้าง)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(self.WIDTH)
        self.inner = QWidget()
        scroll.setWidget(self.inner)
        outer.addWidget(scroll)
        self.sep = QFrame()
        self.sep.setFixedWidth(1)
        self.sep.setStyleSheet("background: #d8dce6;")
        outer.addWidget(self.sep)
        self.stats_view = StatsWindow(ctrl)
        outer.addWidget(self.stats_view, 1)
        self.sep.hide()
        self.stats_view.hide()
        self._wide = False
        root = QVBoxLayout(self.inner)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # ---- การ์ดสถานะ ----
        self.card = QFrame()
        self.card.setObjectName("card")
        cl = QVBoxLayout(self.card)
        cl.setContentsMargins(18, 16, 18, 16)
        cl.setSpacing(4)
        self.face = QLabel("😴")
        self.face.setAlignment(Qt.AlignCenter)
        self.face.setStyleSheet("font-size: 44pt;")
        self.title = QLabel("")
        self.title.setAlignment(Qt.AlignCenter)
        self.sub = QLabel("")
        self.sub.setAlignment(Qt.AlignCenter)
        self.sub.setObjectName("hint")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar_lbl = QLabel("ระดับความหลังค่อม")
        self.bar_lbl.setObjectName("hint")
        self.bar_lbl.setAlignment(Qt.AlignCenter)
        cl.addWidget(self.face)
        cl.addWidget(self.title)
        cl.addWidget(self.sub)
        cl.addSpacing(6)
        cl.addWidget(self.bar)
        cl.addWidget(self.bar_lbl)
        root.addWidget(self.card)

        # ---- แถบแจ้งเตือนครั้งแรก (ยังไม่เคยปรับท่านั่ง) ----
        self.first = QLabel("👉 ยังไม่ได้ตั้งท่านั่งมาตรฐาน กดปุ่ม \"ปรับท่านั่งมาตรฐาน\" ด้านล่าง\n"
                            "แล้วนั่งหลังตรงๆ มองกล้อง 5 วินาที")
        self.first.setWordWrap(True)
        self.first.setStyleSheet("background:#fff3dc; border-radius:10px; padding:10px; color:#7a5300;")
        root.addWidget(self.first)

        # ---- ปุ่มหลัก ----
        self.btn_main = QPushButton("⏸  หยุดตรวจจับชั่วคราว")
        self.btn_main.setObjectName("primary")
        self.btn_main.clicked.connect(lambda: ctrl.act_pause.setChecked(not ctrl.act_pause.isChecked()))
        root.addWidget(self.btn_main)

        row = QHBoxLayout()
        b1 = QPushButton("🎯  ปรับท่านั่งมาตรฐาน")
        b1.clicked.connect(ctrl.start_calibration)
        self.btn_cam = QPushButton("📷  ดูภาพกล้อง")
        self.btn_cam.setCheckable(True)
        _bind(ctrl.act_preview, self.btn_cam)
        row.addWidget(b1)
        row.addWidget(self.btn_cam)
        root.addLayout(row)
        self.b_stats = QPushButton("📊  ดูสถิติ")
        self.b_stats.clicked.connect(self.toggle_stats)
        root.addWidget(self.b_stats)
        self.b_web = QPushButton(tr("🌐  ส่งสถิติขึ้นเว็บ…"))
        self.b_web.clicked.connect(ctrl.open_web_sync)
        root.addWidget(self.b_web)

        # ---- ตั้งค่า ----
        box = self._section("ตั้งค่า")
        bl = box.layout()
        lbl = QLabel("ความไวในการเตือน")
        bl.addWidget(lbl)
        seg = QHBoxLayout()
        seg.setSpacing(6)
        self.sens_group = QButtonGroup(self)
        self.sens_btns = {}
        for key, name in (("low", "ผ่อนปรน"), ("normal", "ปกติ"), ("high", "เข้มงวด")):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setProperty("seg", True)
            self.sens_group.addButton(b)
            b.clicked.connect(lambda _=False, k=key: ctrl.cfg.__setitem__("sensitivity", k))
            seg.addWidget(b)
            self.sens_btns[key] = b
        self.sens_btns.get(ctrl.cfg["sensitivity"], self.sens_btns["normal"]).setChecked(True)
        bl.addLayout(seg)

        self.cb_sound = QCheckBox("เสียงเตือนเมื่อหลังค่อม")
        _bind(ctrl.act_sound, self.cb_sound)
        vr = QHBoxLayout()
        vr.setSpacing(6)
        vr.addWidget(QLabel("ความดังเสียงเตือน"))
        self.vol_group = QButtonGroup(self)
        self.vol_btns = {}
        for key, name in (("low", "เบา"), ("mid", "กลาง"), ("high", "ดัง")):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setProperty("seg", True)
            self.vol_group.addButton(b)
            b.clicked.connect(lambda _=False, k=key: ctrl.set_volume(k))
            vr.addWidget(b)
            self.vol_btns[key] = b
        self.vol_btns.get(ctrl.cfg["volume"], self.vol_btns["high"]).setChecked(True)
        bl.addLayout(vr)
        test = QPushButton("🔊  ลองฟังเสียงเตือน")
        test.clicked.connect(ctrl.test_alarm)
        bl.addWidget(test)
        vhint = QLabel("ตอนหลังค่อมชัดเจน เสียงจะดังต่อเนื่องจนกว่าจะนั่งตรง "
                       "(คลิกที่มาสคอตเพื่อพักเสียง 1 นาที)")
        vhint.setObjectName("hint")
        vhint.setWordWrap(True)
        bl.addWidget(vhint)
        self.cb_auto = QCheckBox("เปิดโปรแกรมอัตโนมัติเมื่อเปิดเครื่อง")
        _bind(ctrl.act_auto, self.cb_auto)
        bl.addWidget(self.cb_sound)
        bl.addWidget(self.cb_auto)

        cr = QHBoxLayout()
        cr.addWidget(QLabel("กล้องที่ใช้"))
        self.combo = NoWheelCombo()
        self.fill_cameras()
        self.combo.currentIndexChanged.connect(
            lambda i: i >= 0 and ctrl.set_camera(self.combo.itemData(i)))
        cr.addWidget(self.combo, 1)
        rb = QPushButton("🔄")
        rb.setToolTip(tr("ค้นหากล้องใหม่"))
        rb.setFixedWidth(44)
        rb.clicked.connect(self.fill_cameras)
        cr.addWidget(rb)
        bl.addLayout(cr)
        hint = QLabel("ไม่เห็นภาพ? ลองเลือกกล้องตัวอื่น หรือปิดโปรแกรมที่ใช้กล้องอยู่ (Zoom/Teams)")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        bl.addWidget(hint)

        for title, items, cur, fn, store in (
                ("ธีมสี", [(k, THEMES[k]["name"]) for k in THEMES], THEME_KEY, ctrl.set_theme, "theme_btns"),
                ("ภาษา / Language", [("th", "ไทย"), ("en", "English")], LANG, ctrl.set_lang, "lang_btns")):
            hl = QHBoxLayout()
            hl.setSpacing(6)
            hl.addWidget(QLabel(title))
            grp = QButtonGroup(self)
            btns = {}
            for key, name in items:
                b = QPushButton(name)
                b.setCheckable(True)
                b.setProperty("seg", True)
                grp.addButton(b)
                b.clicked.connect(lambda _=False, k=key, f=fn: f(k))
                hl.addWidget(b)
                btns[key] = b
            btns[cur].setChecked(True)
            setattr(self, store, btns)
            setattr(self, store + "_grp", grp)
            bl.addLayout(hl)
        for text, key in (("ทักทายตามเวลา (เช้า/เที่ยง/เย็น/ดึก)", "time_greet"),
                          ("ให้มาสคอตกระโดดขึ้นนั่งบนขอบหน้าต่าง และตามเมาส์ (Windows)", "roam")):
            cb = QCheckBox()
            cb.setChecked(bool(ctrl.cfg[key]))
            cb.toggled.connect(lambda v, k=key: ctrl.set_opt(k, v))
            lab = QLabel(text)
            lab.setWordWrap(True)
            lab.mousePressEvent = lambda e, c=cb: c.toggle()
            wl = QHBoxLayout()
            wl.setSpacing(8)
            wl.addWidget(cb, 0, Qt.AlignTop)
            wl.addWidget(lab, 1)
            bl.addLayout(wl)
        root.addWidget(box)

        # ---- มาสคอต ----
        box2 = self._section("มาสคอต")
        b2 = box2.layout()
        grid = QGridLayout()
        grid.setSpacing(6)
        self.spr_group = QButtonGroup(self)
        self.char_btns = {}
        for i, key in enumerate(SPECIES):
            b = QPushButton("  " + SPECIES[key]["name"])
            b.setCheckable(True)
            b.setProperty("seg", True)
            b.setIcon(QIcon(make_icon("normal", 64, key)))
            b.setIconSize(QSize(46, 46))
            b.setMinimumHeight(56)
            self.spr_group.addButton(b)
            b.clicked.connect(lambda _=False, k=key: ctrl.set_character(k))
            grid.addWidget(b, i // 2, i % 2)
            self.char_btns[key] = b
        self.spr_custom = QPushButton("🖼  รูปของฉัน")
        self.spr_custom.setCheckable(True)
        self.spr_custom.setProperty("seg", True)
        self.spr_custom.setMinimumHeight(56)
        self.spr_group.addButton(self.spr_custom)
        self.spr_custom.clicked.connect(lambda _=False: ctrl.set_custom(True))
        n = len(SPECIES)
        grid.addWidget(self.spr_custom, n // 2, n % 2)
        b2.addLayout(grid)
        pick = QPushButton("📂  เลือกไฟล์รูปใหม่ (PNG)…")
        pick.clicked.connect(ctrl.pick_sprite)
        b2.addWidget(pick)
        snack = QPushButton("🍪  ให้ขนมมาสคอต")
        snack.clicked.connect(ctrl.give_snack)
        b2.addWidget(snack)
        sh = QLabel("ลากขนมไปวางให้กิน หรือปล่อยให้ตกพื้นแล้วมาสคอตจะเดินมากินเอง (ต้องนั่งท่าดีก่อนถึงจะกิน) / กดค้างที่ตัวมาสคอตเพื่อลูบหัว")
        sh.setObjectName("hint")
        sh.setWordWrap(True)
        b2.addWidget(sh)
        sr = QHBoxLayout()
        sr.addWidget(QLabel("ขนาดตัวละคร"))
        self.size_lbl = QLabel("")
        self.size_lbl.setMinimumWidth(48)
        self.size_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        sr.addStretch(1)
        sr.addWidget(self.size_lbl)
        b2.addLayout(sr)
        self.size_sl = NoWheelSlider(Qt.Horizontal)
        self.size_sl.setRange(40, 200)
        self.size_sl.setSingleStep(5)
        self.size_sl.setPageStep(10)
        self.size_sl.setValue(int(round(float(ctrl.cfg["size"]) * 100)))
        self.size_lbl.setText(f"{self.size_sl.value()}%")

        def _size_changed(v):
            self.size_lbl.setText(f"{v}%")
            ctrl.set_size(v / 100.0, save=not self.size_sl.isSliderDown())
        self.size_sl.valueChanged.connect(_size_changed)
        self.size_sl.sliderReleased.connect(lambda: ctrl.set_size(self.size_sl.value() / 100.0))
        b2.addWidget(self.size_sl)
        rs = QPushButton("↺  กลับเป็นขนาดเดิม (100%)")
        rs.clicked.connect(lambda: self.size_sl.setValue(100))
        b2.addWidget(rs)
        cr2 = QHBoxLayout()
        cr2.addWidget(QLabel("ตัวละครคู่หู (อยู่ด้วยกัน 2 ตัว)"))
        self.comp_combo = NoWheelCombo()
        self.comp_combo.addItem(tr("ไม่มี"), "none")
        for k in SPECIES:
            self.comp_combo.addItem(tr(SPECIES[k]["name"]), k)
        self.comp_combo.setCurrentIndex(max(0, self.comp_combo.findData(ctrl.cfg["companion"])))
        self.comp_combo.currentIndexChanged.connect(lambda i: ctrl.set_companion(self.comp_combo.itemData(i)))
        cr2.addWidget(self.comp_combo, 1)
        b2.addLayout(cr2)
        act = QPushButton("🎬  ดูท่าทางน่ารักๆ (สุ่ม)")
        act.clicked.connect(lambda: ctrl.mascot.demo_action())
        b2.addWidget(act)
        pr = QHBoxLayout()
        pr.setSpacing(6)
        self.act_combo = NoWheelCombo()
        pr.addWidget(self.act_combo, 1)
        pbtn = QPushButton("▶ เล่นท่านี้")
        pbtn.clicked.connect(lambda: ctrl.mascot.demo_action(self.act_combo.currentData()))
        pr.addWidget(pbtn)
        b2.addLayout(pr)
        self.refresh_actions()
        b2.addWidget(QLabel("ลองดูท่าทางมาสคอต (ไม่ต้องนั่งค่อมจริง)"))
        dr = QHBoxLayout()
        dr.setSpacing(6)
        for s, name in (("normal", "😊 ปกติ"), ("warning", "😟 เตือน"), ("bad", "😣 ค่อม")):
            b = QPushButton(name)
            b.clicked.connect(lambda _=False, k=s: ctrl.demo(k))
            dr.addWidget(b)
        stop = QPushButton("หยุดดู")
        stop.clicked.connect(lambda: ctrl.demo(None))
        dr.addWidget(stop)
        b2.addLayout(dr)
        root.addWidget(box2)

        foot = QLabel("ปิดหน้าต่างนี้แล้วโปรแกรมยังทำงานต่อที่ไอคอนข้างนาฬิกา (system tray)\n"
                      "คลิกไอคอนเพื่อเปิดหน้านี้อีกครั้ง")
        foot.setObjectName("hint")
        foot.setAlignment(Qt.AlignCenter)
        root.addWidget(foot)
        quit_b = QPushButton("ออกจากโปรแกรม")
        quit_b.clicked.connect(ctrl.quit)
        root.addWidget(quit_b)
        root.addStretch(1)

        self.refresh()
        self.refresh_sprites()
        self.retranslate()

    def fill_cameras(self):
        """ใส่ชื่อกล้องจริงที่มีในเครื่องลงช่องเลือก (อ่านไม่ได้ก็ใช้ 'กล้องตัวที่ N')"""
        names = list_cameras() or [tr("กล้องตัวที่ ") + str(i + 1) for i in range(4)]
        cur = int(self.ctrl.cfg["camera"])
        self.combo.blockSignals(True)
        self.combo.clear()
        for i, n in enumerate(names):
            self.combo.addItem(n, i)
        self.combo.setCurrentIndex(max(0, min(len(names) - 1, cur)))
        self.combo.setToolTip(self.combo.currentText())
        self.combo.blockSignals(False)

    def retranslate(self):
        """แปลข้อความที่เขียนเป็นภาษาไทยตอนสร้างปุ่ม/ป้าย (เมื่อเลือกภาษาอังกฤษ)"""
        if LANG != "en":
            return
        for w in self.findChildren(QWidget):
            if isinstance(w, (QLabel, QPushButton, QCheckBox)):
                t = w.text()
                core = t.strip()
                if core and core in EN:
                    w.setText(t.replace(core, EN[core]))

    def _section(self, title):
        fr = QFrame()
        fr.setObjectName("card")
        lay = QVBoxLayout(fr)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(8)
        h = QLabel(title)
        h.setObjectName("h")
        lay.addWidget(h)
        return fr

    # ---- สถิติในหน้าเดียวกัน ----
    WIDE_MIN = 780                                   # กว้างเท่านี้ขึ้นไป = แสดงสถิติด้านขวา

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if not hasattr(self, "_wide"):
            return
        wide = self.width() >= self.WIDE_MIN
        if wide != self._wide:
            self._wide = wide
            self.stats_view.setVisible(wide)
            self.sep.setVisible(wide)
            if wide:
                self.stats_view.refresh()
            self.b_stats.setText(tr("◂  ซ่อนสถิติ") if wide else tr("📊  ดูสถิติ"))

    def open_stats(self, toggle=False):
        """ขยายหน้าต่างให้เห็นสถิติ (toggle=True: ถ้าเปิดอยู่แล้วให้หุบกลับ)"""
        if self._wide:
            if toggle:
                self.showNormal()
                self.resize(self.WIDTH, self.height())
            return
        scr = self.screen() or QGuiApplication.primaryScreen()
        geo = scr.availableGeometry() if scr else QRect(0, 0, 1366, 768)
        w = max(self.WIDE_MIN, min(geo.width() - 40, self.WIDTH + 760))
        h = min(geo.height() - 70, max(self.height(), 800))
        self.resize(w, h)
        x = max(geo.left() + 10, min(self.x(), geo.right() - w - 10))
        y = max(geo.top() + 10, min(self.y(), geo.bottom() - h - 10))
        self.move(x, y)

    def toggle_stats(self):
        self.open_stats(toggle=True)

    # ---- ถูกเรียกจาก Controller ----
    def fit_to_screen(self):
        """ขนาดพอดีเนื้อหา แต่ไม่สูงเกินจอ (เกินแล้วเลื่อนลงได้)"""
        scr = self.screen() or QGuiApplication.primaryScreen()
        avail = scr.availableGeometry().height() if scr else 800
        want = self.inner.sizeHint().height() + 6
        if self.isMaximized():                       # เปิดใหม่ให้กลับมาขนาดปกติเสมอ
            self.showNormal()
        self.resize(self.WIDTH, max(360, min(want, avail - 70)))

    def refresh_actions(self):
        """รายการท่าที่เลือกเล่นได้ ขึ้นกับตัวละครที่ใช้อยู่ (ท่าเฉพาะตัวโผล่เฉพาะตัวนั้น)"""
        if not hasattr(self, "act_combo"):
            return
        sp = self.ctrl.cfg["character"]
        custom = bool(self.ctrl.cfg["use_custom"]) and self.ctrl.has_custom_files()
        self.act_combo.clear()
        for n in IDLE_ACTIONS:
            if ONLY.get(n, sp) == sp and (not custom or n in SPRITE_ACTIONS):
                self.act_combo.addItem(("★ " if n in ONLY else "") + tr(ACTION_TH.get(n, n)), n)

    def refresh_sprites(self):
        self.refresh_actions()
        has = self.ctrl.has_custom_files()
        self.spr_custom.setEnabled(has)
        use = has and bool(self.ctrl.cfg["use_custom"])
        key = self.ctrl.cfg["character"]
        (self.spr_custom if use else self.char_btns.get(key, self.char_btns["cat"])).setChecked(True)

    def set_status(self, status):
        self._status = status
        self.refresh()

    def set_paused(self, v):
        self._paused = bool(v)
        self.refresh()

    def set_score(self, score):
        self.bar.setValue(int(round(max(0.0, min(1.0, score)) * 100)))

    def refresh(self):
        key = "paused" if self._paused else self._status
        emoji, title, sub, color, bg = status_ui(key)
        self.face.setText(emoji)
        self.title.setText(title)
        self.title.setStyleSheet(f"font-size: 17pt; font-weight: bold; color: {color};")
        self.sub.setText(sub)
        self.card.setStyleSheet(f"QFrame#card {{ background: {bg}; border: 1px solid {color}; "
                                f"border-radius: 14px; }}")
        self.bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; }}")
        if self._paused:
            self.bar.setValue(0)
        self.btn_main.setText(tr("▶  เริ่มตรวจจับ") if self._paused else tr("⏸  หยุดตรวจจับชั่วคราว"))
        self.btn_main.setObjectName("primary")
        self.first.setVisible(self.ctrl.baseline is None)

    def closeEvent(self, e):
        e.ignore()
        self.hide()
        self.ctrl.on_panel_closed()


# ----------------------------------------------------------------------------
#  หน้าต่างสถิติท่านั่ง
# ----------------------------------------------------------------------------
STAT_COLORS = ("#2e9e5b", "#e0a800", "#d64545")
WD_TH = ("จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส.", "อา.")
WD_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
CAUSE_INFO = (   # (ชื่อ, คำแนะนำ)
    ("หัวก้มต่ำกว่าไหล่", "ยกจอให้สูงขึ้น ให้ขอบบนจออยู่ระดับสายตา"),
    ("คอยื่นไปข้างหน้า", "ดึงคางเข้า แล้วให้หลังชิดพนักเก้าอี้"),
    ("โน้มตัวเข้าหาจอ", "ขยับจอให้ไกลขึ้นหรือขยายตัวอักษร จะได้ไม่ต้องโน้มตัว"),
    ("ไหล่เอียง", "เช็กความสูงของที่วางแขนและตำแหน่งเมาส์"),
)


def nice_max(v):
    for c in (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 24, 30, 40, 45, 60):
        if v <= c:
            return float(c)
    return float(int(v / 10 + 1) * 10)


class StackChart(QWidget):
    """กราฟแท่งซ้อน (ดี/เตือน/ค่อม) bars = [(ป้ายใต้แท่ง, (ดี, เตือน, ค่อม วินาที), tooltip)]"""

    def __init__(self):
        super().__init__()
        self.bars = []
        self.unit = "min"
        self.setMinimumHeight(200)
        self.setMouseTracking(True)

    def set_bars(self, bars, unit):
        self.bars, self.unit = bars, unit
        self.update()

    def _plot(self):
        return 34, 10, max(10, self.width() - 44), max(10, self.height() - 38)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        th = theme()
        left, top, w, h = self._plot()
        f = QFont()
        f.setFamilies(FONT_FAMILIES)
        f.setPointSize(8)
        p.setFont(f)
        div = 60.0 if self.unit == "min" else 3600.0
        mx = nice_max(max([sum(b[1]) / div for b in self.bars] + [0.5]))
        grid, txt = QColor(th["border"]), QColor(th["hint"])
        for i in range(5):
            y = top + h - h * i / 4.0
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(left, y), QPointF(left + w, y))
            p.setPen(txt)
            p.drawText(QRectF(0, y - 8, left - 4, 16), Qt.AlignRight | Qt.AlignVCenter, f"{mx * i / 4:g}")
        if not self.bars:
            p.setPen(txt)
            p.drawText(QRectF(left, top, w, h), Qt.AlignCenter, tr("ยังไม่มีข้อมูล"))
            p.end()
            return
        n = len(self.bars)
        slot = w / float(n)
        bw = max(3.0, min(34.0, slot * 0.68))
        step = 1 if n <= 12 else (2 if n <= 24 else 3)
        for i, (label, secs, _tip) in enumerate(self.bars):
            x = left + slot * i + (slot - bw) / 2.0
            y = top + h
            for k in range(3):
                hh = h * (secs[k] / div) / mx
                if hh <= 0.2:
                    continue
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(STAT_COLORS[k]))
                p.drawRect(QRectF(x, y - hh, bw, hh))
                y -= hh
            if i % step == 0:
                p.setPen(txt)
                p.drawText(QRectF(left + slot * i - 6, top + h + 4, slot + 12, 16), Qt.AlignCenter, label)
        p.end()

    def mouseMoveEvent(self, e):
        if not self.bars:
            return
        left, _top, w, _h = self._plot()
        i = int((e.position().x() - left) / (w / float(len(self.bars))))
        self.setToolTip(self.bars[i][2] if 0 <= i < len(self.bars) else "")


def lerp_color(a, b, t):
    a, b, t = QColor(a), QColor(b), max(0.0, min(1.0, t))
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t))


def score_color(v):
    """สัดส่วนท่าดี 0..1 -> แดง (<=0.3) / เหลือง (0.6) / เขียว (>=0.9)"""
    red, amb, grn = QColor(STAT_COLORS[2]), QColor(STAT_COLORS[1]), QColor(STAT_COLORS[0])
    if v <= 0.3:
        return red
    if v < 0.6:
        return lerp_color(red, amb, (v - 0.3) / 0.3)
    if v < 0.9:
        return lerp_color(amb, grn, (v - 0.6) / 0.3)
    return grn


def fmt_short(sec):
    """วินาที -> '45 วิ' ถ้าไม่ถึง 90 วินาที ไม่งั้นเป็น ชม./น."""
    if sec < 90:
        return tr("{s} วิ").format(s=int(round(sec)))
    return fmt_dur(sec)


def _chart_font(p):
    f = QFont()
    f.setFamilies(FONT_FAMILIES)
    f.setPointSize(8)
    p.setFont(f)


class KpiTile(QFrame):
    """การ์ดตัวเลขเล็กๆ: หัวข้อ / ค่าใหญ่ / คำอธิบายใต้ค่า"""

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(1)
        self.cap = QLabel("")
        self.cap.setObjectName("hint")
        self.cap.setWordWrap(True)
        self.val = QLabel("—")
        self.sub = QLabel("")
        self.sub.setObjectName("hint")
        self.sub.setWordWrap(True)
        lay.addWidget(self.cap)
        lay.addWidget(self.val)
        lay.addWidget(self.sub)

    def set(self, cap, val, sub="", color=None, tip=""):
        th = theme()
        self.cap.setText(cap)
        self.val.setText(val)
        self.val.setStyleSheet(f"font-size: 16pt; font-weight: bold; color: {color or th['text']};")
        self.sub.setText(sub)
        self.sub.setVisible(bool(sub))
        self.setToolTip(tip)


class HBars(QWidget):
    """แท่งแนวนอนพร้อมป้ายชื่อ  items = [(ชื่อ, สัดส่วน 0..1, ข้อความท้ายแท่ง, สี)]"""
    ROW = 30

    def __init__(self):
        super().__init__()
        self.items = []
        self.setMinimumHeight(40)

    def set_items(self, items):
        self.items = items
        self.setFixedHeight(max(40, self.ROW * len(items) + 4))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        th = theme()
        f = QFont()
        f.setFamilies(FONT_FAMILIES)
        f.setPointSize(9)
        p.setFont(f)
        fm = QFontMetrics(f)
        lw, tw = max(150, min(236, int(self.width() * 0.42))), 46
        bw = max(20, self.width() - lw - tw - 8)
        for i, (label, frac, text, color) in enumerate(self.items):
            y = 2 + i * self.ROW
            p.setPen(QColor(th["text"]))
            p.drawText(QRectF(0, y, lw - 6, self.ROW - 6), Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(label, Qt.ElideRight, lw - 8))
            by = y + (self.ROW - 6 - 12) / 2.0
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(th["prog"]))
            p.drawRoundedRect(QRectF(lw, by, bw, 12), 6, 6)
            if frac > 0.004:
                p.setBrush(QColor(color))
                p.drawRoundedRect(QRectF(lw, by, max(8.0, bw * min(1.0, frac)), 12), 6, 6)
            p.setPen(QColor(th["hint"]))
            p.drawText(QRectF(self.width() - tw, y, tw, self.ROW - 6), Qt.AlignRight | Qt.AlignVCenter, text)
        p.end()


class ColChart(QWidget):
    """กราฟแท่ง 0-100% มีเส้นเป้าหมาย  cols = [(ป้ายใต้แท่ง, สัดส่วน|None, tooltip)]"""

    def __init__(self):
        super().__init__()
        self.cols = []
        self.setMinimumHeight(170)
        self.setMouseTracking(True)

    def set_cols(self, cols):
        self.cols = cols
        self.update()

    def _plot(self):
        return 38, 16, max(10, self.width() - 48), max(10, self.height() - 16 - 30)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        th = theme()
        _chart_font(p)
        left, top, w, h = self._plot()
        for i in range(3):
            y = top + h - h * i / 2.0
            p.setPen(QPen(QColor(th["border"]), 1))
            p.drawLine(QPointF(left, y), QPointF(left + w, y))
            p.setPen(QColor(th["hint"]))
            p.drawText(QRectF(0, y - 8, left - 4, 16), Qt.AlignRight | Qt.AlignVCenter, f"{i * 50}%")
        n = max(1, len(self.cols))
        slot = w / float(n)
        bw = max(8.0, min(56.0, slot * 0.6))
        for i, (label, frac, _tip) in enumerate(self.cols):
            x = left + slot * i + (slot - bw) / 2.0
            if frac is None:
                p.setPen(QColor(th["hint"]))
                p.drawText(QRectF(x - 8, top + h - 20, bw + 16, 18), Qt.AlignCenter, "—")
            else:
                hh = max(2.0, h * frac)
                p.setPen(Qt.NoPen)
                p.setBrush(score_color(frac))
                p.drawRoundedRect(QRectF(x, top + h - hh, bw, hh), 4, 4)
                p.setPen(QColor(th["text"]))
                p.drawText(QRectF(x - 10, top + h - hh - 17, bw + 20, 16), Qt.AlignCenter, f"{frac * 100:.0f}%")
            p.setPen(QColor(th["hint"]))
            p.drawText(QRectF(left + slot * i - 4, top + h + 5, slot + 8, 18), Qt.AlignCenter, label)
        gy = top + h - h * GOAL                                   # เส้นเป้าหมาย
        pen = QPen(QColor(STAT_COLORS[0]), 1.2, Qt.DashLine)
        p.setPen(pen)
        p.drawLine(QPointF(left, gy), QPointF(left + w, gy))
        p.end()

    def mouseMoveEvent(self, e):
        if not self.cols:
            return
        left, _t, w, _h = self._plot()
        i = int((e.position().x() - left) / (w / float(len(self.cols))))
        self.setToolTip(self.cols[i][2] if 0 <= i < len(self.cols) else "")


class TrendChart(QWidget):
    """เส้นแนวโน้มคะแนนรายวัน  points = [(ป้าย, สัดส่วน|None, tooltip)]"""

    def __init__(self):
        super().__init__()
        self.pts = []
        self.setMinimumHeight(170)
        self.setMouseTracking(True)

    def set_points(self, pts):
        self.pts = pts
        self.update()

    def _plot(self):
        return 38, 12, max(10, self.width() - 52), max(10, self.height() - 12 - 28)

    def _xy(self, i, frac):
        left, top, w, h = self._plot()
        n = max(1, len(self.pts))
        return left + w * (i + 0.5) / n, top + h - h * frac

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        th = theme()
        _chart_font(p)
        left, top, w, h = self._plot()
        for i in range(3):
            y = top + h - h * i / 2.0
            p.setPen(QPen(QColor(th["border"]), 1))
            p.drawLine(QPointF(left, y), QPointF(left + w, y))
            p.setPen(QColor(th["hint"]))
            p.drawText(QRectF(0, y - 8, left - 4, 16), Qt.AlignRight | Qt.AlignVCenter, f"{i * 50}%")
        gy = top + h - h * GOAL
        p.setPen(QPen(QColor(STAT_COLORS[0]), 1.2, Qt.DashLine))
        p.drawLine(QPointF(left, gy), QPointF(left + w, gy))
        n = len(self.pts)
        step = 1 if n <= 8 else (2 if n <= 16 else 5)
        line = QPen(QColor(th["primary"]), 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        prev = None
        for i, (label, frac, _tip) in enumerate(self.pts):
            if i % step == 0:
                p.setPen(QColor(th["hint"]))
                x0, _ = self._xy(i, 0)
                p.drawText(QRectF(x0 - 16, top + h + 5, 32, 16), Qt.AlignCenter, label)
            if frac is None:
                prev = None
                continue
            cur = QPointF(*self._xy(i, frac))
            if prev is not None:
                p.setPen(line)
                p.drawLine(prev, cur)
            prev = cur
        for i, (_l, frac, _tip) in enumerate(self.pts):
            if frac is None:
                continue
            p.setPen(QPen(QColor(th["card"]), 1.5))
            p.setBrush(score_color(frac))
            p.drawEllipse(QPointF(*self._xy(i, frac)), 4.2, 4.2)
        if not any(q[1] is not None for q in self.pts):
            p.setPen(QColor(th["hint"]))
            p.drawText(QRectF(left, top, w, h), Qt.AlignCenter, tr("ยังไม่มีข้อมูล"))
        p.end()

    def mouseMoveEvent(self, e):
        if not self.pts:
            return
        left, _t, w, _h = self._plot()
        i = int((e.position().x() - left) / (w / float(len(self.pts))))
        self.setToolTip(self.pts[i][2] if 0 <= i < len(self.pts) else "")


class HeatMap(QWidget):
    """ตารางสี วัน x ชั่วโมง  grid[wd][hour] = [ดี(วิ), ที่ตรวจจับ(วิ)]"""
    CELL_H = 18
    LEFT = 34

    def __init__(self):
        super().__init__()
        self.grid = None
        self.setMouseTracking(True)
        self.setFixedHeight(20 + 7 * self.CELL_H + 4)

    def set_grid(self, grid):
        self.grid = grid
        self.update()

    def _cw(self):
        return max(6.0, (self.width() - self.LEFT - 4) / 24.0)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        th = theme()
        _chart_font(p)
        cw = self._cw()
        wd = WD_EN if LANG == "en" else WD_TH
        for h in range(0, 24, 3):
            p.setPen(QColor(th["hint"]))
            p.drawText(QRectF(self.LEFT + cw * h - 6, 0, cw * 3, 16), Qt.AlignLeft | Qt.AlignVCenter, f"{h:02d}")
        for r in range(7):
            y = 20 + r * self.CELL_H
            p.setPen(QColor(th["hint"]))
            p.drawText(QRectF(0, y, self.LEFT - 4, self.CELL_H), Qt.AlignRight | Qt.AlignVCenter, wd[r])
            for h in range(24):
                good, tot = self.grid[r][h] if self.grid else (0.0, 0.0)
                p.setPen(Qt.NoPen)
                p.setBrush(score_color(good / tot) if tot >= 120 else QColor(th["prog"]))
                p.drawRoundedRect(QRectF(self.LEFT + cw * h + 1, y + 1, cw - 2, self.CELL_H - 2), 3, 3)
        p.end()

    def mouseMoveEvent(self, e):
        if not self.grid:
            return
        h = int((e.position().x() - self.LEFT) / self._cw())
        r = int((e.position().y() - 20) / self.CELL_H)
        if 0 <= h < 24 and 0 <= r < 7 and self.grid[r][h][1] >= 120:
            good, tot = self.grid[r][h]
            wd = WD_EN if LANG == "en" else WD_TH
            self.setToolTip(tr("{d} {a:02d}:00   นั่งดี {p:.0f}%  (ตรวจจับรวม {t})").format(
                d=wd[r], a=h, p=good / tot * 100, t=fmt_dur(tot)))
        else:
            self.setToolTip("")


class StatsWindow(QWidget):
    """หน้าสถิติ: ฝังอยู่ในหน้าต่างหลักด้านขวา จัดคอลัมน์ตามความกว้าง (1 / 2 / 3 คอลัมน์)"""

    def __init__(self, ctrl):
        super().__init__()
        self.ctrl = ctrl
        self.period = "today"
        f = QFont()
        f.setFamilies(FONT_FAMILIES)
        f.setPointSize(10)
        self.setFont(f)
        self.setStyleSheet(make_qss())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        root = QVBoxLayout(inner)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        head = QLabel("📊  " + tr("สถิติท่านั่ง"))
        head.setStyleSheet("font-size: 16pt; font-weight: bold;")
        root.addWidget(head)
        seg = QHBoxLayout()
        seg.setSpacing(6)
        self.grp = QButtonGroup(self)
        self.pbtn = {}
        for key, name in (("today", "วันนี้"), ("7", "7 วัน"), ("30", "30 วัน")):
            b = QPushButton(tr(name))
            b.setCheckable(True)
            b.setProperty("seg", True)
            self.grp.addButton(b)
            b.clicked.connect(lambda _=False, k=key: self.set_period(k))
            seg.addWidget(b)
            self.pbtn[key] = b
        self.pbtn["today"].setChecked(True)
        root.addLayout(seg)

        # ---- การ์ดคะแนน ----
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 14, 18, 14)
        cl.setSpacing(2)
        self.big = QLabel("—")
        self.big.setAlignment(Qt.AlignCenter)
        self.big.setStyleSheet("font-size: 36pt; font-weight: bold;")
        self.big_sub = QLabel("")
        self.big_sub.setAlignment(Qt.AlignCenter)
        self.big_sub.setObjectName("hint")
        self.legend = QLabel("")
        self.legend.setAlignment(Qt.AlignCenter)
        self.legend.setTextFormat(Qt.RichText)
        self.goal_lbl = QLabel("")
        self.goal_lbl.setAlignment(Qt.AlignCenter)
        self.goal_lbl.setTextFormat(Qt.RichText)
        cl.addWidget(self.big)
        cl.addWidget(self.big_sub)
        cl.addSpacing(4)
        cl.addWidget(self.legend)
        cl.addWidget(self.goal_lbl)
        root.addWidget(card)

        # ---- ตัวเลขสรุป (KPI) ----
        self.kpi_grid = QGridLayout()
        self.kpi_grid.setSpacing(8)
        self.tiles = [KpiTile() for _ in range(8)]
        self.place_tiles(2)
        root.addLayout(self.kpi_grid)
        self.items = []                               # กล่องกราฟ/ข้อสังเกต (จัดเรียงเป็นคอลัมน์ตามความกว้าง)

        # ---- กราฟ ----
        box = QFrame()
        box.setObjectName("card")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(14, 12, 14, 10)
        self.chart_title = QLabel("")
        self.chart_title.setObjectName("h")
        self.chart = StackChart()
        bl.addWidget(self.chart_title)
        bl.addWidget(self.chart)
        self.items.append(box)

        def make_box(title, widget, hint=None):
            fr = QFrame()
            fr.setObjectName("card")
            lay = QVBoxLayout(fr)
            lay.setContentsMargins(14, 12, 14, 10)
            ttl = QLabel("")
            ttl.setObjectName("h")
            lay.addWidget(ttl)
            lay.addWidget(widget)
            hl = QLabel("")
            hl.setObjectName("hint")
            hl.setWordWrap(True)
            lay.addWidget(hl)
            self.items.append(fr)
            return fr, ttl, hl

        self.trend = TrendChart()
        self.trend_box, self.trend_title, self.trend_hint = make_box("", self.trend)
        self.fat = ColChart()
        self.fat_box, self.fat_title, self.fat_hint = make_box("", self.fat)
        self.causes = HBars()
        self.cause_box, self.cause_title, self.cause_hint = make_box("", self.causes)
        self.heat = HeatMap()
        self.heat_box, self.heat_title, self.heat_hint = make_box("", self.heat)

        # ---- ข้อสังเกต ----
        box2 = QFrame()
        box2.setObjectName("card")
        il = QVBoxLayout(box2)
        il.setContentsMargins(16, 12, 16, 12)
        h2 = QLabel(tr("ข้อสังเกต"))
        h2.setObjectName("h")
        self.insight = QLabel("")
        self.insight.setWordWrap(True)
        self.insight.setTextFormat(Qt.RichText)
        il.addWidget(h2)
        il.addWidget(self.insight)
        self.items.append(box2)
        self.body = QHBoxLayout()
        self.body.setSpacing(10)
        root.addLayout(self.body)
        self.columns = []
        self.cols_cur = 0
        self.relayout(1)

        row = QHBoxLayout()
        b_csv = QPushButton(tr("📤  ส่งออก CSV"))
        b_csv.clicked.connect(self.export_csv)
        b_clr = QPushButton(tr("🗑  ล้างสถิติ"))
        b_clr.clicked.connect(self.clear_stats)
        b_json = QPushButton(tr("🧾  ส่งออก JSON"))
        b_json.setToolTip(tr("ข้อมูลรายวันแบบละเอียด (ไว้ต่อยอดไปทำ dashboard)"))
        b_json.clicked.connect(self.export_json)
        row.addWidget(b_csv)
        row.addWidget(b_json)
        row.addWidget(b_clr)
        root.addLayout(row)
        note = QLabel(tr("ข้อมูลเก็บในเครื่องคุณ (stats.json) จะส่งออกเฉพาะเมื่อคุณเปิด \"ส่งสถิติขึ้นเว็บ\" เอง"))
        note.setObjectName("hint")
        note.setWordWrap(True)
        root.addWidget(note)
        root.addStretch(1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.isVisible() and self.refresh())
        self.timer.start(5000)
        self.refresh()

    def set_period(self, key):
        self.period = key
        self.refresh()

    def place_tiles(self, cols):
        for t in self.tiles:
            self.kpi_grid.removeWidget(t)
        for i, t in enumerate(self.tiles):
            self.kpi_grid.addWidget(t, i // cols, i % cols)

    def relayout(self, cols):
        """แบ่งกล่องสถิติเป็น cols คอลัมน์ (กว้างขึ้น = วางเคียงกันมากขึ้น)"""
        if cols == self.cols_cur:
            return
        self.cols_cur = cols
        for lay in self.columns:
            for w in self.items:
                lay.removeWidget(w)
            self.body.removeItem(lay)
            lay.deleteLater()
        self.columns = []
        for _ in range(cols):
            lay = QVBoxLayout()
            lay.setSpacing(10)
            self.body.addLayout(lay, 1)
            self.columns.append(lay)
        for i, w in enumerate(self.items):
            self.columns[i % cols].addWidget(w)
        for lay in self.columns:
            lay.addStretch(1)
        self.place_tiles(2 if cols == 1 else 4)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if not hasattr(self, "cols_cur"):
            return
        w = self.width()
        self.relayout(1 if w < 760 else (2 if w < 1250 else 3))

    def _keys(self, offset=0):
        n = {"today": 1, "7": 7, "30": 30}[self.period]
        return Stats.date_keys(n, offset * n)

    def refresh(self):
        st = self.ctrl.stats
        keys, prev_keys = self._keys(0), self._keys(1)
        tot, ptot = st.totals(keys), st.totals(prev_keys)
        score = Stats.score([tot["n"], tot["w"], tot["b"]])
        pscore = Stats.score([ptot["n"], ptot["w"], ptot["b"]])
        th = theme()

        # ---- การ์ดคะแนน ----
        if score is None:
            self.big.setText("—")
            self.big.setStyleSheet(f"font-size: 36pt; font-weight: bold; color: {th['hint']};")
            self.big_sub.setText(tr("ยังไม่มีข้อมูลพอ (ต้องตรวจจับอย่างน้อย 1 นาที)"))
        else:
            col = STAT_COLORS[0] if score >= 0.7 else (STAT_COLORS[1] if score >= 0.45 else STAT_COLORS[2])
            self.big.setText(f"{score * 100:.0f}%")
            self.big.setStyleSheet(f"font-size: 36pt; font-weight: bold; color: {col};")
            sub = tr("ของเวลาที่ตรวจจับ นั่งท่าดี")
            if pscore is not None:
                diff = (score - pscore) * 100
                vs = {"today": "เมื่อวาน", "7": "7 วันก่อนหน้า", "30": "30 วันก่อนหน้า"}[self.period]
                if abs(diff) >= 1:
                    sub += "   " + ("▲ " if diff > 0 else "▼ ") + f"{abs(diff):.0f}% " + tr("เทียบกับ") + tr(vs)
            self.big_sub.setText(sub)
        names = (tr("นั่งดี"), tr("เตือน"), tr("ค่อม"))
        vals = (tot["n"], tot["w"], tot["b"])
        self.legend.setText("&nbsp;&nbsp;&nbsp;".join(
            f'<span style="color:{STAT_COLORS[i]}">●</span> {names[i]} <b>{fmt_dur(vals[i])}</b>'
            for i in range(3)))

        # ---- กราฟ ----
        bars = []
        if self.period == "today":
            hv = st.hourly(keys)
            act = [h for h in range(24) if sum(hv[h][:3]) > 0]
            if act:
                a, b = max(0, min(act) - 1), min(23, max(act) + 1)
                while b - a < 7:
                    if a > 0:
                        a -= 1
                    if b - a < 7 and b < 23:
                        b += 1
                    if a == 0 and b == 23:
                        break
                for h in range(a, b + 1):
                    v = hv[h]
                    tip = tr("{a:02d}:00 - {b:02d}:00   นั่งดี {n}  เตือน {w}  ค่อม {c}").format(
                        a=h, b=(h + 1) % 24, n=fmt_dur(v[0]), w=fmt_dur(v[1]), c=fmt_dur(v[2]))
                    bars.append((str(h), tuple(v[:3]), tip))
            self.chart_title.setText(tr("เวลาที่นั่งแต่ละชั่วโมง (นาที)"))
            unit = "min"
        else:
            wd = WD_EN if LANG == "en" else WD_TH
            for key, v in st.daily(keys):
                d = datetime.date.fromisoformat(key)
                label = wd[d.weekday()] if self.period == "7" else str(d.day)
                tip = tr("{d}   นั่งดี {n}  เตือน {w}  ค่อม {c}").format(
                    d=key, n=fmt_dur(v[0]), w=fmt_dur(v[1]), c=fmt_dur(v[2]))
                bars.append((label, tuple(v[:3]), tip))
            if not any(sum(b[1]) > 0 for b in bars):
                bars = []
            self.chart_title.setText(tr("เวลาที่นั่งแต่ละวัน (ชั่วโมง)"))
            unit = "hr"
        if bars and not any(sum(b[1]) > 0 for b in bars):
            bars = []
        self.chart.set_bars(bars, unit)

        self.refresh_details(st, keys, tot, score, th)

        # ---- ข้อสังเกต ----
        lines = []
        tracked = tot["n"] + tot["w"] + tot["b"]
        if tracked < 60:
            lines.append(tr("เปิดโปรแกรมทิ้งไว้สักพัก แล้วกลับมาดูใหม่นะ"))
        else:
            hv = st.hourly(keys)
            worst = None
            for h in range(24):
                tt = sum(hv[h][:3])
                if tt >= 180:
                    ratio = (hv[h][1] + hv[h][2]) / tt
                    if worst is None or ratio > worst[0]:
                        worst = (ratio, h)
            if worst and worst[0] >= 0.15:
                lines.append(tr("ช่วงที่ค่อมบ่อยสุด: {a:02d}:00 - {b:02d}:00 (ค่อมหรือเริ่มค่อม {p:.0f}% ของช่วงนั้น)").format(
                    a=worst[1], b=(worst[1] + 1) % 24, p=worst[0] * 100))
            else:
                lines.append(tr("ท่านั่งสม่ำเสมอดีมาก ไม่มีช่วงไหนที่ค่อมเด่นชัด 👏"))
            cs = tot["cause"]
            if sum(cs) >= 20:
                j = max(range(4), key=lambda i: cs[i])
                share = cs[j] / sum(cs) * 100
                lines.append(tr("สาเหตุหลักที่ทำให้ค่อม: {c} ({p:.0f}%)").format(c=tr(CAUSE_INFO[j][0]), p=share)
                             + "<br>&nbsp;&nbsp;&nbsp;💡 " + tr(CAUSE_INFO[j][1]))
            if self.period != "today":
                sc = [(Stats.score(v), k) for k, v in st.daily(keys)
                      if v[0] + v[1] + v[2] >= 600]
                sc = [(s, k) for s, k in sc if s is not None]
                if len(sc) >= 2:
                    best = max(sc)
                    lines.append(tr("วันที่นั่งดีที่สุด: {d} ({p:.0f}%)").format(d=best[1], p=best[0] * 100))
            lines.append(tr("ถูกเตือนหลังค่อมชัดเจน {n} ครั้ง").format(n=tot["alerts"]))
        lines += self.extra_insights(st, keys)
        if tot["pet"] or tot["snack"]:
            lines.append(tr("ลูบหัวมาสคอต {a} ครั้ง ป้อนขนม {b} ชิ้น 🍪").format(a=tot["pet"], b=tot["snack"]))
        if tot["x"] >= 60:
            lines.append(tr("ไม่อยู่หน้ากล้องรวม {t}").format(t=fmt_dur(tot["x"])))
        self.insight.setText("<br>".join("• " + ln for ln in lines))

    def refresh_details(self, st, keys, tot, score, th):
        """การ์ดตัวเลข / แนวโน้ม / ผลของการนั่งนาน / สาเหตุ / heatmap"""
        sm = st.summary(keys)
        multi = self.period != "today"

        # ---- เป้าหมาย ----
        streak = st.goal_streak()
        if score is None:
            self.goal_lbl.setText("")
        else:
            gp = GOAL * 100
            if score >= GOAL:
                txt = f'<span style="color:{STAT_COLORS[0]}">✓ ' + tr("ถึงเป้าหมาย {g:.0f}% แล้ว!").format(g=gp) + "</span>"
            else:
                txt = tr("🎯 เป้าหมาย {g:.0f}%  อีก {d:.0f}% จะถึงเป้า").format(g=gp, d=(GOAL - score) * 100)
            if streak >= 2:
                txt += "   🔥 " + tr("ทำเป้าติดต่อกัน {n} วัน").format(n=streak)
            self.goal_lbl.setText(txt)

        # ---- KPI ----
        T = self.tiles
        T[0].set(tr("เวลาที่ตรวจจับรวม"), fmt_dur(sm["tracked"]) if sm["tracked"] else "—",
                 tr("ใน {n} วันที่มีข้อมูล").format(n=sm["days_active"]) if multi and sm["days_active"] else "")
        if multi:
            T[1].set(tr("เฉลี่ยต่อวัน"), fmt_dur(sm["avg_day"]) if sm["days_active"] else "—",
                     tr("ไม่อยู่หน้ากล้องรวม {t}").format(t=fmt_dur(sm["away"])) if sm["away"] >= 60 else "")
        else:
            T[1].set(tr("ไม่อยู่หน้ากล้อง"), fmt_dur(sm["away"]) if sm["away"] >= 60 else "—")
        pen = sm["avg_penalty"]
        pcol = None if pen is None else (STAT_COLORS[0] if pen < 25 else (STAT_COLORS[1] if pen < 50 else STAT_COLORS[2]))
        T[2].set(tr("คะแนนความค่อมเฉลี่ย"), "—" if pen is None else f"{pen:.0f}/100",
                 tr("ยิ่งต่ำยิ่งดี (0 = ตรงเท่าท่ามาตรฐาน)"), pcol,
                 tr("เฉลี่ยจากคะแนนทุกวินาทีที่ตรวจจับ"))
        T[3].set(tr("นั่งดีต่อเนื่องนานสุด"), fmt_dur(sm["good_max"]) if sm["good_max"] >= 60 else "—",
                 tr("ไม่ค่อม ไม่เตือน ต่อเนื่องไม่ขาด"), STAT_COLORS[0] if sm["good_max"] >= 1800 else None)
        smax = sm["sess_max"]
        scol = None if smax < 3600 else (STAT_COLORS[1] if smax < 5400 else STAT_COLORS[2])
        T[4].set(tr("นั่งนานสุดโดยไม่ลุก"), fmt_dur(smax) if smax >= 60 else "—",
                 tr("ควรลุกยืดเส้นทุก 45-60 นาที") if smax >= 3600 else "", scol)
        T[5].set(tr("ลุกพัก (ครั้ง)"), str(sm["breaks"]) if sm["sessions"] or sm["breaks"] else "—",
                 tr("นั่งเฉลี่ยรอบละ {t}").format(t=fmt_dur(sm["avg_sess"])) if sm["avg_sess"] else "",
                 None, tr("นับเมื่อออกจากหน้ากล้องเกิน 2 นาที"))
        T[6].set(tr("ค่อมชัดเจน (ครั้ง)"), str(sm["bad_ep"]) if sm["tracked"] >= 60 else "—",
                 tr("แก้ท่าเฉลี่ยครั้งละ {t}").format(t=fmt_short(sm["bad_avg"])) if sm["bad_avg"] else "",
                 STAT_COLORS[2] if sm["bad_ep"] >= 10 else None)
        T[7].set(tr("ทำเป้าติดต่อกัน (วัน)"), f"🔥 {streak}" if streak else "0",
                 tr("วันที่นั่งดีถึง {g:.0f}%").format(g=GOAL * 100))

        # ---- แนวโน้มรายวัน / heatmap (เฉพาะ 7 และ 30 วัน) ----
        self.trend_box.setVisible(multi)
        self.heat_box.setVisible(multi)
        if multi:
            wd = WD_EN if LANG == "en" else WD_TH
            pts = []
            for k, v in st.daily(keys):
                d = datetime.date.fromisoformat(k)
                sc = Stats.score(v, 600.0)
                label = wd[d.weekday()] if self.period == "7" else str(d.day)
                tip = tr("{d}   นั่งดี {p:.0f}%  (ตรวจจับ {t})").format(
                    d=k, p=(sc or 0) * 100, t=fmt_dur(v[0] + v[1] + v[2])) if sc is not None else f"{k}   —"
                pts.append((label, sc, tip))
            self.trend_title.setText(tr("แนวโน้มคะแนนท่านั่งรายวัน"))
            self.trend.set_points(pts)
            good_days = sum(1 for p_ in pts if p_[1] is not None and p_[1] >= GOAL)
            have = sum(1 for p_ in pts if p_[1] is not None)
            self.trend_hint.setText(tr("เส้นประสีเขียว = เป้าหมาย {g:.0f}%   ·   ถึงเป้า {a} จาก {b} วันที่มีข้อมูล").format(
                g=GOAL * 100, a=good_days, b=have))
            self.heat_title.setText(tr("ช่วงเวลาที่นั่งดี/แย่ (วัน × ชั่วโมง)"))
            self.heat.set_grid(st.heatmap(keys))
            self.heat_hint.setText(tr("เขียว = นั่งดี  เหลือง = เริ่มค่อม  แดง = ค่อมบ่อย  เทา = ไม่มีข้อมูล (เอาเมาส์ชี้ดูรายละเอียด)"))

        # ---- นั่งนานท่ายิ่งแย่ไหม ----
        fat = st.fatigue(keys)
        names = (tr("0-30 น."), tr("30-60 น."), tr("60-90 น."), tr("90+ น."))
        cols = []
        for i, (frac, secs) in enumerate(fat):
            tip = (tr("นั่งต่อเนื่อง {n}   นั่งดี {p:.0f}%  (ตรวจจับ {t})").format(
                n=names[i], p=frac * 100, t=fmt_dur(secs)) if frac is not None else tr("ยังไม่มีข้อมูลพอ"))
            cols.append((names[i], frac, tip))
        self.fat_box.setVisible(any(c_[1] is not None for c_ in cols))   # ไม่มีข้อมูลก็ซ่อนไปเลย ไม่รก
        self.fat_title.setText(tr("ยิ่งนั่งนาน ท่ายิ่งแย่ไหม?"))
        self.fat.set_cols(cols)
        self.fat_hint.setText(tr("คะแนนท่าดีแยกตามเวลาที่นั่งมาแล้วในแต่ละรอบ (นับใหม่ทุกครั้งที่ลุกพักเกิน 2 นาที)"))

        # ---- สาเหตุที่ทำให้ค่อม (ครบทั้ง 4) ----
        cs = tot["cause"]
        total = sum(cs)
        self.cause_title.setText(tr("สาเหตุที่ทำให้ค่อม (สัดส่วน)"))
        if total < 20:
            self.causes.set_items([])
            self.cause_hint.setText(tr("ยังไม่มีช่วงที่ค่อม/เตือนมากพอ จึงยังวิเคราะห์สาเหตุไม่ได้ 👍"))
        else:
            order = sorted(range(4), key=lambda i: -cs[i])
            cols_ = (STAT_COLORS[2], STAT_COLORS[1], "#5b8def", "#9b7fe0")
            self.causes.set_items([(tr(CAUSE_INFO[i][0]), cs[i] / total, f"{cs[i] / total * 100:.0f}%", cols_[r])
                                   for r, i in enumerate(order)])
            self.cause_hint.setText("💡 " + tr(CAUSE_INFO[order[0]][1]))

    def extra_insights(self, st, keys):
        out = []
        sm = st.summary(keys)
        if sm["sess_max"] >= 3600:
            out.append(tr("นั่งต่อเนื่องนานที่สุด {t} โดยไม่ลุก ลองตั้งใจลุกยืดเส้นทุก 45-60 นาที 🚶").format(
                t=fmt_dur(sm["sess_max"])))
        fat = [x for x in st.fatigue(keys) if x[0] is not None]
        if len(fat) >= 2:
            a, b = fat[0][0] * 100, fat[-1][0] * 100
            if b <= a - 10:
                out.append(tr("ยิ่งนั่งนานท่ายิ่งแย่: ช่วงแรกนั่งดี {a:.0f}% แต่พอนั่งนานเหลือ {b:.0f}%").format(a=a, b=b))
            elif b >= a - 3:
                out.append(tr("นั่งนานแล้วท่ายังนิ่งเหมือนเดิม ทำได้ดีมาก 👏"))
        if sm["bad_ep"] and sm["bad_avg"]:
            out.append(tr("ค่อมชัดเจน {n} ครั้ง ใช้เวลาแก้ท่าเฉลี่ยครั้งละ {t}").format(
                n=sm["bad_ep"], t=fmt_short(sm["bad_avg"])))
        return out

    def export_json(self):
        fn, _ = QFileDialog.getSaveFileName(self, tr("ส่งออกสถิติ"),
                                            str(Path.home() / "posture_stats.json"), "JSON (*.json)")
        if not fn:
            return
        try:
            self.ctrl.stats.export_json(fn)
            QMessageBox.information(self, APP_NAME, tr("บันทึกแล้ว"))
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, tr("บันทึกไม่สำเร็จ: ") + str(e))

    def export_csv(self):
        fn, _ = QFileDialog.getSaveFileName(self, tr("ส่งออกสถิติ"),
                                            str(Path.home() / "posture_stats.csv"), "CSV (*.csv)")
        if not fn:
            return
        try:
            self.ctrl.stats.export_csv(fn)
            QMessageBox.information(self, APP_NAME, tr("บันทึกแล้ว"))
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, tr("บันทึกไม่สำเร็จ: ") + str(e))

    def clear_stats(self):
        ans = QMessageBox.question(self, APP_NAME, tr("ล้างสถิติทั้งหมดใช่ไหม? (กู้คืนไม่ได้)"))
        if ans == QMessageBox.Yes:
            self.ctrl.stats.clear()
            self.refresh()

    def closeEvent(self, e):
        e.ignore()
        self.hide()


# ----------------------------------------------------------------------------
#  6) ตัวควบคุมหลัก + system tray
# ----------------------------------------------------------------------------
STATUS_TH = {"normal": "ปกติ", "warning": "เตือน", "bad": "หลังค่อม!", "no_person": "ไม่พบคน"}


class WebSyncDialog(QDialog):
    """ตั้งค่าการส่งสถิติขึ้นเว็บ (ปิดไว้เป็นค่าเริ่มต้น ส่งเฉพาะตัวเลขสรุป ไม่ส่งภาพ)"""

    def __init__(self, ctrl):
        super().__init__(ctrl.panel)
        self.ctrl, self.cfg = ctrl, ctrl.cfg
        self.setWindowTitle(tr("🌐  ส่งสถิติขึ้นเว็บ"))
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        info = QLabel(tr("ส่งเฉพาะตัวเลขสรุป (เวลานั่งดี/เตือน/ค่อม รายชั่วโมง สาเหตุ) ไม่ส่งภาพจากกล้อง "
                         "แต่ละเครื่องใช้รหัสส่วนตัวของตัวเอง คนอื่นที่ใช้เว็บเดียวกันจะไม่เห็นข้อมูลของคุณ ปิดไว้เป็นค่าเริ่มต้น"))
        info.setWordWrap(True)
        info.setObjectName("hint")
        lay.addWidget(info)
        self.cb = QCheckBox(tr("เปิดการส่งสถิติขึ้นเว็บ"))
        self.cb.setChecked(bool(self.cfg["web_on"]))
        lay.addWidget(self.cb)
        lay.addWidget(QLabel(tr("URL เว็บ (เช่น https://ชื่อไซต์.netlify.app)")))
        self.url = QLineEdit(self.cfg["web_url"] or "")
        self.url.setPlaceholderText("https://your-site.netlify.app")
        lay.addWidget(self.url)
        lay.addWidget(QLabel(tr("รหัสเชิญ (ถ้าเจ้าของไซต์ตั้งไว้ — เว้นว่างได้)")))
        self.inv = QLineEdit(self.cfg["web_invite"] or "")
        lay.addWidget(self.inv)
        lay.addWidget(QLabel(tr("รหัสส่วนตัวของคุณ (ใช้ดูข้อมูลบนเว็บ อย่าแชร์ให้ใคร)")))
        row = QHBoxLayout()
        self.key = QLineEdit(ctrl.web.key())
        self.key.setReadOnly(True)
        self.key.setEchoMode(QLineEdit.Password)
        row.addWidget(self.key, 1)
        show = QPushButton("👁")
        show.setFixedWidth(44)
        show.setCheckable(True)
        show.toggled.connect(lambda v: self.key.setEchoMode(QLineEdit.Normal if v else QLineEdit.Password))
        row.addWidget(show)
        cp = QPushButton(tr("📋  คัดลอกรหัส"))
        cp.clicked.connect(self.copy_key)
        row.addWidget(cp)
        gen = QPushButton(tr("สุ่มรหัสใหม่"))
        gen.clicked.connect(self.gen)
        row.addWidget(gen)
        lay.addLayout(row)
        dev = QLabel(tr("รหัสเครื่องนี้: ") + ctrl.web.device_id())
        dev.setObjectName("hint")
        lay.addWidget(dev)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)
        btns = QHBoxLayout()
        t = QPushButton(tr("🔌  ทดสอบการเชื่อมต่อ"))
        t.clicked.connect(self.test)
        o = QPushButton(tr("🌍  เปิดแดชบอร์ดในเบราว์เซอร์"))
        o.clicked.connect(self.open_dash)
        ok = QPushButton(tr("บันทึก"))
        ok.clicked.connect(self.save)
        btns.addWidget(t)
        btns.addWidget(o)
        btns.addStretch(1)
        btns.addWidget(ok)
        lay.addLayout(btns)

    def copy_key(self):
        QApplication.clipboard().setText(self.ctrl.web.key())
        self.msg.setText(tr("คัดลอกรหัสแล้ว"))

    def gen(self):
        self.key.setText(self.ctrl.web.new_key())
        self.key.setEchoMode(QLineEdit.Normal)
        self.msg.setText(tr("สุ่มรหัสใหม่แล้ว ต้องเปิดแดชบอร์ดผ่านปุ่มด้านล่างอีกครั้ง (ข้อมูลเก่าจะไม่แสดงภายใต้รหัสใหม่)"))

    def _apply(self):
        self.cfg["web_url"] = self.url.text().strip()
        self.cfg["web_invite"] = self.inv.text().strip()
        self.cfg["web_on"] = self.cb.isChecked()

    def open_dash(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        self._apply()
        link = self.ctrl.web.dashboard_url()
        if not link:
            self.msg.setText(tr("กรอก URL เว็บก่อน"))
            return
        QDesktopServices.openUrl(QUrl(link))

    def test(self):
        self._apply()
        if not normalize_url(self.cfg["web_url"]):
            self.msg.setText(tr("กรอก URL เว็บก่อน"))
            return
        self.msg.setText(tr("กำลังทดสอบ…"))
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            ok, text = self.ctrl.web.test()
        finally:
            QApplication.restoreOverrideCursor()
        self.msg.setText(("✅ " + tr("เชื่อมต่อสำเร็จ")) if ok else ("❌ " + text))

    def save(self):
        self._apply()
        self.ctrl.web._soon = True
        self.accept()


def set_autostart(enable):
    if sys.platform != "win32":
        return False
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                         winreg.KEY_SET_VALUE)
    try:
        if enable:
            cmd = f'"{sys.executable}"' if FROZEN else \
                f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)
    return True


def is_autostart():
    if sys.platform != "win32":
        return False
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run")
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def play_alert():
    def _run():
        try:
            if sys.platform == "win32":
                import winsound
                winsound.Beep(880, 160)
                winsound.Beep(660, 220)
            else:
                QApplication.beep()
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


class Controller(QObject):
    def __init__(self, app, use_camera=True):
        super().__init__()
        self.app = app
        self.cfg = Config()
        set_lang(self.cfg["lang"])
        set_theme(self.cfg["theme"])
        self.sound = SoundFx(self.cfg)
        self.stats = Stats()
        self.web = WebSync(self.cfg, self.stats, log, GOAL)
        self.stats_win = None
        self.wins = WindowTracker()
        self.wins.enabled = bool(self.cfg["roam"])
        self.mascot2 = None
        self.snack = None
        self._last_stat_t = None
        self._prev_stat = None
        self.away_since = None
        self.next_pair = time.time() + 30
        self.last_night = 0.0
        self._timers = []
        self.baseline = self.cfg["baseline"]
        self.machine = StatusMachine("no_person")
        self.cur_status = None
        self.ema = None
        self.calibrating = False
        self.override = None
        self.last_info = ""

        self.snooze_until = 0.0
        self.mascot = Mascot(load_sprites(bool(self.cfg["use_custom"])))
        self.mascot.set_species(self.cfg["character"])
        self.mascot.nag.connect(self.on_nag)
        self.mascot.wins = self.wins
        self.mascot.set_roam(self.cfg["roam"])
        self.mascot.set_size(self.cfg["size"])
        self.preview_win = PreviewWindow()
        self.preview_win.closed.connect(lambda: self.act_preview.setChecked(False))
        self.last_score = 0.0

        self.make_actions()
        self.build_menu()
        self.mascot.menu = self.menu
        self.panel = ControlPanel(self)

        self.tray = QSystemTrayIcon(QIcon(make_icon("normal", 64, self.cfg["character"])), self)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.on_tray)
        self.tray.show()

        self.tracker = Tracker(self.cfg["camera"])
        self.tracker.result.connect(self.on_result)
        self.tracker.preview.connect(self.preview_win.set_frame)
        self.tracker.calib.connect(self.on_calib)
        self.tracker.error.connect(self.on_error)
        if use_camera:
            self.tracker.start()

        self.apply_status("no_person", force=True)
        self.mascot.show()
        self.apply_companion()
        for ms, fn in ((30000, lambda: self.stats.save()), (20000, self.check_time), (200, self.pair_tick),
                       (2000, self.web.tick)):
            t = QTimer(self)
            t.timeout.connect(fn)
            t.start(ms)
            self._timers.append(t)
        QTimer.singleShot(4000, self.check_time)
        if self.baseline is None:
            self.mascot.say("สวัสดี! ฉันจะช่วยเฝ้าท่านั่งให้\nนั่งหลังตรงๆ แล้วรอสักครู่นะ", "info", 120)
            self.show_panel()                         # เปิดหน้าควบคุมให้เห็นครั้งแรก
            QTimer.singleShot(3500, self.start_calibration)

    # ---------- menu ----------
    def make_actions(self):
        """ตัวเก็บสถานะการตั้งค่า (หน้าควบคุมกับเมนูใช้ร่วมกัน)"""
        self.act_pause = QAction(tr("หยุดตรวจจับชั่วคราว (ปิดกล้อง)"), self)
        self.act_pause.setCheckable(True)
        self.act_pause.toggled.connect(self.toggle_pause)

        self.act_preview = QAction(tr("แสดงภาพกล้อง"), self)
        self.act_preview.setCheckable(True)
        self.act_preview.toggled.connect(self.toggle_preview)

        self.act_sound = QAction(tr("เสียงเตือนเมื่อหลังค่อม"), self)
        self.act_sound.setCheckable(True)
        self.act_sound.setChecked(bool(self.cfg["sound"]))
        self.act_sound.toggled.connect(self.toggle_sound)

        self.act_auto = QAction(tr("เปิดอัตโนมัติเมื่อเริ่ม Windows"), self)
        self.act_auto.setCheckable(True)
        self.act_auto.setChecked(is_autostart())
        self.act_auto.toggled.connect(self.toggle_autostart)

    def retext_actions(self):
        self.act_pause.setText(tr("หยุดตรวจจับชั่วคราว (ปิดกล้อง)"))
        self.act_preview.setText(tr("แสดงภาพกล้อง"))
        self.act_sound.setText(tr("เสียงเตือนเมื่อหลังค่อม"))
        self.act_auto.setText(tr("เปิดอัตโนมัติเมื่อเริ่ม Windows"))

    def build_menu(self):
        """เมนูคลิกขวา: เหลือแค่ที่ใช้บ่อย ที่เหลืออยู่ในหน้าควบคุม"""
        m = QMenu()
        f = QFont()
        f.setFamilies(FONT_FAMILIES)
        m.setFont(f)
        self.act_head = m.addAction("STATUS: ...")
        self.act_head.setEnabled(False)
        m.addSeparator()
        open_a = m.addAction(tr("⚙  เปิดหน้าต่างควบคุม"))
        ft = open_a.font()
        ft.setBold(True)
        open_a.setFont(ft)
        open_a.triggered.connect(self.show_panel)
        m.addAction(tr("📊  สถิติท่านั่ง")).triggered.connect(self.show_stats)
        m.addAction(tr("🍪  ให้ขนม")).triggered.connect(self.give_snack)
        m.addAction(self.act_pause)
        m.addAction(tr("ปรับท่านั่งมาตรฐาน (Calibrate)")).triggered.connect(self.start_calibration)
        m.addSeparator()
        m.addAction(tr("ออกจากโปรแกรม")).triggered.connect(self.quit)
        self.menu = m

    def show_panel(self):
        if not self.panel.isVisible():
            self.panel.fit_to_screen()
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    def on_panel_closed(self):
        if not self.cfg["tip_shown"]:
            self.cfg["tip_shown"] = True
            self.tray.showMessage(APP_NAME, tr("โปรแกรมยังทำงานอยู่ที่ไอคอนข้างนาฬิกา คลิกไอคอนเพื่อเปิดหน้าควบคุมอีกครั้ง"),
                                  QSystemTrayIcon.Information, 5000)

    def on_tray(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.panel.isVisible():
                self.panel.hide()
            else:
                self.show_panel()

    # ---------- actions ----------
    def start_calibration(self):
        if self.act_pause.isChecked():
            self.act_pause.setChecked(False)
        self.calibrating = True
        self.sound.stop_alarm()
        self.override = None
        self.mascot.set_status("calibrating")
        self.mascot.say("นั่งหลังตรงๆ มองกล้อง\nจะเริ่มจดจำใน 5 วินาที", "info", -1)
        self.tracker.request_calibration(5.0, 5.0)
        log("calibration requested")

    def on_calib(self, d):
        ph = d["phase"]
        if ph == "wait":
            self.mascot.say(tr("เตรียมตัว... นั่งหลังตรงๆ\nเริ่มใน {n} วิ").format(n=int(d['remaining']) + 1), "info", -1)
        elif ph == "hold":
            self.mascot.say(tr("นิ่งๆ นะ กำลังจดจำท่าที่ดี\nเหลือ {n} วิ").format(n=int(d['remaining']) + 1), "info", -1)
        elif ph == "done":
            self.baseline = d["baseline"]
            self.cfg["baseline"] = self.baseline
            self.finish_calibration()
            self.sound.play("done")
            self.mascot.say("เรียบร้อย! จำท่านั่งที่ดีของคุณแล้ว\nต่อไปฉันจะคอยเตือนนะ", "info", 150)
            log(f"calibrated: {self.baseline}")
        elif ph == "fail":
            self.finish_calibration()
            self.mascot.say("ไม่เห็นตัวคุณชัดเลย\nลองใหม่จากเมนู Calibrate นะ", "warn", 200)

    def finish_calibration(self):
        self.calibrating = False
        self.ema = None
        self.machine.force("normal")
        self.cur_status = None
        self.mascot.status = "calibrating"      # ให้ set_status ทำงานเต็มรูปแบบ
        self.apply_status("normal", force=True, quiet=True)
        self.panel.refresh()

    def toggle_pause(self, v):
        self.tracker.paused = bool(v)
        self.panel.set_paused(v)
        self.sync_alarm()
        if v:
            self.machine.force("no_person")
            self.apply_status("no_person", force=True)
            self.mascot.say("พักก่อนนะ (ปิดกล้องแล้ว)", "info", 90)

    def toggle_preview(self, v):
        self.tracker.want_preview = bool(v)
        self.preview_win.setVisible(bool(v))

    def toggle_autostart(self, v):
        try:
            set_autostart(bool(v))
        except Exception as e:
            self.tray.showMessage(APP_NAME, tr("ตั้งค่าเปิดอัตโนมัติไม่สำเร็จ: ") + str(e))

    def set_camera(self, i):
        self.cfg["camera"] = i
        self.tracker.reopen_camera(i)

    def demo(self, status):
        if status is None:
            self.override = None
            self.machine.force("normal")
            return
        self.override = (status, time.time() + 12.0)
        self.machine.force(status)
        self.apply_status(status, force=True)
        QTimer.singleShot(12400, self.end_demo)

    def open_sprites(self):
        d = BASE_DIR / "sprites"
        d.mkdir(exist_ok=True)
        readme = d / "README.txt"
        if not readme.exists():
            readme.write_text(
                "วางรูป PNG พื้นหลังโปร่งใส (หันหน้าไปทางขวา) ชื่อ:\n"
                "  normal.png  warning.png  bad.png  no_person.png\n"
                "แล้วเปิดโปรแกรมใหม่ มาสคอตจะใช้รูปของคุณแทนตัวที่วาดไว้\n", encoding="utf-8")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    # ---------- เสียง ----------
    def sync_alarm(self):
        """สัญญาณเตือนดังต่อเนื่องตราบใดที่ยังหลังค่อม (หยุดเมื่อท่าปกติ/พัก/ปรับท่า/ปิดเสียง)"""
        want = (self.cur_status == "bad" and not self.act_pause.isChecked()
                and not self.calibrating and time.time() >= self.snooze_until)
        if want and self.cfg["sound"]:
            self.sound.start_alarm()
        else:
            self.sound.stop_alarm()

    def toggle_sound(self, v):
        self.cfg["sound"] = bool(v)
        self.sync_alarm()

    def set_volume(self, key):
        self.cfg["volume"] = key
        if self.sound.alarm_on:
            self.sound.start_alarm(force=True)

    def test_alarm(self):
        if not self.cfg["sound"]:
            self.mascot.say("เสียงถูกปิดอยู่นะ\nติ๊กเปิดเสียงก่อน", "info", 90)
            return
        self.sound.start_alarm(force=True)
        QTimer.singleShot(2200, self.sync_alarm)

    def end_demo(self):
        if self.override and time.time() >= self.override[1] - 0.3:
            self.override = None
            st = "no_person" if self.act_pause.isChecked() else "normal"
            self.machine.force(st)
            self.apply_status(st, force=True)

    # ---------- รูปมาสคอต ----------
    def has_custom_files(self):
        d = BASE_DIR / "sprites"
        return any((d / f"{n}.png").exists() for n in STATUSES)

    def voice(self):
        return {"cat": "meow", "bunny": "squeak", "shiba": "woof", "raccoon": "chitter"}.get(self.cfg["character"], "meow")

    def set_character(self, key):
        """เปลี่ยนตัวละครที่วาดในโปรแกรม (รูปของผู้ใช้ไม่ถูกลบ สลับกลับมาใช้ได้)"""
        if key not in SPECIES:
            return
        self.cfg["character"] = key
        self.cfg["use_custom"] = False
        self.mascot.set_species(key)
        self.mascot.set_sprites({})
        self.tray.setIcon(QIcon(make_icon(self.cur_status or "normal", 64, key)))
        self.panel.refresh_sprites()
        self.mascot.say(SPECIES[key]["hello"], "info", 100)
        self.sound.play(self.voice())

    def set_custom(self, flag):
        self.cfg["use_custom"] = bool(flag)
        self.mascot.set_sprites(load_sprites(bool(flag)))
        self.panel.refresh_sprites()
        self.mascot.say("เปลี่ยนเป็นรูปของคุณแล้ว" if flag else SPECIES[self.cfg["character"]]["hello"], "info", 90)

    def pick_sprite(self):
        fn, _ = QFileDialog.getOpenFileName(self.panel, tr("เลือกรูปมาสคอต (PNG พื้นหลังโปร่งใส หันหน้าไปทางขวา)"),
                                            str(Path.home()), tr("รูปภาพ (*.png)"))
        if not fn:
            return
        if QPixmap(fn).isNull():
            QMessageBox.warning(self.panel, APP_NAME, tr("เปิดไฟล์รูปนี้ไม่ได้ ลองเลือกไฟล์ PNG อื่น"))
            return
        items = [tr(x) for x in ("ใช้รูปนี้ทุกสถานะ (ง่ายที่สุด)", "เฉพาะสถานะปกติ", "เฉพาะสถานะเตือน",
                                 "เฉพาะสถานะหลังค่อม", "เฉพาะสถานะไม่พบคน")]
        item, ok = QInputDialog.getItem(self.panel, APP_NAME, tr("ใช้รูปนี้กับสถานะไหน?"), items, 0, False)
        if not ok:
            return
        d = BASE_DIR / "sprites"
        d.mkdir(exist_ok=True)
        try:
            targets = STATUSES if item == items[0] else (STATUSES[items.index(item) - 1],)
            for n in targets:
                shutil.copyfile(fn, d / f"{n}.png")
        except Exception as e:
            QMessageBox.warning(self.panel, APP_NAME, tr("บันทึกรูปไม่สำเร็จ: ") + str(e))
            return
        self.set_custom(True)

    def on_nag(self, kind):
        if kind == "warn":
            self.sound.play("warn")
        elif kind == "poke":
            if self.sound.alarm_on:                    # จิ้มมาสคอตตอนเสียงดัง = ขอพัก 1 นาที
                self.snooze_until = time.time() + 60
                self.sound.stop_alarm()
                QTimer.singleShot(60500, self.sync_alarm)
                self.mascot.say("โอเค พักเสียง 1 นาที\nแต่ต้องนั่งตรงๆ นะ!", "warn", 120)
            else:
                self.sound.play(self.voice())
        elif kind == "pet":
            self.stats.bump("pet")
        elif kind == "snack":
            self.stats.bump("snack")
            self.sound.play(self.voice())
        elif kind == "meow":
            self.sound.play(self.voice())

    def on_error(self, msg):
        log(f"ERROR: {msg}")
        self.tray.showMessage(APP_NAME, tr(msg), QSystemTrayIcon.Warning, 6000)
        self.mascot.say("เปิดกล้องไม่ได้ T_T\nเช็คว่ามีโปรแกรมอื่นใช้อยู่ไหม", "warn", 200)

    # ---------- ภาษา / ธีม / ตัวเลือก ----------
    def mascots(self):
        return [m for m in (self.mascot, self.mascot2) if m is not None]

    def set_lang(self, code):
        if code == LANG:
            return
        self.cfg["lang"] = code
        set_lang(code)
        self.rebuild_ui()

    def set_theme(self, key):
        if key == THEME_KEY:
            return
        self.cfg["theme"] = key
        set_theme(key)
        self.rebuild_ui()

    def rebuild_ui(self):
        """สร้างหน้าต่างควบคุม/เมนูใหม่ทั้งหมด (ใช้ตอนเปลี่ยนภาษาหรือธีม)"""
        was_visible = self.panel.isVisible()
        old_pos = self.panel.pos()
        old, self.panel = self.panel, None
        old.hide()
        old.deleteLater()
        stats_vis = was_visible and bool(getattr(old, "_wide", False))
        self.retext_actions()
        self.build_menu()
        for m in self.mascots():
            m.menu = self.menu
        self.tray.setContextMenu(self.menu)
        self.panel = ControlPanel(self)
        st = self.cur_status or "no_person"
        self.panel.set_status(st)
        self.panel.set_paused(self.act_pause.isChecked())
        self.act_head.setText(f"STATUS: {st}  ({tr(STATUS_TH[st])})")
        self.tray.setToolTip(f"{APP_NAME} - STATUS: {st} ({tr(STATUS_TH[st])})")
        if was_visible:
            self.panel.fit_to_screen()
            self.panel.move(old_pos)
            self.show_panel()
        if stats_vis:
            self.show_stats()

    def set_size(self, k, save=True):
        k = max(0.4, min(2.5, float(k)))
        if save:
            self.cfg["size"] = round(k, 2)
        for m in self.mascots():
            m.set_size(k)

    def set_opt(self, key, v):
        self.cfg[key] = bool(v)
        if key == "roam":
            self.wins.enabled = bool(v)
            for m in self.mascots():
                m.set_roam(v)
        elif key == "time_greet":
            if not v:
                for m in self.mascots():
                    m.sleepy = False

    def show_stats(self):
        """สถิติรวมอยู่ในหน้าต่างหลัก: เปิดหน้าต่างแล้วขยายให้เห็นสถิติด้านขวา"""
        self.show_panel()
        self.panel.open_stats()

    def open_web_sync(self):
        WebSyncDialog(self).exec()

    # ---------- ตัวละครคู่หู ----------
    def set_companion(self, key):
        self.cfg["companion"] = key if key in SPECIES else "none"
        self.apply_companion()

    def apply_companion(self):
        key = self.cfg["companion"]
        if key in SPECIES:
            if self.mascot2 is None:
                mm = Mascot(None, companion=True)
                mm.nag.connect(self.on_nag2)
                mm.menu = self.menu
                mm.wins = self.wins
                mm.set_roam(self.cfg["roam"])
                mm.set_size(self.cfg["size"])
                mm.sleepy = self.mascot.sleepy
                mm.snack = self.snack
                self.mascot2 = mm
                mm.show()
            self.mascot2.set_species(key)
            self.mascot2.set_status(self.cur_status or "no_person")
            self.mascot2.say(SPECIES[key]["hello"], "info", 100)
        elif self.mascot2 is not None:
            mm, self.mascot2 = self.mascot2, None
            mm.clock.stop()
            mm.hide()
            mm.deleteLater()
            self.mascot.partner = None
            if self.mascot.mode in ("flee", "chase"):
                self.mascot.mode = "idle"

    def on_nag2(self, kind):
        mm = self.mascot2
        if mm is None:
            return
        if kind in ("meow", "poke"):
            self.sound.play(VOICE_OF.get(mm.species, "meow"))
        elif kind == "pet":
            self.stats.bump("pet")
        elif kind == "snack":
            self.stats.bump("snack")
            self.sound.play(VOICE_OF.get(mm.species, "meow"))

    def pair_tick(self):
        a, b = self.mascot, self.mascot2
        if b is None or self.cur_status != "normal":
            return
        now = time.time()
        if (now >= self.next_pair and abs(a.py - b.py) < 4 and a.free_for_play() and b.free_for_play()):
            self.next_pair = now + random.uniform(35, 80)
            self.start_chase(a, b)
            return
        if (abs(a.body_cx() - b.body_cx()) < 170 and a.mode in ("idle", "sit") and b.mode in ("idle", "sit")):
            a.dir = 1 if b.px > a.px else -1                     # หันหน้าหากัน
            b.dir = -a.dir

    def start_chase(self, a, b):
        lead, chase = (a, b) if random.random() < 0.5 else (b, a)
        lead.mode, chase.mode = "flee", "chase"
        lead.partner, chase.partner = chase, lead
        for m in (lead, chase):
            m.timer = random.randint(140, 220)
        lead.say(random.choice(CHASE_LEAD), "info", 70)
        chase.say(random.choice(CHASE_FOLLOW), "info", 70)

    # ---------- ขนม ----------
    def give_snack(self):
        if self.snack is not None and self.snack.alive:
            self.snack.consume()
        mm = self.mascot
        geo = mm.screen_geo()
        kind = SNACK_OF.get(self.cfg["character"], "cookie")
        x = mm.body_cx() + random.choice((-1, 1)) * random.randint(140, 260) - Snack.SIZE / 2.0
        x = min(max(x, geo.left() + 10), geo.right() - Snack.SIZE - 10)
        sn = Snack(self, kind, x, geo.top() + 80)
        self.snack = sn
        for m in self.mascots():
            m.snack = sn
            m.seek_t = 0
        sn.show()
        if self.cur_status != "normal":
            mm.say(SNACK_WAIT, "info", 110)

    def snack_gone(self, sn):
        if self.snack is sn:
            self.snack = None
        for m in self.mascots():
            if m.snack is sn:
                m.snack = None

    def snack_released(self, sn):
        cx, cy = sn.cx(), sn.fy + Snack.SIZE / 2.0
        for m in self.mascots():
            if m.hit_test(cx, cy):
                if m.try_feed(sn):
                    sn.consume()
                return

    # ---------- ทักทายตามเวลา ----------
    def check_time(self):
        mm = self.mascot
        lt = time.localtime()
        h = lt.tm_hour
        night = h >= 23 or h < 4
        for m in self.mascots():
            m.sleepy = bool(night and self.cfg["time_greet"])
        if not self.cfg["time_greet"] or self.calibrating or self.cur_status != "normal":
            return
        if mm.mode in ("drag", "fall", "jump", "climb", "leap") or mm.bubble_text:
            return
        slot = None
        if 5 <= h < 11:
            slot = "morning"
        elif 11 <= h < 14:
            slot = "lunch"
        elif 17 <= h < 20:
            slot = "evening"
        elif night:
            slot = "night"
        if slot is None:
            return
        now = time.time()
        if slot == "night":
            if now - self.last_night < 2400:                       # เตือนดึกทุก ~40 นาที
                return
            self.last_night = now
        else:
            today = time.strftime("%Y-%m-%d", lt)
            g = dict(self.cfg["greeted"] or {})
            if g.get(slot) == today:
                return
            g[slot] = today
            self.cfg["greeted"] = g
        mm.say(random.choice(GREET[slot]), "info", 150)
        mm.start_action({"morning": "wave", "lunch": "hop"}.get(slot, "stretch"))

    # ---------- main flow ----------
    def on_result(self, r):
        if self.calibrating:
            self._last_stat_t = None
            return
        now = time.time()
        if self.override:
            if now < self.override[1]:
                self._last_stat_t = None
                return
            self.override = None
        score = 0.0
        parts = None
        if not r["valid"]:
            raw = "no_person"
            self.ema = None
            self.last_info = "ไม่พบคน / เห็นไหล่ไม่ชัด"
        else:
            sc, parts = score_posture(r["metrics"], self.baseline)
            self.ema = sc if self.ema is None else 0.3 * sc + 0.7 * self.ema
            score = self.ema
            k = SENS.get(self.cfg["sensitivity"], 1.0)
            raw = classify(score, self.machine.status, k)
            self.last_info = (f"score={score:.2f}  head={parts['head']:.2f} fwd={parts['fwd']:.2f} "
                              f"lean={parts['scale']:.2f} tilt={parts['tilt']:.2f}")
        status = self.machine.update(raw, now)
        dt = min(1.5, now - self._last_stat_t) if self._last_stat_t else 0.0
        self._last_stat_t = now
        self.stats.add(status, dt, parts, score=score if r["valid"] else None)
        self.web.level = score
        self.web.note(status, parts)
        prev_stat, self._prev_stat = self._prev_stat, status
        if status == "bad" and prev_stat != "bad":
            self.stats.bump("alerts")
        self.panel.set_score(score)
        self.apply_status(status)
        if status == "no_person" and prev_stat != "no_person":
            self.away_since = now
        elif status != "no_person" and prev_stat == "no_person" and self.away_since:
            away, self.away_since = now - self.away_since, None
            if away >= 600 and self.cfg["time_greet"]:
                self.mascot.say(tr("ยินดีต้อนรับกลับ!\nพักไป {n} นาทีเลยนะ").format(n=int(away // 60)), "info", 130)
        if self.preview_win.isVisible():
            self.preview_win.info.setText(f"STATUS: {status}    {self.last_info}")

    def apply_status(self, status, force=False, quiet=False):
        if status == self.cur_status and not force:
            return
        prev_status = self.cur_status
        self.cur_status = status
        log(f"STATUS: {status}")
        try:
            STATUS_FILE.write_text(status, encoding="utf-8")
        except Exception:
            pass
        for m in self.mascots():
            m.set_status(status)
        self.tray.setIcon(QIcon(make_icon(status, 64, self.cfg["character"])))
        self.tray.setToolTip(f"{APP_NAME} - STATUS: {status} ({tr(STATUS_TH[status])})")
        self.act_head.setText(f"STATUS: {status}  ({tr(STATUS_TH[status])})")
        self.panel.set_status(status)
        self.sync_alarm()
        if status == "normal" and prev_status in ("warning", "bad") and not quiet:
            self.sound.play("good")

    def quit(self):
        self.stats.close_session()
        self.stats.save(force=True)
        if self.snack is not None:
            self.snack.consume()
        for m in self.mascots():
            m.hide()
        self.sound.stop_alarm()
        self.tracker.stop()
        self.tracker.wait(2000)
        self.tray.hide()
        self.app.quit()


# ----------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)

    if "--make-icon" in args:                      # ใช้ตอน build เพื่อสร้าง icon.ico
        out = args[args.index("--make-icon") + 1]
        ok = make_icon("normal", 256).save(out)
        print("icon saved" if ok else "icon failed")
        return 0

    if "--selftest" in args:                       # ตรวจว่า exe โหลดโมเดล MediaPipe ได้
        try:
            import cv2
            import mediapipe as mp
            pose = mp.solutions.pose.Pose(model_complexity=1)
            pose.process(np.zeros((480, 640, 3), np.uint8))
            pose.close()
            (APP_DIR / "selftest.txt").write_text("OK", encoding="utf-8")
            print("SELFTEST OK")
            return 0
        except Exception as e:
            (APP_DIR / "selftest.txt").write_text(f"FAIL: {e}", encoding="utf-8")
            print("SELFTEST FAIL", e)
            return 1

    try:
        set_lang(Config()["lang"])
    except Exception:
        pass
    lock = QLockFile(os.path.join(tempfile.gettempdir(), f"{APP_NAME}.lock"))
    if not lock.tryLock(200):
        QMessageBox.information(None, APP_NAME, tr("โปรแกรมเปิดอยู่แล้ว (ดูไอคอนที่ system tray)"))
        return 0

    ctrl = Controller(app, use_camera="--no-camera" not in args)
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
