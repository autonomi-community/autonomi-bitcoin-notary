#!/usr/bin/env python3
"""Proof-of-existence on Autonomi + Bitcoin (via OpenTimestamps).

Workflow (each stage is persisted to a state file, so it can be resumed):
  1. upload   - upload FILE publicly to Autonomi (`ant file upload --public
                --json`) and record its public data address.
  2. stamp    - write that address to `<file_name>_address` and timestamp it
                with the OpenTimestamps calendars, producing
                `<file_name>_address.ots` (pending).
  3. confirm  - periodically upgrade the proof from the calendars until it
                carries a Bitcoin block attestation, prune everything else
                (no PendingAttestation left), and check the attested merkle
                root against the real block header (public Esplora API).
  4. publish  - upload the finalized .ots proof to Autonomi and record its
                address.

OpenTimestamps is used through its Python library (no `ots` CLI needed).
State lives in `.<file_name>.state.json` next to the outputs. Re-running the
same command auto-resumes; `--resume STATE_FILE` resumes from any directory.

This module is both a CLI and the engine imported by gui.py.

Requirements:
  - `ant` CLI (PATH, next to this program, or --ant) and SECRET_KEY in the
    environment (EVM wallet key; only needed for the upload stages).
  - pip install opentimestamps

Exit codes: 0 done, 1 error, 2 still pending (--check-only / --max-wait-hours).
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from opentimestamps.calendar import (DEFAULT_AGGREGATORS, DEFAULT_CALENDAR_WHITELIST,
                                     CommitmentNotFoundError, RemoteCalendar)
from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
from opentimestamps.core.op import OpAppend, OpSHA256
from opentimestamps.core.serialize import (StreamDeserializationContext,
                                           StreamSerializationContext)
from opentimestamps.core.timestamp import DetachedTimestampFile

HEX64_RE = re.compile(r"\b(?:ant://)?([0-9a-fA-F]{64})\b")
ADDRESS_KEYS = ("address", "data_address", "data_map_address", "public_address")
ESPLORA_APIS = ("https://mempool.space/api", "https://blockstream.info/api")
STAGES = ("new", "uploaded", "stamped", "confirmed", "done")
STAGE_TO_STEP = {"new": 0, "uploaded": 1, "stamped": 2, "confirmed": 3, "done": 4}
EXIT_PENDING = 2
MIN_CALENDARS = 2          # same default as `ots stamp -m`
CALENDAR_TIMEOUT = 15      # seconds per calendar request
USER_AGENT = "autonomi_timestamp"


class StepError(RuntimeError):
    pass


class Pending(Exception):
    """Proof is not anchored in Bitcoin yet (or could not be checked now)."""


class Cancelled(Exception):
    """The run was cancelled via Engine.cancel; progress is in the state file."""


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def app_dir():
    """Directory of the running program (the executable when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def configure_ssl():
    """A frozen build may not find the OS CA bundle; point it at one."""
    if not getattr(sys, "frozen", False) or os.name == "nt" or "SSL_CERT_FILE" in os.environ:
        return
    for bundle in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
                   "/etc/ssl/ca-bundle.pem", "/etc/ssl/cert.pem"):
        if os.path.isfile(bundle):
            os.environ["SSL_CERT_FILE"] = bundle
            return
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass


def find_ant(override=None):
    """Locate the ant binary: override, program dir, cwd, bundle, PATH, pip user dirs."""
    if override:
        p = Path(override).expanduser()
        return str(p) if p.is_file() else None
    dirs = [str(app_dir()), os.getcwd()]
    if hasattr(sys, "_MEIPASS"):
        dirs.append(sys._MEIPASS)
    found = shutil.which("ant", path=os.pathsep.join(dirs))
    if found:
        return found
    found = shutil.which("ant")
    if found:
        return found
    scheme = "nt_user" if os.name == "nt" else "posix_user"
    extra = [sysconfig.get_path("scripts", scheme), str(Path.home() / ".local" / "bin")]
    return shutil.which("ant", path=os.pathsep.join(extra))


# --------------------------------------------------------------------------
# State file
# --------------------------------------------------------------------------

def state_path_for(src, out_dir):
    return out_dir / f".{src.name}.state.json"


def load_state(path):
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise StepError(f"cannot read state file {path}: {e}")
    if state.get("stage") not in STAGES:
        raise StepError(f"state file {path} has unknown stage {state.get('stage')!r}")
    return state


def save_state(path, state):
    state["updated_at"] = utc_now_iso()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Autonomi output parsing
# --------------------------------------------------------------------------

