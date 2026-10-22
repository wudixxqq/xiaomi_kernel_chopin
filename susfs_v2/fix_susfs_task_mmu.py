#!/usr/bin/env python3
"""
Perbaikan fs/proc/task_mmu.c setelah patch SUSFS 4.14.

Kenapa perlu: fs/proc/task_mmu.c di source rosemary-13 sudah di-backport gaya
4.19+ (ada smap_gather_stats(), show_smap() tanpa is_pid, tidak ada
<linux/ctype.h>). Akibatnya pada patch upstream:
  - Hunk #1 (include susfs_def.h)  -> FAILED  => macro SUSFS_IS_INODE_* jadi
    implicit declaration.
  - Hunk #4 (guard SUS_MAP di show_smap) -> "berhasil" dengan fuzz 2 tapi
    MENDARAT DI smap_gather_stats() yang bertipe void => "return 0" di fungsi
    void.
  - Hunk #5 & #6 -> FAILED, isinya cuma tambah/hapus baris kosong (kosmetik,
    aman diabaikan).

Script ini memasang hunk #1 dan memindahkan hunk #4 ke tempat yang benar.
Idempoten: kalau sudah benar, tidak melakukan apa-apa.
"""
import re, sys

PATH = "fs/proc/task_mmu.c"
INC = ("#if defined(CONFIG_KSU_SUSFS_SUS_KSTAT) || defined(CONFIG_KSU_SUSFS_SUS_MAP) || defined(CONFIG_KSU_SUSFS_OPEN_REDIRECT)\n"
       "#include <linux/susfs_def.h>\n"
       "#endif\n")
GUARD = ("#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n"
         "\tif (vma->vm_file && SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n"
         "\t\treturn 0;\n"
         "#endif\n")

s = open(PATH, encoding='utf-8', errors='surrogateescape').read()

# --- 1) include susfs_def.h (hunk #1 yang gagal)
if "susfs_def.h" in s:
    print("task_mmu: include susfs_def.h sudah ada, dilewati")
else:
    anchor = "#include <linux/mm_inline.h>\n"
    if s.count(anchor) != 1:
        sys.exit("task_mmu: anchor include mm_inline.h ketemu %d kali (harus 1)" % s.count(anchor))
    s = s.replace(anchor, anchor + INC, 1)
    print("task_mmu: include susfs_def.h ditambahkan")

# --- 2) buang guard SUS_MAP yang salah mendarat di smap_gather_stats() (void)
misplaced = re.compile(
    r'\n#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
    r'\tif \(vma->vm_file && SUSFS_IS_INODE_SUS_MAP\(file_inode\(vma->vm_file\)\)\)\n'
    r'\t\treturn 0;\n'
    r'#endif\n\n(\tsmaps_walk\.private = mss;\n)')
n = len(misplaced.findall(s))
if n == 1:
    s = misplaced.sub(lambda m: "\n" + m.group(1), s)
    print("task_mmu: guard SUS_MAP salah tempat di smap_gather_stats() dihapus")
elif n == 0:
    print("task_mmu: tidak ada guard salah tempat (mungkin patch upstream sudah diperbaiki)")
else:
    sys.exit("task_mmu: guard salah tempat ketemu %d kali, struktur tak dikenal" % n)

# --- 3) pasang guard SUS_MAP di show_smap() yang benar
if re.search(r'static int show_smap\(struct seq_file \*m, void \*v\)\n\{\n[^}]*?SUSFS_IS_INODE_SUS_MAP', s):
    print("task_mmu: guard SUS_MAP di show_smap() sudah ada, dilewati")
else:
    anchor = ("static int show_smap(struct seq_file *m, void *v)\n"
              "{\n"
              "\tstruct vm_area_struct *vma = v;\n"
              "\tstruct mem_size_stats mss;\n\n")
    if s.count(anchor) != 1:
        sys.exit("task_mmu: anchor show_smap() ketemu %d kali (harus 1) - "
                 "struktur file berubah, hook SUS_MAP perlu disesuaikan manual" % s.count(anchor))
    s = s.replace(anchor, anchor + GUARD + "\n", 1)
    print("task_mmu: guard SUS_MAP dipasang di show_smap()")

open(PATH, 'w', encoding='utf-8', errors='surrogateescape').write(s)

# --- verifikasi akhir
#
# Dulu bagian ini memakai s.index() dua kali. Dua masalah nyata:
#   a) s.index() melempar ValueError (traceback mentah) kalau salah satu penanda
#      hilang - pesan errornya tidak menjelaskan apa-apa ke pembaca log CI.
#   b) kalau penanda akhir kebetulan berada SEBELUM penanda awal, slicing Python
#      menghasilkan string kosong tanpa error, jadi cek "return 0;" lolos
#      diam-diam (false negative) - persis kegagalan yang mau dicegah.
# Sekarang dipakai find() + validasi urutan secara eksplisit.
s = open(PATH, encoding='utf-8', errors='surrogateescape').read()

if "susfs_def.h" not in s:
    sys.exit("task_mmu: include susfs_def.h hilang setelah penulisan file")

BEG = "static void smap_gather_stats("
END = "/* Show the contents common for smaps"
b = s.find(BEG)
e = s.find(END)
if b < 0:
    sys.exit("task_mmu: penanda awal '%s' tidak ditemukan - struktur file berubah, "
             "verifikasi smap_gather_stats() tidak bisa dijalankan" % BEG)
if e < 0:
    sys.exit("task_mmu: penanda akhir '%s' tidak ditemukan - struktur file berubah, "
             "verifikasi smap_gather_stats() tidak bisa dijalankan" % END)
if e <= b:
    sys.exit("task_mmu: penanda akhir muncul sebelum penanda awal (b=%d, e=%d) - "
             "urutan fungsi berubah, verifikasi tidak valid" % (b, e))

gather = s[b:e]
if "return 0;" in gather:
    sys.exit("task_mmu: masih ada 'return 0;' di smap_gather_stats() yang bertipe void")
print("task_mmu: OK (blok smap_gather_stats() diverifikasi, %d byte)" % len(gather))
