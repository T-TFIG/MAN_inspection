# MAN_inspection — Activity Logger

โปรแกรมบันทึกกิจกรรมการใช้งานคอมพิวเตอร์ (Windows) ของเครื่องตัวเอง แล้วเก็บลงไฟล์ Excel
พร้อม build เป็น `.exe` และอัปเดตตัวเองอัตโนมัติจาก GitHub Releases

> โปรแกรมนี้โปร่งใส: มีไอคอนอยู่ที่ system tray ให้เห็นว่ากำลังทำงาน เปิดโฟลเดอร์ log หรือสั่งปิดได้

## มี 2 โปรแกรมใน Release

| ไฟล์ | ใครใช้ | ทำอะไร |
|------|--------|--------|
| `MAN_inspection.exe` | พนักงานทุกเครื่อง | บันทึก log + ส่งอีเมลตาม policy (ไม่ต้องตั้งค่าเอง) |
| `MAN_inspection_admin.exe` | admin เท่านั้น | หน้าต่างแก้ policy ส่วนกลาง (ผู้รับ/รอบส่ง) แล้ว publish ขึ้น GitHub |

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
1. GitHub Actions build `.exe` บน Windows runner
2. สร้าง Release ติด tag `v<version>` (เวอร์ชันอ่านจาก `activity_logger/__init__.py`)
3. แนบ `MAN_inspection.exe` ไว้ใน Release

**เวลาจะปล่อยเวอร์ชันใหม่**: แก้ `__version__` ใน [activity_logger/\_\_init\_\_.py](activity_logger/__init__.py) ให้สูงขึ้น แล้ว commit + push

## การอัปเดตตัวเองของ .exe

ตอนเปิดโปรแกรม และทุก ๆ 6 ชั่วโมง `.exe` จะเช็ก Release ล่าสุดของ repo
`T-TFIG/MAN_inspection`. ถ้าเวอร์ชันบน GitHub ใหม่กว่า จะดาวน์โหลด `.exe` ตัวใหม่
มาแทนที่ตัวเองแล้วรีสตาร์ท (ไม่ต้องฝัง token เพราะ repo เป็น public)

ปรับ owner/repo ได้ที่ [activity_logger/config.py](activity_logger/config.py)

## ส่ง log ทางอีเมล (Gmail) — admin คุมจากที่เดียว

**พนักงานทั่วไปไม่ต้องตั้งค่าอะไรเลย** การตั้งค่าแบ่งเป็น 2 ชั้น:

| ชั้น | ไฟล์ | เก็บที่ไหน | เปลี่ยนยังไง | ความลับ |
|------|------|-----------|--------------|---------|
| **Policy** (เปิด/ปิด, ผู้รับ, รอบส่ง, เวลา) | [remote_policy.json](remote_policy.json) | ใน repo (public) | admin แก้ไฟล์เดียวบน GitHub → ทุกเครื่องตามอัตโนมัติ | ไม่มี |
| **Secret** (Gmail ผู้ส่ง + App Password) | `email_config.json` | local บนเครื่อง | วางครั้งเดียวตอน deploy | มี — ห้ามขึ้น repo |

แต่ละเครื่องจะดึง policy จาก `remote_policy.json` ใน repo **ทุก 30 นาที** → admin เปลี่ยนที่เดียว เครื่องอื่นเปลี่ยนตาม
มีแค่คนที่ push เข้า repo ได้ (= admin) เท่านั้นที่แก้ policy ได้

ส่งได้ 2 ทาง:
- **กดส่งเอง**: คลิกขวาไอคอน tray → `Send today's log now` (ส่งไฟล์ของวันนั้น)
- **ส่งอัตโนมัติ**: ตาม `schedule` ใน policy (รายวัน/สัปดาห์/เดือน)

### A) ตั้ง secret ครั้งเดียวต่อเครื่อง (admin, ตอน deploy)

1. เปิด 2-Step Verification ในบัญชี Gmail ผู้ส่ง แล้วสร้าง **App Password**
   (Google Account → Security → App passwords) — SMTP ใช้รหัสปกติไม่ได้
2. คัดลอก [email_config.example.json](email_config.example.json) เป็น `email_config.json` กรอก `sender_email` + `app_password`
3. วางไฟล์ไว้ที่ (โปรแกรมหาตามลำดับนี้):
   1. `C:\ProgramData\MAN_inspection\email_config.json` ← แนะนำ (deploy ทั้งเครื่องผ่าน GPO/สคริปต์ แล้วตั้ง ACL ให้พนักงานแก้ไม่ได้)
   2. โฟลเดอร์เดียวกับ `MAN_inspection.exe`

> ⚠️ **ห้าม commit `email_config.json`** (repo public — App Password จะโดนเห็น) — ถูก `.gitignore` ไว้แล้ว