def find_address_in_json(obj):
    """Depth-first search for a 64-hex address under a known key."""
    if isinstance(obj, dict):
        for key in ADDRESS_KEYS:
            val = obj.get(key)
            if isinstance(val, str):
                m = HEX64_RE.search(val)
                if m:
                    return m.group(1).lower()
        for val in obj.values():
            found = find_address_in_json(val)
            if found:
                return found
    elif isinstance(obj, list):
        for val in obj:
            found = find_address_in_json(val)
            if found:
                return found
    return None


def parse_json_objects(text):
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = text.find("{", idx)
        if start == -1:
            return
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            idx = start + 1
            continue
        yield obj
        idx = end


def extract_address(stdout, stderr):
    for obj in reversed(list(parse_json_objects(stdout))):
        addr = find_address_in_json(obj)
        if addr:
            return addr
    # Fallback for human-readable output: last 64-hex string.
    for text in (stdout, stderr):
        matches = HEX64_RE.findall(text)
        if matches:
            return matches[-1].lower()
    return None


# --------------------------------------------------------------------------
# OpenTimestamps (library, no CLI)
# --------------------------------------------------------------------------

def walk(stamp):
    yield stamp
    for sub in stamp.ops.values():
        yield from walk(sub)


def read_proof(ots_file):
    try:
        with open(ots_file, "rb") as f:
            return DetachedTimestampFile.deserialize(StreamDeserializationContext(f))
    except Exception as e:
        raise StepError(f"invalid OTS proof {ots_file}: {e}")


def write_proof(ots_file, detached):
    buf = io.BytesIO()
    detached.serialize(StreamSerializationContext(buf))
    tmp = ots_file.with_name(ots_file.name + ".tmp")
    tmp.write_bytes(buf.getvalue())
    os.replace(tmp, ots_file)


def proof_summary(detached):
    """Return (file_digest_hex, [(msg, height)], pending_count)."""
    btc, pending = [], 0
    for msg, att in detached.timestamp.all_attestations():
        if isinstance(att, BitcoinBlockHeaderAttestation):
            btc.append((msg, att.height))
        elif isinstance(att, PendingAttestation):
            pending += 1
    return detached.file_digest.hex(), btc, pending


def finalize_proof(detached):
    """Keep only the lowest Bitcoin attestation; drop pending ones and dead branches."""
    btc = [(node, att) for node in walk(detached.timestamp) for att in node.attestations
           if isinstance(att, BitcoinBlockHeaderAttestation)]
    best_node, best_att = min(btc, key=lambda x: x[1].height)
    for node in walk(detached.timestamp):
        for att in list(node.attestations):
            if not (node is best_node and att is best_att):
                node.attestations.remove(att)

    def prune(stamp):
        for op, sub in list(stamp.ops.items()):
            if prune(sub):
                del stamp.ops[op]
        return not stamp.attestations and not stamp.ops

    prune(detached.timestamp)


