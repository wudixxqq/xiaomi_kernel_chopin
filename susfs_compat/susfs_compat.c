/*
 * fs/susfs_compat.c - susfs v2.x command dispatch shim for the legacy
 * v1.5.5 kernel-side susfs implementation (4.14 NON-GKI).
 *
 * The BakaSU/ReSukiSU kernel module (CONFIG_KSU_SUSFS) drives susfs through
 * the v2 command dispatch interface: every handler receives a
 * `void __user **arg` (a kernel pointer to a userspace pointer), copies the
 * v2 command struct from userspace itself and writes `err` back.
 *
 * The legacy in-tree susfs (v1.5.5, fs/susfs.c) keeps the actual hiding
 * machinery (INODE_STATE_* flags, sus mnt_id, kstat hlist...). This shim
 * translates the v2 ABI onto the refactored v1 *_info() cores.
 *
 * Features without any v1 backend (SUS_MAP, AVC log spoofing, hide-sus-mnts
 * toggle, sdcard monitor) are accepted and answered so the manager stays
 * functional; susfs_get_enabled_features() reports the truth.
 */

#include <linux/version.h>
#include <linux/cred.h>
#include <linux/fs.h>
#include <linux/slab.h>
#include <linux/printk.h>
#include <linux/namei.h>
#include <linux/list.h>
#include <linux/mutex.h>
#include <linux/workqueue.h>
#include <linux/uaccess.h>
#include <linux/utsname.h>
#include <linux/susfs.h>

/* no logging in the shim itself; the legacy core logs via susfs_is_log_enabled */

/*************************/
/* v2 userspace ABI structs (private) */
/*************************/
#ifdef CONFIG_KSU_SUSFS_SUS_PATH
struct compat_st_sus_path {
	char                                    target_pathname[SUSFS_MAX_LEN_PATHNAME];
	int                                     err;
};

struct compat_st_sus_path_list {
	struct list_head                        list;
	char                                    target_pathname[SUSFS_MAX_LEN_PATHNAME];
};
#endif

#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT
struct compat_st_hide_sus_mnts {
	bool                                    enabled;
	int                                     err;
};
#endif

#ifdef CONFIG_KSU_SUSFS_SUS_KSTAT
struct compat_st_sus_kstat {
	bool                                    is_statically;
	unsigned long                           target_ino;
	char                                    target_pathname[SUSFS_MAX_LEN_PATHNAME];
	unsigned long                           spoofed_ino;
	unsigned long                           spoofed_dev;
	unsigned int                            spoofed_nlink;
	long long                               spoofed_size;
	long                                    spoofed_atime_tv_sec;
	unsigned long                           spoofed_atime_tv_nsec;
	long                                    spoofed_mtime_tv_sec;
	unsigned long                           spoofed_mtime_tv_nsec;
	long                                    spoofed_ctime_tv_sec;
	unsigned long                           spoofed_ctime_tv_nsec;
	long long                               spoofed_blocks;
	long                                    spoofed_blksize;
	int                                     flags;
	int                                     err;
};
#endif

#ifdef CONFIG_KSU_SUSFS_SPOOF_UNAME
struct compat_st_uname {
	char                                    release[__NEW_UTS_LEN+1];
	char                                    version[__NEW_UTS_LEN+1];
	int                                     err;
};
#endif

#ifdef CONFIG_KSU_SUSFS_ENABLE_LOG
struct compat_st_log {
	bool                                    enabled;
	int                                     err;
};
#endif

#ifdef CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG
struct compat_st_cmdline {
	char                                    fake_cmdline_or_bootconfig[SUSFS_FAKE_CMDLINE_OR_BOOTCONFIG_SIZE];
	int                                     err;
};
#endif

#ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT
struct compat_st_open_redirect {
	char                                    target_pathname[SUSFS_MAX_LEN_PATHNAME];
	char                                    redirected_pathname[SUSFS_MAX_LEN_PATHNAME];
	int                                     uid_scheme;
	int                                     err;
};
#endif

struct compat_st_avc_log_spoofing {
	bool                                    enabled;
	int                                     err;
};

