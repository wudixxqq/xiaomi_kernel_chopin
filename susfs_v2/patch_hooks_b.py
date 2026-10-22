#!/usr/bin/env python3
"""
chopin 4.14 SUSFS v2.3.0 integration (Plan B') - full patch chain.

Order matters:
  [1] revert in-tree susfs v1.5.5 (official kernel-4.14 patch, reversed)
  [2] delete old susfs files + clean fdinfo.c / kernel/sys.c leftovers
  [3] whole-tree residue assertion (no susfs trace left)
  [4] strip legacy bool-gate KSU hooks (inline_hook_check forbids them and
      they make susfs_inline_hook_patches.sh skip the files via grep ksu_handle)
  [5] declare filename_lookup() in fs/internal.h
  [6] run JackA1ltman susfs_inline_hook_patches.sh (injects ksu_handle_* calls)
  [7] apply JackA1ltman susfs_patch_to_4.14.patch (v2.3.0 kernel side),
      excluding fs/notify/fdinfo.c and kernel/sys.c (manual port instead)
  [8] exec.c: drop susfs_is_sdcard_android_data_not_decrypted branch
      (BakaSU dac2d18 does not provide that symbol)
  [9] input.c: hook script's sed anchor misses 4.14 (extra `code` arg) - add call
  [10] fdinfo.c: manual port of rejected Hunk #4 (4.14.186 context differs)
  [11] kernel/sys.c: manual port of SPOOF_UNAME (miui bpfloader code sits
       between the upstream hunk's context lines)
  [12] run rosemary fixers (task_mmu + stat/statfs/susfs/proc-fd/super/namespace)
  [13] final verification (inline_hook_check contract + hygiene)

--allow-missing: local simulation mode - files absent from the mini tree are
skipped with a warning instead of failing. NEVER use in CI.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ASSET_DIR = os.path.dirname(os.path.abspath(__file__))
ALLOW_MISSING = '--allow-missing' in sys.argv
PATCH_EXE = os.environ.get('PATCH_EXE', 'patch')

STEP = [0]


def out(msg):
    print(msg, flush=True)


def fatal(msg):
    out('FATAL: ' + msg)
    sys.exit(1)


def next_step(title):
    STEP[0] += 1
    out('')
    out('== [%d] %s ==' % (STEP[0], title))


def ropen(p):
    return open(p, encoding='utf-8', errors='surrogateescape')


def wopen(p):
    return open(p, 'w', encoding='utf-8', errors='surrogateescape', newline='')


def need(path):
    if not os.path.exists(path):
        if ALLOW_MISSING:
            out('-- SKIP (missing, simulation): %s' % path)
            return False
        fatal('%s not found' % path)
    return True


def sub_once(path, pattern, repl, desc, count=1, flags=0):
    with ropen(path) as f:
        src = f.read()
    new, n = re.subn(pattern, repl, src, flags=flags)
    if n != count:
        fatal('%s: expected %d match(es) for %s, found %d' % (path, count, desc, n))
    with wopen(path) as f:
        f.write(new)
    out('OK  %s: %s' % (path, desc))


def insert_before(path, anchor, insertion, desc, count=1):
    with ropen(path) as f:
        src = f.read()
    n = src.count(anchor)
    if n != count:
        fatal('%s: expected %d anchor(s) for %s, found %d' % (path, count, desc, n))
    with wopen(path) as f:
        f.write(src.replace(anchor, insertion + anchor))
    out('OK  %s: %s' % (path, desc))


def insert_after(path, anchor, insertion, desc, count=1):
    with ropen(path) as f:
        src = f.read()
    n = src.count(anchor)
    if n != count:
        fatal('%s: expected %d anchor(s) for %s, found %d' % (path, count, desc, n))
    with wopen(path) as f:
        f.write(src.replace(anchor, anchor + insertion))
    out('OK  %s: %s' % (path, desc))


def strip_ksu_blocks(path, needles, desc):
    """remove #ifdef CONFIG_KSU ... #endif blocks containing any needle"""
    with ropen(path) as f:
        lines = f.readlines()
    block_start = re.compile(r'#\s*ifdef\s+CONFIG_KSU\s*$')
    out_l, i, removed = [], 0, 0
    while i < len(lines):
        if block_start.match(lines[i].strip()):
            j, depth = i, 1
            while j + 1 < len(lines) and depth:
                j += 1
                s = lines[j].strip()
                if s.startswith('#if'):
                    depth += 1
                elif s.startswith('#endif'):
                    depth -= 1
            text = ''.join(lines[i:j + 1])
            if any(nd in text for nd in needles):
                removed += 1
                i = j + 1
                continue
        out_l.append(lines[i])
        i += 1
    if removed == 0:
        fatal('%s: no block removed for %s' % (path, desc))
    with wopen(path) as f:
        f.writelines(out_l)
    out('OK  %s: %s (removed %d block(s))' % (path, desc, removed))