def esplora_get(path):
    last_err = None
    for base in ESPLORA_APIS:
        try:
            req = urllib.request.Request(base + path, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode()
        except Exception as e:
            last_err = e
    raise Pending(f"block explorer unreachable ({last_err})")


def bitcoin_block(height):
    block_hash = esplora_get(f"/block-height/{height}").strip()
    info = json.loads(esplora_get(f"/block/{block_hash}"))
    return block_hash, info["merkle_root"], int(info["timestamp"])


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

def _default_log(msg):
    if sys.stderr:
        print(f"[{datetime.now():%H:%M}] {msg}", file=sys.stderr, flush=True)


class Engine:
    """Runs the 4-stage workflow.

    log(msg)              - human-readable progress lines
    on_event(name, **kw)  - structured progress for UIs:
        "state"   state_file=Path, stage=str
        "step"    step=int (1..4)
        "check"                              (an upgrade attempt starts)
        "pending" reason=str, next_in=float  (seconds until next attempt; 0 = none)
        "anchored" height=int, time=int
    secret_key is passed only to the `ant` child process environment.
    """

    def __init__(self, *, ant=None, secret_key=None, evm_network=None,
                 upload_timeout=1800, interval_min=20.0, max_wait_hours=24.0,
                 wait=True, log=None, on_event=None):
        self.ant = find_ant(ant)
        self.secret_key = secret_key
        self.evm_network = evm_network
        self.upload_timeout = upload_timeout
        self.interval = interval_min * 60
        self.max_wait = max_wait_hours * 3600
        self.wait = wait
        self.log = log or _default_log
        self._emit = on_event or (lambda name, **kw: None)
        self._cancel = threading.Event()
        self._proc = None
        self.state_file = None
        self.state = None
        configure_ssl()

    # ---- control --------------------------------------------------------

    def cancel(self):
        """Thread-safe: stop waiting and terminate a running ant upload."""
        self._cancel.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()

    def _check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled()

    def _advance(self, stage, **fields):
        self.state.update(fields)
        self.state["stage"] = stage
        save_state(self.state_file, self.state)
        self.log(f"stage -> {stage} (saved {self.state_file})")
        self._emit("state", state_file=self.state_file, stage=stage)

    # ---- ant ------------------------------------------------------------

    def ant_upload(self, path):
        if not self.ant:
            raise StepError("`ant` CLI not found. Put it on PATH or next to this program, "
                            "or choose it explicitly.")
        if not self.secret_key:
            raise StepError("no wallet key: `ant file upload` needs the EVM private key "
                            "(SECRET_KEY) to pay for storage.")
        cmd = [self.ant]
        if self.evm_network:
            cmd += ["--evm-network", self.evm_network]
        cmd += ["file", "upload", "--public", "--json", str(path)]
        env = os.environ.copy()
        env["SECRET_KEY"] = self.secret_key
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.log("$ " + " ".join(cmd))
        self._check_cancel()
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                env=env, text=True, encoding="utf-8", errors="replace", creationflags=flags)
        except OSError as e:
            raise StepError(f"cannot run {self.ant}: {e}")
        try:
            out, err = self._proc.communicate(timeout=self.upload_timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.communicate()
            raise StepError(f"ant upload timed out after {self.upload_timeout}s")
        finally:
            code, self._proc = self._proc.returncode, None
        self._check_cancel()
        if code != 0:
            detail = err.strip() or out.strip() or "(no output)"
            raise StepError(f"ant upload failed (exit {code}):\n{detail}")
        addr = extract_address(out, err)
        if not addr:
            raise StepError("upload succeeded but no public address could be parsed.\n"
                            f"stdout:\n{out.strip()}\nstderr:\n{err.strip()}")
        return addr

    # ---- OpenTimestamps -------------------------------------------------

    def ots_stamp(self, address_file, address):
        ots_file = address_file.with_name(address_file.name + ".ots")
        data = address.encode()
        address_file.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        if ots_file.exists():
            # Interrupted right after a previous stamp: adopt it if it matches.
            if proof_summary(read_proof(ots_file))[0] == expected:
                self.log(f"reusing existing proof {ots_file}")
                return ots_file
            raise StepError(f"{ots_file} exists but does not timestamp {address_file}; "
                            "remove it and re-run")

        detached = DetachedTimestampFile.from_fd(OpSHA256(), io.BytesIO(data))
        tip = detached.timestamp.ops.add(OpAppend(os.urandom(16))).ops.add(OpSHA256())
        merged = 0
        with ThreadPoolExecutor(len(DEFAULT_AGGREGATORS)) as pool:
            futures = {pool.submit(RemoteCalendar(url, USER_AGENT).submit, tip.msg,
                                   timeout=CALENDAR_TIMEOUT): url
                       for url in DEFAULT_AGGREGATORS}
            for fut in as_completed(futures):
                url = futures[fut]
                try:
                    tip.merge(fut.result())
                    merged += 1
                    self.log(f"calendar {url}: submitted")
                except Exception as e:
                    self.log(f"calendar {url}: failed ({e})")
        if merged < MIN_CALENDARS:
            raise StepError(f"only {merged} OpenTimestamps calendar(s) answered; "
                            f"need at least {MIN_CALENDARS}")
        write_proof(ots_file, detached)
        return ots_file

    def ots_upgrade(self, detached):
        """Ask each pending calendar for a Bitcoin-anchored path. Returns notes."""
        notes = []
        pendings = [(node, att) for node in walk(detached.timestamp)
                    for att in list(node.attestations) if isinstance(att, PendingAttestation)]
        for node, att in pendings:
            self._check_cancel()
            host = att.uri.split("//")[-1]
            if att.uri not in DEFAULT_CALENDAR_WHITELIST:
                notes.append(f"{host}: not whitelisted, ignored")
                continue
            try:
                upgraded = RemoteCalendar(att.uri, USER_AGENT).get_timestamp(
                    node.msg, timeout=CALENDAR_TIMEOUT)
            except CommitmentNotFoundError as e:
                notes.append(f"{host}: {e.reason}")
                continue
            except Exception as e:
                notes.append(f"{host}: {e}")
                continue
            node.merge(upgraded)
            got_btc = any(isinstance(a, BitcoinBlockHeaderAttestation)
                          for _, a in upgraded.all_attestations())
            notes.append(f"{host}: {'Bitcoin attestation!' if got_btc else 'still pending'}")
        return notes

    def try_confirm(self, ots_file, address_file):
        """One upgrade attempt. Returns block info dict, or raises Pending."""
        self._emit("check")
        self.log(f"checking OpenTimestamps calendars for {ots_file.name}")
        expected = hashlib.sha256(address_file.read_bytes()).hexdigest()
        detached = read_proof(ots_file)
        if proof_summary(detached)[0] != expected:
            raise StepError(f"{ots_file} does not timestamp {address_file}")

        if not proof_summary(detached)[1]:
            notes = self.ots_upgrade(detached)
            for n in notes:
                self.log(f"  {n}")
            write_proof(ots_file, detached)
        _, btc, pending = proof_summary(detached)
        if not btc:
            raise Pending(f"no Bitcoin attestation yet ({pending} pending)")

        if pending or len(btc) > 1:
            finalize_proof(detached)
            write_proof(ots_file, detached)
            _, btc, pending = proof_summary(detached)
            if pending or len(btc) != 1:
                raise StepError(f"finalizing {ots_file} left an unexpected proof "
                                f"({len(btc)} bitcoin, {pending} pending)")
            self.log("pruned pending attestations; proof is final")

        # Independently check the attestation against the real block header:
        # the attested message must equal the block's merkle root.
        msg, height = btc[0]
        block_hash, merkle_root, block_time = bitcoin_block(height)
        if msg[::-1].hex() != merkle_root.lower():
            raise StepError(f"proof claims block {height} but its merkle root does not "
                            f"match ({msg[::-1].hex()} != {merkle_root})")
        return {"block_height": height, "block_hash": block_hash, "block_time": block_time}

    # ---- workflow -------------------------------------------------------

    def start(self, file, out_dir=None):
        """New run for FILE, or auto-resume if its state file exists."""
        src = Path(file).expanduser().resolve()
        if not src.is_file():
            raise StepError(f"not a file: {src}")
        out_dir = Path(out_dir).expanduser().resolve() if out_dir else src.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = state_path_for(src, out_dir)
        if self.state_file.exists():
            self.state = load_state(self.state_file)
            self.log(f"resuming from {self.state_file} (stage: {self.state['stage']})")
        else:
            self.state = {
                "stage": "new",
                "file": str(src),
                "file_sha256": sha256_file(src),
                "address_file": str(out_dir / f"{src.name}_address"),
                "created_at": utc_now_iso(),
            }
            save_state(self.state_file, self.state)
        return self._run()

    def resume(self, state_file):
        self.state_file = Path(state_file).expanduser().resolve()
        self.state = load_state(self.state_file)
        self.log(f"resuming from {self.state_file} (stage: {self.state['stage']})")
        return self._run()

    def _run(self):
        """Returns 0 when done, EXIT_PENDING when still waiting.
        Raises StepError on failure and Cancelled on cancel."""
        state = self.state
        self._emit("state", state_file=self.state_file, stage=state["stage"])
        src = Path(state["file"])

        # Guard against resuming with a file that changed since its upload.
        if state["stage"] == "new" or src.is_file():
            if sha256_file(src) != state["file_sha256"]:
                raise StepError(f"{src} changed since this run started (sha256 mismatch). "
                                f"Delete {self.state_file} to start a new timestamp.")
        address_file = Path(state["address_file"])

        if state["stage"] == "new":
            self._emit("step", step=1)
            self.log(f"Step 1/4: uploading {src.name} to Autonomi")
            addr = self.ant_upload(src)
            self._advance("uploaded", data_address=addr)
            self.log(f"data address: {addr}")

        if state["stage"] == "uploaded":
            self._emit("step", step=2)
            self.log("Step 2/4: stamping the address with OpenTimestamps")
            ots_file = self.ots_stamp(address_file, state["data_address"])
            self._advance("stamped", ots_file=str(ots_file), stamped_at=utc_now_iso())

        if state["stage"] == "stamped":
            self._emit("step", step=3)
            self.log("Step 3/4: waiting for the proof to be anchored in a Bitcoin block")
            ots_file = Path(state["ots_file"])
            deadline = time.monotonic() + self.max_wait if self.max_wait > 0 else None
            while True:
                self._check_cancel()
                state["checks"] = state.get("checks", 0) + 1
                state["last_check"] = utc_now_iso()
                try:
                    block = self.try_confirm(ots_file, address_file)
                    break
                except Pending as p:
                    state["last_status"] = str(p)
                    save_state(self.state_file, state)
                    if not self.wait or (deadline and
                                         time.monotonic() + self.interval > deadline):
                        self._emit("pending", reason=str(p), next_in=0)
                        self.log(f"Still pending: {p}")
                        self.log(f"Resume later with: --resume {self.state_file}")
                        return EXIT_PENDING
                    self._emit("pending", reason=str(p), next_in=self.interval)
                    self.log(f"Still pending ({p}). Next check in {self.interval / 60:g} minutes.")
                    if self._cancel.wait(self.interval):
                        raise Cancelled()
            self._advance("confirmed", last_status="confirmed", **block)
            self._emit("anchored", height=block["block_height"], time=block["block_time"])
            self.log(f"anchored in Bitcoin block {block['block_height']} "
                     f"({fmt_utc(block['block_time'])})")

        if state["stage"] == "confirmed":
            self._emit("step", step=4)
            ots_file = Path(state["ots_file"])
            self.log(f"Step 4/4: uploading final proof {ots_file.name} to Autonomi")
            _, btc, pending = proof_summary(read_proof(ots_file))
            if pending or not btc:
                raise StepError(f"{ots_file} is not a finalized proof; refusing to upload")
            ots_addr = self.ant_upload(ots_file)
            self._advance("done", ots_address=ots_addr)

        return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def summary_text(state):
    rows = [
        ("Original File", Path(state["file"]).name),
        ("Autonomi Data Address", f"ant://{state['data_address']}"),
        ("Bitcoin Block Height", str(state["block_height"])),
        ("Bitcoin Block Time", fmt_utc(state["block_time"])),
        ("Bitcoin Block Hash", state["block_hash"]),
        ("Local OTS Proof File", state["ots_file"]),
        ("Autonomi OTS Proof Address", f"ant://{state['ots_address']}"),
    ]
    width = max(len(k) for k, _ in rows)
    line = "=" * 92
    out = [line, " Autonomi + Bitcoin (OpenTimestamps) proof of existence - CONFIRMED", line]
    out += [f" {k:<{width}} : {v}" for k, v in rows]
    out += [line,
            f" Verify proof : ots verify -f {state['address_file']} {state['ots_file']}",
            f" Fetch proof  : ant file download {state['ots_address']} -o proof.ots",
            line]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="autonomi_timestamp",
        description="Upload a file to Autonomi, anchor its address in Bitcoin via "
                    "OpenTimestamps, wait for the Bitcoin confirmation, then upload "
                    "the final OTS proof to Autonomi.",
        epilog="Exit codes: 0 done, 1 error, 2 still pending.")
    ap.add_argument("file", type=Path, nargs="?", help="local file to timestamp")
    ap.add_argument("--resume", type=Path, metavar="STATE_FILE",
                    help="resume from a .<file>.state.json (FILE not needed)")
    ap.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True,
                    help="keep polling until the proof is in a Bitcoin block "
                         "(default: on; --no-wait does a single check)")
    ap.add_argument("--check-only", action="store_true",
                    help="single upgrade/check attempt, then exit (same as --no-wait)")
    ap.add_argument("--interval", type=float, default=20, metavar="MIN",
                    help="minutes between upgrade attempts (default 20)")
    ap.add_argument("--max-wait-hours", type=float, default=24, metavar="H",
                    help="give up waiting after H hours, 0 = forever (default 24); "
                         "the state is kept so you can resume later")
    ap.add_argument("--out-dir", type=Path,
                    help="where to write <name>_address, <name>_address.ots and the "
                         "state file (default: next to the input file)")
    ap.add_argument("--ant", help="path to ant binary (default: auto-detect)")
    ap.add_argument("--evm-network", help="passed through to ant --evm-network")
    ap.add_argument("--upload-timeout", type=int, default=1800,
                    help="seconds per ant upload (default 1800)")
    args = ap.parse_args(argv)

    if bool(args.file) == bool(args.resume):
        ap.error("give either FILE or --resume STATE_FILE")

    engine = Engine(ant=args.ant, secret_key=os.environ.get("SECRET_KEY"),
                    evm_network=args.evm_network, upload_timeout=args.upload_timeout,
                    interval_min=args.interval, max_wait_hours=args.max_wait_hours,
                    wait=args.wait and not args.check_only)
    try:
        code = engine.resume(args.resume) if args.resume else engine.start(args.file, args.out_dir)
    except StepError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, Cancelled):
        print(f"\ninterrupted; progress is saved. Resume with: --resume {engine.state_file}",
              file=sys.stderr)
        return 1
    if code == 0:
        print("\n" + summary_text(engine.state))
    return code


if __name__ == "__main__":
    sys.exit(main())
