"""Data download: isolated acquisition and preparation responsibilities."""

from __future__ import annotations

import csv
import http.client
import multiprocessing
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from email.utils import parsedate_to_datetime
from pathlib import Path

from metrosim26.datasets.cache import reuse_cached_input
from metrosim26.datasets.files import _file_sha256, atomic_json
from metrosim26.datasets.validation import (
    _VALIDATED_INPUTS,
    VALIDATOR_VERSION,
    _file_identity,
    _validation_path,
    validate_dataset,
    validate_input,
)


def _retry_delay(error, attempt, cfg):
    value = getattr(error, "retry_after", None)
    if value is None and isinstance(error, urllib.error.HTTPError) and error.headers:
        value = error.headers.get("Retry-After")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    return min(
        float(cfg.get("download_backoff_max", 300)),
        float(cfg.get("download_backoff_initial", 30)) * 2**attempt,
    )


class DownloadAttemptError(OSError):
    """A serializable HTTP-attempt failure reported by a disposable process."""

    def __init__(self, message, *, error_type="OSError", http_code=None, retry_after=None):
        super().__init__(message)
        self.error_type = error_type
        self.http_code = http_code
        self.retry_after = retry_after


def _download_once(endpoint, post_data, temp_path, socket_timeout):
    request = urllib.request.Request(
        endpoint,
        data=post_data,
        headers={"User-Agent": "MetroSim26/3 (local research visualization)"},
    )
    with urllib.request.urlopen(request, timeout=socket_timeout) as response:
        source_url = response.geturl()
        with open(temp_path, "wb") as handle:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                handle.write(block)
    return source_url


def _download_attempt_worker(endpoint, post_data, temp_path, socket_timeout, sender):
    """Child target: its process can be terminated even during a blocked socket read."""
    try:
        source_url = _download_once(endpoint, post_data, temp_path, socket_timeout)
        sender.send({"ok": True, "source_url": source_url})
    except BaseException as exc:
        headers = getattr(exc, "headers", None)
        sender.send(
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "message": str(exc),
                "http_code": getattr(exc, "code", None),
                "retry_after": headers.get("Retry-After") if headers else None,
            }
        )
    finally:
        sender.close()


def _download_attempt_with_deadline(endpoint, post_data, temp_path, cfg):
    socket_timeout = float(cfg.get("download_timeout", 240))
    hard_timeout = float(cfg.get("download_attempt_timeout", 300))
    if hard_timeout <= 0:  # Used only by isolated unit tests with mocked HTTP responses.
        return _download_once(endpoint, post_data, temp_path, socket_timeout)

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_download_attempt_worker,
        args=(endpoint, post_data, str(temp_path), socket_timeout, sender),
        daemon=True,
    )
    process.start()
    sender.close()
    try:
        process.join(hard_timeout)
    except BaseException:
        if process.is_alive():
            process.terminate()
            process.join(10)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join()
        receiver.close()
        process.close()
        raise
    if process.is_alive():
        process.terminate()
        process.join(10)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join()
        receiver.close()
        process.close()
        raise DownloadAttemptError(
            f"hard wall-clock timeout after {hard_timeout:g} seconds",
            error_type="HardTimeout",
        )
    exit_code = process.exitcode
    try:
        result = receiver.recv() if receiver.poll() else None
    finally:
        receiver.close()
        process.close()
    if not result:
        raise DownloadAttemptError(
            f"download process exited without a result (exit code {exit_code})",
            error_type="ChildProcessError",
        )
    if not result["ok"]:
        raise DownloadAttemptError(
            result["message"],
            error_type=result["error_type"],
            http_code=result["http_code"],
            retry_after=result["retry_after"],
        )
    return result["source_url"]


def _endpoint_name(url):
    return urllib.parse.urlparse(url).netloc or url


def _attempt_error_text(error):
    code = getattr(error, "http_code", None) or getattr(error, "code", None)
    return (
        f"HTTP {code}" if code else f"{getattr(error, 'error_type', type(error).__name__)}: {error}"
    )


