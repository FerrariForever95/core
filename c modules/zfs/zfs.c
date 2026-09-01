// =============================================================================
//  modzfs.c  —  MicroPython "zfs" module
//
//  Exposes the private Zeno kernel filesystem to Python.
//  No VFS involvement. No path mounting. Completely private.
//
//  Python API:
//    import zfs
//    zfs.mount()
//    zfs.umount()
//    zfs.format()
//    zfs.info()                          → dict
//    zfs.write(path, bytes_or_str)
//    zfs.read(path)                      → bytes
//    zfs.delete(path)
//    zfs.mkdir(path)
//    zfs.rmdir(path)
//    zfs.listdir(path="/")              → list of str  (dirs end with "/")
//    zfs.exists(path)                   → bool
//    zfs.rename(old, new)
//
//  All errors raise OSError with a descriptive message.
//
//  Target: ESP-IDF v5.5.x, MicroPython ESP32-S3 port.
// =============================================================================

#include "py/runtime.h"
#include "py/obj.h"
#include "py/objstr.h"
#include "py/objlist.h"
#include "py/mphal.h"
#include "py/mperrno.h"

#include "zfs_lfs.h"

#include <string.h>
#include <stdlib.h>
#include <errno.h>      /* ENOTEMPTY, ENAMETOOLONG — not in MicroPython mperrno.h */

// ---------------------------------------------------------------------------
//  Internal helpers
// ---------------------------------------------------------------------------

// Raise OSError from a zfs_err_t code.
static NORETURN void _raise(zfs_err_t err) {
    // Map ZFS errors to POSIX errno where possible, fall through to EIO
    int mp_errno;
    switch (err) {
        case ZFS_ERR_NOT_MOUNTED: mp_errno = MP_ENODEV;   break;
        case ZFS_ERR_ALREADY_MNT: mp_errno = MP_EBUSY;    break;
        case ZFS_ERR_NO_PART:     mp_errno = MP_ENODEV;   break;
        case ZFS_ERR_NOENT:       mp_errno = MP_ENOENT;   break;
        case ZFS_ERR_EXIST:       mp_errno = MP_EEXIST;   break;
        case ZFS_ERR_NOMEM:       mp_errno = MP_ENOMEM;   break;
        case ZFS_ERR_NOTDIR:      mp_errno = MP_ENOTDIR;  break;
        case ZFS_ERR_ISDIR:       mp_errno = MP_EISDIR;   break;
        case ZFS_ERR_NOTEMPTY:    mp_errno = ENOTEMPTY;   break;  /* not in MP_ set */
        case ZFS_ERR_INVAL:       mp_errno = MP_EINVAL;   break;
        case ZFS_ERR_NAMETOOLONG: mp_errno = ENAMETOOLONG;break;  /* not in MP_ set */
        default:                  mp_errno = MP_EIO;      break;
    }
    mp_raise_OSError_with_filename(mp_errno, zfs_lfs_strerror(err));
}

// Extract a C string from a MicroPython string or bytes object.
static const char *_path(mp_obj_t o) {
    if (mp_obj_is_str(o)) {
        return mp_obj_str_get_str(o);
    }
    mp_raise_TypeError(MP_ERROR_TEXT("path must be str"));
}

// ---------------------------------------------------------------------------
//  zfs.mount()
// ---------------------------------------------------------------------------

