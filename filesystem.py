"""
filesystem.py -- VFS facade for Zeno OS.

FileManager is the only sanctioned way for apps to touch files/dirs;
apps must never touch .vfs metadata files or the `os` module directly.
Also home to the low-level ZFS text wrapper (dewrapper), path-helper
functions, and the filesystem-related custom exceptions.

Note: usermanager/SystemPrivilege (security.py) are imported lazily
inside FileManager methods rather than at module level, since
security.py imports the exceptions defined here -- this avoids a
module-level import cycle between filesystem.py and security.py.
"""
import os
import json
import zfs

class dewrapper:
    @staticmethod
    def read(path):
        return zfs.read(path).decode("utf-8")

    @staticmethod
    def write(path, text):
        zfs.write(path, text.encode("utf-8"))

    @staticmethod
    def lines(path):
        return [l.strip() for l in dewrapper.read(path).splitlines() if l.strip()]

    @staticmethod
    def records(path):
        return [line.split("###") for line in dewrapper.lines(path)]


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _makedirs(path):
    if not path:
        return
    cur = ""
    for p in path.split('/'):
        if not p:
            continue
        cur = cur + "/" + p if cur else p
        if not _exists(cur):
            try:
                os.mkdir(cur)
            except Exception:
                pass


# =============================================================================
# Exceptions
# =============================================================================

class PermissionError(Exception):
    pass


class FileNotFoundError(Exception):
    pass


class FileExistsError(Exception):
    pass


class NotADirectoryError(Exception):
    pass


# =============================================================================
# FileManager -- VFS facade. Apps must never touch .vfs files or `os`
# directly; everything goes through here.
# =============================================================================

SYS_DIR = "/.sys"

PROGRAM_EXT = (".py", ".mpy", ".zsh")
TEXT_EXT = (".txt", ".md", ".log")
BITMAP_EXT = (".bmp",)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
AUDIO_EXT = (".mp3", ".wav", ".ogg")
VIDEO_EXT = (".mp4", ".avi", ".mov")
JSON_EXT = (".json",)
ARCHIVE_EXT = (".zip", ".tar", ".gz")

EXECUTABLE_EXT = (".py", ".mpy")     # auto-executable by default
ROOT_OWNED_EXT = (".py", ".mpy", ".zsh")

PERM_FULL, PERM_READ, PERM_READWRITE, PERM_NONE = 0, 1, 2, 3
PERM_READEXEC, PERM_WRITEEXEC, PERM_WRITE, PERM_EXEC = 4, 5, 6, 7

READ_PERMS = {PERM_FULL, PERM_READ, PERM_READWRITE, PERM_READEXEC}
WRITE_PERMS = {PERM_FULL, PERM_READWRITE, PERM_WRITEEXEC, PERM_WRITE}
EXEC_PERMS = {PERM_FULL, PERM_READEXEC, PERM_WRITEEXEC, PERM_EXEC}
VALID_PERMS = {0, 1, 2, 3, 4, 5, 6, 7}

S_IFDIR = 0x4000



def _os_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _os_isdir(path):
    try:
        return bool(os.stat(path)[0] & S_IFDIR)
    except OSError:
        return False


def _os_mkdir(path):
    if not _os_exists(path):
        os.mkdir(path)


def _os_stat(path):
    try:
        st = os.stat(path)
    except OSError:
        return False, 0
    is_dir = bool(st[0] & S_IFDIR)
    return is_dir, (0 if is_dir else (st[6] if len(st) > 6 else 0))


def _os_rmnode(path):
    (os.rmdir if _os_isdir(path) else os.remove)(path)


def _os_rename(old, new):
    os.rename(old, new)


def _os_listdir(path):
    return os.listdir(path)


def _os_read(path):
    with open(path, "r") as f:
        return f.read()


def _os_write(path, data):
    with open(path, "w") as f:
        f.write(data)


def _norm(path):
    if not path:
        return "/"
    parts = [p for p in str(path).split("/") if p]
    return "/" + "/".join(parts) if parts else "/"


