"""Pulsar, ATK, VXE and Hitscan wireless mice over USB/HID, without vendor software.

Protocol from andrewrabert/python-pulsar-mouse-tool, which also backs the
"HID: pulsar" driver in review for the Linux kernel and lists these ids:

  * 3554:f508  Pulsar X2 V2 Mini (1 kHz dongle)      3554:f507  the same mouse on the cable
  * 3554:f58f  ATK VXE R1 SE+ (wired)                373b:1085  ATK VXE R1 SE+ (2.4 GHz)
  * 3554:f58a  VXE R1 Pro Max (1 kHz dongle, #87)    3554:f58c  the same mouse on its cable
  * 3710:5406  Pulsar X2 CrazyLight Mini (2.4 GHz)   3710:3414  the same mouse on the cable
  * the Kysona M600 and the VXE Dragonfly R1 Pro use the same protocol (their ids are
    not in the tool, so they are not claimed here).

The cable id (3554:f58c) is claimed too, from the reporter's second report in #87: the
wired mouse lists the same eight collections as the receiver, and the panel below reads it
with the same command 0x04 frame. Both transports are confirmed on that reporter's
hardware - the receiver read the mouse's level, and on the cable the level agreed with
ATK's own panel (hub.atk.pro) and the charging flag followed the cable.

The ATK and Compx builds are also handled by the OpenMouse project's ATK/VXE panel
(@openmouse/protocol, drivers/atk): it lists 3554:f58a for the R1 Pro Max receiver
and 3554:f58c for the same mouse on its cable, and reads the battery with command
0x04, taking the level, the flag and the millivolts from the same three places this
file does. It also opens the collection with usage page 0xFF02 and usage 0x0002,
which is why that one is preferred below.

The same 17-byte framing is in G-Wolves' own web driver (mouse.xyz), which also handles
these Compx-based receivers: its get_Crc() is 0x55 minus the sum of the first fifteen
payload bytes, the result goes in byte 15, and the frame is sent with sendReport(8, ...) -
so the frame on the wire sums to 0x55, the rule this file uses. (Its battery read is for
the G-Wolves protocol; the level offsets here rest on the two sources above.)

The Hitscan Hyperlight speaks the same frame, on the same kind of vendor collection
(ff02:0002): sopparus/hitscan-battery mapped it from USBPcap captures of Hitscan Utility
1.0.2 in both cable (3770:0100) and receiver (3770:0200) mode and reads it with a plain
write()/read(), command 0x04, the level in byte 6, the flag in byte 7, checksum 0x55 minus
the sum. Its notes left byte 8 and byte 9 unresolved; across its captures those read
0x1129 (4393 mV) while charging and 0x1073 (4211 mV) on battery - the big-endian millivolt
field this file already reads. Its own warning also applies: the vendor application's
battery indicator is broken (it showed 100 % while the device answered 75), so the raw
byte is the truth, which is what this file reports.

A mouse that is idle stops answering - the receiver goes quiet a few seconds after the
last movement, and the wired collection does the same while the mouse sits on its
cable - so a silent poll is a normal state and not a device that went away. The last
reading is kept greyed out for as long as the mouse's receiver stays in the USB tree,
however long the mouse sleeps, and it refreshes at the first poll the mouse answers
again; only a receiver that leaves the tree drops the device, icon included. #87
first showed the icon vanishing around the mouse's cable, then that after the mouse's
own deep stop-mode (10-15 idle minutes) the reading took a long time to come back; a
later screen-off report showed that dropping the icon also cost it its tray position
(Windows puts a re-created icon at a new spot), so the greyed value no longer
expires on a timer.

The iRocks M31R (14a5:c009 on its 2.4 GHz receiver, 14a5:c010 on the cable; both read on
real hardware under Windows, against the vendor software) speaks the same frame over a
different transport. Its command collection (ff02:0002, feature report length 17) has no
output report, so the request goes out as a FEATURE report there, and the reply comes back
as an input report on another collection, ff01:0000 (input length 17). The reply header is
0x08 or 0x09 (the mice seen so far answer 0x09, and the checksum counts it), the checksum
must match, and a reply with another command is skipped (0x0a is an event). The level
(byte 6) and the charging flag (byte 7) were checked against the vendor software. Bytes 8-9 are always 0 on
this model - it has no millivolt field, so none is read - and byte 5 (usually 0x02) is
unknown and unused. Every other vendor id keeps the single 0x08 header and the output report.

The M31R receiver answers a power query even while its mouse is off or asleep (from the
level it remembers), so the 2.4 GHz receiver is asked the online command first: request
`08 03 00 ... 4a`, answer `09 03 00 00 00 01 <byte 6> ...`, accepted only with command 0x03,
a valid checksum and byte 2 == 0, and byte 6 == 1 means online. The battery is read only
then; anything else (byte 6 not 1, a bad answer, no answer, a failed send or read) is an
offline or sleeping mouse, and no level is read from it. It is shown like any other device
that stopped answering: the last reading taken while it was online, greyed out ("NN% (last
known value, device asleep)"), until it answers again. Exception, one query: when it is
found offline with no earlier reading (the app started while the mouse was off), the power
query is sent once, after emptying the queue, because a receiver then answers with the level
it remembers (60 % was seen on real hardware); a valid reply of 1..100 % is shown greyed as
the last known value (never as charging), anything else (0, out of range, no reply) leaves
"no link (off or asleep)". That query is not repeated until the mouse has been online
again, and a power frame that merely arrives with the online answer is never used. The
receiver's memory may not be the mouse's last level, and whether the query wakes a sleeping
mouse is not known. The cable is not asked.

Frames are 17 bytes, big-endian, report id 0x08:

    [0] 0x08      header, which is also the report id
    [1] command   0x04 = power details
    [2..15]        arguments, zero for a power query
    [16] checksum 0x55 - the sum of bytes 0..15, mod 256

A power reply carries the level in byte 6, the power flag in byte 7 (the mouse is on
its cable) and millivolts big-endian in bytes 8-9 (not on the M31R). The same endpoint also pushes
events (command 0x0a), so a reply is only accepted when the header and the checksum
both check out, the command is the one we asked for, and the level is 0..100.
"""
from __future__ import annotations

