# MAN_inspection — Activity Logger

โปรแกรมบันทึกกิจกรรมการใช้งานคอมพิวเตอร์ (Windows) ของเครื่องตัวเอง แล้วเก็บลงไฟล์ Excel
พร้อม build เป็น `.exe` และอัปเดตตัวเองอัตโนมัติจาก GitHub Releases

> โปรแกรมนี้โปร่งใส: มีไอคอนอยู่ที่ system tray ให้เห็นว่ากำลังทำงาน เปิดโฟลเดอร์ log หรือสั่งปิดได้

## สถาปัตยกรรมโดยรวม

มี **2 repo** แยกหน้าที่กันชัดเจน:

| repo | การมองเห็น | หน้าที่ |
|------|-----------|--------|
| `T-TFIG/MAN_inspection` (repo นี้) | **Public** | โค้ด + แจก `.exe` ผ่าน GitHub Releases เท่านั้น ไม่มีข้อมูลพนักงานอยู่ในนี้เลย |
| `T-TFIG/MAN_inspection-data` | **Private** | ที่เก็บกลาง: policy, สถานะ/heartbeat ของแต่ละเครื่อง, ไฟล์ Excel ที่อัปโหลด |

มี **2 โปรแกรม** ใน Release:

| ไฟล์ | ใครใช้ | ทำอะไร |
|------|--------|--------|
| `MAN_inspection.exe` | พนักงานทุกเครื่อง | บันทึก log + ส่ง heartbeat/อัปโหลด log ไป data repo ตาม policy (ไม่ต้องตั้งค่าเอง) |
| `MAN_inspection_admin.exe` | admin เท่านั้น | Dashboard เห็นสถานะทุกเครื่อง + แก้ policy ทั้งหมดจากที่เดียว |

## บันทึกอะไรบ้าง

| Event | ความหมาย |
|-------|----------|
| `Window` | โปรแกรม/หน้าต่างที่ใช้ พร้อมระยะเวลา (วินาที) ที่อยู่หน้าต่างนั้น |
| `Idle` | ช่วงที่ไม่ได้แตะเมาส์/คีย์บอร์ดเกิน 60 วินาที (ปรับได้) |
| `Active` | กลับมาใช้งานหลัง idle |
| `Lock` / `Unlock` | ล็อก / ปลดล็อกหน้าจอ |
| `Logon` / `Logoff` | เข้า / ออกจาก session |
| `USB` | เสียบ / ถอด USB หรือไดรฟ์ที่ถอดได้ (พร้อม drive letter) |
| `Start` / `Stop` | โปรแกรมเริ่ม / หยุดทำงาน |

> หมายเหตุ: บันทึกแค่ "ชื่อโปรแกรม + ชื่อหน้าต่าง + เวลา" เท่านั้น ไม่ได้เก็บการพิมพ์ (keystroke) หรือภาพหน้าจอ
>
> แยกจากนี้ยังมี **heartbeat** ส่งไปที่ data repo ทุก ๆ 5 นาที (ปรับได้) มีแค่ hostname + เวอร์ชัน + เวลาล่าสุดที่ยังออนไลน์
> (ใช้แสดงในหน้า Dashboard เท่านั้น ไม่มีเนื้อหากิจกรรมอยู่ในนั้น)

## ไฟล์ Excel

เก็บ **1 ไฟล์ต่อวัน** ที่โฟลเดอร์ `logs/` ข้างตัวโปรแกรม เช่น `logs/activity_2026-10-14.xlsx`
คอลัมน์: `Date | Time | Event | Application | Detail | Duration (s)`

## รันตอนพัฒนา (Python)

```bash
pip install -r requirements.txt
python main.py
```

## Build เป็น .exe (ในเครื่อง)

```bash
build.bat
```
ได้ไฟล์ `dist\MAN_inspection.exe`

## Build + Release อัตโนมัติ (GitHub Actions)

ทุกครั้งที่ push ขึ้น branch `main`:
1. GitHub Actions build ทั้ง `MAN_inspection.exe` และ `MAN_inspection_admin.exe` บน Windows runner
2. สร้าง Release ติด tag `v<version>` (เวอร์ชันอ่านจาก `activity_logger/__init__.py`)
3. แนบทั้งสองไฟล์ไว้ใน Release

**เวลาจะปล่อยเวอร์ชันใหม่**: แก้ `__version__` ใน [activity_logger/\_\_init\_\_.py](activity_logger/__init__.py) ให้สูงขึ้น แล้ว commit + push

## การอัปเดตตัวเองของ .exe

