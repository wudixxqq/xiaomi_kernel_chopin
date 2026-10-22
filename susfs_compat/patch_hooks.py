#!/usr/bin/env python3
"""
susfs_compat patch script for the chopin 4.14 kernel + BakaSU (CONFIG_KSU_SUSFS).

Transforms the legacy KSU manual-style hook call sites into the susfs v2 style
expected by the BakaSU kernel module when built with CONFIG_KSU_SUSFS=y, and
un-statics filename_lookup() so fs/open.c and fs/stat.c can resolve
`struct filename *` paths.

Run from the kernel source root:  python3 <this script>
"""
import os
import re
import sys

def ropen(p):
    return open(p, encoding='utf-8', errors='surrogateescape')

def wopen(p):
    return open(p, 'w', encoding='utf-8', errors='surrogateescape')

def sub_once(path, pattern, repl, desc, count=1, flags=0):
    """regex replace with strict assertion"""
    with ropen(path) as f:
        src = f.read()
    new, n = re.subn(pattern, repl, src, flags=flags)
    if n != count:
        print('FATAL: %s: expected %d match(es) for %s, found %d' % (path, count, desc, n))
        sys.exit(1)
    with wopen(path) as f:
        f.write(new)
    print('OK  %s: %s' % (path, desc))

def insert_after(path, anchor, insertion, desc, count=1):
    with ropen(path) as f:
        src = f.read()
    n = src.count(anchor)
    if n != count:
        print('FATAL: %s: expected %d anchor(s) for %s, found %d' % (path, count, desc, n))
        sys.exit(1)
    new = src.replace(anchor, anchor + insertion)
    with wopen(path) as f:
        f.write(new)
    print('OK  %s: %s' % (path, desc))

def strip_ksu_blocks(path, needles, desc):
    """remove #ifdef CONFIG_KSU ... #endif blocks containing any needle"""
    with ropen(path) as f:
        lines = f.readlines()
    block_start = re.compile(r'#\s*ifdef\s+CONFIG_KSU\s*$')
    out, i, removed = [], 0, 0
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
        out.append(lines[i])
        i += 1
    if removed == 0:
        print('FATAL: %s: no block removed for %s' % (path, desc))
        sys.exit(1)
    with wopen(path) as f:
        f.writelines(out)
    print('OK  %s: %s (removed %d block(s))' % (path, desc, removed))

# ---------------------------------------------------------------------------
# 0. drivers/kernelsu/Kconfig: decouple the hook-method choice and drop the
#    THREAD_INFO_IN_TASK dependency (4.14 arm64 does not select it, which
#    would make KSU_SUSFS invisible to olddefconfig).
# ---------------------------------------------------------------------------
p = 'drivers/kernelsu/Kconfig'
with ropen(p) as f:
    lines = f.readlines()
out, i, n = [], 0, len(lines)
decoupled = 0
while i < n:
    if lines[i].strip() == 'choice' and i + 1 < n and 'KernelSU Hooking Method' in lines[i + 1]:
        j = i
        while j < n and lines[j].strip() != 'endchoice':
            j += 1
        assert j < n, 'endchoice not found'
        k = i
        while k < j and not lines[k].strip().startswith('config '):
            k += 1
        assert k < j, 'no config inside choice'
        out.extend(lines[k:j])
        decoupled += 1
        i = j + 1
        continue
    out.append(lines[i])
    i += 1
assert decoupled == 1, 'hook-method choice not found'
txt = ''.join(out)
txt = re.sub(r'(?m)^(\s*depends on[^\n]*)THREAD_INFO_IN_TASK\s*&&\s*', r'\1', txt)
txt = re.sub(r'(?m)^(\s*depends on[^\n]*)\s*&&\s*THREAD_INFO_IN_TASK', r'\1', txt)
txt = re.sub(r'(?m)^(\s*depends on\s+)THREAD_INFO_IN_TASK\s*$', r'\1', txt)
assert 'config KSU_MANUAL_HOOK' in txt and 'config KSU_SUSFS' in txt
assert not re.search(r'(?m)^\s*choice\s*$', txt)
assert 'THREAD_INFO_IN_TASK' not in txt, 'THREAD_INFO_IN_TASK dependency still present'
with wopen(p) as f:
    f.write(txt)
