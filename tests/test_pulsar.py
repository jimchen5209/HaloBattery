"""Tests for providers/pulsar.py. No hardware is needed.

The fake dongle is the VXE R1 Pro Max 1 kHz receiver from issue #87: 3554:f58a,
whose report lists two collections on interface 0 and 2 and six on interface 1,
with the ATK/VXE panel of the OpenMouse project (@openmouse/protocol, drivers/atk)
opening the one with usage page 0xFF02 and usage 0x0002. That panel reads the battery with command
0x04 and takes the level, the flag and the millivolts from the same three places
providers/pulsar.py does, so the fake answers only on that collection - the first
interface-1 collection stays silent, which is what the real dongle does.

The same mouse on its cable (3554:f58c) is the reporter's second report in that issue: it
lists the same eight collections, so the same rule picks ff02:0002 and the same frame
applies. The keep-window tests at the end cover the reporter's later report: after 10-15
idle minutes his mouse deep-sleeps, the receiver goes quiet, and the reading used to
vanish from the tray until long after the mouse was used again.

The Hitscan Hyperlight (3770:0200 on its receiver, 3770:0100 on the cable, #105) is
faked with its own collection list and the frames sopparus/hitscan-battery captured
from the vendor application; the same rule picks its ff02:0002 collection.

The iRocks M31R (14a5:c009 on the receiver, 14a5:c010 on the cable) is faked with the
collections its Windows report lists and the frames that were read from it on real hardware:
the request is a feature report on ff02:0002 and the reply is an input report on ff01:0000.
The receiver is asked the online command (0x03) first and its battery only when it says
the mouse is online. With the mouse off or asleep (the receiver remembers 60 %) the last
reading from while it was online is shown greyed; with none (the app started with the mouse
off) nothing is reported, and no power query is sent while offline.

hidlist.enumerate() and hid.device() are replaced: the real ones ask Windows for
collections of real devices.

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import pulsar as P  # noqa: E402

# (interface, usage page, usage) exactly as the reporter's diagnostics.txt lists them
SHAPE = [
    (0, 0x0001, 0x0006),
    (1, 0xFF05, 0x0000),
    (1, 0xFF03, 0x0000),
    (1, 0x000C, 0x0001),
    (1, 0x0001, 0x0080),
    (1, 0xFF02, 0x0002),
    (1, 0xFF04, 0x0002),
    (2, 0x0001, 0x0002),
]

# the Hitscan Hyperlight receiver (3770:0200) as #105's diagnostics list its collections
HITSCAN_SHAPE = [
    (1, 0xFF04, 0x0002),
    (1, 0xFF05, 0x0000),
    (1, 0xFF03, 0x0000),
    (1, 0x000C, 0x0001),
    (1, 0x0001, 0x0080),
    (1, 0xFF02, 0x0002),
    (2, 0x0001, 0x0002),
]

# the two frames the Pulsar X2 CrazyLight Mini answered on its reporter's hardware
# (#164, both checksums valid): 3710:5406 the dongle, 3710:3414 the cable
CRAZYLIGHT_DONGLE = bytes.fromhex("08 04 00 00 00 02 14 00 0d ea 00 00 00 00 00 00 3c")
CRAZYLIGHT_CABLE = bytes.fromhex("08 04 00 00 00 02 19 01 0e 52 00 00 00 00 00 00 cd")


def reply(level, power=0, mv=0, command=P.CMD_POWER, header=P.PAYLOAD_HEADER,
          break_checksum=False):
    """A 17-byte frame of the shape the dongle sends."""
    frame = [header, command] + [0x00] * 4 + [level, power] + list(mv.to_bytes(2, "big"))
    frame += [0x00] * 6
    assert len(frame) == P.PAYLOAD_LEN - 1
    ck = P.checksum(frame)
    if break_checksum:
        ck ^= 0xFF
    return bytes(frame + [ck])


def with_checksum(frame, changes):
    """frame with some bytes changed ({index: value}) and its checksum made right again."""
    out = list(frame[:P.PAYLOAD_LEN - 1])
    for index, value in changes.items():
        out[index] = value
    return bytes(out + [P.checksum(out)])


class FakeCollection:
    def __init__(self, path, iface, page, usage, replies):
        self.info = {"path": path, "interface_number": iface, "usage_page": page,
                     "usage": usage}
        self.replies = list(replies)
        self.sent = []
        self.opened = 0

    def read(self, length):
        if not self.sent:
            return b""            # nothing asked yet: no stale data either
        return self.replies.pop(0) if self.replies else b""


class FakeBus:
    """The dongle, the way Windows presents it."""

    def __init__(self, vid, pid, replies=(), answering=(0xFF02, 0x0002), shape=SHAPE):
        self.vid = vid
        self.pid = pid
        self.cols = []
        for i, (iface, page, usage) in enumerate(shape):
            path = b"\\\\?\\hid#vid_%04x&pid_%04x#col%02d" % (vid, pid, i)
            mine = replies if (page, usage) == answering else ()
            self.cols.append(FakeCollection(path, iface, page, usage, mine))

    def enumerate(self, vid):
        if vid != self.vid:
            return []
        return [dict(c.info, product_id=self.pid, vendor_id=self.vid) for c in self.cols]

    def written(self):
        """(usage page, usage) of every collection a frame was written to."""
        return [(c.info["usage_page"], c.info["usage"]) for c in self.cols if c.sent]

    def device_class(self):
        bus = self

        class FakeDevice:
            def open_path(self, path):
                self.c = next(c for c in bus.cols if c.info["path"] == path)
                self.c.opened += 1

            def write(self, frame):
                self.c.sent.append(list(frame))

            def read(self, length, timeout_ms=0):
                return self.c.read(length)

            def close(self):
                pass

        return FakeDevice


class PulsarTest(unittest.TestCase):
    def setUp(self):
        self._hid, self._hidlist, self._outlen = P.hid, P.hidlist, P.output_length
        self.bus = self.one_receiver()
        P.hidlist = types.SimpleNamespace(enumerate=self.bus.enumerate)
        P.hid = types.SimpleNamespace(device=self.bus.device_class())

    def tearDown(self):
        P.hid, P.hidlist, P.output_length = self._hid, self._hidlist, self._outlen

    def one_receiver(self, vid=0x3554, pid=0xF58A, replies=(), lengths=None, **kw):
        """lengths: {(usage_page, usage): OutputReportByteLength}. Unknown stays None,
        which is what Windows gives for a collection it will not classify."""
        lengths = dict(lengths or {})
        self.bus = FakeBus(vid, pid, replies, **kw)
        bus = self.bus

        def output_length(path):
            for c in bus.cols:
                if c.info["path"] == path:
                    return lengths.get((c.info["usage_page"], c.info["usage"]))
            return None

        P.output_length = output_length
        self.lengths = lengths
        P.hidlist = types.SimpleNamespace(enumerate=self.bus.enumerate)
        P.hid = types.SimpleNamespace(device=self.bus.device_class())
        return self.bus

    def test_the_r1_pro_max_receiver_is_read(self):
        bus = self.one_receiver(replies=[reply(78, 0, 3900)])
        provider = P.PulsarProvider()
        found = provider.poll()
        self.assertEqual(1, len(found))
        d = found[0]
        self.assertEqual("pulsar:3554f58a", d.key)
        self.assertEqual("VXE R1 Pro Max (2.4 GHz)", d.name)
        self.assertEqual(78, d.level)
        self.assertFalse(d.charging)
        self.assertTrue(d.online)
        self.assertEqual("mouse", d.kind)
        diag = "\n".join(provider.diagnostics())
        self.assertIn("[Pulsar] pid=3554:f58a 'VXE R1 Pro Max (2.4 GHz)'", diag)
        self.assertIn("3900 mV", diag)

    def test_the_r1_pro_max_on_its_cable_is_read(self):
        # the reporter's second report: the wired mouse is 3554:f58c, product string
        # 'VXE R1 PRO MAX', with the same eight collections as the receiver
        bus = self.one_receiver(vid=0x3554, pid=0xF58C, replies=[reply(54, 1, 4020)])
        found = P.PulsarProvider().poll()
        self.assertEqual(1, len(found))
        d = found[0]
        self.assertEqual("pulsar:3554f58c", d.key)
        self.assertEqual("VXE R1 Pro Max (wired)", d.name)
        self.assertEqual(54, d.level)
        self.assertTrue(d.charging)
        self.assertEqual([(0xFF02, 0x0002)], bus.written())

    def test_the_vendor_control_collection_is_the_one_that_gets_the_request(self):
        bus = self.one_receiver(replies=[reply(64)])
        P.PulsarProvider().poll()
        self.assertEqual([(0xFF02, 0x0002)], bus.written(),
                         "the request must go to the collection the OpenMouse ATK/VXE panel opens")

    def test_the_request_is_the_17_byte_power_query(self):
        bus = self.one_receiver(replies=[reply(64)])
        P.PulsarProvider().poll()
        c = next(c for c in bus.cols if c.sent)
        sent, = c.sent
        self.assertEqual(P.make_request(), sent)
        self.assertEqual(17, len(sent))
        self.assertEqual(P.PAYLOAD_HEADER, sent[0])
        self.assertEqual(P.CMD_POWER, sent[1])
        self.assertEqual(P.CHECKSUM_BASE, sum(sent) % 256)

    def test_the_power_flag_means_charging(self):
        self.one_receiver(replies=[reply(55, 1)])
        found = P.PulsarProvider().poll()
        self.assertTrue(found[0].charging)

    def test_a_wrong_checksum_is_refused(self):
        self.one_receiver(replies=[reply(78, 0, 3900, break_checksum=True)])
        provider = P.PulsarProvider()
        self.assertEqual([], provider.poll())
        self.assertIn("no power reply", "\n".join(provider.diagnostics()))

    def test_a_level_above_100_is_refused(self):
        self.one_receiver(replies=[reply(200)])
        self.assertEqual([], P.PulsarProvider().poll())

    def test_an_event_frame_is_skipped_and_the_reply_after_it_is_used(self):
        # the same endpoint pushes events (command 0x0a); they are not a battery reply
        self.one_receiver(replies=[reply(78, command=0x0A),
                                   reply(41),
                                   reply(78, 0, 3900)])
        found = P.PulsarProvider().poll()
        self.assertEqual(41, found[0].level)

    def test_a_short_reply_is_ignored(self):
        bus = self.one_receiver()
        bus.cols[5].replies = [b"\x08\x04\x00"]
        self.assertEqual([], P.PulsarProvider().poll())

    def test_a_sleeping_dongle_gives_no_device(self):
        self.one_receiver(replies=[])
        self.assertEqual([], P.PulsarProvider().poll())

    def test_the_captured_crazylight_frames_parse(self):
        self.assertEqual((20, False), P.parse_power(CRAZYLIGHT_DONGLE))
        self.assertEqual(3562, P.voltage_mv(CRAZYLIGHT_DONGLE))
        self.assertEqual((25, True), P.parse_power(CRAZYLIGHT_CABLE))
        self.assertEqual(3666, P.voltage_mv(CRAZYLIGHT_CABLE))

    def test_the_crazylight_mini_is_read_on_both_transports(self):
        self.one_receiver(vid=0x3710, pid=0x5406, replies=[CRAZYLIGHT_DONGLE])
        found = P.PulsarProvider().poll()
        self.assertEqual(1, len(found))
        self.assertEqual(("pulsar:37105406", "Pulsar X2 CrazyLight Mini (2.4 GHz)", 20, False),
                         (found[0].key, found[0].name, found[0].level, found[0].charging))
        self.one_receiver(vid=0x3710, pid=0x3414, replies=[CRAZYLIGHT_CABLE])
        found = P.PulsarProvider().poll()
        self.assertEqual(("pulsar:37103414", "Pulsar X2 CrazyLight Mini (wired)", 25, True),
                         (found[0].key, found[0].name, found[0].level, found[0].charging))

    def test_every_claimed_id_answers_the_same_request(self):
        for vid, pids in P.PIDS.items():
            if vid == P.IROCKS_VID:
                continue          # its request is a feature report: M31RTest below
            for pid in pids:
                with self.subTest(vid=vid, pid=pid):
                    bus = FakeBus(vid, pid, [reply(33)])
                    P.output_length = lambda path: None
                    P.hidlist = types.SimpleNamespace(enumerate=bus.enumerate)
                    P.hid = types.SimpleNamespace(device=bus.device_class())
                    found = P.PulsarProvider().poll()
                    self.assertEqual(1, len(found), "no reading for %04x:%04x" % (vid, pid))
                    self.assertEqual(33, found[0].level)
                    c = next(c for c in bus.cols if c.sent)
                    self.assertEqual(P.make_request(), c.sent[0])

    def test_the_control_collection_is_used_when_its_output_report_fits(self):
        bus = self.one_receiver(replies=[reply(64)], lengths={(0xFF02, 0x0002): 17})
        P.PulsarProvider().poll()
        self.assertEqual([(0xFF02, 0x0002)], bus.written())

    def test_a_collection_that_cannot_take_the_frame_is_skipped(self):
        # Windows refuses a 17-byte write to a collection that has no such report:
        # the keepalive report on FF02 is 64 bytes, so the frame goes to FF04 instead
        bus = self.one_receiver(replies=[reply(72)], shape=[(1, 0xFF05, 0x0000),
                                                            (1, 0xFF02, 0x0002),
                                                            (1, 0xFF04, 0x0002)],
                                answering=(0xFF04, 0x0002),
                                lengths={(0xFF05, 0x0000): 0, (0xFF02, 0x0002): 64,
                                         (0xFF04, 0x0002): 17})
        provider = P.PulsarProvider()
        found = provider.poll()
        self.assertEqual(72, found[0].level)
        self.assertEqual([(0xFF04, 0x0002)], bus.written())
        diag = "\n".join(provider.diagnostics())
        self.assertIn("ff02:0002 output=64 cannot take a 17-byte frame; skipped", diag)
        self.assertIn("output=17", diag)

    def test_the_request_goes_to_the_first_collection_that_fits_when_there_is_no_control_one(self):
        bus = self.one_receiver(replies=[reply(48)], shape=[(1, 0xFF05, 0x0000),
                                                            (1, 0xFF03, 0x0000)],
                                answering=(0xFF03, 0x0000),
                                lengths={(0xFF05, 0x0000): 8, (0xFF03, 0x0000): 17})
        found = P.PulsarProvider().poll()
        self.assertEqual(48, found[0].level)
        self.assertEqual([(0xFF03, 0x0000)], bus.written())

    def test_an_unknown_output_length_never_disqualifies_a_collection(self):
        # on a system where the caps query says nothing, the usage rule still decides
        self.one_receiver(replies=[reply(64)])
        P.PulsarProvider().poll()
        self.assertEqual([(0xFF02, 0x0002)], self.bus.written())

    def test_an_unknown_length_still_qualifies_when_the_control_collection_is_wrong(self):
        # the control collection says 8 (no 17-byte report); the next one says nothing.
        # Unknown must not disqualify it, or the frame is written to a collection that
        # refuses it and the device looks switched off
        bus = self.one_receiver(replies=[reply(66)],
                                shape=[(1, 0xFF02, 0x0002), (1, 0xFF05, 0x0000)],
                                answering=(0xFF05, 0x0000),
                                lengths={(0xFF02, 0x0002): 8})
        found = P.PulsarProvider().poll()
        self.assertEqual(66, found[0].level)
        self.assertEqual([(0xFF05, 0x0000)], bus.written())

    def test_the_diagnostic_names_the_output_length(self):
        self.one_receiver(replies=[reply(64)], lengths={(0xFF02, 0x0002): 17})
        provider = P.PulsarProvider()
        provider.poll()
        self.assertIn("ff02:0002 output=17", "\n".join(provider.diagnostics()))

    def test_an_interface_1_collection_is_used_when_the_control_one_is_absent(self):
        shape = [(1, 0xFF05, 0x0000), (1, 0x0001, 0x0080)]
        bus = self.one_receiver(replies=[reply(51)], shape=shape,
                                answering=(0xFF05, 0x0000))
        found = P.PulsarProvider().poll()
        self.assertEqual(51, found[0].level)
        self.assertEqual([(0xFF05, 0x0000)], bus.written())

    def test_a_device_that_is_not_in_the_table_is_not_queried(self):
        bus = self.one_receiver(vid=0x3554, pid=0x1234, replies=[reply(78)])
        self.assertEqual([], P.PulsarProvider().poll())
        self.assertEqual([], bus.written())

    def test_the_hitscan_receiver_is_read(self):
        bus = self.one_receiver(vid=0x3770, pid=0x0200, replies=[reply(100, 0, 4393)],
                                shape=HITSCAN_SHAPE)
        found = P.PulsarProvider().poll()
        self.assertEqual(1, len(found))
        self.assertEqual("Hitscan Hyperlight (2.4 GHz)", found[0].name)
        self.assertEqual(100, found[0].level)
        self.assertEqual("pulsar:37700200", found[0].key)
        self.assertEqual([(0xFF02, 0x0002)], bus.written())

    def test_the_hitscan_on_its_cable_is_read(self):
        self.one_receiver(vid=0x3770, pid=0x0100, replies=[reply(74, 1, 4000)],
                          shape=HITSCAN_SHAPE)
        found = P.PulsarProvider().poll()
        self.assertEqual("Hitscan Hyperlight (wired)", found[0].name)
        self.assertEqual(74, found[0].level)

    def test_the_captured_hitscan_frames_parse(self):
        """The literal frames in sopparus/hitscan-battery's NOTES.md, byte 5 being
        0x02 there - which this provider does not check, and must not. The second is
        the one that disproved the vendor app's indicator: the device answered 75
        while Hitscan Utility displayed 100."""
        for raw, level, mv in (("08040000000264001129000000000000a9", 100, 4393),
                               ("0804000000024b00107300000000000079", 75, 4211),
                               ("0804000000026400112d000000000000a5", 100, 4397)):
            frame = bytes.fromhex(raw)
            self.assertEqual((level, False), P.parse_power(frame))
            self.assertEqual(mv, P.voltage_mv(frame))


    # ------------------------------------------------- the idle mouse (#87, later report)

    def test_a_mouse_that_stops_answering_keeps_its_reading_greyed(self):
        self.one_receiver(replies=[reply(64, 1, 4020)])
        provider = P.PulsarProvider()
        with mock.patch.object(P.time, "time", return_value=1000.0):
            first = provider.poll()
        self.assertEqual(1, len(first))
        self.assertTrue(first[0].online)
        self.assertFalse(first[0].wake_on_input, "a fresh reading is not a wake candidate")
        with mock.patch.object(P.time, "time", return_value=1060.0):
            kept = provider.poll()          # idle now: the receiver goes quiet
        self.assertEqual(1, len(kept), "the reading stays while the mouse is enumerated")
        self.assertFalse(kept[0].online, "a held reading is greyed out")
        self.assertEqual((64, True), (kept[0].level, kept[0].charging))
        self.assertEqual("pulsar:3554f58a", kept[0].key)
        self.assertTrue(kept[0].wake_on_input, "the kept reading can be woken by input")

    def test_the_kept_reading_does_not_expire_while_the_receiver_is_present(self):
        # the 2026-10-03 report: a screen-off let the greyed reading expire and the
        # re-created icon came back at a new tray position - it must never expire
        self.one_receiver(replies=[reply(64)])
        provider = P.PulsarProvider()
        with mock.patch.object(P.time, "time", return_value=1000.0):
            self.assertEqual(1, len(provider.poll()))
        with mock.patch.object(P.time, "time", return_value=1000.0 + 3600.0):
            kept = provider.poll()
        self.assertEqual(1, len(kept), "the receiver is still plugged in")
        self.assertFalse(kept[0].online)
        self.assertEqual(64, kept[0].level)

    def test_other_pulsar_devices_are_not_limited_to_300_s(self):
        # the 300 s limit is the M31R receiver's only (c009, ASLEEP_KEEP)
        self.one_receiver(replies=[reply(64)])
        provider = P.PulsarProvider()
        with mock.patch.object(P.time, "time", return_value=1000.0):
            provider.poll()
        for t in (1299.0, 1300.0, 1301.0, 1000.0 + 86400.0):
            with mock.patch.object(P.time, "time", return_value=t):
                kept = provider.poll()
            self.assertEqual([("pulsar:3554f58a", 64, False)],
                             [(k.key, k.level, k.online) for k in kept], t)

    def test_an_unplugged_receiver_is_not_kept(self):
        self.one_receiver(replies=[reply(64)])
        provider = P.PulsarProvider()
        with mock.patch.object(P.time, "time", return_value=1000.0):
            self.assertEqual(1, len(provider.poll()))
        self.bus.cols.clear()               # the receiver is pulled out of the machine
        with mock.patch.object(P.time, "time", return_value=1005.0):
            self.assertEqual([], provider.poll())

    def test_the_diagnostic_says_the_reading_is_kept(self):
        self.one_receiver(replies=[reply(64)])
        provider = P.PulsarProvider()
        with mock.patch.object(P.time, "time", return_value=1000.0):
            provider.poll()
        with mock.patch.object(P.time, "time", return_value=1060.0):
            provider.poll()
        diag = "\n".join(provider.diagnostics())
        self.assertIn("did not answer", diag)
        self.assertIn("greyed", diag)


# ------------------------------------------------------------------ iRocks M31R

# the 17-byte frames read from the mouse on real hardware under Windows
M31R_55 = bytes.fromhex("09 04 00 00 00 02 37 00 00 00 00 00 00 00 00 00 0f")     # 55 %, not charging
M31R_75_CHARGING = bytes.fromhex("09 04 00 00 00 02 4b 01 00 00 00 00 00 00 00 00 fa")
M31R_70_WIRED = bytes.fromhex("09 04 00 00 00 02 46 01 00 00 00 00 00 00 00 00 ff")
M31R_PAIR_REPLY = bytes.fromhex("09 03 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00")
M31R_REQUEST = bytes.fromhex("08 04 00 00 00 00 00 00 00 00 00 00 00 00 00 00 49")
# the online command (0x03) and the receiver's answers to it, read from the mouse on real
# hardware: byte 6 is 1 with the mouse awake and 0 with it off or asleep
M31R_ONLINE_REQUEST = bytes.fromhex("08 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 4a")
M31R_ONLINE = bytes.fromhex("09 03 00 00 00 01 01 00 00 00 00 00 00 00 00 00 47")
M31R_OFFLINE = bytes.fromhex("09 03 00 00 00 01 00 00 00 00 00 00 00 00 00 00 48")
# a power frame (60 %) the receiver sends 309 ms after the answer to the online command
# while the mouse sleeps: the receiver's stale copy of an old reading
M31R_STALE_60 = bytes.fromhex("09 04 00 00 00 02 3c 00 00 00 00 00 00 00 00 00 0a")
# What the receiver answers to a power query with the mouse switched off, or asleep: the level
# it remembers (60 %), as in the frame above: the real captures read 60 % in both states; the
# two names are that frame, built from the shape of the real replies.
M31R_OFF_60 = M31R_SLEEP_60 = M31R_STALE_60

# the collections of the receiver, plus the keyboard and the mouse that must never be opened
M31R_SHAPE = [
    (1, 0xFF02, 0x0002),    # commands: feature report of 17 bytes
    (1, 0xFF01, 0x0000),    # replies: input report of 17 bytes
    (1, 0xFF03, 0x0000),
    (1, 0xFF04, 0x0002),
    (1, 0x000C, 0x0001),
    (1, 0x0001, 0x0080),
    (0, 0x0001, 0x0006),
    (2, 0x0001, 0x0002),
]
M31R_RECEIVER, M31R_CABLE = 0xC009, 0xC010


class FakeM31R:
    """One or two M31R on the bus. A valid power request (a feature report on ff02:0002)
    is answered by queueing that device's script on its ff01:0000 collection, a valid online
    request (0x03) by queueing its `online` script (the mouse is online by default); a frame
    with a bad checksum is ignored, and the command collection has no output report."""

    def __init__(self, scripts):
        self.scripts = {pid: list(frames) for pid, frames in scripts.items()}
        self.online = {pid: [M31R_ONLINE] for pid in scripts}
        self.refuse = set()       # commands whose feature report is refused (send returns -1)
        self.reads_fail = False   # reading the reply collection raises
        self.queues = {pid: [] for pid in scripts}
        self.cols = {}            # path -> (pid, usage page, usage)
        self.sent = []            # (pid, frame) for every feature report
        self.opened = []          # (pid, usage page, usage)
        self.written = []         # output reports (there is no output report to take them)
        self.refuse_feature = False
        for pid in scripts:
            for i, (iface, page, usage) in enumerate(M31R_SHAPE):
                path = b"\\\\?\\hid#vid_14a5&pid_%04x#col%02d" % (pid, i)
                self.cols[path] = (pid, page, usage)

    def enumerate(self, vid):
        if vid != P.IROCKS_VID:
            return []
        out = []
        for path, (pid, page, usage) in self.cols.items():
            iface = next(i for i, p, u in M31R_SHAPE if (p, u) == (page, usage))
            out.append({"path": path, "interface_number": iface, "usage_page": page,
                        "usage": usage, "product_id": pid, "vendor_id": vid})
        return out

    def device_class(self):
        bus = self

        class FakeDevice:
            def open_path(self, path):
                self.pid, self.page, self.usage = bus.cols[path]
                bus.opened.append(bus.cols[path])

            def send_feature_report(self, frame):
                if (self.page, self.usage) != (0xFF02, 0x0002):
                    raise OSError("no feature report here")
                frame = list(frame)
                bus.sent.append((self.pid, frame))
                if bus.refuse_feature or (len(frame) > 1 and frame[1] in bus.refuse):
                    return -1
                if len(frame) == 17 and sum(frame) & 0xFF == 0x55:
                    if frame[1] == 0x04:
                        bus.queues[self.pid].extend(bus.scripts[self.pid])
                    elif frame[1] == 0x03:
                        bus.queues[self.pid].extend(bus.online[self.pid])
                return len(frame)

            def get_feature_report(self, report_id, length):
                return bytes(length)          # all zeros: no use as a reply

            def write(self, frame):
                bus.written.append(list(frame))
                raise OSError("write error")  # the command collection has no output report

            def read(self, length, timeout_ms=0):
                if (self.page, self.usage) == (0xFF02, 0x0002):
                    raise OSError("read error")
                if (self.page, self.usage) != (0xFF01, 0x0000):
                    return b""
                if bus.reads_fail and bus.sent:      # a read fails once a query went out
                    raise OSError("read error")
                queue = bus.queues[self.pid]
                return queue.pop(0) if queue else b""

            def close(self):
                pass

        return FakeDevice


class M31RTest(unittest.TestCase):
    def setUp(self):
        self._hid, self._hidlist = P.hid, P.hidlist

    def tearDown(self):
        P.hid, P.hidlist = self._hid, self._hidlist

    def bus(self, scripts):
        bus = FakeM31R(scripts)
        P.hidlist = types.SimpleNamespace(enumerate=bus.enumerate)
        P.hid = types.SimpleNamespace(device=bus.device_class())
        return bus

    def poll(self, scripts):
        bus = self.bus(scripts)
        provider = P.PulsarProvider()
        return provider.poll(), bus, provider

    def test_the_real_frames_parse(self):
        self.assertEqual((55, False), P.parse_power(M31R_55, P.IROCKS_VID))
        self.assertEqual((75, True), P.parse_power(M31R_75_CHARGING, P.IROCKS_VID))
        self.assertEqual((70, True), P.parse_power(M31R_70_WIRED, P.IROCKS_VID))

    def test_the_receiver_is_read_55_percent_not_charging(self):
        found, bus, _ = self.poll({M31R_RECEIVER: [M31R_55]})
        self.assertEqual(1, len(found))
        d = found[0]
        self.assertEqual(("pulsar:14a5c009", "iRocks M31R (2.4 GHz)", 55, False, True, "mouse"),
                         (d.key, d.name, d.level, d.charging, d.online, d.kind))

    def test_a_charging_mouse_is_read_75_percent(self):
        found, _, _ = self.poll({M31R_RECEIVER: [M31R_75_CHARGING]})
        self.assertEqual((75, True), (found[0].level, found[0].charging))

    def test_the_cable_is_read_70_percent_under_its_own_name(self):
        found, _, _ = self.poll({M31R_CABLE: [M31R_70_WIRED]})
        self.assertEqual(1, len(found))
        d = found[0]
        self.assertEqual(("pulsar:14a5c010", "iRocks M31R (wired)", 70, True),
                         (d.key, d.name, d.level, d.charging))

    def test_both_ids_are_claimed_with_their_own_names(self):
        self.assertEqual({0xC009: "iRocks M31R (2.4 GHz)", 0xC010: "iRocks M31R (wired)"},
                         P.PIDS[0x14A5])

    def test_the_receiver_and_the_cable_together_are_two_devices(self):
        found, bus, _ = self.poll({M31R_RECEIVER: [M31R_70_WIRED],
                                   M31R_CABLE: [M31R_70_WIRED]})
        self.assertEqual(2, len(found))
        self.assertEqual({"pulsar:14a5c009", "pulsar:14a5c010"}, {d.key for d in found})
        self.assertEqual({"iRocks M31R (2.4 GHz)", "iRocks M31R (wired)"}, {d.name for d in found})
        self.assertEqual({70}, {d.level for d in found})
        self.assertEqual({M31R_RECEIVER, M31R_CABLE}, {pid for pid, _ in bus.sent})

    def test_the_request_is_a_feature_report_on_the_command_collection(self):
        found, bus, _ = self.poll({M31R_RECEIVER: [M31R_55]})
        self.assertEqual([(M31R_RECEIVER, list(M31R_ONLINE_REQUEST)),
                          (M31R_RECEIVER, list(M31R_REQUEST))], bus.sent)
        self.assertEqual(17, len(M31R_REQUEST))
        self.assertEqual(0x49, 0x55 - 0x08 - 0x04)
        self.assertEqual(0x4A, 0x55 - 0x08 - 0x03)
        self.assertEqual(list(M31R_REQUEST), P.make_request())
        self.assertEqual(list(M31R_ONLINE_REQUEST), P.make_request(P.CMD_CONNECT))
        self.assertEqual([], bus.written, "there is no output report to write to")

    def test_only_the_command_and_the_reply_collections_are_opened(self):
        _, bus, _ = self.poll({M31R_RECEIVER: [M31R_55]})
        self.assertEqual({(M31R_RECEIVER, 0xFF02, 0x0002), (M31R_RECEIVER, 0xFF01, 0x0000)},
                         set(bus.opened))

    def test_a_reply_with_a_bad_checksum_is_refused(self):
        bad = bytearray(M31R_55)
        bad[16] ^= 0xFF
        found, _, _ = self.poll({M31R_RECEIVER: [bytes(bad)]})
        self.assertEqual([], found)

    def test_a_bad_checksum_is_skipped_and_the_good_reply_after_it_is_used(self):
        bad = bytearray(M31R_55)
        bad[16] ^= 0x01
        found, _, _ = self.poll({M31R_RECEIVER: [bytes(bad), M31R_75_CHARGING]})
        self.assertEqual((75, True), (found[0].level, found[0].charging))

    def test_header_0x08_and_0x09_are_both_accepted(self):
        for header in (0x08, 0x09):
            with self.subTest(header=header):
                found, _, _ = self.poll({M31R_RECEIVER: [reply(42, 1, header=header)]})
                self.assertEqual(1, len(found))
                self.assertEqual((42, True), (found[0].level, found[0].charging))

    def test_another_header_is_refused(self):
        found, _, _ = self.poll({M31R_RECEIVER: [reply(42, header=0x0A)]})
        self.assertEqual([], found)

    def test_the_pairing_reply_and_events_are_ignored_and_reading_goes_on(self):
        frames = [M31R_PAIR_REPLY,
                  reply(11, command=0x0A, header=0x09),
                  reply(12, command=0x03, header=0x09),
                  M31R_55]
        found, _, _ = self.poll({M31R_RECEIVER: frames})
        self.assertEqual(1, len(found))
        self.assertEqual((55, False), (found[0].level, found[0].charging))

    def test_only_those_other_replies_give_no_device(self):
        found, _, _ = self.poll({M31R_RECEIVER: [M31R_PAIR_REPLY, reply(30, command=0x0A,
                                                                         header=0x09)]})
        self.assertEqual([], found)

    def test_an_all_zero_reply_is_not_a_reading(self):
        found, _, _ = self.poll({M31R_RECEIVER: [bytes(17)]})
        self.assertEqual([], found)

    def test_a_level_above_100_is_refused(self):
        found, _, _ = self.poll({M31R_RECEIVER: [reply(101, header=0x09)]})
        self.assertEqual([], found)

    def test_the_receiver_is_asked_online_first_then_for_the_battery(self):
        _, bus, _ = self.poll({M31R_RECEIVER: [M31R_55]})
        self.assertEqual([0x03, 0x04], [frame[1] for _, frame in bus.sent])

    def test_the_cable_is_not_asked_whether_it_is_online(self):
        found, bus, _ = self.poll({M31R_CABLE: [M31R_70_WIRED]})
        self.assertEqual([(M31R_CABLE, 0x04)], [(pid, frame[1]) for pid, frame in bus.sent])
        self.assertEqual(70, found[0].level)

    def test_there_is_no_millivolt_reading_for_this_model(self):
        found, _, provider = self.poll({M31R_RECEIVER: [reply(55, 0, mv=3900, header=0x09)]})
        self.assertEqual(55, found[0].level)
        self.assertNotIn("mV", "\n".join(provider.diagnostics()))

    def test_a_refused_feature_report_gives_no_device_for_the_cable(self):
        bus = self.bus({M31R_CABLE: [M31R_70_WIRED]})
        bus.refuse_feature = True
        self.assertEqual([], P.PulsarProvider().poll())

    def test_a_missing_reply_collection_gives_no_device_and_sends_nothing(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55], M31R_CABLE: [M31R_70_WIRED]})
        gone = [p for p, (pid, page, usage) in bus.cols.items() if (page, usage) == (0xFF01, 0)]
        for path in gone:
            del bus.cols[path]
        self.assertEqual([], P.PulsarProvider().poll())
        self.assertEqual([], bus.sent)

    def test_a_silent_cable_is_not_kept_greyed_it_reports_nothing(self):
        bus = self.bus({M31R_CABLE: [M31R_70_WIRED]})
        provider = P.PulsarProvider()
        self.assertTrue(provider.poll()[0].online)
        bus.scripts[M31R_CABLE] = []                # no answer now
        self.assertEqual([], provider.poll())

    def test_an_online_receiver_that_does_not_answer_the_battery_query_keeps_the_greyed_reading(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        provider = P.PulsarProvider()
        self.assertTrue(provider.poll()[0].online)
        bus.scripts[M31R_RECEIVER] = []             # still online, but no power reply now
        kept = provider.poll()
        self.assertEqual([0x03, 0x04, 0x03, 0x04], [f[1] for _, f in bus.sent])
        self.assertEqual(1, len(kept))
        self.assertFalse(kept[0].online)
        self.assertEqual(55, kept[0].level)

    # ---- the online check (command 0x03), 2.4 GHz receiver only

    def test_the_real_online_frames_parse(self):
        self.assertIs(True, P.parse_online(M31R_ONLINE, P.IROCKS_VID))
        self.assertIs(False, P.parse_online(M31R_OFFLINE, P.IROCKS_VID))
        self.assertIsNone(P.parse_online(M31R_55, P.IROCKS_VID))      # a power frame
        self.assertIsNone(P.parse_online(bytes(17), P.IROCKS_VID))
        self.assertIsNone(P.parse_online(M31R_ONLINE[:16], P.IROCKS_VID))
        self.assertIsNone(P.parse_online(None, P.IROCKS_VID))

    def assertGreyed(self, found, level, charging=False, key="pulsar:14a5c009"):
        """One entry: a last known level, not online - "NN% (last known value, device
        asleep)" in the tray - that new input can refresh."""
        self.assertEqual(1, len(found), found)
        d = found[0]
        self.assertEqual((key, level, charging, False, "mouse", True),
                         (d.key, d.level, d.charging, d.online, d.kind, d.wake_on_input))

    def commands(self, bus):
        return [f[1] for _, f in bus.sent]

    def test_an_offline_mouse_keeps_its_last_online_reading_greyed_and_is_not_asked(self):
        # off and asleep both answer the online query with byte 6 == 0; the receiver would
        # answer a power query with its remembered 60 % (and may send that frame unasked)
        for what, answer in (("off", [M31R_OFFLINE]),
                             ("asleep", [M31R_OFFLINE, M31R_SLEEP_60]),
                             ("asleep, frame first", [M31R_SLEEP_60, M31R_OFFLINE])):
            with self.subTest(what=what):
                bus = self.bus({M31R_RECEIVER: [M31R_55]})
                provider = P.PulsarProvider()
                self.assertEqual(55, provider.poll()[0].level)       # online: 55 %
                del bus.sent[:]
                bus.online[M31R_RECEIVER] = answer
                bus.scripts[M31R_RECEIVER] = [M31R_OFF_60]
                self.assertGreyed(provider.poll(), 55)               # not the receiver's 60
                self.assertGreyed(provider.poll(), 55)
                self.assertEqual([0x03, 0x03], self.commands(bus), "no power query offline")

    def test_a_charging_reading_is_kept_as_it_was_read(self):
        bus = self.bus({M31R_RECEIVER: [M31R_75_CHARGING]})
        provider = P.PulsarProvider()
        self.assertEqual((75, True), (provider.poll()[0].level, provider.poll()[0].charging))
        bus.online[M31R_RECEIVER] = [M31R_OFFLINE]
        self.assertGreyed(provider.poll(), 75, charging=True)

    def test_an_offline_start_shows_nothing_and_never_sends_a_power_query(self):
        # the app started with the mouse off or asleep: nothing to show yet, as for the other
        # devices. The receiver would answer a power query with the 60 % it remembers (and may
        # send that frame unasked), but it is neither asked nor read.
        for what, answer in (("off", [M31R_OFFLINE]),
                             ("asleep", [M31R_OFFLINE, M31R_SLEEP_60]),
                             ("asleep, frame first", [M31R_SLEEP_60, M31R_OFFLINE])):
            with self.subTest(what=what):
                bus = self.bus({M31R_RECEIVER: [M31R_OFF_60, M31R_75_CHARGING]})
                bus.online[M31R_RECEIVER] = answer
                provider = P.PulsarProvider()
                for _ in range(3):
                    self.assertEqual([], provider.poll())
                self.assertEqual([0x03, 0x03, 0x03], self.commands(bus))

    def test_started_offline_then_online_then_offline_again(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        bus.online[M31R_RECEIVER] = [M31R_OFFLINE]
        provider = P.PulsarProvider()
        self.assertEqual([], provider.poll())
        bus.online[M31R_RECEIVER] = [M31R_ONLINE]
        live = provider.poll()
        self.assertEqual((55, False, True), (live[0].level, live[0].charging, live[0].online))
        bus.online[M31R_RECEIVER] = [M31R_OFFLINE, M31R_SLEEP_60]
        bus.scripts[M31R_RECEIVER] = [M31R_OFF_60]
        self.assertGreyed(provider.poll(), 55)           # the live 55, not the remembered 60

    def test_a_pulled_and_replugged_receiver_that_is_offline_shows_nothing(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        provider = P.PulsarProvider()
        self.assertEqual(55, provider.poll()[0].level)
        bus.online[M31R_RECEIVER] = [M31R_OFFLINE]
        self.assertGreyed(provider.poll(), 55)
        saved = dict(bus.cols)
        bus.cols.clear()                                 # the receiver is pulled out
        self.assertEqual([], provider.poll())
        self.assertNotIn("pulsar:14a5c009", provider._last)
        bus.cols.update(saved)                           # and plugged in again, mouse off
        del bus.sent[:]
        self.assertEqual([], provider.poll())
        self.assertEqual([0x03], self.commands(bus))
        bus.online[M31R_RECEIVER] = [M31R_ONLINE]        # the mouse comes on
        self.assertEqual((55, True), (provider.poll()[0].level, True))

    # ---- the 300 s limit of the greyed M31R receiver reading (ASLEEP_KEEP, as the other providers)

    def at(self, now):
        return mock.patch.object(P.time, "time", return_value=now)

    def start_online(self, level_frame=None, at=1000.0):
        """A receiver read online at `at` (55 %), then its mouse switched off."""
        bus = self.bus({M31R_RECEIVER: [level_frame or M31R_55]})
        provider = P.PulsarProvider()
        with self.at(at):
            self.assertEqual(55, provider.poll()[0].level)
        bus.online[M31R_RECEIVER] = [M31R_OFFLINE]
        return bus, provider

    def test_the_limit_is_the_same_300_s_as_the_other_providers(self):
        from providers import wlmouse, lamzu, gwolves, razer, nintendo
        for module in (wlmouse, lamzu, gwolves, razer, nintendo):
            self.assertEqual(module.ASLEEP_KEEP, P.ASLEEP_KEEP, module.__name__)
        self.assertEqual(300, P.ASLEEP_KEEP)

    def test_the_greyed_reading_shows_at_299_s_and_not_at_300_or_301_s(self):
        for when, shown in ((1.0, True), (299.0, True), (299.9, True), (300.0, False),
                            (301.0, False), (3600.0, False)):
            with self.subTest(seconds_after_the_reading=when):
                bus, provider = self.start_online(at=1000.0)
                with self.at(1000.0 + when):
                    found = provider.poll()
                if shown:
                    self.assertGreyed(found, 55)
                else:
                    self.assertEqual([], found)
                    self.assertNotIn("pulsar:14a5c009", provider._last)
                self.assertEqual([0x03], self.commands(bus)[-1:], "offline: only 0x03, never 0x04")

    def test_offline_polls_do_not_renew_the_time(self):
        bus, provider = self.start_online(at=1000.0)
        for t in (1100.0, 1200.0, 1290.0, 1299.0):      # offline, one poll after another
            with self.at(t):
                self.assertGreyed(provider.poll(), 55)
        with self.at(1301.0):                           # 301 s after the reading, 2 s after a poll
            self.assertEqual([], provider.poll())

    def test_after_the_limit_it_is_like_a_start_with_the_mouse_off(self):
        bus, provider = self.start_online(at=1000.0)
        with self.at(1301.0):
            self.assertEqual([], provider.poll())
        self.assertIn("no online reading for 300 s", "\n".join(provider.diagnostics()))
        with self.at(1302.0):                           # and it stays so, even right after
            self.assertEqual([], provider.poll())
        bus.online[M31R_RECEIVER] = [M31R_ONLINE]       # the mouse comes back: live again
        with self.at(1400.0):
            live = provider.poll()
        self.assertEqual((55, True), (live[0].level, live[0].online))

    def test_a_new_online_reading_starts_the_time_again(self):
        bus, provider = self.start_online(at=1000.0)
        with self.at(1250.0):
            self.assertGreyed(provider.poll(), 55)
        bus.online[M31R_RECEIVER] = [M31R_ONLINE]
        bus.scripts[M31R_RECEIVER] = [M31R_75_CHARGING]
        with self.at(1280.0):                           # the mouse is back, 75 %
            live = provider.poll()
        self.assertEqual((75, True), (live[0].level, live[0].online))
        bus.online[M31R_RECEIVER] = [M31R_OFFLINE]
        with self.at(1578.0):                           # 298 s after the new reading
            self.assertGreyed(provider.poll(), 75, charging=True)
        with self.at(1581.0):                           # 301 s after it (331 s after the first)
            self.assertEqual([], provider.poll())

    def test_an_online_receiver_that_does_not_answer_power_also_expires_at_300_s(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        provider = P.PulsarProvider()
        with self.at(1000.0):
            provider.poll()
        bus.scripts[M31R_RECEIVER] = []                 # online, but the power query is not answered
        with self.at(1299.0):
            self.assertGreyed(provider.poll(), 55)
        with self.at(1301.0):
            self.assertEqual([], provider.poll())

    def test_a_receiver_pulled_out_is_forgotten_within_the_300_s_too(self):
        bus, provider = self.start_online(at=1000.0)
        saved = dict(bus.cols)
        bus.cols.clear()
        with self.at(1100.0):
            self.assertEqual([], provider.poll())
        bus.cols.update(saved)                          # plugged in again, mouse still off
        with self.at(1110.0):                           # well inside 300 s of the old reading
            self.assertEqual([], provider.poll())

    # ---- the cable (c010) has no offline state: online with a good reading, nothing otherwise

    CABLE_KEY = "pulsar:14a5c010"

    def start_cable(self, at=1000.0):
        """The cable read at `at` (70 %, charging); from now on it does not answer."""
        bus = self.bus({M31R_CABLE: [M31R_70_WIRED]})
        provider = P.PulsarProvider()
        with self.at(at):
            live = provider.poll()
        self.assertEqual((70, True, True), (live[0].level, live[0].charging, live[0].online))
        bus.scripts[M31R_CABLE] = []
        return bus, provider

    def cable_failures(self):
        """Every way a poll of a plugged-in cable gets no good power reply."""
        broken = bytearray(M31R_70_WIRED); broken[16] ^= 0xFF           # bad checksum
        too_high = bytearray(M31R_70_WIRED); too_high[6] = 101           # level out of range
        return (
            ("silent", lambda b: b.scripts.update({M31R_CABLE: []})),
            ("bad checksum", lambda b: b.scripts.update({M31R_CABLE: [bytes(broken)]})),
            ("level above 100", lambda b: b.scripts.update({M31R_CABLE: [bytes(too_high)]})),
            ("another command's reply", lambda b: b.scripts.update({M31R_CABLE: [M31R_ONLINE]})),
            ("send refused", lambda b: b.refuse.add(0x04)),
            ("feature report refused", lambda b: setattr(b, "refuse_feature", True)),
            ("reads fail", lambda b: setattr(b, "reads_fail", True)))

    def test_the_m31r_ids_are_split_between_the_300_s_limit_and_no_offline_state(self):
        self.assertEqual({"pulsar:14a5c009"}, set(P.IROCKS_LIMITED_KEYS))
        self.assertEqual({"pulsar:14a5c010"}, set(P.IROCKS_NO_OFFLINE_KEYS))

    def test_a_good_cable_reading_is_online_and_is_not_remembered(self):
        bus, provider = self.start_cable(at=1000.0)
        self.assertNotIn(self.CABLE_KEY, provider._last)
        self.assertEqual({}, provider._last)

    def test_a_cable_that_stops_answering_reports_nothing_at_any_time(self):
        for when in (1.0, 60.0, 299.0, 300.0, 301.0, 3600.0, 86400.0):
            with self.subTest(seconds_after_the_reading=when):
                bus, provider = self.start_cable(at=1000.0)
                with self.at(1000.0 + when):
                    self.assertEqual([], provider.poll())
                self.assertEqual({}, provider._last)

    def test_no_failure_of_the_cable_read_ever_gives_online_false(self):
        for what, setup in self.cable_failures():
            with self.subTest(what=what):
                bus, provider = self.start_cable(at=1000.0)
                setup(bus)
                for t in (1005.0, 1200.0, 1299.0, 1301.0, 9000.0):
                    with self.at(t):
                        found = provider.poll()
                    self.assertEqual([], found, (what, t))
                    self.assertEqual({}, provider._last)
                self.assertIn("nothing reported", "\n".join(provider.diagnostics()))

    def test_the_cable_is_never_asked_online_and_is_asked_for_power_each_poll(self):
        bus, provider = self.start_cable(at=1000.0)
        del bus.sent[:]
        with self.at(1100.0):
            provider.poll()
        self.assertEqual([0x04], self.commands(bus))

    def test_a_cable_whose_collections_vanish_but_stays_listed_reports_nothing(self):
        bus, provider = self.start_cable(at=1000.0)
        for path, (pid, page, usage) in list(bus.cols.items()):
            if (page, usage) in (P.IROCKS_COMMAND_USAGE, P.IROCKS_REPLY_USAGE):
                bus.cols[path] = (pid, 0x0001, 0x0080)
        for t in (1100.0, 1299.0, 1301.0):
            with self.at(t):
                self.assertEqual([], provider.poll())
        self.assertEqual({}, provider._last)

    def test_the_cable_is_online_again_as_soon_as_it_answers(self):
        bus, provider = self.start_cable(at=1000.0)
        for t in (1100.0, 1500.0):
            with self.at(t):
                self.assertEqual([], provider.poll())
        bus.scripts[M31R_CABLE] = [M31R_55]              # answers again (55 %, not charging)
        with self.at(1510.0):
            live = provider.poll()
        self.assertEqual([("pulsar:14a5c010", 55, False, True)],
                         [(f.key, f.level, f.charging, f.online) for f in live])
        bus.scripts[M31R_CABLE] = []
        with self.at(1520.0):                            # and silent again: nothing, no grey
            self.assertEqual([], provider.poll())
        self.assertEqual({}, provider._last)

    def test_a_cable_pulled_out_reports_nothing_and_comes_back_when_it_answers(self):
        bus, provider = self.start_cable(at=1000.0)
        bus.scripts[M31R_CABLE] = [M31R_70_WIRED]
        saved = dict(bus.cols)
        bus.cols.clear()
        with self.at(1100.0):
            self.assertEqual([], provider.poll())
        bus.cols.update(saved)
        with self.at(1110.0):
            self.assertEqual([("pulsar:14a5c010", 70, True)],
                             [(f.key, f.level, f.online) for f in provider.poll()])

    def test_the_receiver_keeps_its_v6_300_s_and_the_cable_has_none(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55], M31R_CABLE: [M31R_70_WIRED]})
        provider = P.PulsarProvider()
        with self.at(1000.0):
            self.assertEqual(2, len(provider.poll()))
        bus.online[M31R_RECEIVER] = [M31R_OFFLINE]
        bus.scripts[M31R_CABLE] = []
        with self.at(1200.0):                            # only the receiver is greyed
            self.assertGreyed(provider.poll(), 55)
        bus.scripts[M31R_CABLE] = [M31R_70_WIRED]        # the cable answers again at 1250
        with self.at(1250.0):
            found = {f.key: f for f in provider.poll()}
        self.assertEqual((55, False), (found["pulsar:14a5c009"].level, found["pulsar:14a5c009"].online))
        self.assertEqual((70, True), (found["pulsar:14a5c010"].level, found["pulsar:14a5c010"].online))
        bus.scripts[M31R_CABLE] = []
        with self.at(1299.0):                            # receiver 299 s: still greyed; cable: nothing
            self.assertGreyed(provider.poll(), 55)
        with self.at(1301.0):                            # receiver 301 s: gone, as in v6
            self.assertEqual([], provider.poll())

    def test_a_silent_cable_does_not_change_other_pulsar_devices_in_the_same_poll(self):
        # a Pulsar receiver on another vendor id next to a silent M31R cable: it keeps its
        # greyed reading as long as it is plugged in, the cable reports nothing
        provider = P.PulsarProvider()
        bus = self.bus({M31R_CABLE: [M31R_70_WIRED]})
        other = {"path": b"other", "product_id": 0xF58A, "vendor_id": 0x3554,
                 "interface_number": 1, "usage_page": 0xFF02, "usage": 0x0002}
        m31r_enumerate = bus.enumerate
        P.hidlist = types.SimpleNamespace(
            enumerate=lambda vid: m31r_enumerate(vid) if vid == P.IROCKS_VID
            else ([other] if vid == 0x3554 else []))
        answers = [reply(64)]
        with mock.patch.object(P.PulsarProvider, "_query", lambda s, path: answers[0]), \
                mock.patch.object(P.PulsarProvider, "_pick", lambda s, mine: mine[0]), \
                mock.patch.object(P, "output_length", lambda path: 17):
            with self.at(1000.0):
                first = {f.key: f for f in provider.poll()}
            self.assertEqual({"pulsar:3554f58a", "pulsar:14a5c010"}, set(first))
            self.assertTrue(all(f.online for f in first.values()))
            answers[0] = b""
            bus.scripts[M31R_CABLE] = []
            with self.at(1000.0 + 86400.0):
                later = provider.poll()
        self.assertEqual([("pulsar:3554f58a", 64, False)],
                         [(f.key, f.level, f.online) for f in later])

    def test_a_pulsar_id_of_vendor_14a5_that_is_not_the_m31r_is_not_limited(self):
        # the 300 s limit is for c009 only, not for the whole vendor id (nor for c010, which
        # has no offline state at all)
        with mock.patch.dict(P.PIDS[P.IROCKS_VID], {0xC011: "iRocks other"}):
            bus = self.bus({0xC011: [M31R_70_WIRED]})
            provider = P.PulsarProvider()
            with self.at(1000.0):
                self.assertEqual(1, len(provider.poll()))
            bus.scripts[0xC011] = []
            for t in (1299.0, 1301.0, 90000.0):
                with self.at(t):
                    kept = provider.poll()
                self.assertEqual([("pulsar:14a5c011", 70, False)],
                                 [(k.key, k.level, k.online) for k in kept], t)

    def test_a_greyed_reading_is_not_recorded_as_a_new_one(self):
        from halo.history import History
        history = History()
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        provider = P.PulsarProvider()
        live = provider.poll()[0]
        history.record(live.key, live.level, live.charging, live.online, 1000.0)
        self.assertEqual(1, len(history.devices[live.key]["samples"]))
        bus.online[M31R_RECEIVER] = [M31R_OFFLINE]
        for t in (1060.0, 1120.0):
            grey = provider.poll()[0]
            self.assertFalse(grey.online)
            history.record(grey.key, grey.level, grey.charging, grey.online, t)
        self.assertEqual(1, len(history.devices[live.key]["samples"]))

    def test_a_stale_power_frame_sent_with_the_online_answer_is_not_a_reading(self):
        # online, the power frame (60 %) comes along after the answer and the real battery
        # query gets no answer: the stray 60 must not be shown
        bus = self.bus({M31R_RECEIVER: []})
        bus.online[M31R_RECEIVER] = [M31R_ONLINE, M31R_STALE_60]
        self.assertEqual([], P.PulsarProvider().poll())
        # and a power frame ahead of the online answer is skipped while reading it
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        bus.online[M31R_RECEIVER] = [M31R_STALE_60, M31R_ONLINE]
        found = P.PulsarProvider().poll()
        self.assertEqual((55, True), (found[0].level, found[0].online))

    def test_only_byte_6_equal_to_1_is_online(self):
        for value, online in ((1, True), (0, False), (2, False), (0xFF, False)):
            with self.subTest(byte6=value):
                bus = self.bus({M31R_RECEIVER: [M31R_55]})
                bus.online[M31R_RECEIVER] = [with_checksum(M31R_ONLINE, {6: value})]
                found = P.PulsarProvider().poll()
                if online:
                    self.assertEqual((55, True), (found[0].level, found[0].online))
                else:
                    self.assertEqual([], found)          # offline, nothing read before
                    self.assertEqual([0x03], self.commands(bus))

    def test_an_online_answer_with_a_bad_checksum_is_offline(self):
        bad = bytearray(M31R_ONLINE)
        bad[16] ^= 0x01
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        bus.online[M31R_RECEIVER] = [bytes(bad)]
        self.assertEqual([], P.PulsarProvider().poll())
        self.assertEqual([0x03], [f[1] for _, f in bus.sent])

    def test_an_online_answer_with_byte_2_set_is_offline(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        bus.online[M31R_RECEIVER] = [with_checksum(M31R_ONLINE, {2: 1})]
        self.assertEqual([], P.PulsarProvider().poll())
        self.assertEqual([0x03], [f[1] for _, f in bus.sent])

    def test_a_power_frame_does_not_answer_the_online_query(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        bus.online[M31R_RECEIVER] = [M31R_55, M31R_75_CHARGING]
        self.assertEqual([], P.PulsarProvider().poll())

    def test_no_online_answer_is_offline(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        bus.online[M31R_RECEIVER] = []
        self.assertEqual([], P.PulsarProvider().poll())
        self.assertEqual([0x03], [f[1] for _, f in bus.sent])

    def test_events_and_zero_frames_before_the_online_answer_are_skipped(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        bus.online[M31R_RECEIVER] = [reply(30, command=0x0A, header=0x09), bytes(17), M31R_ONLINE]
        self.assertEqual(55, P.PulsarProvider().poll()[0].level)

    def test_a_refused_online_query_is_offline(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        bus.refuse = {0x03}
        self.assertEqual([], P.PulsarProvider().poll())
        self.assertEqual([0x03], [f[1] for _, f in bus.sent])

    def test_a_failing_read_of_the_online_answer_is_offline(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        bus.reads_fail = True
        self.assertEqual([], P.PulsarProvider().poll())
        self.assertEqual([0x03], [f[1] for _, f in bus.sent])

    def test_a_refused_battery_query_of_an_online_mouse_is_not_offline(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        bus.refuse = {0x04}
        self.assertEqual([], P.PulsarProvider().poll())

    def test_the_wired_mouse_is_unaffected_by_the_receivers_state(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55], M31R_CABLE: [M31R_70_WIRED]})
        bus.online[M31R_RECEIVER] = [M31R_OFFLINE]
        found = {d.key: d for d in P.PulsarProvider().poll()}
        self.assertNotIn("pulsar:14a5c009", found)                      # offline: not shown
        self.assertEqual((70, True, True), (found["pulsar:14a5c010"].level,
                                            found["pulsar:14a5c010"].charging,
                                            found["pulsar:14a5c010"].online))

    def test_the_reply_collection_is_opened_before_the_command_and_nothing_is_written(self):
        _, bus, _ = self.poll({M31R_RECEIVER: [M31R_55]})
        self.assertEqual([(M31R_RECEIVER, 0xFF01, 0x0000), (M31R_RECEIVER, 0xFF02, 0x0002)],
                         bus.opened)
        self.assertEqual([], bus.written)

    def test_an_offline_diagnostic_says_so(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        bus.online[M31R_RECEIVER] = [M31R_OFFLINE]
        provider = P.PulsarProvider()
        provider.poll()
        diag = "\n".join(provider.diagnostics())
        self.assertIn("offline or asleep", diag)

    def test_other_pulsar_devices_still_need_the_0x08_header(self):
        self.assertIsNone(P.parse_power(reply(50, header=0x09)))
        self.assertIsNone(P.parse_power(reply(50, header=0x09), 0x3554))
        self.assertEqual((50, False), P.parse_power(reply(50)))
        self.assertEqual((50, False), P.parse_power(reply(50), 0x3554))

    def test_other_pulsar_devices_still_get_an_output_report_and_ignore_header_0x09(self):
        bus = FakeBus(0x3554, 0xF58A, [reply(50, header=0x09)], shape=SHAPE)
        P.hidlist = types.SimpleNamespace(enumerate=bus.enumerate)
        P.hid = types.SimpleNamespace(device=bus.device_class())
        outputs = P.output_length
        P.output_length = lambda path: None
        try:
            self.assertEqual([], P.PulsarProvider().poll())
        finally:
            P.output_length = outputs
        self.assertEqual([(0xFF02, 0x0002)], bus.written())


if __name__ == "__main__":
    unittest.main()
