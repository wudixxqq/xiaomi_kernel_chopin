# SUSFS v2.3.0 补丁链（chopin 4.14 + BakaSU）—— 方案 B'

> 状态：**已验证可用** — CI run #28 构建成功，产物 Release `build-20261022-083530`；真机刷入后 SUS_MAP / SPOOF_UNAME 行为级验证通过
> 内核：4.14.186-g0dc1d312efb3（分支 4.14.186-A13）｜ 模块：BakaSU CI 35236 / commit `dac2d186406a0d2b0ce3c67cad5a1fa1adbf9f4d`（SUSFS hook 模式）
> susfs 内核侧：**v2.3.0**（JackA1ltman/NonGKI_Kernel_Build_2nd 的 4.14 完整回移），9 特性全开（含 SUS_MAP）
> 前代方案：`susfs_compat/`（v1.5.5 + v2 分发兼容层，方案 A）已完成历史使命，目录保留存档

## 与方案 A 的区别

| | 方案 A（susfs_compat/） | 方案 B'（本目录） |
|---|---|---|
| susfs 内核侧 | 官方 v1.5.5（NON-GKI 旧接口） | **v2.3.0**（JackA1ltman 4.14 回移） |
| v2 命令分发 | 自写翻译层（14 个 shim） | 原生支持 |
| SUS_MAP / AVC 伪装 / hide-sus-mnts / sdcard monitor | no-op / 不支持 | **真实可用** |
| 版本上报 | v1.5.5（管理器可能告警） | v2.3.0 |
| 真机验证 | run #21 + 用户确认可用 | run #28 + SUS_MAP 行为级验证通过 |

## 文件清单

| 文件 | 说明 |
|---|---|
| `patch_hooks_b.py` | **核心**：15 步补丁链（CI 在 kernel/ 目录执行 `python3 ../ci/susfs_v2/patch_hooks_b.py`），fail-fast 全断言 |
| `susfs_v155_414_official.patch` | 官方 susfs4ksu kernel-4.14 分支 v1.5.5 补丁，用于 `patch -R` 反还原 chopin 树自带的 v1.5.5 |
| `susfs_patch_to_4.14.patch` | JackA1ltman susfs v2.3.0 4.14 回移（20 文件：susfs.c/h/def.h 新建 + 内核核心文件修改） |
| `susfs_inline_hook_patches.sh` | JackA1ltman 钩子注入脚本（7 个 ksu_handle_* 调用点 + faccessat/stat 的 filename_lookup 重构） |
| `fix_susfs_task_mmu.py` | rosemary 配方修复脚本：task_mmu.c reject hunk 1/4 的移植（chopin 4.14 原生结构与 fixer 锚完全兼容） |
| `fix_susfs_414.py` | rosemary 配方修复脚本：stat/mnt_id、filename_lookup 声明、statfs extern 顺序、susfs.c include+调用参、proc/fd 括号、super/namespace 的 IDA 锁 |
| `build.yml` | CI 工作流（.github/workflows/build.yml 同步此文件）：setup BakaSU dac2d18 → 补丁链 → defconfig 9 特性全开 → 编译 → Release |

## 15 步补丁链（patch_hooks_b.py）

```
[1] 模块 Kconfig：解体 hook-method choice + 剥 THREAD_INFO_IN_TASK 依赖
[2] 移除 BakaSU susfs_compat.mk 的 $(error Please wait susfs update) 闸门（双路径 realpath 去重）
[3] 反还原树内 v1.5.5（官方补丁 -R；排除 fdinfo/sys/task_mmu/readdir 四文件，见 [4]）
[4] 删 fs/susfs.c、include/linux/susfs.{h,def.h}、死文件 fs/sus_su.c（Makefile 无引用）；
    python 精修 4 个反还原 exclude 文件：
      - fdinfo.c  ：4 处 v1.5.5 块 + run#18 标签残留（专用正则）
      - kernel/sys.c：SPOOF_UNAME 调用块 + 树内 #include <linux/susfs.h>
      - task_mmu.c / readdir.c：通用嵌套感知 susfs 块删除器
[5] 全树残留断言（fs/include/linux/kernel/mm/security/block 内 susfs 痕迹必须为 0）
[6] strip 5 个文件的旧式 bool 钩子（exec:ksu_execveat_hook / open:旧faccessat /
    read_write:ksu_vfs_read_hook / stat:旧stat / input:ksu_input_hook；devpts 的 ksu_handle_devpts 保留）
    —— 不 strip 会导致 inline_hook_check $(error) 且钩子脚本 grep ksu_handle 跳过这些文件
[7] fs/internal.h 声明 filename_lookup()
[8] bash susfs_inline_hook_patches.sh（注入 7 个 ksu_handle_*；namei un-static 由它完成）
[9] 应用 Jack v2.3.0 补丁（逐段 patch，期望失败容忍：
      task_mmu 3/8 hunks = rosemary 同款 1/5/6 → fixer 修复；
      fdinfo 1/4 hunks = 仅函数体块 → [11] 手工移植；#1/2/3（include+双签名）必须成功）
[10] exec.c：删除 susfs_is_sdcard_android_data_not_decrypted 分支（BakaSU dac2d18 无此符号）
      —— 只在钩子块内（proc_no_su 行至 orig_flow: 标签）操作，块外一律不碰
[11] input.c：补 ksu_handle_input_handle_event 调用（脚本 sed 锚在 4.14 多一个 code 参数）
[12] fdinfo.c：手工移植 reject 的 Hunk#4 函数体（见修复史 fix4/5/6）
[13] kernel/sys.c：手工移植 SPOOF_UNAME（newuname extern 块 + memcpy 后 static_branch 调用；
      锚=SYSCALL_DEFINE1 后第一个 memcpy，第二个 memcpy 属于别的函数）
[14] 跑 rosemary 两个 fixer + 删容忍的 .rej + 断言 task_mmu include/guard 落位
[15] 终验：7 必需钩子、4 禁用符号（grep -w）、7 文件无 CONFIG_KSU_MANUAL_HOOK 字样、
      fs/Makefile susfs.o、susfs 三文件、无 .rej/.orig、susfs_def.h v2.3.0 API
```