### B) เปลี่ยน policy แบบศูนย์กลาง

#### วิธีแนะนำ: ใช้ Admin Console (`MAN_inspection_admin.exe`)

โปรแกรมหน้าต่างสำหรับ admin โดยเฉพาะ เปิดแล้วแก้ค่าได้เลย กด **Save & Publish** → มันจะอัปเดต
`remote_policy.json` บน GitHub ให้อัตโนมัติ (ไม่ต้องแก้ JSON เอง ไม่ต้องมี git ในเครื่อง)

ตั้งค่าครั้งแรก:
1. สร้าง **GitHub token** ที่มีสิทธิ์เขียน repo นี้:
   GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new token
   → เลือก repository `T-TFIG/MAN_inspection` → Permissions → **Contents: Read and write**
2. เปิด `MAN_inspection_admin.exe` → วาง token ลงช่อง **Access token** → กด **Load current**
   (token ถูกเก็บไว้ในเครื่อง admin เท่านั้น ที่ `admin_settings.json` — ไม่ขึ้น repo)
3. ปรับค่า (เปิด/ปิด, ผู้รับ, schedule รายวัน/สัปดาห์/เดือน, เวลา) → กด **Save & Publish**

เครื่องพนักงานทุกเครื่องจะดึง policy ใหม่ไปใช้ภายใน ~30 นาที

#### ทางเลือก: แก้ไฟล์เองบน GitHub

แก้ [remote_policy.json](remote_policy.json) บน GitHub (กด edit บนเว็บ แล้ว Commit — ไม่ต้องใช้ git ในเครื่องก็ได้):

| key | ความหมาย |
|-----|----------|
| `enabled` | `true` เพื่อเริ่มส่งอีเมลทุกเครื่อง |
| `recipient_email` | อีเมลผู้รับ log (เปลี่ยนที่นี่ = ทุกเครื่องส่งไปที่ใหม่) |
| `schedule` | `"daily"` / `"weekly"` / `"monthly"` |
| `send_time` | เวลาส่งแบบ 24 ชม. เช่น `"18:00"` |
| `day_of_week` | ใช้กับ weekly: `mon`..`sun` |
| `day_of_month` | ใช้กับ monthly: `1`..`28` |

- **daily** → แนบไฟล์ของวันนั้น
- **weekly** → แนบไฟล์ของสัปดาห์นั้น (จันทร์ถึงวันที่ส่ง) ส่งในวัน `day_of_week`
- **monthly** → แนบไฟล์ทั้งเดือน (วันที่ 1 ถึงวันที่ส่ง) ส่งในวันที่ `day_of_month`

> หมายเหตุ 1: แต่ละเครื่องส่ง log **ของตัวเอง** — ผู้รับจะได้อีเมลจากหลายเครื่อง (ดูผู้ส่ง/ชื่อไฟล์แยกได้)
> หมายเหตุ 2: `recipient_email` ที่ใส่ใน `remote_policy.json` จะมองเห็นได้ใน repo public
> ถ้าไม่อยากให้เห็น ปล่อย `recipient_email` ใน policy ว่างไว้ แล้วไปใส่ใน `email_config.json` (local) ของแต่ละเครื่องแทน
> โปรแกรมจะใช้ค่าจาก policy ก่อน ถ้าว่างจึงใช้ค่า local

## ข้อควรรู้ด้านกฎหมาย/ความเป็นส่วนตัว (สำคัญสำหรับ admin)

เครื่องมือนี้เก็บ **ชื่อโปรแกรม + ชื่อหน้าต่าง + เวลา + เหตุการณ์ (idle/lock/USB)** ของพนักงาน
และส่งให้ admin — ถือเป็นการ **monitoring พนักงาน** ก่อนนำไปใช้จริง admin ควร:

- **แจ้งพนักงานให้ทราบ** และขอความยินยอมตามที่กฎหมายกำหนด (เช่น PDPA ของไทย / GDPR)
- ใช้กับ **เครื่องของบริษัท** และเพื่อวัตถุประสงค์ที่ชอบด้วยกฎหมายเท่านั้น
- ปรึกษาฝ่าย HR/กฎหมายของบริษัทเรื่องนโยบายการเก็บและใช้ข้อมูล

โปรแกรมออกแบบให้ **โปร่งใส**: มีไอคอน tray ให้เห็นว่ากำลังทำงาน และไม่เก็บการพิมพ์คีย์บอร์ดหรือภาพหน้าจอ

## ให้โปรแกรมรันตอนเปิดเครื่อง (ไม่บังคับ)

กด `Win + R` → พิมพ์ `shell:startup` → วาง shortcut ของ `MAN_inspection.exe` ไว้ในโฟลเดอร์นั้น