print('OK  drivers/kernelsu/Kconfig: choice decoupled, THREAD_INFO_IN_TASK dep removed')

# ---------------------------------------------------------------------------
# 1. fs/namei.c + fs/internal.h: expose filename_lookup()
# ---------------------------------------------------------------------------
sub_once('fs/namei.c',
         r'static int filename_lookup\(int dfd, struct filename \*name, unsigned flags,',
         'int filename_lookup(int dfd, struct filename *name, unsigned flags,',
         'un-static filename_lookup')

with ropen('fs/internal.h') as f:
    ih = f.read()
if 'filename_lookup' not in ih:
    idx = ih.rfind('#endif')
    if idx < 0:
        print('FATAL: fs/internal.h: trailing #endif not found')
        sys.exit(1)
    ih = ih[:idx] + ('extern int filename_lookup(int dfd, struct filename *name, unsigned flags,\n'
                     '\t\t\t  struct path *path, struct path *root);\n\n') + ih[idx:]
    with wopen('fs/internal.h') as f:
        f.write(ih)
    print('OK  fs/internal.h: filename_lookup declared')
else:
    print('OK  fs/internal.h: filename_lookup already declared')

# ---------------------------------------------------------------------------
# 2. fs/exec.c
# ---------------------------------------------------------------------------
sub_once('fs/exec.c',
         r'#ifdef CONFIG_KSU\nextern bool ksu_execveat_hook __read_mostly;\nextern int ksu_handle_execveat\(int \*fd, struct filename \*\*filename_ptr, void \*argv,\n[\t ]*void \*envp, int \*flags\);\nextern int ksu_handle_execveat_sucompat\(int \*fd, struct filename \*\*filename_ptr,\n[\t ]*void \*argv, void \*envp, int \*flags\);\n#endif\n',
         '#ifdef CONFIG_KSU\n'
         '#include <linux/susfs_def.h>\n'
         'extern struct static_key_true ksu_su_compat_enabled;\n'
         'extern int ksu_handle_execveat(int *fd, struct filename **filename_ptr, void *argv,\n'
         '\t\t\tvoid *envp, int *flags);\n'
         'extern int ksu_handle_post_execveat(int *fd, struct filename **filename_ptr,\n'
         '\t\t\tvoid *argv, void *envp, int *flags, int *retval);\n'
         '#endif\n',
         'exec externs -> susfs style')

sub_once('fs/exec.c',
         r'#ifdef CONFIG_KSU\n\tif \(unlikely\(ksu_execveat_hook\)\)\n\t\tksu_handle_execveat\(&fd, &filename, &argv, &envp, &flags\);\n\telse\n\t\tksu_handle_execveat_sucompat\(&fd, &filename, &argv, &envp, &flags\);\n#endif\n',
         '',
         'remove old execveat bool gate')

insert_after('fs/exec.c',
             '\tif (IS_ERR(filename))\n\t\treturn PTR_ERR(filename);\n',
             '#ifdef CONFIG_KSU\n'
             '\tif (likely(!susfs_is_current_proc_no_su()) &&\n'
             '\t    static_branch_likely(&ksu_su_compat_enabled))\n'
             '\t\tis_su_session = !ksu_handle_execveat(&fd, &filename, &argv, &envp, &flags);\n'
             '#endif\n',
             'exec susfs hook after IS_ERR check')

sub_once('fs/exec.c',
         r'\tstruct files_struct \*displaced;\n\tint retval;\n',
         '\tstruct files_struct *displaced;\n\tint retval;\n'
         '#ifdef CONFIG_KSU\n\tbool is_su_session = false;\n#endif\n',
         'declare is_su_session')

insert_after('fs/exec.c',
             '\tretval = exec_binprm(bprm);\n',
             '#ifdef CONFIG_KSU\n'
             '\tif (unlikely(is_su_session))\n'
             '\t\t(void)ksu_handle_post_execveat(&fd, &filename, &argv, &envp, &flags, &retval);\n'
             '#endif\n',
             'exec post_execveat hook')