def split_sections(patch_text):
    """split a git-style multi-file patch into (target_path, section_text)"""
    idx = patch_text.find('diff --git ')
    body = patch_text[idx:] if idx >= 0 else patch_text
    parts = [p for p in re.split(r'(?=^diff --git )', body, flags=re.M)
             if p.startswith('diff --git ')]
    secs = []
    for p in parts:
        m = re.match(r'diff --git a/(\S+)', p)
        secs.append((m.group(1), p))
    return secs


def run_patch_sections(patch_file, mode, excludes=(), expect_fail=()):
    """apply/revert a multi-file patch section by section.

    mode: '-R' (revert) or '-1' (apply)
    excludes: target paths to skip entirely (handled manually elsewhere)
    expect_fail: (path, substring-of-stderr) pairs tolerated; anything else fails
    """
    with ropen(patch_file) as f:
        secs = split_sections(f.read())
    failed = []
    for name, text in secs:
        if name in excludes:
            out('-- SKIP (excluded, manual port): %s' % name)
            continue
        is_new_file = 'new file mode' in text.split('\n@@', 1)[0]
        if not os.path.exists(name) and not is_new_file:
            if ALLOW_MISSING:
                out('-- SKIP (missing, simulation): %s' % name)
                continue
            failed.append((name, 'target file does not exist'))
            continue
        with tempfile.NamedTemporaryFile('w', suffix='.patch', delete=False,
                                         encoding='utf-8',
                                         errors='surrogateescape') as tf:
            tf.write(text)
            tmp = tf.name
        try:
            cmd = [PATCH_EXE] + ([mode] if mode else []) + \
                ['-p1', '--no-backup-if-mismatch', '-i', tmp]
            r = subprocess.run(cmd,
                               capture_output=True, text=True,
                               encoding='utf-8', errors='replace')
        finally:
            os.unlink(tmp)
        ok = r.returncode == 0
        if not ok:
            for ep, esub in expect_fail:
                if name == ep and esub in (r.stderr or '') + (r.stdout or ''):
                    out('-- TOLERATED (expected): %s' % name)
                    ok = 'tolerated'
                    break
        if ok is True:
            out('OK  patch %s: %s' % (mode, name))
        elif ok != 'tolerated':
            failed.append((name, ((r.stderr or '') + (r.stdout or '')).strip()))
    if failed:
        for name, err in failed:
            out('--- patch %s FAILED on %s:' % (mode, name))
            out(err)
        fatal('patch %s: %d section(s) failed' % (mode, len(failed)))


# ===========================================================================
next_step('module Kconfig: decouple hook-choice, drop THREAD_INFO_IN_TASK dep')
p = 'drivers/kernelsu/Kconfig'
if need(p):
    with ropen(p) as f:
        lines = f.readlines()
    out_l, i, n = [], 0, len(lines)
    decoupled = 0
    while i < n:
        if lines[i].strip() == 'choice' and i + 1 < n and 'KernelSU Hooking Method' in lines[i + 1]:
            j = i
            while j < n and lines[j].strip() != 'endchoice':
                j += 1
            if j >= n:
                fatal('endchoice not found in %s' % p)
            k = i
            while k < j and not lines[k].strip().startswith('config '):
                k += 1
            if k >= j:
                fatal('no config inside choice in %s' % p)
            out_l.extend(lines[k:j])
            decoupled += 1
            i = j + 1
            continue
        out_l.append(lines[i])
        i += 1
    if decoupled != 1:
        fatal('hook-method choice not found in %s' % p)
    txt = ''.join(out_l)
    txt = re.sub(r'(?m)^(\s*depends on[^\n]*)THREAD_INFO_IN_TASK\s*&&\s*', r'\1', txt)
    txt = re.sub(r'(?m)^(\s*depends on[^\n]*)\s*&&\s*THREAD_INFO_IN_TASK', r'\1', txt)
    txt = re.sub(r'(?m)^(\s*depends on\s+)THREAD_INFO_IN_TASK\s*$', r'\1', txt)
    if 'config KSU_MANUAL_HOOK' not in txt or 'config KSU_SUSFS' not in txt:
        fatal('%s: KSU_MANUAL_HOOK/KSU_SUSFS missing after decouple' % p)
    if re.search(r'(?m)^\s*choice\s*$', txt):
        fatal('%s: choice block still present' % p)
    if 'THREAD_INFO_IN_TASK' in txt:
        fatal('%s: THREAD_INFO_IN_TASK dependency still present' % p)
    with wopen(p) as f:
        f.write(txt)
    out('OK  %s: choice decoupled, THREAD_INFO_IN_TASK dep removed' % p)

