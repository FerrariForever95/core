"""
packages.py -- package/app management + networked file transfer helpers
for Zeno OS.

downloadhelper (raw-socket HTTP(S) file download), Git (GitHub
download/upload helper), AppInstaller, Wiki, AppDB, and PackageManager
(the module-based package manager with the command registry that
ZenCMD dispatches through).
"""
import sys
import os
import json
import time
import gc
import usocket
import ssl
import urequests
import zeno

from logger import Logger
from filesystem import FileManager
from security import usermanager
from system import Disk

class downloadhelper:
    def __init__(self):
        self.log = Logger()

    def download_file(self, url, save_dir="/", save_file=None):
        if not url:
            self.log.error("Download failed: empty URL provided", source="DOWNLOADSERV")
            return None

        url = url.strip()
        try:
            if url.startswith("http:/") and not url.startswith("http://"):
                url = url.replace("http:/", "http://", 1)
            elif url.startswith("https:/") and not url.startswith("https://"):
                url = url.replace("https:/", "https://", 1)
            elif not (url.startswith("http://") or url.startswith("https://")):
                url = "http://" + url
        except Exception as e:
            self.log.error("Failed to normalize URL '{}': {}".format(url, e), source="DOWNLOADSERV")
            return None

        try:
            proto, _, hostport, *rest = url.split("/", 3)
            host = hostport.split(":")[0]
            port = 443 if proto == "https:" else 80
            path = "/" + (rest[0] if rest else "")
            if path == "/":
                path = "/index.html"
        except Exception as e:
            self.log.error("Invalid URL format '{}': {}".format(url, e), source="DOWNLOADSERV")
            return None

        if not save_file:
            save_file = path.split("/")[-1] or "index.html"

        try:
            addr_info = usocket.getaddrinfo(host, port)
            if not addr_info:
                self.log.error("DNS resolution returned no results for host '{}'".format(host), source="DOWNLOADSERV")
                return None
            addr = addr_info[0][-1]
        except Exception as e:
            self.log.error("DNS resolution failed for host '{}': {}".format(host, e), source="DOWNLOADSERV")
            return None

        try:
            s = usocket.socket()
        except Exception as e:
            self.log.error("Failed to create socket: {}".format(e), source="DOWNLOADSERV")
            return None

        try:
            try:
                s.connect(addr)
            except Exception as e:
                self.log.error("Failed to connect to {}:{} -> {}".format(host, port, e), source="DOWNLOADSERV")
                return None

            if proto == "https:":
                try:
                    s = ssl.wrap_socket(s)
                except Exception as e:
                    self.log.error("SSL wrap failed for '{}': {}".format(url, e), source="DOWNLOADSERV")
                    return None

            try:
                s.send("GET {} HTTP/1.0\r\nHost: {}\r\n\r\n".format(path, host).encode())
            except Exception as e:
                self.log.error("Failed to send HTTP request to '{}': {}".format(url, e), source="DOWNLOADSERV")
                return None

            try:
                status_line = s.readline()
            except Exception as e:
                self.log.error("Failed to read HTTP status from '{}': {}".format(url, e), source="DOWNLOADSERV")
                return None

            if not status_line:
                self.log.warning("No response from server '{}'".format(url), source="DOWNLOADSERV")
                return None

            try:
                parts = status_line.decode().split()
            except Exception as e:
                self.log.error("Failed to decode HTTP status line from '{}': {}".format(url, e), source="DOWNLOADSERV")
                return None

            if len(parts) < 2 or parts[1] != "200":
                self.log.error("HTTP error from '{}': {}".format(url, status_line.decode().strip()), source="DOWNLOADSERV")
                return None

            try:
                while True:
                    line = s.readline()
                    if not line or line == b"\r\n":
                        break
            except Exception as e:
                self.log.error("Failed while reading HTTP headers from '{}': {}".format(url, e), source="DOWNLOADSERV")
                return None

            try:
                if save_dir not in os.listdir("/"):
                    try:
                        os.mkdir(save_dir)
                    except Exception as e_mk:
                        self.log.error("Failed to create directory '{}': {}".format(save_dir, e_mk), source="DOWNLOADSERV")
                        return None
            except Exception as e:
                self.log.error("Failed to list root directories while checking '{}': {}".format(save_dir, e), source="DOWNLOADSERV")
                return None

            full_path = save_dir + "/" + save_file
            try:
                with open(full_path, "wb") as f:
                    while True:
                        try:
                            data = s.recv(512)
                        except Exception as e:
                            self.log.error("Socket recv failed from '{}': {}".format(url, e), source="DOWNLOADSERV")
                            return None
                        if not data:
                            break
                        try:
                            f.write(data)
                        except Exception as e:
                            self.log.error("Failed writing to '{}': {}".format(full_path, e), source="DOWNLOADSERV")
                            return None
            except Exception as e:
                self.log.error("Failed to open/write file '{}': {}".format(full_path, e), source="DOWNLOADSERV")
                return None

            self.log.debug("File saved successfully -> {}".format(full_path), source="DOWNLOADSERV")
            return full_path

        except Exception as e:
            self.log.error("Error during download from '{}': {}".format(url, e), source="DOWNLOADSERV")
            return None
        finally:
            try:
                s.close()
            except Exception:
                pass


