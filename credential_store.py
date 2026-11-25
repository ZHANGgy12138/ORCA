"""Explicit opt-in API-key storage in the current user's Windows credential set.

No credential enumeration, plaintext configuration, log output, or file fallback
is used. Provider names are whitelisted and stored keys are bound to the current
effective service endpoints before being returned to the local settings view.
"""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import re
import threading
from urllib.parse import urlsplit, urlunsplit


MAX_BLOB_BYTES = 2560
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class _VaultError(Exception):
    pass


class _WindowsCredentials:
    def __init__(self):
        if os.name != "nt":
            raise _VaultError("此功能需要 Windows 凭据管理器，当前环境只能在本次会话使用密钥。")
        try:
            self.api = ctypes.WinDLL("advapi32", use_last_error=True)
            self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_Credential))]
            self.api.CredReadW.restype = wintypes.BOOL
            self.api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
            self.api.CredWriteW.restype = wintypes.BOOL
            self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
            self.api.CredDeleteW.restype = wintypes.BOOL
            self.api.CredFree.argtypes = [ctypes.c_void_p]
            self.api.CredFree.restype = None
        except Exception:
            raise _VaultError("Windows 凭据管理器暂时不可用，密钥只能在本次会话使用。") from None

    def read(self, target):
        pointer = ctypes.POINTER(_Credential)()
        if not self.api.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == ERROR_NOT_FOUND:
                return None
            raise _VaultError("Windows 无法读取已保存的密钥，请检查当前账户的凭据管理器。")
        try:
            record = pointer.contents
            if record.Type != CRED_TYPE_GENERIC or record.CredentialBlobSize > MAX_BLOB_BYTES:
                raise _VaultError("已保存的密钥格式无法读取，请忘记后重新保存。")
            return ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize)
        finally:
            self.api.CredFree(pointer)

    def write(self, target, blob):
        if len(blob) > MAX_BLOB_BYTES:
            raise _VaultError("密钥内容超过 Windows 凭据管理器的容量限制，本次未保存。")
        buffer = ctypes.create_string_buffer(blob)
        record = _Credential()
        record.Type = CRED_TYPE_GENERIC
        record.TargetName = target
        record.Comment = "ORCA translation service credentials"
        record.CredentialBlobSize = len(blob)
        record.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE))
        record.Persist = CRED_PERSIST_LOCAL_MACHINE
        record.UserName = "ORCA"
        if not self.api.CredWriteW(ctypes.byref(record), 0):
            raise _VaultError("Windows 未能保存密钥，请重试；当前密钥仍可在本次会话使用。")

    def delete(self, target):
        if not self.api.CredDeleteW(target, CRED_TYPE_GENERIC, 0) and ctypes.get_last_error() != ERROR_NOT_FOUND:
            raise _VaultError("Windows 未能删除已保存的密钥，请重试。")


def _provider(engine):
    from engine_registry import engine_id, is_secret, metadata
    if not isinstance(engine, str) or not engine.strip():
        raise _VaultError("请选择有效的翻译服务。")
    try:
        canonical = engine_id(engine)
        fields = metadata()[canonical].setting_model_type.model_fields
    except (KeyError, TypeError, ValueError):
        raise _VaultError("请选择有效的翻译服务。") from None
    secret_names = {name for name in fields if is_secret(name)}
    return canonical, fields, secret_names