# ---------------------------------------------------------------------------
# 3. fs/open.c
# ---------------------------------------------------------------------------
sub_once('fs/open.c',
         r'#ifdef CONFIG_KSU\nextern int ksu_handle_faccessat\(int \*dfd, const char __user \*\*filename_user, int \*mode,\n[\t ]*int \*flags\);\n#endif\n',
         '#ifdef CONFIG_KSU\n'
         '#include <linux/susfs_def.h>\n'
         'extern struct static_key_true ksu_su_compat_enabled;\n'
         'extern bool __ksu_is_allow_uid_for_current(uid_t uid);\n'
         'extern int ksu_handle_faccessat(int *dfd, struct filename **filename, int *mode,\n'
         '\t\t\t int *__unused_flags);\n'
         '#endif\n',
         'faccessat externs -> susfs style')

sub_once('fs/open.c',
         r'#ifdef CONFIG_KSU\n\tksu_handle_faccessat\(&dfd, &filename, &mode, NULL\);\n#endif\n',
         '',
         'remove early faccessat call')

sub_once('fs/open.c',
         r'retry:\n\tres = user_path_at\(dfd, filename, lookup_flags, &path\);',
         'retry:\n'
         '#ifdef CONFIG_KSU\n'
         '\tfname = getname_flags(filename, lookup_flags, NULL);\n'
         '\tif (likely(!susfs_is_current_proc_no_su()) &&\n'
         '\t    static_branch_likely(&ksu_su_compat_enabled)) {\n'
         '\t\tif (unlikely(__ksu_is_allow_uid_for_current(current_uid().val)))\n'
         '\t\t\tksu_handle_faccessat(&dfd, &fname, &mode, NULL);\n'
         '\t}\n'
         '\tres = filename_lookup(dfd, fname, lookup_flags, &path, NULL);\n'
         '\t/* no putname(fname) here as filename_lookup() does it for us */\n'
         '#else\n'
         '\tres = user_path_at(dfd, filename, lookup_flags, &path);\n'
         '#endif',
         'faccessat lookup -> filename_lookup')

insert_after('fs/open.c',
             '\tint res;\n\tunsigned int lookup_flags = LOOKUP_FOLLOW;\n',
             '#ifdef CONFIG_KSU\n\tstruct filename *fname = NULL;\n#endif\n',
             'declare fname in faccessat')

# ---------------------------------------------------------------------------
# 4. fs/stat.c
# ---------------------------------------------------------------------------
sub_once('fs/stat.c',
         r'#ifdef CONFIG_KSU\nextern int ksu_handle_stat\(int \*dfd, const char __user \*\*filename_user, int \*flags\);\n#endif\n',
         '#ifdef CONFIG_KSU\n'
         '#include <linux/susfs_def.h>\n'
         'extern struct static_key_true ksu_su_compat_enabled;\n'
         'extern bool __ksu_is_allow_uid_for_current(uid_t uid);\n'
         'extern int ksu_handle_stat(int *dfd, struct filename **filename, int *flags);\n'
         '#endif\n',
         'stat externs -> susfs style')

sub_once('fs/stat.c',
         r'#ifdef CONFIG_KSU\n\tksu_handle_stat\(&dfd, &filename, &flags\);\n#endif\n',
         '',
         'remove early stat call')

insert_after('fs/stat.c',
             '\tunsigned int lookup_flags = LOOKUP_FOLLOW | LOOKUP_AUTOMOUNT;\n',
             '#ifdef CONFIG_KSU\n\tstruct filename *fname = NULL;\n#endif\n',
             'declare fname in vfs_statx')

