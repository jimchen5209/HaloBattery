"""The iRocks M31R and Preferences > "Keep disconnected devices until restart" (#151).

A fake M31R on a fake HID bus is polled with the real PulsarProvider and the results go to
the real App.apply() (with the fake icons of test_hide_rename), once with the setting off and
once on. No tray, no hardware.

What the cases show:
  * the 2.4 GHz receiver (c009) that is offline but still plugged in is still in the results
    (online=False), so the app has nothing to "keep": the provider decides what is shown, the
    same whatever the setting is;
  * when the provider stops reporting it (past 300 s, or the receiver pulled out) the setting
    decides: off removes the icon after two polls, on keeps it greyed with its last level;
  * the cable (c010) has no offline state: the provider reports it online or not at all (never
    greyed, no 300 s), so when it is unplugged or gives no reading it is missing from the results
    at once and the setting decides after 2 polls: off removes the icon, on keeps it greyed by
    the app (the one case where a cable icon shows as offline; the provider does not do it);
  * a mouse that was never seen (started offline) has no icon, so nothing is kept either way.

Run from the repository root:

    python -m unittest discover -s tests
"""
import types
import unittest
from unittest import mock

import test_pulsar as TP
from test_hide_rename import HideRenameTestCase, hb, make_app

from providers import pulsar as P

RECEIVER = "pulsar:14a5c009"
CABLE = "pulsar:14a5c010"