struct compat_st_enabled_features {
	char                                    enabled_features[SUSFS_ENABLED_FEATURES_SIZE];
	int                                     err;
};

struct compat_st_variant {
	char                                    susfs_variant[16];
	int                                     err;
};

struct compat_st_version {
	char                                    susfs_version[16];
	int                                     err;
};

/*************************/
/* helpers */
/*************************/
/* resolve an inode number for a pathname; mirrors v2 behavior where the
 * kernel resolves the path itself instead of trusting userspace ino */
static int compat_resolve_target_ino(const char *pathname, unsigned long *out_ino) {
	struct path p;
	struct inode *inode;
	int err;

	err = kern_path(pathname, LOOKUP_FOLLOW, &p);
	if (err)
		return err;

	inode = d_backing_inode(p.dentry);
	if (!inode) {
		path_put(&p);
		return -ENOENT;
	}
	*out_ino = inode->i_ino;
	path_put(&p);
	return 0;
}

/*
 * Deferred work the BakaSU module schedules after umount handling.
 * Re-flags sus paths whose inode may have been recreated (module mounts,
 * data unlock). Always defined: the module references it whenever
 * CONFIG_KSU_SUSFS is on, independent of the per-feature configs.
 */
#ifdef CONFIG_KSU_SUSFS_SUS_PATH
/* sus_path loop list - re-flag paths whose ino may have changed after
 * module mounts / data unlock, equivalent to v2's LH_SUS_PATH_LOOP */
static DEFINE_MUTEX(compat_lock_sus_path_loop);
static LIST_HEAD(compat_LH_SUS_PATH_LOOP);
#endif

struct work_struct susfs_extra_works;
static void susfs_run_extra_works(struct work_struct *work) {
#ifdef CONFIG_KSU_SUSFS_SUS_PATH
	struct compat_st_sus_path_list *cursor = NULL;
	struct st_susfs_sus_path info;

	mutex_lock(&compat_lock_sus_path_loop);
	list_for_each_entry(cursor, &compat_LH_SUS_PATH_LOOP, list) {
		memset(&info, 0, sizeof(info));
		if (compat_resolve_target_ino(cursor->target_pathname, &info.target_ino))
			continue;
		strncpy(info.target_pathname, cursor->target_pathname, SUSFS_MAX_LEN_PATHNAME-1);
		susfs_add_sus_path_info(&info);
	}
	mutex_unlock(&compat_lock_sus_path_loop);
#endif
}

#ifdef CONFIG_KSU_SUSFS_SUS_PATH
void susfs_add_sus_path_loop(void __user **user_info) {
	struct compat_st_sus_path info = {0};
	struct compat_st_sus_path_list *new_list = NULL;

	if (copy_from_user(&info, (struct compat_st_sus_path __user*)*user_info, sizeof(info))) {
		info.err = -EFAULT;
		goto out_copy_to_user;
	}

	if (*info.target_pathname == '\0') {
		info.err = -EINVAL;
		goto out_copy_to_user;
	}

	new_list = kzalloc(sizeof(struct compat_st_sus_path_list), GFP_KERNEL);
	if (!new_list) {
		info.err = -ENOMEM;
		goto out_copy_to_user;
	}
	strncpy(new_list->target_pathname, info.target_pathname, SUSFS_MAX_LEN_PATHNAME-1);
	INIT_LIST_HEAD(&new_list->list);
	mutex_lock(&compat_lock_sus_path_loop);
	list_add_tail(&new_list->list, &compat_LH_SUS_PATH_LOOP);
	mutex_unlock(&compat_lock_sus_path_loop);

	/* add it once right away, then keep re-flagging via extra_works */
	susfs_run_extra_works(NULL);
	info.err = 0;
out_copy_to_user:
	if (copy_to_user(&((struct compat_st_sus_path __user*)*user_info)->err, &info.err, sizeof(info.err))) {
		info.err = -EFAULT;
	}
}