# ===========================================================================
next_step('remove BakaSU susfs_compat.mk $(error) gate')
gate = '$(error Please wait susfs update'
cands = ['drivers/kernelsu/tools/susfs_compat.mk',
         'KernelSU/kernel/tools/susfs_compat.mk']
seen_real = set()
for p in cands:
    if not os.path.exists(p):
        out('-- skip (missing): %s' % p)
        continue
    real = os.path.realpath(p)
    if real in seen_real:
        out('-- skip (same file via symlink): %s' % p)
        continue
    seen_real.add(real)
    with ropen(p) as f:
        mk = f.read()
    if gate in mk:
        mk = re.sub(r'(?m)^\s*\$\(error Please wait susfs update[^\n]*(?:\n|\Z)', '', mk)
        with wopen(p) as f:
            f.write(mk)
        out('OK  %s: gate removed (realpath=%s)' % (p, real))
    else:
        out('OK  %s: no gate present (realpath=%s)' % (p, real))
    with ropen(p) as f:
        if gate in f.read():
            fatal('%s still contains the gate after patching' % p)
if not seen_real:
    if ALLOW_MISSING:
        out('-- SKIP gate removal (simulation, no module tree)')
    else:
        fatal('no susfs_compat.mk found in either module path')
out('OK  susfs update gate verified removed everywhere')

# ===========================================================================
next_step('revert in-tree susfs v1.5.5 (official kernel-4.14 patch, -R)')
# fdinfo.c / kernel/sys.c / task_mmu.c / readdir.c reverted manually in step
# [4]: miui-tree context differences make some stock hunks fail there.
run_patch_sections(os.path.join(ASSET_DIR, 'susfs_v155_414_official.patch'),
                   '-R',
                   excludes=('fs/notify/fdinfo.c', 'kernel/sys.c',
                             'fs/proc/task_mmu.c', 'fs/readdir.c'))

# ===========================================================================
next_step('delete old susfs files; clean fdinfo/sys/task_mmu/readdir leftovers')


def remove_all_susfs_blocks(path):
    """generic cleanup: remove every #ifdef CONFIG_KSU_SUSFS* ... #endif block
    (nesting-aware) plus susfs include lines. Used for files where the stock
    v1.5.5 revert hunks failed (task_mmu.c / readdir.c leftovers)."""
    with ropen(path) as f:
        lines = f.readlines()
    blk = re.compile(r'#\s*ifdef\s+CONFIG_KSU_SUSFS')
    out_l, i, removed = [], 0, 0
    while i < len(lines):
        if blk.match(lines[i].strip()):
            j, depth = i, 1
            while j + 1 < len(lines) and depth:
                j += 1
                s = lines[j].strip()
                if s.startswith('#if'):
                    depth += 1
                elif s.startswith('#endif'):
                    depth -= 1
            removed += 1
            i = j + 1
            continue
        out_l.append(lines[i])
        i += 1
    txt = ''.join(out_l)
    txt2 = re.sub(r'#include <linux/susfs(_def)?\.h>\r?\n', '', txt)
    if txt2 != txt:
        removed += 1
    if 'susfs' in txt2 or 'SUSFS' in txt2:
        fatal('%s: susfs trace remains after generic cleanup' % path)
    with wopen(path) as f:
        f.write(txt2)
    out('OK  %s: generic susfs cleanup (%d block(s)/line(s) removed)' % (path, removed))


for p in ('fs/susfs.c', 'include/linux/susfs.h', 'include/linux/susfs_def.h'):
    if os.path.exists(p):
        os.remove(p)
        out('OK  removed %s' % p)
    elif ALLOW_MISSING:
        out('-- SKIP (missing, simulation): %s' % p)
    else:
        fatal('%s missing (expected in-tree v1.5.5 file)' % p)
# v1.5.5 SUS_SU helper - unreferenced dead file in the chopin tree (no
# fs/Makefile entry); v2.3.0 has no SUS_SU backend, drop it.
if os.path.exists('fs/sus_su.c'):
    os.remove('fs/sus_su.c')
    out('OK  removed fs/sus_su.c (dead SUS_SU helper)')


