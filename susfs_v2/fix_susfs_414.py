#!/usr/bin/env python3
"""
Perbaiki bug patch SUSFS v2.3.0 (JackA1ltman mainline @c231ef5d45) pada kernel 4.14.

Semua yang diperbaiki di sini adalah cacat UPSTREAM, bukan salah workflow: patch
itu ditulis untuk kernel 5.10+ lalu di-backport setengah jalan, sehingga 4 file
gagal compile. Tiap perbaikan idempoten dan memvalidasi anchor-nya (kalau
struktur berubah -> berhenti dgn pesan jelas, bukan menambal serampangan).
"""
import re, sys

def rw(path, fn):
    with open(path, encoding='utf-8', errors='surrogateescape') as f:
        s = f.read()
    out = fn(s)
    if out is None:
        return
    with open(path, 'w', encoding='utf-8', errors='surrogateescape') as f:
        f.write(out)

def need(s, txt, path, label, n=1):
    c = s.count(txt)
    if c != n:
        sys.exit("susfs-fix: anchor '%s' ketemu %d kali (harus %d) di %s" % (label, c, n, path))

# =====================================================================
# 1) fs/stat.c - stat->mnt_id tidak ada di struct kstat sebelum 5.10.
#    susfs.c sendiri menjaga field ini dgn
#        #if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 10, 0)
#    tapi hunk fs/stat.c lupa memakai penjaga yang sama.
#    result_mask tetap di-set, jadi jalur spoofing tidak berubah.
# =====================================================================
MNT_PAT = r'^[ \t]*stat->mnt_id = real_mount\(path->mnt\)->mnt_id;\n'

def fix_stat_mnt_id(s):
    # Sejak JackA1ltman@1c442877 ("fix: needn't real_mount") upstream TIDAK
    # menghapus assignment ini melainkan MENGOMENTARINYA menjadi
    #     // stat->mnt_id = real_mount(path->mnt)->mnt_id;
    # Jadi pencarian substring "stat->mnt_id" masih menemukannya DI DALAM
    # komentar - itu bukan tanda kodenya aktif. Yang dihitung harus assignment
    # yang benar-benar aktif; regex di bawah berjangkar ^[ \t]* sehingga baris
    # yang diawali "//" tidak ikut cocok.
    n = len(re.findall(MNT_PAT, s, re.M))
    if n == 0:
        print("stat.c: tidak ada assignment stat->mnt_id aktif (sudah dikomentari/dihapus upstream), dilewati")
        return None
    if n != 2:
        sys.exit("susfs-fix: assignment stat->mnt_id aktif ketemu %d kali di fs/stat.c (harus 0 atau 2)" % n)
    s = re.sub(MNT_PAT, '', s, flags=re.M)
    print("stat.c: 2 assignment stat->mnt_id dibuang (kstat 4.14 tidak punya field itu)")
    return s

# =====================================================================
# 2) fs/stat.c + fs/namei.c - filename_lookup().
#    susfs_inline_hook_patches.sh menyisipkan pemanggilan filename_lookup()
#    ke fs/stat.c, TAPI penjaganya
#        if ! grep -q "internal.h" fs/stat.c && ! grep "static int filename_lookup" fs/namei.c
#    membuat deklarasinya TIDAK ikut disisipkan justru ketika fungsinya static
#    (kasus 4.14) - jadi kodenya masuk tanpa deklarasi apa pun. Solusinya:
#    buang 'static' di definisi namei.c lalu deklarasikan extern di stat.c.
# =====================================================================
NAMEI_OLD = "static int filename_lookup(int dfd, struct filename *name, unsigned flags,\n"
NAMEI_NEW = "int filename_lookup(int dfd, struct filename *name, unsigned flags,\n"

def fix_namei(s):
    if NAMEI_OLD not in s:
        print("namei.c: filename_lookup sudah non-static, dilewati")
        return None
    need(s, NAMEI_OLD, 'fs/namei.c', 'static int filename_lookup(')
    print("namei.c: 'static' dibuang dari definisi filename_lookup()")
    return s.replace(NAMEI_OLD, NAMEI_NEW, 1)