void susfs_add_sus_path(void __user **user_info) {
	struct compat_st_sus_path info = {0};
	struct st_susfs_sus_path legacy = {0};

	if (copy_from_user(&info, (struct compat_st_sus_path __user*)*user_info, sizeof(info))) {
		info.err = -EFAULT;
		goto out_copy_to_user;
	}

	info.err = compat_resolve_target_ino(info.target_pathname, &legacy.target_ino);
	if (info.err) {
		goto out_copy_to_user;
	}
	strncpy(legacy.target_pathname, info.target_pathname, SUSFS_MAX_LEN_PATHNAME-1);
	if (susfs_add_sus_path_info(&legacy))
		info.err = -EINVAL;
out_copy_to_user:
	if (copy_to_user(&((struct compat_st_sus_path __user*)*user_info)->err, &info.err, sizeof(info.err))) {
		info.err = -EFAULT;
	}
}
#endif // #ifdef CONFIG_KSU_SUSFS_SUS_PATH

#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT
void susfs_set_hide_sus_mnts_for_non_su_procs(void __user **user_info) {
	struct compat_st_hide_sus_mnts info = {0};

	if (copy_from_user(&info, (struct compat_st_hide_sus_mnts __user*)*user_info, sizeof(info))) {
		info.err = -EFAULT;
		goto out_copy_to_user;
	}
	/* v1.5.5 hides sus mounts for non-su procs by design (sus mnt_id);
	 * accept and acknowledge the toggle */
	info.err = 0;
out_copy_to_user:
	if (copy_to_user(&((struct compat_st_hide_sus_mnts __user*)*user_info)->err, &info.err, sizeof(info.err))) {
		info.err = -EFAULT;
	}
}
#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MOUNT

#ifdef CONFIG_KSU_SUSFS_SUS_KSTAT
static void compat_kstat_v2_to_v1(struct compat_st_sus_kstat *v2, struct st_susfs_sus_kstat *v1) {
	memset(v1, 0, sizeof(*v1));
	v1->is_statically = v2->is_statically ? 1 : 0;
	v1->target_ino = v2->target_ino;
	strncpy(v1->target_pathname, v2->target_pathname, SUSFS_MAX_LEN_PATHNAME-1);
	v1->spoofed_ino = v2->spoofed_ino;
	/* v2 ABI sends a raw kernel dev_t; the legacy core no longer decodes it */
	v1->spoofed_dev = v2->spoofed_dev;
	v1->spoofed_nlink = v2->spoofed_nlink;
	v1->spoofed_size = v2->spoofed_size;
	v1->spoofed_atime_tv_sec = v2->spoofed_atime_tv_sec;
	v1->spoofed_atime_tv_nsec = (long)v2->spoofed_atime_tv_nsec;
	v1->spoofed_mtime_tv_sec = v2->spoofed_mtime_tv_sec;
	v1->spoofed_mtime_tv_nsec = (long)v2->spoofed_mtime_tv_nsec;
	v1->spoofed_ctime_tv_sec = v2->spoofed_ctime_tv_sec;
	v1->spoofed_ctime_tv_nsec = (long)v2->spoofed_ctime_tv_nsec;
	v1->spoofed_blocks = v2->spoofed_blocks;
	v1->spoofed_blksize = (unsigned long)v2->spoofed_blksize;
}

void susfs_add_sus_kstat(void __user **user_info) {
	struct compat_st_sus_kstat info = {0};
	struct st_susfs_sus_kstat legacy;

	if (copy_from_user(&info, (struct compat_st_sus_kstat __user*)*user_info, sizeof(info))) {
		info.err = -EFAULT;
		goto out_copy_to_user;
	}

	if (*info.target_pathname == '\0') {
		info.err = -EINVAL;
		goto out_copy_to_user;
	}

	/* v2 resolves the real ino itself; do the same when not provided */
	if (!info.target_ino) {
		info.err = compat_resolve_target_ino(info.target_pathname, &info.target_ino);
		if (info.err)
			goto out_copy_to_user;
	}

	compat_kstat_v2_to_v1(&info, &legacy);
	if (susfs_add_sus_kstat_info(&legacy))
		info.err = -EINVAL;
out_copy_to_user:
	if (copy_to_user(&((struct compat_st_sus_kstat __user*)*user_info)->err, &info.err, sizeof(info.err))) {
		info.err = -EFAULT;
	}
}