def clean_fdinfo(s):
    if 'CONFIG_KSU_SUSFS' not in s:
        return None
    s2 = re.sub(r'#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\r?\n#include <linux/susfs_def\.h>\r?\n#endif\r?\n',
                '', s, count=1)
    if s2 == s:
        fatal('fdinfo.c: include block not found')
    s = s2
    pat = re.compile(
        r'#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\r?\n'
        r'static void show_fdinfo\(struct seq_file \*m, struct file \*f,\r?\n'
        r'[^\n]*\r?\n(?:[^\n]*\r?\n){0,2}'
        r'#else\r?\n'
        r'(static void show_fdinfo\(struct seq_file \*m, struct file \*f,\r?\n'
        r'[^\n]*\r?\n'
        r'[^\n]*\))\r?\n'
        r'#endif\r?\n')
    m = pat.search(s)
    if not m:
        fatal('fdinfo.c: show_fdinfo dual-branch not found')
    s = s[:m.start()] + m.group(1) + '\n' + s[m.end():]
    pat = re.compile(
        r'#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\r?\n'
        r'\t+show\(m, mark, f\);\r?\n'
        r'#else\r?\n'
        r'(\t+show\(m, mark\);)\r?\n'
        r'#endif\r?\n')
    m = pat.search(s)
    if not m:
        fatal('fdinfo.c: show() call dual-branch not found')
    s = s[:m.start()] + m.group(1) + '\n' + s[m.end():]
    pat = re.compile(
        r'#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\r?\n'
        r'static void inotify_fdinfo\(struct seq_file \*m, struct fsnotify_mark \*mark, struct file \*file\)\r?\n'
        r'#else\r?\n'
        r'(static void inotify_fdinfo\(struct seq_file \*m, struct fsnotify_mark \*mark\))\r?\n'
        r'#endif\r?\n')
    m = pat.search(s)
    if not m:
        fatal('fdinfo.c: inotify_fdinfo signature dual-branch not found')
    s = s[:m.start()] + m.group(1) + '\n' + s[m.end():]
    pat = re.compile(
        r'#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\r?\n'
        r'\t+if \(likely\(current->susfs_task_state & TASK_STRUCT_NON_ROOT_USER_APP_PROC\) &&\r?\n'
        r'.*?'
        r'out_seq_printf:\r?\n'
        r'#endif\r?\n', re.S)
    m = pat.search(s)
    if not m:
        fatal('fdinfo.c: function-body sus_mount block not found')
    s = s[:m.start()] + s[m.end():]
    if 'CONFIG_KSU_SUSFS' in s or 'susfs' in s:
        fatal('fdinfo.c: susfs trace remains after cleaning')
    return s


def clean_sys(s):
    if 'susfs_spoof_uname' not in s and 'linux/susfs.h' not in s:
        return None
    pat = re.compile(
        r'#ifdef CONFIG_KSU_SUSFS_SPOOF_UNAME\r?\n'
        r'\tsusfs_spoof_uname\(&tmp\);\r?\n'
        r'#endif\r?\n')
    s2, n = pat.subn('', s, count=1)
    if n != 1:
        fatal('sys.c: susfs_spoof_uname call block not found')
    s2 = re.sub(
        r'#ifdef CONFIG_KSU_SUSFS_SPOOF_UNAME\r?\nextern void susfs_spoof_uname\(struct new_utsname\* tmp\);\r?\n#endif\r?\n',
        '', s2, count=1)
    s2 = re.sub(r'#include <linux/susfs\.h>\r?\n', '', s2, count=1)
    if 'susfs' in s2:
        fatal('sys.c: susfs trace remains after cleaning')
    return s2


if need('fs/notify/fdinfo.c'):
    with ropen('fs/notify/fdinfo.c') as f:
        s = f.read()
    s2 = clean_fdinfo(s)
    if s2 is None:
        out('-- fdinfo.c already clean')
    else:
        with wopen('fs/notify/fdinfo.c') as f:
            f.write(s2)
        out('OK  fs/notify/fdinfo.c: cleaned')
if need('kernel/sys.c'):
    with ropen('kernel/sys.c') as f:
        s = f.read()
    s2 = clean_sys(s)
    if s2 is None:
        out('-- kernel/sys.c already clean')
    else:
        with wopen('kernel/sys.c') as f:
            f.write(s2)
        out('OK  kernel/sys.c: cleaned')
for p in ('fs/proc/task_mmu.c', 'fs/readdir.c'):
    if need(p):
        with ropen(p) as f:
            t = f.read()
        if 'susfs' in t or 'SUSFS' in t:
            remove_all_susfs_blocks(p)
        else:
            out('-- %s already clean' % p)

# ===========================================================================
next_step('residue assertion (no susfs trace outside susfs files)')
scan_roots = ['fs', 'include/linux', 'kernel', 'mm', 'security', 'block']
residue = []
for root in scan_roots:
    if not os.path.isdir(root):
        continue
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ('kernelsu',)]
        for fn in filenames:
            if not fn.endswith(('.c', '.h')):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with ropen(p) as f:
                    t = f.read()
            except OSError:
                continue
            if 'susfs' in t or 'SUSFS' in t:
                residue.append(p)
if residue:
    for p in residue:
        out('RESIDUE: %s' % p)
    fatal('susfs residue in %d file(s) after revert' % len(residue))
out('OK  no susfs residue')

