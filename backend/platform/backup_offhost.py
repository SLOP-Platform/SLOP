"""backend/platform/backup_offhost.py — off-host backup engine (#868 P4, design §13).

The OPERATIONAL off-host tier — a sibling of ``backup_ops.py`` (kept separate so neither
crowds the 500-line production cap, and so the off-host machinery, which shells out to
external binaries, is isolated from the pure-Python local-tar primitives). The SLOP agent
is still runtime-only / observe-only (two-owner firewall): off-host UPLOAD + age ENCRYPT
are actions and live here, never in ``backend/agent/``.

Two layers: (1) the CAPABILITY/PREFLIGHT gate (``offhost_preflight`` — is the toolchain present
and the rclone remote configured? surfaced LOUDLY in the pinned vocabulary); and (2) the EXECUTE
engine (``execute_offhost_backup`` — age dual-recipient ciphertext + rclone copy + ephemeral
per-run restore-verify, the design §13 CRUX). The preflight gate runs FIRST inside execute, so a
misconfigured off-host target is refused before any encrypt/upload, never a silent later failure.

GROUND: every check here touches physics — ``shutil.which`` (the filesystem PATH) and
``rclone listremotes`` (the operator-provisioned ``rclone.conf``). SLOP reads the remote
NAME only; it never reads or writes the secret in ``rclone.conf`` (the no-stored-credential
/ two-owner firewall, design §13 [DToC-3]).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from backend.core.logging import get_logger

from .backup_ops import (
    LOCAL_TARGET,
    VERDICT_DRIFT,
    VERDICT_INDETERMINATE,
    VERDICT_VERIFIED,
    VERIFY_SCOPE_OFFHOST,
    BackupTarget,
    Invariant,
    execute_backup,
    parse_backup_target,
    verify_backup_artifact,
    write_verify_sidecar,
)

log = get_logger(__name__)

# The external binaries the off-host engine shells out to. ``rclone`` is the single
# off-host transport (design §13 [DToC-3] — the remote-name encodes the protocol);
# ``age`` is the encryption tool (§13 [DToC-4] — minimal, recipient-pubkey, custody-clean).
OFFHOST_TOOLS = ("rclone", "age")

# age key markers (the age v1 ASCII formats). "identity" is age's term for a PRIVATE key.
_AGE_PUBKEY_PREFIX = "age1"  # a recipient PUBLIC key — what SLOP config stores / encrypts to.
_AGE_IDENTITY_PREFIX = "AGE-SECRET-KEY-"  # an identity (PRIVATE) key — never stored, never config.

# Timeouts (seconds) for the shelled-out tools — bounded so a hung binary can't stall a backup.
_AGE_TIMEOUT = 120  # keygen / encrypt / decrypt of one artifact
_RCLONE_COPY_TIMEOUT = 3600  # an off-host upload of a large artifact may legitimately be slow


class OffhostError(RuntimeError):
    """An off-host backup step failed. Messages NEVER carry a tool's raw stderr — an rclone/age
    error can echo a remote path or secret material from ``rclone.conf`` (the credential firewall,
    design §13). Exit code + a generic reason only; the operator runs the tool by hand for detail."""


def offhost_toolchain_status() -> tuple[str, str]:
    """Are the off-host binaries (rclone + age) on PATH? GROUND: ``shutil.which``.

    ``verified`` when both are present; ``INDETERMINATE`` (LOUD, never a silent OK) when
    any is missing — the engine cannot encrypt/upload without them, and a missing tool is
    an environment gap to surface, not a backup failure to assert."""
    missing = [tool for tool in OFFHOST_TOOLS if shutil.which(tool) is None]
    if missing:
        return (
            VERDICT_INDETERMINATE,
            f"off-host toolchain incomplete — not on PATH: {', '.join(missing)} "
            "(install rclone + age to enable off-host backups, design §13)",
        )
    return VERDICT_VERIFIED, "off-host toolchain present (rclone + age)"


def rclone_remote_exists(remote: str) -> tuple[str, str]:
    """Is *remote* a configured rclone remote? GROUND: ``rclone listremotes``.

    Reconciles the remote-NAME SLOP stores against the operator-provisioned ``rclone.conf``
    (SLOP never reads the secret in it — only the list of remote names). ``verified`` when
    ``<remote>:`` appears in the listing; ``DRIFT`` when it does not (the operator named a
    remote SLOP cannot see — the off-host copy would silently never land); ``INDETERMINATE``
    when rclone is absent/unrunnable (cannot reach ground truth)."""
    if shutil.which("rclone") is None:
        return VERDICT_INDETERMINATE, "rclone not on PATH — cannot verify remote"
    try:
        proc = subprocess.run(
            ["rclone", "listremotes"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return VERDICT_INDETERMINATE, f"rclone listremotes failed to run: {exc}"
    if proc.returncode != 0:
        # Firewall: do NOT surface proc.stderr — an rclone error can echo secret material
        # from rclone.conf (the very secret SLOP must never read or surface, design §13).
        # Exit code only; the operator runs `rclone listremotes` themselves for the detail.
        return (
            VERDICT_INDETERMINATE,
            f"rclone listremotes failed (exit {proc.returncode}) — check rclone.conf "
            "(stderr withheld: may contain credentials)",
        )
    # rclone prints one ``name:`` per line.
    names = {line.strip().rstrip(":") for line in proc.stdout.splitlines() if line.strip()}
    if remote in names:
        return VERDICT_VERIFIED, f"rclone remote {remote!r} is configured"
    return (
        VERDICT_DRIFT,
        f"rclone remote {remote!r} is NOT configured (have: {', '.join(sorted(names)) or 'none'}) "
        "— the off-host copy would never land",
    )


def offhost_preflight(target: BackupTarget) -> tuple[str, str]:
    """Gate the off-host path runs before any encrypt/upload. GROUND, pinned vocabulary.

    For a ``local`` target there is nothing off-host to check → ``verified``. For ``rclone``
    it ANDs the toolchain check and the remote-exists check: a non-``verified`` leg
    short-circuits and is returned verbatim (the LOUD reason), so the caller never proceeds
    to encrypt/upload against a broken environment."""
    if target.kind == "local":
        return VERDICT_VERIFIED, "local target — no off-host preflight required"
    tool_verdict, tool_reason = offhost_toolchain_status()
    if tool_verdict != VERDICT_VERIFIED:
        return tool_verdict, tool_reason
    remote_verdict, remote_reason = rclone_remote_exists(target.remote)
    if remote_verdict != VERDICT_VERIFIED:
        return remote_verdict, remote_reason
    return (
        VERDICT_VERIFIED,
        f"off-host preflight OK — toolchain present, remote {target.remote!r} configured",
    )


# ── The off-host execute engine (#1283 CRUX, design §13) ─────────────────────────
# Flow: local plaintext tar → age dual-recipient encrypt (operator DR pubkey + an EPHEMERAL
# per-run keypair) → rclone upload of that ONE ciphertext → ephemeral in-window restore-verify
# (decrypt the SAME ciphertext with the ephemeral PRIVATE key, run the existing
# verify_backup_artifact on the decrypted bytes) → DESTROY the ephemeral key. SLOP persists NO
# decryption material: the ephemeral private key lives only in process memory (a ``bytearray``
# zeroed in a ``finally``) and is NEVER written to disk; the operator's DR private key lives
# off-SLOP. This proves the off-host ciphertext decrypts WITHOUT SLOP holding a decryption key —
# the gap a verify-then-encrypt model leaves (it never proves the encrypted copy restores).


def _zero(buf: bytearray) -> None:
    """Best-effort wipe of key material held in a mutable buffer (design §13 residual (b))."""
    for i in range(len(buf)):
        buf[i] = 0


def _ephemeral_dir() -> Path:
    """A private throwaway work dir (mode 0700 via mkdtemp) for the cipher + decrypted scratch;
    removed by the caller in a ``finally``. The decrypted scratch is no more sensitive than the
    local plaintext artifact already on disk in backup_dir; the load-bearing custody guarantee is
    that the ephemeral PRIVATE key never touches disk (it is piped to age via stdin), not where the
    scratch lives."""
    return Path(tempfile.mkdtemp(prefix="ms-offhost-"))


def _age_keygen() -> tuple[bytearray, str]:
    """Generate an EPHEMERAL age keypair. Returns ``(privkey_bytes, pubkey_str)``.

    The private key is returned as a ``bytearray`` (zeroable, never a disk file) — custody-clean.
    GROUND: shells ``age-keygen``; parses the secret-key line + the ``# public key:`` comment from
    its output (falling back to the ``Public key:`` stderr line). Raises :class:`OffhostError` on a
    keygen failure or unparseable output (never returns a half-keypair)."""
    try:
        proc = subprocess.run(
            ["age-keygen"], capture_output=True, timeout=_AGE_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OffhostError(f"age-keygen failed to run: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise OffhostError(f"age-keygen failed (exit {proc.returncode})")
    priv = bytearray()
    pub = ""
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if line.startswith(_AGE_IDENTITY_PREFIX.encode()):
            priv = bytearray(line)
        else:
            text = line.decode("utf-8", "replace")
            if text.lower().startswith("# public key:"):
                pub = text.split(":", 1)[1].strip()
    if not pub:  # some age builds print the pubkey only to stderr
        for raw in proc.stderr.splitlines():
            text = raw.decode("utf-8", "replace").strip()
            if text.lower().startswith("public key:"):
                pub = text.split(":", 1)[1].strip()
                break
    if not priv or not pub.startswith(_AGE_PUBKEY_PREFIX):
        _zero(priv)
        raise OffhostError("age-keygen produced no usable keypair")
    return priv, pub


def _age_encrypt(plaintext: Path, cipher: Path, recipients: list[str]) -> None:
    """age-encrypt *plaintext* to *cipher* for every recipient PUBLIC key in *recipients* (one
    ciphertext, multiple ``-r`` stanzas). GROUND: shells ``age``. Raises :class:`OffhostError`."""
    cmd = ["age", "-o", str(cipher)]
    for r in recipients:
        cmd += ["-r", r]
    cmd.append(str(plaintext))
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_AGE_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OffhostError(f"age encrypt failed to run: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise OffhostError(f"age encrypt failed (exit {proc.returncode})")


def _age_decrypt(cipher: Path, out: Path, privkey: bytearray) -> None:
    """age-decrypt *cipher* to *out* using *privkey* — the identity is piped via STDIN
    (``age -d -i -``) so it NEVER touches disk. GROUND: shells ``age``. Raises
    :class:`OffhostError` (a decrypt failure here means the off-host copy does NOT round-trip).

    The stdin payload is a ``bytearray`` (subprocess accepts a bytes-like input directly) zeroed in
    a ``finally`` — so neither the caller's *privkey* NOR this newline-appended copy survives
    un-wiped in the Python heap (review #1283 finding 1: avoid an un-zeroed ``bytes()`` copy)."""
    stdin = bytearray(privkey)
    stdin += b"\n"
    try:
        proc = subprocess.run(
            ["age", "-d", "-i", "-", "-o", str(out), str(cipher)],
            input=stdin,
            capture_output=True,
            timeout=_AGE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OffhostError(f"age decrypt failed to run: {type(exc).__name__}") from exc
    finally:
        _zero(stdin)
    if proc.returncode != 0:
        raise OffhostError(f"age decrypt (in-window verify) failed (exit {proc.returncode})")


def _rclone_copy(cipher: Path, remote: str) -> None:
    """rclone-copy *cipher* to the named *remote* (``<remote>:``). GROUND: shells ``rclone``;
    SLOP passes only the remote-NAME (the secret lives in the operator's ``rclone.conf``). Raises
    :class:`OffhostError` — stderr is WITHHELD (it may echo credential material from rclone.conf)."""
    try:
        proc = subprocess.run(
            ["rclone", "copy", str(cipher), f"{remote}:"],
            capture_output=True,
            timeout=_RCLONE_COPY_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OffhostError(f"rclone copy failed to run: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise OffhostError(
            f"rclone copy to {remote!r} failed (exit {proc.returncode}) "
            "(stderr withheld: may contain credentials)"
        )


def _age_recipient_stanza_count(cipher: Path) -> int:
    """Count the ``-> `` recipient stanzas in an age v1 header (one per ``-r`` recipient). The
    XREF leg (design §13 residual (a)): a ciphertext encrypted to operator+ephemeral MUST carry 2
    stanzas — fewer means a recipient was silently dropped (the operator could never decrypt). This
    is XREF (text-vs-text on the header), never a GROUND assertion the operator's key decrypts."""
    count = 0
    with open(cipher, "rb") as fh:
        for raw in fh:
            line = raw.rstrip(b"\n")
            if line.startswith(b"---"):  # end-of-header marker — payload (binary) follows
                break
            if line.startswith(b"-> "):
                count += 1
    return count


def execute_offhost_backup(
    source: str | Path,
    backup_dir: str | Path,
    name_prefix: str,
    target: str | dict[str, object] | BackupTarget,
    operator_recipient: str,
    *,
    timestamp: str | None = None,
    invariant: Invariant | None = None,
) -> tuple[Path, str, str]:
    """Execute a full off-host backup (design §13 CRUX). Returns ``(local_artifact, verdict, reason)``.

    Steps (each GROUND / honest-refusal, never a silent local-only fallback):
      1. **Preflight** — :func:`offhost_preflight` must be ``verified`` (toolchain present + remote
         configured); otherwise raise (refusing to encrypt/upload against a broken environment).
      2. **Local tar** — :func:`execute_backup` writes the local plaintext ``.tar.gz`` (the on-host
         recoverable copy AND the encrypt input).
      3. **Dual-recipient encrypt** — one age ciphertext to TWO recipients: the operator's DR public
         key (*operator_recipient*, the real recovery key — its private key lives off-SLOP) AND an
         EPHEMERAL per-run public key.
      4. **XREF stanza check** — the header must carry both recipient stanzas (else the operator
         recipient was dropped → unrecoverable for the operator).
      5. **Upload** — rclone-copy that SAME ciphertext to ``<remote>:``.
      6. **In-window restore-verify** — decrypt the SAME ciphertext with the ephemeral PRIVATE key
         (piped via stdin, never on disk), run :func:`verify_backup_artifact` (+ *invariant*) on the
         decrypted bytes, and record the verdict in a sidecar with scope
         :data:`VERIFY_SCOPE_OFFHOST`.
      7. **Destroy** — zero the ephemeral private key and remove the tmpfs work dir in a ``finally``.

    *operator_recipient* MUST be an age recipient PUBLIC key (``age1…``); a secret/identity key is
    rejected fail-closed (SLOP must never be handed decryption material). Raises :class:`OffhostError`
    on any step failure, ``ValueError`` on a bad target/recipient. The cipher + decrypted scratch
    live ONLY in the ephemeral (tmpfs-preferred) work dir; the off-host copy is on the remote and the
    local plaintext artifact stays in *backup_dir*."""
    tgt = target if isinstance(target, BackupTarget) else parse_backup_target(target)
    if tgt.kind != "rclone":
        raise ValueError(
            f"execute_offhost_backup requires an rclone target, got {tgt.kind!r} "
            "(use backup_ops.execute_backup for local backups)"
        )
    op = (operator_recipient or "").strip()
    if op.startswith(_AGE_IDENTITY_PREFIX) or not op.startswith(_AGE_PUBKEY_PREFIX):
        raise ValueError(
            "operator_recipient must be an age recipient PUBLIC key (age1…), never a secret/"
            "identity key — SLOP encrypts TO the operator's key and must never hold a decryption key"
        )

    pf_verdict, pf_reason = offhost_preflight(tgt)
    if pf_verdict != VERDICT_VERIFIED:
        # LOUD refusal — NOT a silent local-only store (the "green-local but no off-host copy"
        # theater the design forbids). The local tar is not even produced yet.
        raise OffhostError(f"off-host preflight not verified — refusing to proceed: {pf_reason}")

    local_artifact = execute_backup(
        source, backup_dir, name_prefix, timestamp=timestamp, target=LOCAL_TARGET
    )

    workdir = _ephemeral_dir()
    priv = bytearray()
    try:
        priv, ephem_pub = _age_keygen()
        cipher = workdir / (local_artifact.name + ".age")
        _age_encrypt(local_artifact, cipher, [op, ephem_pub])

        stanzas = _age_recipient_stanza_count(cipher)
        if stanzas != 2:
            raise OffhostError(
                f"age ciphertext carries {stanzas} recipient stanza(s), expected 2 "
                "(operator + ephemeral) — the operator recipient was not embedded; the off-host "
                "copy would be unrecoverable for the operator"
            )

        _rclone_copy(cipher, tgt.remote)

        decrypted = workdir / local_artifact.name
        _age_decrypt(cipher, decrypted, priv)
        verdict, reason = verify_backup_artifact(decrypted, invariant=invariant)
        write_verify_sidecar(
            backup_dir, local_artifact.name, verdict, verify_scope=VERIFY_SCOPE_OFFHOST
        )
        return local_artifact, verdict, reason
    finally:
        _zero(priv)
        # tmpfs work dir holds the cipher + decrypted scratch — remove unconditionally so no
        # decrypted plaintext (or cipher) lingers, even on an exception mid-flow.
        shutil.rmtree(workdir, ignore_errors=True)


__all__ = [
    "OFFHOST_TOOLS",
    "OffhostError",
    "execute_offhost_backup",
    "offhost_preflight",
    "offhost_toolchain_status",
    "rclone_remote_exists",
]