class M31RKeepDisconnectedTests(HideRenameTestCase):
    def setUp(self):
        super().setUp()
        self._hid, self._hidlist = P.hid, P.hidlist
        self.addCleanup(lambda: setattr(P, "hid", self._hid))
        self.addCleanup(lambda: setattr(P, "hidlist", self._hidlist))

    def start(self, keep, scripts):
        self.bus = TP.FakeM31R(scripts)
        P.hidlist = types.SimpleNamespace(enumerate=self.bus.enumerate)
        P.hid = types.SimpleNamespace(device=self.bus.device_class())
        self.provider = P.PulsarProvider()
        self.app = make_app({"keep_disconnected": keep})
        self.plugged = dict(self.bus.cols)
        self.t = 1000.0

    def poll(self, seconds=0.0, n=1):
        """n polls `seconds` after the previous step; the results go to the real apply()."""
        self.t += seconds
        for _ in range(n):
            with mock.patch.object(P.time, "time", return_value=self.t):
                results = self.provider.poll()
            self.app.apply(results)
        return results

    def title(self, key):
        return self.app.icons[key].titles[-1]

    def unplug(self):
        self.bus.cols.clear()

    def replug(self):
        self.bus.cols.update(self.plugged)

    # ------------------------------------------------------------------ the receiver (c009)

    def test_started_with_the_mouse_off_there_is_no_icon_with_either_setting(self):
        for keep in (False, True):
            with self.subTest(keep=keep):
                self.start(keep, {TP.M31R_RECEIVER: [TP.M31R_55]})
                self.bus.online[TP.M31R_RECEIVER] = [TP.M31R_OFFLINE]
                self.assertEqual([], self.poll(n=4))
                self.assertNotIn(RECEIVER, self.app.icons)
                self.poll(400.0, n=3)
                self.assertNotIn(RECEIVER, self.app.icons)

    def test_offline_after_online_the_icon_stays_greyed_with_either_setting(self):
        # still in the results (online=False): the provider's greyed reading, not the setting
        for keep in (False, True):
            with self.subTest(keep=keep):
                self.start(keep, {TP.M31R_RECEIVER: [TP.M31R_55]})
                self.poll()
                ic = self.app.icons[RECEIVER]
                self.assertEqual("iRocks M31R (2.4 GHz): 55%", self.title(RECEIVER))
                self.bus.online[TP.M31R_RECEIVER] = [TP.M31R_OFFLINE]
                results = self.poll(60.0, n=4)
                self.assertEqual([(RECEIVER, 55, False)], [(r.key, r.level, r.online) for r in results])
                self.assertIs(self.app.icons[RECEIVER], ic)
                self.assertFalse(ic.stopped)
                self.assertEqual("iRocks M31R (2.4 GHz): 55% (last known value, device asleep)",
                                 self.title(RECEIVER))
                self.assertFalse(any(self.app.missing.values()), "never counted as missing")

    def test_past_300_s_the_setting_decides(self):
        for keep in (False, True):
            with self.subTest(keep=keep):
                self.start(keep, {TP.M31R_RECEIVER: [TP.M31R_55]})
                self.poll()
                ic = self.app.icons[RECEIVER]
                self.bus.online[TP.M31R_RECEIVER] = [TP.M31R_OFFLINE]
                self.poll(299.0)
                self.assertFalse(ic.stopped)
                self.poll(2.0)                      # 301 s after the online reading: not reported
                self.assertFalse(self.provider._last)
                self.poll(n=2)
                if keep:
                    self.assertIs(self.app.icons[RECEIVER], ic)
                    self.assertFalse(ic.stopped)
                    self.assertEqual((55, False), (ic.status.level, ic.status.online))
                    self.assertIn("last known", self.title(RECEIVER))
                else:
                    self.assertNotIn(RECEIVER, self.app.icons)
                    self.assertTrue(ic.stopped)

    def test_the_mouse_back_after_300_s_updates_the_same_kept_icon(self):
        self.start(True, {TP.M31R_RECEIVER: [TP.M31R_55]})
        self.poll()
        ic = self.app.icons[RECEIVER]
        self.bus.online[TP.M31R_RECEIVER] = [TP.M31R_OFFLINE]
        self.poll(400.0, n=3)
        self.bus.online[TP.M31R_RECEIVER] = [TP.M31R_ONLINE]
        self.bus.scripts[TP.M31R_RECEIVER] = [TP.M31R_75_CHARGING]
        self.poll(10.0)
        self.assertIs(self.app.icons[RECEIVER], ic)
        self.assertEqual((75, True, True), (ic.status.level, ic.status.charging, ic.status.online))

    def test_the_receiver_pulled_out_is_the_path_the_setting_acts_on(self):
        for keep in (False, True):
            with self.subTest(keep=keep):
                self.start(keep, {TP.M31R_RECEIVER: [TP.M31R_55]})
                self.poll()
                ic = self.app.icons[RECEIVER]
                self.unplug()
                self.assertEqual([], self.poll(5.0, n=3))      # gone from the results
                if keep:
                    self.assertIs(self.app.icons[RECEIVER], ic)
                    self.assertEqual((55, False), (ic.status.level, ic.status.online))
                    self.assertIn("last known", self.title(RECEIVER))
                else:
                    self.assertNotIn(RECEIVER, self.app.icons)
                    self.assertTrue(ic.stopped)

    def test_pulled_out_and_plugged_in_again_with_the_mouse_off(self):
        # the provider forgets the reading (nothing reported); the app, with the setting on,
        # keeps the old greyed icon instead of making a new one
        for keep in (False, True):
            with self.subTest(keep=keep):
                self.start(keep, {TP.M31R_RECEIVER: [TP.M31R_55]})
                self.poll()
                self.bus.online[TP.M31R_RECEIVER] = [TP.M31R_OFFLINE]
                self.poll(30.0)
                self.unplug()
                self.poll(5.0, n=3)
                self.replug()
                self.assertEqual([], self.poll(5.0, n=3))
                self.assertEqual(keep, RECEIVER in self.app.icons)
                self.bus.online[TP.M31R_RECEIVER] = [TP.M31R_ONLINE]
                self.poll(5.0)
                self.assertEqual(55, self.app.icons[RECEIVER].status.level)
                self.assertTrue(self.app.icons[RECEIVER].status.online)

    def test_a_greyed_icon_raises_no_alert_with_either_setting(self):
        for keep in (False, True):
            with self.subTest(keep=keep):
                self.start(keep, {TP.M31R_RECEIVER: [TP.M31R_55]})
                self.app.cfg["low"] = 90
                self.poll()
                self.app.notes.clear()
                self.bus.online[TP.M31R_RECEIVER] = [TP.M31R_OFFLINE]
                self.poll(30.0, n=3)
                self.poll(400.0, n=3)
                self.assertEqual([], self.app.notes)

    # ------------------------------------------------------------------ the cable (c010)
    # The provider never reports the cable greyed: a poll without a good reading reports nothing
    # for it. What the icon does then is the app's rule only (apply(): a shown device missing
    # from the results for 2 polls in a row): setting off removes it, setting on keeps it greyed
    # by the app (online=False, charging cleared). The cable is the one M31R device whose
    # greyed icon can only come from the app.

    def silence_cable(self):
        self.bus.scripts[TP.M31R_CABLE] = []

    def test_the_provider_never_reports_the_cable_greyed(self):
        self.start(False, {TP.M31R_CABLE: [TP.M31R_70_WIRED]})
        self.assertEqual([(CABLE, 70, True, True)],
                         [(r.key, r.level, r.charging, r.online) for r in self.poll()])
        self.silence_cable()
        for seconds in (1.0, 60.0, 299.0, 2.0, 400.0, 3600.0):
            self.assertEqual([], self.poll(seconds))
        self.assertEqual({}, self.provider._last)

    def test_the_cable_unplugged_follows_the_setting(self):
        # not enumerated: the provider has nothing, the app decides (cable unplugged)
        for keep in (False, True):
            with self.subTest(keep=keep):
                self.start(keep, {TP.M31R_CABLE: [TP.M31R_70_WIRED]})
                self.poll()
                ic = self.app.icons[CABLE]
                self.assertEqual((70, True, True), (ic.status.level, ic.status.charging, ic.status.online))
                self.unplug()
                self.assertEqual([], self.poll(5.0))
                self.assertIs(self.app.icons[CABLE], ic, "one missing poll changes nothing")
                self.assertTrue(ic.status.online)
                self.poll(5.0)
                if keep:
                    self.assertIs(self.app.icons[CABLE], ic)
                    self.assertEqual((70, False, False), (ic.status.level, ic.status.charging, ic.status.online))
                    self.assertIn("last known", self.title(CABLE))
                else:
                    self.assertNotIn(CABLE, self.app.icons)
                    self.assertTrue(ic.stopped)

    def test_a_cable_that_is_plugged_in_but_gives_no_reading_follows_the_setting_too(self):
        # the app cannot tell this from an unplugged cable: both are "missing from the results"
        for keep in (False, True):
            for what, setup in self.cable_failures():
                with self.subTest(keep=keep, what=what):
                    self.start(keep, {TP.M31R_CABLE: [TP.M31R_70_WIRED]})
                    self.poll()
                    ic = self.app.icons[CABLE]
                    setup(self.bus)
                    self.assertEqual([], self.poll(5.0))
                    self.assertIs(self.app.icons[CABLE], ic)
                    self.assertTrue(ic.status.online, "after one missed poll it is still live")
                    self.assertEqual([], self.poll(5.0))
                    if keep:
                        self.assertIs(self.app.icons[CABLE], ic)
                        self.assertFalse(ic.stopped)
                        self.assertEqual((70, False, False),
                                         (ic.status.level, ic.status.charging, ic.status.online))
                    else:
                        self.assertNotIn(CABLE, self.app.icons)
                        self.assertTrue(ic.stopped)
                    # the provider itself never greyed it, nor remembered it, at any time
                    self.assertEqual({}, self.provider._last)

    cable_failures = TP.M31RTest.cable_failures

    def test_one_failed_cable_poll_between_good_ones_changes_nothing_with_either_setting(self):
        for keep in (False, True):
            with self.subTest(keep=keep):
                self.start(keep, {TP.M31R_CABLE: [TP.M31R_70_WIRED]})
                self.poll()
                ic = self.app.icons[CABLE]
                for _ in range(3):
                    self.silence_cable()
                    self.poll(3.0)
                    self.bus.scripts[TP.M31R_CABLE] = [TP.M31R_70_WIRED]
                    self.poll(3.0)
                    self.assertIs(self.app.icons[CABLE], ic)
                    self.assertTrue(ic.status.online)
                    self.assertFalse(any(self.app.missing.values()))

    def test_a_cable_that_answers_again_gets_a_live_icon_with_either_setting(self):
        for keep in (False, True):
            with self.subTest(keep=keep):
                self.start(keep, {TP.M31R_CABLE: [TP.M31R_70_WIRED]})
                self.poll()
                ic = self.app.icons[CABLE]
                self.silence_cable()
                self.poll(400.0, n=3)
                self.bus.scripts[TP.M31R_CABLE] = [TP.M31R_55]
                self.poll(10.0)
                new = self.app.icons[CABLE]
                self.assertEqual(keep, new is ic, "setting on: the kept icon is updated, off: a new one")
                self.assertEqual((55, False, True), (new.status.level, new.status.charging, new.status.online))
                self.assertEqual("iRocks M31R (wired): 55%", self.title(CABLE))

    def test_a_cable_that_never_answered_has_no_icon_with_either_setting(self):
        for keep in (False, True):
            with self.subTest(keep=keep):
                self.start(keep, {TP.M31R_CABLE: []})
                self.poll(n=3)
                self.assertNotIn(CABLE, self.app.icons)

    def test_a_greyed_cable_icon_raises_no_alert_with_either_setting(self):
        for keep in (False, True):
            with self.subTest(keep=keep):
                self.start(keep, {TP.M31R_CABLE: [TP.M31R_70_WIRED]})
                self.app.cfg["low"] = 90
                self.poll()
                self.app.notes.clear()
                self.silence_cable()
                self.poll(30.0, n=4)
                self.assertEqual([], self.app.notes)

    def test_the_receiver_asleep_and_the_cable_silent_each_follow_their_own_rule(self):
        # receiver: provider greys it for 300 s, then the setting; cable: the setting at once
        for keep in (False, True):
            with self.subTest(keep=keep):
                self.start(keep, {TP.M31R_RECEIVER: [TP.M31R_55], TP.M31R_CABLE: [TP.M31R_70_WIRED]})
                self.poll()
                r_ic, c_ic = self.app.icons[RECEIVER], self.app.icons[CABLE]
                self.bus.online[TP.M31R_RECEIVER] = [TP.M31R_OFFLINE]
                self.silence_cable()
                results = self.poll(10.0, n=3)
                self.assertEqual([(RECEIVER, 55, False)], [(r.key, r.level, r.online) for r in results])
                self.assertIs(self.app.icons[RECEIVER], r_ic)
                self.assertFalse(r_ic.status.online)
                self.assertEqual(keep, CABLE in self.app.icons)
                self.assertEqual(not keep, c_ic.stopped)
                self.poll(400.0, n=3)
                self.assertEqual(keep, RECEIVER in self.app.icons)    # past 300 s: the setting
                self.assertEqual(keep, CABLE in self.app.icons)

    # ------------------------------------------------------------------ where the setting is

    def test_the_setting_is_off_by_default_and_has_this_menu_text(self):
        self.assertFalse(hb.DEFAULTS["keep_disconnected"])
        with open(hb.__file__, encoding="utf-8") as f:
            self.assertIn('Item("Keep disconnected devices until restart", toggle("keep_disconnected"),',
                          f.read())


if __name__ == "__main__":
    unittest.main()
