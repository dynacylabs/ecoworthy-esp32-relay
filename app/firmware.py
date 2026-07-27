"""Editing esphome-victron-ble/secrets.yaml and running `esphome
compile`/`upload` against victron-mppt.yaml, for the dashboard's Firmware
tab - see README's "Firmware updates" section.

This runs the real `esphome` CLI in-process (as a subprocess) instead of
embedding the official ESPHome dashboard/device-builder in a second
container: that image is ~1.4GB and needs a reverse-proxy sidecar just to
reskin it to match this app's theme, which isn't worth it for a feature
used only occasionally against one known device. The tradeoff that
doesn't go away either way: the ESP-IDF/PlatformIO toolchain still needs
to be downloaded (~1GB) on the very first build - that cost just shifts
from "image pull" to "first build", cached afterwards in the
firmware-cache volume (see docker-compose.yml's PLATFORMIO_CORE_DIR /
ESPHOME_ESP_IDF_PREFIX).

Saving secrets rewrites secrets.yaml from scratch via yaml.safe_dump, so
any comments in that file (e.g. if it still has the ones copied from
secrets.yaml.example) will be lost the first time it's saved from the UI.
"""

import asyncio
import logging
from pathlib import Path

import yaml

logger = logging.getLogger("victron.firmware")

ESPHOME_DIR = Path(__file__).resolve().parent / "esphome-victron-ble"
SECRETS_PATH = ESPHOME_DIR / "secrets.yaml"
CONFIG_FILE = "victron-mppt.yaml"


class SecretField:
    __slots__ = ("key", "label", "help", "secret")

    def __init__(self, key, label, help, secret=False):
        self.key = key
        self.label = label
        self.help = help
        self.secret = secret


# Mirrors secrets.yaml.example - every `!secret` referenced from
# victron-mppt.yaml.
SCHEMA: list[SecretField] = [
    SecretField("wifi_ssid", "WiFi SSID", "Network the ESP32 connects to."),
    SecretField("wifi_password", "WiFi password", "", secret=True),
    SecretField(
        "static_ip", "Static IP",
        "Must match ESPHOME_HOST in docker-compose.yml once flashed.",
    ),
    SecretField("gateway", "Gateway", ""),
    SecretField("subnet", "Subnet mask", ""),
    SecretField(
        "api_encryption_key", "API encryption key",
        "Must match ESPHOME_API_ENCRYPTION_KEY in docker-compose.yml. Generate with: "
        'python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"',
        secret=True,
    ),
    SecretField(
        "ota_password", "OTA password",
        "Generate with: python -c \"import secrets,string; "
        "print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(24)))\"",
        secret=True,
    ),
    SecretField(
        "victron_mac_address", "Victron MAC address",
        "From the VictronConnect app: Settings -> Product info -> Instant readout details.",
    ),
    SecretField(
        "victron_bindkey", "Victron bindkey",
        "32 hex characters, from the same VictronConnect screen as the MAC address.",
        secret=True,
    ),
]

BY_KEY = {f.key: f for f in SCHEMA}


def load_secrets() -> dict:
    if not SECRETS_PATH.exists():
        return {}
    with open(SECRETS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def describe_secrets() -> list[dict]:
    current = load_secrets()
    return [
        {
            "key": f.key,
            "label": f.label,
            "help": f.help,
            "secret": f.secret,
            "value": current.get(f.key, ""),
        }
        for f in SCHEMA
    ]


def save_secrets(updates: dict) -> list[dict]:
    current = load_secrets()
    for key, value in updates.items():
        if key not in BY_KEY or value is None:
            continue
        current[key] = str(value)
    ESPHOME_DIR.mkdir(parents=True, exist_ok=True)
    with open(SECRETS_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(current, f, sort_keys=False)
    return describe_secrets()


class BuildJob:
    """Runs `esphome compile` + `esphome upload` as a subprocess, one at a
    time, streaming combined stdout/stderr to any number of subscribers
    (see GET /api/firmware/stream) - buffered so a browser tab opened or
    reconnected mid-build still sees everything from the start."""

    def __init__(self):
        self.running = False
        self.status = "idle"  # idle | running | success | error
        self.lines: list[str] = []
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for line in self.lines:
            q.put_nowait(line)
        if not self.running:
            q.put_nowait(None)  # sentinel: stream already finished
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)

    def _emit(self, line: str):
        self.lines.append(line)
        for q in list(self._subscribers):
            q.put_nowait(line)

    def _finish(self, status: str):
        self.status = status
        self.running = False
        for q in list(self._subscribers):
            q.put_nowait(None)

    def start(self, host: str):
        if self.running:
            raise RuntimeError("a build is already running")
        self.running = True
        self.status = "running"
        self.lines = []
        asyncio.create_task(self._run(host))

    async def _run(self, host: str):
        try:
            if not SECRETS_PATH.exists():
                self._emit("! secrets.yaml not found - save the fields above first.")
                self._finish("error")
                return
            self._emit(f"$ esphome compile {CONFIG_FILE}")
            if not await self._exec(["esphome", "compile", CONFIG_FILE]):
                self._finish("error")
                return
            self._emit(f"$ esphome upload {CONFIG_FILE} --device {host}")
            ok = await self._exec(["esphome", "upload", CONFIG_FILE, "--device", host])
            self._finish("success" if ok else "error")
        except Exception:
            logger.exception("firmware build failed")
            self._emit("! internal error - see server logs")
            self._finish("error")

    async def _exec(self, cmd: list[str]) -> bool:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=ESPHOME_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        async for raw in proc.stdout:
            self._emit(raw.decode(errors="replace").rstrip("\n"))
        return await proc.wait() == 0


job = BuildJob()
