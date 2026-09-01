// =============================================================================
//  zfs_lfs.h  —  ZENO OS private kernel filesystem (LittleFS2 on "zfs" partition)
//
//  RULES:
//    * NEVER mounted into MicroPython VFS.
//    * NEVER visible in os.listdir() or open().
//    * Access ONLY via  import zfs  (modzfs.c).
//
//  Target: ESP-IDF v5.5.x, MicroPython ESP32-S3 port.
// =============================================================================

#ifndef ZFS_LFS_H
#define ZFS_LFS_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

/*
 * LittleFS2 include order matters:
 *   lfs2_util.h  — defines LFS2_ASSERT, lfs2_malloc, lfs2_free, etc.
 *   lfs2.h       — depends on lfs2_util.h being included first.
 *
 * MicroPython's tree does NOT provide typedef aliases for struct lfs2_config,
 * struct lfs2_file, or struct lfs2_dir.  We declare them ourselves below.
 */
#include "../../lib/littlefs/lfs2_util.h"
#include "../../lib/littlefs/lfs2.h"

/*
 * Convenience typedefs — MicroPython's lfs2.h uses bare struct tags only.
 * Guard with a unique macro rather than #ifndef <typename> which is unreliable
 * for type names (as opposed to macros).
 */
#ifndef ZFS_LFS_TYPEDEFS_DEFINED
#define ZFS_LFS_TYPEDEFS_DEFINED
typedef struct lfs2_config   lfs2_config_t;
typedef struct lfs2_file     lfs2_file_t;
typedef struct lfs2_dir      lfs2_dir_t;
#endif

#include "esp_err.h"

/* -------------------------------------------------------------------------
 * Error codes
 * ---------------------------------------------------------------------- */
typedef enum {
    ZFS_OK              =  0,
    ZFS_ERR_NOT_MOUNTED = -1,
    ZFS_ERR_ALREADY_MNT = -2,
    ZFS_ERR_NO_PART     = -3,
    ZFS_ERR_LFS         = -4,
    ZFS_ERR_IO          = -5,
    ZFS_ERR_INVAL       = -6,
    ZFS_ERR_NOENT       = -7,
    ZFS_ERR_EXIST       = -8,
    ZFS_ERR_NOMEM       = -9,
    ZFS_ERR_NOTDIR      = -10,
    ZFS_ERR_ISDIR       = -11,
    ZFS_ERR_NOTEMPTY    = -12,
    ZFS_ERR_NAMETOOLONG = -13,
} zfs_err_t;

/* -------------------------------------------------------------------------
 * Info struct returned by zfs_lfs_info()
 * ---------------------------------------------------------------------- */
typedef struct {
    uint32_t  block_size;
    uint32_t  block_count;
    uint32_t  blocks_used;
    uint32_t  partition_offset;
    uint32_t  partition_size;
    bool      mounted;
} zfs_info_t;

/* -------------------------------------------------------------------------
 * Directory entry passed to the listdir callback
 * ---------------------------------------------------------------------- */
#define ZFS_NAME_MAX  255

typedef struct {
    char     name[ZFS_NAME_MAX + 1];
    uint8_t  type;   /* LFS2_TYPE_REG or LFS2_TYPE_DIR */
    uint32_t size;   /* 0 for directories               */
} zfs_dirent_t;

/* -------------------------------------------------------------------------
 * Public API  (all functions are thread-safe via FreeRTOS mutex)
 * ---------------------------------------------------------------------- */

/* Lifecycle */
zfs_err_t  zfs_lfs_mount(void);
zfs_err_t  zfs_lfs_umount(void);
zfs_err_t  zfs_lfs_format(void);
zfs_err_t  zfs_lfs_info(zfs_info_t *out);

/* File I/O */
zfs_err_t  zfs_lfs_write(const char *path, const uint8_t *data, size_t len);
zfs_err_t  zfs_lfs_read(const char *path, uint8_t **out_data, size_t *out_len);
zfs_err_t  zfs_lfs_delete(const char *path);
zfs_err_t  zfs_lfs_exists(const char *path, bool *out);
zfs_err_t  zfs_lfs_rename(const char *oldpath, const char *newpath);

/* Directory */
zfs_err_t  zfs_lfs_mkdir(const char *path);
zfs_err_t  zfs_lfs_rmdir(const char *path);

/* listdir: cb is called once per entry; return non-zero to stop early */
typedef int (*zfs_listdir_cb_t)(const zfs_dirent_t *entry, void *userdata);
zfs_err_t  zfs_lfs_listdir(const char *path,
                            zfs_listdir_cb_t cb, void *userdata);

/* Human-readable error string */
const char *zfs_lfs_strerror(zfs_err_t err);

#endif /* ZFS_LFS_H */