ตอนเปิดโปรแกรม และทุก ๆ 6 ชั่วโมง `.exe` จะเช็ก Release ล่าสุดของ repo
`T-TFIG/MAN_inspection`. ถ้าเวอร์ชันบน GitHub ใหม่กว่า จะดาวน์โหลด `.exe` ตัวใหม่
มาแทนที่ตัวเองแล้วรีสตาร์ท (ไม่ต้องฝัง token เพราะ repo เป็น public)

ปรับ owner/repo ได้ที่ [activity_logger/config.py](activity_logger/config.py)

## Admin Console — dashboard + ตั้งค่าส่วนกลาง

เปิด `MAN_inspection_admin.exe` จะเจอ 2 แท็บ:

### แท็บ Dashboard

ตารางแสดง**ทุกเครื่องที่เคยรันโปรแกรม**: Hostname, Machine ID, เวอร์ชัน, สถานะ (🟢 Online / ⚪ Offline), heartbeat ล่าสุดเมื่อไหร่
รีเฟรชอัตโนมัติทุก 60 วินาที หรือกด **Refresh now** เอง

ถือว่า "Online" ถ้า heartbeat เข้ามาใน 15 นาทีล่าสุด (heartbeat ปกติส่งทุก 5 นาที เผื่อพลาดได้ 2-3 ครั้ง)

เลือกเครื่องในตาราง → กด **View logs for selected…** → เห็นรายชื่อไฟล์ Excel ที่เครื่องนั้นอัปโหลดไว้ →
**Download & open** เปิดดูได้เลยโดยไม่ต้องผ่านอีเมลเลย

#### ขอ log เดี๋ยวนี้ (เช่นตอน debug ไม่ต้องรอตามรอบ schedule)

เลือกเครื่องในตาราง → กด **Request log now** → เครื่องนั้น (ถ้าออนไลน์อยู่) จะอัปโหลด log ของ**วันนี้**มาให้
ภายใน **~15-20 วินาที** แทนที่จะต้องรอรอบ schedule เป็นวัน/สัปดาห์/เดือน

ทำงานยังไง: เครื่องพนักงานเช็ก "มีคำขอไหม" ทุก 15 วินาที ด้วย conditional request (ETag) ของ GitHub
ซึ่ง**ไม่กินโควต้า rate limit เลยถ้าไม่มีคำขอใหม่** (ทดสอบยืนยันแล้ว) จึง poll ถี่แบบนี้ได้อย่างปลอดภัย
แม้มีพนักงานหลายเครื่องใช้ token เดียวกัน

> หมายเหตุ: การอัปโหลด/ส่งอีเมลที่เกิดจาก schedule อัตโนมัติหรือจาก "Request log now" ของ admin
> **ไม่มี popup เด้งที่เครื่องพนักงานเลย** (ทำงานเงียบๆ เบื้องหลัง) จะมี popup ให้เห็นแค่ตอนพนักงาน
> **กดเองจาก tray menu** เท่านั้น (เช่น "Upload today's log now") ดูสถานะ/ประวัติที่แท้จริงได้จาก Admin Console Dashboard แทน

### แท็บ Policy — ปรับได้ครบ จากที่เดียว ส่งผลทุกเครื่องพร้อมกัน

| หมวด | ปรับอะไรได้ |
|------|------------|
| **Monitoring** | เปิด/ปิดการเก็บ: หน้าต่างที่ใช้งาน, idle/active, USB, lock/logon แยกแต่ละอย่าง + ปรับ poll interval, idle threshold, heartbeat interval |
| **Central upload** | เปิด/ปิดการอัปโหลด log ไป data repo (รายวัน/สัปดาห์/เดือน + เวลา) — **แนะนำให้เปิดเป็นค่าเริ่มต้น** เพราะไม่ต้องใช้ความลับอะไรเลย |
| **Email** | ส่งต่อเป็นอีเมลด้วย (เสริม ไม่บังคับ) — ผู้รับ + รอบส่ง |

กด **Load current** เพื่อดึงค่าปัจจุบันมาดู, แก้แล้วกด **Save & Publish** → เครื่องพนักงานทุกเครื่องจะดึง
policy ใหม่ไปใช้ภายใน **heartbeat interval ถัดไป** (ปกติไม่เกิน 5-10 นาที เร็วกว่าของเดิมที่ใช้ repo public มาก)

### ตั้งค่าครั้งแรก (ทำครั้งเดียว)

1. สร้าง **GitHub fine-grained token** สำหรับตัวเอง:
   GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new token
   → **Resource owner**: `T-TFIG` → **Repository access**: Only select repositories → เลือก **`MAN_inspection-data`** (repo private ตัวใหม่ ไม่ใช่ repo โค้ด)
   → Permissions → **Contents: Read and write**
