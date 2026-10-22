# SUSFS 兼容层（chopin 4.14 + BakaSU）

> 状态：**已验证可用** — run #21 构建成功，产物 Release `build-20261022-031111`
> 内核：4.14.186-g0dc1d312efb3（分支 4.14.186-A13）｜ 模块：BakaSU CI 35236 / commit `dac2d186406a0d2b0ce3c67cad5a1fa1adbf9f4d`（SUSFS hook 模式）
> susfs 内核侧：官方 kernel-4.14 分支 v1.5.5（NON-GKI）+ 本目录的 **v2 命令分发兼容层**

## 这是什么

BakaSU/ReSukiSU 内核模块的 `CONFIG_KSU_SUSFS` 模式要求 susfs **v2.x 命令分发接口**
（`ksu_handle_susfs_cmd` → 14 个 `susfs_*(void __user **arg)` 处理函数），而 4.14
NON-GKI 内核只有官方 susfs4ksu `kernel-4.14` 分支的 v1.5.5 旧式直调实现
（simonpunk 已声明 Non-GKI 不再支持，官方要求自行回移 `gki-android12-5.10`）。

本目录 = **方案 A：兼容层桥接**。保留内核树 v1.5.5 的全部隐藏机制
（INODE_STATE_* / sus mnt_id / kstat hlist），新增一个翻译层把 v2 ABI 翻译到 v1 core：

```
BakaSU 管理器 (v35236)
   │ reboot(magic1, SUSFS_MAGIC=0xFAFAFAFA, cmd, arg)
   ▼
KSU 内核模块 (新式分发接口 ×14)
   ▼
fs/susfs_compat.c  ← 本目录：v2 ABI → v1 core 翻译
   ▼
fs/susfs.c (v1.5.5, *_info 内核态 core)
   ▼
fs/*.c 钩子（SUS_PATH / SUS_MOUNT / SUS_KSTAT / OPEN_REDIRECT / UNAME / CMDLINE ...）
```

## 文件清单

