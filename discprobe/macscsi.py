"""macOS SCSI pass-through: this module drives the `discprobe-spt` helper.

macOS has no IOCTL_SCSI_PASS_THROUGH. A bare CDB can only be sent through IOKit's
SCSITaskLib, which needs a little CoreFoundation/IOKit plumbing plus an exclusive
access handshake. `tools/mac/discprobe-spt.c` does that once and then speaks a very
small protocol over pipes; everything that decides *what* to measure stays here in
Python, where the analysis is the same code the simulator is checked against.

Two properties matter for the timing work and both come from the helper:

  * The CDB goes out with no scheduling of its own: the helper creates one
    SCSITaskInterface per command and calls ExecuteTaskSync, so the command waits for
    the drive, not for a timer.
  * The timestamps are mach_absolute_time() immediately before and after that call,
    converted to nanoseconds, so they span the kernel and the USB or SATA round trip
    and nothing else. They come back over the pipe but are never re-stamped here,
    so process scheduling on either side cannot move them.

The drive is opened exclusively for the lifetime of the helper; the medium must not be
mounted (the helper unmounts it through DiskArbitration, and retries once). No root is
needed for this on the drives tested so far.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys

from .scsi import DATA_IN, DATA_NONE, DATA_OUT, ScsiResult, Sense, check_allowed

BUFFER_BYTES = 128 * 1024
HELPER_ENV = "DISCPROBE_SPT"
REQUEST = struct.Struct("<BBBBII16s")
RESPONSE = struct.Struct("<iBBBBQQII")
CMD_EXECUTE, CMD_QUIT = 0, 1
HELPER_READY_TIMEOUT_S = 10.0


def helper_path() -> str:
    """Where the compiled helper lives: $DISCPROBE_SPT, else tools/mac/build/."""
    override = os.environ.get(HELPER_ENV)
    if override:
        return override
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "tools", "mac", "build", "discprobe-spt")


def _gatekeeper_hint(path: str) -> str:
    return (f"macOS refused to run {path}. The helper in the release is signed and notarized; a copy "
            f"that came from a browser download may still carry the quarantine flag, which clears "
            f"with: xattr -d com.apple.quarantine {path}")


def _quarantined(path: str) -> bool:
    return subprocess.run(["xattr", "-p", "com.apple.quarantine", path],
                          capture_output=True).returncode == 0


def _runnable(path: str) -> str:
    """A copy of the helper that macOS will actually execute.

    A downloaded file carries com.apple.quarantine, and Gatekeeper refuses to run a
    quarantined executable unless it is a signed bundle with a stapled ticket. A bare
    command-line binary cannot carry a stapled ticket - notarizing it is accepted by
    Apple but still gets killed on first exec - so the download flag has to go. Both
    routes below are the same thing the user would do by hand, need no privileges, and
    leave the downloaded program untouched in content.
    """
    if not os.path.exists(path) or not _quarantined(path):
        return path
    cleared = subprocess.run(["xattr", "-d", "com.apple.quarantine", path], capture_output=True)
    if cleared.returncode == 0 and not _quarantined(path):
        print(f"note: cleared the download flag on {path} (macOS refuses to run a quarantined "
              f"command-line binary); no privileges were needed", file=sys.stderr)
        return path
    # Fall back to a copy for installs we cannot write to: write the bytes by hand, since a
    # system-assisted copy may carry the attribute along.
    cache = os.path.join(os.path.expanduser("~/Library/Caches"), "discprobe")
    try:
        os.makedirs(cache, exist_ok=True)
        with open(path, "rb") as fh:
            data = fh.read()
        target = os.path.join(cache, f"discprobe-spt-{hashlib.sha256(data).hexdigest()[:12]}")
        if not os.path.exists(target):
            with open(target, "wb") as fh:
                fh.write(data)
            os.chmod(target, 0o755)
            print(f"note: {path} carries the download flag; using the identical copy at {target}",
                  file=sys.stderr)
        return target
    except OSError as e:
        raise OSError(2, f"{_gatekeeper_hint(path)} (and copying it somewhere runnable failed: {e})") from e


def _ensure_helper() -> str:
    path = helper_path()
    if os.path.exists(path):
        return path
    if os.environ.get(HELPER_ENV):
        raise OSError(2, f"{HELPER_ENV} points at {path}, which does not exist")
    script = os.path.normpath(os.path.join(os.path.dirname(path), "..", "build.sh"))
    if not os.path.exists(script):
        raise OSError(2, f"the macOS helper is missing: expected {path}")
    if shutil.which("clang") is None:
        raise OSError(2, "the macOS helper is not built and there is no C compiler here. Either install "
                         "the Xcode command line tools (`xcode-select --install`), or download "
                         "discprobe-spt-darwin-universal from the release page and put it at "
                         f"{path} (or point {HELPER_ENV} at it)")
    proc = subprocess.run(["/bin/sh", script], capture_output=True, text=True)
    if proc.returncode == 0 and os.path.exists(path):
        return path
    raise OSError(2, "could not build the macOS helper "
                     f"({script}): {proc.stderr.strip() or proc.stdout.strip()}")


def normalise_spec(spec: str | None) -> str:
    """'disk10', '10' and '/dev/rdisk10' all name the same drive here."""
    if spec is None:
        return ""
    s = spec.strip()
    if s in ("", "auto"):
        return ""
    if s.startswith("/dev/"):
        s = s[len("/dev/"):]
    if s.startswith("r"):
        s = s[1:]
    if s.isdigit():
        s = f"disk{s}"
    return s


def list_optical_drives() -> list[dict]:
    """Every optical drive the helper can see, with vendor, model and BSD name."""
    helper = _runnable(_ensure_helper())
    proc = subprocess.run([helper, "devices"], capture_output=True, text=True)
    if proc.returncode == -9:
        raise OSError(126, _gatekeeper_hint(helper))
    if proc.returncode != 0:
        raise OSError(proc.returncode, proc.stderr.strip() or "discprobe-spt devices failed")
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as e:
        raise OSError(5, f"could not parse the helper's device list: {e}") from e


def open_device(drive: str | None = None) -> "MacScsiDevice":
    return MacScsiDevice(drive)


def raise_priority() -> bool:
    """Nothing to raise: the timestamps bracket a blocking kernel call, so process
    priority and scheduling delay cannot move them."""
    return True


class MacScsiDevice:
    """A bare-CDB channel to one optical drive, held exclusively by the helper."""

    def __init__(self, drive: str | None = None):
        self.spec = normalise_spec(drive)
        self.name = ""
        self.info: dict = {}
        self._proc: subprocess.Popen | None = None
        self._open()

    # ---------------------------------------------------------------- lifecycle

    def _open(self) -> None:
        drives = list_optical_drives()
        if not drives:
            raise OSError(2, "no optical drive found in the I/O Registry")
        if self.spec:
            match = [d for d in drives if d["name"] == self.spec]
            if not match:
                found = ", ".join(d["name"] for d in drives)
                raise OSError(2, f"no optical drive named {self.spec} (found: {found})")
            chosen = match[0]
        else:
            chosen = drives[0]
        self.info = chosen
        self.name = chosen["name"]
        args = [_runnable(_ensure_helper()), "serve", "--device", self.name]
        self._proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, bufsize=0)
        self._in = self._proc.stdin.fileno()
        self._out = self._proc.stdout.fileno()
        self._out_buf = bytearray()

    def _read_exact(self, n: int) -> bytes:
        while len(self._out_buf) < n:
            chunk = os.read(self._out, 65536)
            if not chunk:
                self._fail("the helper closed the pipe")
            self._out_buf += chunk
        data = bytes(self._out_buf[:n])
        del self._out_buf[:n]
        return data

    def _fail(self, why: str) -> None:
        detail = ""
        proc = self._proc
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.SubprocessError:
                pass
            try:
                detail = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
            except (OSError, ValueError):
                detail = ""
            if not detail and proc.returncode == -9:
                detail = _gatekeeper_hint(helper_path())
            self._proc = None
        raise OSError(5, f"{why} for {self.name}" + (f": {detail}" if detail else ""))

    # ---------------------------------------------------------------- commands

    def execute(self, cdb: bytes, data_len: int = 0, direction: int = DATA_IN,
                timeout_s: int = 30) -> ScsiResult:
        name = check_allowed(cdb)
        if direction == DATA_OUT:
            raise PermissionError("data-out commands are not used by this probe")
        if data_len > BUFFER_BYTES:
            raise ValueError(f"transfer of {data_len} bytes exceeds the {BUFFER_BYTES}-byte buffer")
        if self._proc is None:
            raise OSError(9, "device is closed")
        want_data = DATA_IN if data_len else DATA_NONE
        req = REQUEST.pack(CMD_EXECUTE, len(cdb), {DATA_NONE: 0, DATA_IN: 2}[want_data], 0,
                           data_len, max(1, int(timeout_s)) * 1000, bytes(cdb).ljust(16, b"\0"))
        try:
            os.write(self._in, req)
        except OSError:
            self._fail("could not send the CDB to the helper")
        header = self._read_exact(RESPONSE.size)
        (io_return, status, key, asc, ascq, t0, t1, transferred,
         _reserved) = RESPONSE.unpack(header)
        data = self._read_exact(min(transferred, data_len)) if transferred else b""
        if io_return != 0 or status == 0xFF:
            raise OSError(abs(io_return) or 5,
                          f"{name} failed in the helper: IOReturn 0x{io_return & 0xFFFFFFFF:08X}")
        sense = Sense(key, asc, ascq) if status else None
        return ScsiResult(status, data, sense, t0, t1)

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            os.write(self._in, REQUEST.pack(CMD_QUIT, 0, 0, 0, 0, 0, bytes(16)))
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.SubprocessError:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.SubprocessError:
                pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError, AttributeError):
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