sub_once('fs/stat.c',
         r'retry:\n\terror = user_path_at\(dfd, filename, lookup_flags, &path\);',
         'retry:\n'
         '#ifdef CONFIG_KSU\n'
         '\tfname = getname_flags(filename, lookup_flags, NULL);\n'
         '\tif (likely(!susfs_is_current_proc_no_su()) &&\n'
         '\t    static_branch_likely(&ksu_su_compat_enabled)) {\n'
         '\t\tif (unlikely(__ksu_is_allow_uid_for_current(current_uid().val)))\n'
         '\t\t\tksu_handle_stat(&dfd, &fname, &flags);\n'
         '\t}\n'
         '\terror = filename_lookup(dfd, fname, lookup_flags, &path, NULL);\n'
         '\t/* no putname(fname) here as filename_lookup() does it for us */\n'
         '#else\n'
         '\terror = user_path_at(dfd, filename, lookup_flags, &path);\n'
         '#endif',
         'statx lookup -> filename_lookup')

# NOTE: no vfs_fstat hook on 4.14 - this tree does not define vfs_fstat() in
# fs/stat.c, the module does not require it (inline_hook_check.mk does not
# check it), and ksu_handle_sys_read() already covers init.rc detection.

# ---------------------------------------------------------------------------
# 5. fs/read_write.c
# ---------------------------------------------------------------------------
strip_ksu_blocks('fs/read_write.c',
                 ['ksu_vfs_read_hook', 'ksu_handle_vfs_read', 'ksu_init_rc_hook'],
                 'strip legacy vfs_read hook')

sub_once('fs/read_write.c',
         r'SYSCALL_DEFINE3\(read, unsigned int, fd, char __user \*, buf, size_t, count\)\n\{\n',
         '#ifdef CONFIG_KSU\n'
         'extern struct static_key_true ksu_is_init_rc_hook_enabled;\n'
         'extern __attribute__((cold)) void ksu_handle_sys_read(unsigned int fd);\n'
         '#endif\n'
         'SYSCALL_DEFINE3(read, unsigned int, fd, char __user *, buf, size_t, count)\n{\n'
         '#ifdef CONFIG_KSU\n'
         '\tif (static_branch_unlikely(&ksu_is_init_rc_hook_enabled))\n'
         '\t\tksu_handle_sys_read(fd);\n'
         '#endif\n',
         'sys_read externs + hook')

# ---------------------------------------------------------------------------
# 6. kernel/sys.c
# ---------------------------------------------------------------------------
insert_after('kernel/sys.c',
             'SYSCALL_DEFINE3(setresuid, uid_t, ruid, uid_t, euid, uid_t, suid)\n{\n',
             '#ifdef CONFIG_KSU\n\t(void)ksu_handle_setresuid(ruid, euid, suid);\n#endif\n',
             'setresuid hook')

# ---------------------------------------------------------------------------
# 7. kernel/reboot.c
# ---------------------------------------------------------------------------
with ropen('kernel/reboot.c') as f:
    rb = f.read()
if 'ksu_handle_sys_reboot' not in rb:
    idx = rb.index('SYSCALL_DEFINE4(reboot')
    lb = rb.rfind('\n', 0, idx) + 1
    rb = rb[:lb] + ('#ifdef CONFIG_KSU\n'
                    'extern int ksu_handle_sys_reboot(int magic1, int magic2, unsigned int cmd, void __user **arg);\n'
                    '#endif\n') + rb[lb:]
    idx2 = rb.index('SYSCALL_DEFINE4(reboot')
    a2 = 'int ret = 0;'
    k = rb.index(a2, idx2) + len(a2)
    ls = rb.find('\n', k) + 1  # insert AFTER the "int ret = 0;" line
    rb = rb[:ls] + ('#ifdef CONFIG_KSU\n'
                    '\tret = ksu_handle_sys_reboot(magic1, magic2, cmd, &arg);\n'
                    '\tif (ret) {\n'
                    '\t\tgoto orig_flow;\n'
                    '\t}\n'
                    '\treturn ret;\n'
                    'orig_flow:\n'
                    '#endif\n') + rb[ls:]
    with wopen('kernel/reboot.c') as f:
        f.write(rb)
    print('OK  kernel/reboot.c: sys_reboot supercall entry')
else:
    print('OK  kernel/reboot.c: already patched')

# ---------------------------------------------------------------------------
# 8. drivers/input/input.c
# ---------------------------------------------------------------------------
sub_once('drivers/input/input.c',
         r'#ifdef CONFIG_KSU\nextern bool ksu_input_hook __read_mostly;\n',
         '#ifdef CONFIG_KSU\n'
         'extern struct static_key_true ksu_is_input_hook_enabled;\n',
         'input externs -> susfs style')