# ===========================================================================
next_step('strip legacy bool-gate KSU hooks (5 files)')
LEGACY = [
    ('fs/exec.c', ['ksu_execveat_hook']),
    ('fs/open.c', ['ksu_handle_faccessat']),
    ('fs/read_write.c', ['ksu_vfs_read_hook', 'ksu_handle_vfs_read']),
    ('fs/stat.c', ['ksu_handle_stat']),
    ('drivers/input/input.c', ['ksu_input_hook']),
]
for p, needles in LEGACY:
    if need(p):
        with ropen(p) as f:
            before = f.read()
        if 'CONFIG_KSU' not in before:
            out('-- %s: no CONFIG_KSU blocks, skip' % p)
            continue
        strip_ksu_blocks(p, needles, 'strip legacy hook')
        with ropen(p) as f:
            after = f.read()
        for nd in needles:
            if nd in after:
                fatal('%s: %s still present after strip' % (p, nd))
out('OK  legacy hooks stripped')

# ===========================================================================
next_step('declare filename_lookup() in fs/internal.h')
if need('fs/internal.h'):
    with ropen('fs/internal.h') as f:
        ih = f.read()
    if 'filename_lookup' not in ih:
        idx = ih.rfind('#endif')
        if idx < 0:
            fatal('fs/internal.h: trailing #endif not found')
        ih = ih[:idx] + ('extern int filename_lookup(int dfd, struct filename *name, unsigned flags,\n'
                         '\t\t\t  struct path *path, struct path *root);\n\n') + ih[idx:]
        with wopen('fs/internal.h') as f:
            f.write(ih)
        out('OK  fs/internal.h: filename_lookup declared')
    else:
        out('OK  fs/internal.h: filename_lookup already declared')

# ===========================================================================
next_step('run susfs_inline_hook_patches.sh (7 ksu_handle_* call sites)')
hook_script = os.path.join(ASSET_DIR, 'susfs_inline_hook_patches.sh')
if not os.path.exists(hook_script):
    fatal('susfs_inline_hook_patches.sh not found in assets')
_env = dict(os.environ)
if os.environ.get('BASH_EXTRA_PATH'):
    _env['PATH'] = os.environ['BASH_EXTRA_PATH'] + os.pathsep + _env.get('PATH', '')
r = subprocess.run([os.environ.get('BASH', 'bash'), hook_script],
                   capture_output=True, text=True,
                   encoding='utf-8', errors='replace', env=_env)
out((r.stdout or '') + (r.stderr or ''))
# the script never exits non-zero; individual results checked in final verify

# ===========================================================================
next_step('apply susfs_patch_to_4.14.patch (v2.3.0 kernel side)')
jack_patch = os.path.join(ASSET_DIR, 'susfs_patch_to_4.14.patch')
with ropen(jack_patch) as f:
    jtext = f.read()
if re.search(r'(?m)^\+\+[^+]', jtext.split('diff --git', 1)[1] if 'diff --git' in jtext else jtext):
    fatal('Jack patch contains ^++ double-plus lines (upstream pollution)')
if re.search(r'(?m)^diff --git .*/\.git/', jtext):
    fatal('Jack patch contains .git/ sections (upstream pollution)')
run_patch_sections(jack_patch, '',
                   excludes=('fs/notify/fdinfo.c', 'kernel/sys.c'),
                   # task_mmu hunks 1/5/6 reject exactly like on rosemary
                   # (include + cosmetics); fix_susfs_task_mmu.py repairs them.
                   expect_fail=(('fs/proc/task_mmu.c', '3 out of 8 hunks FAILED'),))

for p in ('fs/susfs.c', 'include/linux/susfs.h', 'include/linux/susfs_def.h'):
    if not os.path.exists(p):
        fatal('%s missing after Jack patch (new-file section failed?)' % p)
    out('OK  %s present (%d bytes)' % (p, os.path.getsize(p)))
if need('fs/Makefile'):
    with ropen('fs/Makefile') as f:
        mk = f.read()
    if 'susfs.o' not in mk:
        fatal('fs/Makefile has no susfs.o after Jack patch')
    out('OK  fs/Makefile: susfs.o entry present')

