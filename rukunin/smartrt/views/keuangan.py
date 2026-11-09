"""Keuangan RT: kas, tagihan iuran, bukti pembayaran, verifikasi, rekap saldo."""
import csv
import io
import re
import sqlite3
from datetime import date

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, session, url_for

from ..core import (STATUS, Invalid, bulan_label, choice, current_user, datestr, db, get_settings, log, monthly,
                    my_warga_id, notify, num, q, roles_required, rupiah, save_image, saldo, scalar, text,
                    user_of_warga, users_by_role)

bp = Blueprint("keuangan", __name__, url_prefix="/keuangan")
tagihan_bp = Blueprint("tagihan", __name__)
KAT_MASUK = ["Iuran Warga", "Donasi", "Saldo Awal", "Lainnya"]
KAT_KELUAR = ["Kebersihan", "Keamanan", "Perbaikan", "Kegiatan Warga", "Administrasi", "Lainnya"]
METODE_PILIHAN = ["Transfer Bank BCA", "QR Dana", "Tunai via perwakilan"]
METODE = METODE_PILIHAN + ["Transfer Bank", "QRIS", "Tunai"]


def _bulan_arg(name="bulan"):
    v = request.args.get(name, "")
    return v if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", v) else ""


# ---------------------------------------------------------------- kas
def _filter_transaksi():
    bulan, tipe = _bulan_arg(), request.args.get("tipe", "")
    where, args = [], []
    if bulan:
        where.append("substr(t.tanggal,1,7)=?")
        args.append(bulan)
    if tipe in ("pemasukan", "pengeluaran"):
        where.append("t.tipe=?")
        args.append(tipe)
    return bulan, tipe, ("WHERE " + " AND ".join(where)) if where else "", args


@bp.route("/")
@roles_required("ketua", "bendahara")
def index():
    bulan, tipe, w, args = _filter_transaksi()
    rows = q("SELECT t.*, u.nama AS oleh FROM transaksi t LEFT JOIN users u ON u.id=t.created_by "
             f"{w} ORDER BY t.tanggal DESC, t.id DESC LIMIT 300", args)
    masuk = scalar("SELECT COALESCE(SUM(nominal),0) FROM transaksi WHERE tipe='pemasukan'")
    keluar = scalar("SELECT COALESCE(SUM(nominal),0) FROM transaksi WHERE tipe='pengeluaran'")
    m_masuk = monthly("pemasukan")
    m_keluar = monthly("pengeluaran")
    
    # Gabungan cashflow 6 bulan untuk grafik keuangan komprehensif
    cashflow_6bln = []
    keluar_dict = dict(m_keluar)
    for b_lbl, m_val in m_masuk:
        k_val = keluar_dict.get(b_lbl, 0)
        cashflow_6bln.append({
            "bulan": b_lbl,
            "masuk": m_val,
            "keluar": k_val,
            "net": m_val - k_val
        })

    # Distribusi pengeluaran & pemasukan per kategori
    dist_keluar = q("SELECT kategori, COALESCE(SUM(nominal),0) total FROM transaksi "
                    "WHERE tipe='pengeluaran' GROUP BY kategori ORDER BY total DESC")
    dist_masuk = q("SELECT kategori, COALESCE(SUM(nominal),0) total FROM transaksi "
                   "WHERE tipe='pemasukan' GROUP BY kategori ORDER BY total DESC")

    return render_template("keuangan/index.html", rows=rows, bulan=bulan, tipe=tipe, masuk=masuk, keluar=keluar, saldo=saldo(),
                           chart_masuk=m_masuk, chart_keluar=m_keluar, cashflow_6bln=cashflow_6bln,
                           dist_keluar=dist_keluar, dist_masuk=dist_masuk,
                           kat_masuk=KAT_MASUK, kat_keluar=KAT_KELUAR, hari_ini=date.today().isoformat())


@bp.post("/transaksi")
@roles_required("bendahara")
def transaksi_add():
    tipe = choice("tipe", "jenis transaksi", ("pemasukan", "pengeluaran"))
    kategori = choice("kategori", "kategori", KAT_MASUK if tipe == "pemasukan" else KAT_KELUAR)
    nominal = num("nominal", "Nominal", 1, 10 ** 10)
    ket = text("keterangan", "Keterangan", 200)
    tanggal = datestr("tanggal", "transaksi")
    if tipe == "pengeluaran" and nominal > saldo():
        raise Invalid("Pengeluaran melebihi saldo kas saat ini.")
    db().execute("INSERT INTO transaksi(tipe,kategori,nominal,keterangan,tanggal,created_by) VALUES(?,?,?,?,?,?)",
                 (tipe, kategori, nominal, ket, tanggal, current_user()["id"]))
    db().commit()
    log("transaksi", f"{tipe} {nominal} {kategori}")
    notify(users_by_role("ketua"), f"Transaksi {tipe} dicatat", f"{kategori}: {ket}", url_for("keuangan.index"))
    flash("Transaksi dicatat.", "ok")
    return redirect(url_for("keuangan.index"))


