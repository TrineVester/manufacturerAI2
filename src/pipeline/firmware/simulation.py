"""Simulation manager — spawns the simavr C harness and bridges commands/events."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

HARNESS_BINARY = Path(__file__).parent / "harness" / "sim_harness"


class SimulationManager:
    """Manages a running simavr harness subprocess for one session."""

    def __init__(self, session_dir: Path) -> None:
        self._session_dir = session_dir
        self._config_path = session_dir / "sim_config.json"
        self._proc: asyncio.subprocess.Process | None = None
        self._booted = False
        self._boot_error: str | None = None
        self._state: dict[str, dict[str, Any]] = {}
        self._listeners: list[Callable[[dict[str, Any]], Any]] = []
        self._reader_task: asyncio.Task[None] | None = None

    @property
    def booted(self) -> bool:
        return self._booted

    @property
    def is_running(self) -> bool:
        return self._proc is not None

    @property
    def state(self) -> dict[str, dict[str, Any]]:
        return dict(self._state)

    def add_listener(self, cb: Callable[[dict[str, Any]], Any]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[dict[str, Any]], Any]) -> None:
        try:
            self._listeners.remove(cb)
        except ValueError:
            pass

    async def start(self, timeout: float = 5.0) -> None:
        if self._proc is not None:
            return

        self._boot_error = None

        if not self._config_path.exists():
            raise FileNotFoundError("sim_config.json not found")

        cfg = json.loads(self._config_path.read_text(encoding="utf-8"))
        elf_path = cfg.get("elf_path")
        if not elf_path:
            raise ValueError("sim_config.json has no elf_path")

        full_elf = self._session_dir / elf_path
        if not full_elf.exists():
            raise FileNotFoundError(f"ELF not found: {full_elf}")

        harness = _find_harness()
        if harness is None:
            raise FileNotFoundError(
                "sim_harness binary not found — build it with "
                "'make' in src/pipeline/firmware/harness/"
            )

        # Initialise peripheral state from config
        for p in cfg.get("peripherals", []):
            iid = p["instance_id"]
            self._state[iid] = {
                "type": p["type"],
                "pressed": False,
                "on": False,
            }

        self._proc = await asyncio.create_subprocess_exec(
            str(harness), str(self._config_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self._reader_task = asyncio.create_task(self._read_stdout())

        # Wait for boot_ok
        try:
            await asyncio.wait_for(self._wait_boot(), timeout=timeout)
        except asyncio.TimeoutError:
            await self.stop()
            raise TimeoutError("simavr harness did not boot in time")

        if self._boot_error:
            err = self._boot_error
            await self.stop()
            raise RuntimeError(f"Harness error: {err}")

    async def stop(self) -> None:
        if self._proc is None:
            return

        try:
            self._send_cmd({"cmd": "quit"})
        except Exception:
            pass

        try:
            self._proc.terminate()
        except ProcessLookupError:
            pass

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            self._proc.kill()

        self._proc = None
        self._booted = False
        self._boot_error = None
        self._state.clear()

    async def restart(self, timeout: float = 5.0) -> None:
        await self.stop()
        await self.start(timeout=timeout)

    async def press(self, instance_id: str) -> None:
        self._send_cmd({"cmd": "press", "instance_id": instance_id})
        if instance_id in self._state:
            self._state[instance_id]["pressed"] = True

    async def release(self, instance_id: str) -> None:
        self._send_cmd({"cmd": "release", "instance_id": instance_id})
        if instance_id in self._state:
            self._state[instance_id]["pressed"] = False

    def _send_cmd(self, cmd: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        line = json.dumps(cmd) + "\n"
        self._proc.stdin.write(line.encode())

    async def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode().strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._handle_event(event)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Error reading harness stdout")

    def _handle_event(self, event: dict[str, Any]) -> None:
        ev_type = event.get("event")
        if ev_type == "boot_ok":
            self._booted = True
        elif ev_type == "pin_change":
            iid = event.get("instance_id", "")
            on = event.get("on", False)
            if iid in self._state:
                self._state[iid]["on"] = on
        elif ev_type == "serial":
            log.debug("AVR serial: %s", event.get("data", ""))
        elif ev_type == "error":
            log.warning("Harness error: %s", event.get("message"))
            self._boot_error = event.get("message", "unknown harness error")

        for cb in self._listeners:
            try:
                cb(event)
            except Exception:
                log.exception("Listener error")

    async def _wait_boot(self) -> None:
        while not self._booted and not self._boot_error:
            await asyncio.sleep(0.05)


def _find_harness() -> Path | None:
    """Locate the compiled sim_harness binary."""
    if HARNESS_BINARY.exists():
        return HARNESS_BINARY
    # Check with .exe suffix on Windows
    exe = HARNESS_BINARY.with_suffix(".exe")
    if exe.exists():
        return exe
    # Check PATH
    found = shutil.which("sim_harness")
    if found:
        return Path(found)
    return None


# Per-session simulation instances
_simulations: dict[str, SimulationManager] = {}


async def get_or_create_simulation(session_dir: Path, session_id: str) -> SimulationManager:
    if session_id in _simulations:
        mgr = _simulations[session_id]
        if mgr.is_running:
            return mgr
    mgr = SimulationManager(session_dir)
    _simulations[session_id] = mgr
    await mgr.start()
    return mgr


async def stop_simulation(session_id: str) -> None:
    mgr = _simulations.pop(session_id, None)
    if mgr:
        await mgr.stop()