# ===========================================================================
next_step('exec.c: drop susfs_is_sdcard_android_data_not_decrypted branch')
if need('fs/exec.c'):
    with ropen('fs/exec.c') as f:
        ex = f.read()
    if 'susfs_is_sdcard_android_data_not_decrypted' in ex:
        lines = ex.split('\n')
        # Operate ONLY inside the injected hook block: from the
        # susfs_is_current_proc_no_su() line to its following orig_flow: label.
        start = end = None
        for i, L in enumerate(lines):
            if 'susfs_is_current_proc_no_su()' in L:
                start = i
            elif start is not None and L.strip() == 'orig_flow:':
                end = i
                break
        if start is None or end is None:
            fatal('exec.c: hook block (proc_no_su .. orig_flow) not found')
        removed = 0
        keep = []
        for i, L in enumerate(lines):
            if start < i < end:
                if 'susfs_is_sdcard_android_data_not_decrypted' in L:
                    removed += 1
                    continue
                if re.match(r'^\s*is_su_session = !ksu_handle_execveat\(', L):
                    removed += 1
                    continue
                if L.strip() == 'else':
                    removed += 1
                    continue
            keep.append(L)
        ex = '\n'.join(keep)
        # extern declaration for the dropped static key (file scope)
        ex = re.sub(r'(?m)^extern struct static_key_true susfs_is_sdcard_android_data_not_decrypted;\r?\n', '', ex)
        if 'susfs_is_sdcard_android_data_not_decrypted' in ex:
            fatal('exec.c: sdcard symbol still present after cleanup')
        if removed < 3:
            fatal('exec.c: expected >=3 lines removed inside hook block, got %d' % removed)
        with wopen('fs/exec.c') as f:
            f.write(ex)
        out('OK  fs/exec.c: sdcard branch dropped (%d lines removed in block)' % removed)
        # sanity: the resulting sucompat call must still be there
        with ropen('fs/exec.c') as f:
            ex2 = f.read()
        if 'is_su_session = !ksu_handle_execveat_sucompat(&fd, &filename, &argv, &envp, &flags);' not in ex2:
            fatal('exec.c: sucompat call missing after cleanup')
        out('OK  fs/exec.c: sucompat call retained')
    else:
        out('-- fs/exec.c: no sdcard symbol, skip')

# ===========================================================================
next_step('input.c: add missing hook call (script anchor misses 4.14)')
if need('drivers/input/input.c'):
    with ropen('drivers/input/input.c') as f:
        ip = f.read()
    if 'ksu_is_input_hook_enabled))' in ip:
        out('-- input.c: call already present, skip')
    else:
        anchor = 'input_get_disposition(dev, type, code, &value);'
        ins = ('#ifdef CONFIG_KSU_SUSFS\n'
               '\tif (static_branch_unlikely(&ksu_is_input_hook_enabled))\n'
               '\t\tksu_handle_input_handle_event(&type, &code, &value);\n'
               '#endif\n')
        insert_after('drivers/input/input.c', anchor, ins,
                     'input hook call (4.14 anchor)')
        with ropen('drivers/input/input.c') as f:
            ip2 = f.read()
        if 'ksu_handle_input_handle_event(&type, &code, &value);' not in ip2:
            fatal('input.c: call insertion failed')
        out('OK  input.c: call verified')

# ===========================================================================
next_step('fdinfo.c: manual port of rejected Hunk #4')
FDINFO_ANCHOR = ('inode_mark = container_of(mark, struct inotify_inode_mark, fsn_mark);\n'
                 '\tinode = igrab(mark->connector->inode);\n'
                 '\tif (inode) {\n')
FDINFO_INS = (
    '#ifdef CONFIG_KSU_SUSFS_SUS_KSTAT\n'
    '\t\tif (susfs_is_current_app_uid()) {\n'
    '\t\t\tbool is_fuse = false;\n'
    '\t\t\tif (susfs_is_inode_sus_kstat(inode, &is_fuse)) {\n'
    '\t\t\t\tunsigned long ino = inode->i_ino;\n'
    '\t\t\t\tdev_t dev = inode->i_sb->s_dev;\n'
    '\t\t\t\tsusfs_sus_kstat_spoof_inotify_fdinfo(&ino, &dev);\n'
    '\t\t\t\tseq_printf(m, "inotify wd:%x ino:%lx sdev:%x mask:%x ignored_mask:0 ",\n'
    '\t\t\t\t\t\tinode_mark->wd, ino, dev,\n'
    '\t\t\t\t\t\t(mark->mask & IN_ALL_EVENTS));\n'
    '\t\t\t\tshow_mark_fhandle(m, inode);\n'
    '\t\t\t\tseq_putc(m, \'\\n\');\n'
    '\t\t\t\tiput(inode);\n'
    '\t\t\t\treturn;\n'
    '\t\t\t}\n'
    '\t\t}\n'
    '#endif\n'
    '#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\n'
    '\t\tif (likely(susfs_is_current_proc_umounted())) {\n'
    '\t\t\tstruct mount *mnt = real_mount(file->f_path.mnt);\n'
    '\t\t\tif (mnt->mnt_id >= DEFAULT_KSU_MNT_ID) {\n'
    '\t\t\t\tstruct path path;\n'
    '\t\t\t\tchar *pathname = kmalloc(PAGE_SIZE, GFP_KERNEL);\n'
    '\t\t\t\tchar *dpath;\n'
    '\t\t\t\tif (!pathname) {\n'
    '\t\t\t\t\tgoto orig_flow;\n'
    '\t\t\t\t}\n'
    '\t\t\t\tdpath = d_path(&file->f_path, pathname, PAGE_SIZE);\n'
    '\t\t\t\tif (!dpath) {\n'
    '\t\t\t\t\tgoto out_kfree;\n'
    '\t\t\t\t}\n'
    '\t\t\t\tif (kern_path(dpath, 0, &path)) {\n'
    '\t\t\t\t\tgoto out_kfree;\n'
    '\t\t\t\t}\n'
    '\t\t\t\tif (!d_backing_inode(path.dentry)) {\n'
    '\t\t\t\t\tgoto out_path_put;\n'
    '\t\t\t\t}\n'
    '\t\t\t\tseq_printf(m, "inotify wd:%x ino:%lx sdev:%x mask:%x ignored_mask:0 ",\n'
    '\t\t\t\t\t\tinode_mark->wd, d_backing_inode(path.dentry)->i_ino, d_backing_inode(path.dentry)->i_sb->s_dev,\n'
    '\t\t\t\t\t\t(mark->mask & IN_ALL_EVENTS));\n'
    '\t\t\t\tshow_mark_fhandle(m, d_backing_inode(path.dentry));\n'
    '\t\t\t\tseq_putc(m, \'\\n\');\n'
    '\t\t\t\tpath_put(&path);\n'
    '\t\t\t\tkfree(pathname);\n'
    '\t\t\t\tiput(inode);\n'
    '\t\t\t\treturn;\n'
    'out_path_put:\n'
    '\t\t\t\tpath_put(&path);\n'
    'out_kfree:\n'
    '\t\t\t\tkfree(pathname);\n'
    '\t\t\t}\n'
    '\t\t}\n'
    'orig_flow:\n'
    '#endif\n')