sub_once('drivers/input/input.c',
         r'#ifdef CONFIG_KSU\n\tif \(unlikely\(ksu_input_hook\)\)\n\t\tksu_handle_input_handle_event\(&type, &code, &value\);\n#endif\n',
         '#ifdef CONFIG_KSU\n'
         '\tif (static_branch_unlikely(&ksu_is_input_hook_enabled))\n'
         '\t\tksu_handle_input_handle_event(&type, &code, &value);\n'
         '#endif\n',
         'input hook -> static key')

# ---------------------------------------------------------------------------
# 9. fs/notify/fdinfo.c
# ---------------------------------------------------------------------------
# 9a. susfs patch leaves `out_seq_printf:` label directly before a variable
#     declaration, which is illegal C before C23 - insert an empty statement.
sub_once('fs/notify/fdinfo.c',
         r'out_seq_printf:\n#endif',
         'out_seq_printf:\n\t;\n#endif',
         'fdinfo out_seq_printf label fix')

# 9b. align fanotify_fdinfo with the 3-param show_fdinfo
sub_once('fs/notify/fdinfo.c',
         r'static void fanotify_fdinfo\(struct seq_file \*m, struct fsnotify_mark \*mark\)\n\{\n',
         'static void fanotify_fdinfo(struct seq_file *m, struct fsnotify_mark *mark, struct file *file)\n{\n\t(void)file;\n',
         'fanotify_fdinfo 3-param signature')

# ---------------------------------------------------------------------------
# 10. fs/Makefile: build the compat shim
# ---------------------------------------------------------------------------
sub_once('fs/Makefile',
         r'obj-\$\(CONFIG_KSU_SUSFS\) \+= susfs\.o',
         'obj-$(CONFIG_KSU_SUSFS) += susfs.o susfs_compat.o',
         'fs/Makefile susfs_compat.o')

# ---------------------------------------------------------------------------
# 11. verification: required hooks present, forbidden symbols absent
# ---------------------------------------------------------------------------
required = {
    'fs/exec.c': ['ksu_handle_execveat(&fd', 'ksu_handle_post_execveat(&fd',
                  'ksu_su_compat_enabled', 'susfs_is_current_proc_no_su'],
    'fs/open.c': ['ksu_handle_faccessat(&dfd', 'filename_lookup(dfd, fname',
                  '__ksu_is_allow_uid_for_current'],
    'fs/stat.c': ['ksu_handle_stat(&dfd', 'filename_lookup(dfd, fname'],
    'fs/read_write.c': ['ksu_handle_sys_read(fd', 'ksu_is_init_rc_hook_enabled'],
    'kernel/sys.c': ['ksu_handle_setresuid(ruid'],
    'kernel/reboot.c': ['ksu_handle_sys_reboot(magic1', 'orig_flow'],
    'drivers/input/input.c': ['ksu_handle_input_handle_event(&type',
                              'ksu_is_input_hook_enabled'],
    'fs/namei.c': ['int filename_lookup(int dfd'],
    'fs/internal.h': ['filename_lookup'],
}
for path, needles in required.items():
    with ropen(path) as f:
        src = f.read()
    for nd in needles:
        if nd not in src:
            print('FATAL: %s missing required: %s' % (path, nd))
            sys.exit(1)
    print('OK  verify', path)

forbidden = ['ksu_execveat_hook', 'ksu_vfs_read_hook', 'ksu_input_hook',
             'ksu_handle_vfs_read', 'ksu_init_rc_hook',
             'ksu_handle_execveat_sucompat', 'CONFIG_KSU_MANUAL_HOOK']
for path in required:
    with ropen(path) as f:
        for ln, line in enumerate(f, 1):
            for nd in forbidden:
                if nd in line:
                    print('FATAL: %s:%d forbidden symbol %s: %s' % (path, ln, nd, line.strip()))
                    sys.exit(1)
print('OK  verify: no forbidden symbols')

print('ALL HOOK PATCHES APPLIED')