STAT_DECL = ("extern int filename_lookup(int dfd, struct filename *name, unsigned flags,\n"
             "\t\t\tstruct path *path, struct path *root);\n")
STAT_ANCHOR = "extern int ksu_handle_stat(int *dfd, struct filename **filename, int *flags);\n"

def fix_stat_decl(s):
    if "extern int filename_lookup(" in s:
        print("stat.c: deklarasi filename_lookup sudah ada, dilewati")
        return None
    if "filename_lookup(" not in s:
        print("stat.c: filename_lookup tidak dipakai (hook script berubah?), dilewati")
        return None
    need(s, STAT_ANCHOR, 'fs/stat.c', 'extern int ksu_handle_stat(')
    print("stat.c: deklarasi extern filename_lookup() ditambahkan")
    return s.replace(STAT_ANCHOR, STAT_ANCHOR + STAT_DECL, 1)

# =====================================================================
# 3) fs/statfs.c - susfs_statfs_by_dentry() memanggil
#    susfs_sus_kstat_spoof_vfs_statfs() padahal blok extern-nya ditaruh
#    SESUDAH fungsi itu. Deklarasi diulang di atas (deklarasi ganda sah di C).
#    Anchor sengaja cuma nama fungsinya, tanpa daftar parameter: SUSFS
#    6b9e7acf9 menambah parameter `struct vfsmount *mnt`, dan anchor yang
#    memuat signature lengkap membuat fixer ini (dan verifikasinya) berhenti
#    dengan "tidak ketemu" walau extern-nya sudah benar di atas.
# =====================================================================
STATFS_ANCHOR = "static int susfs_statfs_by_dentry("
STATFS_DECL = ("extern bool susfs_is_inode_sus_kstat(struct inode *inode, bool *out_is_fuse);\n"
               "extern int susfs_sus_kstat_spoof_vfs_statfs(struct inode *inode, struct kstatfs *buf, bool *is_fuse);\n")

def fix_statfs(s):
    i_decl = s.find("extern int susfs_sus_kstat_spoof_vfs_statfs")
    i_use = s.find(STATFS_ANCHOR)
    if i_use < 0:
        sys.exit("susfs-fix: susfs_statfs_by_dentry() tidak ketemu di fs/statfs.c")
    if 0 <= i_decl < i_use:
        print("statfs.c: extern sudah mendahului pemakaian, dilewati")
        return None
    need(s, STATFS_ANCHOR, 'fs/statfs.c', 'static int susfs_statfs_by_dentry(')
    print("statfs.c: deklarasi extern dipindah ke atas susfs_statfs_by_dentry()")
    return s.replace(STATFS_ANCHOR, STATFS_DECL + STATFS_ANCHOR, 1)

# =====================================================================
# 4) fs/susfs.c - security_sb_statfs() butuh <linux/security.h>.
# =====================================================================
SUSFS_INC_ANCHOR = "#include <linux/fs.h>\n"
SUSFS_INC = "#include <linux/security.h>\n"

def fix_susfs_include(s):
    if SUSFS_INC in s:
        print("susfs.c: <linux/security.h> sudah ada, dilewati")
        return None
    need(s, SUSFS_INC_ANCHOR, 'fs/susfs.c', '#include <linux/fs.h>')
    print("susfs.c: #include <linux/security.h> ditambahkan (untuk security_sb_statfs)")
    return s.replace(SUSFS_INC_ANCHOR, SUSFS_INC_ANCHOR + SUSFS_INC, 1)

# =====================================================================
# 5) fs/susfs.c - satu call site susfs_mark_inode_sus_kstat() ketinggalan
#    argumen ke-3 (is_update) waktu signature-nya ditambah. Jalur ini adalah
#    "if no duplicated, add it to list" = penambahan baru, jadi is_update=false
#    (sama seperti call site sejenis di atasnya).
# =====================================================================
CALL_BAD = "susfs_mark_inode_sus_kstat(new_entry->info.target_pathname, new_entry);\n"
CALL_OK = "susfs_mark_inode_sus_kstat(new_entry->info.target_pathname, new_entry, false);\n"