def _download_cached(spec, cfg):
    """Sequential, bounded retries with atomic per-file publication and receipts."""
    try:
        record = validate_input(spec)
        # Register valid old caches without redownloading; endpoint remains unknown.
        if cfg.get("data_mode", "OFFLINE").upper() == "ONLINE":
            atomic_json(_validation_path(spec.path), record)
        return record
    except (OSError, ValueError, EOFError, csv.Error, zlib.error):
        pass
    if reuse_cached_input(spec, cfg):
        return validate_input(spec)
    if cfg.get("data_mode", "OFFLINE").upper() != "ONLINE":
        raise FileNotFoundError(
            f"{spec.label} is missing/invalid: {spec.path}. "
            "Run scripts/prefetch_data.py outside Blender first."
        )
    spec.path.parent.mkdir(parents=True, exist_ok=True)
    attempts = int(cfg.get("download_attempts", 3))
    endpoints = (
        [spec.url] + list(cfg.get("overpass_fallback_urls", []))
        if spec.kind == "osm"
        else [spec.url]
    )
    health = cfg.setdefault("_endpoint_health", {})
    used = {endpoint: 0 for endpoint in endpoints}
    errors = []

    def next_endpoint():
        available = [
            url
            for url in endpoints
            if used[url] < attempts and not health.get(url, {}).get("disabled")
        ]
        return (
            min(available, key=lambda url: (used[url], endpoints.index(url))) if available else None
        )

    last_error = None
    limit = 1 if attempts == 1 else attempts * len(endpoints)
    for attempt in range(limit):
        temp_path = None
        endpoint = next_endpoint()
        if endpoint is None:
            break
        used[endpoint] += 1
        endpoint_name = _endpoint_name(endpoint)
        started = time.monotonic()
        try:
            print(f"  {endpoint_name}: try {used[endpoint]}/{attempts}", flush=True)
            post = urllib.parse.urlencode({"data": spec.query}).encode() if spec.query else None
            with tempfile.NamedTemporaryFile(
                prefix=spec.path.name + ".", suffix=".part", dir=spec.path.parent, delete=False
            ) as handle:
                temp_path = Path(handle.name)
            source_url = _download_attempt_with_deadline(endpoint, post, temp_path, cfg)
            details = validate_dataset(temp_path, spec.kind)
            record = {
                "validator_version": VALIDATOR_VERSION,
                "kind": spec.kind,
                "sha256": _file_sha256(temp_path),
                "size": temp_path.stat().st_size,
                "details": details,
                "source_url": source_url,
                "requested_url": endpoint,
                "validated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            os.replace(temp_path, spec.path)
            atomic_json(_validation_path(spec.path), record)
            _VALIDATED_INPUTS[(_file_identity(spec.path), spec.kind, VALIDATOR_VERSION)] = dict(
                record
            )
            elapsed = time.monotonic() - started
            size_mb = record["size"] / (1024 * 1024)
            print(
                f"  {endpoint_name} -> SUCCESS — {size_mb:.1f} MB — {elapsed:.1f} s",
                flush=True,
            )
            health[endpoint] = {}
            return record
        except (
            OSError,
            ValueError,
            EOFError,
            csv.Error,
            zlib.error,
            http.client.HTTPException,
        ) as exc:
            last_error = exc
            elapsed = time.monotonic() - started
            print(f"  {endpoint_name} -> {_attempt_error_text(exc)} — {elapsed:.1f} s", flush=True)
            http_code = getattr(exc, "http_code", None) or getattr(exc, "code", None)
            errors.append(f"{endpoint_name}: {_attempt_error_text(exc)}")
            if http_code == 500 and endpoint != spec.url:
                health[endpoint] = {"disabled": True}
                print(
                    f"  Skipping {endpoint_name} for the rest of this job (HTTP 500).", flush=True
                )
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            if http_code is not None and http_code not in {408, 429, 500, 502, 503, 504}:
                break
            following = next_endpoint() if attempt + 1 < limit else None
            if following is not None:
                delay = _retry_delay(exc, used[endpoint] - 1, cfg)
                # Switching servers does not need a same-server cooldown, except
                # explicit Retry-After and rate-limit responses.
                if (
                    http_code in {500, 502, 503, 504}
                    and following != endpoint
                    and not getattr(exc, "retry_after", None)
                    and not (getattr(exc, "headers", None) and exc.headers.get("Retry-After"))
                ):
                    delay = min(delay, 1.0)
                print(f"  retrying in {delay:g} seconds (prefetch only)", flush=True)
                time.sleep(delay)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    raise RuntimeError(
        f"Data acquisition failed for {spec.label}: {'; '.join(errors) or str(last_error)}"
    ) from last_error