import ctypes
import sys
import time
from typing import Dict, List, Optional, Tuple

import hid

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

CMD_CONNECT = 0x03      # online status (M31R receiver)
CMD_POWER = 0x04
PAYLOAD_HEADER = 0x08
PAYLOAD_LEN = 17
CHECKSUM_BASE = 0x55

LEVEL_INDEX = 6
POWER_INDEX = 7
VOLTAGE_SLICE = (8, 10)

# iRocks M31R (vendor id 0x14A5): the request is a feature report on one collection and the
# reply an input report on another; replies may carry header 0x09 as well as 0x08
IROCKS_VID = 0x14A5
IROCKS_COMMAND_USAGE = (0xFF02, 0x0002)    # feature length 17, no output report
IROCKS_REPLY_USAGE = (0xFF01, 0x0000)      # input length 17
REPLY_HEADERS: Dict[int, Tuple[int, ...]] = {IROCKS_VID: (PAYLOAD_HEADER, 0x09)}
IROCKS_READ_ATTEMPTS = 8        # events and other commands are skipped, so allow a few more
# Only the 2.4 GHz receiver is asked whether its mouse is there: a power reply from it can be
# a stale one (the receiver answers for a mouse that is off or asleep), so the battery is
# read only after command 0x03 says byte 6 == 1. The cable (c010) is not asked.
IROCKS_RECEIVER_PID = 0xC009
ONLINE_INDEX = 6
ONLINE_TIMEOUT_MS = 500

CONTROL_INTERFACE = 1           # the tool reads its 17-byte replies on interface 1
CONTROL_USAGE = (0xFF02, 0x0002)   # the collection the OpenMouse ATK/VXE panel opens


