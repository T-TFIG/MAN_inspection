# MAN_inspection — Activity Logger

โปรแกรมบันทึกกิจกรรมการใช้งานคอมพิวเตอร์ (Windows) ของเครื่องตัวเอง แล้วเก็บลงไฟล์ Excel
พร้อม build เป็น `.exe` และอัปเดตตัวเองอัตโนมัติจาก GitHub Releases

> โปรแกรมนี้โปร่งใส: มีไอคอนอยู่ที่ system tray ให้เห็นว่ากำลังทำงาน เปิดโฟลเดอร์ log หรือสั่งปิดได้

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

## ให้โปรแกรมรันตอนเปิดเครื่อง (ไม่บังคับ)

กด `Win + R` → พิมพ์ `shell:startup` → วาง shortcut ของ `MAN_inspection.exe` ไว้ในโฟลเดอร์นั้น