def fix_susfs_call(s):
    if CALL_BAD not in s:
        print("susfs.c: call susfs_mark_inode_sus_kstat sudah 3 argumen, dilewati")
        return None
    need(s, CALL_BAD, 'fs/susfs.c', 'susfs_mark_inode_sus_kstat(.., ..) 2-argumen')
    print("susfs.c: argumen is_update=false ditambahkan ke call susfs_mark_inode_sus_kstat()")
    return s.replace(CALL_BAD, CALL_OK, 1)

# =====================================================================
# 6) fs/proc/fd.c - blok SUS_MOUNT kurang satu '}': ada dua 'if' yang dibuka
#    tapi cuma satu yang ditutup sebelum label orig_flow, jadi seluruh sisa
#    file ikut tertelan ke dalam seq_show() ("function definition is not
#    allowed here"). Ini bug sejenis yg baru saja upstream perbaiki di
#    fs/notify/fdinfo.c tapi terlewat di sini.
# =====================================================================
FD_OLD = "\t\tgoto orig_flow;\n\t}\norig_flow:\n#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\n"
FD_NEW = "\t\tgoto orig_flow;\n\t\t}\n\t}\norig_flow:\n#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\n"

def fix_fd(s):
    if FD_NEW in s:
        print("proc/fd.c: brace sudah seimbang, dilewati")
        return None
    need(s, FD_OLD, 'fs/proc/fd.c', "'goto orig_flow; } orig_flow:'")
    print("proc/fd.c: satu '}' yang hilang di blok SUS_MOUNT ditambahkan")
    return s.replace(FD_OLD, FD_NEW, 1)

# ============================================================================
# 7) fs/super.c - hunk get_anon_bdev() yang BARU (mulai pin c231ef5d45)
#    memanggil ida_simple_get() pada unnamed_dev_ida. Di 4.14 IDA itu dikelola
#    dengan ida_pre_get()+ida_get_new_above() DI BAWAH unnamed_dev_lock,
#    sedangkan ida_simple_get() memakai simple_ida_lock miliknya sendiri ->
#    satu IDA dimodifikasi di bawah dua lock berbeda = data race, karena
#    get_anon_bdev() bisa jalan bersamaan di CPU lain untuk mount anonim apa
#    pun. Di 5.10 (target asli SUSFS) tidak ada masalah: get_anon_bdev() di
#    sana sudah memakai ida_alloc_range() yang ber-lock sendiri.
#    Perbaikan MINIMAL: panggilannya dibungkus unnamed_dev_lock, teks upstream
#    tidak diubah. Tidak mungkin "sleeping in atomic": flag-nya GFP_ATOMIC,
#    jadi ida_pre_get() di dalam ida_simple_get() tidak pernah tidur. Urutan
#    lock aman (tidak ada ABBA): simple_ida_lock hanya dipegang DI DALAM
#    ida_simple_*(), yang tidak pernah mengambil unnamed_dev_lock.
# ============================================================================
SUPER_OLD = ("\t\t\tdev = ida_simple_get(&unnamed_dev_ida, DEFAULT_KSU_MNT_MINOR_DEV,\n"
             "\t\t\t\t\t     (1 << MINORBITS), GFP_ATOMIC);\n")
SUPER_NEW = ("\t\t\tspin_lock(&unnamed_dev_lock);\n" + SUPER_OLD +
             "\t\t\tspin_unlock(&unnamed_dev_lock);\n")

def fix_super_ida(s):
    if SUPER_NEW in s:
        print("super.c: ida_simple_get sudah dibungkus unnamed_dev_lock, dilewati")
        return None
    if "ida_simple_get(&unnamed_dev_ida" not in s:
        print("super.c: hunk get_anon_bdev SUSFS tidak ada (pin lebih lama?), dilewati")
        return None
    need(s, SUPER_OLD, 'fs/super.c', 'dev = ida_simple_get(&unnamed_dev_ida, ...)')
    print("super.c: ida_simple_get(&unnamed_dev_ida) dibungkus unnamed_dev_lock")
    return s.replace(SUPER_OLD, SUPER_NEW, 1)