class _HIDP_CAPS(ctypes.Structure):
    """The part of HIDP_CAPS this file needs (same shape as providers/gwolves.py)."""
    _fields_ = [("Usage", ctypes.c_ushort), ("UsagePage", ctypes.c_ushort),
                ("InputReportByteLength", ctypes.c_ushort),
                ("OutputReportByteLength", ctypes.c_ushort),
                ("FeatureReportByteLength", ctypes.c_ushort),
                ("Reserved", ctypes.c_ushort * 17),
                ("NumberLinkCollectionNodes", ctypes.c_ushort),
                ("NumberInputButtonCaps", ctypes.c_ushort),
                ("NumberInputValueCaps", ctypes.c_ushort),
                ("NumberInputDataIndices", ctypes.c_ushort),
                ("NumberOutputButtonCaps", ctypes.c_ushort),
                ("NumberOutputValueCaps", ctypes.c_ushort),
                ("NumberOutputDataIndices", ctypes.c_ushort),
                ("NumberFeatureButtonCaps", ctypes.c_ushort),
                ("NumberFeatureValueCaps", ctypes.c_ushort),
                ("NumberFeatureDataIndices", ctypes.c_ushort)]


def _query_output_length(path) -> Optional[int]:
    """OutputReportByteLength of one HID collection, or None when Windows does not
    say. The handle is opened with no access rights, so nothing is sent to the device."""
    if sys.platform != "win32":
        return None
    try:
        p = path.decode("utf-8", "ignore") if isinstance(path, (bytes, bytearray)) else str(path)
        k32, hidd = ctypes.windll.kernel32, ctypes.windll.hid
        k32.CreateFileW.restype = ctypes.c_void_p
        handle = k32.CreateFileW(p, 0, 3, None, 3, 0, None)   # no access, share r/w, open existing
        if handle in (None, ctypes.c_void_p(-1).value):
            return None
        try:
            pp = ctypes.c_void_p()
            if not hidd.HidD_GetPreparsedData(ctypes.c_void_p(handle), ctypes.byref(pp)):
                return None
            try:
                caps = _HIDP_CAPS()
                if hidd.HidP_GetCaps(pp, ctypes.byref(caps)) != 0x00110000:   # HIDP_STATUS_SUCCESS
                    return None
                return caps.OutputReportByteLength
            finally:
                hidd.HidD_FreePreparsedData(pp)
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:            # a probe must never take the provider down
        return None


_CAPS: Dict[bytes, Optional[int]] = {}      # collection path -> output report length


def output_length(path) -> Optional[int]:
    """Cached _query_output_length. The paths change when a receiver is re-plugged,
    so the cache never outlives the collection it describes."""
    if path not in _CAPS:
        _CAPS[path] = _query_output_length(path)
    return _CAPS[path]


READ_ATTEMPTS = 4
READ_TIMEOUT_MS = 250
FLUSH_TIMEOUT_MS = 30

# vendor id -> product ids
PIDS: Dict[int, Dict[int, str]] = {
    0x3554: {
        0xF508: "Pulsar X2 V2 Mini (wireless)",
        0xF507: "Pulsar X2 V2 Mini (wired)",
        0xF58F: "ATK VXE R1 SE+ (wired)",
        0xF58A: "VXE R1 Pro Max (2.4 GHz)",
        0xF58C: "VXE R1 Pro Max (wired)",
    },
    0x373B: {
        0x1085: "ATK VXE R1 SE+ (2.4 GHz)",
    },
    0x3770: {
        0x0200: "Hitscan Hyperlight (2.4 GHz)",
        0x0100: "Hitscan Hyperlight (wired)",
    },
    0x3710: {
        0x5406: "Pulsar X2 CrazyLight Mini (2.4 GHz)",
        0x3414: "Pulsar X2 CrazyLight Mini (wired)",
    },
    IROCKS_VID: {
        0xC009: "iRocks M31R (2.4 GHz)",
        0xC010: "iRocks M31R (wired)",
    },
}


def checksum(payload: List[int]) -> int:
    return (CHECKSUM_BASE - sum(payload)) % 256


def make_request(command: int = CMD_POWER) -> List[int]:
    frame = [PAYLOAD_HEADER, command] + [0x00] * (PAYLOAD_LEN - 3)
    frame.append(checksum(frame))
    return frame


