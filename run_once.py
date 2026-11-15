# -*- coding: utf-8 -*-
"""
ALERT RSI14 + SUPERTREND (ATR 12, faktor 3) -> Telegram   (Gate.io perpetual USDT, GitHub Actions)
Hanya alert, tidak membuka posisi. TF 15m, 1h, 4h. Hanya candle yang sudah tertutup.
Versi ini sekali-jalan (satu scan per eksekusi), dipicu terjadwal oleh GitHub Actions
(lihat .github/workflows/scan.yml). TOKEN dan CHAT_ID diambil dari GitHub Secrets
(environment variable), bukan dari config.txt seperti versi CMD di laptop.

BUY:
  Tahap 1 : candle close di mana Supertrend baru berubah HIJAU, dan RSI14 candle itu di atas 50 dan di bawah 70.
  Tahap 2 : RSI14 menembus 70 ke atas (candle sebelumnya <= 70, candle ini > 70) selama Supertrend hijau terus.
            Tanpa batas candle. Setup BATAL kalau Supertrend berubah merah ATAU RSI close < 50 sebelum tembus 70.
  Entry   : close candle tahap 2 (alert dikirim saat itu). Satu entry per setup.
  SL      : garis Supertrend candle entry. TP: RR 1,5.
SELL = kebalikannya (Supertrend merah, RSI antara 30 dan 50, RSI menembus 30 ke bawah; batal kalau Supertrend
  hijau atau RSI > 50; SL di garis Supertrend atas).

Riwayat sinyal yang sudah terkirim (anti dobel) disimpan di rsi_st_sudah_terkirim.json dan
di-commit balik ke repo oleh workflow setiap kali jalan, supaya tidak hilang antar run.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
SEEN = os.path.join(BASE, "rsi_st_sudah_terkirim.json")

API = "https://api.gateio.ws/api/v4/futures/usdt"
TF_INTERVAL = {"15": "15m", "60": "1h", "240": "4h"}
DETIK_TF = {"15m": 900, "1h": 3600, "4h": 14400}
WIB = timezone(timedelta(hours=7))

JUMLAH_KOIN = 123
JUMLAH_CANDLE = 600       # candle terakhir yang diambil tiap koin/TF (Supertrend dan RSI sudah stabil)
MIN_INDEX = 120
ST_ATR, ST_FAKTOR = 12, 3.0
RSI_LEN = 14
RR = 1.5
SINYAL_BARU = 2           # entry hanya dianggap baru bila terjadi di 2 candle terakhir
SIMPAN_HARI = 7
WORKERS = 5

BUANG_BAWAAN = {
    "BMNR_USDT", "MSTR_USDT", "COIN_USDT", "TSLA_USDT", "NVDA_USDT", "AAPL_USDT",
    "HOOD_USDT", "CRCL_USDT", "SBET_USDT",
    "ANTHROPIC_USDT", "OPENAI_USDT", "SPACEX_USDT", "SPCX_USDT",
    "STRIPE_USDT", "DATABRICKS_USDT", "PERPLEXITY_USDT",
}
KATA_NON_KRIPTO = ("stock", "equit", "metal", "index", "indice", "forex", "fx",
                   "commodit", "tradfi", "etf", "gold", "silver")

sesi = requests.Session()
sesi.headers["User-Agent"] = "Mozilla/5.0"


def log(msg):
    print(f"[{datetime.now():%d-%b %H:%M:%S}] {msg}", flush=True)


def jam_wib(ts):
    return datetime.fromtimestamp(ts, WIB).strftime("%d-%m %H:%M")


# ------------------------------------------------------------------ Telegram
def api(token, method, **data):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=20)
    return r.json()


def kirim(token, chat_id, teks):
    for _ in range(3):
        try:
            j = api(token, "sendMessage", chat_id=chat_id, text=teks[:4000], disable_web_page_preview="true")
            if j.get("ok"):
                return True
            log(f"Telegram gagal: {j.get('description')}")
        except Exception as e:
            log(f"Telegram error: {type(e).__name__}")
        time.sleep(3)
    return False


# ------------------------------------------------------------------ data Gate.io
def kontrak_non_kripto():
    try:
        r = sesi.get(f"{API}/contracts", timeout=20)
        r.raise_for_status()
        return {k["name"] for k in r.json()
                if str(k.get("contract_type") or "").lower()
                and any(w in str(k.get("contract_type")).lower() for w in KATA_NON_KRIPTO)}
    except Exception:
        return set()


def daftar_koin(jumlah=JUMLAH_KOIN):
    r = sesi.get(f"{API}/tickers", timeout=20)
    r.raise_for_status()
    data = [d for d in r.json() if d.get("contract", "").endswith("_USDT")]
    buang = BUANG_BAWAAN | kontrak_non_kripto()
    data = [d for d in data if d["contract"] not in buang]
    data.sort(key=lambda d: float(d.get("volume_24h_quote") or 0), reverse=True)
    return [d["contract"] for d in data[:jumlah]]


def ambil_candle(kontrak, tf):
    """Return (candle_tertutup, harga_terakhir). Candle = (t, o, h, l, c), urut lama -> baru."""
    interval = TF_INTERVAL[tf]
    step = DETIK_TF[interval]
    k = None
    for p in range(4):
        try:
            r = sesi.get(f"{API}/candlesticks",
                         params={"contract": kontrak, "interval": interval, "limit": JUMLAH_CANDLE}, timeout=20)
            if r.status_code == 429:
                time.sleep(2 + 2 * p)
                continue
            r.raise_for_status()
            k = r.json()
            break
        except Exception:
            time.sleep(1 + p)
    if not k:
        raise RuntimeError("tidak ada data")
    rows = sorted(((int(float(x["t"])), float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"])) for x in k))
    px = rows[-1][4]
    now = time.time()
    rows = [c for c in rows if c[0] + step <= now]
    return rows, px


# ------------------------------------------------------------------ indikator (sama seperti TradingView)
def atr_series(H, L, C, n):
    m = len(C)
    tr = [H[0] - L[0]] + [max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1])) for i in range(1, m)]
    out = [None] * m
    if m < n:
        return out
    out[n - 1] = sum(tr[:n]) / n
    for i in range(n, m):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def supertrend(H, L, C, period, factor):
    n = len(C)
    atr = atr_series(H, L, C, period)
    trend, line, upper, lower = [0] * n, [None] * n, [None] * n, [None] * n
    for i in range(n):
        if atr[i] is None:
            continue
        hl2 = (H[i] + L[i]) / 2
        ub, lb = hl2 + factor * atr[i], hl2 - factor * atr[i]
        if i > 0 and upper[i - 1] is not None:
            pu, pl, pc = upper[i - 1], lower[i - 1], C[i - 1]
            lb = lb if (lb > pl or pc < pl) else pl
            ub = ub if (ub < pu or pc > pu) else pu
            if trend[i - 1] == -1:
                trend[i] = 1 if C[i] > ub else -1
            else:
                trend[i] = -1 if C[i] < lb else 1
        else:
            trend[i] = -1
        upper[i], lower[i] = ub, lb
        line[i] = lb if trend[i] == 1 else ub
    return trend, line


def rsi_series(values, n=14):
    m = len(values)
    out = [None] * m
    if m <= n:
        return out
    g = [max(values[i] - values[i - 1], 0.0) for i in range(1, m)]
    l = [max(values[i - 1] - values[i], 0.0) for i in range(1, m)]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, m):
        ag = (ag * (n - 1) + g[i - 1]) / n
        al = (al * (n - 1) + l[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


# ------------------------------------------------------------------ logika setup (sama dengan versi CMD)
def cari_sinyal(data, tf, baru=SINYAL_BARU):
    """Entry (tahap 2 selesai) yang terjadi di `baru` candle terakhir. Return list dict."""
    n = len(data)
    if n < MIN_INDEX + 20:
        return []
    T = [c[0] for c in data]
    H = [c[2] for c in data]
    L = [c[3] for c in data]
    C = [c[4] for c in data]
    trend, line = supertrend(H, L, C, ST_ATR, ST_FAKTOR)
    rsi = rsi_series(C, RSI_LEN)
    step = DETIK_TF[TF_INTERVAL[tf]]
    out = []
    for arah in ("BUY", "SELL"):
        buy = arah == "BUY"
        ok = 1 if buy else -1
        i = MIN_INDEX
        while i < n - 1:
            if not (trend[i] == ok and trend[i - 1] == -ok and rsi[i] is not None):
                i += 1
                continue
            if (buy and not (50 < rsi[i] < 70)) or ((not buy) and not (30 < rsi[i] < 50)):
                i += 1
                continue
            j, e = i + 1, None
            while j < n:
                if trend[j] != ok:
                    break
                if (buy and rsi[j] < 50) or ((not buy) and rsi[j] > 50):
                    break
                if (buy and rsi[j - 1] <= 70 < rsi[j]) or ((not buy) and rsi[j - 1] >= 30 > rsi[j]):
                    e = j
                    break
                j += 1
            if e is None:
                i = max(j, i + 1)
                continue
            if e >= n - baru:
                px, sl = C[e], line[e]
                risk = (px - sl) if buy else (sl - px)
                if risk > 0:
                    tp = px + RR * risk if buy else px - RR * risk
                    # sudah kena SL/TP setelah entry? maka basi, jangan dikirim
                    basi = any((L[k] <= sl or H[k] >= tp) if buy else (H[k] >= sl or L[k] <= tp)
                               for k in range(e + 1, n))
                    if not basi:
                        out.append({
                            "arah": arah, "t_flip": T[i], "rsi_flip": rsi[i], "t_entry_tutup": T[e] + step,
                            "rsi_entry": rsi[e], "umur": n - 1 - e, "entry": px, "sl": sl, "tp": tp,
                            "risk_pct": risk / px * 100, "tunggu": e - i,
                        })
            i = e + 1
    return out


def tv_link(label):
    """Link chart TradingView (gratis) untuk perpetual Gate.io."""
    return "https://www.tradingview.com/chart/?symbol=" + quote(f"GATEIO:{label}USDT.P", safe=":")


def fmt(x):
    return "n/a" if x is None else f"{x:.6g}"


def pesan(tf_nama, label, r, px):
    buy = r["arah"] == "BUY"
    ikon = "ð¢" if buy else "ð´"
    warna = "HIJAU" if buy else "MERAH"
    batas = "70" if buy else "30"
    arah_rsi = "naik" if buy else "turun"
    out = [f"{ikon} {r['arah']} | RSI + SUPERTREND | {label} {tf_nama}"]
    out.append(f"1) Supertrend (12,3) jadi {warna}: candle {jam_wib(r['t_flip'])} WIB, RSI saat itu {r['rsi_flip']:.1f}")
    out.append(f"2) RSI menembus {batas} ({arah_rsi}): RSI {r['rsi_entry']:.1f}, Supertrend {warna.lower()} tidak putus ({r['tunggu']} candle sejak tahap 1)")
    out.append(f"ENTRY (tutup candle): {jam_wib(r['t_entry_tutup'])} WIB ({r['umur']} candle lalu), harga {fmt(r['entry'])}")
    out.append(f"Stop {fmt(r['sl'])} (garis Supertrend, risiko {r['risk_pct']:.2f}%)")
    out.append(f"Target {fmt(r['tp'])} (RR {RR:g})")
    if r["risk_pct"] > 5:
        out.append("â ï¸ Stop lebar (>5%): kecilkan ukuran posisi/leverage.")
    jarak = (px - r["entry"]) / r["entry"] * 100
    out.append(f"Harga sekarang {fmt(px)} ({jarak:+.2f}% dari entry)")
    out.append(f"Chart: {tv_link(label)}")
    return "\n".join(out)


# ------------------------------------------------------------------ riwayat sinyal (anti dobel)
def muat_seen():
    try:
        d = json.load(open(SEEN, encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def simpan_seen(d):
    batas = time.time() - SIMPAN_HARI * 86400
    d = {k: v for k, v in d.items() if v >= batas}
    json.dump(d, open(SEEN, "w", encoding="utf-8"))
    return d


def scan_satu(args):
    kontrak, tf = args
    try:
        data, px = ambil_candle(kontrak, tf)
        return [(TF_INTERVAL[tf], kontrak[:-5], r, px) for r in cari_sinyal(data, tf)]
    except Exception:
        return None


def scan(tfs, koin):
    tugas = [(k, tf) for k in koin for tf in tfs]
    hasil, gagal = [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for h in ex.map(scan_satu, tugas):
            if h is None:
                gagal += 1
            else:
                hasil.extend(h)
    return hasil, gagal, len(tugas)


def jalan_scan(tfs, koin, token, chat_id, seen):
    t0 = time.time()
    hasil, gagal, total = scan(tfs, koin)
    baru = []
    for T, label, r, px in hasil:
        key = f"{label}|{T}|{r['arah']}|{r['t_flip']}"          # satu alert per setup
        if key not in seen:
            seen[key] = time.time()
            baru.append((T, label, r, px))
    baru.sort(key=lambda x: (x[2]["umur"], x[0]))
    log(f"scan {','.join(TF_INTERVAL[t] for t in tfs)}: {len(hasil)} entry aktif, {len(baru)} baru, "
        f"{gagal}/{total} data gagal ({time.time() - t0:.0f} detik)")
    for T, label, r, px in baru:
        kirim(token, chat_id, pesan(T, label, r, px))
        time.sleep(0.4)
    return baru, seen


# ------------------------------------------------------------------ main (sekali jalan, dipicu GitHub Actions)
def main():
    token = os.environ.get("TOKEN", "")
    chat_id = os.environ.get("CHAT_ID", "")
    if not token or not chat_id:
        print("TOKEN / CHAT_ID belum diisi di GitHub Secrets (Settings > Secrets and variables > Actions).")
        sys.exit(2)

    seen = muat_seen()
    pertama = seen is None
    seen = seen or {}

    log("Mengambil daftar koin dari Gate.io ...")
    koin = daftar_koin()
    log(f"{len(koin)} koin dimuat")

    baru, seen = jalan_scan(["15", "60", "240"], koin, token, chat_id, seen)
    seen = simpan_seen(seen)

    if pertama:
        log(f"Run pertama: {len(baru)} entry aktif dikirim (riwayat baru dibuat).")


if __name__ == "__main__":
    main()