def _csv_safe(v):
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v      # cegah CSV/formula injection


@bp.get("/export.csv")
@roles_required("ketua", "bendahara")
def export():
    _, _, w, args = _filter_transaksi()
    rows = q(f"SELECT t.* FROM transaksi t {w} ORDER BY t.tanggal, t.id", args)
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["Tanggal", "Jenis", "Kategori", "Keterangan", "Nominal"])
    for r in rows:
        wr.writerow([r["tanggal"], r["tipe"], _csv_safe(r["kategori"]), _csv_safe(r["keterangan"]), r["nominal"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=laporan-keuangan-rt.csv"})


# ---------------------------------------------------------------- iuran bulanan & tunggakan
NAMA_IURAN = "Iuran Bulanan"
IURAN_DEFAULT = 35000                      # Rp35.000 per bulan (bisa diubah di Admin > Pengaturan)
BELUM = ("belum_bayar", "ditolak")         # status yang dihitung sebagai tunggakan
# Tunggakan sebelum tagihan t = semua tagihan warga yang sama, belum lunas, dan lebih lama dari t.
TUNGGAKAN_LALU = ("(SELECT COALESCE(SUM(x.nominal),0) FROM tagihan x WHERE x.warga_id=t.warga_id "
                  "AND x.status IN ('belum_bayar','ditolak') "
                  "AND (x.periode<t.periode OR (x.periode=t.periode AND x.id<t.id)))")
GRUP_N = ("(SELECT COUNT(*) FROM tagihan g WHERE g.warga_id=t.warga_id AND g.bukti=t.bukti "
          "AND g.status='menunggu_verifikasi')")


def iuran_nominal():
    try:
        return max(1000, int(get_settings().get("iuran_bulanan") or IURAN_DEFAULT))
    except ValueError:
        return IURAN_DEFAULT


def _bulan_berikut(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y + m // 12}-{m % 12 + 1:02d}"


def ensure_iuran_bulanan():
    """Terbitkan iuran bulan berjalan (dan bulan yang terlewat) untuk semua warga aktif.

    Aman dipanggil berulang: tagihan yang sudah ada tidak dibuat ulang (UNIQUE warga+periode+jenis).
    Tunggakan tidak diubah di sini; ia otomatis ikut terhitung di tagihan bulan-bulan berikutnya.
    """
    sekarang = date.today().strftime("%Y-%m")
    terakhir = scalar("SELECT MAX(periode) FROM tagihan WHERE jenis=? AND periode<=?", (NAMA_IURAN, sekarang))
    mulai = _bulan_berikut(terakhir) if terakhir and terakhir < sekarang else sekarang
    daftar = []
    while mulai <= sekarang:               # format YYYY-MM: urutan teks = urutan waktu
        daftar.append(mulai)
        mulai = _bulan_berikut(mulai)
    nominal, baru = iuran_nominal(), {}
    for per in daftar[-12:]:               # paling banyak 12 bulan terakhir
        calon = q("SELECT w.id, w.user_id FROM warga w WHERE w.aktif=1 AND substr(w.created_at,1,7)<=? "
                  "AND NOT EXISTS (SELECT 1 FROM tagihan t WHERE t.warga_id=w.id AND t.periode=? AND t.jenis=?)",
                  (per, per, NAMA_IURAN))
        for w in calon:
            cur = db().execute("INSERT OR IGNORE INTO tagihan(warga_id,periode,jenis,nominal) VALUES(?,?,?,?)",
                               (w["id"], per, NAMA_IURAN, nominal))
            if cur.rowcount:
                baru[w["id"]] = w["user_id"]
    if not baru:
        return 0
    db().commit()
    for wid, uid in baru.items():
        total = scalar("SELECT COALESCE(SUM(nominal),0) FROM tagihan WHERE warga_id=? AND status IN ('belum_bayar','ditolak')", (wid,))
        notify([uid], "Tagihan iuran baru",
               f"Iuran {bulan_label(sekarang)}: {rupiah(nominal)}. Total yang harus dibayar {rupiah(total)}"
               + (" (termasuk tunggakan)." if total > nominal else "."), url_for("tagihan.saya"))
    log("iuran_otomatis", f"{daftar[-1]} x{len(baru)}")
    return len(baru)


@tagihan_bp.before_app_request
def _iuran_otomatis():
    """Tanpa penjadwal: iuran bulan baru terbit otomatis saat ada yang membuka dashboard/halaman tagihan."""
    if request.method == "GET" and session.get("uid") and request.endpoint in ("main.dashboard", "tagihan.index", "tagihan.saya"):
        try:
            ensure_iuran_bulanan()
        except sqlite3.Error:
            db().rollback()


def _ringkas(rows):
    rows = sorted(rows, key=lambda r: (r["periode"], r["id"]))
    if len(rows) == 1:
        return f"{rows[0]['jenis']} {rows[0]['periode']}"
    return f"{len(rows)} tagihan ({bulan_label(rows[0]['periode'])} - {bulan_label(rows[-1]['periode'])})"


def _belum_sampai(t):
    """Tagihan t beserta seluruh tunggakan yang lebih lama (belum bayar / bukti ditolak) milik warga yang sama."""
    return q("SELECT t.*, w.nama FROM tagihan t JOIN warga w ON w.id=t.warga_id WHERE t.warga_id=? "
             "AND t.status IN ('belum_bayar','ditolak') AND (t.periode<? OR (t.periode=? AND t.id<=?)) "
             "ORDER BY t.periode, t.id", (t["warga_id"], t["periode"], t["periode"], t["id"]))


def _grup(t):
    """Tagihan yang dibayar dengan satu bukti yang sama (satu kali kirim) dan masih menunggu verifikasi."""
    if not t["bukti"]:
        return [t]
    return q("SELECT t.*, w.nama FROM tagihan t JOIN warga w ON w.id=t.warga_id WHERE t.warga_id=? AND t.bukti=? "
             "AND t.status='menunggu_verifikasi' ORDER BY t.periode, t.id", (t["warga_id"], t["bukti"]))


# ---------------------------------------------------------------- tagihan (bendahara/ketua)
def _lunas(t, metode, oleh, commit=True):
    """Tandai lunas + catat pemasukan kas, atomik dalam satu transaksi database."""
    d = db()
    d.execute("UPDATE tagihan SET status='lunas', metode=?, tgl_bayar=date('now','localtime'), verified_by=?, catatan=NULL WHERE id=?",
              (metode, oleh, t["id"]))
    d.execute("INSERT INTO transaksi(tipe,kategori,nominal,keterangan,tanggal,tagihan_id,created_by) "
              "VALUES('pemasukan','Iuran Warga',?,?,date('now','localtime'),?,?)",
              (t["nominal"], f"{t['jenis']} {t['periode']} - {t['nama']}", t["id"], oleh))
    if commit:
        d.commit()


@tagihan_bp.route("/tagihan")
@roles_required("ketua", "bendahara")
def index():
    periode = _bulan_arg("periode") or date.today().strftime("%Y-%m")
    status = request.args.get("status", "")
    where, args = ["t.periode=?"], [periode]
    if status in STATUS["tagihan"]:
        where.append("t.status=?")
        args.append(status)
    rows = q(f"SELECT t.*, w.nama, w.alamat, {TUNGGAKAN_LALU} AS tunggakan_lalu, {GRUP_N} AS grup_n "
             "FROM tagihan t JOIN warga w ON w.id=t.warga_id "
             f"WHERE {' AND '.join(where)} ORDER BY w.nama", args)
    tot = q("SELECT COALESCE(SUM(nominal),0) total, COALESCE(SUM(CASE WHEN status='lunas' THEN nominal END),0) lunas "
            "FROM tagihan WHERE periode=?", (periode,), one=True)
    tunggak_lalu = scalar("SELECT COALESCE(SUM(nominal),0) FROM tagihan WHERE periode<? AND status IN ('belum_bayar','ditolak')",
                          (periode,))
    return render_template("keuangan/tagihan.html", rows=rows, periode=periode, status=status, tot=tot,
                           tunggak_lalu=tunggak_lalu, iuran=get_settings().get("iuran_bulanan", str(IURAN_DEFAULT)))


@tagihan_bp.post("/tagihan/generate")
@roles_required("bendahara")
def generate():
    periode = text("periode", "Periode", 7, pattern=r"\d{4}-(0[1-9]|1[0-2])")
    jenis = text("jenis", "Jenis tagihan", 60)
    nominal = num("nominal", "Nominal", 1000, 10 ** 8)
    cur = db().execute("INSERT OR IGNORE INTO tagihan(warga_id,periode,jenis,nominal) "
                       "SELECT id,?,?,? FROM warga WHERE aktif=1", (periode, jenis, nominal))
    n = cur.rowcount
    db().commit()
    log("tagihan_generate", f"{periode} {jenis} x{n}")
    if n:
        akun = [r["user_id"] for r in q("SELECT user_id FROM warga WHERE aktif=1 AND user_id IS NOT NULL")]
        notify(akun, "Tagihan baru", f"{jenis} {periode}: Rp {nominal:,}".replace(",", "."), url_for("tagihan.saya"))
    flash(f"{n} tagihan dibuat." if n else "Tagihan untuk periode & jenis ini sudah ada.", "ok" if n else "error")
    return redirect(url_for("tagihan.index", periode=periode))


@tagihan_bp.post("/tagihan/<int:tid>/aksi")
@roles_required("bendahara")
def aksi(tid):
    t = q("SELECT t.*, w.nama FROM tagihan t JOIN warga w ON w.id=t.warga_id WHERE t.id=?", (tid,), one=True)
    if not t:
        abort(404)
    act, u = request.form.get("aksi", ""), current_user()
    wuser, link = user_of_warga(t["warga_id"]), url_for("tagihan.saya")
    if act == "terima" and t["status"] == "menunggu_verifikasi":
        grup = _grup(t)                    # satu bukti bisa mencakup tunggakan + iuran bulan ini
        for g in grup:
            _lunas(g, g["metode"] or "Transfer", u["id"], commit=False)
        db().commit()
        notify([wuser], "Pembayaran diverifikasi", f"{_ringkas(grup)} lunas. Terima kasih!", link)
    elif act == "tunai" and t["status"] in BELUM:
        sasaran = _belum_sampai(t)         # tunai melunasi tagihan ini beserta tunggakan sebelumnya
        for s in sasaran:
            _lunas(s, "Tunai", u["id"], commit=False)
        db().commit()
        notify([wuser], "Pembayaran tunai dicatat", f"{_ringkas(sasaran)} lunas.", link)
    elif act == "tolak" and t["status"] == "menunggu_verifikasi":
        alasan = text("catatan", "Alasan penolakan", 200)
        ids = [g["id"] for g in _grup(t)]
        db().execute(f"UPDATE tagihan SET status='ditolak', catatan=?, verified_by=? WHERE id IN ({','.join('?' * len(ids))})",
                     (alasan, u["id"], *ids))
        db().commit()
        notify([wuser], "Bukti pembayaran ditolak", alasan, link)
    else:
        raise Invalid("Aksi tidak valid untuk status tagihan saat ini.")
    log(f"tagihan_{act}", f"#{tid}")
    flash("Tagihan diperbarui.", "ok")
    return redirect(request.referrer and url_for("tagihan.index", periode=t["periode"]) or url_for("tagihan.index"))


# ---------------------------------------------------------------- tagihan saya (warga)
@tagihan_bp.route("/tagihan-saya")
@roles_required("warga")
def saya():
    wid = my_warga_id() or 0
    rows = q(f"SELECT t.*, {TUNGGAKAN_LALU} AS tunggakan_lalu FROM tagihan t WHERE t.warga_id=? "
             "ORDER BY t.periode DESC, t.id DESC", (wid,))
    belum = [r for r in rows if r["status"] in BELUM]
    tunggak = sum(r["nominal"] for r in belum)
    # Satu pembayaran untuk semuanya: membayar tagihan terbaru otomatis melunasi tunggakan sebelumnya.
    return render_template("keuangan/tagihan_saya.html", rows=rows, belum=belum, tunggak=tunggak,
                           bayar_id=belum[0]["id"] if belum else None, metode=METODE_PILIHAN,
                           info=get_settings().get("info_pembayaran", ""))


@tagihan_bp.post("/tagihan-saya/<int:tid>/bayar")
@roles_required("warga")
def bayar(tid):
    t = q("SELECT * FROM tagihan WHERE id=? AND warga_id=?", (tid, my_warga_id() or 0), one=True)
    if not t:
        abort(404)
    if t["status"] not in BELUM:
        raise Invalid("Tagihan ini tidak dapat dibayar lagi.")
    metode = choice("metode", "metode pembayaran", METODE)
    if metode == "QRIS":
        metode = "QR Dana"
    elif metode == "Transfer Bank":
        metode = "Transfer Bank BCA"
    bukti = save_image("bukti", required=True)
    sasaran = _belum_sampai(t)             # tagihan ini + semua tunggakan yang lebih lama, satu bukti
    ids = [s["id"] for s in sasaran]
    db().execute(f"UPDATE tagihan SET status='menunggu_verifikasi', bukti=?, metode=?, catatan=NULL "
                 f"WHERE id IN ({','.join('?' * len(ids))})", (bukti, metode, *ids))
    db().commit()
    log("tagihan_bayar", f"#{tid} x{len(ids)}")
    notify(users_by_role("bendahara"), "Bukti pembayaran masuk",
           f"{_ringkas(sasaran)} · {rupiah(sum(s['nominal'] for s in sasaran))}",
           url_for("tagihan.index", periode=t["periode"]))
    flash("Bukti pembayaran terkirim. Menunggu verifikasi Bendahara.", "ok")
    return redirect(url_for("tagihan.saya"))
