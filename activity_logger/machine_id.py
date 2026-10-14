"""A stable identifier for this machine, persisted across restarts/updates."""

import os
import platform
import uuid

from . import config


def _id_path() -> str:
    return os.path.join(config.data_dir(), "machine_id.txt")


def get_machine_id() -> str:
    """A short, stable, random id for this installation.

    Deliberately NOT tied to hardware (MAC address, disk serial, Windows
    MachineGuid): those can collide after cloning a disk image and reading
    some of them needs extra privileges. A locally generated id, persisted
    once, is simpler and good enough to key a status/log file per machine.
    """
    path = _id_path()
    try:
        with open(path, encoding="utf-8") as fh:
            existing = fh.read().strip()
            if existing:
                return existing
    except Exception:
        pass

    new_id = uuid.uuid4().hex[:12]
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_id)
    except Exception:
        pass
    return new_id


def get_hostname() -> str:
    try:
        return platform.node() or "unknown-host"
    except Exception:
        return "unknown-host"