def _parent(path):
    path = _norm(path)
    if path == "/":
        return "/"
    idx = path.rfind("/")
    return path[:idx] if idx > 0 else "/"


def _name(path):
    path = _norm(path)
    return "" if path == "/" else path.rsplit("/", 1)[-1]


def _join(parent, name):
    parent = _norm(parent)
    return name if parent == "/" else parent + "/" + name


def _ext(name):
    idx = name.rfind(".")
    return "" if idx <= 0 else name[idx:].lower()


def _mirror_dir_for(path):
    path = _norm(path)
    return SYS_DIR if path == "/" else SYS_DIR + path


def _meta_file_for(path):
    path = _norm(path)
    return SYS_DIR + "/root.vfs" if path == "/" else SYS_DIR + path + ".vfs"


def is_hidden(name):
    return bool(name) and name.startswith(".")


class FileManager:
    def __init__(self):
        from security import usermanager
        self.um = usermanager()
        self._fd_table = {}
        self._next_fd = 3
        self._bootstrap()

    def _bootstrap(self):
        if not _os_exists(SYS_DIR):
            _os_mkdir(SYS_DIR)
        if not _os_exists(_meta_file_for("/")):
            self._rebuild_meta("/")

    def _current_user(self):
        return self.um.userinfo().get("user", "unknown")

    def _is_root(self):
        return self.um.is_session_root()

    def _check_permission(self, path, action):
        if self._is_root():
            return True
        perm = self.metadata(path).get("permission", PERM_NONE)
        allowed = {"read": READ_PERMS, "write": WRITE_PERMS, "exec": EXEC_PERMS}[action]
        if perm not in allowed:
            raise PermissionError(
                "%s: %s access denied for user '%s'" % (path, action, self._current_user())
            )
        return True

    def _filetype(self, name, is_dir):
        if is_dir:
            return "directory"
        ext = _ext(name)
        for exts, label in (
            (PROGRAM_EXT, "program"), (TEXT_EXT, "text"), (BITMAP_EXT, "bitmap"),
            (IMAGE_EXT, "image"), (AUDIO_EXT, "audio"), (VIDEO_EXT, "video"),
            (JSON_EXT, "json"), (ARCHIVE_EXT, "archive"),
        ):
            if ext in exts:
                return label
        return "unknown"

    def _default_owner(self, name):
        return "root" if _ext(name) in ROOT_OWNED_EXT else self._current_user()

    def _default_permission(self, name, is_dir):
        if is_dir:
            return PERM_FULL
        return PERM_READEXEC if _ext(name) in EXECUTABLE_EXT else PERM_READWRITE

    def _scan_entry(self, parent, name):
        is_dir, size = _os_stat(_join(parent, name))
        return {
            "owner": self._default_owner(name),
            "permission": self._default_permission(name, is_dir),
            "type": self._filetype(name, is_dir),
            "size": size,
        }

    def _ensure_dir_chain(self, path):
        path = _norm(path)
        if path == "/" or _os_exists(path):
            return
        self._ensure_dir_chain(_parent(path))
        _os_mkdir(path)

    def _rebuild_meta(self, dirpath):
        dirpath = _norm(dirpath)
        names = _os_listdir(dirpath) if _os_exists(dirpath) else []
        meta = {}
        for n in names:
            if dirpath == "/" and n == _name(SYS_DIR):
                continue
            meta[n] = self._scan_entry(dirpath, n)
        self._write_meta(dirpath, meta)
        return meta

    def _read_meta(self, dirpath):
        dirpath = _norm(dirpath)
        meta_file = _meta_file_for(dirpath)
        if not _os_exists(meta_file):
            return self._rebuild_meta(dirpath)
        raw = _os_read(meta_file)
        if not raw:
            return self._rebuild_meta(dirpath)
        try:
            return json.loads(raw)
        except Exception:
            return self._rebuild_meta(dirpath)

    def _write_meta(self, dirpath, data, _skip_ensure=False):
        dirpath = _norm(dirpath)
        meta_file = _meta_file_for(dirpath)
        if not _skip_ensure:
            container = SYS_DIR if dirpath == "/" else _mirror_dir_for(_parent(dirpath))
            self._ensure_dir_chain(container)
        _os_write(meta_file, json.dumps(data))

    def exists(self, path):
        return _os_exists(_norm(path))

    def metadata(self, path):
        path = _norm(path)
        if path == "/":
            return {"owner": "root", "permission": PERM_FULL, "type": "directory", "size": 0}
        if not self.exists(path):
            raise FileNotFoundError(path)
        parent, name = _parent(path), _name(path)
        meta = self._read_meta(parent)
        if name not in meta:
            meta = self._rebuild_meta(parent)
        return dict(meta.get(name) or self._scan_entry(parent, name))

    def listdir(self, path="/", show_hidden=False):
        path = _norm(path)
        if not self.exists(path):
            raise FileNotFoundError(path)
        if not _os_isdir(path) and path != "/":
            raise NotADirectoryError(path)
        self._check_permission(path, "read")

        meta = self._read_meta(path)
        real_names = _os_listdir(path)
        if path == "/":
            real_names = [n for n in real_names if n != _name(SYS_DIR)]

        changed = False
        for n in real_names:
            if n not in meta:
                meta[n] = self._scan_entry(path, n)
                changed = True
        for n in list(meta.keys()):
            if n not in real_names:
                del meta[n]
                changed = True
        if changed:
            self._write_meta(path, meta)

        names = real_names if show_hidden else [n for n in real_names if not is_hidden(n)]
        return sorted(names)

    def create(self, path, content="", owner=None, permission=None):
        path = _norm(path)
        parent, name = _parent(path), _name(path)
        if not self.exists(parent):
            raise FileNotFoundError(parent)
        if self.exists(path):
            raise FileExistsError(path)
        self._check_permission(parent, "write")

        owner = owner if owner is not None else self._default_owner(name)
        permission = permission if permission is not None else self._default_permission(name, False)
        if permission not in VALID_PERMS:
            raise ValueError("invalid permission: %r" % (permission,))

        _os_write(path, content)
        _, size = _os_stat(path)
        meta = self._read_meta(parent)
        meta[name] = {"owner": owner, "permission": permission, "type": self._filetype(name, False), "size": size}
        self._write_meta(parent, meta)
        return True

    def mkdir(self, path, owner=None, permission=None):
        path = _norm(path)
        if path == "/":
            raise FileExistsError("root already exists")
        parent, name = _parent(path), _name(path)
        if not self.exists(parent):
            raise FileNotFoundError(parent)
        if self.exists(path):
            raise FileExistsError(path)
        self._check_permission(parent, "write")

        owner = owner if owner is not None else self._default_owner(name)
        permission = permission if permission is not None else self._default_permission(name, True)
        if permission not in VALID_PERMS:
            raise ValueError("invalid permission: %r" % (permission,))

        _os_mkdir(path)
        meta = self._read_meta(parent)
        meta[name] = {"owner": owner, "permission": permission, "type": "directory", "size": 0}
        self._write_meta(parent, meta)

        self._ensure_dir_chain(_mirror_dir_for(path))
        self._write_meta(path, {}, _skip_ensure=True)
        return True

    def delete(self, path):
        path = _norm(path)
        if path == "/":
            raise PermissionError("cannot delete root")
        if not self.exists(path):
            raise FileNotFoundError(path)
        parent, name = _parent(path), _name(path)
        self._check_permission(parent, "write")

        if _os_isdir(path):
            for child in _os_listdir(path):
                self.delete(_join(path, child))
            _os_rmnode(path)
            if _os_exists(_meta_file_for(path)):
                _os_rmnode(_meta_file_for(path))
            if _os_exists(_mirror_dir_for(path)):
                _os_rmnode(_mirror_dir_for(path))
        else:
            _os_rmnode(path)

        meta = self._read_meta(parent)
        if name in meta:
            del meta[name]
            self._write_meta(parent, meta)
        return True

    def rename(self, path, new_name):
        path = _norm(path)
        parent, old_name = _parent(path), _name(path)
        if "/" in new_name:
            new_path = _norm(new_name)
            if _parent(new_path) != parent:
                raise ValueError("rename() cannot change directory, use move()")
            new_name_only = _name(new_path)
        else:
            new_name_only = new_name
            new_path = _join(parent, new_name_only)

        if not self.exists(path):
            raise FileNotFoundError(path)
        if self.exists(new_path):
            raise FileExistsError(new_path)
        self._check_permission(parent, "write")

        is_dir = _os_isdir(path)
        _os_rename(path, new_path)

        meta = self._read_meta(parent)
        entry = meta.pop(old_name, None) or self._scan_entry(parent, new_name_only)
        meta[new_name_only] = entry
        self._write_meta(parent, meta)

        if is_dir:
            if _os_exists(_meta_file_for(path)):
                _os_rename(_meta_file_for(path), _meta_file_for(new_path))
            if _os_exists(_mirror_dir_for(path)):
                _os_rename(_mirror_dir_for(path), _mirror_dir_for(new_path))
        return True

    def move(self, src, dst):
        src, dst = _norm(src), _norm(dst)
        if not self.exists(src):
            raise FileNotFoundError(src)
        if self.exists(dst):
            raise FileExistsError(dst)
        src_parent, src_name = _parent(src), _name(src)
        dst_parent, dst_name = _parent(dst), _name(dst)
        if not self.exists(dst_parent):
            raise FileNotFoundError(dst_parent)
        if src_parent == dst_parent:
            return self.rename(src, dst_name)

        self._check_permission(src_parent, "write")
        self._check_permission(dst_parent, "write")

        is_dir = _os_isdir(src)
        _os_rename(src, dst)

        src_meta = self._read_meta(src_parent)
        entry = src_meta.pop(src_name, None)
        self._write_meta(src_parent, src_meta)

        dst_meta = self._read_meta(dst_parent)
        dst_meta[dst_name] = entry or self._scan_entry(dst_parent, dst_name)
        self._write_meta(dst_parent, dst_meta)

        if is_dir:
            if _os_exists(_meta_file_for(src)):
                _os_rename(_meta_file_for(src), _meta_file_for(dst))
            if _os_exists(_mirror_dir_for(src)):
                _os_rename(_mirror_dir_for(src), _mirror_dir_for(dst))
        return True

    def copy(self, src, dst):
        src, dst = _norm(src), _norm(dst)
        if not self.exists(src):
            raise FileNotFoundError(src)
        if self.exists(dst):
            raise FileExistsError(dst)
        src_parent, dst_parent = _parent(src), _parent(dst)
        if not self.exists(dst_parent):
            raise FileNotFoundError(dst_parent)
        self._check_permission(src_parent, "read")
        self._check_permission(dst_parent, "write")

        src_entry = self._read_meta(src_parent).get(_name(src), {})
        if _os_isdir(src):
            self.mkdir(dst, owner=src_entry.get("owner"), permission=src_entry.get("permission"))
            for child in _os_listdir(src):
                self.copy(_join(src, child), _join(dst, child))
        else:
            self.create(dst, _os_read(src) or "", owner=src_entry.get("owner"), permission=src_entry.get("permission"))
        return True

    def chmod(self, path, permission):
        path = _norm(path)
        if permission not in VALID_PERMS:
            raise ValueError("invalid permission: %r" % (permission,))
        if path == "/":
            raise PermissionError("cannot chmod root")
        parent, name = _parent(path), _name(path)
        meta = self._read_meta(parent)
        entry = meta.get(name)
        if entry is None:
            if not self.exists(path):
                raise FileNotFoundError(path)
            entry = self._scan_entry(parent, name)
        if not self._is_root() and entry.get("owner") != self._current_user():
            raise PermissionError("only the owner or root may chmod '%s'" % path)
        entry["permission"] = permission
        meta[name] = entry
        self._write_meta(parent, meta)
        return True

    def chown(self, path, new_owner):
        path = _norm(path)
        if path == "/":
            raise PermissionError("cannot chown root")
        if not self._is_root():
            raise PermissionError("only root may chown")
        parent, name = _parent(path), _name(path)
        meta = self._read_meta(parent)
        entry = meta.get(name)
        if entry is None:
            if not self.exists(path):
                raise FileNotFoundError(path)
            entry = self._scan_entry(parent, name)
        entry["owner"] = new_owner
        meta[name] = entry
        self._write_meta(parent, meta)
        return True

    def refresh_tree(self, path="/", system_token=None):
        """Permission gate only -- delegates to _refresh_tree_impl().
        Pass system_token=_SYSTEM_TOKEN for trusted kernel callers that
        need this to work without an elevated interactive session."""
        from security import SystemPrivilege, _SYSTEM_TOKEN
        if system_token is not None:
            if system_token != _SYSTEM_TOKEN:
                raise PermissionError("invalid system privilege token")
            with SystemPrivilege(system_token, "refresh_tree"):
                return self._refresh_tree_impl(path)
        if not self._is_root():
            raise PermissionError("only root may refresh the metadata tree")
        return self._refresh_tree_impl(path)

    def _refresh_tree_impl(self, path="/"):
        """No permission check of its own -- always go through refresh_tree()."""
        path = _norm(path)
        if not self.exists(path):
            raise FileNotFoundError(path)
        if path != "/" and not _os_isdir(path):
            raise NotADirectoryError(path)

        refreshed = 0
        stack = [path]
        while stack:
            current = stack.pop()
            try:
                names = _os_listdir(current)
            except OSError:
                continue
            meta = {}
            for n in names:
                if current == "/" and n == _name(SYS_DIR):
                    continue
                entry = self._scan_entry(current, n)
                meta[n] = entry
                if entry["type"] == "directory":
                    stack.append(_join(current, n))
            self._write_meta(current, meta)
            refreshed += 1
        return refreshed

    def open(self, path, mode="r"):
        path = _norm(path)
        parent = _parent(path)
        wants_write = any(c in mode for c in "wa+")
        wants_read = "r" in mode or "+" in mode

        if not self.exists(path):
            if wants_write:
                self._check_permission(parent, "write")
                self.create(path, "")
            else:
                raise FileNotFoundError(path)

        if wants_read:
            self._check_permission(path, "read")
        if wants_write:
            self._check_permission(path, "write")

        handle = open(path, mode)
        fd = self._next_fd
        self._next_fd += 1
        self._fd_table[fd] = handle
        return fd

    def _handle(self, fd):
        handle = self._fd_table.get(fd)
        if handle is None:
            raise ValueError("invalid file descriptor: %r" % (fd,))
        return handle

    def read(self, fd, size=-1):
        return self._handle(fd).read(size)

    def write(self, fd, data):
        return self._handle(fd).write(data)

    def close(self, fd):
        handle = self._fd_table.pop(fd, None)
        if handle is None:
            raise ValueError("invalid file descriptor: %r" % (fd,))
        handle.close()
        return True


def help():
    """Return a description of what's available in this module."""
    return (
        "filesystem.py -- VFS facade\n"
        "  FileManager()\n"
        "    .exists(path) / .metadata(path) / .listdir(path='/', show_hidden=False)\n"
        "    .create(path, content='', owner=None, permission=None)\n"
        "    .mkdir(path, owner=None, permission=None) / .delete(path)\n"
        "    .rename(path, new_name) / .move(src, dst) / .copy(src, dst)\n"
        "    .chmod(path, permission) / .chown(path, new_owner)\n"
        "    .refresh_tree(path='/', system_token=None)\n"
        "    .open(path, mode='r') / .read(fd, size=-1) / .write(fd, data) / .close(fd)\n"
        "  dewrapper -- low-level ZFS text wrapper (.read/.write/.lines/.records)\n"
        "  Exceptions: PermissionError, FileNotFoundError, FileExistsError, NotADirectoryError"
    )