# ============================================================================
# 8) fs/namespace.c - cacat SEJENIS di susfs_alloc_non_unshare_ksu_vfsmnt():
#    ida_simple_get(&mnt_id_ida, ...) padahal mnt_id_ida di 4.14 dilindungi
#    mnt_id_lock (lihat mnt_alloc_id()/mnt_free_id() di file yang sama). Ini
#    SUDAH ada sejak pin-pin sebelumnya (revisi ke-19/ke-20 boot normal dengan
#    cacat ini), jadi bukan regresi baru - tapi tetap race yang sama.
#    Di sini bungkus-lock TIDAK cukup: flag-nya GFP_KERNEL, dan ida_pre_get()
#    dengan GFP_KERNEL BOLEH TIDUR -> memanggilnya di dalam spinlock justru
#    membuat bug baru yang lebih parah ("sleeping function called from invalid
#    context"). Karena itu jalurnya ditulis ulang persis gaya 4.14: ida_pre_get()
#    di LUAR lock, ida_get_new_above() di DALAM mnt_id_lock, retry saat -EAGAIN.
#    ida_get_new_above() menulis id ke &mnt->mnt_id dan mengembalikan 0, jadi
#    di akhir res di-set ulang ke mnt->mnt_id supaya DUA baris upstream
#    sesudahnya ("if (res < 0) goto out_free_cache;" dan "mnt->mnt_id = res;")
#    tetap benar tanpa perlu dihapus - yang pertama jadi cek yang selalu lolos,
#    yang kedua jadi self-assignment.
# ============================================================================
NS_OLD = "\t\tres = ida_simple_get(&mnt_id_ida, DEFAULT_KSU_MNT_ID, 0, GFP_KERNEL);\n"
NS_NEW = ("\t\tdo {\n"
          "\t\t\tif (!ida_pre_get(&mnt_id_ida, GFP_KERNEL)) {\n"
          "\t\t\t\tres = -ENOMEM;\n"
          "\t\t\t\tgoto out_free_cache;\n"
          "\t\t\t}\n"
          "\t\t\tspin_lock(&mnt_id_lock);\n"
          "\t\t\tres = ida_get_new_above(&mnt_id_ida, DEFAULT_KSU_MNT_ID, &mnt->mnt_id);\n"
          "\t\t\tspin_unlock(&mnt_id_lock);\n"
          "\t\t} while (res == -EAGAIN);\n"
          "\t\tif (res < 0)\n"
          "\t\t\tgoto out_free_cache;\n"
          "\t\tres = mnt->mnt_id;\n")

def fix_namespace_ida(s):
    if "ida_get_new_above(&mnt_id_ida, DEFAULT_KSU_MNT_ID" in s:
        print("namespace.c: alokasi mnt_id ksu sudah lewat jalur ber-lock, dilewati")
        return None
    if "ida_simple_get(&mnt_id_ida" not in s:
        print("namespace.c: tidak ada ida_simple_get(&mnt_id_ida) (struktur patch berubah?), dilewati")
        return None
    need(s, NS_OLD, 'fs/namespace.c', 'res = ida_simple_get(&mnt_id_ida, ...)')
    print("namespace.c: alokasi mnt_id ksu dipindah ke ida_pre_get+ida_get_new_above di bawah mnt_id_lock")
    return s.replace(NS_OLD, NS_NEW, 1)

rw('fs/stat.c', fix_stat_mnt_id)
rw('fs/namei.c', fix_namei)
rw('fs/stat.c', fix_stat_decl)
rw('fs/statfs.c', fix_statfs)
rw('fs/susfs.c', fix_susfs_include)
rw('fs/susfs.c', fix_susfs_call)
rw('fs/proc/fd.c', fix_fd)
rw('fs/super.c', fix_super_ida)
rw('fs/namespace.c', fix_namespace_ida)