def parse_power(r, vid: Optional[int] = None) -> Optional[Tuple[int, bool]]:
    """(percent, power flag) from a power reply, or None when this is not one.
    vid picks the headers a reply may carry (REPLY_HEADERS); the default is 0x08 only."""
    if not r or len(r) < PAYLOAD_LEN:
        return None
    frame = list(r[:PAYLOAD_LEN])
    if frame[0] not in REPLY_HEADERS.get(vid, (PAYLOAD_HEADER,)) or frame[1] != CMD_POWER:
        return None
    if frame[PAYLOAD_LEN - 1] != checksum(frame[:PAYLOAD_LEN - 1]):
        return None
    level = frame[LEVEL_INDEX]
    if not 0 <= level <= 100:
        return None
    return level, frame[POWER_INDEX] != 0


def parse_online(r, vid: Optional[int] = None) -> Optional[bool]:
    """Whether the mouse is online, from an M31R receiver's answer to command 0x03, or None
    when this is not one: the header and the checksum must be right, the command 0x03 and
    the status byte (2) zero. Only byte 6 == 1 means online; any other value is offline."""
    if not r or len(r) < PAYLOAD_LEN:
        return None
    frame = list(r[:PAYLOAD_LEN])
    if frame[0] not in REPLY_HEADERS.get(vid, (PAYLOAD_HEADER,)) or frame[1] != CMD_CONNECT:
        return None
    if frame[PAYLOAD_LEN - 1] != checksum(frame[:PAYLOAD_LEN - 1]):
        return None
    if frame[2] != 0:
        return None
    return frame[ONLINE_INDEX] == 1


def voltage_mv(r) -> Optional[int]:
    if not r or len(r) < VOLTAGE_SLICE[1]:
        return None
    return int.from_bytes(bytes(r[VOLTAGE_SLICE[0]:VOLTAGE_SLICE[1]]), "big")