def _binding(engine, fields, values):
    endpoints = {}
    for name, field in fields.items():
        if not name.endswith(("_url", "_host", "_endpoint")):
            continue
        default = field.default if isinstance(field.default, str) else None
        raw = values.get(name)
        if raw is not None and not isinstance(raw, str):
            raise _VaultError("接口地址格式无效，请检查引擎设置。")
        value = raw.strip() if isinstance(raw, str) and raw.strip() else default
        if name == "openai_base_url" and not value:
            value = os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        if value:
            try:
                parts = urlsplit(value.strip())
                # Keep the full endpoint, including paths and query/user info,
                # in the hash so a changed destination never receives old keys.
                path = parts.path.rstrip("/")
                if name.endswith("_base_url"):
                    path = re.sub(r"/chat/completions$", "", path)
                userinfo, separator, authority = parts.netloc.rpartition("@")
                netloc = userinfo + separator + authority.lower() if separator else parts.netloc.lower()
                value = urlunsplit((parts.scheme.lower(), netloc, path, parts.query, parts.fragment))
            except ValueError:
                raise _VaultError("接口地址格式无效，请检查引擎设置。") from None
        endpoints[name] = value
    payload = json.dumps({"engine": engine, "endpoints": endpoints}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class CredentialStore:
    def __init__(self, namespace="ORCA/credentials/v1"):
        # Only Python callers may choose a namespace; the JS bridge does not
        # expose it. Tests use a random isolated ORCA/credential-test namespace.
        if not isinstance(namespace, str) or len(namespace) > 200 or not re.fullmatch(r"ORCA/[A-Za-z0-9_./-]+", namespace):
            raise ValueError("Invalid ORCA credential namespace")
        self._namespace = namespace.rstrip("/")
        self._backend = None
        self._lock = threading.RLock()

    def _storage(self):
        if self._backend is None:
            self._backend = _WindowsCredentials()
        return self._backend

    def _target(self, engine):
        return self._namespace + "/" + engine

    def load(self, engine, engine_settings):
        found = False
        try:
            canonical, fields, allowed = _provider(engine)
            if not isinstance(engine_settings, dict):
                raise _VaultError("引擎设置格式无效。")
            binding = _binding(canonical, fields, engine_settings)
            with self._lock:
                blob = self._storage().read(self._target(canonical))
            if blob is None:
                return {"ok": True, "remembered": False, "secrets": {}}
            found = True
            try:
                record = json.loads(blob.decode("utf-8"))
                valid = (isinstance(record, dict) and record.get("version") == 1 and record.get("engine") == canonical
                         and isinstance(record.get("binding"), str) and re.fullmatch(r"[0-9a-f]{64}", record["binding"])
                         and isinstance(record.get("secrets"), dict))
                if not valid:
                    raise ValueError()
                secrets = record["secrets"]
                if not secrets or any(name not in allowed or not isinstance(value, str) or not value.strip() for name, value in secrets.items()):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise _VaultError("已保存的密钥格式无法读取，请忘记后重新保存。") from None
            if record["binding"] != binding:
                return {"ok": True, "remembered": True, "secrets": {}, "endpoint_mismatch": True}
            return {"ok": True, "remembered": True, "secrets": secrets.copy()}
        except _VaultError as exc:
            return {"ok": False, "remembered": found, "secrets": {}, "error": str(exc)}
        except Exception:
            return {"ok": False, "remembered": found, "secrets": {}, "error": "无法读取已保存的密钥，请稍后重试。"}

    def save(self, engine, engine_settings):
        try:
            canonical, fields, allowed = _provider(engine)
            if not isinstance(engine_settings, dict):
                raise _VaultError("引擎设置格式无效。")
            if not allowed:
                raise _VaultError("此引擎无需保存密钥。")
            secrets = {}
            for name in allowed:
                value = engine_settings.get(name)
                if value is None or value == "":
                    continue
                if not isinstance(value, str):
                    raise _VaultError("密钥格式无效，请重新填写。")
                if value.strip():
                    secrets[name] = value.strip()
            if not secrets:
                raise _VaultError("请先填写密钥，再选择记住密钥。")
            record = {"version": 1, "engine": canonical, "binding": _binding(canonical, fields, engine_settings), "secrets": secrets}
            blob = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(blob) > MAX_BLOB_BYTES:
                raise _VaultError("密钥内容过长，超过 Windows 保存限制；请仅在本次会话使用。")
            with self._lock:
                self._storage().write(self._target(canonical), blob)
            return {"ok": True, "remembered": True}
        except _VaultError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "无法保存密钥，请稍后重试；当前密钥仍可在本次会话使用。"}

    def forget(self, engine):
        try:
            canonical, _, _ = _provider(engine)
            with self._lock:
                self._storage().delete(self._target(canonical))
            return {"ok": True, "remembered": False}
        except _VaultError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "无法删除已保存的密钥，请稍后重试。"}