# ---------------- verifikasi akhir ----------------
def rd(p):
    return open(p, encoding='utf-8', errors='surrogateescape').read()

err = []
st = rd('fs/stat.c')
if re.search(MNT_PAT, st, re.M):
    err.append("fs/stat.c masih memakai stat->mnt_id (assignment aktif, bukan komentar)")
if 'filename_lookup(' in st and 'extern int filename_lookup(' not in st:
    err.append("fs/stat.c memanggil filename_lookup() tanpa deklarasi")
if re.search(r'^\+', st, re.M):
    err.append("fs/stat.c masih punya baris berawalan '+' (sisa marker diff)")
nm = rd('fs/namei.c')
if 'static int filename_lookup(' in nm:
    err.append("fs/namei.c: filename_lookup masih static")
sf = rd('fs/statfs.c')
# Pemeriksaan ini dulu `find(decl) > find(use)`. Kalau extern-nya TIDAK ADA sama
# sekali, find() mengembalikan -1 dan -1 > n selalu False -> lolos padahal
# fs/statfs.c tidak bisa compile. Sekarang extern WAJIB ada DAN mendahului.
i_decl = sf.find('extern int susfs_sus_kstat_spoof_vfs_statfs')
i_use = sf.find(STATFS_ANCHOR)
if i_use < 0:
    err.append("fs/statfs.c: susfs_statfs_by_dentry() hilang (patch SUSFS tidak ter-apply?)")
elif not (0 <= i_decl < i_use):
    err.append("fs/statfs.c: extern susfs_sus_kstat_spoof_vfs_statfs tidak ada atau "
               "masih di bawah pemakaian (decl=%d, use=%d)" % (i_decl, i_use))
su = rd('fs/susfs.c')
if '#include <linux/security.h>' not in su:
    err.append("fs/susfs.c: <linux/security.h> belum ada")
if CALL_BAD in su:
    err.append("fs/susfs.c: masih ada call susfs_mark_inode_sus_kstat 2-argumen")
fd = rd('fs/proc/fd.c')
if fd.count('{') != fd.count('}'):
    err.append("fs/proc/fd.c: brace tidak seimbang ({=%d, }=%d)" % (fd.count('{'), fd.count('}')))
sp = rd('fs/super.c')
if 'ida_simple_get(&unnamed_dev_ida' in sp and SUPER_NEW not in sp:
    err.append("fs/super.c: ida_simple_get(&unnamed_dev_ida) tidak dilindungi unnamed_dev_lock "
               "(race dgn jalur ida_get_new_above di bawah lock itu)")
if sp.count('spin_lock(&unnamed_dev_lock);') != sp.count('spin_unlock(&unnamed_dev_lock);'):
    err.append("fs/super.c: spin_lock/spin_unlock unnamed_dev_lock tidak seimbang (%d vs %d)"
               % (sp.count('spin_lock(&unnamed_dev_lock);'), sp.count('spin_unlock(&unnamed_dev_lock);')))
ns = rd('fs/namespace.c')
if 'ida_simple_get(&mnt_id_ida' in ns:
    err.append("fs/namespace.c: masih ada ida_simple_get(&mnt_id_ida) - lock-nya campur dgn mnt_id_lock")
if ns.count('spin_lock(&mnt_id_lock);') != ns.count('spin_unlock(&mnt_id_lock);'):
    err.append("fs/namespace.c: spin_lock/spin_unlock mnt_id_lock tidak seimbang (%d vs %d)"
               % (ns.count('spin_lock(&mnt_id_lock);'), ns.count('spin_unlock(&mnt_id_lock);')))
for _p, _t in (('fs/super.c', sp), ('fs/namespace.c', ns)):
    if _t.count('{') != _t.count('}'):
        err.append("%s: brace tidak seimbang ({=%d, }=%d)" % (_p, _t.count('{'), _t.count('}')))
if err:
    print("\nsusfs-fix: VERIFIKASI GAGAL:")
    for e in err:
        print("  - " + e)
    sys.exit(1)
print("susfs-fix: OK, 6 file SUSFS diverifikasi")