void susfs_update_sus_kstat(void __user **user_info) {
	struct compat_st_sus_kstat info = {0};
	struct st_susfs_sus_kstat legacy;

	if (copy_from_user(&info, (struct compat_st_sus_kstat __user*)*user_info, sizeof(info))) {
		info.err = -EFAULT;
		goto out_copy_to_user;
	}

	if (*info.target_pathname == '\0') {
		info.err = -EINVAL;
		goto out_copy_to_user;
	}

	if (!info.target_ino) {
		info.err = compat_resolve_target_ino(info.target_pathname, &info.target_ino);
		if (info.err)
			goto out_copy_to_user;
	}

	compat_kstat_v2_to_v1(&info, &legacy);
	if (susfs_update_sus_kstat_info(&legacy))
		info.err = -EINVAL;
out_copy_to_user:
	if (copy_to_user(&((struct compat_st_sus_kstat __user*)*user_info)->err, &info.err, sizeof(info.err))) {
		info.err = -EFAULT;
	}
}
#endif // #ifdef CONFIG_KSU_SUSFS_SUS_KSTAT

#ifdef CONFIG_KSU_SUSFS_SPOOF_UNAME
void susfs_set_uname(void __user **user_info) {
	struct compat_st_uname info = {0};
	struct st_susfs_uname legacy;

	if (copy_from_user(&info, (struct compat_st_uname __user*)*user_info, sizeof(info))) {
		info.err = -EFAULT;
		goto out_copy_to_user;
	}

	if (*info.release == '\0' || *info.version == '\0') {
		info.err = -EFAULT;
		goto out_copy_to_user;
	}

	strncpy(legacy.release, info.release, __NEW_UTS_LEN);
	legacy.release[__NEW_UTS_LEN] = '\0';
	strncpy(legacy.version, info.version, __NEW_UTS_LEN);
	legacy.version[__NEW_UTS_LEN] = '\0';
	if (susfs_set_uname_info(&legacy))
		info.err = -EINVAL;
out_copy_to_user:
	if (copy_to_user(&((struct compat_st_uname __user*)*user_info)->err, &info.err, sizeof(info.err))) {
		info.err = -EFAULT;
	}
}
#endif // #ifdef CONFIG_KSU_SUSFS_SPOOF_UNAME

#ifdef CONFIG_KSU_SUSFS_ENABLE_LOG
void susfs_enable_log(void __user **user_info) {
	struct compat_st_log info = {0};

	if (copy_from_user(&info, (struct compat_st_log __user*)*user_info, sizeof(info))) {
		info.err = -EFAULT;
		goto out_copy_to_user;
	}

	susfs_set_log(info.enabled);
	info.err = 0;
out_copy_to_user:
	if (copy_to_user(&((struct compat_st_log __user*)*user_info)->err, &info.err, sizeof(info.err))) {
		info.err = -EFAULT;
	}
}
#endif // #ifdef CONFIG_KSU_SUSFS_ENABLE_LOG

#ifdef CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG
void susfs_set_cmdline_or_bootconfig(void __user **user_info) {
	struct compat_st_cmdline info = {0};

	if (copy_from_user(&info, (struct compat_st_cmdline __user*)*user_info, sizeof(info))) {
		info.err = -EFAULT;
		goto out_copy_to_user;
	}

	if (*info.fake_cmdline_or_bootconfig == '\0') {
		info.err = -EINVAL;
		goto out_copy_to_user;
	}

	info.err = susfs_set_cmdline_or_bootconfig_info(info.fake_cmdline_or_bootconfig);
out_copy_to_user:
	if (copy_to_user(&((struct compat_st_cmdline __user*)*user_info)->err, &info.err, sizeof(info.err))) {
		info.err = -EFAULT;
	}
}
#endif // #ifdef CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG

#ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT
void susfs_add_open_redirect(void __user **user_info) {
	struct compat_st_open_redirect info = {0};
	struct st_susfs_open_redirect legacy = {0};

	if (copy_from_user(&info, (struct compat_st_open_redirect __user*)*user_info, sizeof(info))) {
		info.err = -EFAULT;
		goto out_copy_to_user;
	}

	info.err = compat_resolve_target_ino(info.target_pathname, &legacy.target_ino);
	if (info.err)
		goto out_copy_to_user;
	strncpy(legacy.target_pathname, info.target_pathname, SUSFS_MAX_LEN_PATHNAME-1);
	strncpy(legacy.redirected_pathname, info.redirected_pathname, SUSFS_MAX_LEN_PATHNAME-1);
	if (susfs_add_open_redirect_info(&legacy))
		info.err = -EINVAL;
out_copy_to_user:
	if (copy_to_user(&((struct compat_st_open_redirect __user*)*user_info)->err, &info.err, sizeof(info.err))) {
		info.err = -EFAULT;
	}
}
#endif // #ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT

/* sus_map: no v1.5.5 backend, the feature is not advertised via
 * susfs_get_enabled_features() - reject politely */
struct compat_st_sus_map {
	char                                    target_pathname[SUSFS_MAX_LEN_PATHNAME];
	int                                     err;
};

void susfs_add_sus_map(void __user **user_info) {
	struct compat_st_sus_map info = {0};

	if (copy_from_user(&info, (struct compat_st_sus_map __user*)*user_info, sizeof(info))) {
		info.err = -EFAULT;
		goto out_copy_to_user;
	}
	info.err = -EOPNOTSUPP;
out_copy_to_user:
	if (copy_to_user(&((struct compat_st_sus_map __user*)*user_info)->err, &info.err, sizeof(info.err))) {
		info.err = -EFAULT;
	}
}

void susfs_set_avc_log_spoofing(void __user **user_info) {
	struct compat_st_avc_log_spoofing info = {0};

	if (copy_from_user(&info, (struct compat_st_avc_log_spoofing __user*)*user_info, sizeof(info))) {
		info.err = -EFAULT;
		goto out_copy_to_user;
	}
	/* no selinux-side hook in this backport; acknowledge only */
	info.err = 0;
out_copy_to_user:
	if (copy_to_user(&((struct compat_st_avc_log_spoofing __user*)*user_info)->err, &info.err, sizeof(info.err))) {
		info.err = -EFAULT;
	}
}

static int compat_copy_config_to_buf(const char *config_string, char *buf_ptr, size_t *copied_size, size_t bufsize) {
	size_t tmp_size = strlen(config_string);

	*copied_size += tmp_size;
	if (*copied_size >= bufsize)
		return -EINVAL;
	memcpy(buf_ptr, config_string, tmp_size);
	return 0;
}

