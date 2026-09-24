"""Windows ACL helpers for private plugin state (stdlib only, no new dependency).

Hermes's own Windows permission fix grants the current user full control and
removes other grants with ``icacls /grant:r`` (see ``hermes_cli/plugins_cmd.py``
in the pinned Hermes tree). This module applies the same contract to a single
published file without spawning a subprocess: a protected DACL granting full
control to the current user, SYSTEM, and Administrators, and nothing to anyone
else. ``plugin_data_dir`` itself performs no ACL hardening, and the Hermes home
is operator-configurable (the hosted compatibility matrix even uses a runner
temp directory), so inherited ACLs are not a reliable privacy contract.

Imports are deferred and guarded by ``os.name == "nt"`` so the module loads on
every platform; callers on other platforms must not use these functions.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

__all__ = ["current_user_sid", "effective_rights", "read_dacl", "set_private_dacl"]

SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x00000001
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
ACL_REVISION = 2
ACCESS_ALLOWED_ACE_TYPE = 0x0
OBJECT_INHERIT_ACE = 0x01
CONTAINER_INHERIT_ACE = 0x02
GENERIC_ALL = 0x10000000
TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1
TRUSTEE_IS_SID = 0
TRUSTEE_IS_UNKNOWN = 0
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"

if os.name == "nt":
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    advapi32 = None
    kernel32 = None


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", SID_AND_ATTRIBUTES)]


class ACL(ctypes.Structure):
    _fields_ = [
        ("AclRevision", wintypes.BYTE),
        ("Sbz1", wintypes.BYTE),
        ("AclSize", wintypes.WORD),
        ("AceCount", wintypes.WORD),
        ("Sbz2", wintypes.WORD),
    ]


class ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", wintypes.BYTE),
        ("AceFlags", wintypes.BYTE),
        ("AceSize", wintypes.WORD),
    ]


class ACCESS_ALLOWED_ACE(ctypes.Structure):
    _fields_ = [
        ("Header", ACE_HEADER),
        ("Mask", wintypes.DWORD),
        ("SidStart", wintypes.DWORD),
    ]


class TRUSTEE_W(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", wintypes.DWORD),
        ("TrusteeForm", wintypes.DWORD),
        ("TrusteeType", wintypes.DWORD),
        ("ptstrName", wintypes.LPWSTR),
    ]


def _prototypes() -> None:
    assert advapi32 is not None and kernel32 is not None
    advapi32.InitializeAcl.restype = wintypes.BOOL
    advapi32.InitializeAcl.argtypes = [ctypes.POINTER(ACL), wintypes.DWORD, wintypes.DWORD]
    advapi32.AddAccessAllowedAceEx.restype = wintypes.BOOL
    advapi32.AddAccessAllowedAceEx.argtypes = [
        ctypes.POINTER(ACL), wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.POINTER(ACL)), ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorOwner.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorOwner.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [ctypes.POINTER(ACL), wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetEffectiveRightsFromAclW.restype = wintypes.DWORD
    advapi32.GetEffectiveRightsFromAclW.argtypes = [
        ctypes.POINTER(ACL), ctypes.POINTER(TRUSTEE_W), ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]


def _require_nt() -> None:
    if os.name != "nt":
        raise RuntimeError("Windows ACL helpers are only available on Windows")
    _prototypes()


def _check(result: bool, name: str) -> None:
    if not result:
        raise ctypes.WinError(ctypes.get_last_error(), name)


def _sid_from_string(text: str) -> ctypes.c_void_p:
    sid = ctypes.c_void_p()
    _check(advapi32.ConvertStringSidToSidW(text, ctypes.byref(sid)), "ConvertStringSidToSidW")
    return sid


def _sid_to_string(sid: ctypes.c_void_p) -> str:
    out = wintypes.LPWSTR()
    _check(advapi32.ConvertSidToStringSidW(sid, ctypes.byref(out)), "ConvertSidToStringSidW")
    try:
        return out.value
    finally:
        kernel32.LocalFree(out)


def current_user_sid() -> str:
    """Return the current process user's SID string."""
    _require_nt()
    process = kernel32.GetCurrentProcess()
    token = wintypes.HANDLE()
    _check(advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)), "OpenProcessToken")
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        _check(
            advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, buffer, needed.value, ctypes.byref(needed)),
            "GetTokenInformation",
        )
        token_user = ctypes.cast(buffer, ctypes.POINTER(TOKEN_USER)).contents
        return _sid_to_string(token_user.User.Sid)
    finally:
        kernel32.CloseHandle(token)