if need('fs/notify/fdinfo.c'):
    with ropen('fs/notify/fdinfo.c') as f:
        fd = f.read()
    if 'orig_flow:' in fd:
        out('-- fdinfo.c: manual port already present, skip')
    else:
        insert_after('fs/notify/fdinfo.c', FDINFO_ANCHOR, FDINFO_INS,
                     'inotify_fdinfo SUS_KSTAT/SUS_MOUNT blocks')
        with ropen('fs/notify/fdinfo.c') as f:
            fd2 = f.read()
        for marker in ('susfs_is_current_app_uid', 'susfs_is_current_proc_umounted',
                       'orig_flow:'):
            if marker not in fd2:
                fatal('fdinfo.c: expected marker %r missing after port' % marker)
        if 'inotify_mark_user_mask' in fd2:
            fatal('fdinfo.c: 4.20 API inotify_mark_user_mask leaked into port')
        out('OK  fdinfo.c: manual port verified')

# ===========================================================================
next_step('kernel/sys.c: manual port of SPOOF_UNAME (bpfloader context)')
if need('kernel/sys.c'):
    with ropen('kernel/sys.c') as f:
        sy = f.read()
    if 'susfs_is_uname_spoof_buffer_set' in sy:
        out('-- sys.c: uname port already present, skip')
    else:
        uname_fn = 'SYSCALL_DEFINE1(newuname, struct new_utsname __user *, name)'
        k = sy.find(uname_fn)
        if k < 0:
            fatal('sys.c: newuname not found')
        # extern block goes right before the SYSCALL_DEFINE1 line
        line_start = sy.rfind('\n', 0, k) + 1
        ext = ('#ifdef CONFIG_KSU_SUSFS_SPOOF_UNAME\n'
               'extern struct static_key_false susfs_is_uname_spoof_buffer_set;\n'
               'extern void susfs_spoof_uname(struct new_utsname* tmp);\n'
               '#endif\n')
        sy = sy[:line_start] + ext + sy[line_start:]
        # call block goes right after the memcpy line inside newuname
        mc = 'memcpy(&tmp, utsname(), sizeof(tmp));'
        k2 = sy.find(mc, k)
        if k2 < 0:
            fatal('sys.c: memcpy in newuname not found')
        line_end = sy.find('\n', k2) + 1
        call = ('#ifdef CONFIG_KSU_SUSFS_SPOOF_UNAME\n'
                '\tif (static_branch_likely(&susfs_is_uname_spoof_buffer_set))\n'
                '\t\tsusfs_spoof_uname(&tmp);\n'
                '#endif\n')
        sy = sy[:line_end] + call + sy[line_end:]
        with wopen('kernel/sys.c') as f:
            f.write(sy)
        out('OK  kernel/sys.c: SPOOF_UNAME ported')
        with ropen('kernel/sys.c') as f:
            sy2 = f.read()
        for marker in ('susfs_is_uname_spoof_buffer_set', 'susfs_spoof_uname(&tmp);'):
            if marker not in sy2:
                fatal('sys.c: marker %r missing after port' % marker)

