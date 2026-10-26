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
    is answered by queueing that device's script on its ff01:0000 collection; a frame with
    a bad checksum is ignored, and the command collection has no output report."""

    def __init__(self, scripts):
        self.scripts = {pid: list(frames) for pid, frames in scripts.items()}
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
                if bus.refuse_feature:
                    return -1
                if len(frame) == 17 and frame[1] == 0x04 and sum(frame) & 0xFF == 0x55:
                    bus.queues[self.pid].extend(bus.scripts[self.pid])
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
        self.assertEqual([(M31R_RECEIVER, list(M31R_REQUEST))], bus.sent)
        self.assertEqual(17, len(M31R_REQUEST))
        self.assertEqual(0x49, 0x55 - 0x08 - 0x04)
        self.assertEqual(list(M31R_REQUEST), P.make_request())
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

    def test_no_pairing_probe_is_sent(self):
        _, bus, _ = self.poll({M31R_RECEIVER: [M31R_55]})
        self.assertEqual([0x04], [frame[1] for _, frame in bus.sent])

    def test_there_is_no_millivolt_reading_for_this_model(self):
        found, _, provider = self.poll({M31R_RECEIVER: [reply(55, 0, mv=3900, header=0x09)]})
        self.assertEqual(55, found[0].level)
        self.assertNotIn("mV", "\n".join(provider.diagnostics()))

    def test_a_refused_feature_report_gives_no_device(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        bus.refuse_feature = True
        self.assertEqual([], P.PulsarProvider().poll())

    def test_a_missing_reply_collection_gives_no_device_and_sends_nothing(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        gone = [p for p, (pid, page, usage) in bus.cols.items() if (page, usage) == (0xFF01, 0)]
        for path in gone:
            del bus.cols[path]
        self.assertEqual([], P.PulsarProvider().poll())
        self.assertEqual([], bus.sent)

    def test_a_silent_mouse_keeps_its_reading_greyed(self):
        bus = self.bus({M31R_RECEIVER: [M31R_55]})
        provider = P.PulsarProvider()
        self.assertTrue(provider.poll()[0].online)
        bus.scripts[M31R_RECEIVER] = []             # asleep: no answer now
        kept = provider.poll()
        self.assertEqual(1, len(kept))
        self.assertFalse(kept[0].online)
        self.assertEqual((55, False), (kept[0].level, kept[0].charging))

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