## 修复史（CI 迭代 #22–#28，共 6 轮）

| 轮次 | 现象 | 根因 | 修复 |
|---|---|---|---|
| #22 | 反还原 task_mmu/readdir reject | 小米树在这两文件的 v1.5.5 应用态与官方补丁上下文有差 | 排除 + 通用块删除器清理 |
| #23 | exec.c:1734 expected expression | 后处理误删**全文件**所有裸 `else` 行 | 限定在钩子块内删除（fix1→fix3） |
| #24 | susfs 残留断言拦截 fs/sus_su.c | v1.5.5 死文件（Makefile 无引用） | 删除（fix2） |
| #25–#26 | fdinfo undeclared 'file'/'DEFAULT_KSU_MNT_ID' | fdinfo 被整体排除，Hunk#1/2/3（include+双签名）没打 | 只容忍 Hunk#4，#1/2/3 正常应用（fix4） |
| #27 | fdinfo:169 expected expression | gnu89 下 label（`orig_flow:`）后不能直接跟变量声明（chopin 尾部是 `u32 mask=...` 声明，rosemary 是表达式语句所以没事） | 标签后插空语句 `;`（fix5） |
| #28 前 | fdinfo:179 extraneous '}' | 手工移植块多写一个 `}` | 删多余行 + 加大括号平衡断言（fix6） |

## ksud 用户态要点（真机验证得出）

1. **`ksud susfs add_sus_map`（旧命令）是静默无效的**——不即时下发内核、不写 sus_maps.txt、不进 config。必须用 `ksud susfs config sus_map add <PATH>`（持久化）。
2. **config 通道纯持久化，开机（boot-completed）才下发内核**。添加 sus_path/sus_map 后需要重启生效。
3. `config logging add` 是**无参 toggle**；logging=false 时 susfs 全部内核日志被吞（排障前先开）。
4. `ksud susfs show` 上报版本/特性（管理器同款接口）；`config list_all` 输出全部持久化配置 JSON（含 uname 伪装串）。
5. **SPOOF_UNAME 由 ksud config 的 `uname` 字段驱动**（伪装官方版本串 gaa24d707761c），对非豁免进程生效；管理器/`/proc/version` 可见真实版本。
6. **SUS_MAP 只对 `umounted process with uid >= 10000` 生效**——root/shell/ksu 域看 maps 永远全量，这是设计不是故障。
7. app 无法 exec `/data/local/tmp`（SELinux shell_data_file 标签）；验证脚本文件要放 app 私有目录（app_data_file 标签）并 chown 为该 app uid。

## 真机验证记录（2026-10-22，chopin _a 槽）

- `/proc/version` = `4.14.186-g0dc1d312efb3 (clang 13) #1 SMP PREEMPT Thu Oct 22 08:35:03 UTC 2026`（与 Release 时间戳精确吻合）
- `ksud susfs show`：v2.3.0 / 9 特性全开 / NON-GKI
- SUS_MAP 端到端：`config sus_map add <termux_home>/smtest/sleep` → 重启 → 内核日志 `is flagged as AS_FLAGS_SUS_MAP ret:0` → Termux（uid 10296 umounted app）内执行该文件后 `cat /proc/$P/maps`：**sus 文件映射 0 行**；未注册对照文件 4 行正常；进程总映射 138 行健康
- 对照组排除假阳性；root 视角全量可见（豁免设计）
- 附加确认：susfs sdcard monitor（fsnotify watch /data/media/0）在工作

## 后期升级指南

1. **升级 BakaSU 模块 commit**：改 `build.yml` 里 setup 步骤的 `bash -s <新SHA>`。先核对：
   - 新模块 `kernel/tools/susfs_compat.mk` 是否还有 $(error) 闸门（[2] 会自动删）
   - `kernel/Kconfig` 的 choice/THREAD_INFO_IN_TASK 结构是否变化（[1] 会 fail-fast 提示）
   - inline_hook_check 的必需/禁用符号表是否变化（对照 [15] 的清单）
   - 若模块新增静态键符号（如 `susfs_is_sdcard_android_data_not_decrypted` 这类新特性符号），可删除 [10] 对应的后处理
2. **升级 JackA1ltman 补丁版本**：替换 `susfs_patch_to_4.14.patch`，检查 expect_fail 清单（task_mmu hunk 数、fdinfo hunk 数）是否漂移；fixer 脚本按 rosemary 仓库更新同步
3. **本地 dry-run**：搭建迷你树（内核钩子文件快照 + dac2d18 Kconfig/susfs_compat.mk + 假 drivers/kernelsu/fake_hook.c 含 ksu_handle_setresuid），设 `PATCH_EXE/BASH/BASH_EXTRA_PATH` 环境变量后 `python patch_hooks_b.py --allow-missing`；**重跑前必须完整重置**（[1]/[2]/[3] 均不可重入）
4. **推单文件**：Git Data API 单提交（blobs→tree(base_tree)→commit→update ref），workflow_dispatch 触发，1 分钟监控循环自动修复
5. 排障顺序：CI 失败先看哪一步（[3] 反还原 / [8] 钩子 / [9] 补丁 / 编译），编译错误 grep `error:`，fdinfo/task_mmu 相关先对照本 README 修复史