2. เปิด `MAN_inspection_admin.exe` → วาง token ลงช่อง **Access token** (ใช้ปุ่ม **Paste** ไม่ใช่ Ctrl+V ถ้าเครื่อง Windows ตั้ง locale ไม่ใช่อังกฤษ เพื่อกัน token เพี้ยน) → โปรแกรมโหลด dashboard + policy ให้อัตโนมัติ

token นี้เก็บไว้ในเครื่อง admin เท่านั้น (`%ProgramData%\MAN_inspection\admin_settings.json`) ไม่ขึ้น repo

## ส่ง log ไปเครื่อง admin — 2 ช่องทาง (เลือกได้จาก Policy)

### 1) Central upload (แนะนำ, ค่าเริ่มต้นที่ดีกว่า Gmail ตรงๆ)

เครื่องพนักงานอัปโหลดไฟล์ Excel ไปที่ **private data repo** โดยตรง ตามรอบที่ตั้งใน policy (รายวัน/สัปดาห์/เดือน)
ไม่ต้องมี Gmail/App Password อยู่บนเครื่องพนักงานเลยสักตัว — admin ดูได้ทันทีจาก Dashboard → View logs

ทำไมดีกว่า Gmail ส่งตรงจากทุกเครื่อง:
- ไม่มี credential ที่ส่งเมลได้อยู่บนเครื่องพนักงาน (ลดความเสียหายถ้าเครื่องโดนแฮ็ก)
- เห็นสถานะ/ประวัติการส่งจาก Dashboard แทนที่จะต้องไปไล่เปิดอีเมล
- พนักงานกดส่งเองได้ทันทีด้วย: คลิกขวาไอคอน tray → **Upload today's log now**

> ⚠️ **ข้อจำกัดที่ควรรู้**: client token (สำหรับอัปโหลด) ฝังเหมือนกันทุกเครื่องที่ build จาก CI เดียวกัน
> ถ้ามีคนแกะ `.exe` ออกมาเจอ token จะ**อ่าน/เขียนไฟล์ของเครื่องอื่นในที่เก็บกลางได้ด้วย** (ขอบเขตสิทธิ์เป็นระดับ repo ไม่ใช่ระดับไฟล์)
> แต่จำกัดอยู่แค่ repo ข้อมูลส่วนตัวนี้ repo เดียว ไม่กระทบโค้ด/Release สาธารณะ และ**ตั้งค่าได้แค่คนที่มี token นี้เท่านั้น**
> ถ้าสงสัยว่า token รั่ว ให้ revoke token เดิมใน GitHub Settings แล้วสร้างใหม่ใส่เป็น Actions secret แล้ว build ปล่อยเวอร์ชันใหม่

### 2) Email (Gmail) — เสริม ไม่บังคับ, admin ตั้งค่าเอง

ยังใช้ได้เหมือนเดิม ถ้า admin อยากได้สำเนาในอีเมลด้วย ต้องตั้ง secret ที่เครื่องที่จะ "ส่ง" (ปกติแค่เครื่อง admin เครื่องเดียวพอ ไม่ต้องทุกเครื่องพนักงาน):

1. เปิด 2-Step Verification ในบัญชี Gmail ผู้ส่ง แล้วสร้าง **App Password**
   (Google Account → Security → App passwords) — SMTP ใช้รหัสปกติไม่ได้
2. คัดลอก [email_config.example.json](email_config.example.json) เป็น `email_config.json` กรอก `sender_email` + `app_password`
3. วางไฟล์ไว้ที่ `C:\ProgramData\MAN_inspection\email_config.json`

> ⚠️ **ห้าม commit `email_config.json`** (repo public — App Password จะโดนเห็น) — ถูก `.gitignore` ไว้แล้ว

## ตั้งค่า CI ให้ build ที่มี central upload (ทำครั้งเดียว โดยเจ้าของ repo โค้ด)

ตัวไฟล์ `.exe` ของพนักงานต้องมี "client token" ฝังมาตอน build เพื่ออัปโหลด/heartbeat ได้:

1. สร้าง **GitHub fine-grained token อีกตัว** (แยกจาก token ส่วนตัวของ admin):
   Resource owner `T-TFIG` → Repository access: only `MAN_inspection-data` → Permissions: **Contents: Read and write**
2. ไปที่ repo โค้ด `T-TFIG/MAN_inspection` → **Settings → Secrets and variables → Actions → New repository secret**
   ตั้งชื่อ `CLIENT_DATA_TOKEN` ใส่ค่า token จากข้อ 1