static mp_obj_t zfs_mount(void) {
    zfs_err_t rc = zfs_lfs_mount();
    if (rc != ZFS_OK) _raise(rc);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(zfs_mount_obj, zfs_mount);

// ---------------------------------------------------------------------------
//  zfs.umount()
// ---------------------------------------------------------------------------

static mp_obj_t zfs_umount(void) {
    zfs_err_t rc = zfs_lfs_umount();
    if (rc != ZFS_OK) _raise(rc);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(zfs_umount_obj, zfs_umount);

// ---------------------------------------------------------------------------
//  zfs.format()
// ---------------------------------------------------------------------------

static mp_obj_t zfs_format(void) {
    zfs_err_t rc = zfs_lfs_format();
    if (rc != ZFS_OK) _raise(rc);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(zfs_format_obj, zfs_format);

// ---------------------------------------------------------------------------
//  zfs.info()  →  dict
//    { "mounted": bool,
//      "block_size": int,
//      "block_count": int,
//      "blocks_used": int,
//      "bytes_total": int,
//      "bytes_used": int,
//      "bytes_free": int,
//      "partition_offset": int,
//      "partition_size": int }
// ---------------------------------------------------------------------------

static mp_obj_t zfs_info(void) {
    zfs_info_t info;
    zfs_err_t rc = zfs_lfs_info(&info);
    if (rc != ZFS_OK) _raise(rc);

    mp_obj_t dict = mp_obj_new_dict(9);

    mp_obj_dict_store(dict,
        MP_OBJ_NEW_QSTR(MP_QSTR_mounted),
        mp_obj_new_bool(info.mounted));
    mp_obj_dict_store(dict,
        MP_OBJ_NEW_QSTR(MP_QSTR_block_size),
        mp_obj_new_int_from_uint(info.block_size));
    mp_obj_dict_store(dict,
        MP_OBJ_NEW_QSTR(MP_QSTR_block_count),
        mp_obj_new_int_from_uint(info.block_count));
    mp_obj_dict_store(dict,
        MP_OBJ_NEW_QSTR(MP_QSTR_blocks_used),
        mp_obj_new_int_from_uint(info.blocks_used));
    mp_obj_dict_store(dict,
        MP_OBJ_NEW_QSTR(MP_QSTR_bytes_total),
        mp_obj_new_int_from_uint(info.block_size * info.block_count));
    mp_obj_dict_store(dict,
        MP_OBJ_NEW_QSTR(MP_QSTR_bytes_used),
        mp_obj_new_int_from_uint(info.block_size * info.blocks_used));
    mp_obj_dict_store(dict,
        MP_OBJ_NEW_QSTR(MP_QSTR_bytes_free),
        mp_obj_new_int_from_uint(
            info.block_size * (info.block_count - info.blocks_used)));
    mp_obj_dict_store(dict,
        MP_OBJ_NEW_QSTR(MP_QSTR_partition_offset),
        mp_obj_new_int_from_uint(info.partition_offset));
    mp_obj_dict_store(dict,
        MP_OBJ_NEW_QSTR(MP_QSTR_partition_size),
        mp_obj_new_int_from_uint(info.partition_size));

    return dict;
}
static MP_DEFINE_CONST_FUN_OBJ_0(zfs_info_obj, zfs_info);

// ---------------------------------------------------------------------------
//  zfs.write(path, data)
//    data: bytes, bytearray, or str (str encoded as UTF-8)
// ---------------------------------------------------------------------------

static mp_obj_t zfs_write(mp_obj_t path_obj, mp_obj_t data_obj) {
    const char *path = _path(path_obj);

    mp_buffer_info_t bufinfo;
    uint8_t   *data;
    size_t     len;

    if (mp_obj_is_str(data_obj)) {
        // Allow writing plain strings
        size_t slen;
        const char *s = mp_obj_str_get_data(data_obj, &slen);
        data = (uint8_t *)s;
        len  = slen;
    } else {
        mp_get_buffer_raise(data_obj, &bufinfo, MP_BUFFER_READ);
        data = (uint8_t *)bufinfo.buf;
        len  = bufinfo.len;
    }

    zfs_err_t rc = zfs_lfs_write(path, data, len);
    if (rc != ZFS_OK) _raise(rc);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_2(zfs_write_obj, zfs_write);

// ---------------------------------------------------------------------------
//  zfs.read(path)  →  bytes
// ---------------------------------------------------------------------------

static mp_obj_t zfs_read(mp_obj_t path_obj) {
    const char *path = _path(path_obj);

    uint8_t *data = NULL;
    size_t   len  = 0;

    zfs_err_t rc = zfs_lfs_read(path, &data, &len);
    if (rc != ZFS_OK) _raise(rc);

    mp_obj_t result = mp_obj_new_bytes(data, len);
    free(data);
    return result;
}
static MP_DEFINE_CONST_FUN_OBJ_1(zfs_read_obj, zfs_read);

// ---------------------------------------------------------------------------
//  zfs.delete(path)
// ---------------------------------------------------------------------------

static mp_obj_t zfs_delete(mp_obj_t path_obj) {
    zfs_err_t rc = zfs_lfs_delete(_path(path_obj));
    if (rc != ZFS_OK) _raise(rc);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(zfs_delete_obj, zfs_delete);

// ---------------------------------------------------------------------------
//  zfs.mkdir(path)
// ---------------------------------------------------------------------------

static mp_obj_t zfs_mkdir(mp_obj_t path_obj) {
    zfs_err_t rc = zfs_lfs_mkdir(_path(path_obj));
    if (rc != ZFS_OK) _raise(rc);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(zfs_mkdir_obj, zfs_mkdir);

// ---------------------------------------------------------------------------
//  zfs.rmdir(path)
// ---------------------------------------------------------------------------

static mp_obj_t zfs_rmdir(mp_obj_t path_obj) {
    zfs_err_t rc = zfs_lfs_rmdir(_path(path_obj));
    if (rc != ZFS_OK) _raise(rc);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(zfs_rmdir_obj, zfs_rmdir);

// ---------------------------------------------------------------------------
//  zfs.exists(path)  →  bool
// ---------------------------------------------------------------------------

static mp_obj_t zfs_exists(mp_obj_t path_obj) {
    bool found = false;
    zfs_err_t rc = zfs_lfs_exists(_path(path_obj), &found);
    if (rc != ZFS_OK) _raise(rc);
    return mp_obj_new_bool(found);
}
static MP_DEFINE_CONST_FUN_OBJ_1(zfs_exists_obj, zfs_exists);

// ---------------------------------------------------------------------------
//  zfs.rename(old, new)
// ---------------------------------------------------------------------------

static mp_obj_t zfs_rename(mp_obj_t old_obj, mp_obj_t new_obj) {
    zfs_err_t rc = zfs_lfs_rename(_path(old_obj), _path(new_obj));
    if (rc != ZFS_OK) _raise(rc);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_2(zfs_rename_obj, zfs_rename);

// ---------------------------------------------------------------------------
//  zfs.listdir([path="/"])  →  list of str
//    Directories are returned with a trailing "/".
// ---------------------------------------------------------------------------

typedef struct {
    mp_obj_t list;
} _listdir_ctx_t;

static int _listdir_cb(const zfs_dirent_t *ent, void *userdata) {
    _listdir_ctx_t *ctx = (_listdir_ctx_t *)userdata;

    char namebuf[ZFS_NAME_MAX + 2]; // +2 for "/" and NUL
    if (ent->type == LFS2_TYPE_DIR) {
        snprintf(namebuf, sizeof(namebuf), "%s/", ent->name);
    } else {
        strncpy(namebuf, ent->name, ZFS_NAME_MAX);
        namebuf[ZFS_NAME_MAX] = '\0';
    }

    mp_obj_list_append(ctx->list, mp_obj_new_str(namebuf, strlen(namebuf)));
    return 0; // continue
}

static mp_obj_t zfs_listdir(size_t n_args, const mp_obj_t *args) {
    const char *path = (n_args > 0) ? _path(args[0]) : "/";

    _listdir_ctx_t ctx = { .list = mp_obj_new_list(0, NULL) };

    zfs_err_t rc = zfs_lfs_listdir(path, _listdir_cb, &ctx);
    if (rc != ZFS_OK) _raise(rc);

    return ctx.list;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(zfs_listdir_obj, 0, 1, zfs_listdir);

// ---------------------------------------------------------------------------
//  Module table
// ---------------------------------------------------------------------------

static const mp_rom_map_elem_t zfs_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),  MP_ROM_QSTR(MP_QSTR_zfs)  },

    // Lifecycle
    { MP_ROM_QSTR(MP_QSTR_mount),     MP_ROM_PTR(&zfs_mount_obj)   },
    { MP_ROM_QSTR(MP_QSTR_umount),    MP_ROM_PTR(&zfs_umount_obj)  },
    { MP_ROM_QSTR(MP_QSTR_format),    MP_ROM_PTR(&zfs_format_obj)  },
    { MP_ROM_QSTR(MP_QSTR_info),      MP_ROM_PTR(&zfs_info_obj)    },

    // File I/O
    { MP_ROM_QSTR(MP_QSTR_write),     MP_ROM_PTR(&zfs_write_obj)   },
    { MP_ROM_QSTR(MP_QSTR_read),      MP_ROM_PTR(&zfs_read_obj)    },
    { MP_ROM_QSTR(MP_QSTR_delete),    MP_ROM_PTR(&zfs_delete_obj)  },
    { MP_ROM_QSTR(MP_QSTR_exists),    MP_ROM_PTR(&zfs_exists_obj)  },
    { MP_ROM_QSTR(MP_QSTR_rename),    MP_ROM_PTR(&zfs_rename_obj)  },

    // Directory
    { MP_ROM_QSTR(MP_QSTR_mkdir),     MP_ROM_PTR(&zfs_mkdir_obj)   },
    { MP_ROM_QSTR(MP_QSTR_rmdir),     MP_ROM_PTR(&zfs_rmdir_obj)   },
    { MP_ROM_QSTR(MP_QSTR_listdir),   MP_ROM_PTR(&zfs_listdir_obj) },
};
static MP_DEFINE_CONST_DICT(zfs_module_globals, zfs_module_globals_table);

const mp_obj_module_t mp_module_zfs = {
    .base    = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&zfs_module_globals,
};

/*
 * Register with MicroPython module system.
 * In mpconfigport.h add:
 *   extern const mp_obj_module_t mp_module_zfs;
 *   #define MICROPY_PORT_BUILTIN_MODULES_EXTRA
 *       { MP_ROM_QSTR(MP_QSTR_zfs), MP_ROM_PTR(&mp_module_zfs) },
 */
MP_REGISTER_MODULE(MP_QSTR_zfs, mp_module_zfs);
