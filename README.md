# RSI14 + Supertrend Alert (Gate.io perpetual)

Bot alert (bukan auto-trade) untuk futures perpetual Gate.io, berjalan otomatis lewat
**GitHub Actions** setiap 15 menit â tidak perlu laptop menyala.

## Strategi

TF 15m, 1h, 4h. Hanya candle yang sudah tertutup.

**BUY**
1. Tahap 1 â Supertrend (ATR 12, faktor 3) baru berubah **hijau**, dan RSI14 saat itu di antara 50â70.
2. Tahap 2 â RSI14 menembus 70 ke atas selama Supertrend tetap hijau (tanpa batas candle).
   Setup batal kalau Supertrend berubah merah, atau RSI close turun di bawah 50 sebelum tembus 70.
3. Entry di close candle tahap 2. SL di garis Supertrend saat entry. TP di RR 1,5.

**SELL** kebalikannya (Supertrend merah, RSI 30â50, lalu tembus 30 ke bawah).

Ini adalah versi GitHub Actions dari bot yang sama yang berjalan di CMD laptop
(folder `BOT JALAN NINJAKU`) â logika persis sama, hanya cara menjalankannya yang berbeda.

## Cara kerja otomatisasi

- `.github/workflows/scan.yml` memicu `run_once.py` setiap 15 menit (dan bisa dipicu manual lewat tab **Actions > Run workflow**).
- `run_once.py` melakukan satu kali scan (bukan loop tanpa henti seperti versi CMD), kirim alert ke Telegram untuk setiap entry baru, lalu keluar.
- Riwayat sinyal yang sudah terkirim disimpan di `rsi_st_sudah_terkirim.json` dan di-commit balik ke repo setiap run, supaya tidak ada alert dobel walau repo "mati" lalu jalan lagi.
- TOKEN dan CHAT_ID Telegram **tidak** disimpan di kode â diisi lewat **Settings > Secrets and variables > Actions** sebagai `TOKEN` dan `CHAT_ID`.

## Setup sekali saja

1. Repo ini sudah di-push ke GitHub.
2. Buka **Settings > Secrets and variables > Actions > New repository secret**, tambahkan:
   - `TOKEN` â token bot Telegram dari BotFather (pakai bot Telegram baru, jangan pakai bot lama yang sudah dipakai di bot lain)
   - `CHAT_ID` â chat id Telegram tujuan alert
3. Buka tab **Actions**, aktifkan workflow bila diminta.
4. Uji coba manual: tab **Actions > RSI14 + Supertrend Alert > Run workflow**.

Setelah itu bot jalan otomatis tiap 15 menit selama repo ini ada di GitHub, tanpa laptop menyala.
