"""
security.py -- privilege/session model for Zeno OS.

SystemPrivilege is a trust seam (not a hard security boundary -- single
interpreter, no process isolation) that lets trusted kernel code
(PackageManager, boot sequencer, FileManager.refresh_tree) run
root-gated operations independent of whether the interactive user is
currently elevated. usermanager is the user/session/root layer built
on top of it.
"""
import os
import time
import json
import zfs
import zeno

from filesystem import dewrapper, PermissionError
from process import ProcessError

try:
    _SYSTEM_TOKEN = os.urandom(16)
except Exception:
    _SYSTEM_TOKEN = bytes(str(time.ticks_us()), "utf-8")


class SystemPrivilege:
    _active_depth = 0

    def __init__(self, token, reason="system operation"):
        if token != _SYSTEM_TOKEN:
            raise PermissionError("invalid system privilege token")
        self.reason = reason

    def __enter__(self):
        SystemPrivilege._active_depth += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        SystemPrivilege._active_depth -= 1
        return False

    @classmethod
    def active(cls):
        return cls._active_depth > 0

class usermanager:
    # Class-level, shared by every usermanager() instance in the process.
    # Always False on interpreter start -- normal mode by default on
    # every boot, regardless of what "root" says in userinfo.json.
    _session_root = False

    def __init__(self):
        self.path = "OS/users/userinfo.json"
        self.d = dewrapper()

    def _checkpath(self, path):
        return zfs.exists(path)

    def _require_system(self):
        # Internal-only gate. Anything below this line may only be
        # entered from inside a SystemPrivilege(_SYSTEM_TOKEN, ...)
        # block -- i.e. from trusted code in this same class, not from
        # apps, modules, or the shell.
        if not SystemPrivilege.active():
            raise PermissionError("access denied")

    def __read__(self):
        self._require_system()
        if not self._checkpath("OS"):
            zfs.mkdir("OS")
        if not self._checkpath("OS/users"):
            zfs.mkdir("OS/users")
        if not self._checkpath(self.path):
            self.d.write(self.path, json.dumps({
                "user": zeno.user, "password": zeno.password, "root": False
            }))

        data = self.d.read(self.path)
        if not data:
            zfs.delete(self.path)
            return self.__read__()
        try:
            parsed = json.loads(data)
        except Exception:
            zfs.delete(self.path)
            return self.__read__()

        # zeno.py is the source of truth for identity. If the stored
        # record belongs to a different user than the one zeno.py
        # currently declares, resync the record instead of silently
        # comparing against stale credentials.
        if parsed.get("user") != zeno.user:
            parsed = {"user": zeno.user, "password": zeno.password, "root": False}
            self.d.write(self.path, json.dumps(parsed))

        return parsed

    def _write(self, key, value):
        self._require_system()
        a = self.__read__()
        a[key] = value
        if self._checkpath(self.path):
            self.d.write(self.path, json.dumps(a))

    def __username__(self):
        self._require_system()
        return self.__read__()["user"]

    def __password__(self):
        self._require_system()
        return self.__read__()["password"]

    def userinfo(self):
        with SystemPrivilege(_SYSTEM_TOKEN, "userinfo"):
            info = dict(self.__read__())
        info.pop("password", None)
        return info

    def removeuser(self, name):
        with SystemPrivilege(_SYSTEM_TOKEN, "removeuser"):
            user = self.__username__()
            data = self.__read__()
            if name == user and data["root"] is True:
                print(f"{user} is deleted and default user name will be created")
                zfs.delete(self.path)
                self.__read__()
            else:
                print("user does't exists or not root")

    def is_session_root(self):
        """The only thing permission checks should ever call."""
        return usermanager._session_root or SystemPrivilege.active()

    def elevate(self, user, password):
        with SystemPrivilege(_SYSTEM_TOKEN, "elevate"):
            ok = (user == self.__username__() and str(password) == str(self.__password__()))
            if ok and not self.__read__().get("root", False):
                self._write("root", True)
                print(f"{user} promoted to administrator")
        if ok:
            usermanager._session_root = True
            print("elevated user (this session)")
        else:
            print("user or password is wrong")

    def delevate(self, user, password):
        with SystemPrivilege(_SYSTEM_TOKEN, "delevate"):
            ok = (user == self.__username__() and str(password) == str(self.__password__()))
        if ok:
            usermanager._session_root = False
            print("delevated user")
        else:
            print("user or password is wrong")

    def isrooted(self, user):
        """True only if elevated THIS session -- not just an admin
        account on paper."""
        with SystemPrivilege(_SYSTEM_TOKEN, "isrooted"):
            current = self.__username__()
        if user == current:
            return self.is_session_root()
        return False

    # ---------------- self-service identity changes ----------------

    def change_password(self, user, old_password, new_password):
        """Change the account password. Requires the current password
        (not just root) -- same as delevate/elevate."""
        with SystemPrivilege(_SYSTEM_TOKEN, "change_password"):
            ok = (user == self.__username__() and str(old_password) == str(self.__password__()))
            if ok:
                self._write("password", new_password)
        if ok:
            print("password changed")
            return True
        print("user or password is wrong")
        return False

    def change_username(self, user, password, new_username):
        """Change the stored username. Requires the current password.
        Note: this only updates userinfo.json -- if zeno.py's `user`
        field doesn't match afterward, __read__() will resync the
        record back to zeno.user on next access, undoing this. Update
        zeno.py's `user` value too if you want the change to persist."""
        with SystemPrivilege(_SYSTEM_TOKEN, "change_username"):
            ok = (user == self.__username__() and str(password) == str(self.__password__()))
            if ok:
                self._write("user", new_username)
        if ok:
            print(f"username changed to {new_username}")
            return True
        print("user or password is wrong")
        return False
    def rebuild(self, system_token=None):
        if system_token != _SYSTEM_TOKEN:
            raise PermissionError("invalid system privilege token")

        with SystemPrivilege(system_token, "rebuild"):
            if not self._checkpath("OS"):
                zfs.mkdir("OS")
            if not self._checkpath("OS/users"):
                zfs.mkdir("OS/users")

            if self._checkpath(self.path):
                zfs.delete(self.path)

            fresh = {"user": zeno.user, "password": zeno.password, "root": False}
            self.d.write(self.path, json.dumps(fresh))

            # verify it round-trips before declaring success
            try:
                check = json.loads(self.d.read(self.path))
            except Exception:
                raise ProcessError("rebuild: wrote userinfo.json but could not read it back")
            if check.get("user") != zeno.user:
                raise ProcessError("rebuild: verification failed after write")

        usermanager._session_root = False
        return True
    def current_user(self):
        """Public accessor -- safe for external callers (PackageManager,
        etc). Does not require system privilege."""
        with SystemPrivilege(_SYSTEM_TOKEN, "current_user"):
            return self.__username__()


def help():
    """Return a description of what's available in this module."""
    return (
        "security.py -- privilege / user / session model\n"
        "  SystemPrivilege(token, reason='system operation')  -- context manager, trusted-code gate\n"
        "    SystemPrivilege.active()  -- classmethod, True while inside such a block\n"
        "  usermanager()\n"
        "    .userinfo() / .current_user() / .is_session_root()\n"
        "    .elevate(user, password) / .delevate(user, password)\n"
        "    .isrooted(user)\n"
        "    .change_password(user, old_password, new_password)\n"
        "    .change_username(user, password, new_username)\n"
        "    .removeuser(name) / .rebuild(system_token=None)"
    )