class Git:
    def __init__(self, base_raw=None, default_branch="main"):
        self.logger = Logger()
        self.base_raw = base_raw or "https://raw.githubusercontent.com"
        self.default_branch = default_branch
        self.default_download_dir = "/"
        self.source = "GITSERV"
        self.token = zeno.gitsecret
        self._http = None

    def download_url(self, url, save_dir=None):
        try:
            user, repo, branch, filename = self._parse_github_url(url)
        except ValueError as e:
            self.logger.error(str(e), source=self.source)
            print("Download failed: invalid GitHub URL.")
            return False
        return self.download(user, repo, filename, branch=branch, save_dir=save_dir)

    def download(self, user, repo, filename, branch=None, save_dir=None):
        filename = filename or ""
        if filename.startswith("http://") or filename.startswith("https://"):
            try:
                parsed_user, parsed_repo, parsed_branch, filename = self._parse_github_url(filename)
            except ValueError as e:
                self.logger.error(str(e), source=self.source)
                print("Download failed: invalid GitHub URL.")
                return False
            user = user or parsed_user
            repo = repo or parsed_repo
            branch = branch or parsed_branch

        if not user or not repo:
            self.logger.error("Download failed: 'user' and 'repo' are required.", source=self.source)
            print("Download failed: missing repository information.")
            return False

        branch = branch or self.default_branch
        filename = filename.lstrip("/")
        if not filename:
            self.logger.error("Download failed: empty file path.", source=self.source)
            print("Download failed: no file specified.")
            return False

        original_filename = filename
        url = self._build_url(user, repo, self._encode_path(filename), branch)

        target_dir = self._resolve_download_dir(save_dir if save_dir is not None else self.default_download_dir)
        fname = original_filename.split("/")[-1]
        full_path = "{}/{}".format(target_dir.rstrip("/") or "", fname)
        if not full_path.startswith("/"):
            full_path = "/" + full_path

        print("Cloning '{}' from {}/{}...".format(fname, user, repo))
        self.logger.debug("Downloading {} -> {}".format(url, full_path), source=self.source)

        http = self._get_http()
        if http is None:
            print("Download failed: no HTTP client available.")
            return False

        # raw.githubusercontent.com returns 404 (not 401) for a bad auth
        # header, indistinguishable from "file not found" -- so try
        # unauthenticated first, retry with token only on 404.
        headers = {"User-Agent": "ZenoMicroPC"}
        try:
            resp = http.get(url, headers=headers)
        except Exception as e:
            self.logger.error("HTTP request failed for URL {}: {!r}".format(url, e), source=self.source)
            print("Download failed: connection error.")
            return False

        try:
            status = resp.status_code
        except Exception:
            status = 0

        if status == 404 and self.token:
            try:
                resp.close()
            except Exception:
                pass
            auth_headers = dict(headers)
            auth_headers["Authorization"] = "token {}".format(self.token)
            self.logger.debug("Unauthenticated request 404'd, retrying with token", source=self.source)
            try:
                resp = http.get(url, headers=auth_headers)
                status = resp.status_code
            except Exception as e:
                self.logger.error("HTTP retry-with-auth failed for URL {}: {!r}".format(url, e), source=self.source)
                print("Download failed: connection error.")
                return False

        if status != 200:
            self.logger.error("HTTP {} while downloading {} from {}".format(status, filename, url), source=self.source)
            if status == 404:
                print("Download failed: file not found.")
            else:
                print("Download failed: server returned an error.")
            try:
                resp.close()
            except Exception:
                pass
            return False

        try:
            data = resp.content
        except Exception:
            try:
                data = resp.text
                if isinstance(data, str):
                    data = data.encode()
            except Exception as e:
                self.logger.error("Failed to read HTTP response body: {}".format(e), source=self.source)
                print("Download failed: could not read response.")
                try:
                    resp.close()
                except Exception:
                    pass
                return False

        try:
            with open(full_path, "wb") as f:
                f.write(data)
        except Exception as e:
            self.logger.error("Failed to write file '{}': {}".format(full_path, e), source=self.source)
            print("Download failed: could not save file.")
            try:
                resp.close()
            except Exception:
                pass
            return False

        try:
            resp.close()
        except Exception:
            pass

        self.logger.debug("Download completed. Saved as '{}'".format(full_path), source=self.source)
        print("Downloaded '{}' successfully.".format(fname))
        return True

    def _get_http(self):
        if self._http is not None:
            return self._http
        try:
            import requests as _req
            self._http = _req
        except ImportError:
            try:
                import urequests as _req
                self._http = _req
            except ImportError as e:
                self.logger.error("No HTTP client available (requests/urequests): {}".format(e), source=self.source)
                return None
        return self._http

    def _parse_github_url(self, url):
        url = (url or "").strip()

        if url.startswith("https://raw.githubusercontent.com/"):
            parts = url[len("https://raw.githubusercontent.com/"):].split("/", 3)
            if len(parts) < 4 or not parts[3]:
                raise ValueError("Malformed raw.githubusercontent.com URL: {}".format(url))
            return parts[0], parts[1], parts[2], parts[3]

        if url.startswith("https://github.com/"):
            rest = url[len("https://github.com/"):]
            for marker in ("/blob/", "/raw/"):
                if marker in rest:
                    repo_part, _, tail = rest.partition(marker)
                    if "/" not in repo_part:
                        raise ValueError("Malformed github.com URL (missing repo): {}".format(url))
                    user, repo = repo_part.split("/", 1)
                    branch, _, path = tail.partition("/")
                    if not branch or not path:
                        raise ValueError("Malformed github.com URL (missing branch/path): {}".format(url))
                    return user, repo, branch, path
            raise ValueError("Unrecognized github.com URL (expected '/blob/' or '/raw/' in path): {}".format(url))

        raise ValueError("Unrecognized GitHub URL format: {}".format(url))

    def _encode_path(self, path):
        safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~/"
        return "".join(ch if ch in safe else "%{:02X}".format(ord(ch)) for ch in path)

    def _build_url(self, user, repo, encoded_path, branch):
        if encoded_path.startswith("/"):
            encoded_path = encoded_path[1:]
        return "/".join([self.base_raw.rstrip("/"), user, repo, branch, encoded_path])

    def _resolve_download_dir(self, preferred_dir):
        preferred_dir = preferred_dir or "/"
        if preferred_dir in ("", "/"):
            return "/"

        curr = ""
        try:
            for p in [p for p in preferred_dir.strip("/").split("/") if p]:
                curr = curr + "/" + p
                try:
                    os.stat(curr)
                except OSError:
                    try:
                        os.mkdir(curr)
                    except Exception as e:
                        self.logger.warning("Failed to create '{}' ({}), falling back to '/'".format(curr, e), source=self.source)
                        return "/"
            return curr or "/"
        except Exception as e:
            self.logger.warning("Error resolving download dir '{}': {}. Falling back to '/'".format(preferred_dir, e), source=self.source)
            return "/"

    def upload(self, user, repo, local_path, repo_path=None, branch=None, message="Upload from ESP32"):
        import ubinascii
        token = self.token
        if not token:
            self.logger.error("Upload failed: no GitHub token configured (zeno.gitsecret).", source=self.source)
            print("Upload failed: no GitHub token configured.")
            return False

        branch = branch or self.default_branch
        repo_path = (repo_path or local_path.split("/")[-1]).lstrip("/")

        try:
            with open(local_path, "rb") as f:
                data = f.read()
        except Exception as e:
            self.logger.error("Git upload: cannot read local file '{}': {}".format(local_path, e), source=self.source)
            print("Upload failed: could not read local file.")
            return False

        try:
            b64 = ubinascii.b2a_base64(data).decode().strip()
        except Exception as e:
            self.logger.error("Git upload: base64 encode failed: {}".format(e), source=self.source)
            print("Upload failed: could not encode file.")
            return False

        print("Uploading '{}' to {}/{}...".format(repo_path, user, repo))

        api_url = "https://api.github.com/repos/{}/{}/contents/{}".format(user, repo, self._encode_path(repo_path))
        http = self._get_http()
        if http is None:
            print("Upload failed: no HTTP client available.")
            return False

        headers = {"Authorization": "token {}".format(token), "User-Agent": "ZenoMicroPC", "Accept": "application/vnd.github+json"}

        sha = None
        try:
            r = http.get(api_url + "?ref={}".format(branch), headers=headers)
            try:
                if r.status_code == 200:
                    try:
                        sha = r.json().get("sha")
                    except Exception:
                        sha = None
            finally:
                r.close()
        except Exception as e:
            self.logger.debug("Git upload: GET existing file failed/404 (new file maybe): {}".format(e), source=self.source)

        body = {"message": message, "content": b64, "branch": branch}
        if sha:
            body["sha"] = sha

        try:
            r = http.put(api_url, headers=headers, json=body)
        except Exception as e:
            self.logger.error("Git upload: PUT request failed: {}".format(e), source=self.source)
            print("Upload failed: connection error.")
            return False

        try:
            status = r.status_code
        except Exception:
            status = 0

        if status not in (200, 201):
            try:
                err_txt = r.text
            except Exception:
                err_txt = "no body"
            self.logger.error("Git upload: HTTP {} from GitHub: {}".format(status, err_txt), source=self.source)
            print("Upload failed: server returned an error.")
            try:
                r.close()
            except Exception:
                pass
            return False

        try:
            r.close()
        except Exception:
            pass

        self.logger.debug("Git upload completed: {} -> {}/{} ({})".format(local_path, user, repo, repo_path), source=self.source)
        print("Uploaded '{}' successfully.".format(repo_path))
        return True