3. push โค้ด (หรือกด Run workflow) ตามปกติ — CI จะฝัง token นี้ลงใน `.exe` ของพนักงานให้อัตโนมัติ
   (ถ้าไม่ตั้ง secret นี้ไว้ ตัว `.exe` จะ build ได้ปกติแต่ฟีเจอร์ central upload/dashboard จะปิดเงียบๆ เหมือนตอนยังไม่มี secret)

## ข้อควรรู้ด้านกฎหมาย/ความเป็นส่วนตัว (สำคัญสำหรับ admin)

เครื่องมือนี้เก็บ **ชื่อโปรแกรม + ชื่อหน้าต่าง + เวลา + เหตุการณ์ (idle/lock/USB)** ของพนักงาน
และส่งให้ admin — ถือเป็นการ **monitoring พนักงาน** ก่อนนำไปใช้จริง admin ควร:

- **แจ้งพนักงานให้ทราบ** และขอความยินยอมตามที่กฎหมายกำหนด (เช่น PDPA ของไทย / GDPR)
- ใช้กับ **เครื่องของบริษัท** และเพื่อวัตถุประสงค์ที่ชอบด้วยกฎหมายเท่านั้น
- ปรึกษาฝ่าย HR/กฎหมายของบริษัทเรื่องนโยบายการเก็บและใช้ข้อมูล

โปรแกรมออกแบบให้ **โปร่งใส**: มีไอคอน tray ให้เห็นว่ากำลังทำงาน และไม่เก็บการพิมพ์คีย์บอร์ดหรือภาพหน้าจอ

## ให้โปรแกรมรันตอนเปิดเครื่อง

### วิธีแนะนำสำหรับ deploy หลายเครื่อง: Scheduled Task (สคริปต์เดียว)

[deploy/install.ps1](deploy/install.ps1) ทำให้ครบในคำสั่งเดียว: โหลด `.exe` เวอร์ชันล่าสุดจาก Release
มาติดตั้งที่ `C:\ProgramData\MAN_inspection\app\` แล้วตั้ง **Scheduled Task** ให้รันตอน login ของ
**ผู้ใช้คนไหนก็ได้** (ไม่ต้องสิทธิ์ admin ตอนรัน, ไม่ติด time limit 72 ชม.แบบ default, restart ให้เองถ้า crash)

รันบนเครื่องพนักงาน (เปิด PowerShell **แบบ Run as Administrator**):

```powershell
irm https://raw.githubusercontent.com/T-TFIG/MAN_inspection/main/deploy/install.ps1 | iex
```

หรือถ้ามี `.exe` อยู่แล้ว ไม่อยากให้โหลดใหม่:
```powershell
.\install.ps1 -ExePath C:\Downloads\MAN_inspection.exe
```

ดัน script นี้ผ่าน GPO startup script / Intune / PDQ Deploy ก็ได้เหมือนกัน (เป็น PowerShell ธรรมดา)

ถอนการติดตั้ง: [deploy/uninstall.ps1](deploy/uninstall.ps1) (ลบ Scheduled Task + ตัว `.exe`, ไม่ลบ log/config)

### วิธีง่ายสำหรับเครื่องเดียว: Startup folder

กด `Win + R` → พิมพ์ `shell:startup` → วาง shortcut ของ `MAN_inspection.exe` ไว้ในโฟลเดอร์นั้น

## เช็กว่าแต่ละเครื่อง up-to-date รึยัง

### จาก Admin Console (เห็นทุกเครื่องพร้อมกัน)

แท็บ **Dashboard** จะเทียบเวอร์ชันของทุกเครื่องกับ Release ล่าสุดบน GitHub ให้อัตโนมัติทุกครั้งที่รีเฟรช —
เครื่องที่ล้าหลังจะโชว์เป็น**สีส้ม** พร้อมข้อความ เช่น `0.2.0 (v0.3.2 available)` ในคอลัมน์ Version
(เครื่องที่เป็นเวอร์ชันล่าสุดอยู่แล้วจะโชว์ปกติ ไม่มีข้อความเตือน)

### จากเครื่องพนักงานเอง

คลิกขวาไอคอน tray → **Check for updates** จะเด้ง notification บอกทันที:
- `Up to date (v0.3.2).` — ล่าสุดอยู่แล้ว
- `Updating to v0.3.3 — the app will restart shortly.` — เจอเวอร์ชันใหม่ กำลังอัปเดตให้ (จะปิด-เปิดใหม่เอง)

> ปกติ `.exe` เช็กอัปเดตให้เองอยู่แล้วตอนเปิดโปรแกรม + ทุก 6 ชั่วโมง ปุ่มนี้มีไว้เผื่ออยากเช็กทันทีไม่ต้องรอ