class PulsarProvider(Provider):
    name = "pulsar"

    def __init__(self):
        self._diag: List[str] = []
        # key -> (name, level, charging, when it was last read)
        self._last: Dict[str, Tuple[str, int, bool, float]] = {}
        # receivers (M31R c009) asked for their remembered level during this offline stretch
        self._asked: set = set()
        self._asked_now = False     # set by _query_feature when it sent that one query

    def _pick(self, infos: List[dict]) -> Optional[dict]:
        """The collection to open.

        The ATK/VXE panel of the OpenMouse project opens the collection with usage
        page 0xFF02 and usage 0x0002 and sends its report-0x08 frames there, so that
        one is tried first - the R1 Pro Max dongle has it next to four other
        interface-1 collections, and the first of those is not the one it answers on.
        Without it, the first interface-1 collection is used, which is where the
        reference tool reads its replies.

        A collection whose output report cannot carry the 17-byte frame is skipped
        even when it has the right usage: Windows refuses that write and no reply
        comes, which is indistinguishable from a device that is off. The length is
        only known when Windows says so, and an unknown length never disqualifies a
        collection. The probe is the same read-only HidP_GetCaps query that
        providers/gwolves.py uses for its feature length, cached per collection path.
        (Spotted by ahmedkhursheed23 in #87, from mouse.xyz's writeFile().)
        """
        control = [d for d in infos
                   if (d.get("usage_page"), d.get("usage")) == CONTROL_USAGE]
        for d in control + infos:
            if output_length(d["path"]) in (None, PAYLOAD_LEN):
                if not control:
                    self._diag.append("  no control collection; using "
                                      f"iface={d.get('interface_number')} "
                                      f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x} "
                                      f"(output={output_length(d['path'])})")
                return d
            self._diag.append(f"  {d.get('usage_page', 0):04x}:{d.get('usage', 0):04x} "
                              f"output={output_length(d['path'])} cannot take a "
                              f"{PAYLOAD_LEN}-byte frame; skipped")
        if control:
            return control[0]
        self._diag.append(f"  no interface {CONTROL_INTERFACE} collection; "
                          f"falling back to the first of {len(infos)}")
        return infos[0] if infos else None

    def _read_reply(self, dev, vid: Optional[int] = None, attempts: int = READ_ATTEMPTS,
                    command: int = CMD_POWER) -> Optional[List[int]]:
        """The first reply to `command` (power by default) from dev; events and frames of
        another command are skipped."""
        parse, what, timeout = ((parse_power, "power", READ_TIMEOUT_MS) if command == CMD_POWER
                                else (parse_online, "online", ONLINE_TIMEOUT_MS))
        for attempt in range(attempts):
            r = dev.read(PAYLOAD_LEN, timeout)
            if not r:
                break
            self._diag.append(f"  reply {attempt + 1}: {hexdump(r)}")
            if parse(r, vid) is not None:
                return list(r)
            time.sleep(0.02)
        self._diag.append(f"  no {what} reply (events and short frames are ignored)")
        return None

    def _query(self, path: bytes) -> Optional[List[int]]:
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"  open: {e}")
            return None
        try:
            self._drain(dev)
            dev.write(make_request())
            return self._read_reply(dev)
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"  query error: {e}")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _query_feature(self, infos: List[dict], check_online: bool = False,
                       ask_remembered: bool = False
                       ) -> Tuple[Optional[bool], Optional[List[int]]]:
        """iRocks M31R: the request as a feature report on ff02:0002, the reply read
        from the input reports of ff01:0000. Returns (online, power reply).

        With check_online (the 2.4 GHz receiver) the online command goes first and the
        battery is only asked for when it answers "online": online is then False for
        everything but a valid "byte 6 == 1" answer (bad checksum, byte 2 set, no answer,
        a failed send or read), and no power query is made (but for ask_remembered). online is None when the
        collections could not even be opened, which says nothing about the mouse. Without
        check_online (the cable) online is True or None, as before.

        ask_remembered: when the receiver answers "offline" and there is no earlier reading
        to show, ask once for the level it remembers (see the module docstring); that power
        reply, if any, comes back with online False. It is the only power query ever sent
        to a receiver that said offline."""
        command = next((d for d in infos
                        if (d.get("usage_page"), d.get("usage")) == IROCKS_COMMAND_USAGE), None)
        replies = next((d for d in infos
                        if (d.get("usage_page"), d.get("usage")) == IROCKS_REPLY_USAGE), None)
        if command is None or replies is None:
            self._diag.append("  needs the ff02:0002 and ff01:0000 collections; "
                              f"{'ff02:0002' if command is None else 'ff01:0000'} not found")
            return None, None
        opened = []
        state: Optional[bool] = None    # what a failure from here on means for the mouse
        try:
            for d in (replies, command):
                dev = hid.device()
                try:
                    dev.open_path(d["path"])
                except (OSError, IOError) as e:
                    self._diag.append(f"  open: {e}")
                    return None, None
                opened.append(dev)
            reader, writer = opened
            if check_online:
                state = False       # opened: from here on a failure means offline
            self._drain(reader)
            if check_online:
                n = writer.send_feature_report(make_request(CMD_CONNECT))
                if n is not None and n < 0:
                    self._diag.append("  online query: send refused")
                    return False, None
                answer = self._read_reply(reader, IROCKS_VID, IROCKS_READ_ATTEMPTS, CMD_CONNECT)
                online = parse_online(answer, IROCKS_VID)
                self._diag.append(f"  online: {online if answer else 'no answer'}"
                                  f"{'' if online else ' - treated as offline or asleep'}")
                if not online:
                    if ask_remembered and answer:
                        # a clear "offline" answer: the one power query, after emptying
                        # the queue so that only a reply to it can be read
                        self._drain(reader)
                        self._asked_now = True
                        n = writer.send_feature_report(make_request())
                        if n is not None and n < 0:
                            self._diag.append("  remembered level: send refused")
                            return False, None
                        return False, self._read_reply(reader, IROCKS_VID, IROCKS_READ_ATTEMPTS)
                    return False, None
                state = True        # online: a battery failure is not an offline mouse
                # a power frame the receiver sent along with the answer is not ours
                self._drain(reader)
            n = writer.send_feature_report(make_request())
            if n is not None and n < 0:
                self._diag.append("  send: refused")
                return state, None
            return True, self._read_reply(reader, IROCKS_VID, IROCKS_READ_ATTEMPTS)
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"  query error: {e}")
            return state, None
        finally:
            for dev in opened:
                try:
                    dev.close()
                except Exception:
                    pass

    def _drain(self, dev) -> None:
        for _ in range(4):
            if not dev.read(PAYLOAD_LEN, FLUSH_TIMEOUT_MS):
                return

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        out = []
        now = time.time()
        found: set = set()      # answered this poll
        present: set = set()    # enumerated this poll, answering or not
        for vid, pids in PIDS.items():
            try:
                infos = hidlist.enumerate(vid)
            except Exception as e:  # pragma: no cover
                log.warning("hid.enumerate(%04x): %s", vid, e)
                continue
            for pid in pids:
                mine = [d for d in infos if d["product_id"] == pid]
                if not mine:
                    continue
                key = f"pulsar:{vid:04x}{pid:04x}"
                present.add(key)
                name = pids[pid]
                if vid == IROCKS_VID:
                    self._diag.append(f"[Pulsar] pid={vid:04x}:{pid:04x} '{name}'")
                    receiver = pid == IROCKS_RECEIVER_PID
                    self._asked_now = False
                    online, reply = self._query_feature(
                        mine, receiver, receiver and key not in self._last and key not in self._asked)
                    if self._asked_now:
                        self._asked.add(key)
                    if online is False:
                        # The receiver says its mouse is off or asleep (or would not say).
                        # Its battery is not read: the last reading from while the mouse was
                        # online stays, greyed out, by the loop below ("last known value,
                        # device asleep", like the other devices). With none yet, the
                        # level the receiver remembers (one query) is shown the same way,
                        # when it is a real level; else "no link (off or asleep)".
                        self._diag.append("    mouse offline or asleep")
                        parsed = parse_power(reply, vid)
                        if key not in self._last and parsed is not None and 1 <= parsed[0] <= 100:
                            self._diag.append(f"    remembered by the receiver: {parsed[0]}% "
                                              "(shown as the last known value)")
                            # its charging flag is no longer true, so it is not kept
                            self._last[key] = (name, parsed[0], False, now)
                        elif reply:
                            self._diag.append("    the remembered level is not usable")
                        if key not in self._last:
                            out.append(DeviceStatus(key, name, None, False, False, "pulsar",
                                                    kind="mouse", wake_on_input=True))
                        continue
                else:
                    d = self._pick(mine)
                    if d is None:
                        continue
                    self._diag.append(f"[Pulsar] pid={vid:04x}:{pid:04x} '{name}' "
                                      f"iface={d.get('interface_number')} "
                                      f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}"
                                      f" output={output_length(d['path'])}")
                    reply = self._query(d["path"])
                parsed = parse_power(reply, vid)
                if parsed is None:
                    if not reply:
                        self._diag.append("    no reply (asleep, or the mouse is idle)")
                    continue
                level, on_cable = parsed
                mv = None if vid == IROCKS_VID else voltage_mv(reply)   # the M31R has none
                if mv:
                    self._diag.append(f"  {mv} mV")
                self._last[key] = (name, level, on_cable, now)
                found.add(key)
                out.append(DeviceStatus(key, name, level, on_cable, True, "pulsar",
                                        kind="mouse"))
        self._asked &= present
        # A mouse that stopped answering is not necessarily a mouse that went away
        # (idle it stops answering - see the docstring), so its last reading stays
        # greyed out for as long as its receiver is still in the USB tree, however
        # long it sleeps. Only a receiver pulled out drops the device: Windows
        # re-creates a removed icon at a new tray position, which is what the
        # 2026-10-03 screen-off report was about.
        for key in list(self._last):
            name, level, charging, seen = self._last[key]
            if key in found:
                continue
            if key in present:
                self._diag.append(f"[Pulsar] {name} did not answer; keeping {level}%"
                                  f"{' (charging)' if charging else ''} greyed "
                                  f"(its receiver is still plugged in)")
                out.append(DeviceStatus(key, name, level, charging, False, "pulsar",
                                        kind="mouse", wake_on_input=True))
            else:
                del self._last[key]
        return out

    def diagnostics(self) -> List[str]:
        return list(self._diag)