void susfs_get_enabled_features(void __user **user_info) {
	struct compat_st_enabled_features *info;
	char *buf_ptr = NULL;
	size_t copied_size = 0;
	int err = 0;

	info = kzalloc(sizeof(struct compat_st_enabled_features), GFP_KERNEL);
	if (!info) {
		err = -ENOMEM;
		if (copy_to_user(&((struct compat_st_enabled_features __user*)*user_info)->err, &err, sizeof(err)))
			err = -EFAULT;
		return;
	}

	if (copy_from_user(info, (struct compat_st_enabled_features __user*)*user_info, sizeof(struct compat_st_enabled_features))) {
		info->err = -EFAULT;
		goto out_copy_to_user;
	}

	buf_ptr = info->enabled_features;

#ifdef CONFIG_KSU_SUSFS_SUS_PATH
	info->err = compat_copy_config_to_buf("CONFIG_KSU_SUSFS_SUS_PATH\n", buf_ptr, &copied_size, SUSFS_ENABLED_FEATURES_SIZE);
	if (info->err) goto out_copy_to_user;
	buf_ptr = info->enabled_features + copied_size;
#endif
#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT
	info->err = compat_copy_config_to_buf("CONFIG_KSU_SUSFS_SUS_MOUNT\n", buf_ptr, &copied_size, SUSFS_ENABLED_FEATURES_SIZE);
	if (info->err) goto out_copy_to_user;
	buf_ptr = info->enabled_features + copied_size;
#endif
#ifdef CONFIG_KSU_SUSFS_SUS_KSTAT
	info->err = compat_copy_config_to_buf("CONFIG_KSU_SUSFS_SUS_KSTAT\n", buf_ptr, &copied_size, SUSFS_ENABLED_FEATURES_SIZE);
	if (info->err) goto out_copy_to_user;
	buf_ptr = info->enabled_features + copied_size;
#endif
#ifdef CONFIG_KSU_SUSFS_SPOOF_UNAME
	info->err = compat_copy_config_to_buf("CONFIG_KSU_SUSFS_SPOOF_UNAME\n", buf_ptr, &copied_size, SUSFS_ENABLED_FEATURES_SIZE);
	if (info->err) goto out_copy_to_user;
	buf_ptr = info->enabled_features + copied_size;
#endif
#ifdef CONFIG_KSU_SUSFS_ENABLE_LOG
	info->err = compat_copy_config_to_buf("CONFIG_KSU_SUSFS_ENABLE_LOG\n", buf_ptr, &copied_size, SUSFS_ENABLED_FEATURES_SIZE);
	if (info->err) goto out_copy_to_user;
	buf_ptr = info->enabled_features + copied_size;
#endif
#ifdef CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS
	info->err = compat_copy_config_to_buf("CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS\n", buf_ptr, &copied_size, SUSFS_ENABLED_FEATURES_SIZE);
	if (info->err) goto out_copy_to_user;
	buf_ptr = info->enabled_features + copied_size;
#endif
#ifdef CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG
	info->err = compat_copy_config_to_buf("CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG\n", buf_ptr, &copied_size, SUSFS_ENABLED_FEATURES_SIZE);
	if (info->err) goto out_copy_to_user;
	buf_ptr = info->enabled_features + copied_size;
#endif
#ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT
	info->err = compat_copy_config_to_buf("CONFIG_KSU_SUSFS_OPEN_REDIRECT\n", buf_ptr, &copied_size, SUSFS_ENABLED_FEATURES_SIZE);
	if (info->err) goto out_copy_to_user;
	buf_ptr = info->enabled_features + copied_size;
#endif

	info->err = 0;
out_copy_to_user:
	if (copy_to_user((struct compat_st_enabled_features __user*)*user_info, info, sizeof(struct compat_st_enabled_features))) {
		info->err = -EFAULT;
	}
	kfree(info);
}

void susfs_show_variant(void __user **user_info) {
	struct compat_st_variant info = {0};

	if (copy_from_user(&info, (struct compat_st_variant __user*)*user_info, sizeof(info))) {
		info.err = -EFAULT;
		goto out_copy_to_user;
	}

	strncpy(info.susfs_variant, SUSFS_VARIANT, SUSFS_MAX_VARIANT_BUFSIZE-1);
	info.err = 0;
out_copy_to_user:
	if (copy_to_user((struct compat_st_variant __user*)*user_info, &info, sizeof(info))) {
		info.err = -EFAULT;
	}
}

void susfs_show_version(void __user **user_info) {
	struct compat_st_version info = {0};

	if (copy_from_user(&info, (struct compat_st_version __user*)*user_info, sizeof(info))) {
		info.err = -EFAULT;
		goto out_copy_to_user;
	}

	strncpy(info.susfs_version, SUSFS_VERSION, SUSFS_MAX_VERSION_BUFSIZE-1);
	info.err = 0;
out_copy_to_user:
	if (copy_to_user((struct compat_st_version __user*)*user_info, &info, sizeof(info))) {
		info.err = -EFAULT;
	}
}

void susfs_start_sdcard_monitor_fn(void) {
	/* v2-only fsnotify based /sdcard/Android decryption monitor.
	 * The legacy backend has no such state, treat sdcard as always
	 * decrypted (which it is for the v1 code paths). */
}

void susfs_compat_init(void) {
#ifdef CONFIG_KSU_SUSFS_SUS_PATH
	INIT_WORK(&susfs_extra_works, susfs_run_extra_works);
#endif
	pr_info("susfs compat shim initialized, kernel-side susfs: " SUSFS_VERSION "\n");
}