# ===========================================================================
next_step('run rosemary fixers (task_mmu + 4.14 stat/statfs/susfs/fd/super/namespace)')
for fx in ('fix_susfs_task_mmu.py', 'fix_susfs_414.py'):
    fp = os.path.join(ASSET_DIR, fx)
    if not os.path.exists(fp):
        fatal('%s not found in assets' % fx)
    if ALLOW_MISSING and not os.path.exists('fs/proc/task_mmu.c') and fx == 'fix_susfs_task_mmu.py':
        out('-- SKIP fixer (simulation, no fs/proc)')
        continue
    if ALLOW_MISSING and not os.path.exists('fs/statfs.c') and fx == 'fix_susfs_414.py':
        out('-- SKIP fixer (simulation, no fs/statfs.c)')
        continue
    r = subprocess.run([sys.executable, fp], capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    out('--- %s ---' % fx)
    out((r.stdout or '') + (r.stderr or ''))
    if r.returncode != 0:
        fatal('%s exited %d' % (fx, r.returncode))
# fixer repaired the include + guard; drop the tolerated reject files
for rej in ('fs/proc/task_mmu.c.rej', 'patch_rejects/fs/proc/task_mmu.c.rej'):
    if os.path.exists(rej):
        os.remove(rej)
        out('OK  removed tolerated reject %s' % rej)
if need('fs/proc/task_mmu.c'):
    with ropen('fs/proc/task_mmu.c') as f:
        tm = f.read()
    if 'susfs_def.h' not in tm:
        fatal('task_mmu.c: susfs_def.h include missing after fixer')
    if 'SUSFS_IS_INODE_SUS_MAP' not in tm:
        fatal('task_mmu.c: SUS_MAP guard missing after fixer')
    out('OK  task_mmu.c: include + SUS_MAP guard verified')

# ===========================================================================
next_step('final verification')
CHECKS = [
    ('ksu_handle_execveat', 'fs/exec.c'),
    ('ksu_handle_faccessat', 'fs/open.c'),
    ('ksu_handle_sys_read', 'fs/read_write.c'),
    ('ksu_handle_stat', 'fs/stat.c'),
    ('ksu_handle_sys_reboot', 'kernel/reboot.c'),
    ('ksu_handle_input_handle_event', 'drivers/input/input.c'),
    ('ksu_handle_setresuid', 'kernel/sys.c'),
]
for sym, p in CHECKS:
    if not need(p):
        continue
    with ropen(p) as f:
        t = f.read()
    if sym not in t:
        fatal('inline hook missing: %s in %s' % (sym, p))
    out('OK  hook present: %s in %s' % (sym, p))

FORBIDDEN = ['ksu_vfs_read_hook', 'ksu_input_hook', 'ksu_execveat_hook', 'ksu_init_rc_hook']
for p in ('fs/exec.c', 'fs/open.c', 'fs/read_write.c', 'fs/stat.c',
          'drivers/input/input.c', 'kernel/reboot.c', 'kernel/sys.c'):
    if not need(p):
        continue
    with ropen(p) as f:
        t = f.read()
    for sym in FORBIDDEN:
        if re.search(r'\b%s\b' % re.escape(sym), t):
            fatal('forbidden legacy symbol %s in %s' % (sym, p))
    if 'CONFIG_KSU_MANUAL_HOOK' in t:
        fatal('CONFIG_KSU_MANUAL_HOOK guard text in %s (module manual-guard check would fail)' % p)
out('OK  no forbidden symbols / no MANUAL_HOOK guard text in 7 hook files')

if need('drivers/input/input.c'):
    with ropen('drivers/input/input.c') as f:
        t = f.read()
    if 'ksu_handle_input_handle_event(&type, &code, &value);' not in t:
        fatal('input.c: hook call missing')
if need('fs/exec.c'):
    with ropen('fs/exec.c') as f:
        t = f.read()
    if 'susfs_is_sdcard_android_data_not_decrypted' in t:
        fatal('exec.c: sdcard symbol still present')
out('OK  post-fix markers verified')

rej = []
for dirpath, dirnames, filenames in os.walk('.'):
    dirnames[:] = [d for d in dirnames if d not in ('.git', 'KernelSU')]
    for fn in filenames:
        if fn.endswith('.rej') or fn.endswith('.orig'):
            rej.append(os.path.join(dirpath, fn))
if rej:
    for p in rej:
        out('LEFTOVER: %s' % p)
    fatal('%d .rej/.orig leftover file(s)' % len(rej))
out('OK  no .rej/.orig leftovers')

if need('include/linux/susfs_def.h'):
    with ropen('include/linux/susfs_def.h') as f:
        t = f.read()
    for sym in ('susfs_is_current_app_uid', 'susfs_is_current_proc_umounted',
                'DEFAULT_KSU_MNT_ID', 'susfs_is_current_proc_no_su'):
        if sym not in t:
            fatal('susfs_def.h: expected symbol %s missing' % sym)
    out('OK  susfs_def.h v2.3.0 API verified')

out('')
out('PATCH CHAIN COMPLETE - all %d steps OK' % STEP[0])