| 文件 | 说明 |
|---|---|
| `susfs_def.h` | 合并版：v1 基座 + v2 的 SUSFS_MAGIC / CMD 号 / TIF_PROC_* 线程标志；static inline **双写**（TIF + `task_struct->susfs_task_state` 位），内核 fs/*.c 零改动 |
| `susfs.h` | v2 分发函数声明 + `*_info` 内核态 core 声明；`SUSFS_VERSION` 保持 `v1.5.5` |
| `susfs.c` | v1.5.5 重构：6 个入口拆成 `*_info` 内核态 core（去 `huge_decode_dev`），`susfs_init()` 末尾调用 `susfs_compat_init()` |
| `susfs_compat.c` | **v2 分发 shim**：14 个 `void __user **arg` 处理函数 + `susfs_extra_works`（sus_path 重打标）+ features/version/variant 上报 + SUS_MAP/AVC/sdcard monitor 占位 |
| `patch_hooks.py` | CI 钩子补丁脚本（fail-fast 断言），见下文"它做什么" |
| `build.yml` | 同步自 `.github/workflows/build.yml` 的工作流副本 |

构建时工作流会：`patch_hooks.py`（改内核钩子 + 模块 Kconfig/闸门）→ 覆盖上述 4 个
susfs 文件到内核树 → defconfig 断言 → 编译。

## patch_hooks.py 做了什么

**模块侧（drivers/kernelsu，经 symlink 实体在 `KernelSU/kernel`）：**
1. Kconfig：解体 hook-method choice（MANUAL_HOOK/SUSFS/TRACEPOINT 变独立 bool）+ 删除 `KSU_SUSFS` 的 `depends on THREAD_INFO_IN_TASK`（4.14 arm64 无此符号，否则 olddefconfig 静默丢弃）
2. `tools/susfs_compat.mk`：删除 BakaSU 的无条件 `$(error Please wait susfs update.)` 闸门（作者等上游时临时封禁 SUSFS 构建；本兼容层就是内核侧）。⚠️ 该行是文件最后一行**无结尾换行**，且实体在 `KernelSU/kernel/tools/`（Kbuild 的 `KSU_SRC` 用 `realpath` 解析），必须双路径 patch + 写后重读验证

**内核侧：**
3. `fs/namei.c` + `fs/internal.h`：`filename_lookup()` 去 static 并声明（4.14 里是 static）
4. `fs/exec.c`：externs 换 susfs 签名（`ksu_su_compat_enabled` static key、`ksu_handle_execveat`、`ksu_handle_post_execveat`）；去掉 `ksu_execveat_hook` bool 门（模块将此符号判为不兼容）；`do_execveat_common` 在 IS_ERR 检查后调 `ksu_handle_execveat`（`susfs_is_current_proc_no_su()` + static key 双守卫），`exec_binprm()`（4.14 不是 `bprm_execve`）后调 `ksu_handle_post_execveat`
5. `fs/open.c`：`faccessat` 重构为 `getname_flags` + `filename_lookup`（struct filename** 签名），`__ksu_is_allow_uid_for_current` + static key 守卫
6. `fs/stat.c`：`vfs_statx` 同上重构；⚠️ 本树 `vfs_fstat` 不在 fs/stat.c，模块也不强制 `ksu_handle_vfs_fstat`（inline_hook_check 不查），已省略（`ksu_handle_sys_read` 兜底 init.rc 检测）
7. `fs/read_write.c`：删除 `ksu_handle_vfs_read`/`ksu_vfs_read_hook`（不兼容旧符号）；`SYSCALL_DEFINE3(read)` 入口挂 `ksu_handle_sys_read(fd)`（1 参，v2.3.0 补丁风格，static key 守卫）
8. `kernel/sys.c`：`SYSCALL_DEFINE3(setresuid)` 入口挂 `ksu_handle_setresuid(ruid, euid, suid)`
9. `kernel/reboot.c`：v2 式 supercall 入口（`ret = ksu_handle_sys_reboot(...); if (ret) goto orig_flow; return ret;`），**必须插在 `int ret = 0;` 声明行之后**，守卫用 `CONFIG_KSU`（避免 `CONFIG_KSU_MANUAL_HOOK` 字样触发模块 manual-guard 警告）
10. `drivers/input/input.c`：去 `ksu_input_hook` bool，改 `ksu_is_input_hook_enabled` static key
11. `fs/notify/fdinfo.c`：`out_seq_printf:` 标签后补空语句 `;`（susfs 补丁遗留，标签后直接跟声明在 C23 前非法）+ `fanotify_fdinfo` 对齐三参签名
12. `fs/Makefile`：`obj-$(CONFIG_KSU_SUSFS) += susfs.o susfs_compat.o`

**defconfig（Configure 步骤硬断言）**：`KSU=y`、`KSU_SUSFS=y` + SUS_PATH/SUS_MOUNT/SUS_KSTAT/SPOOF_UNAME/ENABLE_LOG/HIDE_KSU_SUSFS_SYMBOLS/SPOOF_CMDLINE_OR_BOOTCONFIG/OPEN_REDIRECT 全 y；**关闭**：KSU_MANUAL_HOOK（choice 互斥）、TRY_UMOUNT + AUTO_ADD_TRY_UMOUNT_FOR_BIND_MOUNT（模块无 `ksu_try_umount` 符号，会链接失败）、SUS_MAP / SUS_SU / SUS_OVERLAYFS（无 v1 后端）

## 已知限制（方案 A）

- SUS_MAP、AVC 日志伪装、hide-sus-mnts 开关、sdcard monitor：shim 应答但不实现（features 列表不上报，管理器不会展示）
- sus_path 不支持 tmpfs/fuse（v1.5.5 内核侧固有限制，添加会返回错误）
- 版本上报 `v1.5.5`；若管理器版本门槛报警，改 `susfs_compat.c` 里 `susfs_show_version` 的字符串
- TRY_UMOUNT 关闭：模块用自身 kernel_umount 路径卸载（v2 也弃用了该命令）

## 修复史（升级模块 commit 时对照）

| run | 失败原因 |
|---|---|
| #14 | Apply 步骤缺 `working-directory: kernel` |
| #15 | exec.c extern 块缩进是 4 tab + 空格（正则改 `[\t ]*` 宽松） |
| #16 | 4.14 是 `exec_binprm(bprm)`，不是 5.x 的 `bprm_execve(...)` |
| #17 | reboot 钩子插到 `int ret = 0;` 声明之前导致 ret 未声明 |
| #18 | fdinfo `out_seq_printf:` 标签修复缺失（老工作流有、新脚本漏了） |
| #19/#20 | `susfs_compat.mk` 的 `$(error)` 闸门：行尾无换行导致正则 `subn=0` 却误报成功；且必须 patch `KernelSU/kernel/tools/` 实体路径 |
| #21 | ✅ 成功 |

## 后期更新指南（换 BakaSU commit / 升级时）

1. **模块 ABI 检查**：对比新 commit 的 `kernel/supercall/dispatch.c`（14 个 susfs 分发函数）与 `kernel/feature/sucompat.h`（钩子签名）——与本兼容层对接的是这两个文件的契约
2. **闸门检查**：`grep -n "error" KernelSU/kernel/tools/*.mk`，新的阻断闸门照 0b 步方式处理
3. **inline_hook_check 要求**（SUSFS 模式强制 grep）：`kernel/sys.c`→`ksu_handle_setresuid`、`fs/exec.c`→`ksu_handle_execveat`、`fs/open.c`→`ksu_handle_faccessat`、`fs/read_write.c`→`ksu_handle_sys_read`、`fs/stat.c`→`ksu_handle_stat`、`kernel/reboot.c`→`ksu_handle_sys_reboot`、`drivers/input/input.c`→`ksu_handle_input_handle_event`；**禁止出现**：`ksu_vfs_read_hook`、`ksu_execveat_hook`、`ksu_input_hook`、`ksu_init_rc_hook`
4. **本地 dry-run**：把目标树的相关文件 + 模块 `Kconfig`/`tools/susfs_compat.mk` 摆成 mini 树，`python3 susfs_compat/patch_hooks.py` 全绿再推 CI（脚本全程断言，锚点漂移会 FATAL 定位）
5. **susfs 上游变化**：本层锁定的 v2 ABI 参照 simonpunk `gki-android12-5.10` 分支 v2.3.0（`SUSFS_MAGIC=0xFAFAFAFA`、CMD 号见 `susfs_def.h`）；上游改 ABI 时需同步 `susfs_def.h` CMD 表和 `susfs_compat.c` 的结构体
6. **方案 B（完整 v2 回移）**：把 gki-android12-5.10 的 kernel_patches 全套（58KB susfs.c、AS_FLAGS/i_mapping 新机制、SUS_MAP、selinux avc、readlink spoof）移植到 4.14，可解锁全部特性——工作量另估

## 构建方法

Actions → **Build chopin kernel (Android 13)** → Run workflow（kernel_ref=`4.14.186-A13`）。
工作流自动：setup BakaSU dac2d18 → 本目录补丁 → SUSFS defconfig 断言 → 编译 → Release。