# =============================================================================
# AppInstaller
# =============================================================================

class AppInstaller:
    def __init__(self):
        self.user = "FerrariForever95"
        self.repo = "Zeno-Micro-PC"
        self.branch = "main"
        self.remote_base = "APPS"
        self.git = Git(default_branch=self.branch)
        self.apps_dir = "/SYSTEM32/APPS"

    def prompt_and_install(self):
        try:
            app_name = input("Enter app name to install (without .py): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("Install cancelled.")
            return False
        if not app_name:
            print("No app name given.")
            return False
        return self.install(app_name)

    def _build_remote_path(self, app_name: str) -> str:
        base = (self.remote_base or "").strip().strip("/")
        return f"{base}/{app_name}.py" if base else f"{app_name}.py"

    def install(self, app_name):
        remote_path = self._build_remote_path(app_name)
        print("[APPINST] Installing app:", app_name)

        try:
            d = Disk()
            d.begin()
        except Exception as e:
            print("[APPINST] WARNING: Disk init failed:", e)

        old_dir = self.git.default_download_dir
        self.git.default_download_dir = self.apps_dir
        try:
            ok = self.git.download(self.user, self.repo, remote_path, branch=self.branch)
        finally:
            self.git.default_download_dir = old_dir

        if ok:
            print("[APPINST] App '{}' installed to {}/{}.py".format(app_name, self.apps_dir, app_name))
            try:
                time.sleep(1)
            except Exception:
                pass
        else:
            print("[APPINST] Failed to install app '{}'".format(app_name))
        return ok

    def uninstall(self, name):
        d = Disk()
        d.begin()
        try:
            os.remove(f"/SYSTEM32/APPS/{name}.py")
            print("[APPINST] uninstalled", name)
        except OSError as e:
            print("[APPINST] uninstall failed:", e)

    def listapps(self):
        files = os.listdir("/SYSTEM32/APPS")
        print("Detected apps:", [f[:-3] for f in files if f.endswith(".py")])


# =============================================================================
# Wiki
# =============================================================================

_HEADERS = {"User-Agent": "ZenoOS/1.0", "Accept": "application/json"}


class Wiki:
    def __init__(self, lang="en", width=60, lines=10, out=print):
        self.lang = lang
        self.width = width
        self.lines = lines
        self.out = out
        self.buf = []
        self.pos = 0

    def _wrap(self, text):
        out = []
        for raw in text.split("\n"):
            s = raw.strip()
            if not s:
                out.append("")
                continue
            while len(s) > self.width:
                cut = s.rfind(" ", 0, self.width)
                if cut < 0:
                    cut = self.width
                out.append(s[:cut])
                s = s[cut:].lstrip()
            out.append(s)
        return out

    def fetch(self, title, preview_dots=3):
        self.buf, self.pos = [], 0
        url = "https://{}.wikipedia.org/api/rest_v1/page/summary/{}".format(self.lang, title.replace(" ", "%20"))

        try:
            r = urequests.get(url, headers=_HEADERS)
            if r.status_code != 200:
                self.out("[wiki] http", r.status_code)
                return None
            text = r.json().get("extract", "")
            r.close()
            if not text:
                self.out("[wiki] empty article")
                return None

            self.buf = self._wrap(text)
            gc.collect()

            dots, printed = 0, 0
            for line in self.buf:
                self.out(line)
                printed += 1
                dots += line.count(".")
                if dots >= preview_dots or printed >= self.lines:
                    break
            self.pos = printed
            return None
        except Exception as e:
            self.out("[wiki] error:", e)
            return None

    def next(self):
        if self.pos >= len(self.buf):
            self.out("[wiki] end of article")
            return None
        end = self.pos + self.lines
        for line in self.buf[self.pos:end]:
            self.out(line)
        self.pos = end
        return None

    def search(self, query, n=5):
        url = "https://{}.wikipedia.org/w/api.php?action=query&list=search&format=json&srsearch={}".format(self.lang, query.replace(" ", "%20"))
        try:
            r = urequests.get(url, headers=_HEADERS)
            data = r.json()
            r.close()
            for i, item in enumerate(data["query"]["search"][:n]):
                self.out(i + 1, item["title"])
            return None
        except Exception as e:
            self.out("[wiki] search error:", e)
            return None


# =============================================================================
# AppDB
# =============================================================================

_DB_DIR = "/SYSTEM32/APPS/Data"
_DB_FILE = _DB_DIR + "/appdb.json"


class AppDB:
    def __init__(self):
        self._data = {}
        self._load()

    def _load(self):
        try:
            if _DB_DIR not in os.listdir("/SYSTEM32/APPS"):
                os.mkdir(_DB_DIR)
        except Exception:
            pass
        try:
            with open(_DB_FILE, "r") as f:
                self._data = json.load(f)
        except Exception:
            self._data = {}

    def _save(self):
        try:
            with open(_DB_FILE, "w") as f:
                json.dump(self._data, f)
        except Exception as e:
            print("[TinyAppDB] save failed:", e)

    def set(self, app, key, value):
        app, key = str(app), str(key)
        self._data.setdefault(app, {})[key] = value
        self._save()

    def get(self, app, key, default=None):
        try:
            return self._data.get(app, {}).get(key, default)
        except Exception:
            return default

    def delete(self, app, key):
        try:
            del self._data[app][key]
            if not self._data[app]:
                del self._data[app]
            self._save()
        except Exception:
            pass

    def clear(self, app):
        if app in self._data:
            del self._data[app]
            self._save()

    def dump(self):
        return self._data


class PackageManager:
    DEFAULT_USER = "FerrariForever95"
    DEFAULT_REPO = "Zeno-Micro-PC"
    PKGLIST_PATH = "/pkglist.json"
    PKGTABLE_CACHE_PATH = "/pkgtable_cache.json"

    def __init__(self, git=None, repo_user=None, repo_name=None):
        self.logger = Logger()
        self.source = "PKGMGR"
        self.git = git or Git()
        self.fm = FileManager()
        self.um = usermanager()
        self.repo_user = repo_user or self.DEFAULT_USER
        self.repo_name = repo_name or self.DEFAULT_REPO
        # nested install() calls (deps, module providers) share one
        # pkgtable.json download; only the outermost call deletes it
        self._op_depth = 0
        # in-memory copy of the pkgtable for the duration of one
        # outermost operation, so nested calls don't re-download it
        self._pkgtable_cache = None

        # -----------------------------------------------------------
        # Command registry -- maps command name -> callable.
        # This is what makes an installed package immediately usable
        # as a shell command in ZenCMD, without a reboot.
        # Kept as a single flat dict (name -> function reference) to
        # minimize RAM: no wrapper objects, no per-package class
        # instances, just a reference to the already-imported module's
        # function.
        # -----------------------------------------------------------
        self.commands = {}
        # command name -> module name, so unregister()/reload can find
        # and drop the right entry from sys.modules without guessing
        self._command_modules = {}
        # set to the module name currently running its install(pm)
        # hook, so register() can auto-record which module owns which
        # command without requiring package authors to pass it
        # explicitly (MicroPython has no `inspect` to derive this from
        # the call stack)
        self._installing_module = None

        # Bring every previously-installed package's command(s) back
        # online automatically -- without this, self.commands starts
        # empty on every boot and stays empty until something happens
        # to reinstall a package. Failures are logged per-package and
        # don't prevent the rest of PackageManager from working.
        self.load_all_installed()

    def install(self, name, force=False):
        if not self._require_root("install"):
            return False

        self._begin_op()
        try:
            print("[PKG] === Installing '{}' ===".format(name))
            pkgtable = self._fetch_pkgtable()
            if pkgtable is None:
                return False

            entry = self._find_package(pkgtable, name)
            if entry is None:
                self._error("Package '{}' not found in pkgtable.json".format(name))
                return False

            installed = self._load_pkglist()
            if name in installed and not force:
                print("[PKG] '{}' is already installed (v{}). Use update() or reinstall() instead.".format(name, installed[name].get("version")))
                return False

            prior_record = installed.get(name) if force else None

            for dep in entry.get("dependencies", []):
                if not self._ensure_dependency(dep, pkgtable, installed):
                    self._error("Failed to satisfy dependency '{}' for '{}'".format(dep.get("name"), name))
                    return False
                installed = self._load_pkglist()

            required_modules = entry.get("modules", [])
            missing = self._check_modules(required_modules)
            if missing:
                if not self._resolve_missing_modules(missing, pkgtable, installed):
                    self._error("Cannot install '{}': missing required module(s) with no available package in pkgtable.json: {}".format(name, ", ".join(missing)))
                    return False
                installed = self._load_pkglist()
                still_missing = self._check_modules(required_modules)
                if still_missing:
                    self._error("Cannot install '{}': still missing module(s) after attempted auto-install: {}".format(name, ", ".join(still_missing)))
                    return False

            if not self._download_package(entry):
                self._error("Failed to download package '{}'".format(name))
                return False

            installed = self._load_pkglist()
            installed[name] = self._make_pkglist_entry(entry)
            if not self._save_pkglist(installed):
                return False

            self.fm.refresh_tree(entry["install_path"])

            # a force-install that moved filename/install_path leaves an
            # orphaned old file behind -- clean it up
            if prior_record:
                old_path = self._full_path(prior_record.get("install_path"), prior_record.get("filename"))
                new_path = self._full_path(entry.get("install_path"), entry.get("file", "").split("/")[-1])
                if old_path != new_path:
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass

            # bring the new command online immediately -- import the
            # module and let it register itself, no reboot required
            self._load_and_install_module(name, installed[name])

            print("[PKG] '{}' v{} installed successfully.".format(name, entry.get("version")))
            return True
        finally:
            self._end_op()

    def _module_name_for(self, record):
        """
        Derive an importable module name for a package record.

        Packages are plain .py modules dropped under install_path, so
        the module name is just the filename without the .py suffix.
        install_path is expected to already be on sys.path (or be '/'
        or '/lib', which MicroPython searches by default) -- this
        mirrors how packages were located for exec() under the old
        architecture, just importing instead of exec'ing.
        """
        filename = record.get("filename", "")
        if filename.endswith(".py"):
            filename = filename[:-3]
        return filename

    def _ensure_on_path(self, install_path):
        """
        Make sure a package's install_path is importable.

        __import__() only finds a module if its directory is in
        sys.path. '/' and '/lib' are on sys.path by default in
        MicroPython, but any other install_path (e.g. '/pkgs/wifimgr')
        is not -- so without this, packages installed anywhere else
        would import-fail every time, including right after a fresh
        install() and especially on the next boot. Called before every
        import; a no-op if the path is already present.
        """
        path = (install_path or "/").rstrip("/") or "/"
        if path not in sys.path:
            sys.path.append(path)

    def _load_and_install_module(self, name, record):
        """
        Import a package's module (fresh, dropping any stale cached
        copy first) and call its install(pm) hook if present.

        This is the core of the new architecture: a package is just a
        module with a module-level install(pm)/uninstall(pm) and a
        run(args, shell) function. Importing it and calling install()
        is what makes its command appear in self.commands right away.
        """
        module_name = self._module_name_for(record)
        if not module_name:
            self._error("Cannot determine module name for package '{}'".format(name))
            return False

        self._ensure_on_path(record.get("install_path"))

        # drop any previously-imported copy so we always pick up the
        # freshly-downloaded file (needed for update()/reinstall())
        if module_name in sys.modules:
            del sys.modules[module_name]

        try:
            module = __import__(module_name)
        except Exception as e:
            self._error("Could not import module '{}' for package '{}': {}".format(module_name, name, e))
            return False

        installer = getattr(module, "install", None)
        if installer is not None:
            self._installing_module = module_name
            try:
                installer(self)
            except Exception as e:
                self._error("install(pm) failed for package '{}': {}".format(name, e))
                return False
            finally:
                self._installing_module = None
        else:
            # older/simple packages may not define install() at all --
            # that's fine, they just won't appear in self.commands and
            # can still be run via the legacy exec path
            self.logger.debug("Package '{}' has no install(pm) hook".format(name), source=self.source)

        return True

    def load_all_installed(self):
        """
        Auto-import every package listed in /pkglist.json and run its
        install(pm) hook, bringing all previously-installed commands
        back online without the user having to reinstall or manually
        re-trigger anything.

        This is what makes packages actually persistent across a
        reboot: install()/update() already did this for a single
        package right after installing it, but nothing previously
        replayed that step for the *whole* pkglist.json on startup --
        so a fresh PackageManager() used to come up on every boot with
        an empty self.commands until something happened to reinstall a
        package. Call this once, right after constructing
        PackageManager (e.g. from boot.py / wherever `zeno.sched` and
        friends get wired up).

        Failures for one package are logged and skipped rather than
        aborting the rest, so one broken package can't take every
        other package's commands down with it.
        """
        installed = self._load_pkglist()
        if not installed:
            self.logger.debug("No installed packages to auto-import.", source=self.source)
            return True

        all_ok = True
        for name, record in installed.items():
            try:
                ok = self._load_and_install_module(name, record)
            except Exception as e:
                ok = False
                self._error("Auto-import crashed for '{}': {}".format(name, e))
            if ok:
                self.logger.debug("Auto-imported '{}' on startup.".format(name), source=self.source)
            else:
                self.logger.warning("Failed to auto-import '{}' on startup -- command(s) from it will be unavailable until fixed/reinstalled.".format(name), source=self.source)
            all_ok = ok and all_ok
        return all_ok

    def _unload_module(self, name, record):
        """
        Call a package's uninstall(pm) hook if present, then drop its
        module from sys.modules and remove any command(s) it left in
        the registry. Best-effort: failures here should never block
        the actual file removal in uninstall().
        """
        module_name = self._module_name_for(record)
        module = sys.modules.get(module_name) if module_name else None

        if module is not None:
            uninstaller = getattr(module, "uninstall", None)
            if uninstaller is not None:
                try:
                    uninstaller(self)
                except Exception as e:
                    self.logger.warning(
                        "uninstall(pm) failed for package '{}': {}".format(name, e), source=self.source
                    )

        # belt-and-suspenders: even if the package didn't call
        # self.unregister() itself, drop any command(s) that were
        # pointing at this module so nothing dangling is left behind
        stale = [cmd for cmd, mod in self._command_modules.items() if mod == module_name]
        for cmd in stale:
            self.unregister(cmd)

        if module_name and module_name in sys.modules:
            del sys.modules[module_name]

    def uninstall(self, name):
        if not self._require_root("uninstall"):
            return False

        self._begin_op()
        try:
            print("[PKG] === Uninstalling '{}' ===".format(name))
            installed = self._load_pkglist()
            if name not in installed:
                self._error("Package '{}' is not installed.".format(name))
                return False

            record = installed[name]

            # core OS components (ZenCMD, FileManager, PackageManager, Network,
            # BootConfig, etc.) must never be removed through the normal
            # uninstall path -- only the future Recovery module should touch them
            if record.get("core", False):
                self._error("Package '{}' is a core OS component and cannot be uninstalled.".format(name))
                return False

            install_dir = record.get("install_path")

            # give the package a chance to clean up after itself and
            # pull its command out of the registry before its files
            # are deleted
            self._unload_module(name, record)

            # refuse to recursively delete anything that isn't a real,
            # specific package subdirectory -- protects against wiping
            # out '/' (or the whole tree) when install_path is missing,
            # empty, or points at the filesystem root
            if not self._is_safe_install_dir(install_dir):
                self._error("Refusing to uninstall '{}': install_path '{}' is missing or unsafe to remove.".format(name, install_dir))
                return False

            if not self._remove_dir_recursive(install_dir):
                self.logger.warning("Could not fully remove folder '{}' for package '{}'".format(install_dir, name), source=self.source)

            del installed[name]
            if not self._save_pkglist(installed):
                return False

            self.fm.refresh_tree(self._parent_dir(install_dir))
            print("[PKG] '{}' uninstalled.".format(name))
            return True
        finally:
            self._end_op()

    def reinstall(self, name):
        if not self._require_root("reinstall"):
            return False
        installed = self._load_pkglist()
        if name not in installed:
            self._error("Package '{}' is not installed, cannot reinstall.".format(name))
            return False

        # wrap the whole uninstall+install sequence as a single operation so
        # they share one pkgtable.json download/cache and only clean it up once
        self._begin_op()
        try:
            return self.uninstall(name) and self.install(name, force=True)
        finally:
            self._end_op()

    def update(self, name=None):
        if not self._require_root("update"):
            return False

        self._begin_op()
        try:
            installed = self._load_pkglist()
            if name is None:
                if not installed:
                    print("[PKG] No packages installed.")
                    return True
                ok = True
                for pkg_name in list(installed.keys()):
                    ok = self._update_one(pkg_name, installed) and ok
                return ok
            return self._update_one(name, installed)
        finally:
            self._end_op()

    def run(self, command, *args):
        """
        Execute a registered command's callback.

        This is the normal, RAM-cheap path used by ZenCMD: a package's
        install() already put a function reference into self.commands,
        so running it is just a dict lookup + call -- no re-exec, no
        re-reading the file from flash.
        """
        callback = self.commands.get(command)
        if callback is None:
            # fall back to the legacy exec-based path for any package
            # installed under the old architecture (no install()/run()
            # module functions, no registry entry) -- keeps old
            # pkglist.json entries working without forcing a reinstall
            return self._run_legacy(command, *args)

        try:
            callback(list(args), self)
        except Exception as e:
            self._error("Command '{}' raised an error while running: {}".format(command, e))
            return False
        return True

    def _run_legacy(self, name, *args):
        installed = self._load_pkglist()
        if name not in installed:
            self._error("Package '{}' is not installed.".format(name))
            return False

        record = installed[name]
        full_path = self._full_path(record.get("install_path"), record.get("filename"))
        try:
            with open(full_path) as f:
                code = f.read()
        except OSError as e:
            self._error("Cannot read '{}' for package '{}': {}".format(full_path, name, e))
            return False

        try:
            exec(compile(code, full_path, "exec"), {"__name__": "__main__", "argv": list(args)})
        except Exception as e:
            self._error("Package '{}' raised an error while running: {}".format(name, e))
            return False
        return True

    # -----------------------------------------------------------------
    # Command registry helpers
    # -----------------------------------------------------------------

    def register(self, command, callback, module_name=None):
        """
        Register a command -> callback mapping. Called by a package's
        own install(pm) function, e.g.:

            def install(pm):
                pm.register(COMMAND, run)

        Overwrites any existing registration for the same command name
        (this is what makes reload()/update() work without a reboot).
        Returns True on success.
        """
        if not command or not callable(callback):
            self._error("register() requires a non-empty command name and a callable")
            return False
        self.commands[command] = callback
        module_name = module_name or self._installing_module
        if module_name:
            self._command_modules[command] = module_name
        self.logger.debug("Registered command '{}'".format(command), source=self.source)
        return True

    def unregister(self, command):
        """Remove a command from the registry. Returns True if it existed."""
        existed = command in self.commands
        self.commands.pop(command, None)
        self._command_modules.pop(command, None)
        if existed:
            self.logger.debug("Unregistered command '{}'".format(command), source=self.source)
        return existed

    def check(self, command):
        """
        Return True if `command` is currently registered and runnable.

        Note: this now checks the live command registry rather than
        pkglist.json, since a package can register a command without
        necessarily matching its own package name (or vice versa).
        """
        return command in self.commands

    def list_commands(self):
        """Return a list of every currently registered command name."""
        return list(self.commands.keys())

    def reload(self, name):
        """
        Hot-reload an already-installed package's module: drop it from
        sys.modules, re-import it, and re-run install(pm). Since
        register() overwrites any existing entry for the same command,
        this swaps the callback in place -- no reboot needed.

        Use after a package's files have been updated on disk outside
        of update()/install() (e.g. manually edited), or simply to
        force a fresh import.
        """
        installed = self._load_pkglist()
        record = installed.get(name)
        if record is None:
            self._error("Package '{}' is not installed.".format(name))
            return False
        return self._load_and_install_module(name, record)

    def info(self, name):
        installed = self._load_pkglist()
        record = installed.get(name)

        print("\n[Package] {}".format(name))
        if record:
            print("  Installed     : yes")
            print("  Version       : {}".format(record.get("version")))
            print("  Author        : {}".format(record.get("author")))
            print("  Repository    : {}".format(record.get("repository")))
            print("  Branch        : {}".format(record.get("branch")))
            print("  Install path  : {}".format(record.get("install_path")))
            print("  Filename      : {}".format(record.get("filename")))
            print("  Dependencies  : {}".format(record.get("dependencies")))
            print("  Core          : {}".format(record.get("core", False)))
        else:
            print("  Installed     : no")

        pkgtable = self._fetch_pkgtable()
        try:
            entry = self._find_package(pkgtable, name) if pkgtable else None
            if entry:
                print("  Catalog version : {}".format(entry.get("version")))
                print("  Catalog author  : {}".format(entry.get("author")))
                print("  Catalog repo    : {}".format(entry.get("repository")))
                print("  Modules needed  : {}".format(entry.get("modules")))
                print("  Catalog core    : {}".format(entry.get("core", False)))
            elif not record:
                print("  Not found in catalog either.")
            return record or entry
        finally:
            self._cleanup_pkgtable_cache()

    def list(self):
        installed = self._load_pkglist()
        if not installed:
            print("[PKG] No packages installed.")
            return []
        print("\n[Installed Packages]")
        for pkg_name, record in installed.items():
            print("  {:<20} v{:<10} ({})".format(pkg_name, record.get("version", "?"), record.get("author", "?")))
        return list(installed.keys())

    def verify(self):
        installed = self._load_pkglist()
        issues = {}
        pkgtable = self._fetch_pkgtable()
        try:
            for pkg_name, record in installed.items():
                pkg_issues = []
                full_path = self._full_path(record.get("install_path"), record.get("filename"))
                try:
                    os.stat(full_path)
                except OSError:
                    pkg_issues.append("missing file: {}".format(full_path))

                entry = self._find_package(pkgtable, pkg_name) if pkgtable else None
                missing_modules = self._check_modules(entry.get("modules", []) if entry else [])
                if missing_modules:
                    pkg_issues.append("missing modules: {}".format(", ".join(missing_modules)))

                if pkg_issues:
                    issues[pkg_name] = pkg_issues

            if issues:
                for pkg_name, pkg_issues in issues.items():
                    print("  {}:".format(pkg_name))
                    for issue in pkg_issues:
                        print("    - {}".format(issue))
            return issues
        finally:
            self._cleanup_pkgtable_cache()
    def _begin_op(self):
        self._op_depth += 1

    def _end_op(self):
        self._op_depth -= 1
        if self._op_depth <= 0:
            self._op_depth = 0
            self._cleanup_pkgtable_cache()

    def _cleanup_pkgtable_cache(self):
        self._pkgtable_cache = None
        try:
            os.remove("/pkgtable.json")
        except OSError:
            pass

    def _current_user(self):
        try:
            return self.um.current_user()
        except Exception as e:
            self.logger.debug("Could not resolve current user: {}".format(e), source=self.source)
            return None

    def _is_root(self):
        user = self._current_user()
        if not user:
            return False
        try:
            return bool(self.um.isrooted(user))
        except Exception as e:
            self.logger.debug("Could not check root status: {}".format(e), source=self.source)
            return False

    def _require_root(self, action):
        if self._is_root():
            return True
        self._error("Permission denied: '{}' requires root privileges. Use usermanager().elevate(user, password) first.".format(action))
        return False

    def _fetch_pkgtable(self):
        # reuse the in-memory copy for the duration of one outermost
        # operation (install w/ nested deps, reinstall, update, ...)
        # instead of re-downloading pkgtable.json for every nested call
        if self._pkgtable_cache is not None:
            return self._pkgtable_cache

        ok = self.git.download(self.repo_user, self.repo_name, "pkgtable.json", save_dir="/")
        if not ok:
            self._error("Could not download pkgtable.json from {}/{}".format(self.repo_user, self.repo_name))
            return None
        data = self._load_json("/pkgtable.json")
        if data is None:
            self._error("Downloaded pkgtable.json but it could not be parsed as JSON")
            return None
        self._pkgtable_cache = data
        return data

    def _find_package(self, pkgtable, name):
        return pkgtable.get(name) if pkgtable else None

    def _find_package_providing_module(self, pkgtable, module_name):
        if not pkgtable:
            return None, None
        if module_name in pkgtable:
            return module_name, pkgtable[module_name]
        for pkg_name, entry in pkgtable.items():
            if module_name in entry.get("provides", []):
                return pkg_name, entry
        return None, None

    def _load_pkglist(self):
        data = self._load_json(self.PKGLIST_PATH)
        return data if isinstance(data, dict) else {}

    def _save_pkglist(self, data):
        return self._save_json(self.PKGLIST_PATH, data)

    def _load_json(self, path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            self.logger.debug("Could not load JSON from '{}': {}".format(path, e), source=self.source)
            return None

    def _save_json(self, path, data):
        try:
            with open(path, "w") as f:
                json.dump(data, f)
            return True
        except OSError as e:
            self._error("Could not write '{}': {}".format(path, e))
            return False

    def _make_pkglist_entry(self, entry):
        return {
            "name": entry.get("name"), "version": entry.get("version"), "author": entry.get("author"),
            "repository": entry.get("repository"), "branch": entry.get("branch") or self.git.default_branch,
            "install_path": entry.get("install_path"), "filename": entry.get("file", "").split("/")[-1],
            "dependencies": entry.get("dependencies", []),
            # essential-OS-component flag, kept only for the future Recovery
            # module; defaults to False for backward compatibility with
            # older pkgtable.json files that don't have it yet
            "core": bool(entry.get("core", False)),
        }

    def _download_package(self, entry):
        # each catalog entry carries its own author/repository/branch --
        # independent of the repo used for pkgtable.json itself
        return self.git.download(entry.get("author"), entry.get("repository"), entry.get("file"),
                                  branch=entry.get("branch"), save_dir=entry.get("install_path"))

    def _ensure_dependency(self, dep, pkgtable, installed):
        dep_name = dep.get("name")
        dep_version = dep.get("version", "0")
        if dep_name in installed:
            current_version = installed[dep_name].get("version", "0")
            if not self._version_gt(dep_version, current_version):
                return True
            return self.install(dep_name, force=True)
        return self.install(dep_name)

    def _resolve_missing_modules(self, missing_modules, pkgtable, installed):
        all_ok = True
        for mod_name in missing_modules:
            pkg_name, entry = self._find_package_providing_module(pkgtable, mod_name)
            if not pkg_name:
                all_ok = False
                continue
            if pkg_name in installed:
                all_ok = False
                continue
            if not self.install(pkg_name):
                all_ok = False
        return all_ok

    def _check_modules(self, modules):
        missing = []
        for mod_name in modules or []:
            try:
                __import__(mod_name)
            except ImportError:
                missing.append(mod_name)
        return missing

    def _update_one(self, name, installed):
        if name not in installed:
            self._error("Package '{}' is not installed.".format(name))
            return False

        pkgtable = self._fetch_pkgtable()
        if pkgtable is None:
            return False

        entry = self._find_package(pkgtable, name)
        if entry is None:
            self._error("Package '{}' no longer exists in the catalog.".format(name))
            return False

        record = installed[name]
        if entry.get("author") != record.get("author") or entry.get("repository") != record.get("repository"):
            return False
        if not self._version_gt(entry.get("version", "0"), record.get("version", "0")):
            return True
        return self.install(name, force=True)

    def _full_path(self, install_path, filename):
        return "{}/{}".format((install_path or "/").rstrip("/"), filename)

    def _parent_dir(self, path):
        path = (path or "/").rstrip("/")
        if not path:
            return "/"
        idx = path.rfind("/")
        return path[:idx] if idx > 0 else "/"

    def _is_safe_install_dir(self, path):
        # a package's install_path must be a real, specific subdirectory --
        # never missing/empty, and never the filesystem root itself, since
        # that gets handed straight to a recursive delete
        if not path:
            return False
        normalized = path.rstrip("/")
        if normalized in ("", "/", ".", ".."):
            return False
        return True

    def _remove_dir_recursive(self, path):
        if not path:
            return False

        # defense in depth: even if a caller forgets to pre-check with
        # _is_safe_install_dir, never let this recurse over '/' or an
        # otherwise unsafe path
        normalized = path.rstrip("/")
        if normalized in ("", "/", ".", ".."):
            self.logger.warning("Refusing to recursively remove unsafe path '{}'".format(path), source=self.source)
            return False

        try:
            entries = os.listdir(path)
        except OSError:
            return True

        ok = True
        for entry in entries:
            full = path.rstrip("/") + "/" + entry
            try:
                is_dir = bool(os.stat(full)[0] & 0x4000)
            except OSError:
                continue
            if is_dir:
                if not self._remove_dir_recursive(full):
                    ok = False
            else:
                try:
                    os.remove(full)
                except OSError as e:
                    self.logger.warning("Could not remove file '{}': {}".format(full, e), source=self.source)
                    ok = False

        try:
            os.rmdir(path)
        except OSError as e:
            self.logger.warning("Could not remove directory '{}': {}".format(path, e), source=self.source)
            ok = False
        return ok

    def _version_tuple(self, version):
        parts = []
        for p in str(version).split("."):
            try:
                parts.append(int(p))
            except ValueError:
                parts.append(0)
        return tuple(parts)

    def _version_gt(self, a, b):
        ta, tb = self._version_tuple(a), self._version_tuple(b)
        length = max(len(ta), len(tb))
        ta += (0,) * (length - len(ta))
        tb += (0,) * (length - len(tb))
        return ta > tb

    def _error(self, message):
        self.logger.error(message, source=self.source)
        print("[PKG] Error:", message)


def help():
    """Return a description of what's available in this module."""
    return (
        "packages.py -- package manager + net file transfer helpers\n"
        "  PackageManager(git=None, repo_user=None, repo_name=None)\n"
        "    .install(name, force=False) / .uninstall(name) / .reinstall(name)\n"
        "    .update(name=None) / .reload(name) / .list() / .info(name) / .verify()\n"
        "    .run(command, *args) / .register(command, callback, module_name=None)\n"
        "    .unregister(command) / .check(command) / .list_commands()\n"
        "    .load_all_installed()  -- auto-runs at end of __init__\n"
        "  Git(base_raw=None, default_branch='main')\n"
        "    .download(user, repo, filename, branch=None, save_dir=None)\n"
        "    .download_url(url, save_dir=None) / .upload(user, repo, local_path, ...)\n"
        "  downloadhelper().download_file(url, save_dir='/', save_file=None)\n"
        "  AppInstaller().install(app_name) / .uninstall(name) / .listapps()\n"
        "  Wiki(lang='en').fetch(title) / .next() / .search(query, n=5)\n"
        "  AppDB().set(app, key, value) / .get(app, key, default=None) / .delete/.clear/.dump"
    )