def set_private_dacl(path: Path, *, inherit_to_children: bool = False) -> None:
    """Replace *path*'s DACL with a protected DACL granting full control to
    the current user, SYSTEM, and Administrators only. When protecting a
    directory, optionally pass those grants to newly created children.
    Raises ``OSError`` on failure so callers can fail closed."""
    _require_nt()
    sids = [
        _sid_from_string(SYSTEM_SID),
        _sid_from_string(ADMINISTRATORS_SID),
        _sid_from_string(current_user_sid()),
    ]
    try:
        ace_body = ctypes.sizeof(ACCESS_ALLOWED_ACE) - ctypes.sizeof(wintypes.DWORD)
        acl_size = ctypes.sizeof(ACL) + sum(ace_body + advapi32.GetLengthSid(sid) for sid in sids)
        buffer = ctypes.create_string_buffer(acl_size)
        acl = ctypes.cast(buffer, ctypes.POINTER(ACL))
        _check(advapi32.InitializeAcl(acl, acl_size, ACL_REVISION), "InitializeAcl")
        flags = (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE) if inherit_to_children else 0
        for sid in sids:
            _check(
                advapi32.AddAccessAllowedAceEx(acl, ACL_REVISION, flags, GENERIC_ALL, sid),
                "AddAccessAllowedAceEx",
            )
        result = advapi32.SetNamedSecurityInfoW(
            str(path), SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None,
        )
        if result != 0:
            raise ctypes.WinError(result, "SetNamedSecurityInfoW")
    finally:
        for sid in sids:
            kernel32.LocalFree(sid)


def _security_descriptor(path: Path) -> ctypes.c_void_p:
    # Request the owner alongside the DACL: GetSecurityDescriptorOwner reads
    # only the parts the descriptor was fetched with.
    sd = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path), SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
        None, None, None, None, ctypes.byref(sd),
    )
    if result != 0:
        raise ctypes.WinError(result, "GetNamedSecurityInfoW")
    return sd


def read_dacl(path: Path) -> dict:
    """Return the file's DACL shape: owner SID, present/defaulted flags, and
    a list of ``(sid, access_mask, ace_type)`` tuples."""
    _require_nt()
    sd = _security_descriptor(path)
    try:
        owner = ctypes.c_void_p()
        owner_defaulted = wintypes.BOOL()
        _check(
            advapi32.GetSecurityDescriptorOwner(sd, ctypes.byref(owner), ctypes.byref(owner_defaulted)),
            "GetSecurityDescriptorOwner",
        )
        owner_sid = _sid_to_string(owner) if owner else None
        dacl = ctypes.POINTER(ACL)()
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        _check(
            advapi32.GetSecurityDescriptorDacl(
                sd, ctypes.byref(dacl_present), ctypes.byref(dacl), ctypes.byref(dacl_defaulted),
            ),
            "GetSecurityDescriptorDacl",
        )
        aces = []
        if dacl_present and dacl:
            for index in range(dacl.contents.AceCount):
                ace_ptr = ctypes.c_void_p()
                _check(advapi32.GetAce(dacl, index, ctypes.byref(ace_ptr)), "GetAce")
                ace = ctypes.cast(ace_ptr, ctypes.POINTER(ACCESS_ALLOWED_ACE)).contents
                sid = ctypes.c_void_p(ctypes.addressof(ace) + ACCESS_ALLOWED_ACE.SidStart.offset)
                aces.append((_sid_to_string(sid), ace.Mask, ace.Header.AceType))
        return {
            "owner": owner_sid,
            "dacl_present": bool(dacl_present.value),
            "dacl_defaulted": bool(dacl_defaulted.value),
            "aces": aces,
        }
    finally:
        kernel32.LocalFree(sd)


def effective_rights(path: Path, sid_string: str) -> int:
    """Return the effective access mask the DACL grants *sid_string*."""
    _require_nt()
    sd = _security_descriptor(path)
    try:
        dacl = ctypes.POINTER(ACL)()
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        _check(
            advapi32.GetSecurityDescriptorDacl(
                sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted),
            ),
            "GetSecurityDescriptorDacl",
        )
        if not present.value or not dacl:
            return 0
        sid = _sid_from_string(sid_string)
        try:
            trustee = TRUSTEE_W(None, 0, TRUSTEE_IS_SID, TRUSTEE_IS_UNKNOWN, ctypes.cast(sid, wintypes.LPWSTR))
            access = wintypes.DWORD()
            result = advapi32.GetEffectiveRightsFromAclW(dacl, ctypes.byref(trustee), ctypes.byref(access))
            if result != 0:
                raise ctypes.WinError(result, "GetEffectiveRightsFromAclW")
            return access.value
        finally:
            kernel32.LocalFree(sid)
    finally:
        kernel32.LocalFree(sd)
